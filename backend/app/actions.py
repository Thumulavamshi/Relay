"""Action bus - side effects that fire during or after a call.

Two properties matter, both from the assignment:

1. "Firing an action mid call without blocking the conversation." Nothing here is
   awaited by a webhook handler. `dispatch` claims a row and returns; the handler
   runs as a detached task.

2. Exactly-once. The mid-call WhatsApp has two independent trigger paths - the
   LLM calling a tool, and an async watchdog that fires if the LLM does not. Both
   route through `dispatch` with the SAME idempotency key, so the second is a
   no-op. Two WhatsApps would look worse than one.

No handler is registered yet. Registering one is the whole of the next phase:

    @handler("whatsapp_hot")
    async def send_hot(call_id, payload): ...
"""

import asyncio
import logging
import os
import traceback

from . import db

log = logging.getLogger("elevatebox.actions")

# type -> async callable(call_id, payload)
HANDLERS = {}

# Strong references to in-flight tasks. asyncio only holds a WEAK reference to a
# task created by create_task, so without this a fire-and-forget send can be
# garbage-collected mid-flight and vanish silently - which on the 15-point row
# would look exactly like "the WhatsApp never arrived". Caught by smoke_test.
_INFLIGHT = set()

# Every action type the system will fire. Declared up front so the API can report
# honestly on what is wired and what is still a stub.
KNOWN_TYPES = {
    "whatsapp_hot":       "Mid-call message on high intent. 15-pt row.",
    "whatsapp_followup":  "Post-call context message with the architecture image.",
    "whatsapp_resume":    "Resume as a document, right after the follow-up.",
    "whatsapp_brochure":  "Cold-lead brochure.",
    "callback_confirm":   "Confirmation that a callback was booked.",
    # The multi-app layer (integrations.py). Keyed per MOMENT, not per call.
    "calendar_event":     "Google Calendar event for the booked callback.",
    "crm_sync":           "HubSpot contact and deal, converged to the current read.",
    "crm_note":           "HubSpot note at call end: read, quotes, links.",
    "team_alert":         "Slack message for the sales team, edited in place.",
}


def handler(action_type):
    """Register an async handler for an action type."""
    def register(fn):
        HANDLERS[action_type] = fn
        return fn
    return register


def idempotency_key(call_id, action_type, suffix=None):
    """One action of each type per call - or, with a suffix, one per call per
    MOMENT (`crm_sync:hot`). This is the exactly-once guarantee."""
    base = f"{call_id}:{action_type}"
    return f"{base}:{suffix}" if suffix else base


def dispatch(call_id, action_type, payload=None, trigger_source=None, background=None,
             key_suffix=None):
    """Claim and fire. Returns the action id, or None if already claimed.

    Safe to call from a webhook handler: it does one INSERT and schedules work.
    Never raises - a failed side effect must not break the call.

    `background` is FastAPI's BackgroundTasks. When a request has one, use it:
    the framework then guarantees the task runs after the response is sent.
    Outside a request (the watchdog, the callback worker) we fall back to
    create_task and hold a strong reference.
    """
    key = idempotency_key(call_id, action_type, key_suffix)
    action_id = db.claim_action(call_id, action_type, key,
                                trigger_source=trigger_source, payload=payload)
    if action_id is None:
        log.info("action %s for %s already claimed - no-op", action_type, call_id)
        return None

    fn = HANDLERS.get(action_type)
    if fn is None:
        # Declared but not built yet. The ledger still records that something
        # WANTED to fire, which is what makes the gap visible instead of silent.
        db.update_action(action_id, status="failed",
                         error="no handler registered for this action type")
        log.warning("action %s claimed but no handler registered", action_type)
        return action_id

    if background is not None:
        background.add_task(_run, action_id, call_id, action_type, fn, payload)
        return action_id

    try:
        task = asyncio.get_running_loop().create_task(
            _run(action_id, call_id, action_type, fn, payload))
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
    except RuntimeError:
        # No running loop (a sync worker, or a test calling directly).
        asyncio.run(_run(action_id, call_id, action_type, fn, payload))
    return action_id


# Design §7 called for "retry with backoff on the action bus" and it was never
# built. A real follow-up then died on `EOF occurred in violation of protocol`
# part-way through a 1.8 MB upload - one transient TLS hiccup and the evaluator
# silently never receives the architecture image, a required Section 06 element.
MAX_ATTEMPTS = int(os.environ.get("ACTION_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = 3

# Worth retrying: the network gave out. Not worth retrying: we are misconfigured
# or the destination is refused - those fail identically every time and a retry
# just delays an honest error.
_TRANSIENT = ("eof occurred", "timed out", "timeout", "connection", "reset",
              "temporarily", "unreachable", "ssl", "500", "502", "503", "504",
              # HubSpot and Groq rate-limit with 429; Slack says it in words.
              "429", "ratelimited")


def _is_transient(exc):
    return any(s in str(exc).lower() for s in _TRANSIENT)


async def _run(action_id, call_id, action_type, fn, payload):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        db.update_action(action_id, status="sending", attempts=attempt)
        try:
            result = await fn(call_id, payload or {})
            db.update_action(
                action_id,
                status="sent",
                sent_at=db.utc_now(),
                provider_message_id=(result or {}).get("message_id"),
            )
            log.info("action %s sent for call %s%s", action_type, call_id,
                     f" (attempt {attempt})" if attempt > 1 else "")
            return
        except Exception as exc:
            last = attempt == MAX_ATTEMPTS
            if last or not _is_transient(exc):
                db.update_action(action_id, status="failed",
                                 error=f"{exc}\n{traceback.format_exc()[:800]}")
                log.exception("action %s failed for call %s (attempt %d/%d)",
                              action_type, call_id, attempt, MAX_ATTEMPTS)
                return
            wait = BACKOFF_SECONDS * attempt
            log.warning("action %s attempt %d/%d failed transiently (%s) - "
                        "retrying in %ds", action_type, attempt, MAX_ATTEMPTS,
                        str(exc)[:80], wait)
            await asyncio.sleep(wait)


def status_report():
    """What is wired vs still a stub. Surfaced on /health so gaps stay visible."""
    return {t: ("wired" if t in HANDLERS else "stub") for t in KNOWN_TYPES}
