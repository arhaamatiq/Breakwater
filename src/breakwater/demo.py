"""Chaos demo: ~100 Docker-isolated trials across concurrent workers, with two
injected failures mid-run:

  1. SIGKILL one worker (hard crash)          -> lease expiry -> reaper requeue
  2. SIGSTOP another for > lease, then SIGCONT (stall/partition) -> its in-flight
     trials get re-executed elsewhere; when it resumes and submits, the result
     ledger suppresses the duplicates.

Then verifies the invariants (report.verify_invariants, all by SQL): 0 lost
results, 0 duplicate result rows, and the trials table, result ledger and event
log reconcile (every result traces to the claim that produced it, results ==
accepted events, no (trial, epoch) claimed twice).

Run: python -m breakwater.demo [--replicates 8] [--workers 4]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

import httpx

from .config import CFG

PY = sys.executable


def start_worker(worker_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-m", "breakwater.worker", "--id", worker_id],
        stdout=open(f"logs/{worker_id}.log", "w"), stderr=subprocess.STDOUT)


def start_api() -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-m", "breakwater.api"],
        stdout=open("logs/api.log", "w"), stderr=subprocess.STDOUT)


def wait_api(client: httpx.Client, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if client.get("/healthz").status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("API did not come up; see logs/api.log")


def results_count(client: httpx.Client, exp_id: str) -> tuple[int, bool]:
    s = client.get(f"/experiments/{exp_id}").json()
    return s["results"], s["done"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replicates", type=int, default=8)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-chaos", action="store_true")
    args = ap.parse_args()

    os.makedirs("logs", exist_ok=True)
    procs: dict[str, subprocess.Popen] = {}
    api = None
    try:
        api = start_api()
        client = httpx.Client(base_url=CFG.api_url, timeout=10)
        wait_api(client)
        print("== control plane up ==")

        r = client.post("/experiments", json={
            "name": "chaos-demo", "replicates": args.replicates, "solution": "agent"})
        r.raise_for_status()
        exp_id = r.json()["experiment_id"]
        n_trials = r.json()["n_trials"]
        print(f"== experiment {exp_id}: {n_trials} trials enqueued ==")

        t_start = time.time()
        for i in range(1, args.workers + 1):
            procs[f"w{i}"] = start_worker(f"w{i}")
        print(f"== {args.workers} workers started ==")

        killed_at = stalled_at = conted_at = None
        kill_target, stall_target = "w1", "w2"
        while True:
            n, done = results_count(client, exp_id)
            elapsed = time.time() - t_start
            print(f"   t={elapsed:5.1f}s  results={n}/{n_trials}", flush=True)

            if not args.no_chaos and killed_at is None and n >= n_trials * 0.20:
                p = procs[kill_target]
                p.send_signal(signal.SIGKILL)
                killed_at = time.time()
                print(f"== CHAOS: SIGKILL {kill_target} (pid {p.pid}) at t={elapsed:.1f}s ==")
                # replacement worker joins, like an autoscaler would add one
                procs["w-replacement"] = start_worker("w-replacement")
                print("== replacement worker started ==")

            if not args.no_chaos and stalled_at is None and killed_at is not None \
                    and n >= n_trials * 0.35:
                p = procs[stall_target]
                p.send_signal(signal.SIGSTOP)
                stalled_at = time.time()
                print(f"== CHAOS: SIGSTOP {stall_target} (pid {p.pid}) at t={elapsed:.1f}s "
                      f"— simulated stall/partition, lease is {CFG.lease_seconds:.0f}s ==")

            # resume the zombie once its leases have certainly been reaped,
            # or right away if the rest of the fleet already finished everything
            if stalled_at is not None and conted_at is None and \
                    (time.time() - stalled_at > CFG.lease_seconds * 1.5 or done):
                procs[stall_target].send_signal(signal.SIGCONT)
                conted_at = time.time()
                print(f"== CHAOS: SIGCONT {stall_target} at t={time.time()-t_start:.1f}s "
                      f"— zombie resumes, submits already-re-executed work ==")

            if done:
                break
            time.sleep(1.0)

        if conted_at is not None:
            # give the resumed zombie a moment to submit its stale results so the
            # duplicate suppressions land in the provenance log
            time.sleep(5.0)

        wall = time.time() - t_start
        print(f"== experiment finished in {wall:.1f}s ==")

        report = client.get(f"/experiments/{exp_id}/report").json()
        with open("demo_report.json", "w") as f:
            json.dump(report, f, indent=2)

        print()
        print("================ REPORT ================")
        print(json.dumps(report, indent=2))
        print("=========================================")
        inv = report["invariants"]
        ft = report["fault_tolerance"]
        ok = inv["ok"]
        print()
        print(f"trials={report['trials']['total']}  results={report['results']['total']}  "
              f"throughput={report['throughput_trials_per_sec']}/s  wall={report['wall_clock_seconds']}s")
        print(f"exec p50/p99: {report['latency_ms']['exec_p50']}ms / {report['latency_ms']['exec_p99']}ms   "
              f"e2e p50/p99: {report['latency_ms']['e2e_p50_s']}s / {report['latency_ms']['e2e_p99_s']}s")
        print(f"executions started={ft['total_executions_started']}  requeues={ft['requeues_after_lease_expiry']}  "
              f"duplicates suppressed={ft['duplicate_submissions_suppressed']}  "
              f"recovered by different worker={ft['trials_recovered_by_different_worker']}")
        print(f"stale submissions (superseded/revoked lease)={ft['stale_submissions']}  "
              f"of which won the race={ft['stale_results_accepted']}")
        print(f"workers died: {ft['workers_died']}")
        print()
        print(f"RECONCILIATION: results={report['results']['total']}  "
              f"result_accepted events={inv['accepted_events']}  "
              f"executions started={inv['executions_started']}  "
              f"double claims={inv['double_claims']}  safety violations={inv['violations'] or 0}")
        print(f"INVARIANTS: lost_results={inv['lost_results']}  "
              f"duplicate_result_rows={inv['duplicate_result_rows']}  -> "
              f"{'ALL GOOD ✓' if ok else 'VIOLATION ✗'}")
        print("full report -> demo_report.json ; worker logs -> logs/")
        return 0 if ok else 1
    finally:
        for p in procs.values():
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGCONT)
                except Exception:
                    pass
                p.terminate()
        if api:
            api.terminate()


if __name__ == "__main__":
    sys.exit(main())
