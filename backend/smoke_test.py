#!/usr/bin/env python3
"""Local end-to-end check. No network, no phone calls, no spend.

Replays a realistic Vapi webhook sequence against the app and asserts the
persistence, idempotency and safety properties hold.

    python backend/smoke_test.py
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Point at a throwaway DB before anything imports settings.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")
os.environ.setdefault("VAPI_API_KEY", "test-key")
os.environ.setdefault("VAPI_PHONE_NUMBER_ID", "test-phone")
os.environ.setdefault("VAPI_ASSISTANT_ID", "test-assistant")
os.environ["ALLOWED_DESTINATION"] = "+919876543210"
# Pinned: the real .env may point the mid-call action at hello_world (which
# takes no parameters), and the tests below assert on parameter composition.
os.environ["WHATSAPP_TEMPLATE_MIDCALL"] = "elevatebox_call_recap_now"
os.environ["WHATSAPP_MIDCALL_PARAMS"] = "1"
# Pinned so the provider in the real .env cannot change what these tests mean.
# The free-form path is exercised explicitly further down by flipping it.
os.environ["WHATSAPP_PROVIDER"] = "meta"
# WhatsApp is off by default now. These sections test the send path itself, so
# they switch it on; the WHATSAPP OFF section at the end checks the default.
os.environ["WHATSAPP_ENABLED"] = "1"
os.environ.setdefault("YOUR_NAME", "Test Sender")
os.environ.setdefault("YOUR_MOBILE_NUMBER", "+910000000000")
# The real .env now carries live Google, HubSpot and Slack credentials. Blank them
# before settings load (load_env never overrides a variable that is already set),
# so every section before MULTI-APP runs exactly as it did before those apps
# existed - and the transports are faked below as well, so nothing in this file
# can create a real calendar event, CRM record or Slack post.
for _key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
             "HUBSPOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID",
             "PUBLIC_BASE_URL", "SERVER_URL"):
    os.environ[_key] = ""

from fastapi.testclient import TestClient             # noqa: E402
from app import actions, classifier, db, extraction, vapi, whatsapp  # noqa: E402
from app.config import EVALUATOR_NUMBER, settings     # noqa: E402
from app.main import app                              # noqa: E402

# Stub the WhatsApp transport BEFORE anything can run a handler. Once real
# credentials exist in .env, the post-call follow-up handler would otherwise
# send a REAL message every time someone runs the smoke test - the same trap the
# Anthropic key sprang earlier. Nothing in this file may reach a live service.
WA_SENT = []
whatsapp._post = lambda payload: (WA_SENT.append(payload) or
                                  {"messages": [{"id": "wamid.TEST"}]})

# Second transport, same trap. UltraMsg sends to a REAL linked WhatsApp account
# with no template gate at all, so an unstubbed handler here would message a
# live handset on every smoke run - and unlike Meta there is no approval step to
# fail safe behind.
UM_SENT = []
from app import ultramsg                                  # noqa: E402
ultramsg._request = lambda method, path, fields, timeout=None: (
    UM_SENT.append({"path": path, **{k: v for k, v in fields.items() if k != "token"}})
    or {"sent": "true", "message": "ok", "id": "um.TEST"})
whatsapp.MIN_GAP_SECONDS = 0          # never sleep the 6s inter-message gap in tests
settings.wa_phone_number_id = "TEST_PNID"
settings.wa_access_token = "TEST_TOKEN"

# The smoke test must never reach a paid API: it has to stay free, fast and
# deterministic. Once ANTHROPIC_API_KEY existed this file silently started
# making real billed calls, so the model is stubbed. The RULES overlay is left
# real - it is the part these tests are actually asserting on.
classifier.available = lambda provider=None: (True, "stubbed for tests")
classifier.classify_transcript = lambda transcript, provider=None: classifier.apply_rules(
    classifier.LeadRead(label="cold", confidence=0.5, barrier="none",
                        evidence_quote="", reasoning="stub"),
    transcript,
)


# Same trap, second door. Extraction shares the classifier's transport, so
# stubbing classify_transcript alone would leave extract_slots making real billed
# calls on every smoke run. This is the choke point both go through.
def _no_model(*a, **kw):
    raise AssertionError("the smoke test must never reach a real model")


classifier.run_structured = _no_model

def slots(**kw):
    """An ExtractedSlots with everything empty except what is named."""
    empty = extraction.Slot(value="", quote="", confidence=0.0)
    # ALL_SLOT_NAMES, not SLOT_NAMES: the model carries contact_name too, which
    # is stored like a slot but deliberately excluded from discovery coverage.
    fields = {n: empty for n in extraction.ALL_SLOT_NAMES}
    for name, (value, quote) in kw.items():
        fields[name] = extraction.Slot(value=value, quote=quote, confidence=0.9)
    return extraction.ExtractedSlots(**fields)


# What the extractor is pretending to have found. A one-element list so tests can
# swap it without rebinding a global. The merge, quote-verification and
# persistence logic underneath are all the real ones.
EXTRACTED = [slots()]
extraction.extract_from_transcript = lambda transcript, provider=None: EXTRACTED[0]

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


# Never let the smoke test reach the real Vapi API.
PLACED = []
_SEQ = [0]


OVERRIDES = []


def _fake_place_call(destination, assistant_id=None, overrides=None):
    PLACED.append(destination)
    OVERRIDES.append(overrides or {})
    _SEQ[0] += 1
    return {"id": f"prov-call-{_SEQ[0]}", "status": "queued"}


vapi.place_call = _fake_place_call

# What the provider would say if asked about a call. The reconciliation sweeper
# polls this when a webhook goes missing.
VAPI_CALLS = {}
vapi.get_call = lambda provider_call_id: VAPI_CALLS.get(
    provider_call_id, {"status": "in-progress"})

# A fake action handler, so the bus is exercised rather than just declared.
SENT = []


@actions.handler("whatsapp_hot")
async def _fake_send(call_id, payload):
    SENT.append((call_id, payload))
    return {"message_id": "wamid.TEST"}


def webhook(client, msg, secret=None):
    headers = {"x-vapi-secret": secret} if secret is not None else {}
    return client.post("/vapi/webhook", json={"message": msg}, headers=headers)


def main():
    with TestClient(app) as client:
        print("\nHEALTH + CONFIG")
        h = client.get("/health").json()
        check("health ok", h["status"] == "ok")
        check("db initialised", os.path.exists(settings.db_path))
        check("allowed destination is our test number",
              h["allowed_destination"] == "+919876543210")
        check("health flags evaluator number (currently false)",
              h["destination_is_evaluator"] is False)
        check("action registry shows the whatsapp handlers as wired",
              h["actions"]["whatsapp_hot"] == "wired"
              and h["actions"]["whatsapp_followup"] == "wired", h["actions"])
        check("and still reports genuinely unbuilt actions as stubs",
              h["actions"]["whatsapp_brochure"] == "stub", h["actions"])
        check("the callback confirmation is wired",
              h["actions"]["callback_confirm"] == "wired", h["actions"])

        print("\nTRIGGER SAFETY")
        check("endpoint accepts no destination parameter",
              "requestBody" not in (client.get("/openapi.json").json()
                                    ["paths"]["/calls"]["post"]))
        r = client.post("/calls", json={"number": EVALUATOR_NUMBER})
        check("a number in the body is ignored", r.status_code == 200)
        check("dialled ONLY the configured destination", PLACED == ["+919876543210"], PLACED)
        call_id = r.json()["call_id"]

        print("\nWEBHOOK INGRESS")
        # A call placed outside the backend (agent.py dials Vapi directly) must
        # still be tracked, or its transcripts are never classified and its tool
        # calls are answered with nothing. This exact gap silently killed a live
        # mid-call test: 648 webhooks arrived, every one with a NULL call_id.
        before = len(db.list_calls(50))
        webhook(client, {"type": "status-update", "status": "ringing",
                         "call": {"id": "adopt-me",
                                  "customer": {"number": "+919876543210"}}})
        adopted = db.call_id_for_provider("adopt-me")
        check("a call we did not place is adopted, not dropped", adopted is not None)
        check("adoption creates exactly one row", len(db.list_calls(50)) == before + 1)

        webhook(client, {"type": "status-update", "status": "in-progress",
                         "call": {"id": "adopt-me"}})
        check("a second webhook reuses the adopted row",
              len(db.list_calls(50)) == before + 1
              and db.get_call(adopted)["status"] == "in-progress")

        r = webhook(client, {"type": "tool-calls", "call": {"id": "adopt-me"},
                             "toolCallList": [{"id": "tc-a", "function":
                                               {"name": "send_details_now",
                                                "arguments": {}}}]})
        check("a tool call on an adopted call is answered AND acted on",
              r.json()["results"][0]["toolCallId"] == "tc-a"
              and any(a["type"] == "whatsapp_hot" for a in db.get_actions(adopted)),
              r.json())

        webhook(client, {"type": "status-update", "status": "in-progress",
                         "call": {"id": "prov-call-1"}})
        check("status update mapped to our call",
              db.get_call(call_id)["status"] == "in-progress")

        webhook(client, {"type": "transcript", "transcriptType": "partial",
                         "role": "user", "transcript": "umm",
                         "call": {"id": "prov-call-1"}})
        check("partial transcripts are not persisted", len(db.get_turns(call_id)) == 0)

        for role, text in [("assistant", "Hi, this is Maya."),
                           ("user", "I sell custom t-shirts."),
                           ("user", "I sell custom t-shirts.")]:      # duplicate
            webhook(client, {"type": "transcript", "transcriptType": "final",
                             "role": role, "transcript": text,
                             "call": {"id": "prov-call-1"}})
        turns = db.get_turns(call_id)
        check("final transcripts persisted, duplicate dropped", len(turns) == 2,
              [t["text"] for t in turns])
        check("turn order preserved", [t["seq"] for t in turns] == [0, 1])

        print("\nTOOL CALLS  (the mid-call action path)")
        r = webhook(client, {"type": "tool-calls", "call": {"id": "prov-call-1"},
                             "toolCallList": [{"id": "tc-1", "function":
                                               {"name": "send_details_now", "arguments": {}}}]})
        body = r.json()
        check("tool call answered synchronously", r.status_code == 200)
        check("result string is speakable", "WhatsApp" in body["results"][0]["result"], body)
        sent_before = len(SENT) - 1   # adoption fired one already
        check("handler actually ran", len(SENT) == sent_before + 1, SENT)
        acts = db.get_actions(call_id)
        check("action recorded as sent", len(acts) == 1 and acts[0]["status"] == "sent",
              [(a["type"], a["status"]) for a in acts])
        check("trigger source recorded", acts[0]["trigger_source"] == "tool_call")

        print("\nIDEMPOTENCY  (two trigger paths, one message)")
        webhook(client, {"type": "tool-calls", "call": {"id": "prov-call-1"},
                         "toolCallList": [{"id": "tc-2", "function":
                                           {"name": "send_details_now", "arguments": {}}}]})
        check("second identical trigger is a no-op",
              len(db.get_actions(call_id)) == 1 and len(SENT) == sent_before + 1,
              f"actions={len(db.get_actions(call_id))} sent={len(SENT)}")
        check("watchdog path shares the key",
              actions.dispatch(call_id, "whatsapp_hot", trigger_source="watchdog") is None)

        print("\nUNKNOWN TOOL")
        r = webhook(client, {"type": "tool-calls", "call": {"id": "prov-call-1"},
                             "toolCallList": [{"id": "tc-3", "function":
                                               {"name": "nope", "arguments": {}}}]})
        check("unknown tool answered, not crashed",
              r.status_code == 200 and "Unknown" in r.json()["results"][0]["result"])

        print("\nEND OF CALL")
        webhook(client, {"type": "end-of-call-report", "endedReason": "customer-ended-call",
                         "call": {"id": "prov-call-1", "startedAt": "2026-08-28T10:00:00Z"},
                         "summary": "Sells t-shirts.",
                         "artifact": {"recordingUrl": "https://example/rec.wav",
                                      "messages": [
                                          {"role": "user", "message": "I sell custom t-shirts."},
                                          {"role": "bot", "message": "How many designs?"},
                                          {"role": "user", "message": "About fifty."}]}})
        call = db.get_call(call_id)
        check("call marked ended", call["status"] == "ended")
        check("ended reason stored", call["ended_reason"] == "customer-ended-call")
        check("recording url stored", call["recording_url"] == "https://example/rec.wav")
        texts = [t["text"] for t in db.get_turns(call_id)]
        check("backfill added only missing turns, no duplicates",
              texts.count("I sell custom t-shirts.") == 1 and "About fifty." in texts, texts)
        followup = [a for a in db.get_actions(call_id) if a["type"] == "whatsapp_followup"]
        check("post-call follow-up was claimed exactly once", len(followup) == 1)
        # It runs for real here (credentials are absent in tests), so it fails on
        # config rather than on a missing handler. Either way the ledger records
        # that something WANTED to fire - that visibility is the point.
        check("follow-up attempt recorded in the ledger",
              followup[0]["status"] in ("pending", "sending", "sent", "failed"),
              followup[0])

        print("\nCLASSIFICATION  (rules path - works with no API key)")
        # Fresh call, so the earlier idempotency key cannot mask the result.
        PLACED.clear()
        call2 = client.post("/calls").json()["call_id"]
        for role, text in [("assistant", "What do you sell?"),
                           ("user", "We do custom t-shirts. Send me the details.")]:
            webhook(client, {"type": "transcript", "transcriptType": "final",
                             "role": role, "transcript": text,
                             "call": {"id": "prov-call-2"}})
        cls = db.latest_classification(call2)
        check("lead classified hot by the rules overlay",
              cls is not None and cls["label"] == "hot", cls)
        check("evidence quote captured",
              bool(cls) and "Send me the details" in (cls["evidence_quote"] or ""))
        hot = [a for a in db.get_actions(call2) if a["type"] == "whatsapp_hot"]
        check("watchdog fired the mid-call action", len(hot) == 1, db.get_actions(call2))
        check("attributed to the watchdog, not a tool call",
              bool(hot) and hot[0]["trigger_source"] == "watchdog", hot)

        print("\nCLASSIFICATION  (a refusal must NOT fire anything)")
        PLACED.clear()
        call3 = client.post("/calls").json()["call_id"]
        webhook(client, {"type": "transcript", "transcriptType": "final", "role": "user",
                         "transcript": "Not interested. Send me the details if you want.",
                         "call": {"id": "prov-call-3"}})
        # The invariant is about the ACTION, not the label. An earlier version
        # asserted "no classification stored", which only held because no API key
        # existed - it passed for the wrong reason and broke the moment one did.
        cls3 = db.latest_classification(call3)
        check("refusal is not read as hot",
              cls3 is None or cls3["label"] != "hot", cls3)
        check("and no mid-call action fired", db.get_actions(call3) == [], db.get_actions(call3))

        print("\nQUOTE VERIFICATION  (pure functions - no key, no labels)")
        # The one part of extraction quality that can be checked without grading
        # our own homework: did the lead actually say this?
        vturns = [
            {"seq": 0, "role": "assistant",
             "text": "You're looking at seventy thousand to one and a half lakh."},
            {"seq": 1, "role": "user", "text": "Uh, I sold a. Custom-made T-shirts�"},
            {"seq": 2, "role": "user", "text": "In our catalog we try to maintain around 3 to 2. 100."},
        ]
        q, seq = extraction.attach_quote("Custom-made T-shirts", "custom t-shirts", vturns)
        check("a real quote is kept, with its turn", q == "Custom-made T-shirts" and seq == 1,
              (q, seq))
        q, seq = extraction.attach_quote("I sell custom made t-shirts online",
                                         "custom-made T-shirts", vturns)
        check("a tidied-up quote is replaced by the lead's actual turn",
              q == vturns[1]["text"].strip() and seq == 1, (q, seq))
        q, seq = extraction.attach_quote("we can do it for one lakh", "one lakh", vturns)
        check("a quote the lead never said is dropped entirely", q is None and seq is None,
              (q, seq))
        # The live call had Maya quoting prices the lead never named. Attributing
        # that back to the lead would be a fabricated quote in the follow-up.
        q, seq = extraction.attach_quote("seventy thousand to one and a half lakh",
                                         "70,000 to 1.5 lakh", vturns)
        check("the AGENT's words are never accepted as the lead's quote",
              q is None and seq is None, (q, seq))
        check("punctuation and STT noise do not break a match",
              extraction.attach_quote("around 3 to 2 100", "", vturns)[1] == 2)

        print("\nEXTRACTION  (model stubbed - merge and persistence are real)")
        PLACED.clear()
        call4 = client.post("/calls").json()["call_id"]
        for role, text in [("assistant", "What do you sell?"),
                           ("user", "We make custom-made T-shirts."),
                           ("assistant", "How many designs?"),
                           ("user", "Around three hundred items, and I need it in a month.")]:
            EXTRACTED[0] = slots(
                products=("custom-made t-shirts", "We make custom-made T-shirts."),
                # Quote paraphrased, value still verbatim -> falls back to the turn.
                timeline=("in a month", "I want the store live in a month"),
                # Neither quote nor value appears -> value kept, quote refused.
                catalogue_size=("300 designs", "we carry 300 designs"),
            )
            webhook(client, {"type": "transcript", "transcriptType": "final",
                             "role": role, "transcript": text,
                             "call": {"id": "prov-call-4"}})
        got = db.get_slots(call4)
        check("extracted value persisted", got.get("products", {}).get("value")
              == "custom-made t-shirts", got.get("products"))
        check("verbatim quote stored against its turn",
              got["products"]["raw_quote"] == "We make custom-made T-shirts."
              and got["products"]["source_turn_seq"] == 1, got.get("products"))
        check("a paraphrase falls back to the lead's real words",
              got.get("timeline", {}).get("raw_quote") == "Around three hundred items, "
              "and I need it in a month.", got.get("timeline"))
        check("an unverifiable quote is dropped but the value survives",
              got.get("catalogue_size", {}).get("value") == "300 designs"
              and got["catalogue_size"]["raw_quote"] is None, got.get("catalogue_size"))
        check("slots the lead never mentioned stay empty",
              "budget" not in got and "features" not in got, list(got))
        check("coverage reflects what was actually extracted",
              client.get(f"/calls/{call4}").json()["coverage"]
              == {"products": True, "catalogue_size": True, "timeline": True,
                  "features": False, "budget": False},
              client.get(f"/calls/{call4}").json()["coverage"])

        # A later pass that finds nothing means "not mentioned again", never
        # "retract it". Forgetting what the lead said would be worse than stale.
        EXTRACTED[0] = slots(budget=("one lakh", "about one lakh"))
        webhook(client, {"type": "transcript", "transcriptType": "final",
                         "role": "user", "transcript": "Budget is about one lakh.",
                         "call": {"id": "prov-call-4"}})
        got = db.get_slots(call4)
        check("a later empty pass does not erase an earlier fact",
              got["products"]["value"] == "custom-made t-shirts", got.get("products"))
        check("and new facts still land", got.get("budget", {}).get("value") == "one lakh",
              got.get("budget"))

        print("\nSILENT CALL  (nothing to follow up about)")
        # A call where the line failed must not send the architecture image and
        # resume with an empty recap. Redialling then delivers the whole set
        # twice for one conversation, which is what happened in production.
        PLACED.clear()
        callS = client.post("/calls").json()["call_id"]
        provS = db.get_call(callS)["provider_call_id"]
        webhook(client, {"type": "transcript", "transcriptType": "final",
                         "role": "user", "transcript": "hello",
                         "call": {"id": provS}})
        webhook(client, {"type": "end-of-call-report",
                         "endedReason": "customer-ended-call",
                         "call": {"id": provS}, "artifact": {"messages": []}})
        acts = [a["type"] for a in db.get_actions(callS)]
        check("a silent call sends no follow-up", "whatsapp_followup" not in acts, acts)
        check("and no resume either", "whatsapp_resume" not in acts, acts)

        print("\nWHATSAPP PAYLOADS  (transport stubbed - no real send, no cost)")
        sent = WA_SENT          # stubbed at import time, see top of file
        dest = settings.allowed_destination

        whatsapp.send_template(dest, "tpl_x", body_params=["a", "b"],
                               lang=settings.wa_template_lang)
        p = sent[-1]
        check("template payload shape", p["type"] == "template"
              and p["template"]["name"] == "tpl_x", p)
        check("destination normalised to digits only", p["to"].isdigit(), p["to"])
        check("body params sent in order",
              [x["text"] for x in p["template"]["components"][0]["parameters"]] == ["a", "b"], p)

        # Meta rejects the whole send if a parameter contains a newline, so an
        # extracted quote with a line break would kill the mid-call message.
        whatsapp.send_template(dest, "tpl_x", body_params=["one\ntwo\tthree     four"])
        cleaned = sent[-1]["template"]["components"][0]["parameters"][0]["text"]
        check("newlines/tabs/4+ spaces stripped from params",
              "\n" not in cleaned and "\t" not in cleaned and "    " not in cleaned,
              repr(cleaned))

        whatsapp.send_template(dest, "tpl_img", body_params=["a"],
                               header={"type": "image", "link": "https://x/a.png"})
        hdr = sent[-1]["template"]["components"][0]
        check("image header shape",
              hdr["type"] == "header" and hdr["parameters"][0]["image"]["link"] == "https://x/a.png",
              hdr)

        whatsapp.send_template(dest, "tpl_doc", body_params=["a"],
                               header={"type": "document", "link": "https://x/cv.pdf",
                                       "filename": "cv.pdf"})
        doc = sent[-1]["template"]["components"][0]["parameters"][0]["document"]
        check("document header carries filename", doc["filename"] == "cv.pdf", doc)

        print("\nWHATSAPP SAFETY")
        before = len(sent)
        try:
            whatsapp.send_template("+919999999999", "tpl_x", body_params=["a"])
            check("a non-allowed destination is refused", False, "it sent!")
        except whatsapp.WhatsAppError:
            check("a non-allowed destination is refused", len(sent) == before)
        try:
            whatsapp.send_template(EVALUATOR_NUMBER, "tpl_x", body_params=["a"])
            check("the evaluator's number is refused", False, "it sent!")
        except whatsapp.WhatsAppError:
            check("the evaluator's number is refused", len(sent) == before)

        print("\nWHATSAPP HANDLERS  (composition from real slots)")
        from app import handlers
        # Written to call2, not call_id, so this cannot pollute the coverage
        # assertion made against call_id further down.
        db.upsert_slot(call2, "products", "custom t-shirts", raw_quote="we do custom tees")
        db.upsert_slot(call2, "budget", "1000 USD", raw_quote="around\n1,000 US dollars")
        before = len(sent)
        await_res = handlers.send_mid_call.__wrapped__ if hasattr(
            handlers.send_mid_call, "__wrapped__") else handlers.send_mid_call
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            await_res(call2, {}))
        params = [x["text"] for x in sent[-1]["template"]["components"][0]["parameters"]]
        check("mid-call uses extracted slot values", "custom t-shirts" in params[0], params)
        check("missing slots get an honest fallback, never an invented value",
              params[2] == handlers.FALLBACKS["timeline"], params)
        check("mid-call has no media header (fastest delivery)",
              all(c["type"] != "header" for c in sent[-1]["template"]["components"]))

        print("\nFREE-FORM PROVIDER  (UltraMsg - no templates, no approval)")
        settings.wa_provider = "ultramsg"
        settings.um_instance, settings.um_token = "instanceTEST", "tokenTEST"
        settings.wa_architecture_url = "https://example/arch.png"
        settings.wa_resume_url = "https://example/cv.pdf"
        try:
            check("provider reports free-form support", whatsapp.supports_freeform())
            check("config check passes on the ultramsg credentials",
                  whatsapp.configured()[0], whatsapp.configured())

            UM_SENT.clear()
            run = asyncio.get_event_loop_policy().new_event_loop().run_until_complete
            run(handlers.send_mid_call(call2, {}))
            body = UM_SENT[-1]["body"]
            check("mid-call goes out as free-form chat",
                  UM_SENT[-1]["path"] == "messages/chat", UM_SENT[-1]["path"])
            check("mid-call quotes real extracted values", "custom t-shirts" in body, body)
            check("mid-call carries the mobile number",
                  "+910000000000" in body, body)
            check("free-form keeps real line breaks (templates could not)",
                  "\n" in body)
            check("a slot we never learned is OMITTED, not padded with a fallback",
                  handlers.FALLBACKS["timeline"] not in body, body)

            UM_SENT.clear()
            run(handlers.send_followup(call2, {}))
            sent = UM_SENT[-1]
            check("follow-up rides on the architecture image",
                  sent["path"] == "messages/image"
                  and sent["image"] == "https://example/arch.png", sent["path"])
            cap = sent["caption"]
            # Section 06 wants all four in the message that reaches them.
            check("caption carries the call context", "custom t-shirts" in cap, cap[:120])
            check("caption quotes something they actually said",
                  '"' in cap and "1,000 US dollars" in cap, cap[:200])
            check("caption carries the mobile number", "+910000000000" in cap)
            check("so one message holds all four Section 06 items",
                  all(x in cap for x in ("custom t-shirts", "+910000000000"))
                  and sent["image"], sent["path"])

            UM_SENT.clear()
            run(handlers.send_resume(call2, {"demo_url": "https://demo"}))
            check("resume goes as a document, separately",
                  UM_SENT[-1]["path"] == "messages/document"
                  and UM_SENT[-1]["document"] == "https://example/cv.pdf", UM_SENT[-1])

            # The guards must survive a provider swap - that is the whole point
            # of routing both transports through one _guard().
            before = len(UM_SENT)
            try:
                whatsapp.send_text(EVALUATOR_NUMBER, "nope")
                check("evaluator still refused on the new provider", False, "it sent!")
            except whatsapp.WhatsAppError:
                check("evaluator still refused on the new provider", len(UM_SENT) == before)
            try:
                whatsapp.send_text("+919999999999", "nope")
                check("non-allowed destination still refused", False, "it sent!")
            except whatsapp.WhatsAppError:
                check("non-allowed destination still refused", len(UM_SENT) == before)
            try:
                whatsapp.send_template(dest, "any_template")
                check("templates are refused on a provider that has none", False, "sent!")
            except whatsapp.WhatsAppError as exc:
                check("templates are refused on a provider that has none",
                      "no templates" in str(exc), str(exc))
        finally:
            settings.wa_provider = "meta"

        print("\nWEBHOOK SECRET")
        settings.webhook_secret = "s3cret"
        check("wrong secret rejected",
              webhook(client, {"type": "status-update", "call": {"id": "prov-call-1"}},
                      secret="wrong").status_code == 401)
        check("right secret accepted",
              webhook(client, {"type": "status-update", "call": {"id": "prov-call-1"}},
                      secret="s3cret").status_code == 200)
        settings.webhook_secret = ""

        print("\nREAD ROUTES")
        detail = client.get(f"/calls/{call_id}").json()
        check("detail returns turns", len(detail["turns"]) >= 3)
        check("detail returns coverage for all five slots",
              set(detail["coverage"]) == {"products", "catalogue_size", "timeline",
                                          "features", "budget"})
        check("coverage claims nothing when extraction found nothing",
              not any(detail["coverage"].values()))
        check("events captured", len(db.get_events(call_id)) > 5)
        check("404 on unknown call", client.get("/calls/nope").status_code == 404)

        print("\nTIME HANDLING")
        ist = db.to_ist("2026-08-28T10:00:00+00:00")
        check("UTC -> IST is +5:30", ist.hour == 15 and ist.minute == 30, str(ist))

        print("\nACTION RETRIES  (a transient blip must not lose a message)")
        from app import actions as _act
        _act.BACKOFF_SECONDS = 0          # no real sleeping in tests
        attempts = {"n": 0}

        @_act.handler("whatsapp_brochure")
        async def _flaky(call_id, payload):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("EOF occurred in violation of protocol")
            return {"message_id": "wamid.RETRY"}

        PLACED.clear()
        callR = client.post("/calls").json()["call_id"]
        _act.dispatch(callR, "whatsapp_brochure", trigger_source="test")
        act = [a for a in db.get_actions(callR) if a["type"] == "whatsapp_brochure"][0]
        check("a transient TLS failure is retried, not dropped",
              act["status"] == "sent" and attempts["n"] == 3, (act["status"], attempts))
        check("and the attempt count is recorded", act["attempts"] == 3, act["attempts"])

        permanent = {"n": 0}

        @_act.handler("whatsapp_brochure")
        async def _refused(call_id, payload):
            permanent["n"] += 1
            raise RuntimeError("destination is not the allowed destination")

        callR2 = client.post("/calls").json()["call_id"]
        _act.dispatch(callR2, "whatsapp_brochure", trigger_source="test")
        act2 = [a for a in db.get_actions(callR2) if a["type"] == "whatsapp_brochure"][0]
        check("a permanent refusal is NOT retried",
              act2["status"] == "failed" and permanent["n"] == 1,
              (act2["status"], permanent))

        print("\nCLAIMED SEND  (the agent must not be able to lie)")
        # On 30 Aug the model said "you should see it come through now" having
        # never called the tool, on a warm-classified call. Nothing was sent.
        PLACED.clear()
        callA = client.post("/calls").json()["call_id"]
        provA = db.get_call(callA)["provider_call_id"]
        webhook(client, {"type": "transcript", "transcriptType": "final",
                         "role": "assistant", "call": {"id": provA},
                         "transcript": "I'll put that together and send it across."})
        check("a PROMISE to send fires nothing",
              not any(a["type"] == "whatsapp_hot" for a in db.get_actions(callA)),
              db.get_actions(callA))
        webhook(client, {"type": "transcript", "transcriptType": "final",
                         "role": "assistant", "call": {"id": provA},
                         "transcript": "Just sent that to your WhatsApp - "
                                       "you should see it come through now."})
        acts = [a for a in db.get_actions(callA) if a["type"] == "whatsapp_hot"]
        check("a CLAIM to have sent fires the message, making it true",
              len(acts) == 1, db.get_actions(callA))
        check("and is attributed to that path, not faked as a tool call",
              acts and acts[0]["trigger_source"] == "claimed_by_agent", acts)
        webhook(client, {"type": "transcript", "transcriptType": "final",
                         "role": "assistant", "call": {"id": provA},
                         "transcript": "As I said, I have sent it already."})
        check("repeating the claim does not send twice",
              len([a for a in db.get_actions(callA)
                   if a["type"] == "whatsapp_hot"]) == 1, db.get_actions(callA))

        print("\nCALLBACK BOOKING  (from the tool call, as the agent would)")
        from app import callbacks as cb
        PLACED.clear()
        call5 = client.post("/calls").json()["call_id"]
        # Read the provider id back rather than guessing "prov-call-5" - the fake
        # dialler's counter moves whenever a section above adds a call.
        prov5 = db.get_call(call5)["provider_call_id"]
        r = webhook(client, {"type": "tool-calls", "call": {"id": prov5},
                             "toolCallList": [{"id": "cb-1", "function":
                                               {"name": "schedule_callback",
                                                "arguments": {"when": "tomorrow morning"}}}]})
        said = r.json()["results"][0]["result"]
        booked = db.get_callbacks(call5)
        check("spoken time booked from a tool call", len(booked) == 1
              and booked[0]["status"] == "pending", booked)
        # The agent has to be able to SAY the resolved time - that sentence is
        # the only part of this row the evaluator can actually hear.
        check("the result tells the agent a real time to say back",
              "tomorrow morning" in said and "at 10" in said, said)
        check("the resolving rule is recorded, so a vague phrase is defensible",
              "morning=10:00" in (booked[0]["resolution_rule"] or ""), booked[0])

        # Vapi's arguments arrive as an object from some providers and a JSON
        # string from others. Losing a booking to that would be a silly way to
        # drop ten points.
        webhook(client, {"type": "tool-calls", "call": {"id": prov5},
                         "toolCallList": [{"id": "cb-2", "function":
                                           {"name": "schedule_callback",
                                            "arguments": '{"when": "next monday"}'}}]})
        booked = db.get_callbacks(call5)
        check("arguments as a JSON string still book", len(booked) == 2, booked)
        check("restating the time replaces the booking, never adds a second",
              [b["status"] for b in booked] == ["cancelled", "pending"],
              [b["status"] for b in booked])

        r = webhook(client, {"type": "tool-calls", "call": {"id": prov5},
                             "toolCallList": [{"id": "cb-3", "function":
                                               {"name": "schedule_callback",
                                                "arguments": {"when": "whenever"}}}]})
        check("an unparseable time books nothing and says so",
              len(db.get_callbacks(call5)) == 2
              and "Could not work out" in r.json()["results"][0]["result"],
              r.json()["results"][0]["result"])

        # Booking must also put the time in writing. Otherwise the lead has
        # nothing but memory until the phone rings, and we have no visible proof
        # the scheduler ran at all.
        confirms = [a for a in db.get_actions(call5) if a["type"] == "callback_confirm"]
        check("booking a callback also confirms it in writing",
              len(confirms) == 1 and confirms[0]["status"] == "sent", confirms)
        check("attributed to the booking, not to call end",
              confirms and confirms[0]["trigger_source"] == "callback_booked", confirms)

        print("\nCALLBACK EXECUTION  (the worker actually dials)")
        from datetime import datetime, timedelta, timezone as _tzone
        now = datetime.now(_tzone.utc)
        pending = [b for b in db.get_callbacks(call5) if b["status"] == "pending"][0]

        PLACED.clear()
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(cb.tick(now))
        check("a callback that is not due yet is left alone", PLACED == [], PLACED)

        # Due now: the worker should place exactly one call, linked to its origin.
        db.update_callback(pending["id"], status="pending")
        with db.conn() as cx:
            cx.execute("UPDATE callbacks SET resolved_at_utc=? WHERE id=?",
                       ((now - timedelta(minutes=1)).isoformat(timespec="seconds"),
                        pending["id"]))
        placed = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            cb.tick(now))
        check("a due callback places exactly one call", len(placed) == 1
              and PLACED == ["+919876543210"], (placed, PLACED))
        check("the new call records what it is a callback of",
              db.get_call(placed[0])["is_callback_of"] == call5,
              db.get_call(placed[0]))
        row = [b for b in db.get_callbacks(call5) if b["id"] == pending["id"]][0]
        check("the callback is marked placed, so it cannot fire twice",
              row["status"] == "placed" and row["placed_call_id"] == placed[0], row)

        PLACED.clear()
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(cb.tick(now))
        check("a second pass does not re-dial it", PLACED == [], PLACED)

        # Misfire: the process was down when it came due. Ringing someone hours
        # late is worse than not ringing - and this is what stops a stale row in
        # a dev database from dialling on the next server start.
        db.add_callback(call5, (now - timedelta(hours=6)).isoformat(timespec="seconds"),
                        spoken_phrase="ages ago", resolution_rule="test")
        PLACED.clear()
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(cb.tick(now))
        check("a badly overdue callback is marked missed, not dialled",
              PLACED == [] and any(b["status"] == "missed" for b in db.get_callbacks(call5)),
              [(b["status"], b["spoken_phrase"]) for b in db.get_callbacks(call5)])

        print("\nCALLBACK CONTEXT  (a callback is not a cold call)")
        ctx = cb.build_context(call4)
        check("context carries what they already told us",
              "custom-made t-shirts" in ctx and "300 designs" in ctx, ctx)
        check("and forbids re-asking it", "Do NOT ask any of the above again" in ctx)
        check("opening line references the last conversation",
              "again" in (cb.opening_line(call4) or ""), cb.opening_line(call4))
        check("no context when nothing was extracted", cb.build_context(call3) == "",
              cb.build_context(call3))
        check("and then no opening override, so the normal greeting stands",
              cb.opening_line(call3) is None)

        # The context has to actually reach Vapi, not just be computed.
        PLACED.clear()
        OVERRIDES.clear()
        db.add_callback(call4, (datetime.now(_tzone.utc) - timedelta(minutes=1))
                        .isoformat(timespec="seconds"), spoken_phrase="tomorrow")
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            cb.tick(datetime.now(_tzone.utc)))
        ov = OVERRIDES[-1] if OVERRIDES else {}
        check("callback_context is sent to the provider",
              "t-shirts" in ov.get("variableValues", {}).get("callback_context", ""), ov)
        check("and the first message is overridden",
              "again" in ov.get("firstMessage", ""), ov)

        print("\nRECONCILIATION  (the webhook that never arrived)")
        from app import reconcile
        # A live call must NOT be swept mid-conversation.
        PLACED.clear()
        call6 = client.post("/calls").json()["call_id"]
        # The provider id comes from the fake dialler's counter, which has moved
        # on since call1 - read it back rather than guessing "prov-call-6".
        prov6 = db.get_call(call6)["provider_call_id"]
        webhook(client, {"type": "status-update", "status": "in-progress",
                         "call": {"id": prov6}})
        now = datetime.now(_tzone.utc)
        swept = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            reconcile.sweep(now))
        check("a call that is still talking is left alone", call6 not in swept, swept)

        # Now age it past the stale window, with the provider reporting ended.
        # A REAL conversation, not a one-liner: the post-call follow-up is now
        # skipped for calls where nobody actually spoke, so the fixture has to
        # clear that bar or this stops testing reconciliation and starts
        # testing the silent-call guard.
        VAPI_CALLS[prov6] = {
            "status": "ended", "endedReason": "customer-ended-call",
            "artifact": {"messages": [
                {"role": "bot", "message": "What do you sell?"},
                {"role": "user", "message": "We sell sarees, mostly custom designs."},
                {"role": "bot", "message": "How many designs?"},
                {"role": "user", "message": "Around two hundred at any time."}]}}
        swept = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            reconcile.sweep(now + timedelta(minutes=10)))
        check("a call whose webhook was lost gets reconciled", call6 in swept, swept)
        call = db.get_call(call6)
        check("reconciled call is marked ended", call["status"] == "ended", call["status"])
        check("its transcript is backfilled from the provider",
              any("sarees" in t["text"] for t in db.get_turns(call6)),
              [t["text"] for t in db.get_turns(call6)])
        check("and the post-call follow-up finally fires",
              any(a["type"] == "whatsapp_followup" for a in db.get_actions(call6)),
              db.get_actions(call6))

        again = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            reconcile.sweep(now + timedelta(minutes=20)))
        check("an already-finalized call is not swept twice", call6 not in again, again)
        check("and still only one follow-up exists",
              sum(1 for a in db.get_actions(call6)
                  if a["type"] == "whatsapp_followup") == 1, db.get_actions(call6))

        print("\nEVALUATOR ARMING  (the call must never outrun the messages)")
        # The real bug this replaces: the dialler had no evaluator check while
        # the WhatsApp sender did, so pointing ALLOWED_DESTINATION at the
        # evaluator without ALLOW_EVALUATOR=1 gave a flawless call and refused
        # every message on it. Both paths must now agree in all three states.
        from app import dialler                                    # noqa: E402
        original = (settings.allowed_destination, settings.allow_evaluator)
        try:
            def both(number):
                """(dial_ok, whatsapp_ok) for one destination."""
                dial_ok = settings.permits(number)[0]
                try:
                    whatsapp._check_destination(number)
                    wa_ok = True
                except whatsapp.WhatsAppError:
                    wa_ok = False
                return dial_ok, wa_ok

            settings.allowed_destination = "+919876543210"
            settings.allow_evaluator = False
            check("own number: dial and message both allowed",
                  both("+919876543210") == (True, True), both("+919876543210"))
            check("evaluator refused from a normal run",
                  both(EVALUATOR_NUMBER) == (False, False), both(EVALUATOR_NUMBER))

            # HALF-ARMED: the state that used to lose most of the scorecard.
            settings.allowed_destination = EVALUATOR_NUMBER
            settings.allow_evaluator = False
            check("half-armed: dial and message BOTH refused, never split",
                  both(EVALUATOR_NUMBER) == (False, False), both(EVALUATOR_NUMBER))
            check("half-armed is reported on /health",
                  settings.evaluator_ready()[0] is False
                  and "every WhatsApp would be refused" in settings.evaluator_ready()[1],
                  settings.evaluator_ready())
            try:
                dialler.place()
                check("half-armed dialler refuses to place the call", False, "it dialled")
            except dialler.DialError as exc:
                check("half-armed dialler refuses to place the call",
                      exc.status == 403, exc.detail)

            # ARMED: the real run.
            settings.allow_evaluator = True
            check("armed: dial and message both allowed",
                  both(EVALUATOR_NUMBER) == (True, True), both(EVALUATOR_NUMBER))
            check("armed is reported ready on /health",
                  settings.evaluator_ready()[0] is True, settings.evaluator_ready())
            check("armed still refuses a number that is not the destination",
                  both("+919876543210") == (False, False), both("+919876543210"))
        finally:
            settings.allowed_destination, settings.allow_evaluator = original

        print("\nCALLBACK CLAIM  (cannot double-fire)")
        cb = db.add_callback(call_id, "2020-01-01T00:00:00+00:00", "tomorrow morning", "morning->10:00")
        first = db.claim_due_callback()
        second = db.claim_due_callback()
        check("due callback claimed once", first is not None and first["id"] == cb)
        check("second claim gets nothing", second is None)

        print("\nMULTI-APP  (Calendar / HubSpot / Slack - transports faked, logic real)")
        import re as _re
        import time as _time
        from datetime import datetime as _dt, timedelta as _td
        from app import callfacts, gcal, hubspot, integrations, rest, slack

        def eventually(cond, timeout=5.0):
            """App handlers run on the server's loop in another thread; give them a moment."""
            end = _time.time() + timeout
            while _time.time() < end:
                if cond():
                    return True
                _time.sleep(0.02)
            return cond()

        settings.google_client_id = "gid"
        settings.google_client_secret = "gsecret"
        settings.google_refresh_token = "grefresh"
        settings.google_calendar_id = "primary"
        settings.hubspot_token, settings.hubspot_portal_id = "pat-test", "999"
        settings.hubspot_stage_hot, settings.hubspot_stage_warm = "STAGE_HOT", "STAGE_WARM"
        settings.slack_bot_token, settings.slack_channel_id = "xoxb-test", "C0TEST"
        settings.public_base_url = "https://relay.test"

        BUSY, EVENTS, FREEBUSY_DOWN = [], {}, [False]

        def fake_google(method, url, body=None, form=None, timeout=10, auth=True):
            if url.endswith("/freeBusy"):
                if FREEBUSY_DOWN[0]:
                    raise rest.ApiError("google", 0, "timed out after 0.8s")
                return {"calendars": {"primary": {"busy": [
                    {"start": s.isoformat(), "end": e.isoformat()} for s, e in BUSY]}}}
            if method == "POST" and url.endswith("/events"):
                if body["id"] in EVENTS:
                    raise rest.ApiError("google", 409, "The requested identifier already exists.")
                EVENTS[body["id"]] = body
            elif method == "PUT":
                EVENTS[body["id"]] = body
            else:
                raise AssertionError(f"unexpected Google call {method} {url}")
            return {"id": body["id"], "htmlLink": "https://calendar.test/" + body["id"]}

        HS = {"contacts": {}, "deals": {}, "notes": {}}

        def fake_hubspot(method, path, body=None, timeout=15):
            if path.startswith("/account-info"):
                return {"portalId": 999, "uiDomain": "app-na2.hubspot.com"}
            if path == "/crm/v3/objects/contacts/search":
                phone = body["filterGroups"][0]["filters"][0]["value"]
                hits = [{"id": i, "properties": p} for i, p in HS["contacts"].items()
                        if p.get("phone") == phone]
                return {"total": len(hits), "results": hits[:1]}
            m = _re.match(r"/crm/v3/objects/(contacts|deals|notes)(?:/([^/?]+))?", path)
            kind, rid = m.group(1), m.group(2)
            if method == "POST":
                rid = f"{kind}-{len(HS[kind]) + 1}"
                HS[kind][rid] = dict(body["properties"], associations=body.get("associations"))
            elif method == "PATCH":
                HS[kind][rid].update(body["properties"])
            elif method != "GET":
                raise AssertionError(f"unexpected HubSpot call {method} {path}")
            return {"id": rid, "properties": HS[kind][rid]}

        SLACK = {"posts": [], "updates": []}

        def fake_slack(method, body, form=False, timeout=10):
            if method == "chat.postMessage":
                ts = f"1700000000.{len(SLACK['posts']) + 1:06d}"
                SLACK["posts"].append(dict(body, ts=ts))
                return {"ok": True, "ts": ts, "channel": body["channel"]}
            if method == "chat.update":
                SLACK["updates"].append(body)
                return {"ok": True, "ts": body["ts"], "channel": body["channel"]}
            raise AssertionError(f"unexpected Slack call {method}")

        gcal._request, hubspot._request, slack._request = fake_google, fake_hubspot, fake_slack
        hubspot._UI["domain"] = None

        def top_posts():
            return [p for p in SLACK["posts"] if "thread_ts" not in p]

        def say(prov, role, text):
            webhook(client, {"type": "transcript", "transcriptType": "final", "role": role,
                             "transcript": text, "call": {"id": prov}})

        def book_via_tool(prov, when, tool_id):
            r = webhook(client, {"type": "tool-calls", "call": {"id": prov}, "toolCallList": [
                {"id": tool_id, "function": {"name": "schedule_callback",
                                             "arguments": {"when": when}}}]})
            return r.json()["results"][0]["result"]

        h = client.get("/health").json()
        check("/health reports all three apps as configured",
              all(h["integrations"][a]["configured"]
                  for a in ("google_calendar", "hubspot", "slack")), h.get("integrations"))

        # --- a hot lead, mid-call
        PLACED.clear()
        callM = client.post("/calls").json()["call_id"]
        provM = db.get_call(callM)["provider_call_id"]
        EXTRACTED[0] = slots(products=("groceries", "We sell groceries"),
                             catalogue_size=("around 200 products", "around 200 products"))
        say(provM, "assistant", "What do you sell?")
        say(provM, "user", "We sell groceries, around 200 products. Send me the details.")
        check("a hot read creates the HubSpot contact",
              eventually(lambda: len(HS["contacts"]) == 1), HS["contacts"])
        check("and a deal at the HOT stage, associated with that contact",
              eventually(lambda: db.get_call(callM)["hubspot_deal_stage"] == "STAGE_HOT")
              and len(HS["deals"]) == 1
              and list(HS["deals"].values())[0]["associations"][0]["to"]["id"]
              == db.get_call(callM)["hubspot_contact_id"], HS["deals"])
        check("the sales team is alerted in Slack while the call is live",
              eventually(lambda: len(top_posts()) == 1)
              and "on the line now" in top_posts()[0]["text"], SLACK["posts"])

        say(provM, "user", "Send me the details.")
        _time.sleep(0.3)
        check("a repeated hot read makes no second deal and no second post",
              len(HS["deals"]) == 1 and len(top_posts()) == 1,
              (len(HS["deals"]), len(top_posts())))

        # --- a callback on a free slot
        said = book_via_tool(provM, "tomorrow at 4", "m-cb1")
        check("a free slot is booked and said back", said.startswith("Booked"), said)
        check("the calendar check is recorded on the booking",
              (callfacts.active_callback(callM) or {}).get("availability") == "free",
              callfacts.active_callback(callM))
        check("the calendar event is created from the booking",
              eventually(lambda: bool(db.get_call(callM)["gcal_event_link"]))
              and len(EVENTS) == 1, list(EVENTS))
        eid = db.get_call(callM)["gcal_event_id"]
        check("the event id uses only Google's base32hex alphabet",
              bool(_re.fullmatch(r"[0-9a-v]{5,1024}", eid or "")), eid)
        check("the event sits at the resolved IST time",
              list(EVENTS.values())[0]["start"]["dateTime"].endswith("T16:00:00+05:30"),
              list(EVENTS.values())[0]["start"])
        check("the Slack message gains the callback by EDIT, not a new post",
              eventually(lambda: any("Callback" in json.dumps(u["blocks"])
                                     for u in SLACK["updates"]))
              and len(top_posts()) == 1, len(top_posts()))

        said = book_via_tool(provM, "tomorrow at 5", "m-cb2")
        check("restating the time MOVES the one event, never adds a second",
              eventually(lambda: list(EVENTS.values())[0]["start"]["dateTime"]
                         .endswith("T17:00:00+05:30")) and len(EVENTS) == 1,
              [e["start"] for e in EVENTS.values()])
        check("and a callback sync never moves a hot deal backwards",
              db.get_call(callM)["hubspot_deal_stage"] == "STAGE_HOT")

        # --- escalation
        say(provM, "user", "Actually, can I speak to a real person about this?")
        check("asking for a human pages the team with a threaded, broadcast reply",
              eventually(lambda: any(p.get("thread_ts") and p.get("reply_broadcast")
                                     for p in SLACK["posts"])), SLACK["posts"])
        say(provM, "user", "I really want to talk to a human.")
        _time.sleep(0.3)
        check("and pages only once per call",
              sum(1 for p in SLACK["posts"] if p.get("thread_ts")) == 1)

        # --- call end
        end_report = {"type": "end-of-call-report", "endedReason": "customer-ended-call",
                      "call": {"id": provM}, "summary": "Grocer, wants a store in a month.",
                      "artifact": {"messages": []}}
        webhook(client, end_report)
        check("call end writes ONE HubSpot note",
              eventually(lambda: len(HS["notes"]) == 1), HS["notes"])
        note = next(iter(HS["notes"].values()), {})
        assoc_types = sorted(a["types"][0]["associationTypeId"]
                             for a in note.get("associations") or [])
        check("the note is associated with the contact (202) and the deal (214)",
              assoc_types == [202, 214], assoc_types)
        check("the note quotes the lead verbatim",
              "We sell groceries" in note.get("hs_note_body", ""),
              note.get("hs_note_body", "")[:200])
        check("the Slack message now says the call ended - edited, not re-posted",
              eventually(lambda: bool(SLACK["updates"])
                         and "Call ended" in SLACK["updates"][-1]["text"])
              and len(top_posts()) == 1, len(top_posts()))
        webhook(client, end_report)
        _time.sleep(0.3)
        check("replaying the end of call adds no second note and no second post",
              len(HS["notes"]) == 1 and len(top_posts()) == 1,
              (len(HS["notes"]), len(top_posts())))

        # --- a warm lead: taken slot, contact reuse, stage only moves forward
        PLACED.clear()
        callW = client.post("/calls").json()["call_id"]
        provW = db.get_call(callW)["provider_call_id"]
        four = (_dt.now(db.IST) + _td(days=1)).replace(hour=16, minute=0, second=0,
                                                       microsecond=0)
        BUSY.append((four, four + _td(hours=1)))
        db.add_classification(callW, "warm", barrier="timing")
        integrations.on_classified(callW)
        check("a warm read with no callback is contact-only - no deal",
              bool(db.get_call(callW)["hubspot_contact_id"])
              and not db.get_call(callW)["hubspot_deal_id"], db.get_call(callW))
        check("the same phone reuses the existing contact instead of duplicating it",
              len(HS["contacts"]) == 1, HS["contacts"])

        said = book_via_tool(provW, "tomorrow at 4", "w-1")
        check("a taken slot is NOT booked",
              said.startswith("Not booked") and callfacts.active_callback(callW) is None, said)
        check("and the agent is handed the next free slot to offer", "17:00" in said, said)
        said = book_via_tool(provW, "tomorrow at 5", "w-2")
        check("accepting the offered slot books it",
              said.startswith("Booked")
              and (callfacts.active_callback(callW) or {}).get("availability") == "free", said)
        check("warm + a booked callback earns a deal at the WARM stage",
              eventually(lambda: db.get_call(callW)["hubspot_deal_stage"] == "STAGE_WARM"),
              db.get_call(callW))
        said = book_via_tool(provW, "tomorrow at 4", "w-3")
        check("insisting on the taken slot books it, flagged as such",
              said.startswith("Booked")
              and (callfacts.active_callback(callW) or {}).get("availability") == "busy_confirmed",
              said)

        db.add_classification(callW, "hot")
        integrations.on_classified(callW)
        check("a later hot read moves the deal forward",
              db.get_call(callW)["hubspot_deal_stage"] == "STAGE_HOT", db.get_call(callW))
        db.add_classification(callW, "warm", barrier="budget")
        hubspot.sync_call(callW)
        deal = HS["deals"][db.get_call(callW)["hubspot_deal_id"]]
        check("and a cooler read never moves it back",
              db.get_call(callW)["hubspot_deal_stage"] == "STAGE_HOT"
              and deal["dealstage"] == "STAGE_HOT", deal)

        # --- the calendar does not answer
        FREEBUSY_DOWN[0] = True
        PLACED.clear()
        callU = client.post("/calls").json()["call_id"]
        said = book_via_tool(db.get_call(callU)["provider_call_id"], "tomorrow at 11", "u-1")
        check("a calendar that does not answer never blocks the booking",
              said.startswith("Booked")
              and (callfacts.active_callback(callU) or {}).get("availability") == "unchecked",
              said)
        FREEBUSY_DOWN[0] = False

        print("\nGROQ  (strict JSON schema - transport faked)")
        for model in (classifier.LeadRead, extraction.ExtractedSlots):
            schema = classifier.strict_schema(model)
            objects = []

            def walk(node):
                if isinstance(node, dict):
                    if node.get("type") == "object":
                        objects.append(node)
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(schema)
            flat = json.dumps(schema)
            check(f"{model.__name__}: no $ref or $defs left for strict mode",
                  "$ref" not in flat and "$defs" not in flat, flat[:160])
            check(f"{model.__name__}: every object closed and fully required",
                  bool(objects) and all(o["additionalProperties"] is False
                                        and set(o["required"]) == set(o["properties"])
                                        for o in objects), flat[:160])

        GROQ_SENT = []
        classifier._groq_request = lambda payload: GROQ_SENT.append(payload) or {"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps({
                "label": "warm", "confidence": 0.7, "barrier": "decision_maker",
                "evidence_quote": "my brother decides", "reasoning": "defers"})}}]}
        got = classifier._call_groq("sys", "user", "openai/gpt-oss-20b", classifier.LeadRead, 300)
        sent = GROQ_SENT[-1]
        check("Groq is asked for strict json_schema output",
              sent["response_format"]["type"] == "json_schema"
              and sent["response_format"]["json_schema"]["strict"] is True,
              sent["response_format"].get("type"))
        check("the reasoning budget is added on top of the answer budget",
              sent["max_completion_tokens"] > 300, sent.get("max_completion_tokens"))
        check("and the reply comes back as the validated model",
              isinstance(got, classifier.LeadRead) and got.barrier == "decision_maker", got)

        print("\nNO CALLING HOURS  (the lead picks the time)")
        night = (_dt.now(db.IST) + _td(days=1)).replace(hour=23, minute=0, second=0,
                                                        microsecond=0)
        alt = gcal.next_free(night, [(night, night + _td(hours=2))])
        check("the next free slot is not limited to calling hours",
              alt == night + _td(hours=2), alt)

        print("\nWHATSAPP OFF  (the default: the agent never promises the lead a message)")
        settings.whatsapp_enabled = False
        try:
            PLACED.clear()
            callO = client.post("/calls").json()["call_id"]
            provO = db.get_call(callO)["provider_call_id"]
            r = webhook(client, {"type": "tool-calls", "call": {"id": provO}, "toolCallList": [
                {"id": "o-1", "function": {"name": "send_details_now", "arguments": {}}}]})
            said = r.json()["results"][0]["result"]
            check("a stale send_details_now call is answered without claiming a send",
                  "WhatsApp" not in said and said.startswith("Nothing is sent"), said)
            say(provO, "user", "We sell sarees, lots of designs. Send me the details.")
            say(provO, "assistant", "I've sent the details to your WhatsApp now.")
            said = book_via_tool(provO, "tomorrow at 1 am", "o-2")
            check("a 1 am callback is booked with no calling-hours objection",
                  said.startswith("Booked") and "hours" not in said, said)
            webhook(client, {"type": "end-of-call-report", "endedReason": "customer-ended-call",
                             "call": {"id": provO}, "artifact": {"messages": [
                                 {"role": "user", "message": "We sell sarees, lots of designs."},
                                 {"role": "user", "message": "Send me the details please."}]}})
            _time.sleep(0.3)
            sent_wa = [a["type"] for a in db.get_actions(callO)
                       if a["type"].startswith("whatsapp") or a["type"] == "callback_confirm"]
            check("no WhatsApp of any kind fires: hot read, claimed send, booking, call end",
                  sent_wa == [], sent_wa)
            check("while Slack and HubSpot still act on the hot read",
                  eventually(lambda: bool(db.get_call(callO)["hubspot_deal_id"])
                             and bool(db.get_call(callO)["slack_ts"])), db.get_call(callO))
        finally:
            settings.whatsapp_enabled = True

    print("\n" + "=" * 46)
    print(f"{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
