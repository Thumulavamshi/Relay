"""Read models for the web UI.

The pages poll these, and they are built from the same rows the apps were driven
from - so a badge can never show an action the ledger does not hold.
"""

from datetime import datetime, timezone

from . import callfacts, db, hubspot
from .config import digits_of, settings

LIVE = ("initiating", "queued", "ringing", "in-progress")

APP_ACTIONS = {
    "calendar": ("calendar_event",),
    "hubspot": ("crm_sync", "crm_note"),
    "slack": ("team_alert",),
    "whatsapp": ("whatsapp_hot", "whatsapp_followup", "whatsapp_resume", "callback_confirm"),
}

# HACKATHON_PLAN.md, "Reliability": about 18% of calls delivered no caller audio
# even though the carrier saw healthy RTP. These are its two signatures. A flag
# only - never an automatic redial, which would place a second real call.
SILENT_RING_SECONDS = 15
NO_AUDIO_SECONDS = 20


def _at(iso):
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mask(number):
    """'+91 ••••• 2858' - enough to recognise the number, not enough to lift it."""
    digits = digits_of(number)
    if len(digits) < 10:
        return ""
    return f"+{digits[:-10]} ••••• {digits[-4:]}" if digits[:-10] else f"••••• {digits[-4:]}"


def app_status(actions, types):
    """The most recent action's status for one app, or None if it never fired."""
    rows = [a for a in actions if a["type"] in types]
    return rows[-1]["status"] if rows else None


def flags(call, turns, now=None):
    now = now or datetime.now(timezone.utc)
    out = []
    created, answered = _at(call.get("created_at")), _at(call.get("answered_at"))
    ended = _at(call.get("ended_at"))
    lead = sum(1 for t in turns if t["role"] == "user")
    agent = len(turns) - lead
    if (call.get("status") in ("queued", "ringing") and created
            and (now - created).total_seconds() > SILENT_RING_SECONDS):
        out.append("ringing_silently")
    if (answered and lead == 0 and agent >= 1
            and ((ended or now) - answered).total_seconds() >= NO_AUDIO_SECONDS):
        out.append("suspected_no_audio")
    if db.has_event(call["id"], "escalation.detected"):
        out.append("escalation")
    return out


def call_summary(call):
    """One row of the Calls list."""
    call_id = call["id"]
    facts = callfacts.read(call_id)
    turns = db.get_turns(call_id)
    actions = db.get_actions(call_id)
    start = _at(call.get("answered_at") or call.get("started_at"))
    end = _at(call.get("ended_at"))
    return {
        "id": call_id,
        "created_at": call["created_at"],
        "status": call["status"],
        "ended_reason": call.get("ended_reason"),
        "destination_masked": mask(call.get("destination")),
        "who": facts["who"],
        "products": facts["products"],
        "label": facts["label"],
        "duration_s": round((end - start).total_seconds()) if start and end else None,
        "turns": len(turns),
        "callback_when": facts["callback_when"],
        "apps": {name: app_status(actions, types) for name, types in APP_ACTIONS.items()},
        "flags": flags(call, turns),
    }


def detail_extras(call_id):
    """What the call page adds to GET /calls/{id}: app deep links, flags, checks."""
    call = db.get_call(call_id) or {}
    turns = db.get_turns(call_id)
    actions = db.get_actions(call_id)
    stage = call.get("hubspot_deal_stage")
    return {
        "links": {
            "calendar": call.get("gcal_event_link"),
            "hubspot_deal": hubspot.deal_url(call.get("hubspot_deal_id")),
            "hubspot_contact": hubspot.contact_url(call.get("hubspot_contact_id")),
            "slack_channel": (f"https://slack.com/app_redirect?channel={call['slack_channel']}"
                              if call.get("slack_channel") else None),
        },
        "deal_stage": ("hot" if stage and stage == settings.hubspot_stage_hot
                       else "warm + callback" if stage and stage == settings.hubspot_stage_warm
                       else stage),
        "apps": {name: app_status(actions, types) for name, types in APP_ACTIONS.items()},
        "flags": flags(call, turns) if call else [],
        "escalation": db.latest_event_payload(call_id, "escalation.detected"),
        "calendar_checks": db.event_payloads(call_id, ("calendar.availability",)),
        "callback_when": callfacts.read(call_id)["callback_when"],
        "destination_masked": mask(call.get("destination")),
    }
