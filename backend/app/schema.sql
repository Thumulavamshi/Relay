-- ElevateBox voice agent - persistence.
--
-- SQLite locally. Written to stay portable to Postgres: no SQLite-only types,
-- TEXT timestamps in ISO-8601 UTC, explicit column types. Swapping to Postgres
-- means changing the driver and INTEGER PRIMARY KEY -> BIGSERIAL, nothing else.
--
-- Timestamp rule (CLAUDE.md): every stored timestamp is UTC. Asia/Kolkata is
-- applied only when reasoning about or speaking a time.

CREATE TABLE IF NOT EXISTS calls (
    id                TEXT PRIMARY KEY,          -- our correlation id (uuid4)
    provider_call_id  TEXT UNIQUE,               -- Vapi's call id
    status            TEXT NOT NULL,             -- initiating|queued|ringing|in-progress|ended|failed
    direction         TEXT NOT NULL DEFAULT 'outbound',
    destination       TEXT NOT NULL,
    assistant_id      TEXT,
    is_callback_of    TEXT REFERENCES calls(id), -- set when a scheduled callback places this call
    started_at        TEXT,
    answered_at       TEXT,
    ended_at          TEXT,
    ended_reason      TEXT,
    recording_url     TEXT,
    summary           TEXT,
    -- The multi-app layer's own duplicate guards: once an id is here, the next
    -- sync updates that record instead of creating another.
    hubspot_contact_id TEXT,
    hubspot_deal_id    TEXT,
    hubspot_deal_stage TEXT,                     -- only ever moves forward
    hubspot_note_id    TEXT,
    slack_channel      TEXT,
    slack_ts           TEXT,                     -- set = edit the message, never re-post
    gcal_event_id      TEXT,
    gcal_event_link    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- One row per final transcript turn. Partials are not stored; they churn and
-- the understanding lane only ever reasons over finals.
CREATE TABLE IF NOT EXISTS turns (
    id                 INTEGER PRIMARY KEY,
    call_id            TEXT NOT NULL REFERENCES calls(id),
    seq                INTEGER NOT NULL,
    role               TEXT NOT NULL,            -- user|assistant
    text               TEXT NOT NULL,
    language           TEXT,                     -- filled in the multilingual phase
    seconds_from_start REAL,
    created_at         TEXT NOT NULL,
    UNIQUE (call_id, seq)
);

-- Raw provider payloads, kept verbatim. Debugging a live phone call without
-- these is guesswork, and they are the evidence trail for "defend your choices".
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    call_id    TEXT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,                    -- JSON
    created_at TEXT NOT NULL
);

-- Current best value per discovery slot. Overwritten as better information
-- arrives; raw_quote keeps the person's own words for the follow-up message.
CREATE TABLE IF NOT EXISTS slots (
    call_id         TEXT NOT NULL REFERENCES calls(id),
    name            TEXT NOT NULL,               -- products|catalogue_size|timeline|features|budget
    value           TEXT,
    raw_quote       TEXT,
    source_turn_seq INTEGER,
    confidence      REAL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (call_id, name)
);

-- Append-only on purpose. How the read of a lead EVOLVED during the call is
-- worth being able to show, and it makes the mid-call trigger auditable.
CREATE TABLE IF NOT EXISTS classifications (
    id             INTEGER PRIMARY KEY,
    call_id        TEXT NOT NULL REFERENCES calls(id),
    label          TEXT NOT NULL,                -- hot|warm|cold
    confidence     REAL,
    barrier        TEXT,                         -- budget|timing|decision_maker|none
    evidence_quote TEXT,
    at_turn_seq    INTEGER,
    created_at     TEXT NOT NULL
);

-- The action ledger. idempotency_key is UNIQUE, which is what makes the two
-- independent mid-call trigger paths (LLM tool call + async watchdog) safe:
-- whichever fires second is a no-op instead of a second WhatsApp.
CREATE TABLE IF NOT EXISTS actions (
    id                  INTEGER PRIMARY KEY,
    call_id             TEXT NOT NULL REFERENCES calls(id),
    type                TEXT NOT NULL,           -- whatsapp_hot|whatsapp_followup|brochure|...
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,           -- pending|sending|sent|delivered|failed
    trigger_source      TEXT,                    -- tool_call|watchdog|post_call
    payload             TEXT,                    -- JSON
    provider_message_id TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    requested_at        TEXT NOT NULL,
    sent_at             TEXT,
    delivered_at        TEXT
);

-- Callbacks are claimed by a polling worker with a status transition rather than
-- an in-process scheduler (audit R3): an in-process scheduler sharing state across
-- two instances can double-fire, which would mean calling the evaluator twice.
CREATE TABLE IF NOT EXISTS callbacks (
    id              INTEGER PRIMARY KEY,
    call_id         TEXT NOT NULL REFERENCES calls(id),
    spoken_phrase   TEXT,
    resolved_at_utc TEXT NOT NULL,
    resolution_rule TEXT,                        -- which convention resolved a vague phrase
    status          TEXT NOT NULL,               -- pending|claimed|placed|failed|cancelled
    confirmed_aloud INTEGER NOT NULL DEFAULT 0,
    placed_call_id  TEXT REFERENCES calls(id),
    availability    TEXT,                        -- free|busy_confirmed|unchecked|not_configured
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_call        ON turns (call_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_call       ON events (call_id, id);
CREATE INDEX IF NOT EXISTS idx_actions_call      ON actions (call_id);
CREATE INDEX IF NOT EXISTS idx_class_call        ON classifications (call_id, id);
CREATE INDEX IF NOT EXISTS idx_callbacks_due     ON callbacks (status, resolved_at_utc);
CREATE INDEX IF NOT EXISTS idx_calls_provider    ON calls (provider_call_id);
