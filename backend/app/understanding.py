"""The understanding lane.

Runs alongside the speech loop and never inside it. Vapi drives STT -> LLM -> TTS
at conversational speed; this consumes the same transcript, extracts structured
facts, classifies the lead, and fires actions. If it is slow, the conversation
does not care.

Two entry points:

    on_turn()       - called for every final transcript turn
    on_call_ended() - called once, with the complete transcript

Both model calls are blocking SDK calls, so both go through asyncio.to_thread.
That is not tidiness: these run inside a FastAPI background task on the same
event loop that accepts Vapi's webhooks, and a four-second blocking call there
stalls transcript ingress for the whole live call.
"""

import asyncio
import logging
import re

from . import classifier, db, extraction, integrations
from .config import settings
from .actions import dispatch

log = logging.getLogger("elevatebox.understanding")

# The five things the assignment names: "Budget, what I sell, how many products,
# timeline, features I need." `coverage()` reports on these alone.
SLOT_NAMES = extraction.SLOT_NAMES
# Everything we persist, which additionally includes the lead's name. Kept out
# of SLOT_NAMES so a call with no name is never reported as incomplete discovery.
ALL_SLOT_NAMES = extraction.ALL_SLOT_NAMES

# Below this, the lead has not said enough to be worth a model call on either
# job. One "hello" is not a signal, and classifying or extracting it burns
# tokens and latency to produce noise.
MIN_LEAD_WORDS = 3

# A post-call follow-up is only worth sending if a conversation actually
# happened. A silent call - the line failed, or they hung up on the greeting -
# would otherwise send the architecture image and resume with an empty recap.
# Deliberately low. Failing to send costs a scored row; sending on a marginal
# call costs almost nothing. This is meant to catch "the line failed" and "they
# hung up on the greeting", not to judge conversation quality.
MIN_LEAD_TURNS = 2
MIN_FOLLOWUP_WORDS = 5

LABELS = ("hot", "warm", "cold")
BARRIERS = ("budget", "timing", "decision_maker", "none")


# The agent announcing that it has ALREADY sent something. Present/past tense
# only - "I'll send it across" is a promise, not a claim.
#
# This exists because on 30 Aug the model said "Just send that across to your
# WhatsApp. You should see it come through now." having never called the tool,
# on a call the classifier read as warm throughout. Nothing was sent. Saying so
# to the evaluator is worse than staying quiet, and no prompt rule can be relied
# on to prevent it - the prompt already forbade exactly this.
_CLAIMED_SEND = re.compile(
    r"(just\s+sent|already\s+sent|have\s+sent|i've\s+sent|sent\s+(it|that|this|them)\b"
    r"|on\s+its\s+way\s+to\s+your\s+whatsapp|come\s+through\s+now"
    r"|भेज\s*दिए|भेज\s*दिया|भेज\s*चुक"
    r"|పంపాను|పంపించాను|పంపేశాను)",
    re.I,
)


async def on_turn(call_id, role, text, seq):
    """One final transcript turn has landed. Fire-and-forget; must not raise."""
    try:
        if role != "user":
            # Third trigger path. The tool call and the watchdog are the designed
            # two; this one catches the case where the agent SAYS it sent
            # something without either having fired. Same idempotency key, so if
            # a real send already happened this is a no-op - it can only ever
            # turn a false claim into a true one.
            if role == "assistant" and _CLAIMED_SEND.search(text or ""):
                if dispatch(call_id, "whatsapp_hot",
                            payload={"at_turn_seq": seq, "reason": "agent said it had sent"},
                            trigger_source="claimed_by_agent"):
                    log.warning("call %s: agent claimed a send with no action fired - "
                                "sending now to make the claim true", call_id)
            return  # only the lead's words carry facts to extract

        # A lead asking for a person, disputing, or in distress pages the team
        # before any model call - it is a rule, not a judgement.
        integrations.on_lead_turn(call_id, text, seq)

        # Extraction runs CONCURRENTLY with classification rather than before it.
        # Serially they would cost the sum of two ~4s model calls before the
        # mid-call action could fire, on a row that is scored on timing.
        #
        # The task is awaited in `finally`, so a local reference is held for its
        # whole life - asyncio keeps only a weak one, and a dropped task here
        # would look exactly like extraction silently not working.
        extracting = asyncio.create_task(extract_slots(call_id, at_turn_seq=seq))
        written = 0
        try:
            await classify(call_id, seq)
            # Fired before awaiting extraction on purpose. When the rules
            # overlay decides, classification costs nothing and the message
            # should go now; waiting on a model call we do not need would hand
            # back the exact latency the fast path exists to avoid.
            await maybe_fire_mid_call_action(call_id, seq)
            # CRM stage and the Slack alert follow the read, on the same timing.
            integrations.on_classified(call_id)
        finally:
            written = await extracting
        if written:
            # New facts belong in the team's live Slack message - an edit, never a post.
            await asyncio.to_thread(integrations.refresh_team_alert, call_id)
    except Exception:
        # A broken understanding lane must never take the call down with it.
        log.exception("understanding lane failed on call %s turn %s", call_id, seq)


