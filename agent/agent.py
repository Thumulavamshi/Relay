#!/usr/bin/env python3
"""
Phase 2: the English discovery conversation.

Standard library only - nothing to pip install.

    python agent/agent.py check          offline validation, no network, no spend
    python agent/agent.py deploy         push prompt.md + assistant.json to Vapi (free)
    python agent/agent.py call --yes     place one call, poll, save the transcript
    python agent/agent.py review         score the most recent call (offline)
    python agent/agent.py review <id>    score a specific call
    python agent/agent.py inspect        show what Vapi actually stored (free)

Safety: refuses to dial the evaluator's number under any circumstances.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPT_PATH = os.path.join(HERE, "prompt.md")
ASSISTANT_PATH = os.path.join(HERE, "assistant.json")
TRANSCRIPTS = os.path.join(HERE, "transcripts")
API = "https://api.vapi.ai"

# Credentials are shared with the milestone-1 probe rather than duplicated.
ENV_CANDIDATES = [
    os.path.join(HERE, ".env"),
    os.path.join(ROOT, ".env"),
    os.path.join(ROOT, "experiments", "voice-feasibility", ".env"),
]

EVALUATOR_NUMBER = "+918688664337"
OK, BAD, WARN, INFO = "  [ok]", "  [FAIL]", "  [warn]", "  [..]"

# The five things a discovery call has to learn, and the words the agent would
# plausibly use to raise each one. Used by `review` to check coverage without an
# LLM call - it measures whether the AGENT RAISED the topic, which is far more
# reliable to detect than whether an answer was successfully extracted.
# Hindi and Telugu terms are here because a real Hindi call scored 0/5 on this
# check while the transcript showed all five topics asked. An English-only
# heuristic does not measure a multilingual agent - it just fails it, and a
# false alarm here sends us fixing something that already works.
TOPICS = {
    "what they sell": ["what kind of product", "what do you sell", "do you sell",
                       # "What is it you sell?" is the phrasing the agent now
                       # actually uses; without it a raised topic scores as missed.
                       "is it you sell", "what are you selling", "what you sell",
                       "what sort of", "what products", "kind of business",
                       "what are you selling",
                       "क्या बेचते", "क्या बेचती", "kya bechte", "kya bechti",
                       "ఏమి అమ్ము", "ఏం అమ్ము", "em ammuta", "emi ammuta"],
    "catalogue size": ["how many", "catalogue", "catalog", "how large", "how big",
                       "number of products", "designs", "skus", "few dozen",
                       "a hundred", "few hundred", "items are you",
                       "कितने", "कितनी", "kitne", "kitni", "डिज़ाइन्स", "प्रोडक्ट्स",
                       "ఎన్ని", "enni", "ప్రొడక్ట్",
                       # "ఎంత ఉత్పత్తులు" is wrong Telugu (ఎంత is for amounts,
                       # ఎన్ని for countables) but the agent says it, and a
                       # measurement that only recognises correct grammar scores
                       # a raised topic as missed. Matched as a PHRASE, never as
                       # bare "ఎంత", which also appears in "బడ్జెట్ ఎంత".
                       "ఎంత ఉత్పత్తుల", "ఉత్పత్తులు ఉన్నాయి", "ఉత్పత్తుల కోసం"],
    "timeline": ["when would", "when do you", "how soon", "go live", "timeline",
                 "by when", "want it live", "when are you",
                 "कब तक", "कब लाइव", "kab tak", "कब चाहिए",
                 "ఎప్పుడు", "eppudu", "ఎప్పటికి"],
    "features": ["payment", "delivery", "tracking", "feature", "variant", "checkout",
                 "whatsapp", "cash on delivery", "sizes", "colours", "colors",
                 "फीचर्स", "फ़ीचर", "पेमेंट", "डिलीवरी", "ट्रैकिंग", "features chahiye",
                 "ఫీచర్", "చెల్లింపు", "ట్రాకింగ్"],
    "budget": ["budget", "how much were you", "how much are you", "spend",
               "invest", "price range", "range in mind",
               "बजट", "बजेट", "bajat", "कितना खर्च", "रेंज",
               "బడ్జెట్", "బడ్జెట్ ఎంత", "ఖర్చు"],
}


# ---------------------------------------------------------------- env + config

def load_env():
    """Parse the first .env found into os.environ. Values are never printed."""
    for path in ENV_CANDIDATES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        return path
    return None


def load_prompt():
    """Everything after the first '---' line in prompt.md is the system prompt."""
    with open(PROMPT_PATH, encoding="utf-8") as fh:
        text = fh.read()
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError("prompt.md must contain a '---' line separating notes from the prompt")
    return parts[1].strip()


def load_tool_definitions():
    """The custom tools, which are SEPARATE Vapi resources, not part of the assistant."""
    with open(ASSISTANT_PATH, encoding="utf-8") as fh:
        return json.load(fh).get("_tool_definitions") or []


def load_assistant(tool_ids=None):
    """assistant.json + prompt.md + .env overrides -> the exact Vapi payload.

    `tool_ids` are attached as model.toolIds. Vapi rejects a top-level `tools`
    property on an assistant with 400 "property tools should not exist" - custom
    tools are their own resource and are referenced by id.
    """
    with open(ASSISTANT_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = {k: v for k, v in raw.items() if not k.startswith("_")}

    cfg.setdefault("model", {})["messages"] = [
        {"role": "system", "content": load_prompt()}
    ]

    # Voice override, so a voice can be swapped between test calls from .env.
    provider, voice_id = os.environ.get("VOICE_PROVIDER"), os.environ.get("VOICE_ID")
    voice_model = os.environ.get("VOICE_MODEL")
    if provider or voice_id or voice_model:
        cfg.setdefault("voice", {})
        if provider:
            cfg["voice"]["provider"] = provider
            # Switching provider must drop the old provider's model, or Vapi 400s
            # on e.g. azure carrying a cartesia sonic model.
            if not voice_model:
                cfg["voice"].pop("model", None)
        if voice_id:
            cfg["voice"]["voiceId"] = voice_id
        if voice_model:
            cfg["voice"]["model"] = voice_model

    # Endpointing overrides. These are the latency dials - being able to change
    # them between calls without editing files is the whole point.
    plan = cfg.setdefault("startSpeakingPlan", {})
    if os.environ.get("ENDPOINT_WAIT"):
        plan["waitSeconds"] = float(os.environ["ENDPOINT_WAIT"])
    tep = plan.setdefault("transcriptionEndpointingPlan", {})
    if os.environ.get("ENDPOINT_NO_PUNCT"):
        tep["onNoPunctuationSeconds"] = float(os.environ["ENDPOINT_NO_PUNCT"])
    if os.environ.get("ENDPOINT_PUNCT"):
        tep["onPunctuationSeconds"] = float(os.environ["ENDPOINT_PUNCT"])

    # Where Vapi pushes live events. Kept out of the config file so the same
    # file works against a tunnel today and the deployed host later.
    server_url = (os.environ.get("SERVER_URL") or "").rstrip("/")
    if server_url:
        cfg["serverUrl"] = f"{server_url}/vapi/webhook"
        secret = os.environ.get("VAPI_WEBHOOK_SECRET")
        if secret:
            cfg["serverUrlSecret"] = secret
    else:
        # Without this the mid-call action cannot fire: no transcripts reach the
        # understanding lane and no tool call reaches our handler.
        cfg.pop("serverMessages", None)

    # Attached by id. Never a top-level `tools` property - Vapi 400s on that.
    if tool_ids:
        cfg.setdefault("model", {})["toolIds"] = list(tool_ids)

    # Room ambience. The assignment hints it reduces hang-ups, but it may cost
    # STT accuracy - off unless explicitly switched on, so it can be A/B tested.
    if os.environ.get("BACKGROUND_SOUND"):
        cfg["backgroundSound"] = os.environ["BACKGROUND_SOUND"]

    return cfg


# ---------------------------------------------------------------- http

def request(method, path, body=None, timeout=30):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ.get("VAPI_API_KEY", ""),
            "Content-Type": "application/json",
            "User-Agent": "ElevateBox-agent/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:900]
        raise SystemExit(f"\n{BAD} Vapi API {exc.code} on {method} {path}\n  {detail}\n"
                         + hint_for(exc.code, detail))
    except urllib.error.URLError as exc:
        raise SystemExit(f"\n{BAD} Network error reaching Vapi: {exc.reason}")


def hint_for(code, detail):
    low = detail.lower()
    if code in (401, 403):
        return "  HINT: use the PRIVATE key from Vapi -> API Keys.\n"
    if "voice" in low:
        return "  HINT: voice rejected. Set VOICE_PROVIDER / VOICE_ID in .env.\n"
    if "smartendpointing" in low or "startspeakingplan" in low:
        return ("  HINT: your Vapi plan may not expose smart endpointing. Remove\n"
                "        startSpeakingPlan.smartEndpointingPlan from assistant.json and retry.\n")
    if "phonenumber" in low:
        return "  HINT: VAPI_PHONE_NUMBER_ID wrong, or Twilio number not imported.\n"
    return ""


# ---------------------------------------------------------------- safety

def destination():
    """The number we dial. ALLOWED_DESTINATION wins, TEST_NUMBER is the fallback.

    Same precedence as backend/app/config.py on purpose. They used to disagree -
    the CLI dialled TEST_NUMBER while the backend messaged ALLOWED_DESTINATION -
    so setting one and not the other would ring one phone and WhatsApp another.
    """
    return (os.environ.get("ALLOWED_DESTINATION")
            or os.environ.get("TEST_NUMBER", "")).strip()


def valid_e164(num):
    return bool(re.fullmatch(r"\+[1-9]\d{7,14}", num or ""))


def check_destination(num):
    problems = []
    if not num:
        return ["no destination set - put TEST_NUMBER (or ALLOWED_DESTINATION) in .env"]
    digits = re.sub(r"\D", "", num)
    if digits.endswith(re.sub(r"\D", "", EVALUATOR_NUMBER)[-10:]):
        problems.append("TEST_NUMBER is the EVALUATOR'S number. Rehearsal never dials it.")
    if not valid_e164(num):
        problems.append(f"TEST_NUMBER '{num}' is not E.164 (needs +91XXXXXXXXXX)")
    elif not num.startswith("+91"):
        problems.append(f"TEST_NUMBER '{num}' is not an Indian (+91) number")
    return problems


# ---------------------------------------------------------------- commands

def cmd_check():
    print("\nOFFLINE CHECK  (no network, no spend)\n" + "-" * 52)
    fails = []

    try:
        prompt = load_prompt()
        print(f"{OK} prompt.md loads ({len(prompt.split())} words)")
    except Exception as exc:
        print(f"{BAD} prompt.md: {exc}")
        return 1

    if "EDIT ME" in prompt:
        print(f"{WARN} prompt still contains the 'EDIT ME' marker - the pricing block")
        print("       is placeholder. The agent WILL quote those numbers on a live call.")

    try:
        cfg = load_assistant()
        print(f"{OK} assistant.json parses and merges with the prompt")
    except Exception as exc:
        print(f"{BAD} assistant.json: {exc}")
        return 1

    if any(k.startswith("_") for k in cfg):
        print(f"{BAD} _comment keys leaked into the payload")
        fails.append("_comment")
    else:
        print(f"{OK} _comment keys stripped")

    sysmsg = cfg["model"]["messages"][0]["content"]
    print(f"{OK} system prompt injected ({len(sysmsg)} chars)")

    t, v = cfg.get("transcriber", {}), cfg.get("voice", {})
    langs = t.get("languages")
    multi_stt = langs == [] or (isinstance(langs, list) and len(langs) > 1)
    if langs == []:
        how = "auto-detect across ALL languages"
    elif multi_stt:
        how = f"code-switching within {langs}"
    else:
        how = f"pinned to {langs}"
    print(f"{OK} transcriber {t.get('provider')}/{t.get('model')} - {how}")
    if langs == []:
        # Measured 29 Aug: unconstrained auto-detect transcribed spoken ENGLISH
        # as Marathi, and the agent mirrored the language it was handed.
        print(f"{WARN} an EMPTY list lets the detector choose any of 60+ languages.")
        print("       On 8 kHz telephony audio it has mis-fired. Name the three")
        print('       you actually want instead: ["en", "hi", "te"].')
    # A multilingual transcriber feeding a single-language voice means the agent
    # HEARS Telugu and answers in mispronounced English - worse than English-only,
    # because it reads as a bug to the person on the call.
    #
    # Telugu-capable per Vapi's own validator: cartesia sonic-3 and newer. Azure
    # has no voice that covers te+hi+en, so any azure voice fails this check by
    # design, not by oversight.
    model = str(v.get("model", ""))
    multilingual = (v.get("provider") == "cartesia"
                    and (model.startswith("sonic-3") or model.startswith("sonic-4"))
                    or "multilingual" in str(v.get("voiceId", "")).lower())
    if multi_stt and not multilingual:
        print(f"{WARN} transcriber is multilingual but voice "
              f"{v.get('provider')}/{v.get('voiceId')} {model} may not speak te/hi.")
        print("       Telugu needs cartesia sonic-3 or newer. Set VOICE_PROVIDER /")
        print("       VOICE_ID, or pin transcriber.languages back to ['en'].")
        print("       Bake-off script: agent/language-test-script.md")
    elif multi_stt:
        print(f"{OK} voice model {model} covers te/hi/en (language left unset = "
              f"follows the text)")
    print(f"{OK} voice {v.get('provider')} / {v.get('voiceId')}")

    # Regression guard. Vapi rejects a top-level `tools` on an assistant with
    # 400 "property tools should not exist"; custom tools are their own resource
    # and attach via model.toolIds. Caught offline so a deploy never fails on it.
    if "tools" in cfg:
        print(f"{BAD} payload has a top-level `tools` property - Vapi rejects this")
        print("       with 400. Tools must attach via model.toolIds.")
        fails.append("tools")
    else:
        print(f"{OK} no top-level `tools` (attaches by id via model.toolIds)")

    # Any {{variable}} in the prompt must have a value supplied at call time, or
    # Vapi ships the literal braces to the model. Only callback_context is
    # wired; anything else is a typo waiting to be spoken aloud.
    import re as _re
    unknown = {v for v in _re.findall(r"\{\{(\w+)\}\}", load_prompt())} - {"callback_context"}
    if unknown:
        print(f"{BAD} prompt has unsubstituted variables: {', '.join(sorted(unknown))}")
        print("       Supply them in assistantOverrides.variableValues or remove them.")
        fails.append("variables")
    else:
        print(f"{OK} prompt variables all have values supplied at call time")

    tool_defs = load_tool_definitions()
    if tool_defs:
        names = ", ".join(f"{d['function']['name']} (async={d.get('async')})"
                          for d in tool_defs)
        print(f"{OK} {len(tool_defs)} tool definition(s): {names}")
        print(f"       each created as its own /tool resource")
    else:
        print(f"{WARN} no _tool_definitions in assistant.json")

    url = cfg.get("serverUrl")
    if url:
        print(f"{OK} serverUrl {url}")
    else:
        print(f"{WARN} SERVER_URL not set - deploying WITHOUT webhooks or tools.")
        print("       The mid-call WhatsApp cannot fire on this assistant.")

    sp = cfg.get("startSpeakingPlan", {})
    tep = sp.get("transcriptionEndpointingPlan", {})
    print(f"{OK} endpointing: wait={sp.get('waitSeconds')}s "
          f"noPunct={tep.get('onNoPunctuationSeconds')}s "
          f"smart={(sp.get('smartEndpointingPlan') or {}).get('provider')}")

    print()
    env_used = load_env.__dict__.get("path")
    print(f"{OK} credentials from {env_used}" if env_used else f"{WARN} no .env found")
    for var in ("VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID", "TEST_NUMBER"):
        val = os.environ.get(var, "")
        if val:
            print(f"{OK} {var} set ({len(val)} chars, not shown)")
        else:
            print(f"{BAD} {var} is EMPTY")
            fails.append(var)

    print()
    dest = destination()
    problems = check_destination(dest)
    if problems:
        for p in problems:
            print(f"{BAD} {p}")
        fails.append("destination")
    else:
        print(f"{OK} destination {dest} is valid and is not the evaluator's")

    variants = ["+918688664337", "+91 8688664337", "+91-8688664337",
                "+91 86886 64337", "918688664337", "08688664337", "8688664337"]
    leaked = [x for x in variants if not check_destination(x)]
    if leaked:
        print(f"{BAD} SAFETY GUARD BROKEN - got through: {leaked}")
        fails.append("guard")
    else:
        print(f"{OK} safety guard rejects all {len(variants)} spellings (self-tested)")

    print("\n" + "-" * 52)
    if fails:
        print(f"NOT READY - outstanding: {', '.join(dict.fromkeys(fails))}")
        return 1
    print("READY.  next:  python agent/agent.py deploy")
    return 0


def ensure_tools(server_url):
    """Create or update every custom tool RESOURCE. Returns their ids.

    Idempotent by function name: we list existing tools once and reuse matches
    rather than creating duplicates on every deploy. The server URL is re-pointed
    each time, because a cloudflared tunnel gets a fresh hostname per run and a
    stale URL means the tool fires into nothing.
    """
    definitions = load_tool_definitions()
    if not definitions:
        return []

    listed = request("GET", "/tool")
    if isinstance(listed, dict):
        listed = listed.get("results", [])
    by_name = {(t.get("function") or {}).get("name"): t.get("id") for t in listed}

    secret = os.environ.get("VAPI_WEBHOOK_SECRET")
    tool_ids = []
    for definition in definitions:
        name = definition["function"]["name"]
        payload = dict(definition)
        payload["server"] = {"url": f"{server_url}/vapi/webhook"}
        if secret:
            payload["server"]["secret"] = secret

        existing = by_name.get(name)
        if existing:
            print(f"{INFO} updating tool {name} ({existing}) ...")
            request("PATCH", f"/tool/{existing}", payload)
            tool_ids.append(existing)
        else:
            print(f"{INFO} creating tool {name} ...")
            created = request("POST", "/tool", payload)
            print(f"{OK} tool created: {created['id']}")
            tool_ids.append(created["id"])
    return tool_ids


def cmd_deploy():
    """Create or update the assistant on Vapi. Costs nothing, places no call."""
    print("\nDEPLOY  (free, no call placed)\n" + "-" * 52)
    if cmd_check() != 0:
        return 1

    server_url = (os.environ.get("SERVER_URL") or "").rstrip("/")
    tool_ids = []
    if server_url:
        tool_ids = ensure_tools(server_url)
    else:
        print(f"\n{WARN} SERVER_URL not set - skipping tool creation.")

    cfg = load_assistant(tool_ids=tool_ids)
    existing = os.environ.get("VAPI_ASSISTANT_ID", "").strip()

    if existing:
        print(f"\n{INFO} updating assistant {existing} ...")
        request("PATCH", f"/assistant/{existing}", cfg)
        print(f"{OK} updated. The next call uses the new prompt and endpointing.")
    else:
        print(f"\n{INFO} creating assistant ...")
        created = request("POST", "/assistant", cfg)
        print(f"{OK} created: {created['id']}")
        print("\n  Add this line to your .env so future deploys update in place:")
        print(f"    VAPI_ASSISTANT_ID={created['id']}")

    # Verify what Vapi actually stored before anyone spends a call on it.
    print("\n" + "-" * 52)
    if cmd_inspect() != 0:
        return 1
    print("\nnext:  python agent/agent.py call --yes")
    return 0


def local_build():
    """Hash of backend/app/*.py - must match what the running server reports."""
    import glob
    import hashlib
    h = hashlib.sha256()
    app_dir = os.path.join(ROOT, "backend", "app")
    for path in sorted(glob.glob(os.path.join(app_dir, "*.py"))):
        with open(path, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def check_backend():
    """Confirm the backend is reachable AND running current code.

    Two live calls were wasted because uvicorn was serving a build from before a
    fix. The config, the deploy and the tunnel all looked correct - only the
    process was stale, and nothing surfaced it. Comparing build hashes catches
    that before the phone rings instead of during a post-mortem.
    """
    server_url = (os.environ.get("SERVER_URL") or "").rstrip("/")
    if not server_url:
        print(f"{WARN} SERVER_URL not set - the mid-call WhatsApp cannot fire on this call.")
        return True

    try:
        with urllib.request.urlopen(server_url + "/health", timeout=10) as resp:
            health = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"{BAD} backend unreachable at {server_url}/health : {exc}")
        print("      Is uvicorn running? Is the tunnel up? Is SERVER_URL current?")
        return False

    mine, live = local_build(), health.get("build")
    if live != mine:
        print(f"{BAD} the running backend is STALE - your fixes are not live.")
        print(f"      on disk : {mine}")
        print(f"      running : {live}")
        print("      Restart uvicorn (use --reload so this cannot recur).")
        return False

    print(f"{OK} backend reachable and current (build {live})")
    if not (health.get("whatsapp") or {}).get("ready"):
        print(f"{WARN} backend reports WhatsApp not configured - the send would fail")
    return True


def live_matches_local():
    """Refuse to dial an assistant that is not the config on disk.

    `check` validates the LOCAL files; `call` dials whatever Vapi has stored.
    Those are different things, and the gap has cost several debugging calls:
    the prompt was edited, a call was placed without deploying, and the
    transcript was then read as evidence about rules that were never live.
    A diagnosis drawn from the wrong build is worse than no diagnosis.

    Read-only and free - one GET before anything is spent.
    """
    aid = os.environ.get("VAPI_ASSISTANT_ID", "").strip()
    if not aid:
        return True
    try:
        live = request("GET", f"/assistant/{aid}")
    except Exception as exc:
        print(f"{WARN} could not read the live assistant ({exc}) - skipping")
        print("       the drift check. Run `inspect` if the call behaves oddly.")
        return True

    local = load_assistant()
    def sysprompt(cfg):
        msgs = (cfg.get("model") or {}).get("messages") or []
        return next((m.get("content", "") for m in msgs
                     if m.get("role") == "system"), "")

    drift = []
    lp, rp = sysprompt(local).strip(), sysprompt(live).strip()
    if lp != rp:
        drift.append(f"system prompt   local {len(lp)} chars != live {len(rp)} chars")
    for key in ("voice", "stopSpeakingPlan", "startSpeakingPlan", "transcriber"):
        if local.get(key) != live.get(key):
            drift.append(f"{key:15} local {json.dumps(local.get(key))[:55]}"
                         f" != live {json.dumps(live.get(key))[:55]}")
    lm = (local.get("model") or {}).get("model")
    rm = (live.get("model") or {}).get("model")
    if lm and lm != rm:
        drift.append(f"model           local {lm} != live {rm}")

    if not drift:
        print(f"  {OK} live assistant matches your local config")
        return True

    print(f"{BAD} THE LIVE ASSISTANT IS NOT WHAT IS ON DISK.")
    print("      Dialling now tests the OLD config, and the transcript would")
    print("      tell you nothing about the changes you just made.")
    print("")
    for d in drift:
        print(f"      - {d}")
    print("")
    print("      Fix:  SERVER_URL=... python agent/agent.py deploy")
    print("      Then re-run this command.")
    return False

def cmd_call():
    print("\nPLACING CALL  (spends money, rings a real phone)\n" + "-" * 52)
    if "--yes" not in sys.argv:
        print(f"{BAD} refusing to dial without explicit confirmation.")
        print("      Re-run:  python agent/agent.py call --yes")
        return 1
    if cmd_check() != 0:
        return 1

    assistant_id = os.environ.get("VAPI_ASSISTANT_ID", "").strip()
    if not assistant_id:
        print(f"\n{BAD} VAPI_ASSISTANT_ID not set. Run `deploy` first and save the id to .env.")
        return 1

    print()
    if not live_matches_local():
        return 1
    if not check_backend():
        print(f"\n{BAD} refusing to place the call - fix the above first.")
        return 1

    dest = destination()
    # Show the caller ID before spending anything. A call placed from a number
    # that cannot originate dies with `call.start.error-get-transport`, which
    # names neither the number nor the cause - it looks like a Vapi outage. One
    # free GET makes the actual variable visible.
    try:
        caller = request("GET", f"/phone-number/{os.environ['VAPI_PHONE_NUMBER_ID']}")
        print(f"\n{INFO} calling FROM {caller.get('number')} "
              f"({caller.get('provider')}, {caller.get('name')!r})")
        if not str(caller.get("number", "")).startswith("+1"):
            print(f"{WARN} that is not a US Twilio number. Verified caller IDs and")
            print("       non-US numbers often cannot ORIGINATE calls, which fails")
            print("       as call.start.error-get-transport with $0 cost.")
    except Exception as exc:
        print(f"{WARN} could not read the caller-ID number: {exc}")

    print(f"\n{INFO} dialling {dest} ...")
    call = request("POST", "/call", {
        "assistantId": assistant_id,
        "phoneNumberId": os.environ["VAPI_PHONE_NUMBER_ID"],
        "customer": {"number": dest},
        # ALWAYS sent, even empty. prompt.md carries a {{callback_context}}
        # placeholder that the backend fills when re-dialling a booked callback;
        # without a value here the literal "{{callback_context}}" would sit in
        # the system prompt of every call placed from this CLI.
        "assistantOverrides": {"variableValues": {"callback_context": ""}},
    })
    call_id = call.get("id")
    print(f"{OK} queued: {call_id}")
    print("\n  >>> ANSWER. Play a real business owner. Interrupt it at least once. <<<\n")

    last = None
    for _ in range(96):
        time.sleep(5)
        info = request("GET", f"/call/{call_id}")
        status = info.get("status")
        if status != last:
            print(f"{INFO} {status}")
            last = status
        if status == "ended":
            os.makedirs(TRANSCRIPTS, exist_ok=True)
            path = os.path.join(TRANSCRIPTS, f"{call_id}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(info, fh, indent=2)
            print(f"{OK} saved transcripts/{call_id}.json")
            return review(info)
    print(f"{WARN} still running after 8 minutes - check the Vapi dashboard")
    return 0


# ---------------------------------------------------------------- review

def sentences(text):
    return [s for s in re.split(r"[.!?]+", text or "") if s.strip()]


def review(info):
    """Score one call. Pure local analysis - no network, no LLM."""
    msgs = [m for m in (info.get("messages") or []) if m.get("role") in ("user", "bot")]
    bot_turns = [str(m.get("message", "")) for m in msgs if m["role"] == "bot"]

    print("\n" + "=" * 52)
    print("CALL REVIEW")
    print("=" * 52)
    print(f"  ended reason : {info.get('endedReason')}")
    if info.get("endedReason") == "call.start.error-get-transport":
        # Observed across Twilio AND Telnyx on 29 Aug, both $0 with no transport.
        # Vapi raises a DIFFERENT, specific error when the fault is its own
        # (error-vapi-number-international for a free number dialling +91), so
        # this one means the BYO carrier itself refused to originate.
        print("    ^ no transport, $0 charged: the CARRIER refused to originate.")
        print("      Not a Vapi or config fault - Vapi reports its own limits")
        print("      separately (e.g. error-vapi-number-international).")
        print("      Check, on the carrier's side:")
        print("        - account verified and funded, not suspended")
        print("        - the number is attached to an outbound voice profile")
        print("        - INDIA is enabled as an international destination")
        print("      Then read the carrier's own call logs - they say why.")
    if info.get("cost") is not None:
        print(f"  cost         : ${info['cost']}")
    if info.get("recordingUrl"):
        print(f"  recording    : {info['recordingUrl']}")

    # --- latency: the 3-second cliff the assignment calls fatal.
    #
    # Use Vapi's own performanceMetrics. An earlier version of this function
    # derived latency as (next bot start - end of transcribed user utterance),
    # which OVERSTATED it by 1-1.5s: Soniox finalises a transcript after the
    # person has actually stopped talking, so "end of utterance" lands late.
    # That bug made two calls look like they were failing the 3s cliff when
    # Vapi had measured them at ~2.0s. Trust the provider's instrumentation.
    print("\n  RESPONSE LATENCY  (assignment: 3s = 'the conversation is dead')")
    pm = (info.get("artifact") or {}).get("performanceMetrics") or {}
    turns = [t for t in (pm.get("turnLatencies") or []) if t.get("turnLatency")]
    if turns:
        tl = sorted(t["turnLatency"] for t in turns)
        over = [x for x in tl if x > 3000]
        print(f"    turns={len(tl)}  min={tl[0]}ms  median={tl[len(tl)//2]}ms  max={tl[-1]}ms")
        print(f"  {BAD if over else OK} over 3s: {len(over)}/{len(tl)}"
              + (f"   {sorted(over, reverse=True)}" if over else ""))
        print("\n    where the time goes (per-turn average):")
        for label, key in (("LLM        ", "modelLatencyAverage"),
                           ("TTS        ", "voiceLatencyAverage"),
                           ("STT        ", "transcriberLatencyAverage"),
                           ("endpointing", "endpointingLatencyAverage")):
            val = pm.get(key)
            if val is not None:
                print(f"      {label} {val:7.0f} ms")
        print(f"      {'TOTAL      '} {pm.get('turnLatencyAverage', 0):7.0f} ms")
    else:
        print(f"    {WARN} no performanceMetrics in this call payload")

    # --- dead air the turn-latency number cannot see.
    #
    # `turnLatency` measures end-of-caller-speech -> FIRST agent audio. It stops
    # counting there. If the agent then says a filler ("this will just take a
    # sec"), waits on a synchronous tool, and only then answers, the caller hears
    # one long pause but the metric records two short turns. A caller reported
    # feeling >3 s gaps on a call that scored 0/10 over 3 s; this is where those
    # seconds were hiding.
    print("\n  DEAD AIR BETWEEN AGENT TURNS  (what turn latency cannot see)")
    gaps = []
    for i in range(1, len(msgs)):
        prev, cur = msgs[i - 1], msgs[i]
        if prev["role"] != "bot" or cur["role"] != "bot":
            continue
        start, prev_start = cur.get("secondsFromStart"), prev.get("secondsFromStart")
        if start is None or prev_start is None:
            continue
        gap = start - (prev_start + (prev.get("duration") or 0) / 1000.0)
        if gap > 0.25:                       # ignore normal sentence joins
            gaps.append((round(gap, 1), str(prev.get("message", ""))[:46]))
    if gaps:
        gaps.sort(reverse=True)
        worst = gaps[0][0]
        print(f"    {len(gaps)} mid-turn pause(s), longest {worst}s")
        for g, after in gaps[:3]:
            print(f"      {g:>4}s after: {after!r}")
        if worst > 3:
            print(f"  {BAD} the caller heard {worst}s of silence mid-answer. Turn")
            print("       latency scored this call as clean - it is not.")
        else:
            print(f"  {OK} longest mid-answer pause is under 3 s")
    else:
        print(f"    {OK} no mid-answer pauses")

    # --- transcript health: is the LINE working, or just the software?
    #
    # Added after a Telnyx call where every scored number looked fine but the
    # conversation was unusable: the caller's speech arrived as fragments
    # ("Uh, V7. And customer exclusions.") with 16-20 s holes between turns.
    # Latency cannot see that - it only measures how fast we answered, not
    # whether we heard anything worth answering.
    print("\n  TRANSCRIPT HEALTH  (is the audio path actually working?)")
    user_msgs = [m for m in msgs if m["role"] == "user"]
    if user_msgs:
        words = sorted(len(str(m.get("message", "")).split()) for m in user_msgs)
        median_words = words[len(words) // 2]
        stubs = [w for w in words if w <= 3]
        # Gap from the end of the agent's turn to the start of the caller's next.
        # `endTime` is an absolute epoch in ms, NOT relative - mixing it with
        # secondsFromStart produced gaps of minus 1.8 billion seconds. Derive the
        # end of a turn from its own start plus its duration instead.
        gaps = []
        for i, m in enumerate(msgs):
            if m["role"] == "user" and i and msgs[i - 1]["role"] == "bot":
                prev, start = msgs[i - 1], m.get("secondsFromStart")
                prev_start, dur = prev.get("secondsFromStart"), prev.get("duration")
                if start is None or prev_start is None:
                    continue
                prev_end = prev_start + (dur or 0) / 1000.0
                gaps.append(round(start - prev_end, 1))
        print(f"    caller turns={len(user_msgs)}  median words={median_words}"
              f"  one-to-three-word turns={len(stubs)}/{len(words)}")
        if gaps:
            gaps.sort()
            print(f"    silence before caller turns: median {gaps[len(gaps)//2]}s"
                  f"  max {gaps[-1]}s")
        bad = median_words <= 3 or len(stubs) > len(words) / 2
        if bad:
            print(f"  {BAD} the caller's speech is arriving as FRAGMENTS.")
            print("       That is an audio path problem, not a prompt or model")
            print("       problem - no prompt can answer words that never arrived.")
            print("       Suspect the carrier media path, the handset, or the")
            print("       mobile signal. Compare against a known-good call.")
        else:
            print(f"  {OK} caller turns look like whole utterances")
    else:
        print(f"    {WARN} no caller turns at all - nothing was heard")

    ipm = (info.get("artifact") or {}).get("performanceMetrics") or {}
    if ipm.get("numAssistantInterrupted") or ipm.get("numUserInterrupted"):
        print(f"    interruptions: assistant cut off "
              f"{ipm.get('numAssistantInterrupted', 0)}x, "
              f"caller cut off {ipm.get('numUserInterrupted', 0)}x")

    # --- discovery coverage: the 10-point row
    # Only count topics the agent actually ASKED about. Matching on every bot
    # turn gives false credit - "do you have product photos ready?" is not
    # asking what they sell, and "budget range" is not asking catalogue size.
    print("\n  DISCOVERY COVERAGE  (did the agent ASK about each topic?)")
    # Skip the greeting: "I help small businesses..." is not asking what they sell.
    questions = [t for t in bot_turns[1:] if "?" in t] or bot_turns[1:]
    joined = " ".join(questions).lower()
    covered = 0
    for topic, words in TOPICS.items():
        hit = any(w in joined for w in words)
        covered += hit
        print(f"    {OK if hit else BAD} {topic}")
    print(f"    {covered}/5 topics raised")

    # --- turn length: the 'sounds like a recording' tell
    print("\n  TURN LENGTH  (rule: 1-2 sentences, never more)")
    long_turns = [t for t in bot_turns if len(sentences(t)) > 2 or len(t.split()) > 40]
    if long_turns:
        print(f"  {WARN} {len(long_turns)}/{len(bot_turns)} agent turns ran long:")
        for t in long_turns[:3]:
            print(f"       {t[:90]}...")
    else:
        print(f"  {OK} all {len(bot_turns)} agent turns were short")

    # --- repeated questions: the 'questionnaire' tell
    repeats = [tp for tp, words in TOPICS.items()
               if sum(1 for t in questions if any(w in t.lower() for w in words)) > 2]
    if repeats:
        print(f"\n  {WARN} topics raised 3+ times (possible re-asking): {repeats}")

    if info.get("summary"):
        print(f"\n  VAPI SUMMARY\n    {info['summary'][:400]}")

    print("\n  TRANSCRIPT\n" + "-" * 52)
    print("  " + (info.get("transcript") or "(none)").replace("\n", "\n  "))
    print("\n  Score it by ear too - the numbers above cannot hear tone.")
    return 0


def cmd_review():
    args = [a for a in sys.argv[2:] if not a.startswith("-")]
    if args:
        path = os.path.join(TRANSCRIPTS, f"{args[0]}.json")
    else:
        files = [os.path.join(TRANSCRIPTS, f) for f in os.listdir(TRANSCRIPTS)] \
            if os.path.isdir(TRANSCRIPTS) else []
        files = [f for f in files if f.endswith(".json")]
        if not files:
            print(f"{BAD} no transcripts yet. Run: python agent/agent.py call --yes")
            return 1
        path = max(files, key=os.path.getmtime)
    if not os.path.exists(path):
        print(f"{BAD} no such transcript: {path}")
        return 1
    print(f"{INFO} reviewing {os.path.basename(path)}")
    with open(path, encoding="utf-8") as fh:
        return review(json.load(fh))


def cmd_inspect():
    """Show what Vapi ACTUALLY stored for the live assistant. Read-only, free.

    Local config is what we sent; this is what the server kept. When a call
    behaves as if a setting was ignored, this is how you find out.
    """
    aid = os.environ.get("VAPI_ASSISTANT_ID", "").strip()
    if not aid:
        print(f"{BAD} VAPI_ASSISTANT_ID not set in .env")
        return 1
    live = request("GET", f"/assistant/{aid}")
    local = load_assistant()
    print("")
    print("LIVE ASSISTANT " + aid)
    print("-" * 52)
    for key in ("startSpeakingPlan", "stopSpeakingPlan", "transcriber", "voice"):
        l, r = local.get(key), live.get(key)
        same = "same" if l == r else "DIFFERENT"
        print("")
        print(f"  {key}  [{same}]")
        print(f"    sent : {json.dumps(l)}")
        print(f"    live : {json.dumps(r)}")
    rm = live.get("model", {})
    print("")
    print("  model")
    print(f"    live : {rm.get('provider')}/{rm.get('model')} "
          f"prompt={len((rm.get('messages') or [{}])[0].get('content',''))} chars")

    # The mid-call action cannot fire unless ALL of these are true. Checked here,
    # before a call is placed, because finding out live wastes the call.
    print("")
    print("  MID-CALL ACTION READINESS")
    problems = []

    server_url = live.get("serverUrl")
    if server_url:
        print(f"    {OK} assistant serverUrl : {server_url}")
    else:
        print(f"    {BAD} assistant has NO serverUrl - no transcripts, no tool calls")
        problems.append("serverUrl")

    msgs = live.get("serverMessages") or []
    if "tool-calls" in msgs and "transcript" in msgs:
        print(f"    {OK} serverMessages      : {msgs}")
    else:
        print(f"    {BAD} serverMessages missing transcript and/or tool-calls: {msgs}")
        problems.append("serverMessages")

    tool_ids = rm.get("toolIds") or []
    if not tool_ids:
        print(f"    {BAD} model.toolIds is EMPTY - the agent cannot book a callback")
        problems.append("toolIds")
    for tid in tool_ids:
        tool = request("GET", f"/tool/{tid}")
        fn = (tool.get("function") or {}).get("name")
        turl = (tool.get("server") or {}).get("url")
        print(f"    {OK} tool attached       : {fn}  id={tid}")
        print(f"         async={tool.get('async')}  server={turl}")
        if turl != server_url:
            print(f"    {BAD} tool server URL does not match the assistant's serverUrl.")
            print("         A stale tunnel URL means the tool fires into nothing. Re-deploy.")
            problems.append("tool server url")

    print("")
    if problems:
        print(f"  NOT READY for a mid-call test: {', '.join(problems)}")
        return 1
    print("  READY: webhooks and tools are wired to this assistant.")
    return 0


COMMANDS = {"check": cmd_check, "deploy": cmd_deploy, "call": cmd_call,
            "review": cmd_review, "inspect": cmd_inspect}

if __name__ == "__main__":
    load_env.__dict__["path"] = load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[cmd]())
