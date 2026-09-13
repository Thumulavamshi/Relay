#!/usr/bin/env python3
"""Replay harness - "show how you know it works", against the REAL apps.

    python backend/replay_harness.py                    # one run, cleans up after itself
    python backend/replay_harness.py --runs 3           # pass rate over repeated runs
    python backend/replay_harness.py --model scripted   # deterministic reads, no model spend
    python backend/replay_harness.py --keep             # leave the records to show them

Replays realistic call webhooks through the real FastAPI app - the same
/vapi/webhook path a live call takes - with the real Google Calendar, HubSpot and
Slack clients. Then it asks each app, independently of our own database, whether
the right thing exists: the event at the booked IST time, the deal at the stage
the read earned, a note quoting the lead, the Slack message in the channel.

Every scenario is replayed TWICE through the same provider call id. The second
pass is the idempotency proof: it must leave the same contact, deal, note, event
and Slack message as the first - not a second of anything.

Nothing lead-facing happens. WhatsApp transports are stubbed, the dialler refuses,
and the database is a throwaway file, so no replayed callback can ever be dialled.
Cleanup sends CRM records to HubSpot's recycle bin and deletes calendar events; a
contact that existed before the run is never touched. Slack test posts are left in
the channel unless --delete-slack is given.
"""

import argparse
import asyncio
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Set before anything imports settings: a replayed callback must never land
# anywhere the callback worker could later dial it.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(prefix="relay-replay-"), "replay.db")

from fastapi.testclient import TestClient                           # noqa: E402
from app import (callbacks, callfacts, classifier, db, extraction,   # noqa: E402
                 gcal, hubspot, integrations, rest, slack, ultramsg, vapi, whatsapp)
from app.config import digits_of, settings                           # noqa: E402
from app.db import IST                                               # noqa: E402
from app.main import EVIDENCE_PATH, app                              # noqa: E402

# --- nothing lead-facing -----------------------------------------------------

LEAD_FACING = []
whatsapp._post = lambda payload: (LEAD_FACING.append("meta")
                                  or {"messages": [{"id": "wamid.REPLAY"}]})
ultramsg._request = lambda method, path, fields, timeout=None: (
    LEAD_FACING.append(path) or {"sent": "true", "message": "ok", "id": "um.REPLAY"})
whatsapp.MIN_GAP_SECONDS = 0


def _never_dial(*args, **kwargs):
    raise AssertionError("the replay harness never places a call")


vapi.place_call = _never_dial
vapi.get_call = lambda provider_call_id: {"status": "in-progress"}


# --- scenarios ---------------------------------------------------------------
#
# `scripted` is only used with --model scripted: the read and the slots the model
# would have produced, revealed slot by slot as their quote is actually spoken.

