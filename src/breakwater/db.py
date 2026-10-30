"""All Postgres operations: queue claims, leases, the result ledger, provenance.

Design: at-least-once execution with exactly-once result effect.
  - claim:  SELECT ... FOR UPDATE SKIP LOCKED  (safe concurrent dequeue)
  - lease:  lease_expires_at + heartbeat extension, fenced by worker_id
  - reap:   expired running trials -> requeued (or failed after max_attempts)
  - submit: INSERT ... ON CONFLICT (trial_id) DO NOTHING (one result ever counts)
"""
import json
from importlib import resources

import asyncpg

from .config import CFG


async def _init_conn(conn: asyncpg.Connection) -> None:
    for t in ("jsonb", "json"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def make_pool(dsn: str | None = None, min_size: int = 1, max_size: int = 8) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn or CFG.dsn, min_size=min_size, max_size=max_size, init=_init_conn)


async def record_event(conn, kind: str, *, experiment_id=None, trial_id=None, worker_id=None, payload=None):
    await conn.execute(
        "INSERT INTO events (experiment_id, trial_id, worker_id, kind, payload) VALUES ($1,$2,$3,$4,$5)",
        experiment_id, trial_id, worker_id, kind, payload or {})


# ---------------------------------------------------------------- schema

def schema_sql() -> str:
    """The schema ships inside the package, so an installed wheel can migrate."""
    return resources.files("breakwater").joinpath("schema.sql").read_text()


async def apply_schema(conn: asyncpg.Connection) -> None:
    """Idempotent; serialized by an advisory lock because concurrent
    CREATE ... IF NOT EXISTS can still race on the catalog."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('breakwater.schema'))")
        await conn.execute(schema_sql())


# ---------------------------------------------------------------- experiments

async def create_experiment(pool, experiment_id: str, name: str, spec: dict, trials: list[dict]) -> int:
    """Insert experiment + its trials atomically. trial dicts: {trial_id, task_id, replicate, spec}."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO experiments (experiment_id, name, spec) VALUES ($1,$2,$3)",
                experiment_id, name, spec)
            await conn.executemany(
                """INSERT INTO trials (trial_id, experiment_id, task_id, replicate, spec, max_attempts)
                   VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (trial_id) DO NOTHING""",
                [(t["trial_id"], experiment_id, t["task_id"], t["replicate"], t["spec"], CFG.max_attempts)
                 for t in trials])
            await record_event(conn, "experiment_created", experiment_id=experiment_id,
                               payload={"n_trials": len(trials)})
        return len(trials)


# ---------------------------------------------------------------- worker side

CLAIM_SQL = """
WITH next AS (
    SELECT trial_id FROM trials
    WHERE state = 'queued'
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE trials t
SET state = 'running',
    worker_id = $1,
    lease_expires_at = now() + make_interval(secs => $2),
    attempts = attempts + 1,
    first_started_at = COALESCE(first_started_at, now())
FROM next
WHERE t.trial_id = next.trial_id
RETURNING t.trial_id, t.experiment_id, t.task_id, t.replicate, t.spec, t.attempts, t.max_attempts
"""


async def claim_trial(pool, worker_id: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(CLAIM_SQL, worker_id, CFG.lease_seconds)
        if row:
            await record_event(conn, "claimed", experiment_id=row["experiment_id"],
                               trial_id=row["trial_id"], worker_id=worker_id,
                               payload={"attempt": row["attempts"]})
        return row


async def heartbeat_trial(pool, trial_id: str, worker_id: str) -> bool:
    """Extend lease. Returns False if the lease was lost (reaped and possibly re-claimed):
    the fence — a zombie must not extend a lease it no longer owns."""
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """UPDATE trials SET lease_expires_at = now() + make_interval(secs => $1)
               WHERE trial_id = $2 AND worker_id = $3 AND state = 'running'""",
            CFG.lease_seconds, trial_id, worker_id)
        return tag.endswith("1")


