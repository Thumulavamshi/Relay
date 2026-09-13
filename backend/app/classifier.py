"""Hot / Warm / Cold classification. The 15-point row.

Two layers, on purpose:

1. An LLM read of the running transcript. The assignment is explicit that real
   people never say "I am a hot lead" - they say "my brother handles this" - so
   this has to be judgement, not keyword matching.

2. A narrow deterministic overlay. `docs/requirements-analysis.md` OQ-04 decided
   to bias the boundary toward firing: an explicit request to be SENT something,
   or to know when work can START, is treated as hot regardless of the model's
   read. Sending a message we did not strictly need costs nothing; failing to
   send one costs 15 points.

   The overlay is deliberately narrow. A rule that fires on everything would
   destroy the signal the row is actually testing. It does not, for example,
   trigger on a bare "how much is it" - that can equally be idle curiosity, and
   judging it is the model's job.

Runs in the understanding lane, never on the speech path, so latency here does
not affect the conversation.
"""

import logging
import os
import re

from pydantic import BaseModel, Field

from . import rest

log = logging.getLogger("elevatebox.classifier")

LABELS = ("hot", "warm", "cold")
BARRIERS = ("budget", "timing", "decision_maker", "none")

# Provider-agnostic by design: the schema, the system prompt and the rules
# overlay are shared, and only the transport differs. Switching providers is a
# config change, so we can develop on a free tier and move if evidence says to.
PROVIDERS = {
    "groq": {
        # OpenAI-compatible endpoint over stdlib HTTP - nothing to install.
        # gpt-oss-20b is one of the models Groq serves with STRICT json_schema,
        # so the reply is guaranteed to parse as LeadRead / ExtractedSlots.
        "default_model": "openai/gpt-oss-20b",
        "key_env": "GROQ_API_KEY",
        "package": "urllib.request",
        "install": "stdlib - nothing to install",
    },
    "anthropic": {
        "default_model": "claude-opus-5",
        "key_env": "ANTHROPIC_API_KEY",
        "package": "anthropic",
        "install": "pip install anthropic",
    },
    "gemini": {
        # gemini-3.7-flash is documented but returns 503 UNAVAILABLE ("high
        # demand") on this key, and 3-flash-preview answers in ~6s against
        # 2.5-flash's ~2.4s. Measured 2026-08-28; revisit if capacity frees up.
        "default_model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
        "package": "google.genai",
        "install": "pip install -U google-genai",
    },
}

DEFAULT_MODEL = PROVIDERS["anthropic"]["default_model"]  # kept for compatibility

# Bound the Gemini retry loop. A 503 otherwise retries for minutes.
GEMINI_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS", "25000"))


def resolve_provider():
    """CLASSIFIER_PROVIDER wins; otherwise pick whichever key is present."""
    named = (os.environ.get("CLASSIFIER_PROVIDER") or "").strip().lower()
    if named:
        return named
    for name, spec in PROVIDERS.items():
        if os.environ.get(spec["key_env"]):
            return name
    return "anthropic"


def model_for(provider):
    return os.environ.get("CLASSIFIER_MODEL") or PROVIDERS[provider]["default_model"]


class LeadRead(BaseModel):
    """The structured read. Field docs are sent to the model as the schema."""

    label: str = Field(description="One of: hot, warm, cold")
    confidence: float = Field(description="0.0 to 1.0, how sure you are")
    barrier: str = Field(
        description="For warm leads the thing blocking them: budget, timing, "
                    "or decision_maker. Use 'none' for hot and cold."
    )
    evidence_quote: str = Field(
        description="The lead's own words that decided it, quoted verbatim from "
                    "the transcript. Empty string if nothing decisive was said."
    )
    reasoning: str = Field(description="One short sentence.")