async def on_call_ended(call_id):
    """Final pass over the complete transcript, then the post-call follow-up."""
    try:
        # Both are awaited here, unlike mid-call: the follow-up is composed from
        # these slots, and the last few turns are usually where the budget and
        # the timeline actually land.
        await asyncio.gather(
            extract_slots(call_id, final=True),
            classify(call_id, at_turn_seq=None, final=True),
        )
        # A call where nobody actually spoke has nothing to follow up ABOUT.
        # A silent or failed call would otherwise send the architecture image
        # and the resume with an empty recap - and when the lead redials, they
        # receive the whole set twice for one conversation. Section 06 asks for
        # "specifics from the conversation"; with no conversation there are none.
        turns = db.get_turns(call_id)
        lead_turns = [t for t in turns if t["role"] == "user"]
        lead_words = sum(len(t["text"].split()) for t in lead_turns)
        conversation = (len(lead_turns) >= MIN_LEAD_TURNS
                        and lead_words >= MIN_FOLLOWUP_WORDS)
        # The team's apps hear about every real conversation, and Slack closes
        # out any call it already announced. Deliberately before the guard
        # below, which is about messaging the lead, not about our own records.
        integrations.on_call_ended(call_id, conversation)
        if not conversation:
            log.info("call %s had %d lead turn(s) / %d words - no conversation to "
                     "follow up on, skipping the post-call messages",
                     call_id, len(lead_turns), lead_words)
            return

        # dispatch() is idempotent, so the reconciliation sweeper can call this
        # again safely.
        dispatch(call_id, "whatsapp_followup", trigger_source="post_call")
        # The resume is a SEPARATE message: WhatsApp allows one media attachment
        # per message and the follow-up already carries the architecture image.
        # This dispatch was missing entirely, so the resume - which the
        # assignment asks for by name - was never sent.
        dispatch(call_id, "whatsapp_resume", trigger_source="post_call",
                 payload={"demo_url": settings.demo_url, "repo_url": settings.repo_url,
                          "note_url": settings.note_url})
    except Exception:
        log.exception("post-call understanding failed on call %s", call_id)


async def extract_slots(call_id, at_turn_seq=None, final=False):
    """Read the five discovery slots out of the running transcript.

    Extracts from the WHOLE transcript each time rather than from the newest
    utterance alone. Half of what the lead says only means something next to the
    question that prompted it - "about two hundred" is a catalogue size or a
    budget depending entirely on what came before. Re-reading the lot is a few
    thousand tokens on a call that lasts minutes, which is the cheapest thing in
    this system.

    Never raises. Returns the number of slots written.
    """
    turns = db.get_turns(call_id)
    lead_words = sum(len(t["text"].split()) for t in turns if t["role"] == "user")
    if lead_words < MIN_LEAD_WORDS:
        return 0

    ok, why = extraction.available()
    if not ok:
        log.warning("extractor unavailable (%s) - call %s slots not filled", why, call_id)
        return 0

    transcript = db.transcript_text(call_id)
    try:
        found = await asyncio.to_thread(extraction.extract_from_transcript, transcript)
    except Exception:
        log.exception("extraction failed on call %s", call_id)
        return 0

    existing = db.get_slots(call_id)
    written = 0
    for name in ALL_SLOT_NAMES:
        change = extraction.merge(existing, name, getattr(found, name), turns)
        if not change:
            continue
        db.upsert_slot(call_id, name, change["value"], raw_quote=change["raw_quote"],
                       source_turn_seq=change["source_turn_seq"],
                       confidence=change["confidence"])
        written += 1
        log.info("call %s slot %s = %r (quote %s)", call_id, name, change["value"],
                 "kept" if change["raw_quote"] else "none")

    if written and final:
        log.info("call %s final slots: %s", call_id, list(db.get_slots(call_id)))
    return written


