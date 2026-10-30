"""breakwater — verifier regression testing for agent benchmarks.

  breakwater up                      start Postgres (+ schema) for the queue
  breakwater doctor                  check Docker, Postgres, disk
  breakwater tasks  DIR|REPO@REF     what this runtime can run, and why not
  breakwater check  TASK_DIR         oracle + no-op through the real verifier
  breakwater diff   --repo R --base A --head B [--tasks ...]
                                     Verifier Trust Diff across two versions
  breakwater replay RECEIPT          re-run the evidence behind a finding
  breakwater conform TRIAL... --task-dir D
                                     agree with official Harbor on recorded trials?
  breakwater audit  ...              single-version falsification audit
  breakwater chaos                   fault-injection demo of the queue
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time

from . import cas


def _p(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ workers

@contextlib.contextmanager
def worker_pool(n: int, concurrency: int = 1, prefix: str = "bw"):
    """Spawn n worker processes (each claims `concurrency` trials at a time).
    Workers are ordinary queue clients: they can be killed at any point and
    their leased trials are reaped and re-run elsewhere."""
    log_dir = os.path.join(cas.BW_HOME, "logs")
    os.makedirs(log_dir, exist_ok=True)
    env = {**os.environ, "BW_WORKER_CONCURRENCY": str(concurrency)}
    stamp = time.strftime("%H%M%S")
    procs = [subprocess.Popen([sys.executable, "-m", "breakwater.worker", "--id",
                               f"{prefix}-{stamp}-w{i}", "--concurrency", str(concurrency)],
                              stdout=open(os.path.join(log_dir, f"{prefix}-{stamp}-w{i}.log"), "w"),
                              stderr=subprocess.STDOUT, env=env)
             for i in range(1, n + 1)]
    try:
        yield procs
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            with contextlib.suppress(Exception):
                p.wait(timeout=20)


async def _reaper(pool):
    from . import db
    while True:
        with contextlib.suppress(Exception):
            await db.reap_expired_leases(pool)
        await asyncio.sleep(1.0)


# ------------------------------------------------------------------ commands

def cmd_up(args):
    from . import setup
    if args.schema_only:          # Postgres provided elsewhere (CI service, managed DB)
        setup.main()
        return
    from importlib import resources
    compose = None
    with contextlib.suppress(Exception):
        compose = str(resources.files("breakwater") / "compose.yml")
    if not compose or not os.path.exists(compose):
        compose = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.yml")
    services = ["postgres"] + (["minio"] if args.s3 else [])
    subprocess.run(["docker", "compose", "-f", compose, "-p", "breakwater", "up", "-d",
                    "--wait", *services], check=True)
    setup.main()


def cmd_doctor(args):
    ok = True
    try:
        from .runner import docker_client
        info = docker_client().info()
        _p(f"docker      ok  server {info.get('ServerVersion')}, {info.get('NCPU')} cpus, "
           f"{info.get('MemTotal', 0) / 2**30:.1f} GiB")
    except Exception as e:
        ok = False
        _p(f"docker      FAIL {e}")
    try:
        import asyncpg
        from .config import CFG

        async def ping():
            c = await asyncpg.connect(CFG.dsn, timeout=5)
            v = await c.fetchval("select version()")
            await c.close()
            return v
        _p(f"postgres    ok  {asyncio.run(ping())[:40]}")
    except Exception as e:
        ok = False
        _p(f"postgres    FAIL {type(e).__name__}: {e}  (run `breakwater up`)")
    free = shutil.disk_usage(os.path.expanduser("~")).free / 2**30
    _p(f"disk        {'ok ' if free > 20 else 'LOW'} {free:.0f} GiB free (benchmark images are large)")
    _p(f"state dir   {cas.BW_HOME}")
    sys.exit(0 if ok else 1)


def _source(spec: str, label: str):
    from .trustdiff.versions import Source
    if "@" in spec and not os.path.isdir(spec):
        repo, ref = spec.rsplit("@", 1)
        return Source(label, repo=repo, ref=ref)
    return Source(label, tasks_dir=spec)


def cmd_tasks(args):
    from .adapters.harbor_task import unsupported_reason
    src = _source(args.source, "tasks")
    rows, tally = [], {}
    for t in src.list_tasks():
        v = src.version(t)
        s = v.spec
        why = unsupported_reason(s)
        cat = why[0] if why else "supported"
        tally[cat] = tally.get(cat, 0) + 1
        rows.append({"task": t, "status": cat, "detail": why[1] if why else "",
                     "verifier": s.digests["verifier"][:12], "version": s.version,
                     "network": [s.agent_env.network, s.verifier_env.network if s.verifier_env else None]})
    if args.json:
        print(json.dumps({"tasks": rows, "tally": tally}, indent=2))
        return
    for r in rows:
        _p(f"{r['task']:34} {r['status']:16} {r['detail'][:70]}")
    _p("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(tally.items())))


def cmd_check(args):
    """Oracle and no-op of one task through the real verifier, no queue."""
    from .adapters import harbor
    d = os.path.abspath(args.task_dir)
    tasks = harbor.load(os.path.dirname(d), [os.path.basename(d)])
    if not tasks:
        sys.exit(f"cannot run {d}: {harbor.LAST_LOAD_REPORT.get('skipped')}")
    task = next(iter(tasks.values()))
    bad = 0
    for sol, want in (("oracle", "pass"), ("noop", "fail")):
        for k in range(args.repeats):
            r = harbor.RUNTIME.run_trial({"trial_id": f"check/{sol}/{k}", "experiment_id": "check",
                                          "task_id": os.path.basename(d), "attempts": 1,
                                          "spec": {"mode": "full", "solution": sol}}, task)
            dd = r["detail"]
            ok = r["outcome"] == want
            bad += not ok
            _p(f"{sol:6} #{k}: {r['outcome']:5} reward={dd.get('primary_reward')} "
               f"({dd.get('reward_source')}) verify {dd.get('verifier_wall_s')}s "
               f"{'' if ok else '<-- expected ' + want}")
    sys.exit(1 if bad else 0)


def _fixtures(specs: list[str]) -> list:
    """TASK:NAME:EXPECT:PATCH_FILE -> (task, name, expect|None, patch text)."""
    out = []
    for s in specs:
        task, name, expect, path = s.split(":", 3)
        out.append((task, name, None if expect in ("", "unknown") else expect,
                    open(path).read()))
    return out


def cmd_diff(args):
    from . import db
    from .trustdiff import engine, report
    from .trustdiff.versions import changed_verifiers

    if args.repo:
        base = _source(f"{args.repo}@{args.base}", "base")
        head = _source(f"{args.repo}@{args.head}", "head")
    else:
        base, head = _source(args.base, "base"), _source(args.head, "head")
    tasks = [t for t in (args.tasks or "").split(",") if t]
    if not tasks:
        tasks = changed_verifiers(base, head)
        _p(f"[diff] {len(tasks)} task(s) with a changed verifier")
    if args.exclude:
        tasks = [t for t in tasks if t not in set(args.exclude.split(","))]
    if not args.keep_unsupported:
        from .adapters.harbor_task import unsupported_reason
        keep = []
        for t in tasks:
            bv, hv = base.version(t), head.version(t)
            why = (bv and unsupported_reason(bv.spec)) or (hv and unsupported_reason(hv.spec))
            if why:
                _p(f"[diff] skip {t}: {why[0]} — {why[1]}")
            else:
                keep.append(t)
        tasks = keep
    cfg = engine.DiffConfig(
        repeats=args.repeats, max_repeats=args.max_repeats,
        corpus=tuple(args.corpus.split(",")), metrics=dict(m.split("=", 1) for m in args.metric),
        network=args.network, solve_from=args.solve_from,
        max_ast_files=args.max_ast_files, max_ast_per_file=args.max_ast_per_file,
        max_files=args.max_files, solve_timeout_cap=args.solve_timeout_cap,
        prune_images=args.prune_images, fixtures=_fixtures(args.fixture), epoch=args.epoch,
        id_scheme=args.id_scheme,
        solution_fixtures=_fixtures(args.solution_fixture),
        imports=[tuple(x.split(":", 1)) for x in args.import_trials],
        bundles=[(t, n, None if e in ("", "unknown") else e, p) for t, n, e, p in
                 (x.split(":", 3) for x in args.bundle)])
    out = args.out or os.path.join("reports", "trustdiff", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)

    async def main():
        pool = await db.make_pool(max_size=10)
        reaper = asyncio.create_task(_reaper(pool))
        try:
            return await engine.run_diff(pool, base, head, tasks, cfg, parallel=args.parallel,
                                         log=_p)
        finally:
            reaper.cancel()
            await pool.close()

    # --workers 0: submit to the shared queue and let an existing fleet
    # (other diffs' workers, `python -m breakwater.worker` on any host) run it
    with (worker_pool(args.workers, args.concurrency, "diff") if args.workers
          else contextlib.nullcontext()):
        result = asyncio.run(main())
    meta = report.export(result, out)
    _p(f"\nreport  -> {out}/REPORT.md\nresults -> {out}/trustdiff.json\n"
       f"receipts: {len(meta['receipts'])} in {out}/receipts/")
    worst = [b for t in result["tasks"] for b in t.get("bundles", [])
             if b["severity"] in ("CRITICAL", "HIGH")]
    sys.exit(2 if (worst and args.fail_on_high) else 0)


def cmd_report(args):
    """Re-classify and re-render a saved Trust Diff (e.g. after the rules improve)."""
    from .trustdiff import engine, report
    path = os.path.join(args.run_dir, "trustdiff.json")
    result = json.load(open(path))
    for t in result["tasks"]:
        for b in t.get("bundles", []):
            b["class"], b["severity"] = engine._transition(b["expect"], b["base"]["state"],
                                                           b["head"]["state"])
    meta = report.export(result, args.run_dir)
    _p(f"re-rendered {args.run_dir}/REPORT.md ({len(meta['receipts'])} receipts)")


def cmd_replay(args):
    rec = json.load(open(args.receipt))
    if rec.get("kind") != "trustdiff":
        from .tester.receipts import replay
        sys.exit(0 if replay(args.receipt) else 1)
    from .adapters import harbor
    from .bundle import Bundle
    from .adapters.harbor_task import artifact_paths, parse_task
    from .trustdiff.versions import Source
    rdir = os.path.dirname(os.path.dirname(os.path.abspath(args.receipt)))
    raw = open(os.path.join(rdir, rec["bundle"]["file"]), "rb").read()
    if cas.put(raw) != rec["bundle"]["digest"]:
        sys.exit("bundle file does not match the receipt digest")
    _p(f"receipt: {rec['task']} [{rec['class']}] {rec['label']} — {rec['claim']}")
    n = args.n or rec["replay"]["n"]
    ok_all = True
    for label in ("base", "head"):
        v = rec["versions"][label]
        path = v.get("path")
        if not (path and os.path.isfile(os.path.join(path, "task.toml"))):
            if not args.repo:
                sys.exit(f"task version {label}@{v['commit']} not cached; pass --repo <clone of {v['repo']}>")
            path = Source(label, repo=args.repo, ref=v["commit"]).version(rec["task"]).path
        spec = parse_task(path)
        if spec.digests["verifier"] != v["verifier_digest"]:
            sys.exit(f"{label}: verifier digest mismatch — not the version in the receipt")
        task = harbor.task_dict(spec)
        bundle = Bundle.from_tar(raw, artifact_paths(spec))
        outs = []
        for k in range(n):
            vr = harbor.RUNTIME.verify(spec, bundle, {}, {}, None, {})
            outs.append(vr["outcome"])
        want = rec["replay"]["expect"][label]
        state = ("PASS" if all(o == "pass" for o in outs) else "FAIL" if all(o == "fail" for o in outs)
                 else "ERROR" if all(o == "error" for o in outs) else "MIXED")
        ok = state == want or (want == "MIXED" and len(set(outs)) > 1)
        ok_all &= ok
        _p(f"  {label:4} @{(v.get('commit') or '')[:10]} {outs} -> {state} "
           f"(recorded {want}) {'REPRODUCED' if ok else 'DID NOT REPRODUCE'}")
        del task
    _p(f"  => {'CONFIRMED' if ok_all else 'NOT CONFIRMED'}")
    sys.exit(0 if ok_all else 1)


def cmd_conform(args):
    """Differential conformance against the official harness: for each recorded
    Harbor trial, re-verify the exact artifact bytes with Breakwater's runtime
    and compare the reward (and per-check verdicts) Harbor recorded."""
    from .adapters import harbor
    from .adapters.harbor_task import artifact_paths, parse_task
    from .bundle import from_harbor_trial
    spec = parse_task(args.task_dir)
    rows, mismatches = [], 0
    for tdir in args.trials:
        res = json.load(open(os.path.join(tdir, "result.json")))
        h_rewards = (res.get("verifier_result") or {}).get("rewards")
        h_pr = harbor.primary_reward(h_rewards or {})
        if h_pr is None:
            exc = (res.get("exception_info") or {}).get("exception_type")
            rows.append({"trial": os.path.basename(tdir.rstrip("/")), "harbor_reward": None,
                         "harbor_exception": exc, "agree": None})
            _p(f"{os.path.basename(tdir.rstrip('/'))[:40]:40} harbor recorded no reward "
               f"({exc}); not comparable")
            continue
        h_checks = {}
        ctrf = os.path.join(tdir, "verifier", "ctrf.json")
        if os.path.exists(ctrf):
            for t in json.load(open(ctrf)).get("results", {}).get("tests", []):
                h_checks[t.get("name", "").split("::")[-1]] = t.get("status") == "passed"
        bundle = from_harbor_trial(tdir, artifact_paths(spec))
        for k in range(args.n):
            v = harbor.RUNTIME.verify(spec, bundle, {}, {}, None, {})
            b_checks = {c["name"].split("::")[-1]: c["status"] == "passed" for c in v["checks"]}
            diff = sorted(n for n in set(h_checks) | set(b_checks) if h_checks.get(n) != b_checks.get(n))
            same = (v["primary_reward"] == h_pr) and not diff
            mismatches += not same
            row = {"trial": os.path.basename(tdir.rstrip("/")), "bundle": bundle.digest,
                   "harbor_reward": h_pr, "breakwater_reward": v["primary_reward"],
                   "breakwater_outcome": v["outcome"], "checks_compared": len(set(h_checks) | set(b_checks)),
                   "check_mismatches": diff, "agree": same}
            rows.append(row)
            _p(f"{row['trial'][:40]:40} harbor={h_pr} breakwater={v['primary_reward']} "
               f"checks={row['checks_compared']} {'AGREE' if same else 'MISMATCH ' + str(diff[:5])}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"task": spec.name, "verifier_digest": spec.digests["verifier"],
                       "rows": rows}, f, indent=2)
    scored = [r for r in rows if r["agree"] is not None]
    _p(f"{len(scored) - mismatches}/{len(scored)} verifications agree with Harbor"
       f" ({len(rows) - len(scored)} Harbor trials unscored)")
    sys.exit(1 if mismatches else 0)


def cmd_audit(args, rest):
    sys.argv = ["breakwater audit", "audit", *rest]
    from .tester.__main__ import main
    main()


def cmd_chaos(args, rest):
    sys.argv = ["breakwater chaos", *rest]
    from .demo import main
    main()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="breakwater", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up", help="start Postgres and apply the schema")
    u.add_argument("--s3", action="store_true", help="also start MinIO")
    u.add_argument("--schema-only", action="store_true",
                   help="only apply the schema to BW_DSN (no containers)")
    sub.add_parser("doctor", help="check the local environment")
    t = sub.add_parser("tasks", help="list tasks and runtime support")
    t.add_argument("source", help="tasks dir, or REPO@REF")
    t.add_argument("--json", action="store_true")
    c = sub.add_parser("check", help="oracle + no-op of one task")
    c.add_argument("task_dir")
    c.add_argument("--repeats", type=int, default=1)
    d = sub.add_parser("diff", help="Verifier Trust Diff between two task versions")
    d.add_argument("--repo", help="git repo containing tasks/ (then --base/--head are refs)")
    d.add_argument("--base", required=True)
    d.add_argument("--head", required=True)
    d.add_argument("--tasks", help="comma-separated task names (default: every task whose verifier changed)")
    d.add_argument("--exclude")
    d.add_argument("--repeats", type=int, default=2)
    d.add_argument("--max-repeats", type=int, default=5)
    d.add_argument("--corpus", default="reference,noop,degenerate,path,file,ast")
    d.add_argument("--max-files", type=int, default=6)
    d.add_argument("--max-ast-files", type=int, default=2)
    d.add_argument("--max-ast-per-file", type=int, default=6)
    d.add_argument("--metric", action="append", default=[], metavar="NAME=REGEX",
                   help="numeric margin to extract from test stdout, e.g. "
                        "'speedup=Geomean speedup:\\s+([0-9.]+)x'")
    d.add_argument("--fixture", action="append", default=[], metavar="TASK:NAME:EXPECT:PATCH",
                   help="add a hand-written output: the reference bundle with PATCH (a unified "
                        "diff, paths from /) applied; EXPECT is pass, fail or unknown")
    d.add_argument("--solution-fixture", action="append", default=[],
                   metavar="TASK:NAME:EXPECT:PATCH",
                   help="add a constructed candidate solution: PATCH (unified diff, paths from the "
                        "task dir, e.g. a/solution/solve.py) is applied to solution/, solved once, "
                        "and the output judged by both versions")
    d.add_argument("--bundle", action="append", default=[], metavar="TASK:LABEL:EXPECT:TAR",
                   help="a saved bundle tar (e.g. from a report's bundles/) to judge")
    d.add_argument("--import", dest="import_trials", action="append", default=[],
                   metavar="TASK:DIR", help="recorded Harbor trial dir(s) to judge with both "
                   "versions (e.g. from `harbor trial download`); expected = recorded reward")
    d.add_argument("--network", choices=["on", "off"], default=None,
                   help="override the task's network policy for every container")
    d.add_argument("--solve-from", choices=["base", "head"], default="base")
    d.add_argument("--solve-timeout-cap", type=float, default=None)
    d.add_argument("--id-scheme", choices=["v2", "legacy"], default="v2",
                   help="legacy: rebuild a report from cells of a pre-hb2 run")
    d.add_argument("--epoch", help="namespace for verify cells: re-measure instead of reusing "
                                   "cached verdicts (timing studies)")
    d.add_argument("--workers", type=int, default=3,
                   help="worker processes to spawn (0 = use an already-running fleet)")
    d.add_argument("--concurrency", type=int, default=1)
    d.add_argument("--parallel", type=int, default=3, help="tasks diffed concurrently")
    d.add_argument("--keep-unsupported", action="store_true")
    d.add_argument("--prune-images", action="store_true",
                   help="remove each task's images when its diff finishes (saves disk)")
    d.add_argument("--fail-on-high", action="store_true", help="exit 2 on CRITICAL/HIGH (CI)")
    d.add_argument("--out")
    rp = sub.add_parser("report", help="re-classify and re-render a saved Trust Diff")
    rp.add_argument("run_dir")
    r = sub.add_parser("replay", help="re-run a receipt")
    r.add_argument("receipt")
    r.add_argument("--repo", help="local clone of the benchmark repo")
    r.add_argument("-n", type=int, default=None)
    cf = sub.add_parser("conform", help="re-verify recorded Harbor trials; compare rewards")
    cf.add_argument("trials", nargs="+", help="Harbor trial directories (harbor run / download)")
    cf.add_argument("--task-dir", required=True, help="the task version Harbor verified with")
    cf.add_argument("-n", type=int, default=1)
    cf.add_argument("--json")
    sub.add_parser("audit", help="single-version falsification audit (see `audit -h`)",
                   add_help=False)
    sub.add_parser("chaos", help="fault-injection demo of the queue", add_help=False)

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("audit", "chaos"):
        return {"audit": cmd_audit, "chaos": cmd_chaos}[argv[0]](None, argv[1:])
    args = ap.parse_args(argv)
    return {"up": cmd_up, "doctor": cmd_doctor, "tasks": cmd_tasks, "check": cmd_check,
            "diff": cmd_diff, "replay": cmd_replay, "conform": cmd_conform,
            "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
