-- Project X schema: Postgres is the queue, lease registry, and result ledger.

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    spec          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
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
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_started_at TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_trials_claim ON trials (state, created_at) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS idx_trials_lease ON trials (lease_expires_at) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS idx_trials_exp   ON trials (experiment_id);

-- Result ledger. PRIMARY KEY (trial_id) + INSERT ON CONFLICT DO NOTHING is the
-- exactly-once-effect mechanism: execution may happen N times, one row wins.
CREATE TABLE IF NOT EXISTS results (
    trial_id      TEXT PRIMARY KEY REFERENCES trials(trial_id),
    experiment_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('pass','fail','error')),
    detail        JSONB NOT NULL DEFAULT '{}',
    exec_ms       DOUBLE PRECISION,
    worker_id     TEXT NOT NULL,
    attempt       INT NOT NULL,
    artifacts     JSONB NOT NULL DEFAULT '[]',
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_results_exp ON results (experiment_id);

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
CREATE INDEX IF NOT EXISTS idx_events_exp ON events (experiment_id, event_id);

CREATE TABLE IF NOT EXISTS workers (
    worker_id      TEXT PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
    state          TEXT NOT NULL DEFAULT 'alive'
                   CHECK (state IN ('alive','dead','stopped'))
);