# Definitions are lifted verbatim from the assignment's own Hot/Warm/Cold cards,
# and the four example phrases are the ones the PDF publishes as what it will
# push us with. They are the closest thing to a marking key we have.
SYSTEM_PROMPT = """\
You read sales-call transcripts and decide how serious a buyer is. The business \
sells custom e-commerce websites to small Indian businesses.

Classify the LEAD (the person being called), never the agent, into exactly one of:

HOT - High buying intent. Wants it, asking price and timeline.
WARM - Interested, not ready. There is a real need, but a barrier: budget, \
timing, or someone else decides.
COLD - Just looking. Curious, with no clear need or budget.

Real people do not announce their intent. Judge from indirect language:

  "send me the details"            -> HOT. Asking to be sent material now.
  "how soon can you start"         -> HOT. Asking about start date is buying intent.
  "my budget is not much right now" -> WARM, barrier=budget.
  "my brother handles this"        -> WARM, barrier=decision_maker.

More guidance:
- Politeness is not intent. "Sounds good, thanks" after refusing to give a budget \
is COLD or WARM, not HOT.
- Vagueness is not automatically COLD. A vague answer with a real need is WARM.
- Someone who already has a website and is only comparing is COLD unless they \
show intent to switch.
- Only use barrier for WARM. Hot and cold take "none".
- The transcript comes from live speech recognition, so it will contain "uh", \
broken punctuation and mis-heard words. Read through the noise; do not treat \
transcription errors as hesitation.
- evidence_quote must be the lead's actual words, copied from the transcript.
- **The transcript may be in English, Hindi or Telugu, in native script or \
romanised, and frequently mixes English words into a Hindi or Telugu sentence.** \
Classify on meaning, not language. "Details pampandi", "details bhej dijiye" and \
"send me the details" are the same signal. Hesitation markers differ by language \
too - treat "haan haan", "avunu" and "yeah" alike.
- Write reasoning in English, but keep evidence_quote in whatever language and \
script the lead actually used. A translated quote is not a quote.

If the lead has barely spoken yet, say COLD with low confidence rather than guessing.\
"""


# --- deterministic overlay -------------------------------------------------
#
# High precision by design. Each pattern is an unambiguous request for an action,
# not a mood. Kept as regexes so eval_classifier.py can test them with no API key.

_SEND_ME = re.compile(
    r"\b(send|share|forward|whatsapp|mail|email)\b[^.?!]{0,30}\b"
    r"(me|us|it|them|the\s+(details|detail|brochure|quote|quotation|proposal|"
    r"pricing|price|portfolio|catalogue|info|information))\b",
    re.I,
)
_SEND_ACROSS = re.compile(r"\bsend\s+(it|that|those|them)\s+(across|over|through)\b", re.I)
# Word order is NOT fixed: "when can you start" and "by when you can make it
# ready" are the same question. The original pattern required can/could BEFORE
# you/we and missed the second form on a live call - which is the assignment's
# own "how soon can you start" category, the thing this rule exists for.
# The delivery verbs are widened for the same reason ("make it ready", "build").
_WHEN_START = re.compile(
    r"\b(how\s+soon|when|by\s+when)\b[^.?!]{0,30}"
    r"(\b(can|could|will|would|do)\b[^.?!]{0,15}\b(you|we|u)\b"
    r"|\b(you|we|u)\b[^.?!]{0,15}\b(can|could|will|would)\b)"
    r"[^.?!]{0,20}\b(start|begin|deliver|finish|do\s+it|make|build|ready|"
    r"complete|hand\s*over|give\s+it)\b",
    re.I,
)
_CAN_YOU_START = re.compile(r"\b(can|could)\s+(you|we)\s+start\b", re.I)

# Hindi and Telugu, romanised and in native script.
#
# Without these the fast path is English-only, so on a Telugu call the mid-call
# action would wait on a ~4-8 s model call - handing back exactly the latency the
# overlay exists to avoid, on the two languages we just added.
#
# NO \b ANCHORS on the native-script alternatives. Python's \b is defined by \w,
# which excludes Indic combining marks, so a boundary lands in the middle of a
# character cluster and the match silently fails. Same trap as the punctuation
# stripping in timeparse.py.
#
# Word order is the mirror of English - object first, verb last ("details
# bhejo", "details pampandi") - so these key off the imperative send verb, which
# in a sales call is only ever aimed at us.
_SEND_ME_INDIC = re.compile(
    r"(bhej\w*|भेज|pamp(?:and|inch|u)\w*|పంప|"           # send
    r"whatsapp\s*(?:kar|che|pe|lo|par)\w*|व्हाट्सएप|వాట్సాప్)",
    re.I,
)
_WHEN_START_INDIC = re.compile(
    r"(kab\s+(?:se\s+)?(?:shuru|start)|कब\s+शुरू|"
    r"eppudu\s+\w*\s*(?:start|modal)|ఎప్పుడు\s*\w*\s*(?:start|మొదల))",
    re.I,
)

