"""Independent full-mode replication runs for three follow-up checks.

Every trial here runs mode="full": a fresh solve container (reference solve.sh,
optionally with one mechanical edit) is built, committed, its declared artifacts
extracted, and a fresh verify container runs the task's own tests/test.sh. No
verify_only replay, no artifact reuse between runs.

Trial ids are namespaced `rep/<stamp>/...` and carry the run stamp, so each
invocation produces genuinely new trials rather than deduping against the
ablation sweep's deterministic `mut/...` ids.
"""
import argparse
import asyncio
import ast
import json
import os
import time

from projectx import db
from projectx.adapters import harbor
from projectx.demo import start_worker
from projectx.tester.mutate import mutants as ast_mutants


def _spread(seq, k):
    if len(seq) <= k:
        return list(seq)
    step = len(seq) / k
    return [seq[int(i * step)] for i in range(k)]


def byte_stops_mutant_source(task_dir):
    """The exact surviving candidate: `start >= 0` -> `start >= 1`, i.e. the
    operator id ast::byte_stops.py::int+1(0)#1 from the sweep."""
    src = open(os.path.join(task_dir, "solution", "byte_stops.py")).read()
    sel = _spread(ast_mutants(src, limit=60), 8)
    m = sel[1]
    assert m["op"] == "int+1(0)", f"unexpected operator at index 1: {m['op']}"
    assert "if start >= 1:" in m["src"], "mutant is not the >=0 -> >=1 edit"
    return m["src"]


def solve_with_file_override(ref_solve: str, dest_path: str, new_src: str,
                             before_marker: str) -> str:
    """Reference solve.sh with one file overwritten just before the line that
    contains `before_marker`, so the task runs with the mutated file in place."""
    heredoc = (f"cat > {dest_path} <<'__PX_MUT__'\n{new_src}\n__PX_MUT__\n")
    assert "__PX_MUT__" not in new_src
    out, done = [], False
    for line in ref_solve.splitlines(keepends=True):
        if not done and before_marker in line:
            out.append(heredoc)
            done = True
        out.append(line)
    assert done, f"marker {before_marker!r} not found in solve.sh"
    return "".join(out)


def solve_with_trailing(ref_solve: str, extra: str) -> str:
    return ref_solve.rstrip("\n") + "\n" + extra + "\n"


