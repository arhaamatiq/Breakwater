"""All Postgres operations: queue claims, leases, the result ledger, provenance.

Design: at-least-once execution with exactly-once result effect.
  - claim:  SELECT ... FOR UPDATE SKIP LOCKED  (safe concurrent dequeue)
  - lease:  lease_expires_at + heartbeat extension, fenced by (worker_id, lease_epoch)
  - reap:   expired running trials -> requeued (or failed after max_attempts)
  - submit: INSERT ... ON CONFLICT (trial_id) DO NOTHING (one result ever counts)

Fencing. `trials.lease_epoch` is a fencing token: every claim increments it and
hands the new value to the claimer. Heartbeat and release only act when the
caller presents the trial's *current* epoch. worker_id alone is not a fence: a
worker that stalls, loses its lease, restarts under the same id and re-claims
the same trial would otherwise let its old incarnation extend or release the
new lease. Result submission is deliberately NOT fenced (a finished execution's
result is valid whoever holds the lease now); the ledger arbitrates, and a
submission from a superseded or revoked lease is recorded as `stale_submission`.

Every state change and its provenance event commit in the same transaction, so
the event log can be reconciled against the tables (report.verify_invariants).
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

async def create_experiment(pool, experiment_id: str, name: str, spec: dict, trials: list[dict],
                            max_attempts: int | None = None) -> int:
    """Insert experiment + its trials atomically. trial dicts: {trial_id, task_id, replicate, spec}.
    max_attempts: claims before a trial is marked failed; defaults to BW_MAX_ATTEMPTS."""
    max_attempts = max_attempts or CFG.max_attempts
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO experiments (experiment_id, name, spec, max_attempts)
                   VALUES ($1,$2,$3,$4)""",
                experiment_id, name, spec, max_attempts)
            await conn.executemany(
                """INSERT INTO trials (trial_id, experiment_id, task_id, replicate, spec,
                                       max_attempts)
                   VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (trial_id) DO NOTHING""",
                [(t["trial_id"], experiment_id, t["task_id"], t["replicate"], t["spec"],
                  max_attempts)
                 for t in trials])
            await record_event(conn, "experiment_created", experiment_id=experiment_id,
                               payload={"n_trials": len(trials), "max_attempts": max_attempts})
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
    attempts = t.attempts + 1,
    lease_epoch = t.lease_epoch + 1,
    first_started_at = COALESCE(t.first_started_at, now())
FROM next
WHERE t.trial_id = next.trial_id
RETURNING t.trial_id, t.experiment_id, t.task_id, t.replicate, t.spec, t.attempts,
          t.max_attempts, t.lease_epoch