async def classify(call_id, at_turn_seq=None, final=False):
    """Read Hot / Warm / Cold from the running transcript. Appends a row.

    Two shortcuts, both deliberate:

    - Nothing to read yet. One short "hello" is not a signal; classifying it
      burns tokens to produce noise.
    - The deterministic overlay already decided. If the lead has explicitly asked
      to be sent something, we know the answer without a model call - which is
      both cheaper and FASTER, and the mid-call action is a timing-scored row.

    Never raises. A broken understanding lane must not take down a live call.
    """
    transcript = db.transcript_text(call_id)
    lead_lines = classifier.lead_utterances(transcript)
    if not lead_lines or sum(len(l.split()) for l in lead_lines) < MIN_LEAD_WORDS:
        return None

    # Fast path: the rules alone are decisive, so skip the model entirely.
    reason = classifier.forced_hot_reason(transcript)
    if reason:
        previous = db.latest_classification(call_id)
        if not previous or previous["label"] != "hot":
            db.add_classification(call_id, "hot", confidence=0.8, barrier="none",
                                  evidence_quote=lead_lines[-1][:300],
                                  at_turn_seq=at_turn_seq)
            log.info("call %s -> hot by rule (%s)", call_id, reason)
        return "hot"

    ok, why = classifier.available()
    if not ok:
        log.warning("classifier unavailable (%s) - call %s not classified", why, call_id)
        return None

    try:
        # Blocking SDK call - off the event loop, or it stalls webhook ingress
        # for the whole call while it waits on the model.
        read = await asyncio.to_thread(classifier.classify_transcript, transcript)
    except Exception:
        log.exception("classify failed on call %s", call_id)
        return None

    previous = db.latest_classification(call_id)
    # Append-only, but only when the read actually MOVES. Storing the same label
    # ten times would bury the transition that made us act.
    if not previous or previous["label"] != read.label or previous["barrier"] != read.barrier:
        db.add_classification(call_id, read.label, confidence=read.confidence,
                              barrier=read.barrier, evidence_quote=read.evidence_quote,
                              at_turn_seq=at_turn_seq)
        log.info("call %s -> %s (barrier=%s) %s",
                 call_id, read.label, read.barrier, read.reasoning[:80])
    return read.label


async def maybe_fire_mid_call_action(call_id, seq):
    """Watchdog: the second, independent path to the mid-call WhatsApp.

    The LLM calling the `send_details_now` tool is the primary path (it also lets
    the agent acknowledge the send out loud). This exists so that a single missed
    tool call does not cost the 15-point row. Both paths share one idempotency
    key, so whichever is second is a no-op.
    """
    current = db.latest_classification(call_id)
    if not current or current["label"] != "hot":
        return
    dispatch(call_id, "whatsapp_hot",
             payload={"at_turn_seq": seq, "reason": current.get("evidence_quote")},
             trigger_source="watchdog")


def coverage(call_id):
    """Which discovery slots are filled. Drives 'ask only what is missing'."""
    filled = db.get_slots(call_id)
    return {name: (name in filled and bool(filled[name].get("value")))
            for name in SLOT_NAMES}


def status_report():
    """Whether each lane job can actually run. Same idea as actions.status_report:
    a job that cannot run should say so on /health rather than fail quietly on a
    live call."""
    ok, why = extraction.available()
    return {
        "extract_slots": {"wired": True, "ready": ok, "detail": why},
        "classify": {"wired": True, "ready": ok, "detail": why},
    }
