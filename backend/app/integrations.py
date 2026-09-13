"""The multi-app layer: Google Calendar, HubSpot and Slack on the action bus.

Every app action is an ordinary action-bus row, so it inherits what the bus
already guarantees - background execution, retries on transient failures, and a
ledger row saying what fired, why, and whether it worked. What this module adds
is WHEN each one fires, and one rule the bus alone cannot give:

    The bus key is `call:type` - once per call. Apps need more than once: the
    deal moves as the read changes, the calendar event moves when the lead
    restates a time, Slack edits its message. So each trigger carries a key
    suffix naming the MOMENT (`crm_sync:hot`, `calendar_event:cb12`), and every
    handler CONVERGES its app to the call's current state instead of appending.
    A repeated moment is a no-op on the bus; a repeated handler run is a no-op
    in the app.

Each app also keeps its own duplicate guard (ids on the call row, a derived
event id), because the bus protects against our double-dispatch, not against a
network retry after a success we never saw.

An app with no credentials is never dispatched to, so a missing token reads as
"not configured" on /health rather than a ledger full of failures.
"""

import asyncio
import logging

from . import classifier, db, escalation, gcal, hubspot, slack
from .actions import dispatch, handler

log = logging.getLogger("elevatebox.integrations")

APPS = {"google_calendar": gcal, "hubspot": hubspot, "slack": slack}
APP_OF_ACTION = {"calendar_event": "google_calendar", "crm_sync": "hubspot",
                 "crm_note": "hubspot", "team_alert": "slack"}


def _on(app):
    return app.configured()[0]


# ----------------------------------------------------------------- triggers

def on_classified(call_id, background=None):
    """After a classification pass. Cheap when nothing moved - the keys dedupe."""
    read = db.latest_classification(call_id)
    if not read:
        return
    label = read["label"]
    if _on(hubspot):
        dispatch(call_id, "crm_sync", payload={"moment": label},
                 trigger_source="classification", key_suffix=label, background=background)
    if label == "hot" and _on(slack):
        dispatch(call_id, "team_alert", payload={"moment": "hot"},
                 trigger_source="classification", key_suffix="hot", background=background)


def on_lead_turn(call_id, text, seq):
    """Escalation check on every lead turn. Recorded whether or not Slack is
    connected, so the call view shows it either way. First detection only."""
    reason = escalation.detect(text)
    if not reason or db.has_event(call_id, "escalation.detected"):
        return None
    db.add_event(call_id, "escalation.detected",
                 {"reason": reason, "quote": (text or "")[:300], "at_turn_seq": seq})
    log.warning("call %s: escalation - %s", call_id, reason)
    if _on(slack):
        dispatch(call_id, "team_alert", payload={"moment": "escalation"},
                 trigger_source="escalation", key_suffix="escalation")
    return reason


def on_callback_booked(call_id, callback_id, background=None):
    """A booking moves all three apps: the event, the deal stage, the message."""
    suffix = f"cb{callback_id}"
    for app, action in ((gcal, "calendar_event"), (hubspot, "crm_sync"), (slack, "team_alert")):
        if _on(app):
            dispatch(call_id, action, payload={"moment": "callback", "callback_id": callback_id},
                     trigger_source="callback_booked", key_suffix=suffix, background=background)


def on_call_ended(call_id, conversation):
    """The CRM note for any real conversation; Slack closes out any call it
    announced, and announces any conversation it had not."""
    call = db.get_call(call_id) or {}
    if conversation and _on(hubspot):
        dispatch(call_id, "crm_note", payload={"moment": "ended"}, trigger_source="post_call")
    if _on(slack) and (conversation or call.get("slack_ts")):
        dispatch(call_id, "team_alert", payload={"moment": "ended"},
                 trigger_source="post_call", key_suffix="ended")


def refresh_team_alert(call_id):
    return slack.refresh(call_id)


# ----------------------------------------------------------------- handlers
#
# Calendar and CRM handlers finish by refreshing the Slack message, so the
# links they just created appear in the channel without a second post.

@handler("calendar_event")
async def calendar_event(call_id, payload):
    result = await asyncio.to_thread(gcal.sync_call, call_id)
    await asyncio.to_thread(slack.refresh, call_id)
    return result


@handler("crm_sync")
async def crm_sync(call_id, payload):
    result = await asyncio.to_thread(hubspot.sync_call, call_id)
    await asyncio.to_thread(slack.refresh, call_id)
    return result


@handler("crm_note")
async def crm_note(call_id, payload):
    result = await asyncio.to_thread(hubspot.sync_call, call_id, True)
    await asyncio.to_thread(slack.refresh, call_id)
    return result


@handler("team_alert")
async def team_alert(call_id, payload):
    return await asyncio.to_thread(slack.sync_call, call_id, payload.get("moment"))


# ----------------------------------------------------------------- status

def status_report():
    """Per app: configured, and how its most recent action went. For /health."""
    latest = db.latest_action_per_type(tuple(APP_OF_ACTION))
    report = {}
    for name, app in APPS.items():
        ok, detail = app.configured()
        mine = sorted((row for t, row in latest.items() if APP_OF_ACTION[t] == name),
                      key=lambda row: row["requested_at"], reverse=True)
        last = None
        if mine:
            row = mine[0]
            last = {"action": row["type"], "status": row["status"], "at": row["requested_at"],
                    "error": (row["error"] or "").split("\n")[0][:200] or None}
        report[name] = {"configured": ok, "detail": detail, "last_action": last}
    provider = classifier.resolve_provider()
    ready, detail = classifier.available(provider)
    report["understanding_model"] = {"provider": provider,
                                     "model": classifier.model_for(provider),
                                     "ready": ready, "detail": detail}
    return report


async def live_check():
    """Read-only calls to every app, concurrently."""
    results = await asyncio.gather(*(asyncio.to_thread(app.check) for app in APPS.values()))
    return dict(zip(APPS, results))