"""


async def claim_trial(pool, worker_id: str, lease_seconds: float | None = None):
    """Claim the oldest queued trial. Returns the trial row (including the
    `lease_epoch` fencing token the caller must present later) or None.
    The claim and its event commit together, so the log can be reconciled."""
    lease = CFG.lease_seconds if lease_seconds is None else lease_seconds
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(CLAIM_SQL, worker_id, lease)
            if row:
                await record_event(conn, "claimed", experiment_id=row["experiment_id"],
                                   trial_id=row["trial_id"], worker_id=worker_id,
                                   payload={"attempt": row["attempts"],
                                            "epoch": row["lease_epoch"]})
            return row


async def heartbeat_trial(pool, trial_id: str, worker_id: str, lease_epoch: int,
                          lease_seconds: float | None = None) -> bool:
    """Extend the lease. False means the lease is gone (reaped, re-claimed — possibly
    by this same worker id under a newer epoch — or the trial already completed)."""
    lease = CFG.lease_seconds if lease_seconds is None else lease_seconds
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """UPDATE trials SET lease_expires_at = now() + make_interval(secs => $1)
               WHERE trial_id = $2 AND worker_id = $3 AND lease_epoch = $4
                 AND state = 'running'""",
            lease, trial_id, worker_id, lease_epoch)
        return tag == "UPDATE 1"


async def submit_result(pool, *, trial_id, experiment_id, task_id, outcome, detail,
                        exec_ms, worker_id, attempt, lease_epoch, artifacts) -> bool:
    """Exactly-once result effect. Returns True if this submission won, False if a
    result already existed (duplicate execution -> suppressed, logged for metrics).

    Not fenced: first writer wins regardless of epoch. The submission is *stale*
    if its lease was superseded by a newer claim (epoch mismatch) or revoked and
    not re-granted (trial back in queued/failed). Stale winners are flagged on the
    result row; every stale submission, won or suppressed, gets an event."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the trial first: serializes against claim/reap/release on this
            # trial, so the staleness verdict and the state change are consistent.
            cur = await conn.fetchrow(
                "SELECT state, lease_epoch FROM trials WHERE trial_id = $1 FOR UPDATE", trial_id)
            if cur is None:
                raise ValueError(f"unknown trial {trial_id!r}")
            stale = lease_epoch != cur["lease_epoch"] or cur["state"] in ("queued", "failed")
            won = await conn.fetchval(
                """INSERT INTO results (trial_id, experiment_id, task_id, outcome, detail,
                                        exec_ms, worker_id, attempt, artifacts,
                                        lease_epoch, stale_submission)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (trial_id) DO NOTHING
                   RETURNING trial_id""",
                trial_id, experiment_id, task_id, outcome, detail, exec_ms, worker_id, attempt,
                artifacts, lease_epoch, stale)
            ids = dict(experiment_id=experiment_id, trial_id=trial_id, worker_id=worker_id)
            payload = {"outcome": outcome, "attempt": attempt, "epoch": lease_epoch}
            if won:
                await conn.execute(
                    """UPDATE trials SET state='completed', finished_at=now(),
                       worker_id=$2, lease_expires_at=NULL WHERE trial_id=$1""",
                    trial_id, worker_id)
                await record_event(conn, "result_accepted", **ids,
                                   payload={**payload, "stale": stale})
            else:
                await record_event(conn, "duplicate_suppressed", **ids, payload=payload)
            if stale:
                await record_event(conn, "stale_submission", **ids, payload={
                    **payload, "accepted": bool(won), "current_epoch": cur["lease_epoch"],
                    "trial_state": cur["state"]})
            return bool(won)


async def release_trial(pool, trial_id: str, worker_id: str, lease_epoch: int, reason: str) -> bool:
    """Worker hit an execution error: hand the trial back immediately (don't wait for
    lease expiry). Fenced like heartbeat; returns False if the lease was already gone."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """UPDATE trials
                   SET state = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                       worker_id = NULL, lease_expires_at = NULL, last_error = $4,
                       finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE finished_at END
                   WHERE trial_id = $1 AND worker_id = $2 AND lease_epoch = $3 AND state = 'running'
                   RETURNING experiment_id, state, attempts""",
                trial_id, worker_id, lease_epoch, reason)
            if row:
                await record_event(conn, "released", experiment_id=row["experiment_id"],
                                   trial_id=trial_id, worker_id=worker_id,
                                   payload={"reason": reason, "new_state": row["state"],
                                            "epoch": lease_epoch})
            return row is not None


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
    last_error = format('lease expired: worker %s, epoch %s, attempt %s/%s',
                        expired.worker_id, t.lease_epoch, t.attempts, t.max_attempts),
    finished_at = CASE WHEN t.attempts >= t.max_attempts THEN now() ELSE t.finished_at END
FROM expired
WHERE t.trial_id = expired.trial_id
RETURNING t.trial_id, t.experiment_id, t.state, t.attempts, t.lease_epoch,
          expired.worker_id AS lost_worker
"""


async def reap_expired_leases(pool) -> list:
    """Requeue (or fail, once max_attempts is spent) trials whose lease expired.
    The epoch is left alone: the next claim bumps it, which is what fences out
    the worker that lost this lease."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(REAP_SQL)
            for r in rows:
                kind = "requeued" if r["state"] == "queued" else "exhausted"
                await record_event(conn, kind, experiment_id=r["experiment_id"],
                                   trial_id=r["trial_id"], worker_id=r["lost_worker"],
                                   payload={"attempts": r["attempts"], "epoch": r["lease_epoch"]})
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
