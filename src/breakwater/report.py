"""Experiment metrics + correctness invariants, computed straight from Postgres."""

# The checks reconcile three independent records of the same history: the
# trials table (state machine), the results ledger, and the append-only event
# log, which every state change writes in the same transaction. They run in one
# REPEATABLE READ snapshot, so they are exact even while workers are running.
#
# Safety checks: violation counts that must be 0 at EVERY instant, mid-run included.
#   completed_without_result      `completed` is never set without a result row
#   result_on_unfinished_trial    a result row always marks its trial completed
#   duplicate_result_rows         at most one result per trial (the ledger PK; a
#                                 schema regression would show up here)
#   results_without_accepted_event  each result row has the result_accepted event
#                                 its own transaction wrote (same worker + epoch)
#   accepted_events_mismatch      #result_accepted events == #results, so no
#                                 submission was told "won" without a row to show
#   results_without_claim         each winning (worker, epoch) was actually issued
#                                 by a claim: no result from an unleased execution
#   double_claims                 no (trial, epoch) was handed out twice: the
#                                 SKIP LOCKED claim never double-claimed
#   epoch_mismatch                trials.lease_epoch equals the newest epoch any
#                                 claim issued (fencing tokens are monotonic)
#   executions_below_results      executions started (claims) >= results
# Liveness check: must be 0 once the experiment has finished.
#   lost_results                  trials with neither a result row nor `failed`
#
# What they do NOT prove: exactly-once *execution*. A trial may run several
# times (crash, stall, double submit); only its effect on the ledger is counted
# once. Side effects outside the ledger (artifact uploads, container work) are
# at-least-once by design. They cannot see a submission that never reached
# Postgres, they do not show that the *right* execution won (any finished
# execution's result is accepted), and they say nothing about whether a
# verdict is correct.
_TRIAL_CHECKS = """
SELECT count(*) AS trials,
       count(*) FILTER (WHERE t.state IN ('queued','running'))         AS unfinished,
       count(*) FILTER (WHERE t.state = 'failed')                      AS failed,
       count(r.trial_id)                                               AS results,
       count(*) FILTER (WHERE r.stale_submission)                      AS stale_results_accepted,
       count(*) FILTER (WHERE r.trial_id IS NULL AND t.state <> 'failed') AS lost_results,
       count(*) FILTER (WHERE r.trial_id IS NULL AND t.state = 'completed') AS completed_without_result,
       count(*) FILTER (WHERE r.trial_id IS NOT NULL AND t.state <> 'completed') AS result_on_unfinished_trial,
       COALESCE(sum(t.attempts), 0)                                    AS attempts_total
FROM trials t LEFT JOIN results r USING (trial_id)
WHERE t.experiment_id = $1
"""

_EVENT_CHECKS = """
WITH ev AS (
    SELECT trial_id, kind, (payload->>'epoch')::bigint AS epoch
    FROM events WHERE experiment_id = $1
      AND kind IN ('claimed','result_accepted','duplicate_suppressed','stale_submission')
), res AS (
    SELECT r.* FROM results r JOIN trials t USING (trial_id) WHERE t.experiment_id = $1
)
-- correlated lookups go to `events` itself (idx_events_trial), not the CTE
SELECT
  (SELECT count(*) FROM ev WHERE kind = 'claimed')              AS executions_started,
  (SELECT count(*) FROM ev WHERE kind = 'result_accepted')      AS accepted_events,
  (SELECT count(*) FROM ev WHERE kind = 'duplicate_suppressed') AS suppressed_events,
  (SELECT count(*) FROM ev WHERE kind = 'stale_submission')     AS stale_submission_events,
  (SELECT count(*) FROM (SELECT trial_id FROM res GROUP BY trial_id HAVING count(*) > 1) d)
                                                                AS duplicate_result_rows,
  (SELECT count(*) FROM res r WHERE NOT EXISTS (
      SELECT 1 FROM events e WHERE e.trial_id = r.trial_id AND e.kind = 'result_accepted'
        AND e.worker_id = r.worker_id
        AND (e.payload->>'epoch')::bigint IS NOT DISTINCT FROM r.lease_epoch))
                                                                AS results_without_accepted_event,
  (SELECT count(*) FROM res r WHERE NOT EXISTS (
      SELECT 1 FROM events e WHERE e.trial_id = r.trial_id AND e.kind = 'claimed'
        AND e.worker_id = r.worker_id
        AND (e.payload->>'epoch')::bigint IS NOT DISTINCT FROM r.lease_epoch))
                                                                AS results_without_claim,
  (SELECT count(*) FROM (SELECT 1 FROM ev WHERE kind = 'claimed' AND epoch IS NOT NULL
                         GROUP BY trial_id, epoch HAVING count(*) > 1) d)
                                                                AS double_claims,
  (SELECT count(*) FROM trials t WHERE t.experiment_id = $1
      AND NULLIF(t.lease_epoch, 0) IS DISTINCT FROM (
          SELECT max((e.payload->>'epoch')::bigint) FROM events e
          WHERE e.trial_id = t.trial_id AND e.kind = 'claimed'))
                                                                AS epoch_mismatch
"""

