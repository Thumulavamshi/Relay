"""HubSpot - the system of record a sales team opens the morning after.

Our read of the call becomes CRM state:

    any real conversation     contact, found or created by phone
    hot                       deal at HUBSPOT_STAGE_HOT
    warm + callback booked    deal at HUBSPOT_STAGE_WARM
    cold                      contact only - log it, move on
    call ended                one note: the read, barrier, verbatim quotes, links

Stage ids come from env because they belong to the portal. This account runs a
customised pipeline with numeric stage ids, where the stock `qualifiedtobuy`
would be rejected outright.

`sync_call` CONVERGES rather than appends: it reads the call's current state and
makes HubSpot match it, so it is safe to run on every change of read, on a booked
callback, at call end, and again from a replay. Its duplicate guards are its own,
independent of the action bus - the bus stops us dispatching twice, not a retry
after a success whose response we never saw:

- ids live on the call row, so a second sync updates instead of creating
- a contact is searched by phone before one is created; HubSpot de-duplicates on
  email only, and a phone call never gives us one
- a deal stage only moves forward - a lead read hot and then hesitant was still
  the best opportunity on that call, and a rep may already be working it
"""

import html
import logging
import threading

from . import callfacts, db, rest
from .config import digits_of, settings

log = logging.getLogger("elevatebox.hubspot")

API = "https://api.hubapi.com"

# HUBSPOT_DEFINED association type ids.
DEAL_TO_CONTACT = 3
NOTE_TO_CONTACT = 202
NOTE_TO_DEAL = 214

_LOCKS = {}
_UI = {"domain": None}


def configured():
    if not settings.hubspot_token:
        return False, "missing HUBSPOT_TOKEN"
    if not settings.hubspot_portal_id:
        return True, "token set; HUBSPOT_PORTAL_ID missing, so no deep links"
    return True, f"portal {settings.hubspot_portal_id}"


def _request(method, path, body=None, timeout=15):
    """The single transport. The smoke test replaces this."""
    return rest.call("hubspot", method, API + path,
                     headers={"Authorization": "Bearer " + settings.hubspot_token},
                     json_body=body, timeout=timeout)


def _lock(call_id):
    return _LOCKS.setdefault(call_id, threading.Lock())


