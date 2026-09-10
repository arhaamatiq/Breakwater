"""Tester CLI.

  python -m projectx.tester audit [--task T ...] [--parallel 3] [--workers 4]
  python -m projectx.tester replay receipts/<file>.json

`audit` boots its own workers + reaper (needs Postgres/MinIO up and the image
pulled: docker compose up -d && python -m projectx.setup)."""
import argparse
import asyncio
import json
import os
import sys
import time

import importlib

from .. import db
from ..adapters import base as adapters_base
from ..benchmarks.suite import TASKS
from ..demo import start_worker
from .audit import TaskAudit
from .properties import SEVERITY_ORDER
from .receipts import write_receipt, replay


async def reaper_loop(pool):
    while True:
        try:
            await db.reap_expired_leases(pool)
        except Exception:
            pass
        await asyncio.sleep(1.0)


async def run_audits(task_ids, parallel, out):
    pool = await db.make_pool(max_size=10)
    reaper = asyncio.create_task(reaper_loop(pool))
    sem = asyncio.Semaphore(parallel)
    results = {}

    async def one(tid):
        async with sem:
            t0 = time.time()
            print(f"[audit] {tid}: started", flush=True)
            res = await TaskAudit(pool, tid).run()
            results[tid] = res
            sevs = [f["severity"] for f in res["findings"]]
            print(f"[audit] {tid}: done in {time.time()-t0:.1f}s — "
                  f"{len(res['findings'])} finding(s) {sevs} "
                  f"({res['stats']['trials']} trials)", flush=True)

    await asyncio.gather(*(one(t) for t in task_ids))
    reaper.cancel()
    await pool.close()
    return results


def summarize(results, out_path):
    receipts_written = []
    for tid, res in results.items():
        for f in res["findings"]:
            if f["severity"] != "INFO":
                receipts_written.append(write_receipt(f))
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "tasks": results, "receipts": receipts_written}
    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=2, default=str)

    print()
    print(f"{'task':22} {'planted flaw':28} findings")
    print("-" * 100)
    hits = misses = false_crit = 0
    for tid in sorted(results):
        planted = TASKS[tid].get("planted_flaw") or "-"
        fs = sorted(results[tid]["findings"],
                    key=lambda f: SEVERITY_ORDER.index(f["severity"]))
        shown = ", ".join(f"{f['class']}[{f['severity'][0]}]" for f in fs) or "(clean)"
        print(f"{tid:22} {planted:28} {shown}")
        serious = [f for f in fs if f["severity"] in ("CRITICAL", "HIGH", "MEDIUM")]
        if planted != "-":
            hits += bool(serious)
            misses += not serious
        elif any(f["severity"] in ("CRITICAL", "HIGH") for f in fs):
            false_crit += 1
    print("-" * 100)
    n_planted = sum(1 for t in results if TASKS[t].get("planted_flaw"))
    print(f"planted flaws detected: {hits}/{n_planted}   missed: {misses}   "
          f"CRITICAL/HIGH on sound tasks: {false_crit}")
    print(f"report -> {out_path} ; receipts -> receipts/ "
          f"({len(receipts_written)} written)")


def main():
    ap = argparse.ArgumentParser(prog="projectx.tester")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--task", action="append", default=None)
    a.add_argument("--adapter", action="append", default=[], metavar="NAME=ARG[#ID,ID]",
                   help="load an external benchmark, e.g. "
                        "humaneval=external/HumanEval.jsonl#HumanEval/0 or "
                        "tbench=external/terminal-bench/original-tasks")
    a.add_argument("--plant", action="append", default=[], metavar="TASK:KIND",
                   help="add a labeled broken copy of a loaded task "
                        "(KIND: weak|flaky) to validate detection wiring")
    a.add_argument("--adapters-only", action="store_true",
                   help="audit only adapter-loaded tasks")
    a.add_argument("--parallel", type=int, default=3)
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--out", default="audit_report.json")
    r = sub.add_parser("replay")
    r.add_argument("receipt")
    args = ap.parse_args()

    if args.cmd == "replay":
        sys.exit(0 if replay(args.receipt) else 1)

    loaded = []
    for spec in args.adapter:
        name, _, arg = spec.partition("=")
        arg, _, idpart = arg.partition("#")
        ids = [i for i in idpart.split(",") if i] or None
        mod = importlib.import_module(f"projectx.adapters.{name}")
        tasks = mod.load(arg, ids=ids)
        TASKS.update(tasks)
        loaded += list(tasks)
        print(f"[adapter] {name}: loaded {len(tasks)} task(s) from {arg}")
    for spec in args.plant:
        tid, _, kind = spec.partition(":")
        if tid not in TASKS:
            sys.exit(f"--plant: unknown task {tid}")
        planted = adapters_base.plant(TASKS[tid], kind)
        pid = f"{tid}__planted_{kind}"
        TASKS[pid] = planted
        loaded.append(pid)
        print(f"[adapter] planted {kind} variant -> {pid}")

    task_ids = args.task or (sorted(loaded) if args.adapters_only else sorted(TASKS))
    unknown = [t for t in task_ids if t not in TASKS]
    if unknown:
        sys.exit(f"unknown tasks: {unknown}")

    os.makedirs("logs", exist_ok=True)
    procs = [start_worker(f"audit-w{i}") for i in range(1, args.workers + 1)]
    try:
        results = asyncio.run(run_audits(task_ids, args.parallel, args.out))
        summarize(results, args.out)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