SCENARIOS = [
    {
        "key": "hot",
        "name": "Hot grocer (English)",
        "turns": [
            ("assistant", "Hi, this is Geeta. We build online stores for small shops. Is now a good time?"),
            ("user", "Yes, go ahead. I'm Ravi."),
            ("assistant", "Thanks Ravi. What do you sell?"),
            ("user", "We run a grocery shop, around 200 products, mostly staples and snacks."),
            ("assistant", "When would you want the store live?"),
            ("user", "Within a month. I need UPI payments and delivery tracking."),
            ("assistant", "And roughly what budget do you have in mind?"),
            ("user", "Around fifty thousand rupees. Send me the details on WhatsApp."),
        ],
        "callback": "tomorrow at 4",
        "closing": [("user", "Tomorrow at four works for me."),
                    ("assistant", "Perfect, I'll call you then.")],
        "scripted": {"label": "hot", "barrier": "none", "slots": {
            "contact_name": ("Ravi", "I'm Ravi"),
            "products": ("groceries", "We run a grocery shop"),
            "catalogue_size": ("around 200 products", "around 200 products"),
            "timeline": ("within a month", "Within a month"),
            "features": ("UPI payments, delivery tracking", "I need UPI payments and delivery tracking"),
            "budget": ("fifty thousand rupees", "Around fifty thousand rupees")}},
    },
    {
        "key": "warm",
        "name": "Warm, brother decides (Hindi-English), asks for a taken slot",
        "turns": [
            ("assistant", "Namaste, main Geeta bol rahi hoon. Online store ke baare mein do minute baat kar sakte hain?"),
            ("user", "Haan boliye. Main kapde bechta hoon, lagbhag 300 designs hain."),
            ("assistant", "Achha. Aapko store kab tak chahiye?"),
            ("user", "Do teen mahine mein, koi jaldi nahi hai."),
            ("assistant", "Budget kya socha hai aapne?"),
            ("user", "Budget ka decision mere bhai lete hain, unse baat karke bataunga."),
        ],
        "block_slot": True,
        "closing": [("user", "Theek hai, tab baat karte hain."),
                    ("assistant", "Dhanyavaad, tab baat karte hain.")],
        "scripted": {"label": "warm", "barrier": "decision_maker", "slots": {
            "products": ("clothes", "Main kapde bechta hoon"),
            "catalogue_size": ("about 300 designs", "lagbhag 300 designs hain"),
            "timeline": ("in two to three months", "Do teen mahine mein")}},
    },
    {
        "key": "cold",
        "name": "Cold, not interested",
        "turns": [
            ("assistant", "Hi, this is Geeta. We build online stores for small shops. Is now a good time?"),
            ("user", "Not really. We are not interested, we already sell on Amazon."),
            ("assistant", "No problem at all. Could I ask what you sell?"),
            ("user", "Electronics accessories. But honestly, not interested, thank you."),
        ],
        "closing": [("assistant", "Understood, thanks for your time.")],
        "scripted": {"label": "cold", "barrier": "none", "slots": {
            "products": ("electronics accessories", "Electronics accessories")}},
    },
]


def use_scripted_model():
    """Deterministic reads, for runs that should prove the apps and nothing else."""
    current = {"scenario": None}

    def classify(transcript, provider=None):
        s = current["scenario"]["scripted"]
        read = classifier.LeadRead(label=s["label"], confidence=0.9, barrier=s["barrier"],
                                   evidence_quote="", reasoning="scripted for replay")
        return classifier.apply_rules(read, transcript)

    def extract(transcript, provider=None):
        empty = extraction.Slot(value="", quote="", confidence=0.0)
        fields = {name: empty for name in extraction.ALL_SLOT_NAMES}
        for name, (value, quote) in current["scenario"]["scripted"]["slots"].items():
            if quote.lower() in transcript.lower():
                fields[name] = extraction.Slot(value=value, quote=quote, confidence=0.9)
        return extraction.ExtractedSlots(**fields)

    classifier.available = lambda provider=None: (True, "scripted for replay")
    classifier.classify_transcript = classify
    extraction.extract_from_transcript = extract
    return current


# --- replay ------------------------------------------------------------------

def post(client, message):
    headers = {"x-vapi-secret": settings.webhook_secret} if settings.webhook_secret else {}
    r = client.post("/vapi/webhook", json={"message": message}, headers=headers)
    r.raise_for_status()
    return r.json()


def book(client, call, phrase):
    r = post(client, {"type": "tool-calls", "call": call, "toolCallList": [{
        "id": f"tc-{uuid.uuid4().hex[:8]}",
        "function": {"name": "schedule_callback", "arguments": {"when": phrase}}}]})
    return r["results"][0]["result"]


_OFFER = re.compile(r"\((\d{2}:\d{2}) on \w+ (\d{1,2}) (\w+)\)")


def offered_phrase(said):
    """'Offer ... (17:00 on Monday 14 September) instead' -> '17:00 on september 14'."""
    m = _OFFER.search(said)
    return f"{m.group(1)} on {m.group(3).lower()} {int(m.group(2))}" if m else None