def _assoc(record_id, type_id):
    return {"to": {"id": record_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}


# ----------------------------------------------------------------- links

def ui_domain():
    """app.hubspot.com, app-eu1..., app-na2... A deep link on the wrong data
    centre lands on a login page, so ask the portal once."""
    if _UI["domain"] is None:
        try:
            _UI["domain"] = (_request("GET", "/account-info/v3/details").get("uiDomain")
                             or "app.hubspot.com")
        except Exception as exc:
            log.warning("could not read the HubSpot uiDomain (%s)", exc)
            return "app.hubspot.com"
    return _UI["domain"]


def _record_url(object_type, record_id):
    if not (record_id and settings.hubspot_token and settings.hubspot_portal_id):
        return None
    return (f"https://{ui_domain()}/contacts/{settings.hubspot_portal_id}"
            f"/record/{object_type}/{record_id}")


def deal_url(deal_id):
    return _record_url("0-3", deal_id)


def contact_url(contact_id):
    return _record_url("0-1", contact_id)


# ----------------------------------------------------------------- contacts

def find_contact_by_phone(phone):
    """(id, firstname) of an existing contact with this number, or (None, None).

    The groups are ORed: the number as we write it, as a mobile, and HubSpot's
    own normalised digits, which ignore how a rep happened to format it."""
    digits = digits_of(phone)
    if not digits:
        return None, None
    groups = [
        {"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone}]},
        {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": phone}]},
        {"filters": [{"propertyName": "hs_searchable_calculated_phone_number",
                      "operator": "EQ", "value": digits[-10:]}]},
    ]
    data = _request("POST", "/crm/v3/objects/contacts/search", {
        "filterGroups": groups, "properties": ["firstname", "phone"], "limit": 1})
    hits = data.get("results") or []
    if not hits:
        return None, None
    return hits[0]["id"], (hits[0].get("properties") or {}).get("firstname")


def _known_contact(phone):
    """(id, firstname) of the contact a previous call resolved, if it still exists."""
    known = db.hubspot_contact_for(phone)
    if not known:
        return None, None
    try:
        record = _request("GET", f"/crm/v3/objects/contacts/{known}?properties=firstname")
    except rest.ApiError as exc:
        if exc.status == 404:       # archived since - fall back to search
            return None, None
        raise
    return known, (record.get("properties") or {}).get("firstname")


def upsert_contact(call_id, name=None, final=False):
    """The call's contact id, finding or creating it once."""
    call = db.get_call(call_id) or {}
    contact_id = call.get("hubspot_contact_id")
    if contact_id:
        # The name often lands late in a call. Fill it in at the end, but never
        # overwrite one a rep may already have typed.
        if final and name:
            current = _request("GET", f"/crm/v3/objects/contacts/{contact_id}?properties=firstname")
            if not (current.get("properties") or {}).get("firstname"):
                _request("PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                         {"properties": {"firstname": name}})
        return contact_id

    phone = call.get("destination") or settings.allowed_destination
    # HubSpot's search index lags record creation by several seconds, so two calls
    # to one number a minute apart would both miss the search and both create. A
    # contact an earlier call already resolved is tried first.
    contact_id, firstname = _known_contact(phone)
    if not contact_id:
        contact_id, firstname = find_contact_by_phone(phone)
    if contact_id:
        if name and not firstname:
            _request("PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                     {"properties": {"firstname": name}})
        log.info("call %s: matched existing HubSpot contact %s", call_id, contact_id)
    else:
        props = {"phone": phone}
        if name:
            props["firstname"] = name
        contact_id = _request("POST", "/crm/v3/objects/contacts", {"properties": props})["id"]
        log.info("call %s: created HubSpot contact %s", call_id, contact_id)
    db.update_call(call_id, hubspot_contact_id=contact_id)
    return contact_id


# ----------------------------------------------------------------- deals

def _rank(stage):
    return {settings.hubspot_stage_warm: 1, settings.hubspot_stage_hot: 2}.get(stage, 0)


def target_stage(facts):
    """The stage this call has earned, or None for contact-only."""
    if facts["label"] == "hot":
        return settings.hubspot_stage_hot
    if facts["label"] == "warm" and facts["callback"]:
        return settings.hubspot_stage_warm
    return None


def _deal_properties(facts):
    who = facts["who"] or "Lead"
    lines = [f"Qualified by Relay on a call to {facts['phone']}."]
    lines += [f"{title}: {value}" for title, value in facts["facts"]]
    if facts["barrier"]:
        lines.append(f"Barrier: {facts['barrier']}")
    if facts["callback_when"]:
        lines.append(f"Callback: {facts['callback_when']}")
    if facts["call_view"]:
        lines.append(f"Call view: {facts['call_view']}")
    return {"dealname": f"{who} — {facts['products'] or 'online store'}"[:200],
            "description": "\n".join(lines)}


def ensure_deal(call_id, contact_id, stage, facts):
    """Create the deal, or update it and move it FORWARD. Returns its id or None."""
    call = db.get_call(call_id) or {}
    props = _deal_properties(facts)
    deal_id = call.get("hubspot_deal_id")
    if deal_id:
        moved = bool(stage) and _rank(stage) > _rank(call.get("hubspot_deal_stage"))
        if moved:
            props["dealstage"] = stage
        _request("PATCH", f"/crm/v3/objects/deals/{deal_id}", {"properties": props})
        if moved:
            db.update_call(call_id, hubspot_deal_stage=stage)
            log.info("call %s: deal %s moved to %s", call_id, deal_id, stage)
        return deal_id
    if not stage:
        return None
    props.update(pipeline=settings.hubspot_pipeline_id, dealstage=stage)
    deal = _request("POST", "/crm/v3/objects/deals", {
        "properties": props, "associations": [_assoc(contact_id, DEAL_TO_CONTACT)]})
    db.update_call(call_id, hubspot_deal_id=deal["id"], hubspot_deal_stage=stage)
    log.info("call %s: created deal %s at %s", call_id, deal["id"], stage)
    return deal["id"]


# ----------------------------------------------------------------- notes

def note_html(call_id, facts):
    e = html.escape
    call = db.get_call(call_id) or {}
    head = (f"<p><strong>Relay AI call</strong> — read as "
            f"<strong>{e((facts['label'] or 'unclassified').title())}</strong>")
    if facts["barrier"]:
        head += f", barrier: {e(facts['barrier'])}"
    parts = [head + "</p>"]
    if facts["facts"]:
        parts.append("<p><strong>What they told us</strong></p><ul>"
                     + "".join(f"<li>{e(t)}: {e(v)}</li>" for t, v in facts["facts"]) + "</ul>")
    quotes = [f"<li>{e(t)}: “{e(q)}”</li>" for t, q in facts["quotes"]]
    if facts["evidence"]:
        quotes.append(f"<li>Intent: “{e(facts['evidence'])}”</li>")
    if quotes:
        parts.append("<p><strong>In their own words</strong></p><ul>" + "".join(quotes) + "</ul>")
    if facts["callback_when"]:
        link = call.get("gcal_event_link")
        parts.append(f"<p><strong>Callback:</strong> {e(facts['callback_when'])}"
                     + (f' — <a href="{e(link)}">calendar event</a>' if link else "") + "</p>")
    if call.get("summary"):
        parts.append(f"<p><strong>Call summary:</strong> {e(call['summary'])}</p>")
    if facts["call_view"]:
        parts.append(f'<p><a href="{e(facts["call_view"])}">Open the call in Relay</a></p>')
    return "".join(parts)


def write_note(call_id, contact_id, deal_id, facts):
    """One note per call, updated in place if it already exists."""
    call = db.get_call(call_id) or {}
    props = {"hs_timestamp": db.utc_now().replace("+00:00", "Z"),
             "hs_note_body": note_html(call_id, facts)}
    if call.get("hubspot_note_id"):
        _request("PATCH", f"/crm/v3/objects/notes/{call['hubspot_note_id']}",
                 {"properties": props})
        return call["hubspot_note_id"]
    associations = [_assoc(contact_id, NOTE_TO_CONTACT)]
    if deal_id:
        associations.append(_assoc(deal_id, NOTE_TO_DEAL))
    note = _request("POST", "/crm/v3/objects/notes",
                    {"properties": props, "associations": associations})
    db.update_call(call_id, hubspot_note_id=note["id"])
    log.info("call %s: note %s written", call_id, note["id"])
    return note["id"]


# ----------------------------------------------------------------- sync

def sync_call(call_id, final=False):
    """Make HubSpot match this call. Blocking; handlers run it in a thread."""
    with _lock(call_id):
        facts = callfacts.read(call_id)
        if not facts["call"]:
            raise RuntimeError(f"no such call {call_id}")
        if facts["label"] is None and not final:
            return {"skipped": "not classified yet"}
        contact_id = upsert_contact(call_id, name=facts["who"], final=final)
        stage = target_stage(facts)
        deal_id = ensure_deal(call_id, contact_id, stage, facts)
        note_id = write_note(call_id, contact_id, deal_id, facts) if final else None
        return {"message_id": note_id or deal_id or contact_id,
                "contact_id": contact_id, "deal_id": deal_id, "stage": stage}


def archive(object_type, record_id):
    """Send a record to HubSpot's recycle bin (restorable). Replay-harness cleanup."""
    _request("DELETE", f"/crm/v3/objects/{object_type}/{record_id}")


def check():
    """Live check for the Integrations view: token, portal, and that the stage
    ids we write actually exist in the pipeline. Read-only."""
    ok, why = configured()
    if not ok:
        return {"ok": False, "detail": why}
    try:
        account = _request("GET", "/account-info/v3/details")
        pipeline = _request("GET", f"/crm/v3/pipelines/deals/{settings.hubspot_pipeline_id}")
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}
    stages = {s["id"]: s["label"] for s in pipeline.get("stages", [])}
    missing = [name for name, sid in (("HUBSPOT_STAGE_HOT", settings.hubspot_stage_hot),
                                      ("HUBSPOT_STAGE_WARM", settings.hubspot_stage_warm))
               if sid not in stages]
    if missing:
        return {"ok": False, "detail": f"not a stage in pipeline "
                                       f"'{pipeline.get('label')}': {', '.join(missing)}"}
    if settings.hubspot_portal_id and str(account.get("portalId")) != settings.hubspot_portal_id:
        return {"ok": False, "detail": "HUBSPOT_PORTAL_ID does not match the token's portal"}
    return {"ok": True, "detail": f"pipeline '{pipeline.get('label')}': "
                                  f"hot -> {stages[settings.hubspot_stage_hot]}, "
                                  f"warm + callback -> {stages[settings.hubspot_stage_warm]}"}