# If any of these are present the lead is pushing back, so no rule may force hot.
_DECLINED = re.compile(
    r"\b(not\s+interested|no\s+need|don'?t\s+(call|contact)|remove\s+my\s+number|"
    r"stop\s+calling|already\s+have\s+(a\s+)?(one|website|site))\b",
    re.I,
)
_DECLINED_INDIC = re.compile(
    r"(nahi\s+chahiye|nahin\s+chahiye|नहीं\s*चाहिए|interest\s+nahi|mat\s+kar\w*|"
    r"\bvaddu\b|వద్దు|avasaram\s+ledu|అవసరం\s*లేదు)",
    re.I,
)

FORCE_HOT_PATTERNS = (
    ("asked to be sent material", _SEND_ME),
    ("asked for it to be sent across", _SEND_ACROSS),
    ("asked when work can start", _WHEN_START),
    ("asked if we can start", _CAN_YOU_START),
    ("asked to be sent material (hi/te)", _SEND_ME_INDIC),
    ("asked when work can start (hi/te)", _WHEN_START_INDIC),
)


def lead_utterances(transcript):
    """Only the lead's lines. The agent offering to send something is not intent."""
    out = []
    for line in (transcript or "").splitlines():
        low = line.strip().lower()
        if low.startswith(("user:", "lead:", "customer:")):
            out.append(line.split(":", 1)[1].strip())
    return out


def forced_hot_reason(transcript):
    """Return why the overlay forces hot, or None. Pure function, no API needed."""
    lines = lead_utterances(transcript) or [transcript or ""]
    joined = " ".join(lines)
    if _DECLINED.search(joined) or _DECLINED_INDIC.search(joined):
        return None
    for reason, pattern in FORCE_HOT_PATTERNS:
        m = pattern.search(joined)
        if m:
            return f"{reason}: {m.group(0).strip()!r}"
    return None


def apply_rules(read, transcript):
    """Overlay the deterministic rules on a model read. Returns a new LeadRead."""
    data = read.model_dump()

    reason = forced_hot_reason(transcript)
    if reason and data["label"] != "hot":
        log.info("overlay raised %s -> hot (%s)", data["label"], reason)
        data["label"] = "hot"
        data["barrier"] = "none"
        data["reasoning"] = f"Rule: {reason}. Model said {read.label}: {read.reasoning}"
        data["confidence"] = max(data.get("confidence") or 0.0, 0.75)

    if data["label"] not in LABELS:
        data["label"] = "cold"
    if data["label"] != "warm":
        data["barrier"] = "none"
    if data["barrier"] not in BARRIERS:
        data["barrier"] = "none"
    return LeadRead(**data)


# --- model call ------------------------------------------------------------

def _importable(dotted):
    import importlib
    try:
        importlib.import_module(dotted)
        return True
    except ImportError:
        return False


def available(provider=None):
    """Whether the LLM path can run. The rules layer works without any of this."""
    provider = provider or resolve_provider()
    spec = PROVIDERS.get(provider)
    if not spec:
        return False, f"unknown provider {provider!r} (choose: {', '.join(PROVIDERS)})"
    if not _importable(spec["package"]):
        return False, f"{provider} SDK not installed ({spec['install']})"
    if not os.environ.get(spec["key_env"]):
        return False, f"{spec['key_env']} not set"
    return True, f"ready ({provider}/{model_for(provider)})"


def providers_status():
    """Per-provider readiness. Surfaced on /health so gaps are visible."""
    out = {}
    for name in PROVIDERS:
        ok, why = available(name)
        out[name] = {"ready": ok, "detail": why, "model": model_for(name)}
    out["selected"] = resolve_provider()
    return out


USER_TEMPLATE = "Transcript so far:\n\n{transcript}\n\nClassify the lead."


def _call_anthropic(system, user, model, schema, max_tokens):
    import anthropic

    response = anthropic.Anthropic().messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    return response.parsed_output


_GEMINI_CLIENT = None


def _gemini_client():
    """One cached client, held for the process lifetime.

    THIS IS LOAD-BEARING, not an optimisation. Writing
    `genai.Client().models.generate_content(...)` makes the Client a temporary:
    it becomes unreachable the moment `.models` is bound, its finalizer closes
    the underlying httpx transport, and the request then dies with
    "Cannot send a request, as the client has been closed" - with a traceback
    that ends near ssl.create_default_context() and looks like a TLS problem.
    It is not. It is object lifetime, the same bug class as an un-referenced
    asyncio task being garbage-collected mid-flight.

    Reusing one client also keeps the HTTPS connection warm across the ~8
    classifications in a call, which is worth having on a timing-scored row.
    """
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        from google import genai
        _GEMINI_CLIENT = genai.Client()
    return _GEMINI_CLIENT