def replay(client, scenario, prov, slot_phrase=None):
    """One full pass of a call's webhooks. Returns [(phrase, what the tool said)]."""
    call = {"id": prov, "customer": {"number": settings.allowed_destination}}
    post(client, {"type": "status-update", "status": "in-progress", "call": call})
    for role, text in scenario["turns"]:
        post(client, {"type": "transcript", "transcriptType": "final", "role": role,
                      "transcript": text, "call": call})
    if scenario.get("send_details"):
        post(client, {"type": "tool-calls", "call": call, "toolCallList": [{
            "id": f"tc-{uuid.uuid4().hex[:8]}",
            "function": {"name": "send_details_now", "arguments": {}}}]})

    booking = []
    phrase = scenario.get("callback") or slot_phrase
    if phrase:
        said = book(client, call, phrase)
        booking.append((phrase, said))
        offered = offered_phrase(said) if said.startswith("Not booked") else None
        if offered:
            booking.append((offered, book(client, call, offered)))

    for role, text in scenario.get("closing", []):
        post(client, {"type": "transcript", "transcriptType": "final", "role": role,
                      "transcript": text, "call": call})
    messages = [{"role": "user" if role == "user" else "bot", "message": text}
                for role, text in scenario["turns"] + scenario.get("closing", [])]
    post(client, {"type": "end-of-call-report", "endedReason": "customer-ended-call",
                  "call": call, "summary": f"Replay: {scenario['name']}",
                  "artifact": {"messages": messages}})
    return booking


def settle(call_id, timeout=180):
    """Wait until every action on the call has finished and nothing new has been
    claimed for a few seconds (Slack refreshes follow the actions)."""
    deadline, quiet_since, last_count = time.time() + timeout, None, -1
    while time.time() < deadline:
        acts = db.get_actions(call_id)
        busy = any(a["status"] in ("pending", "sending") for a in acts)
        if not busy and len(acts) == last_count:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= 3:
                return acts
        else:
            quiet_since = None
        last_count = len(acts)
        time.sleep(0.5)
    return db.get_actions(call_id)


# --- verification, asked of the apps themselves --------------------------------

class Results:
    def __init__(self):
        self.rows = []

    def check(self, scenario, name, ok=False, detail="", skip=None):
        status = "skip" if skip else ("pass" if ok else "fail")
        self.rows.append({"scenario": scenario, "assertion": name, "status": status,
                          "detail": skip or ("" if ok else str(detail)[:300])})
        mark = {"pass": "ok", "fail": "FAIL", "skip": "skip"}[status]
        tail = f"  - {skip}" if skip else ("" if ok else f"  {str(detail)[:200]}")
        print(f"  [{mark:>4}] {name}{tail}")


def _get(fn, *args, **kwargs):
    """A read that treats 404 as 'not there' rather than an error."""
    try:
        return fn(*args, **kwargs)
    except rest.ApiError as exc:
        if exc.status in (404, 410):
            return None
        raise


def _plain(body):
    return html.unescape(re.sub(r"<[^>]+>", " ", body or ""))


