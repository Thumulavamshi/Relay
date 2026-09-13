"""Google Calendar - check the rep's calendar, then book the callback.

Not a write-only sink. `schedule_callback` asks freeBusy whether the slot the
lead just named is actually free, and the answer changes what the agent says:

    free        book it; the event follows in the background
    busy        book NOTHING; hand the agent the next free slot to offer
    no answer   book anyway, flagged `unchecked`

freeBusy runs INSIDE a synchronous tool call while the lead is waiting, so it
has a hard timeout (GCAL_FREEBUSY_TIMEOUT, default 0.8 s) and the access token
is warmed at startup. A slow calendar must never cost the conversation: an
unchecked booking can be fixed by a rep, dead air cannot be taken back.

Duplicate guard of its own, independent of the action bus: the event id is
derived from the CALL id, so a call owns exactly one event. A retry after a
success we never saw gets 409 and becomes an update, and a lead who restates
the time moves the event rather than leaving a stale one behind.

Auth is a refresh token for the rep's own Google account. Stdlib HTTP only.
"""

import hashlib
import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import callfacts, db, hubspot, rest
from .config import settings
from .db import IST
from .timeparse import WINDOW

log = logging.getLogger("elevatebox.gcal")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

SLOT_MINUTES = int(os.environ.get("GCAL_SLOT_MINUTES", "30"))
FREEBUSY_TIMEOUT = float(os.environ.get("GCAL_FREEBUSY_TIMEOUT", "0.8"))
# How far past the requested time to look for an alternative slot.
SEARCH_DAYS = int(os.environ.get("GCAL_SEARCH_DAYS", "3"))

# Google event ids: base32hex only (0-9, a-v), 5 to 1024 characters.
_B32HEX = "0123456789abcdefghijklmnopqrstuv"

