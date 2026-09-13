"""Slack - tell the sales team while the call is still live.

The lead never touches Slack. The agent posts into the team's own channel with a
bot token, and each call owns ONE message that is edited in place as the call
develops - hot, callback booked, CRM record created, call ended - rather than a
stream of posts that bury each other. The message `ts` lives on the call row: if
it exists we update, never post. That is Slack's duplicate guard, independent of
the action bus.

The one thing that is NOT an edit is an escalation. An edited message notifies
nobody, and "the lead wants a person" needs someone to look now, so it goes out
as a threaded reply broadcast to the channel - once per call.
"""

import logging
import threading
from datetime import datetime

from . import callfacts, db, hubspot, rest
from .config import settings
from .db import IST

log = logging.getLogger("elevatebox.slack")

API = "https://slack.com/api/"

INTENT = {"hot": ":fire: Hot", "warm": ":large_yellow_circle: Warm", "cold": ":snowflake: Cold"}
AVAILABILITY_NOTE = {
    "unchecked": " _(calendar not checked)_",
    "busy_confirmed": " _(slot was taken - booked at their request)_",
}

_LOCKS = {}


class SlackError(RuntimeError):
    pass


def configured():
    missing = [name for name, value in (("SLACK_BOT_TOKEN", settings.slack_bot_token),
                                        ("SLACK_CHANNEL_ID", settings.slack_channel_id))
               if not value]
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, f"channel {settings.slack_channel_id}"


def _request(method, body, form=False, timeout=10):
    """The single transport. The smoke test replaces this."""
    data = rest.call("slack", "POST", API + method,
                     headers={"Authorization": "Bearer " + settings.slack_bot_token},
                     json_body=None if form else body, form=body if form else None,
                     timeout=timeout)
    if not data.get("ok"):
        # Slack answers HTTP 200 with ok=false. "ratelimited" is worth a retry;
        # not_in_channel, invalid_auth and channel_not_found are configuration.
        raise SlackError(f"slack {method}: {data.get('error')}"
                         + (f" (needs {data['needed']})" if data.get("needed") else ""))
    return data


def _lock(call_id):
    return _LOCKS.setdefault(call_id, threading.Lock())


def _esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def compose(call_id):
    """(notification text, blocks) for the call's one message, from current state."""
    f = callfacts.read(call_id)
    call = f["call"]
    if f["live"] and f["label"] == "hot":
        head = ":telephone_receiver: *Hot lead on the line now*"
    elif f["live"]:
        head = ":telephone_receiver: *Call in progress*"
    else:
        head = ":white_check_mark: *Call ended*"
    text = f"{head} — {_esc(f['who'] or 'Unnamed lead')} · {_esc(f['phone'])}"
    if f["sentence"]:
        text += "\n" + _esc(f["sentence"][0].upper() + f["sentence"][1:]) + "."
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    fields = [f"*Intent*\n{INTENT.get(f['label'], ':hourglass_flowing_sand: reading...')}"]
    if f["barrier"]:
        fields.append(f"*Barrier*\n{_esc(f['barrier'])}")
    if f["callback_when"]:
        callback = f"*Callback*\n{f['callback_when']}"
        if call.get("gcal_event_link"):
            callback += f" · <{call['gcal_event_link']}|Calendar>"
        fields.append(callback + AVAILABILITY_NOTE.get(f["callback"].get("availability"), ""))
    deal = hubspot.deal_url(call.get("hubspot_deal_id"))
    contact = hubspot.contact_url(call.get("hubspot_contact_id"))
    links = [f"<{deal}|Deal>" if deal else None, f"<{contact}|Contact>" if contact else None]
    if any(links):
        fields.append("*HubSpot*\n" + " · ".join(l for l in links if l))
    blocks.append({"type": "section",
                   "fields": [{"type": "mrkdwn", "text": t} for t in fields]})

    if f["evidence"]:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"> “{_esc(f['evidence'])}”"}})
    flagged = db.latest_event_payload(call_id, "escalation.detected")
    if flagged:
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": f":rotating_light: *Needs a human* — {_esc(flagged.get('reason'))}"}})

    context = [f"<{f['call_view']}|Open call view>"] if f["call_view"] else []
    context.append(f"Relay · updated {datetime.now(IST):%H:%M} IST")
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": " · ".join(context)}]})
    return text, blocks


def sync_call(call_id, moment=None):
    """Post the call's message, or edit it if it exists. Blocking."""
    with _lock(call_id):
        call = db.get_call(call_id)
        if not call:
            raise RuntimeError(f"no such call {call_id}")
        text, blocks = compose(call_id)
        if call.get("slack_ts"):
            ts, channel = call["slack_ts"], call["slack_channel"]
            _request("chat.update", {"channel": channel, "ts": ts,
                                     "text": text, "blocks": blocks})
        else:
            data = _request("chat.postMessage", {
                "channel": settings.slack_channel_id, "text": text, "blocks": blocks,
                "unfurl_links": False, "unfurl_media": False})
            ts, channel = data["ts"], data["channel"]
            db.update_call(call_id, slack_ts=ts, slack_channel=channel)
            log.info("call %s: Slack alert posted (%s)", call_id, moment)
        if moment == "escalation":
            _escalate(call_id, channel, ts)
        return {"message_id": ts}


def _escalate(call_id, channel, ts):
    """A NEW post, threaded and broadcast. Guarded by an event so a retry cannot
    page the team twice."""
    if db.has_event(call_id, "slack.escalation_posted"):
        return
    flagged = db.latest_event_payload(call_id, "escalation.detected") or {}
    _request("chat.postMessage", {
        "channel": channel, "thread_ts": ts, "reply_broadcast": True,
        "text": (f":rotating_light: <!here> Needs a human on this call — "
                 f"{_esc(flagged.get('reason', 'escalation'))}: "
                 f"“{_esc(flagged.get('quote', ''))}”")})
    db.add_event(call_id, "slack.escalation_posted", {"ts": ts})


def refresh(call_id):
    """Edit the call's message to current state, if one was posted. Never posts
    and never raises - a Slack hiccup must not fail the calendar or CRM action
    that asked for the refresh."""
    try:
        call = db.get_call(call_id) or {}
        if configured()[0] and call.get("slack_ts"):
            sync_call(call_id)
            return True
    except Exception as exc:
        log.warning("slack refresh failed for %s: %s", call_id, exc)
    return False


def check():
    """Live check for the Integrations view. Read-only."""
    ok, why = configured()
    if not ok:
        return {"ok": False, "detail": why}
    try:
        data = _request("auth.test", {}, form=True)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}
    return {"ok": True, "detail": f"bot @{data.get('user')} in {data.get('team')}, "
                                  f"posting to {settings.slack_channel_id}"}
