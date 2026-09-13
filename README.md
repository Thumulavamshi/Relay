# Relay — an AI sales agent that works inside your team's apps

Relay phones a lead, holds a real sales conversation in **English, Hindi or Telugu**, works out how
serious the buyer is, and does the sales team's follow-up work **while the call is still live**:
it checks the rep's **Google Calendar** before booking a callback, keeps **HubSpot** up to date, and
alerts the team in **Slack** — each action exactly once, with proof.

Voice is the input channel. The apps, and the evidence that every action happened once, are the
product.

*Started as the ElevateBox SDE Intern assignment; extended into a multi-app agent for the
Lemma × Comma Capital Multi-App Agent Hackathon.*

---

## Demo Video

**Demo Link:** [View Demo Video](https://drive.google.com/file/d/1I5SUvSTlaAzBnO6durlsTxkytW9DO_dl/view?usp=sharing)

## What it does

1. **Calls by itself.** One button on the dashboard rings the configured number, or starts a **web
   call** in the browser (same agent, same tools, no phone line). A booked callback re-dials on its
   own at the agreed time, and opens by referring to the last conversation.
2. **Speaks the lead's language.** Opens in English; if they answer in Hindi or Telugu it switches
   and stays there, and it copes with code-mixed sentences.
3. **Runs discovery** — what they sell, catalogue size, timeline, features, budget — in a natural
   order, never re-asking what was already volunteered.
4. **Reads intent** as Hot / Warm / Cold from indirect answers, continuously, without slowing the
   conversation down.
5. **Checks the calendar before promising a time.** "Call me tomorrow at four" → if four is taken,
   nothing is booked and the agent offers the next free slot. There are no calling hours: the lead
   picks the time, and the calendar only decides whether it is free.
6. **Keeps the CRM honest.** Contact by phone; a deal at the pipeline stage the conversation earned;
   one note at the end with the lead's own words.
7. **Tells the team while it matters.** A Slack message the moment a lead turns hot, edited in place
   as the call moves on. "Can I speak to a real person?", a complaint or distress pages the team
   separately.
8. **Shows its work.** A dashboard with the live transcript, the intent timeline, deep links into
   every app action, carrier-fault warnings, live integration checks, and replay evidence.

The agent never sends the lead anything and never says it has. WhatsApp to the lead is switched off
(see [Configuration](#configuration-reference)).

---

## One call, moment by moment

| On the call | Relay does | You see it in |
|---|---|---|
| The lead answers | Call row created; transcript starts streaming | Dashboard → Calls → the call |
| "I run a grocery store, about 200 products, within a month" | Facts extracted, each tied to the lead's exact words | Call page → *What they told us* |
| "Send me the details" | Read as **Hot**. HubSpot contact + deal at the hot stage. Slack: *"Hot lead on the line now"*. The agent says the team will put details together and offers a callback | Slack · HubSpot · Call page → *Intent* |
| "Call me back tomorrow at four" | Calendar `freeBusy` on 4:00. Taken → the agent offers the next free slot | Call page → *In the team's apps* |
| "Five is fine" | Callback booked; Calendar event created; Slack message edited with the time; a warm lead's deal opens now | Google Calendar · Slack |
| "Can I speak to a real person?" | Threaded Slack reply broadcast to the channel, once per call | Slack |
| Hang-up | HubSpot note with the read, barrier, verbatim quotes and links; Slack message changes to *Call ended* | HubSpot deal timeline · Slack |
| The agreed callback time | Relay rings the lead back, with the last call's facts so it does not repeat discovery | Calls list |

---

## Measured, not claimed

| | Result |
|---|---|
| **Multi-app replay against the real apps** | **54/54** — 3 scenarios (hot, warm Hindi-English with a taken slot, cold), each replayed twice through Google Calendar, HubSpot and Slack, and verified by asking each app. Replays created no second event, deal, note or Slack message |
| Offline backend suite | **175 assertions**, every transport faked, no network, no spend |
| Intent classification | **97%** (57/59) on Groq `gpt-oss-20b` · **4/4** on the brief's own example phrases · Hindi 4/4, Telugu 3/4, code-mixed 5/5 · rules layer 59/59 |
| Callback time resolution | **57/57** spoken phrasings in English, Hindi and Telugu, against a frozen clock |
| Slot extraction | 30 slots over 7 real calls, 29 verbatim-quoted, 0 quote-integrity violations *(measured on the earlier Claude understanding lane)* |
| Turn latency | median 1.7 s, 0 of 9 turns over 3 s *(Vapi's own metrics, pre-hackathon calls)* |

How to reproduce each one is in [Testing](#testing).

---

## How it's built

Two lanes. The **speech loop** is never blocked by a decision or a side effect. A parallel
**understanding lane** reads the same transcript, extracts facts, classifies intent and fires
actions independently. That split is what lets the agent keep talking at conversational speed while
the Calendar, HubSpot and Slack work happens.

```
 Dashboard (web call / phone call)
        │
        ▼
      Vapi ──SIP──► Telnyx ──► the lead's phone        (or the browser, for a web call)
        │
        │  Soniox STT (en/hi/te) ─► gpt-4.1 ─► Cartesia TTS            SPEECH LANE
        │
        │  webhooks: transcript · tool calls · status · end-of-call
        ▼
  FastAPI + SQLite ──► extraction ──┐  Groq gpt-oss-20b,            UNDERSTANDING LANE
        │               classification ┘  strict JSON, off the speech path
        │
        ▼
  idempotent action bus ──► Google Calendar · HubSpot · Slack
        │
        ├── callback worker (re-dials at the booked time)
        └── reconcile sweeper (finishes calls whose webhook never arrived)
```

### The action bus and its guarantees

Every app action is a row in one ledger, keyed by **call × action × moment** (`crm_sync:hot`,
`calendar_event:cb12`). A repeated moment is a no-op; the key is enforced by a UNIQUE index, not by
code. Actions run in the background, retry transient failures (timeouts, 429, 5xx) with backoff, and
record what fired, why, and whether it worked. Each handler **converges** its app to the call's
current state rather than appending, so running it twice changes nothing.

Each app also carries **its own duplicate guard**, because the bus stops us dispatching twice but not
a network retry after a success whose response never arrived:

| App | When it acts | What it does | Its own duplicate guard |
|---|---|---|---|
| **Google Calendar** | Inside the `schedule_callback` tool, while the lead waits | `freeBusy` on the slot (0.8 s budget). Free → book. Taken → book nothing, hand the agent the next free half-hour. No answer in time → book anyway, flagged `unchecked` | Event id derived from the call id: a retry becomes a 409, then an update. A restated time *moves* the one event |
| **HubSpot** | Each change of intent, a booked callback, call end | Contact by phone · Hot → deal at `HUBSPOT_STAGE_HOT` · Warm + callback → `HUBSPOT_STAGE_WARM` · Cold → contact only · one note at the end | Ids stored on the call row; a contact an earlier call found is reused before searching; a deal stage only moves forward |
| **Slack** | Hot mid-call, callback booked, CRM record written, call end, escalation | One message per call, edited in place. Escalations are a threaded reply broadcast to the channel | Message `ts` on the call row: update, never re-post. The escalation ping is recorded, so it cannot repeat |

### Tech stack

| Layer | Choice | Why |
|---|---|---|
| Voice orchestration | **Vapi** | Barge-in, endpointing, tool calls and webhooks as configuration |
| Telephony | **Telnyx**, bring-your-own SIP trunk | Direct RTP to Vapi; the voice-API relay dropped caller audio |
| Speech-to-text | **Soniox** `stt-rt-v5`, constrained to en/hi/te | The only option tested that does Telugu *and* code-switching |
| Conversation model | **OpenAI `gpt-4.1`** via Vapi | Instruction-following across a long multilingual prompt |
| Text-to-speech | **Cartesia** `sonic-3.5`, one native Telugu voice | One speaker identity across all three languages |
| Understanding lane | **Groq `openai/gpt-oss-20b`**, strict JSON schema | 97% on the labelled set; fast; schema-valid output guaranteed |
| Backend | **FastAPI** + **SQLite** (stdlib `sqlite3`) | Async webhook ingress; no ORM, no migration tool |
| App integrations | Stdlib `urllib`, no SDKs | Google, HubSpot, Slack and Groq all fail the same way, into the ledger |
| Dashboard | Static HTML + vanilla JS, **Vapi Web SDK** for web calls | No build step; polls the same read routes as the API |
| Local exposure | **cloudflared** quick tunnel | Vapi can reach a laptop; nothing to host |

Design decisions, rejected alternatives, and where reality diverged from the plan:
[docs/system-design.md](docs/system-design.md) (§8b is the as-built record).

---

## The dashboard

Open `http://localhost:8000` while the backend runs.

| Page | What it shows |
|---|---|
| **Start** | *Web call* (talk from the browser) and *Phone call* (rings the configured number), plus the latest call |
| **Calls** | Every call: time, length, lead, intent, a status badge per app (Calendar / HubSpot / Slack), fault flags, callback time |
| **Call page** | Live transcript · intent timeline with the quote that decided it · extracted facts with the lead's exact words · per-app status with deep links into the Calendar event, HubSpot deal and contact, and Slack channel · the action ledger · banners for escalations and carrier faults |
| **Integrations** | A live, read-only check of each app — including that the HubSpot stage ids exist in the pipeline — and each app's last action and error |
| **Evidence** | The latest replay-harness run: pass rate, every assertion per scenario, what was skipped and why |

**Carrier-fault flags.** About 18% of past phone calls delivered no caller audio even though the
carrier saw healthy media. The call page flags both signatures: *not answered after 15 s* (silent
ring) and *answered, 20 s in, agent spoke, lead never did* (no caller audio). It never redials by
itself — that would place a second real call. A web call avoids the carrier entirely.

### HTTP routes

| Route | Purpose |
|---|---|
| `GET /` | The dashboard (`/classic` is the original one-button page; `/monitor` the original call monitor) |
| `POST /calls` | Place a call. **Takes no phone number** — the destination comes only from `ALLOWED_DESTINATION` / `TEST_NUMBER`, so the endpoint cannot be used to dial anyone else |
| `GET /calls`, `GET /calls/{id}`, `GET /calls/{id}/transcript` | Calls; one call with turns, facts, intent history, actions, callbacks, app links and flags |
| `POST /vapi/webhook` | The single ingress from Vapi (transcripts, tool calls, status, end-of-call report) |
| `GET /health` | Config, readiness, action wiring, per-app status |
| `GET /api/integrations` | Live, read-only check of every app |
| `GET /api/calls` · `GET /api/calls/by-provider/{id}` | Call summaries for the dashboard · map a Vapi call id to ours (web calls) |
| `GET /api/config` | What the dashboard needs to start a call (the Vapi *public* key; the destination masked) |
| `GET /api/evals/latest` | The latest replay-harness evidence |
| `GET /docs` | Interactive API docs |

---

## Setup

### 1. Prerequisites

- **Python 3.11+**
- **cloudflared**, for the tunnel: `winget install --id Cloudflare.cloudflared` (Windows) or `brew install cloudflared` (macOS)
- Accounts: **Vapi** (with a phone number connected), **Groq**, **Google** (Cloud project + Calendar), **HubSpot** (free CRM), **Slack** (a workspace you can install apps in)

### 2. Get the keys

<details>
<summary><b>Vapi</b> — private key, public key, phone number id</summary>

1. Vapi dashboard → **API Keys**. Copy the **private** key → `VAPI_API_KEY`, and the **public** key → `VAPI_PUBLIC_KEY` (the public key is for the browser web call).
   If you restrict the public key's allowed origins, include `http://localhost:8000`.
2. **Phone Numbers** → your number → copy its **ID** (a UUID, not the number itself) → `VAPI_PHONE_NUMBER_ID`.
3. `VAPI_ASSISTANT_ID` is printed by your first `python agent/agent.py deploy`. Paste it into `.env` so later deploys update the same assistant.
</details>

<details>
<summary><b>Groq</b> — the understanding model</summary>

1. [console.groq.com](https://console.groq.com) → **API Keys** → **Create API Key** → `GROQ_API_KEY`.
2. Set `CLASSIFIER_PROVIDER=groq` and `CLASSIFIER_MODEL=openai/gpt-oss-20b`.
</details>

<details>
<summary><b>Google Calendar</b> — OAuth client and refresh token</summary>

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project → **APIs & Services → Library** → enable **Google Calendar API**.
2. **OAuth consent screen** → *External* → fill in the app name and emails. Under **Audience → Test users**, add your own Google account. Under **Data access**, add the scope `https://www.googleapis.com/auth/calendar`.
3. **Clients → Create client → Web application**. Add the redirect URI `https://developers.google.com/oauthplayground`. Copy the client id → `GOOGLE_CLIENT_ID` and the secret → `GOOGLE_CLIENT_SECRET`.
4. Open [OAuth Playground](https://developers.google.com/oauthplayground/) → gear icon → **tick "Use your own OAuth credentials"** and paste the id and secret. *(Without this the token belongs to the Playground's own client and fails with `invalid_grant`.)*
5. Paste the scope into *Input your own scopes* → **Authorize APIs** → sign in → **Exchange authorization code for tokens** → copy the **refresh token** → `GOOGLE_REFRESH_TOKEN`.
6. In Google Calendar → settings for the calendar you want → **Integrate calendar** → **Calendar ID** → `GOOGLE_CALENDAR_ID` (or use `primary`). Set that calendar's time zone to India Standard Time.
</details>

<details>
<summary><b>HubSpot</b> — private app token, portal id, stage ids</summary>

1. HubSpot → **Development → Legacy apps → Create legacy app → Private**. Scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.deals.read`, `crm.objects.deals.write` (notes need only the contact scopes). **Create** → **Auth** tab → **Show token** → `HUBSPOT_TOKEN` (starts `pat-`).
2. Account menu (top right) → copy the **Hub ID** → `HUBSPOT_PORTAL_ID` (used for deep links).
3. Stage ids belong to your pipeline. List them with:
   ```bash
   curl -H "Authorization: Bearer <HUBSPOT_TOKEN>" https://api.hubapi.com/crm/v3/pipelines/deals
   ```
   Set `HUBSPOT_PIPELINE_ID` (usually `default`), `HUBSPOT_STAGE_HOT` (e.g. *Campaign assessment*) and `HUBSPOT_STAGE_WARM` (e.g. *Introductory meeting*). The Integrations page shows an error if a stage id is not in the pipeline.
</details>

<details>
<summary><b>Slack</b> — bot token and channel</summary>

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From scratch** → pick your workspace.
2. **OAuth & Permissions → Bot Token Scopes** → add `chat:write` → **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`) → `SLACK_BOT_TOKEN`.
3. Create a channel such as `#sales-alerts` and run `/invite @your-app` in it.
4. Channel name → **About** → copy the **Channel ID** (`C…`) → `SLACK_CHANNEL_ID`.
</details>

### 3. Create the `.env`

Copy [`backend/.env.example`](backend/.env.example) and fill it in. The backend and `agent.py` read
the **first** of these that exists, and **only that one** — keep every key in a single file:

`backend/.env` → `.env` (repo root) → `experiments/voice-feasibility/.env`

`.env` and `.env.*` are gitignored; never commit keys. The minimum for a full demo:

```dotenv
# Voice
VAPI_API_KEY=
VAPI_PUBLIC_KEY=
VAPI_PHONE_NUMBER_ID=
VAPI_ASSISTANT_ID=            # printed by the first deploy
TEST_NUMBER=+91XXXXXXXXXX     # the ONLY number Relay may dial

# Understanding lane
CLASSIFIER_PROVIDER=groq
CLASSIFIER_MODEL=openai/gpt-oss-20b
GROQ_API_KEY=

# Google Calendar
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
GOOGLE_CALENDAR_ID=primary

# HubSpot
HUBSPOT_TOKEN=
HUBSPOT_PORTAL_ID=
HUBSPOT_PIPELINE_ID=default
HUBSPOT_STAGE_HOT=
HUBSPOT_STAGE_WARM=

# Slack
SLACK_BOT_TOKEN=
SLACK_CHANNEL_ID=

# Where Vapi reaches this machine - the cloudflared URL (see Running it)
SERVER_URL=https://<words>.trycloudflare.com
```

- **`SERVER_URL`** does two jobs: `deploy` points Vapi's webhooks and tools at it, and the backend
  uses it for the "open call view" links in Slack, HubSpot and Calendar. `PUBLIC_BASE_URL` overrides
  the second job only; leave it unset.
- **`VAPI_WEBHOOK_SECRET`**: leave unset for local runs. The backend expects the secret in an
  `x-vapi-secret` header, and Vapi's current authentication model may not send it that way — if it
  does not, every webhook is rejected with 401. A quick-tunnel URL is random and short-lived anyway.

### 4. Install

```bash
python -m pip install -r backend/requirements.txt
```

---

## Running it

Nothing is hosted. The backend runs on your laptop, and a free Cloudflare tunnel gives Vapi a public
URL that reaches it. Use three terminals, from the repo root.

**Terminal 1 — the tunnel.** Copy the `https://….trycloudflare.com` URL it prints into `SERVER_URL`.

```bash
cloudflared tunnel --url http://localhost:8000
```

**Terminal 2 — the backend.** Start it *after* setting `SERVER_URL`; `.env` is read once at startup.

```bash
python -m uvicorn app.main:app --app-dir backend --port 8000 --reload
```

Then open `http://localhost:8000/#/integrations` — Calendar, HubSpot, Slack and the model should
all be green. `https://<tunnel>/health` should answer too.

**Terminal 3 — point Vapi at the tunnel.** Pushes the prompt, the voice config and the
`schedule_callback` tool, then verifies what Vapi stored. It should end with `READY`.

```bash
python agent/agent.py deploy
```

In the Vapi dashboard, clear any old organisation-level server URL, or set it to the same
`https://<tunnel>/vapi/webhook`, so nothing points at a dead deployment.

**Make a call**

- **Web call:** `http://localhost:8000` → **Start web call** → allow the microphone.
- **Phone call:** the **Call** button on the Start page, or:
  ```bash
  python agent/agent.py call --yes
  ```
  It refuses to dial if the running backend is stale or the assistant on Vapi differs from the files.

**Review a call offline** (latency per turn, discovery coverage, turn length):

```bash
python agent/agent.py review
```

**Every time the tunnel restarts** its URL changes: update `SERVER_URL` → restart the backend →
`python agent/agent.py deploy`.

---

## Testing

### Offline — no keys, no network, no spend

```bash
python backend/smoke_test.py
```
175 assertions. Replays realistic webhook sequences with every transport faked: trigger safety, idempotency, taken slots, one calendar event per call, deal stages that never move back, one Slack message per call, escalation paging once, no WhatsApp when disabled, no calling-hours limit.

```bash
python backend/eval_timeparse.py
```
57 spoken callback times in English, Hindi and Telugu against a frozen clock.

```bash
python backend/eval_classifier.py
```
The deterministic rules layer over 59 labelled cases.

```bash
python agent/agent.py check
```
Voice agent config, safety guards, credentials present.

### With a model key

```bash
python backend/eval_classifier.py --llm
```
The full Hot / Warm / Cold pipeline on the 59 cases (uses Groq tokens).

### Against the real apps — the replay harness

```bash
python backend/replay_harness.py --model scripted
```

Replays three calls — hot (English), warm Hindi-English asking for a slot the harness has blocked,
and cold — **twice each** through the real FastAPI app and the real Google Calendar, HubSpot and
Slack. Then it asks each app, not our database, whether the right thing exists: the event at the
booked IST time, exactly one event per call, the deal at the stage the read earned (or no deal),
exactly one note quoting the lead, the Slack message in the channel, and the same ids on the second
pass as the first.

- `--model scripted` uses fixed reads (no model tokens); drop it to use the live model.
- It uses a throwaway database, stubs anything lead-facing, and cannot dial.
- **Cleanup** sends its HubSpot records to the recycle bin and deletes its calendar events; a
  contact that existed before the run is never touched. Slack test posts stay unless you pass
  `--delete-slack`. `--keep` leaves everything in place to show it; `--runs N` repeats it.
- Results land on the dashboard's **Evidence** page.

### Manual test on a live call

Block **4–5 PM tomorrow** on the calendar first.

| Say | Expect |
|---|---|
| "I run a grocery store, about 200 products, I need UPI payments" | *What they told us* fills in, with your words |
| "Send me the details" | Intent → **hot**; Slack *"Hot lead on the line now"*; HubSpot deal at the hot stage; the agent offers a callback and does not claim to send anything |
| "Call me back tomorrow at four" | *"Four is taken — would five work?"* Nothing booked yet |
| "Five is fine" | Calendar event at 5 PM; the Slack message edits in the time |
| "Call me back in an hour" (at any hour) | Booked and read back, with no calling-hours objection |
| "Can I speak to a real person?" | A threaded Slack ping, once |
| Hang up | HubSpot note with your quotes; Slack shows *Call ended* |

---

## Troubleshooting and workarounds

| Symptom | Cause | Fix |
|---|---|---|
| Integrations: Google `invalid_grant` | The refresh token was issued to OAuth Playground's own client, was revoked, or expired — tokens from a consent screen in *Testing* mode last **7 days** | Regenerate it with "Use your own OAuth credentials" ticked (Setup → Google, step 4); replace `GOOGLE_REFRESH_TOKEN`; restart the backend |
| Nothing appears on the Calls page during a call | Vapi cannot reach the backend: tunnel down, or `SERVER_URL` stale | Check `https://<tunnel>/health`; update `SERVER_URL`; restart; `deploy` |
| Backend log shows `401` on `/vapi/webhook` | `VAPI_WEBHOOK_SECRET` is set and Vapi is not sending it as `x-vapi-secret` | Unset it, restart the backend, `deploy` |
| `agent.py call` refuses: *backend is STALE* | The running server is older than the code on disk | Restart uvicorn (keep `--reload`) |
| `agent.py call` refuses: *live assistant is not what is on disk* | The prompt or config changed without a deploy | `python agent/agent.py deploy` |
| Intent stuck at "reading…"; `groq 429` in the log | Groq free tier: 8,000 tokens a minute, 1,000 requests a day. Each lead turn sends two model passes | Wait a minute between calls; or upgrade the Groq tier; or set `CLASSIFIER_PROVIDER=anthropic` with `ANTHROPIC_API_KEY`. "Send me the details"-style phrases still read hot without the model |
| HubSpot action failed with `403` | The token is missing a scope | Add contacts/deals read+write scopes to the private app |
| HubSpot action failed on the deal stage | `HUBSPOT_STAGE_*` is not a stage in the pipeline | Use ids from `/crm/v3/pipelines/deals`; the Integrations page confirms them |
| Slack action failed: `not_in_channel` | The bot was never invited | `/invite @your-app` in the channel |
| The agent never offers an alternative slot | The blocked time is on a different calendar than `GOOGLE_CALENDAR_ID` | Block it on that calendar, or change the id |
| *Start web call* is disabled | `VAPI_PUBLIC_KEY` not set | Add it and restart the backend |
| Web call errors immediately | Public key restricted to other origins or assistants; microphone blocked | Allow `http://localhost:8000` and the assistant in Vapi; allow the mic in the browser |
| Call page banner: *No audio from the caller* | Carrier media fault, not the agent | Hang up and redial, or use a web call |
| `.env` change has no effect | Settings are read once at startup; `--reload` only watches code | Restart the backend |
| A second `.env` "does nothing" | Only the first `.env` found is read | Keep all keys in one file |
| Port 8000 already in use | Another backend is running | Stop it, or use another `--port` and tunnel to that port |

---

## Known limitations

- **Groq free-tier limits** can starve the understanding lane on a busy call (see Troubleshooting). A
  paid tier, or coalescing passes to one at a time per call, removes it.
- **Speech recognition can mishear names** — deal names then carry the misheard name.
- **Google refresh tokens from a Testing-mode consent screen expire after 7 days.** Publish the
  consent screen for a long-lived token.
- **One destination number, by design.** `POST /calls` dials only the configured number, so a leaked
  URL cannot become an open dialler. Calling a lead list is future work.
- **The quick-tunnel URL changes on every restart**, which means re-deploying. A named tunnel or a
  hosted backend fixes that.
- **SQLite on one instance.** Scaling out needs Postgres; the schema is written to port.
- **Replaying the same webhooks duplicates transcript turns** in the database; app records are
  unaffected.
- **The Vapi Web SDK loads from a CDN** in the browser, so web calls need internet access on the
  viewing machine.
- **WhatsApp is off.** If re-enabled (`WHATSAPP_ENABLED=1`), it runs through UltraMsg, an unofficial
  gateway, because Meta business verification stalled.

---

## Configuration reference

All settings are environment variables, read once at startup.

| Variable | Default | Meaning |
|---|---|---|
| `VAPI_API_KEY` · `VAPI_PHONE_NUMBER_ID` · `VAPI_ASSISTANT_ID` | — | Vapi private key, phone number id, assistant id |
| `VAPI_PUBLIC_KEY` | — | Enables the browser web call |
| `TEST_NUMBER` · `ALLOWED_DESTINATION` | — | The only number Relay may dial; `ALLOWED_DESTINATION` wins |
| `SERVER_URL` · `PUBLIC_BASE_URL` | — | Tunnel/host URL for Vapi webhooks and app deep links |
| `VAPI_WEBHOOK_SECRET` | unset | Shared secret checked on webhooks (see Setup) |
| `DATABASE_PATH` | `backend/data/elevatebox.db` | SQLite file |
| `CLASSIFIER_PROVIDER` · `CLASSIFIER_MODEL` | `groq` · `openai/gpt-oss-20b` | Understanding model (`groq`, `anthropic`, `gemini`) |
| `GROQ_API_KEY` · `ANTHROPIC_API_KEY` · `GEMINI_API_KEY` | — | Key for the chosen provider |
| `GROQ_REASONING_EFFORT` · `GROQ_REASONING_TOKENS` · `GROQ_TIMEOUT_SECONDS` | `low` · `1024` · `30` | Groq tuning |
| `GOOGLE_CLIENT_ID` · `GOOGLE_CLIENT_SECRET` · `GOOGLE_REFRESH_TOKEN` | — | Google OAuth |
| `GOOGLE_CALENDAR_ID` | `primary` | The rep's calendar |
| `GCAL_FREEBUSY_TIMEOUT` · `GCAL_SLOT_MINUTES` · `GCAL_SEARCH_DAYS` | `0.8` · `30` · `3` | Availability budget (s), callback length (min), how far to look for an alternative (days) |
| `HUBSPOT_TOKEN` · `HUBSPOT_PORTAL_ID` | — | HubSpot private app token; portal id for links |
| `HUBSPOT_PIPELINE_ID` · `HUBSPOT_STAGE_HOT` · `HUBSPOT_STAGE_WARM` | `default` · `qualifiedtobuy` · `appointmentscheduled` | Where deals go (stage ids are per portal) |
| `SLACK_BOT_TOKEN` · `SLACK_CHANNEL_ID` | — | Slack bot and channel |
| `ACTION_MAX_ATTEMPTS` | `3` | Retries for a transient app failure |
| `CALLBACK_POLL_SECONDS` · `CALLBACK_MAX_LATENESS_MINUTES` | `20` · `30` | Callback worker cadence; a callback later than this is marked missed, not dialled |
| `RECONCILE_POLL_SECONDS` · `RECONCILE_STALE_MINUTES` | `60` · `3` | Sweeper for calls whose final webhook never arrived |
| `ENDPOINT_WAIT` · `ENDPOINT_NO_PUNCT` · `ENDPOINT_PUNCT` | from `assistant.json` | Turn-taking dials, applied on `deploy` |
| `VOICE_PROVIDER` · `VOICE_ID` · `VOICE_MODEL` · `BACKGROUND_SOUND` | from `assistant.json` | Voice overrides, applied on `deploy` |
| `WHATSAPP_ENABLED` | off | `1` restores lead-facing WhatsApp (plus `ULTRAMSG_*` or Meta `WHATSAPP_*` keys) |

An app with no credentials is simply never called, and the Integrations page says so.

---

## Project layout

```
agent/
  prompt.md            the system prompt - the agent's behaviour, in one readable file
  assistant.json       Vapi config: transcriber, voice, turn-taking, the schedule_callback tool
  agent.py             check · deploy · inspect · call · review  (stdlib only)
backend/
  app/
    main.py            routes: dashboard, API, Vapi webhook ingress, tool calls
    understanding.py   the understanding lane: extraction + classification per turn, call end
    classifier.py      Hot/Warm/Cold: rules overlay + model (Groq / Anthropic / Gemini)
    extraction.py      the five discovery facts, each quote verified against the transcript
    timeparse.py       spoken time -> IST datetime, deterministic, en/hi/te
    callbacks.py       calendar-checked booking; the worker that re-dials at the booked time
    actions.py         the idempotent action bus: ledger, background runs, retries
    integrations.py    when each app acts, and the app handlers on the bus
    gcal.py            Google Calendar: freeBusy, next free slot, one event per call
    hubspot.py         HubSpot: contact by phone, forward-only deal stages, one note per call
    slack.py           Slack: one message per call, edited in place; escalations
    escalation.py      "speak to a person", disputes, distress - en/hi/te rules
    callfacts.py       one read of a call, shared by Calendar, HubSpot and Slack
    views.py           read models for the dashboard; carrier-fault flags
    reconcile.py       finishes calls whose end-of-call webhook never arrived
    rest.py            the one JSON-over-HTTPS helper every integration uses
    static/index.html  the dashboard
    schema.sql · db.py SQLite schema and access
  smoke_test.py        offline suite (175 assertions)
  replay_harness.py    replay against the real apps, verified by the apps
  eval_classifier.py · eval_timeparse.py · eval_extraction.py
docs/                  design, audit, runbooks, demo script
```

Direct dependencies: `fastapi` and `uvicorn` (plus an optional model SDK for Anthropic or Gemini).
Groq, Google, HubSpot and Slack are reached over the standard library.

---

## Documentation

| Read this | For |
|---|---|
| [HACKATHON_PLAN.md](HACKATHON_PLAN.md) | The multi-app plan: the three apps, reliability, proof, demo, cut lines |
| [docs/demo-video-script.md](docs/demo-video-script.md) | The two-minute demo, shot by shot |
| [docs/system-design.md](docs/system-design.md) | Architecture, call flow, data model, every choice and its rejected alternatives |
| [docs/requirements-analysis.md](docs/requirements-analysis.md) | Requirements normalised, open questions, requirement-to-test matrix |
| [docs/audit.md](docs/audit.md) | A critical review of the original plan |
| [docs/g10-provider-inventory.md](docs/g10-provider-inventory.md) | Why Soniox; where vendor docs were wrong and the live API corrected them |
| [docs/model-comparison.md](docs/model-comparison.md) | Classification model choice and pricing |
| [docs/live-test-runbook.md](docs/live-test-runbook.md) · [docs/deployment.md](docs/deployment.md) | Live testing; hosting on Render |
| [NOTE.md](NOTE.md) | The original short note: what works, what doesn't, what next |