async def main_async(args):
    ids = ["distributed-dedup", "risk-scorer-replay", "batched-eval-parity"]
    tasks = harbor.load(args.root, ids=ids)
    print("[rep] loaded:", sorted(tasks), flush=True)

    stamp = args.stamp
    trials, plan = [], {}

    def add(label, tid, spec_extra, n):
        t = tasks[tid]
        for i in range(n):
            trial_id = f"rep/{stamp}/{label}/run{i}"
            trials.append({"trial_id": trial_id, "task_id": tid, "replicate": i,
                           "spec": {"mode": "full", "task_blob": t, **spec_extra}})
            plan.setdefault(label, []).append(trial_id)

    # ---- check 1: distributed-dedup reference, fresh independent run
    dd = tasks["hb_distributed_dedup"]
    add("c1_dd_reference", "hb_distributed_dedup", {"solution": "oracle"},
        args.dd_runs)

    # ---- check 2: batched-eval-parity byte_stops candidate, full mode x5
    bep_dir = os.path.join(args.root, "batched-eval-parity")
    bep = tasks["hb_batched_eval_parity"]
    mut_src = byte_stops_mutant_source(bep_dir)
    bep_solve = solve_with_file_override(
        bep["oracle"], "/app/evalbench/byte_stops.py", mut_src, "evaluate.py")
    add("c2_bep_byte_stops_mutant", "hb_batched_eval_parity",
        {"solution_source": bep_solve}, args.bep_runs)
    add("c2_bep_reference", "hb_batched_eval_parity", {"solution": "oracle"},
        args.bep_ref_runs)

    # ---- check 3: risk-scorer-replay, drop each declared output file
    rsr = tasks["hb_risk_scorer_replay"]
    for short, path in (("parity_scores_csv", "/app/output/parity_scores.csv"),
                        ("scorer_audit_sqlite", "/app/output/scorer_audit.sqlite")):
        add(f"c3_rsr_drop_{short}", "hb_risk_scorer_replay",
            {"solution_source": solve_with_trailing(rsr["oracle"],
                                                    f"rm -f {path}")}, args.rsr_runs)
    add("c3_rsr_drop_both", "hb_risk_scorer_replay",
        {"solution_source": solve_with_trailing(
            rsr["oracle"], "rm -f /app/output/parity_scores.csv "
                           "/app/output/scorer_audit.sqlite")}, args.rsr_runs)
    add("c3_rsr_drop_whole_output_dir", "hb_risk_scorer_replay",
        {"solution_source": solve_with_trailing(rsr["oracle"],
                                                "rm -rf /app/output")}, args.rsr_runs)
    add("c3_rsr_reference", "hb_risk_scorer_replay", {"solution": "oracle"},
        args.rsr_runs)

    print(f"[rep] submitting {len(trials)} full-mode trials", flush=True)
    for lbl, tids in sorted(plan.items()):
        print(f"       {lbl:34} x{len(tids)}", flush=True)

    os.makedirs("logs", exist_ok=True)
    procs = [start_worker(f"rep-w{i}") for i in range(1, args.workers + 1)]
    pool = await db.make_pool(max_size=10)

    async def reaper():
        while True:
            try:
                await db.reap_expired_leases(pool)
            except Exception:
                pass
            await asyncio.sleep(1.0)
    rp = asyncio.create_task(reaper())
    try:
        await db.create_experiment(pool, f"rep-{stamp}", "replication",
                                   {"checks": sorted(plan)}, trials)
        all_ids = [t["trial_id"] for t in trials]
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            pend = await pool.fetchval(
                "SELECT count(*) FROM trials WHERE trial_id = ANY($1::text[]) "
                "AND state IN ('queued','running')", all_ids)
            if pend == 0:
                break
            print(f"[rep] {pend} pending...", flush=True)
            await asyncio.sleep(20)
        rows = await pool.fetch(
            """SELECT t.trial_id, t.state, t.attempts, r.outcome, r.detail,
                      r.artifacts, r.exec_ms
               FROM trials t LEFT JOIN results r USING (trial_id)
               WHERE t.trial_id = ANY($1::text[])""", all_ids)
    finally:
        rp.cancel()
        await pool.close()
        for p in procs:
            if p.poll() is None:
                p.terminate()

    by = {r["trial_id"]: dict(r) for r in rows}
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "stamp": stamp,
           "mode": "full (fresh solve + fresh verify per run)", "checks": {}}
    for lbl, tids in sorted(plan.items()):
        recs = []
        for tid in tids:
            r = by.get(tid) or {}
            det = r.get("detail")
            if isinstance(det, str):
                try:
                    det = json.loads(det)
                except Exception:
                    det = {}
            det = det or {}
            recs.append({"trial_id": tid, "outcome": r.get("outcome"),
                         "trial_state": r.get("state"),
                         "solution_exit_code": det.get("solution_exit_code"),
                         "reward_txt": det.get("reward_txt"),
                         "artifacts_missing": det.get("artifacts_missing"),
                         "exec_ms": r.get("exec_ms"),
                         "cases": det.get("cases"),
                         "storage": r.get("artifacts")})
        n_pass = sum(1 for x in recs if x["outcome"] == "pass")
        n_fail = sum(1 for x in recs if x["outcome"] == "fail")
        n_other = len(recs) - n_pass - n_fail
        out["checks"][lbl] = {"runs": len(recs), "pass": n_pass, "fail": n_fail,
                              "no_verdict": n_other, "records": recs}
        print(f"[rep] {lbl:34} pass={n_pass}/{len(recs)} fail={n_fail} other={n_other}",
              flush=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"[rep] wrote {args.out}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="external/harbor-tb/tasks")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--bep-runs", type=int, default=5)
    ap.add_argument("--bep-ref-runs", type=int, default=1)
    ap.add_argument("--rsr-runs", type=int, default=3)
    ap.add_argument("--dd-runs", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=5400)
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--out", default="reports/mut_run_20260910/replication.json")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