def verify(res, scenario, call_id, live, pass_no, booking, blocked_start):
    name = scenario["name"]
    tag = f"pass {pass_no}"
    call = db.get_call(call_id) or {}
    facts = callfacts.read(call_id)
    label, callback = facts["label"], facts["callback"]

    failed = [f"{a['type']}: {(a['error'] or '').splitlines()[0][:120]}"
              for a in db.get_actions(call_id)
              if a["type"] in integrations.APP_OF_ACTION and a["status"] == "failed"
              and live[integrations.APP_OF_ACTION[a["type"]]]["ok"]]
    res.check(name, f"{tag}: every action on a connected app succeeded", not failed, failed)
    res.check(name, f"{tag}: the lead was read ({label or 'no read'})", label is not None, label)

    # HubSpot
    if live["hubspot"]["ok"]:
        contact = (_get(hubspot._request, "GET",
                        f"/crm/v3/objects/contacts/{call['hubspot_contact_id']}"
                        "?properties=phone,mobilephone")
                   if call.get("hubspot_contact_id") else None)
        props = (contact or {}).get("properties") or {}
        want = digits_of(call.get("destination"))[-10:]
        res.check(name, f"{tag}: HubSpot contact exists for the lead's number",
                  bool(contact) and want in (digits_of(props.get("phone"))[-10:],
                                             digits_of(props.get("mobilephone"))[-10:]),
                  props or "no contact")

        expected = (settings.hubspot_stage_hot if label == "hot" else
                    settings.hubspot_stage_warm if label == "warm" and callback else None)
        if expected:
            deal = (_get(hubspot._request, "GET",
                         f"/crm/v3/objects/deals/{call['hubspot_deal_id']}?properties=dealstage")
                    if call.get("hubspot_deal_id") else None)
            res.check(name, f"{tag}: deal stage matches the read "
                            f"({label}{' + callback' if label == 'warm' else ''})",
                      bool(deal) and deal["properties"].get("dealstage") == expected,
                      (deal or {}).get("properties") or "no deal")
        else:
            res.check(name, f"{tag}: no deal for a {label} read without a callback",
                      not call.get("hubspot_deal_id"), call.get("hubspot_deal_id"))

        note = (_get(hubspot._request, "GET",
                     f"/crm/v3/objects/notes/{call['hubspot_note_id']}?properties=hs_note_body")
                if call.get("hubspot_note_id") else None)
        body = _plain(((note or {}).get("properties") or {}).get("hs_note_body"))
        quotes = [q for _, q in facts["quotes"]] + ([facts["evidence"]] if facts["evidence"] else [])
        res.check(name, f"{tag}: HubSpot note quotes the lead verbatim",
                  any(q in body for q in quotes), body[:160] or "no note")
        if call.get("hubspot_deal_id"):
            assoc = hubspot._request(
                "GET", f"/crm/v4/objects/deals/{call['hubspot_deal_id']}/associations/notes")
            res.check(name, f"{tag}: exactly one note on the deal",
                      len(assoc.get("results") or []) == 1, assoc.get("results"))
    else:
        res.check(name, f"{tag}: HubSpot", skip=f"not connected ({live['hubspot']['detail']})")

    # Google Calendar
    if callback and live["google_calendar"]["ok"]:
        events_url = gcal.API + gcal._cal_path() + "/events"
        event = _get(gcal._request, "GET", f"{events_url}/{gcal.event_id(call_id)}")
        booked_at = datetime.fromisoformat(callback["resolved_at_utc"])
        starts = datetime.fromisoformat(event["start"]["dateTime"]) if event else None
        res.check(name, f"{tag}: Calendar event exists at the booked IST time",
                  bool(event) and event.get("status") == "confirmed" and starts == booked_at,
                  {"event": (event or {}).get("start"), "booked": callback["resolved_at_utc"]})
        listed = gcal._request("GET", events_url + "?privateExtendedProperty="
                               + urllib.parse.quote(f"relay_call_id={call_id}"))
        res.check(name, f"{tag}: exactly one Calendar event for the call",
                  len(listed.get("items") or []) == 1, len(listed.get("items") or []))
        if blocked_start and pass_no == 1:
            res.check(name, "a taken slot produced an alternative, not a double-booking",
                      len(booking) == 2 and booking[0][1].startswith("Not booked")
                      and booking[1][1].startswith("Booked")
                      and booked_at != blocked_start, booking)
    elif callback:
        res.check(name, f"{tag}: Google Calendar",
                  skip=f"not connected ({live['google_calendar']['detail']})")

    # Slack
    if live["slack"]["ok"]:
        permalink = None
        if call.get("slack_ts"):
            permalink = _get(slack._request, "chat.getPermalink",
                             {"channel": call["slack_channel"], "message_ts": call["slack_ts"]},
                             form=True)
        res.check(name, f"{tag}: Slack message exists in the channel",
                  bool(permalink and permalink.get("permalink")), call.get("slack_ts"))
    else:
        res.check(name, f"{tag}: Slack", skip=f"not connected ({live['slack']['detail']})")


