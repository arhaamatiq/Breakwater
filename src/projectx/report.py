"""Experiment metrics + correctness invariants, computed straight from Postgres."""


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
               WHERE experiment_id=$1 AND kind IN ('requeued','duplicate_suppressed','released','exhausted')
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

        # ---- invariants (verified by query, not assumed) ----
        dup_rows = await conn.fetchval(
            """SELECT count(*) FROM (SELECT trial_id FROM results WHERE experiment_id=$1
                                     GROUP BY trial_id HAVING count(*)>1) d""", experiment_id)
        completed_without_result = await conn.fetchval(
            """SELECT count(*) FROM trials t WHERE t.experiment_id=$1 AND t.state='completed'
               AND NOT EXISTS (SELECT 1 FROM results r WHERE r.trial_id=t.trial_id)""", experiment_id)
        n_failed = states.get("failed", 0)
        n_results = res["n_results"]
        lost = n_trials - n_results - n_failed
        pending = states.get("queued", 0) + states.get("running", 0)

    seconds = float(window["seconds"]) if window and window["seconds"] else None
    return {
        "experiment_id": experiment_id,
        "trials": {"total": n_trials, **states},
        "results": {"total": n_results, "pass": res["n_pass"], "fail": res["n_fail"],
                    "error": res["n_error"]},
        "throughput_trials_per_sec": round(n_results / seconds, 3) if seconds else None,
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
            "trials_recovered_by_different_worker": recovered,
            "workers_used": workers_used,
            "workers_died": dead_workers,
        },
        "invariants": {
            "experiment_finished": pending == 0,
            "lost_results": lost,
            "duplicate_result_rows": dup_rows,
            "completed_trials_without_result": completed_without_result,
            "infra_failed_trials": n_failed,
            "zero_lost": lost == 0,
            "zero_duplicates": dup_rows == 0,
        },
    }