_TOKEN = {"value": None, "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()
_LOCKS = {}


def configured():
    missing = [name for name, value in (
        ("GOOGLE_CLIENT_ID", settings.google_client_id),
        ("GOOGLE_CLIENT_SECRET", settings.google_client_secret),
        ("GOOGLE_REFRESH_TOKEN", settings.google_refresh_token)) if not value]
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, f"calendar {settings.google_calendar_id}"


def _request(method, url, body=None, form=None, timeout=10, auth=True):
    """The single transport. The smoke test replaces this, so nothing below it
    can reach Google from a test run."""
    headers = {"Authorization": "Bearer " + access_token(timeout)} if auth else {}
    return rest.call("google", method, url, headers=headers, json_body=body,
                     form=form, timeout=timeout)


def _lock(call_id):
    return _LOCKS.setdefault(call_id, threading.Lock())


# ----------------------------------------------------------------- auth

def access_token(timeout=10):
    """Cached access token, refreshed a minute before it expires."""
    with _TOKEN_LOCK:
        if _TOKEN["value"] and time.time() < _TOKEN["expires_at"] - 60:
            return _TOKEN["value"]
        data = _request("POST", TOKEN_URL, form={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": settings.google_refresh_token,
            "grant_type": "refresh_token",
        }, timeout=timeout, auth=False)
        _TOKEN["value"] = data["access_token"]
        _TOKEN["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        return _TOKEN["value"]


def warm():
    """Fetch the token at startup, so the first freeBusy inside a live tool call
    is one round trip rather than two. Never raises."""
    if not configured()[0]:
        return False
    try:
        access_token()
        return True
    except Exception as exc:
        log.warning("google token warm-up failed: %s", exc)
        return False


# ----------------------------------------------------------------- availability

def _cal_path():
    return "/calendars/" + urllib.parse.quote(settings.google_calendar_id, safe="")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def busy_blocks(start, end, timeout=FREEBUSY_TIMEOUT):
    """[(start, end)] busy intervals on the rep's calendar, as aware datetimes."""
    data = _request("POST", API + "/freeBusy", body={
        "timeMin": _iso(start), "timeMax": _iso(end), "timeZone": "Asia/Kolkata",
        "items": [{"id": settings.google_calendar_id}],
    }, timeout=timeout)
    calendars = data.get("calendars") or {}
    cal = calendars.get(settings.google_calendar_id)
    if cal is None and len(calendars) == 1:
        cal = next(iter(calendars.values()))
    cal = cal or {}
    if cal.get("errors"):
        # A wrong calendar id comes back as HTTP 200 with an errors list. Without
        # this check it would read as a calendar that is always free.
        raise rest.ApiError("google", 200, f"freeBusy: {cal['errors']}")
    return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in cal.get("busy", [])]


def _overlaps(blocks, start, end):
    return any(b_start < end and start < b_end for b_start, b_end in blocks)


def next_free(after, blocks, minutes=SLOT_MINUTES):
    """First free slot strictly after `after`, on the half hour, inside our
    calling window (timeparse.WINDOW). None if nothing fits in SEARCH_DAYS."""
    step, length = timedelta(minutes=30), timedelta(minutes=minutes)
    t = after.astimezone(IST).replace(second=0, microsecond=0)
    t = t - timedelta(minutes=t.minute % 30) + step
    horizon = after + timedelta(days=SEARCH_DAYS)
    while t < horizon:
        closes = t.replace(hour=WINDOW[1], minute=0)
        if t.hour >= WINDOW[0] and t + length <= closes and not _overlaps(blocks, t, t + length):
            return t
        t += step
    return None


class Availability:
    """free | busy | unchecked | not_configured, plus the slot to offer if busy."""

    def __init__(self, status, alternative=None, detail=""):
        self.status = status
        self.alternative = alternative
        self.detail = detail

    def __repr__(self):
        return f"<Availability {self.status} alt={self.alternative}>"


def availability(start, minutes=SLOT_MINUTES, timeout=FREEBUSY_TIMEOUT):
    """Is `start` free? One freeBusy call answers both "is it free" and "if not,
    what is". Never raises - an unanswered check books anyway, flagged."""
    try:
        blocks = busy_blocks(start, start + timedelta(days=SEARCH_DAYS), timeout=timeout)
    except Exception as exc:
        log.warning("freeBusy did not answer (%s) - booking unchecked", exc)
        return Availability("unchecked", detail=str(exc)[:200])
    if not _overlaps(blocks, start, start + timedelta(minutes=minutes)):
        return Availability("free")
    return Availability("busy", alternative=next_free(start, blocks, minutes))


# ----------------------------------------------------------------- events

def event_id(call_id):
    """Derived from the call id, so one call can never own two events."""
    n = int.from_bytes(hashlib.sha1(f"relay:{call_id}".encode()).digest(), "big")
    out = []
    while n:
        n, r = divmod(n, 32)
        out.append(_B32HEX[r])
    return "rl" + "".join(out)


def upsert_event(call_id, start, summary, description, minutes=SLOT_MINUTES):
    """Create the call's event, or move it if it exists. Returns {id, link, created}."""
    eid = event_id(call_id)
    start_ist = start.astimezone(IST)
    body = {
        "id": eid,
        "summary": summary,
        "description": description,
        "status": "confirmed",
        "start": {"dateTime": start_ist.isoformat(timespec="seconds"),
                  "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": (start_ist + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                "timeZone": "Asia/Kolkata"},
        "extendedProperties": {"private": {"relay_call_id": call_id}},
    }
    events = API + _cal_path() + "/events"
    try:
        event, created = _request("POST", events, body=body), True
    except rest.ApiError as exc:
        if exc.status != 409:
            raise
        # Already exists: a retry after an unseen success, a restated time, or a
        # replay. PUT the whole body - it moves the time and revives a deleted one.
        event, created = _request("PUT", f"{events}/{eid}", body=body), False
    return {"id": event.get("id", eid), "link": event.get("htmlLink"), "created": created}


def delete_event(call_id):
    """Remove the call's event. Used by replay-harness cleanup."""
    try:
        _request("DELETE", API + _cal_path() + "/events/" + event_id(call_id))
        return True
    except rest.ApiError as exc:
        if exc.status in (404, 410):
            return False
        raise


def _event_text(facts, row):
    who = facts["who"] or facts["phone"]
    summary = f"Callback: {who}" + (f" — {facts['products']}" if facts["products"] else "")
    lines = ["Booked by Relay during a live call.",
             f"Lead: {facts['who'] or 'name not given'} · {facts['phone']}"]
    if row.get("spoken_phrase"):
        lines.append(f"They asked for: “{row['spoken_phrase']}”")
    lines += [f"{title}: {value}" for title, value in facts["facts"]]
    if facts["label"]:
        lines.append(f"Intent: {facts['label']}"
                     + (f" (barrier: {facts['barrier']})" if facts["barrier"] else ""))
    if facts["evidence"]:
        lines.append(f"In their words: “{facts['evidence']}”")
    deal = hubspot.deal_url(facts["call"].get("hubspot_deal_id"))
    if deal:
        lines.append(f"HubSpot deal: {deal}")
    if facts["call_view"]:
        lines.append(f"Call view: {facts['call_view']}")
    return summary[:250], "\n".join(lines)


def sync_call(call_id):
    """Put the call's one event where its standing callback is. Blocking.

    Reads the LATEST standing callback rather than the one that triggered the
    action, so two bookings racing (the lead restated the time mid-flight)
    converge on the newer time instead of on whichever request landed last.
    """
    with _lock(call_id):
        facts = callfacts.read(call_id)
        row = facts["callback"]
        if row is None:
            return {"skipped": "no standing callback"}
        summary, description = _event_text(facts, row)
        event = upsert_event(call_id, datetime.fromisoformat(row["resolved_at_utc"]),
                             summary, description)
        db.update_call(call_id, gcal_event_id=event["id"], gcal_event_link=event["link"])
        log.info("call %s: calendar event %s %s", call_id, event["id"],
                 "created" if event["created"] else "updated")
        return {"message_id": event["id"], "link": event["link"], "created": event["created"]}


def check():
    """Live check for the Integrations view. Read-only."""
    ok, why = configured()
    if not ok:
        return {"ok": False, "detail": why}
    try:
        cal = _request("GET", API + _cal_path())
        now = datetime.now(timezone.utc)
        busy_blocks(now, now + timedelta(hours=1), timeout=5)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}
    return {"ok": True, "detail": f"calendar reachable, freeBusy answers, "
                                  f"time zone {cal.get('timeZone')}"}