IDS = ("hubspot_contact_id", "hubspot_deal_id", "hubspot_note_id", "gcal_event_id", "slack_ts")


# --- cleanup -----------------------------------------------------------------

def cleanup(call_ids, blocks, contact_before, live, delete_slack):
    done, problems = [], []

    def attempt(label, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            done.append(label)
        except Exception as exc:
            problems.append(f"{label}: {exc}")

    contacts = set()
    for call_id in call_ids:
        call = db.get_call(call_id) or {}
        if call.get("gcal_event_id") and live["google_calendar"]["ok"]:
            attempt("calendar event", gcal.delete_event, call_id)
        if live["hubspot"]["ok"]:
            if call.get("hubspot_note_id"):
                attempt("note", hubspot.archive, "notes", call["hubspot_note_id"])
            if call.get("hubspot_deal_id"):
                attempt("deal", hubspot.archive, "deals", call["hubspot_deal_id"])
            if call.get("hubspot_contact_id"):
                contacts.add(call["hubspot_contact_id"])
        if delete_slack and call.get("slack_ts") and live["slack"]["ok"]:
            attempt("slack message", slack._request, "chat.delete",
                    {"channel": call["slack_channel"], "ts": call["slack_ts"]})
    for contact_id in contacts - {contact_before}:
        attempt("contact", hubspot.archive, "contacts", contact_id)
    for event_id in blocks:
        attempt("blocking event", gcal._request, "DELETE",
                gcal.API + gcal._cal_path() + "/events/" + event_id)
    print(f"\ncleanup: removed {len(done)} record(s)"
          + (f"; {len(problems)} problem(s): {problems}" if problems else ""))
    if contact_before:
        print("  the lead's contact existed before this run, so it was left untouched")


# --- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--model", choices=("live", "scripted"), default="live",
                    help="live = the configured understanding model; scripted = fixed reads")
    ap.add_argument("--only", help="comma-separated scenario keys: hot,warm,cold")
    ap.add_argument("--keep", action="store_true", help="leave the records in the apps")
    ap.add_argument("--delete-slack", action="store_true", help="also delete the Slack test posts")
    args = ap.parse_args()

    scenarios = [s for s in SCENARIOS
                 if not args.only or s["key"] in args.only.split(",")]
    if not settings.allowed_destination:
        print("TEST_NUMBER / ALLOWED_DESTINATION is not set - the replayed lead needs a number.")
        return 2

    res, run_id = Results(), uuid.uuid4().hex[:8]
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    call_ids, blocks, ids_by_call = [], [], {}

    with TestClient(app) as client:
        live = asyncio.run(integrations.live_check())
        print(f"\nREPLAY HARNESS  run {run_id} · model={args.model} · runs={args.runs}")
        for app_name, state in live.items():
            print(f"  {app_name:16} {'connected' if state['ok'] else 'NOT connected'} - {state['detail']}")

        current = None
        if args.model == "scripted":
            current = use_scripted_model()
        else:
            ready, why = classifier.available()
            if not ready:
                print(f"\nunderstanding model not ready ({why}). Use --model scripted.")
                return 2
            print(f"  model            {classifier.resolve_provider()} / "
                  f"{classifier.model_for(classifier.resolve_provider())}")

        contact_before = (hubspot.find_contact_by_phone(settings.allowed_destination)[0]
                          if live["hubspot"]["ok"] else None)

        for run in range(1, args.runs + 1):
            for scenario in scenarios:
                print(f"\n[{run}/{args.runs}] {scenario['name']}")
                if current is not None:
                    current["scenario"] = scenario
                prov = f"replay-{run_id}-{run}-{scenario['key']}"

                blocked_start, slot_phrase = None, None
                if scenario.get("block_slot"):
                    blocked_start = (datetime.now(IST) + timedelta(days=2)).replace(
                        hour=15, minute=0, second=0, microsecond=0)
                    slot_phrase = f"3 pm on {blocked_start:%B} {blocked_start.day}".lower()
                    if live["google_calendar"]["ok"]:
                        block = gcal._request("POST", gcal.API + gcal._cal_path() + "/events", body={
                            "summary": f"Relay replay {run_id}: blocked slot",
                            "start": {"dateTime": blocked_start.isoformat(), "timeZone": "Asia/Kolkata"},
                            "end": {"dateTime": (blocked_start + timedelta(hours=1)).isoformat(),
                                    "timeZone": "Asia/Kolkata"},
                            "extendedProperties": {"private": {"relay_harness": run_id}}})
                        blocks.append(block["id"])

                booking = replay(client, scenario, prov, slot_phrase)
                call_id = db.call_id_for_provider(prov)
                call_ids.append(call_id)
                settle(call_id)
                verify(res, scenario, call_id, live, 1, booking, blocked_start)
                first = {k: (db.get_call(call_id) or {}).get(k) for k in IDS}

                # Second pass: the same webhooks again. Only in-memory conversation
                # state is reset (which slots this lead was already offered), so the
                # pass replays the call rather than a lead insisting on a taken slot.
                callbacks._OFFERED_BUSY.pop(call_id, None)
                booking = replay(client, scenario, prov, slot_phrase)
                settle(call_id)
                verify(res, scenario, call_id, live, 2, booking, blocked_start)
                second = {k: (db.get_call(call_id) or {}).get(k) for k in IDS}
                res.check(scenario["name"],
                          "replay: same contact, deal, note, event and Slack message as pass 1",
                          first == second, {"pass 1": first, "pass 2": second})
                ids_by_call[call_id] = second

        contacts = {ids["hubspot_contact_id"] for ids in ids_by_call.values()
                    if ids["hubspot_contact_id"]}
        if live["hubspot"]["ok"]:
            res.check("all scenarios", "every call to the same number shares one HubSpot contact",
                      len(contacts) == 1, contacts)
        res.check("all scenarios", "nothing lead-facing was sent for real (WhatsApp stubbed)",
                  True)

        if not args.keep:
            cleanup(call_ids, blocks, contact_before, live, args.delete_slack)
        elif live["hubspot"]["ok"] or live["slack"]["ok"]:
            print("\n--keep: records left in place")

    # Summary: per assertion, how many runs passed.
    summary = {}
    for row in res.rows:
        key = (row["scenario"], re.sub(r"^pass \d: ", "", row["assertion"]))
        entry = summary.setdefault(key, {"scenario": key[0], "assertion": key[1],
                                         "passed": 0, "failed": 0, "skipped": 0})
        entry[{"pass": "passed", "fail": "failed", "skip": "skipped"}[row["status"]]] += 1
    counted = [r for r in res.rows if r["status"] != "skip"]
    passed = sum(1 for r in counted if r["status"] == "pass")
    evidence = {
        "run_id": run_id, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model if args.model == "scripted"
        else f"{classifier.resolve_provider()}/{classifier.model_for(classifier.resolve_provider())}",
        "runs": args.runs, "scenarios": [s["name"] for s in scenarios],
        "apps": live, "lead_facing_sends_stubbed": len(LEAD_FACING),
        "passed": passed, "failed": len(counted) - passed,
        "skipped": len(res.rows) - len(counted),
        "pass_rate": round(passed / len(counted), 3) if counted else None,
        "summary": list(summary.values()), "results": res.rows,
    }
    os.makedirs(os.path.dirname(EVIDENCE_PATH), exist_ok=True)
    with open(EVIDENCE_PATH, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print(f"{passed} passed, {len(counted) - passed} failed, "
          f"{len(res.rows) - len(counted)} skipped  ->  {EVIDENCE_PATH}")
    return 1 if len(counted) - passed else 0


if __name__ == "__main__":
    sys.exit(main())
