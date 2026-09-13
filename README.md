# AI Voice Sales Agent

An autonomous voice system that phones a lead, sells e-commerce website development in
**English, Hindi or Telugu**, works out how serious the buyer is, and acts on it **while
the call is still live** — firing a WhatsApp on high intent, booking callbacks from
spoken time, and following up with a message that quotes what the person actually said.

Built for the ElevateBox SDE Intern assignment.

---

## What it does on a call

```
dial → speak → sell → discover → understand → classify → act mid-call → schedule → follow up
```

1. **Dials by itself.** One button on a web page, or a scheduled callback re-entering the
   same path. No human on the line.
2. **Mirrors the lead's language.** Opens in English; if they answer in Telugu or Hindi it
   switches and stays there. Handles code-mixed sentences — *"Budget ante around one lakh
   anukuntunna"* — without breaking.
3. **Runs discovery** on products, catalogue size, timeline, features and budget, in a
   natural order, never re-asking what was volunteered.
4. **Reads intent** as Hot / Warm / Cold from indirect answers, continuously, off the
   speech path.
5. **Acts during the call.** On high intent a WhatsApp goes out mid-conversation, through
   two independent trigger paths that share one idempotency key.
6. **Books callbacks from speech** — *"రేపు ఉదయం"*, *"कल दो बजे"*, *"after 6"* — and says
   the resolved time back for confirmation.
7. **Follows up** with the call's real specifics, a verbatim quote, the architecture image
   and the résumé.

---

## Measured, not claimed