def _call_gemini(system, user, model, schema, max_tokens):
    """Gemini via models.generate_content with a response_schema.

    `interactions.create(response_format={...})` returns PROSE, not JSON, so
    json.loads() on it fails. `models.generate_content` with `response_schema`
    is the shape that actually validates, and it hands back a populated Pydantic
    object on `.parsed` - the same ergonomics as Anthropic's `parsed_output`.

    The explicit timeout matters too: without it the SDK retries a 503 for
    minutes. The understanding lane is off the speech path, but a two-minute
    stall still delays the mid-call action, which is scored on timing.
    """
    response = _gemini_client().models.generate_content(
        model=model,
        contents=f"{system}\n\n{user}",
        config={
            "response_mime_type": "application/json",
            "response_schema": schema,
            "max_output_tokens": max_tokens,
            "http_options": {"timeout": GEMINI_TIMEOUT_MS},
        },
    )
    if response.parsed is None:
        raise RuntimeError(f"gemini returned unparseable output: {(response.text or '')[:200]}")
    return response.parsed


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT = float(os.environ.get("GROQ_TIMEOUT_SECONDS", "30"))
# Models Groq guarantees schema-valid output for (strict: true). Any other model
# runs best-effort and is still validated by Pydantic on the way back.
GROQ_STRICT_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"}
# gpt-oss reasons before it answers, and those tokens count against the limit. A
# budget sized for the answer alone comes back truncated with no content at all.
GROQ_REASONING_TOKENS = int(os.environ.get("GROQ_REASONING_TOKENS", "1024"))
GROQ_REASONING_EFFORT = os.environ.get("GROQ_REASONING_EFFORT", "low")


def strict_schema(model_cls):
    """Pydantic's JSON schema, reshaped for Groq's strict mode.

    Strict mode wants every object closed (additionalProperties: false) with every
    property required, and Pydantic emits nested models as $ref into $defs. Both
    LeadRead and ExtractedSlots go through this one transform.
    """
    raw = model_cls.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node):
        if isinstance(node, list):
            return [fix(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = dict(defs[node["$ref"].rsplit("/", 1)[-1]])
            target.update({k: v for k, v in node.items() if k != "$ref"})
            return fix(target)
        out = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            out[key] = ({name: fix(sub) for name, sub in value.items()}
                        if key == "properties" else fix(value))
        if out.get("type") == "object":
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out

    return fix(raw)


def _groq_request(payload):
    """The single Groq transport. The smoke test replaces this."""
    return rest.call("groq", "POST", GROQ_URL,
                     headers={"Authorization": "Bearer " + os.environ.get("GROQ_API_KEY", "")},
                     json_body=payload, timeout=GROQ_TIMEOUT)


def _call_groq(system, user, model, schema, max_tokens):
    """Groq's OpenAI-compatible chat completions with a json_schema response format."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": schema.__name__,
            "strict": model in GROQ_STRICT_MODELS,
            "schema": strict_schema(schema)}},
        "max_completion_tokens": max_tokens + GROQ_REASONING_TOKENS,
    }
    if model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = GROQ_REASONING_EFFORT
    data = _groq_request(payload)
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if not content:
        raise RuntimeError(f"groq returned no content "
                           f"(finish_reason={choice.get('finish_reason')})")
    return schema.model_validate_json(content)


_CALLERS = {"groq": _call_groq, "anthropic": _call_anthropic, "gemini": _call_gemini}


def run_structured(system, user, schema, provider=None, max_tokens=1000):
    """One structured model call, whichever provider is configured.

    Shared with `extraction.py` rather than copied into it: provider resolution,
    the readiness check, and the Gemini client-lifetime fix above are all things
    worth getting right once. Both understanding-lane jobs are the same shape -
    a prompt in, a validated Pydantic object out.

    Blocking. Anything on the event loop must call it via asyncio.to_thread.
    """
    provider = provider or resolve_provider()
    ok, why = available(provider)
    if not ok:
        raise RuntimeError(f"model unavailable: {why}")
    return _CALLERS[provider](system, user, model_for(provider), schema, max_tokens)


def classify_transcript(transcript, provider=None):
    """Read the transcript. Returns a LeadRead with the overlay already applied.

    Raises RuntimeError if the LLM path is unavailable - callers decide whether
    that is fatal. `understanding.classify` treats it as non-fatal, because a
    broken understanding lane must never take down a live call.
    """
    read = run_structured(SYSTEM_PROMPT,
                          USER_TEMPLATE.format(transcript=transcript),
                          LeadRead, provider=provider)
    return apply_rules(read, transcript)