async def submit_result(pool, *, trial_id, experiment_id, task_id, outcome, detail,
                        exec_ms, worker_id, attempt, artifacts) -> bool:
    """Exactly-once result effect. Returns True if this submission won, False if a
    result already existed (duplicate execution -> suppressed, logged for metrics)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            won = await conn.fetchval(
                """INSERT INTO results (trial_id, experiment_id, task_id, outcome, detail,
                                        exec_ms, worker_id, attempt, artifacts)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (trial_id) DO NOTHING
                   RETURNING trial_id""",
                trial_id, experiment_id, task_id, outcome, detail, exec_ms, worker_id, attempt, artifacts)
            if won:
                await conn.execute(
                    """UPDATE trials SET state='completed', finished_at=now(),
                       worker_id=$2, lease_expires_at=NULL WHERE trial_id=$1""",
                    trial_id, worker_id)
                await record_event(conn, "result_accepted", experiment_id=experiment_id,
                                   trial_id=trial_id, worker_id=worker_id,
                                   payload={"outcome": outcome, "attempt": attempt})
                return True
            await record_event(conn, "duplicate_suppressed", experiment_id=experiment_id,
                               trial_id=trial_id, worker_id=worker_id,
                               payload={"outcome": outcome, "attempt": attempt})
            return False


async def release_trial(pool, trial_id: str, worker_id: str, reason: str):
    """Worker hit an execution error: hand the trial back immediately (don't wait for lease expiry)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE trials
                   SET state = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                       worker_id = NULL, lease_expires_at = NULL, last_error = $3,
                       finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE finished_at END
                   WHERE trial_id = $1 AND worker_id = $2 AND state = 'running'
                   RETURNING experiment_id, state, attempts""",
                trial_id, worker_id, reason)
            if row:
                await record_event(conn, "released", experiment_id=row["experiment_id"],
                                   trial_id=trial_id, worker_id=worker_id,
                                   payload={"reason": reason, "new_state": row["state"]})


async def register_worker(pool, worker_id: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO workers (worker_id) VALUES ($1)
               ON CONFLICT (worker_id) DO UPDATE SET last_heartbeat = now(), state = 'alive'""",
            worker_id)
        await record_event(conn, "worker_started", worker_id=worker_id)


async def heartbeat_worker(pool, worker_id: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE workers SET last_heartbeat = now(), state = 'alive' WHERE worker_id = $1", worker_id)


# ---------------------------------------------------------------- reaper

REAP_SQL = """
WITH expired AS (
    SELECT trial_id, worker_id FROM trials
    WHERE state = 'running' AND lease_expires_at < now()
    FOR UPDATE SKIP LOCKED
)
UPDATE trials t
SET state = CASE WHEN t.attempts >= t.max_attempts THEN 'failed' ELSE 'queued' END,
    worker_id = NULL, lease_expires_at = NULL,
    last_error = format('lease expired: worker %s, attempt %s/%s',
                        expired.worker_id, t.attempts, t.max_attempts),
    finished_at = CASE WHEN t.attempts >= t.max_attempts THEN now() ELSE t.finished_at END
FROM expired
WHERE t.trial_id = expired.trial_id
RETURNING t.trial_id, t.experiment_id, t.state, t.attempts, expired.worker_id AS lost_worker
"""


async def reap_expired_leases(pool) -> list:
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(REAP_SQL)
            for r in rows:
                kind = "requeued" if r["state"] == "queued" else "exhausted"
                await record_event(conn, kind, experiment_id=r["experiment_id"],
                                   trial_id=r["trial_id"], worker_id=r["lost_worker"],
                                   payload={"attempts": r["attempts"]})
            return rows


async def mark_dead_workers(pool, dead_after_seconds: float) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """UPDATE workers SET state = 'dead'
               WHERE state = 'alive' AND last_heartbeat < now() - make_interval(secs => $1)
               RETURNING worker_id""",
            dead_after_seconds)
        for r in rows:
            await record_event(conn, "worker_dead", worker_id=r["worker_id"])
        return rows
