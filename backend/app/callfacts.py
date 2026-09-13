"""One read of a call, shaped for the apps that report on it.

Google Calendar, HubSpot and Slack each describe the same call - who it was, what
they sell, how serious they are, what they actually said, when we ring back, and
where to click. Built once here so the three can never disagree about it.
"""

from . import db
from .config import settings

# Recap order follows the shape of a call, the same order the WhatsApp handlers use.
RECAP = (
    ("products", "Sells"),
    ("catalogue_size", "Catalogue"),
    ("timeline", "Timeline"),
    ("features", "Needs"),
    ("budget", "Budget"),
)

# How each fact reads inside one sentence:
# "sells groceries, around 200 products, wants it live within a month".
_PHRASE = {
    "products": "sells {}",
    "catalogue_size": "{}",
    "timeline": "wants it live {}",
    "features": "needs {}",
    "budget": "budget {}",
}

# A callback that still stands. Cancelled rows were replaced by a restated time.
ACTIVE_CALLBACK = ("pending", "claimed", "placed")


def active_callback(call_id):
    rows = [r for r in db.get_callbacks(call_id) if r["status"] in ACTIVE_CALLBACK]
    return rows[-1] if rows else None


def call_view_url(call_id):
    base = settings.public_base_url
    return f"{base}/#/call/{call_id}" if base else None


def when_text(dt):
    """'Mon 14 Sep, 5:00 PM IST' - how a person reads a time in a CRM or a channel."""
    return f"{dt:%a} {dt.day} {dt:%b}, {dt.hour % 12 or 12}:{dt:%M} {dt:%p} IST"


def read(call_id):
    call = db.get_call(call_id) or {}
    slots = db.get_slots(call_id)

    def value(name):
        return ((slots.get(name) or {}).get("value") or "").strip()

    def quote(name):
        return ((slots.get(name) or {}).get("raw_quote") or "").strip()

    cls = db.latest_classification(call_id) or {}
    barrier = cls.get("barrier")
    callback = active_callback(call_id)
    return {
        "call": call,
        "who": value("contact_name") or None,
        "phone": call.get("destination") or "",
        "live": call.get("status") not in ("ended", "failed"),
        "label": cls.get("label"),
        "barrier": barrier.replace("_", " ") if barrier and barrier != "none" else None,
        "evidence": (cls.get("evidence_quote") or "").strip() or None,
        "products": value("products") or None,
        "facts": [(title, value(name)) for name, title in RECAP if value(name)],
        "sentence": ", ".join(_PHRASE[name].format(value(name))
                              for name, _ in RECAP if value(name)),
        "quotes": [(title, quote(name)) for name, title in RECAP if quote(name)],
        "callback": callback,
        "callback_when": (when_text(db.to_ist(callback["resolved_at_utc"]))
                          if callback else None),
        "call_view": call_view_url(call_id),
    }