| | Result |
|---|---|
| Turn latency | **median 1.7 s, 0 of 9 turns over 3 s** (Vapi's own metrics) |
| Discovery coverage | **5/5 topics** on a full Hindi call |
| Intent classification | **97%** (57/59) on Groq `gpt-oss-20b` · **4/4** on the brief's own phrases · hi 4/4, te 3/4, code-mixed 5/5 · rules layer **59/59** |
| Callback time resolution | **57/57** phrasings against a frozen clock, en/hi/te |
| Slot extraction | **30 slots** over 7 real calls, 29 verbatim-quoted, **0 quote-integrity violations** |
| Multi-app replay | **45/45** checks against live HubSpot and Slack, every call replayed twice — one contact, one deal, one note, one Slack message (Calendar checks pending a valid Google token) |
| Backend | **170 assertions**, no network, no spend |

Every number above is reproducible from this repo — see **Running it**.

---

## How it's built

Two lanes. The **speech loop** (STT → LLM → TTS) is never blocked by a decision or a side
effect; a parallel **understanding lane** consumes the same transcript, extracts facts,
classifies, and fires actions independently. That single split is what makes sub-3-second
latency and mid-call side effects possible at the same time.

```
Telnyx ──SIP──► Vapi ──► Soniox STT ─► gpt-4o-mini ─► Cartesia TTS      speech lane
                          │
                          ▼
                   FastAPI + SQLite ──► extraction ─┐  gpt-oss-20b       understanding
                          │             classification ┘  (Groq)          lane
                          ▼
                   idempotent action bus ──► Google Calendar · HubSpot · Slack
                                             WhatsApp · callback worker
```

Full component breakdown, every technology decision and what was rejected:
**[docs/system-design.md](docs/system-design.md)** — §8b records where reality diverged
from the plan and why.

---

## The multi-app layer

While the call is live, the agent acts in the sales team's own apps. Every action is a row on
the same idempotent action bus as the WhatsApp messages, so it runs in the background, retries
transient failures, and leaves a ledger entry saying what fired and why
([`backend/app/integrations.py`](backend/app/integrations.py)).

| App | When | What | Its own duplicate guard |
|---|---|---|---|
| **Google Calendar** | inside `schedule_callback` | `freeBusy` on the requested slot, 0.8 s budget. Free → book. Taken → nothing booked; the agent offers the next free slot. No answer → book, flagged `unchecked` | event id derived from the call id: a retry is a 409 that becomes an update, and a restated time moves the one event |
| **HubSpot** | each change of read, a booked callback, call end | contact by phone · Hot → deal at the hot stage · Warm + callback → warm stage · Cold → contact only · one note with verbatim quotes | ids on the call row; a known contact reused before searching; a deal stage only moves forward |
| **Slack** | Hot mid-call, callback booked, CRM record written, call end | one message per call, edited in place. "Speak to a person", a dispute or distress → a threaded reply broadcast to the channel | message `ts` on the call row: update, never re-post |

WhatsApp to the lead is switched off by default (`WHATSAPP_ENABLED=1` brings the whole send path
back), and the agent never promises the lead a message. There are no calling hours: the lead picks
the callback time, and the calendar only decides whether that slot is free.

The web UI at `/` starts a web or phone call, shows the live transcript, the intent timeline and
a deep link into every app action, flags carrier audio faults, checks each integration live, and
shows the latest replay evidence.

---

## Documentation

| Read this | For |
|---|---|
| **[system-design.md](docs/system-design.md)** | Architecture, components, call flow, data model, every choice with its rejected alternatives, and the as-built divergences |
| **[requirements-analysis.md](docs/requirements-analysis.md)** | Every requirement normalised, MUST vs implied vs optional, ten open questions, requirement-to-test matrix |
| **[audit.md](docs/audit.md)** | A critical review of my own plan — 11 problems found, 3 severe, and roughly 30 thresholds marked as mine rather than the brief's |
| **[g10-provider-inventory.md](docs/g10-provider-inventory.md)** | Why Soniox and not Deepgram; §5b is where vendor documentation turned out to be wrong and the live API corrected it |
| **[implementation-plan.md](docs/implementation-plan.md)** | Ordered phases, tests, definitions of done, cut lines |
| **[cost-and-feasibility.md](docs/cost-and-feasibility.md)** | External feasibility gates, cost model, score strategy |
| **[deployment.md](docs/deployment.md)** | Render, step by step |
| **[model-comparison.md](docs/model-comparison.md)** | Classification model choice, verified pricing |
| **[NOTE.md](NOTE.md)** | The short note: what works, what doesn't, what I'd build next |

---

## Running it

Nothing here needs an API key or spends money.

```bash
python backend/smoke_test.py          # 123 assertions, no network
python backend/eval_timeparse.py      # 57 spoken-time phrasings, frozen clock
python backend/eval_classifier.py     # deterministic classifier rules
python backend/eval_extraction.py     # quote-integrity verifier
python agent/agent.py check           # voice agent config, offline
```

With credentials in `.env` (see [`backend/.env.example`](backend/.env.example)):

```bash
python -m uvicorn app.main:app --app-dir backend --port 8000
python agent/agent.py deploy          # push prompt + config to Vapi, free
python agent/agent.py call --yes      # places a REAL call
python agent/agent.py review          # score the last call offline
python backend/replay_harness.py      # 3 calls, each replayed twice through the REAL apps,
                                      # verified by asking the apps, then cleaned up
```

`GET /api/integrations` checks every connected app live, read-only. `backend/replay_harness.py
--model scripted` proves the apps without spending model tokens; WhatsApp is always stubbed there.

`POST /calls` **takes no phone number.** The destination comes only from
`ALLOWED_DESTINATION`, so the endpoint cannot be used as an open dialler if the URL leaks.

---

## Layout

```
agent/       voice agent: system prompt, Vapi config, CLI (stdlib only)
backend/     FastAPI: webhook ingress, understanding lane, action bus, workers
  app/         classifier · extraction · timeparse · callbacks · whatsapp · reconcile
  eval/        labelled classification cases
docs/        analysis, design, audit, runbooks
assets/      architecture image and résumé, inlined into the follow-up
```

Two direct dependencies — `fastapi` and `uvicorn`, plus `anthropic` for the understanding
lane. Persistence is stdlib `sqlite3`; HTTP is stdlib `urllib`. No ORM, no migration tool,
no HTTP client library.
