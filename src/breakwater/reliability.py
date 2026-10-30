"""Rule-based benchmark reliability layer.

Probes each task with three experiments run on the platform itself:
  oracle x5   — known-good solution, 5 replicates
  noop  x1    — a solution that does nothing
  agent x2    — the candidate solution twice (repeat consistency)

Rules (deliberately simple; no learned components):
  NONDETERMINISTIC_VERIFIER  oracle pass rate strictly between 0 and 1
  BROKEN_TASK                oracle never passes
  WEAK_VERIFIER              the no-op solution passes
  NONDETERMINISTIC_REPEAT    identical agent runs disagree
  INFRA_FAILURE              any probe trial exhausted retries

Run: python -m breakwater.reliability
"""
import json
import sys
import time
from collections import defaultdict

import httpx

from .config import CFG
from .demo import start_api, start_worker, wait_api


def wait_done(client, exp_ids, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        statuses = [client.get(f"/experiments/{e}").json() for e in exp_ids]
        n = sum(s["results"] for s in statuses)
        total = sum(s["trials"] for s in statuses)
        print(f"   probes: {n}/{total} results", flush=True)
        if all(s["done"] for s in statuses):
            return
        time.sleep(2)
    raise RuntimeError("probe experiments did not finish in time")


def by_task(client, exp_id):
    grouped = defaultdict(list)
    for r in client.get(f"/experiments/{exp_id}/results").json():
        grouped[r["task_id"]].append(r["outcome"])
    return grouped


def main():
    import os
    os.makedirs("logs", exist_ok=True)
    procs, api = {}, None
    try:
        api = start_api()
        client = httpx.Client(base_url=CFG.api_url, timeout=10)
        wait_api(client)
        for i in (1, 2):
            procs[f"probe-w{i}"] = start_worker(f"probe-w{i}")
        print("== reliability probes: oracle x5, noop x1, agent x2 per task ==")

        probes = {}
        for kind, reps in (("oracle", 5), ("noop", 1), ("agent", 2)):
            r = client.post("/experiments", json={
                "name": f"probe-{kind}", "replicates": reps, "solution": kind})
            r.raise_for_status()
            probes[kind] = r.json()["experiment_id"]
            print(f"   {kind}: {r.json()['n_trials']} trials -> {probes[kind]}")

        wait_done(client, list(probes.values()))

        oracle, noop, agent = (by_task(client, probes[k]) for k in ("oracle", "noop", "agent"))
        failed_counts = {}
        for k, e in probes.items():
            rep = client.get(f"/experiments/{e}/report").json()
            failed_counts[k] = rep["invariants"]["infra_failed_trials"]

        verdicts = {}
        for task_id in sorted(set(oracle) | set(noop) | set(agent)):
            flags = []
            o = oracle.get(task_id, [])
            rate = o.count("pass") / len(o) if o else None
            if rate is not None:
                if rate == 0:
                    flags.append("BROKEN_TASK")
                elif rate < 1:
                    flags.append("NONDETERMINISTIC_VERIFIER")
            if "pass" in noop.get(task_id, []):
                flags.append("WEAK_VERIFIER")
            a = agent.get(task_id, [])
            if len(set(a)) > 1:
                flags.append("NONDETERMINISTIC_REPEAT")
            verdicts[task_id] = {
                "oracle_pass_rate": rate,
                "noop_outcome": (noop.get(task_id) or ["missing"])[0],
                "agent_outcomes": a,
                "flags": flags,
                "verdict": "SUSPECT" if flags else "RELIABLE",
            }
        if any(failed_counts.values()):
            verdicts["_infra"] = {"flags": ["INFRA_FAILURE"], "failed_trials": failed_counts}

        out = {"probes": probes, "verdicts": verdicts, "generated_at": time.time()}
        with open("reliability_report.json", "w") as f:
            json.dump(out, f, indent=2)

        print()
        print(f"{'task':24} {'oracle':>7} {'noop':>6} {'agent':>12}  verdict")
        for t, v in verdicts.items():
            if t.startswith("_"):
                continue
            rate = f"{v['oracle_pass_rate']:.0%}" if v["oracle_pass_rate"] is not None else "-"
            print(f"{t:24} {rate:>7} {v['noop_outcome']:>6} {','.join(v['agent_outcomes']):>12}  "
                  f"{v['verdict']}{': ' + ','.join(v['flags']) if v['flags'] else ''}")
        n_suspect = sum(1 for t, v in verdicts.items() if not t.startswith('_') and v["flags"])
        print(f"\n{n_suspect} task(s) flagged. Full report -> reliability_report.json")
        return 0
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        if api:
            api.terminate()


if __name__ == "__main__":
    sys.exit(main())