SAFETY_CHECKS = ("completed_without_result", "result_on_unfinished_trial",
                 "duplicate_result_rows", "results_without_accepted_event",
                 "accepted_events_mismatch", "results_without_claim", "double_claims",
                 "epoch_mismatch", "executions_below_results")


async def verify_invariants(pool, experiment_id: str) -> dict:
    """Exactly-once *accounting* checks for one experiment, all by SQL (see above).
    Returns the raw counts plus `violations` (non-zero safety checks) and
    `ok` = finished and no violations and no lost results. Rows written before
    fencing epochs existed carry no epoch and are matched as such."""
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            t = dict(await conn.fetchrow(_TRIAL_CHECKS, experiment_id))
            e = dict(await conn.fetchrow(_EVENT_CHECKS, experiment_id))
    out = {**t, **e}
    out["accepted_events_mismatch"] = abs(e["accepted_events"] - t["results"])
    out["executions_below_results"] = max(0, t["results"] - e["executions_started"])
    out["violations"] = {k: out[k] for k in SAFETY_CHECKS if out[k]}
    out["experiment_finished"] = t["unfinished"] == 0
    out["ok"] = (out["experiment_finished"] and out["lost_results"] == 0
                 and not out["violations"])
    return out


async def compute_report(pool, experiment_id: str) -> dict:
    async with pool.acquire() as conn:
        states = {r["state"]: r["n"] for r in await conn.fetch(
            "SELECT state, count(*) AS n FROM trials WHERE experiment_id=$1 GROUP BY state",
            experiment_id)}
        n_trials = sum(states.values())

        res = await conn.fetchrow(
            """SELECT count(*) AS n_results,
                      count(*) FILTER (WHERE outcome='pass') AS n_pass,
                      count(*) FILTER (WHERE outcome='fail') AS n_fail,
                      count(*) FILTER (WHERE outcome='error') AS n_error,
                      percentile_cont(0.5)  WITHIN GROUP (ORDER BY exec_ms) AS exec_p50,
                      percentile_cont(0.99) WITHIN GROUP (ORDER BY exec_ms) AS exec_p99,
                      min(submitted_at) AS first_result,
                      max(submitted_at) AS last_result
               FROM results WHERE experiment_id=$1""", experiment_id)

        e2e = await conn.fetchrow(
            """SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM finished_at - created_at)) AS p50,
                      percentile_cont(0.99) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM finished_at - created_at)) AS p99
               FROM trials WHERE experiment_id=$1 AND state='completed'""", experiment_id)

        window = await conn.fetchrow(
            """SELECT EXTRACT(EPOCH FROM max(r.submitted_at) - min(t.first_started_at)) AS seconds
               FROM results r JOIN trials t USING (trial_id)
               WHERE r.experiment_id=$1""", experiment_id)

        attempts = await conn.fetchrow(
            """SELECT COALESCE(sum(attempts),0) AS total_executions,
                      count(*) FILTER (WHERE attempts > 1) AS retried_trials
               FROM trials WHERE experiment_id=$1""", experiment_id)

        ev = {r["kind"]: r["n"] for r in await conn.fetch(
            """SELECT kind, count(*) AS n FROM events
               WHERE experiment_id=$1 AND kind IN ('requeued','duplicate_suppressed','released',
                                                   'exhausted','stale_submission')
               GROUP BY kind""", experiment_id)}

        dead_workers = [r["worker_id"] for r in await conn.fetch(
            "SELECT worker_id FROM workers WHERE state='dead' ORDER BY worker_id")]
        workers_used = [r["worker_id"] for r in await conn.fetch(
            "SELECT DISTINCT worker_id FROM results WHERE experiment_id=$1 ORDER BY worker_id",
            experiment_id)]

        # Trials whose accepted result came from a different worker than first claim
        recovered = await conn.fetchval(
            """SELECT count(*) FROM results r
               WHERE r.experiment_id=$1 AND r.worker_id <> (
                   SELECT e.worker_id FROM events e
                   WHERE e.trial_id = r.trial_id AND e.kind='claimed'
                   ORDER BY e.event_id LIMIT 1)""", experiment_id)

        failure_reasons = {r["last_error"]: r["n"] for r in await conn.fetch(
            """SELECT last_error, count(*) AS n FROM trials
               WHERE experiment_id=$1 AND state='failed' GROUP BY last_error""", experiment_id)}

    inv = await verify_invariants(pool, experiment_id)
    seconds = float(window["seconds"]) if window and window["seconds"] else None
    return {
        "experiment_id": experiment_id,
        "trials": {"total": n_trials, **states},
        "results": {"total": res["n_results"], "pass": res["n_pass"], "fail": res["n_fail"],
                    "error": res["n_error"]},
        "throughput_trials_per_sec": round(res["n_results"] / seconds, 3) if seconds else None,
        "wall_clock_seconds": round(seconds, 2) if seconds else None,
        "latency_ms": {
            "exec_p50": round(res["exec_p50"], 1) if res["exec_p50"] else None,
            "exec_p99": round(res["exec_p99"], 1) if res["exec_p99"] else None,
            "e2e_p50_s": round(float(e2e["p50"]), 2) if e2e["p50"] else None,
            "e2e_p99_s": round(float(e2e["p99"]), 2) if e2e["p99"] else None,
        },
        "fault_tolerance": {
            "total_executions_started": attempts["total_executions"],
            "retried_trials": attempts["retried_trials"],
            "requeues_after_lease_expiry": ev.get("requeued", 0),
            "releases_after_exec_error": ev.get("released", 0),
            "duplicate_submissions_suppressed": ev.get("duplicate_suppressed", 0),
            "stale_submissions": ev.get("stale_submission", 0),
            "stale_results_accepted": inv["stale_results_accepted"],
            "trials_recovered_by_different_worker": recovered,
            "workers_used": workers_used,
            "workers_died": dead_workers,
            "failure_reasons": failure_reasons,
        },
        "invariants": {
            # legacy keys (demo, reliability, README)
            "experiment_finished": inv["experiment_finished"],
            "lost_results": inv["lost_results"],
            "duplicate_result_rows": inv["duplicate_result_rows"],
            "completed_trials_without_result": inv["completed_without_result"],
            "infra_failed_trials": inv["failed"],
            "zero_lost": inv["lost_results"] == 0,
            "zero_duplicates": inv["duplicate_result_rows"] == 0,
            # full reconciliation (report.verify_invariants)
            **{k: inv[k] for k in SAFETY_CHECKS},
            "executions_started": inv["executions_started"],
            "accepted_events": inv["accepted_events"],
            "violations": inv["violations"],
            "ok": inv["ok"],
        },
    }
