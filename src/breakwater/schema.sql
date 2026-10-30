-- Breakwater schema: Postgres is the queue, lease registry, and result ledger.
--
-- Idempotent by construction, so `breakwater.setup` can re-apply it to any
-- existing database and upgrade it in place:
--   * every object is CREATE ... IF NOT EXISTS
--   * every column added after v1 is ALSO listed in the "upgrades" section as
--     ALTER TABLE ... ADD COLUMN IF NOT EXISTS (a no-op on fresh databases)
-- tests/test_queue.py checks that a v1 database upgraded with this file ends
-- up column-for-column identical to a fresh one.

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id   TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    spec            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    priority        INT NOT NULL DEFAULT 0,      -- higher is claimed first
    max_attempts    INT,                         -- as requested at creation (copied to trials)
    last_claimed_at TIMESTAMPTZ                  -- round-robin tiebreak for the fair claim
);

CREATE TABLE IF NOT EXISTS trials (
    trial_id         TEXT PRIMARY KEY,          -- deterministic: {exp}/{task}/r{replicate}
    experiment_id    TEXT NOT NULL REFERENCES experiments(experiment_id),
    task_id          TEXT NOT NULL,
    replicate        INT  NOT NULL DEFAULT 0,
    spec             JSONB NOT NULL DEFAULT '{}',
    state            TEXT NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued','running','completed','failed')),
    attempts         INT NOT NULL DEFAULT 0,
    max_attempts     INT NOT NULL DEFAULT 5,
    worker_id        TEXT,
    lease_expires_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    first_started_at TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    priority         INT NOT NULL DEFAULT 0,    -- copied from the experiment; what the claim reads
    -- Fencing token: +1 on every claim, never reset (unlike `attempts`, which is
    -- a retry budget). Heartbeat/release must present the epoch they were granted.
    lease_epoch      BIGINT NOT NULL DEFAULT 0,
    last_error       TEXT                       -- why the last attempt ended without a result
);

-- Results ledger. PRIMARY KEY (trial_id) + INSERT ON CONFLICT DO NOTHING is the
-- exactly-once-effect mechanism: execution may happen N times, one row wins.
CREATE TABLE IF NOT EXISTS results (
    trial_id         TEXT PRIMARY KEY REFERENCES trials(trial_id),
    experiment_id    TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    outcome          TEXT NOT NULL CHECK (outcome IN ('pass','fail','error')),
    detail           JSONB NOT NULL DEFAULT '{}',
    exec_ms          DOUBLE PRECISION,
    worker_id        TEXT NOT NULL,
    attempt          INT NOT NULL,
    artifacts        JSONB NOT NULL DEFAULT '[]',
    submitted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_epoch      BIGINT,                    -- epoch of the winning execution (NULL: pre-epoch rows)
    stale_submission BOOLEAN NOT NULL DEFAULT false  -- winner's lease had been superseded/revoked
);

-- Append-only provenance log.
CREATE TABLE IF NOT EXISTS events (
    event_id      BIGSERIAL PRIMARY KEY,
    experiment_id TEXT,
    trial_id      TEXT,
    worker_id     TEXT,
    kind          TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id      TEXT PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
    state          TEXT NOT NULL DEFAULT 'alive'
                   CHECK (state IN ('alive','dead','stopped'))
);

-- ---------------------------------------------------------------- upgrades
-- Columns added after v1. Postgres >= 11 adds a column with a constant default
-- without rewriting the table, so this is cheap on a large existing database.

ALTER TABLE experiments ADD COLUMN IF NOT EXISTS priority        INT NOT NULL DEFAULT 0;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS max_attempts    INT;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS last_claimed_at TIMESTAMPTZ;

ALTER TABLE trials ADD COLUMN IF NOT EXISTS priority    INT NOT NULL DEFAULT 0;
ALTER TABLE trials ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0;
ALTER TABLE trials ADD COLUMN IF NOT EXISTS last_error  TEXT;
-- Claim order within an experiment is (created_at, trial_id). now() is the
-- transaction start, so every trial of one insert batch tied and the order
-- fell back to trial_id; clock_timestamp() keeps insertion order, which is
-- what lets a client interleave arms (e.g. base/head runs of a Trust Diff).
ALTER TABLE trials ALTER COLUMN created_at SET DEFAULT clock_timestamp();

ALTER TABLE results ADD COLUMN IF NOT EXISTS lease_epoch      BIGINT;
ALTER TABLE results ADD COLUMN IF NOT EXISTS stale_submission BOOLEAN NOT NULL DEFAULT false;

-- ---------------------------------------------------------------- indexes

-- v1 FIFO claim index (state, created_at): superseded by idx_trials_fair.
DROP INDEX IF EXISTS idx_trials_claim;
-- Fair claim. Serves both halves of db.claim_trial: the skip-scan that lists
-- (priority, experiment) pairs with queued work, and the per-experiment
-- "oldest queued trial" pick. Partial, so it only ever holds the queue itself.
CREATE INDEX IF NOT EXISTS idx_trials_fair    ON trials (priority, experiment_id, created_at, trial_id)
    WHERE state = 'queued';
-- Running trials per experiment (fair-share input; bounded by fleet size).
CREATE INDEX IF NOT EXISTS idx_trials_running ON trials (experiment_id) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS idx_trials_lease   ON trials (lease_expires_at) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS idx_trials_exp     ON trials (experiment_id);
CREATE INDEX IF NOT EXISTS idx_results_exp    ON results (experiment_id);
CREATE INDEX IF NOT EXISTS idx_events_exp     ON events (experiment_id, event_id);
-- Per-trial provenance lookups (invariant checks, "recovered by" report).
CREATE INDEX IF NOT EXISTS idx_events_trial   ON events (trial_id, kind);
