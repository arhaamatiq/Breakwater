"""Deterministic ablation sweep over Harbor / Terminal-Bench v4 tasks.

Mechanically damages a task's shipped reference solution (or the state that
solution produces), re-runs the task's OWN verifier verbatim, and records
whether the verifier still returns pass. Nothing here judges a benchmark; it
records verifier outcomes for review.

Execution model
---------------
Harbor v4 `separate` mode: only the paths the task declares in `artifacts`
cross from the solved environment into the verify container. So the state a
verifier can possibly observe IS the declared artifact set. That makes two
things possible:

  * the reference solve runs ONCE per task (mode="full"), and its exact
    artifact bytes are captured;
  * every artifact-level mutant then replays through the platform's existing
    verify_only path (`mode="verify_only"`, artifacts supplied in the spec),
    which calls the identical `HarborRuntime._verify`. Same verifier, same
    image, same command — only the artifact bytes differ.

This touches no adapter, queue, lease, heartbeat or exactly-once code. It is a
client of `db.create_experiment` like the auditor is.

Determinism / dedupe
--------------------
trial_id = mut/<task>/<operator>/<sha8 of the mutant artifact manifest>, with no
experiment prefix. Re-running inserts the same trial_ids, so the queue's
ON CONFLICT DO NOTHING dedupes reruns through the exactly-once path.

Controls (enforced, per the run protocol)
-----------------------------------------
  reference  full-mode unmutated solve. Must pass or the task is skipped.
  noop       full-mode empty solve.sh. Must fail or the task is quarantined.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time

from breakwater import db
from breakwater.adapters import harbor
from breakwater.demo import start_worker

TEXTISH = (".json", ".csv", ".tsv", ".txt", ".md", ".yaml", ".yml", ".xml",
           ".py", ".log", ".v", ".scala", ".cu", ".c", ".h", ".cpp", ".js",
           ".ts", ".html", ".ini", ".cfg", ".toml", ".sh", ".sql", ".patch",
           ".step", ".stp", ".svg", ".rs", ".go", ".java", ".lean")
DIGIT_ROT = bytes.maketrans(b"0123456789", b"1234567890")


def _is_textish(path: str, data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    return path.lower().endswith(TEXTISH)


def _under(path: str, declared: str) -> bool:
    """Is an extracted artifact file `path` part of declared path `declared`?"""
    d = declared.rstrip("/")
    return path == d or path.startswith(d + "/")


def _manifest_hash(arts: dict) -> str:
    h = hashlib.sha256()
    for k in sorted(arts):
        h.update(k.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(arts[k]).digest())
    return h.hexdigest()


def _missing_for(arts: dict, declared: list) -> list:
    return [d for d in declared if not any(_under(p, d) for p in arts)]


# --------------------------------------------------------------- operators
# Each returns a NEW {abs_path: bytes} dict, or None when the operator does not
# apply to this task's artifacts (e.g. no digits to rotate). Applicability is an
# experiment-validity gate, not a verdict.

def op_drop_all(arts, declared, env_state):
    return {}


def op_zero_all(arts, declared, env_state):
    return {k: b"" for k in arts}


def op_halve_all(arts, declared, env_state):
    out = {k: v[: len(v) // 2] for k, v in arts.items()}
    return out


def op_digits_all(arts, declared, env_state):
    out = {}
    touched = False
    for k, v in arts.items():
        if _is_textish(k, v):
            nv = v.translate(DIGIT_ROT)
            touched = touched or nv != v
            out[k] = nv
        else:
            out[k] = v
    return out if touched else None


def op_drop_path(i):
    def f(arts, declared, env_state):
        d = declared[i]
        return {k: v for k, v in arts.items() if not _under(k, d)}
    return f


def op_unsolved_path(i):
    """Revert declared path i to its pre-solve (environment image) content,
    leaving every other artifact fully solved."""
    def f(arts, declared, env_state):
        d = declared[i]
        out = {k: v for k, v in arts.items() if not _under(k, d)}
        for k, v in env_state.items():
            if _under(k, d):
                out[k] = v
        return out
    return f


def op_drop_file(path):
    def f(arts, declared, env_state):
        return {k: v for k, v in arts.items() if k != path}
    return f


def op_set_file(path, new_bytes):
    def f(arts, declared, env_state):
        out = dict(arts)
        out[path] = new_bytes
        return out
    return f


def _spread(seq, k):
    """Deterministic evenly-spaced sample of at most k items."""
    if len(seq) <= k:
        return list(seq)
    step = len(seq) / k
    return [seq[int(i * step)] for i in range(k)]


def build_operators(declared: list, arts: dict, max_files: int, max_ast: int):
    """Ablation operators for one task.

    Whole-artifact-set operators apply everywhere. Per-declared-path operators
    need >=2 declared paths to say anything the noop control doesn't. Per-file
    and AST operators are the fine-grained end: they leave the solution intact
    except for one file, or one semantic edit inside one file.
    """
    ops = [("drop_all", op_drop_all), ("zero_all", op_zero_all),
           ("halve_all", op_halve_all), ("digits_all", op_digits_all)]
    if len(declared) >= 2:
        for i, d in enumerate(declared):
            tag = d.strip("/").replace("/", "_")
            ops.append((f"drop_path::{tag}", op_drop_path(i)))
            ops.append((f"unsolved_path::{tag}", op_unsolved_path(i)))

    files = sorted(arts)
    if len(files) > 1:
        for p in _spread(files, max_files):
            ops.append((f"drop_file::{p}", op_drop_file(p)))

    # AST family: single-edit semantic mutations of Python artifacts. Each
    # mutated file is emitted via ast.unparse, so every AST mutant is compared
    # against an `ast_identity` control (same unparse, no semantic edit) — if
    # the control does not pass, that file's AST mutants carry no information.
    from breakwater.tester.mutate import mutants as ast_mutants
    import ast as _ast
    for p in files:
        if not p.endswith(".py"):
            continue
        try:
            src = arts[p].decode()
            base_src = _ast.unparse(_ast.parse(src))
        except Exception:
            continue
        muts = ast_mutants(src, limit=60)
        if not muts:
            continue
        ops.append((f"ast_identity::{p}", op_set_file(p, base_src.encode()),
                    {"kind": "ast_control", "file": p, "ref_text": base_src,
                     "mut_text": base_src}))
        for j, m in enumerate(_spread(muts, max_ast)):
            ops.append((f"ast::{p}::{m['op']}#{j}",
                        op_set_file(p, m["src"].encode()),
                        {"kind": "ast", "file": p, "ref_text": base_src,
                         "mut_text": m["src"], "edit": m["op"]}))
    return [(o[0], o[1], (o[2] if len(o) > 2 else {})) for o in ops]


# ------------------------------------------------------------------ runner
async def submit_and_wait(pool, exp_id, name, trials, timeout, log):
    if not trials:
        return {}
    await db.create_experiment(pool, exp_id, name, {"ablation": True}, trials)
    ids = [t["trial_id"] for t in trials]
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = await pool.fetchval(
            "SELECT count(*) FROM trials WHERE trial_id = ANY($1::text[]) "
            "AND state IN ('queued','running')", ids)
        if pending == 0:
            break
        await asyncio.sleep(1.0)
    else:
        log(f"  [warn] {exp_id}: timeout with trials still pending")
    rows = await pool.fetch(
        """SELECT t.trial_id, t.state, t.attempts,
                  r.outcome, r.detail, r.artifacts, r.exec_ms
           FROM trials t LEFT JOIN results r USING (trial_id)
           WHERE t.trial_id = ANY($1::text[])""", ids)
    out = {r["trial_id"]: dict(r) for r in rows}
    # Execution errors are not a trials column; they are recorded as `released`
    # events carrying the reason. Attach the most recent one per trial so a
    # skipped task can say WHY its reference failed.
    errs = await pool.fetch(
        """SELECT DISTINCT ON (trial_id) trial_id, payload
           FROM events WHERE trial_id = ANY($1::text[]) AND kind = 'released'
           ORDER BY trial_id, event_id DESC""", ids)
    for e in errs:
        p = e["payload"]
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}
        if e["trial_id"] in out:
            out[e["trial_id"]]["last_error"] = (p or {}).get("reason")
    return out


def _detail(row):
    d = row.get("detail")
    if isinstance(d, str):
        try:
            return json.loads(d)
        except Exception:
            return {}
    return d or {}


def _arts_from(row):
    det = _detail(row)
    outs = det.get("outputs") or []
    if not outs:
        return {}, []
    a = outs[0].get("artifacts") or {}
    return ({k: base64.b64decode(v) for k, v in a.items()},
            outs[0].get("missing") or [])


def env_image_state(task, declared):
    """Pre-solve content of the declared paths, straight from the environment
    image. Used by the unsolved_path operator."""
    from breakwater.adapters.harbor import HarborRuntime
    rt = HarborRuntime()
    try:
        got, _ = rt._extract_artifacts(task["hb"]["env_image"], declared)
        return got
    except Exception:
        return {}


def _make_diff(opname, ref_arts, mut_arts, extra, max_chars=6000):
    """Unified diff for a mutant. AST mutants diff the unparsed source against
    the unparsed reference, so the single semantic edit is the only hunk.
    Artifact-level mutants get a per-file structural diff."""
    import difflib
    if extra.get("kind") in ("ast", "ast_control"):
        d = "\n".join(difflib.unified_diff(
            extra["ref_text"].splitlines(), extra["mut_text"].splitlines(),
            fromfile=f"reference{extra['file']}", tofile=f"mutant{extra['file']}",
            lineterm="", n=3))
        return d[:max_chars] or "(no textual change)"
    lines = []
    for k in sorted(set(ref_arts) | set(mut_arts)):
        a, b = ref_arts.get(k), mut_arts.get(k)
        if a == b:
            continue
        if b is None:
            lines.append(f"--- reference{k}\n+++ mutant{k}\n@@ file removed "
                         f"({len(a)} bytes) @@")
            continue
        if a is None:
            lines.append(f"--- reference{k}\n+++ mutant{k}\n@@ file added "
                         f"({len(b)} bytes) @@")
            continue
        try:
            at, bt = a.decode(), b.decode()
        except UnicodeDecodeError:
            lines.append(f"--- reference{k}\n+++ mutant{k}\n"
                         f"@@ binary content changed: {len(a)} -> {len(b)} bytes @@")
            continue
        hunk = "\n".join(difflib.unified_diff(
            at.splitlines(), bt.splitlines(), fromfile=f"reference{k}",
            tofile=f"mutant{k}", lineterm="", n=1))
        lines.append(hunk or f"--- reference{k}\n+++ mutant{k}\n"
                             f"@@ content changed: {len(a)} -> {len(b)} bytes @@")
    return ("\n".join(lines) or "(no change)")[:max_chars]


def fetch_verifier_stdout(storage_refs, max_chars=8000):
    """Pull the verify container's stdout/stderr back out of the artifact store."""
    from breakwater import storage
    if not storage_refs:
        return None
    for ref in storage_refs:
        if not isinstance(ref, str) or not ref.endswith("/verify.log"):
            continue
        try:
            data = storage.get_uri(ref) if "://" in ref else storage.get_bytes(ref)
            return data.decode(errors="replace")[-max_chars:]
        except Exception as e:
            return f"(could not fetch {ref}: {e})"
    return None


async def reaper_loop(pool):
    """Same lease reaper the auditor runs, so a worker dying mid-trial can't
    strand a lease. Uses the existing db function; changes nothing about it."""
    while True:
        try:
            await db.reap_expired_leases(pool)
        except Exception:
            pass
        await asyncio.sleep(1.0)


async def run(args):
    log_lines = []

    def log(m):
        print(m, flush=True)
        log_lines.append(m)

    tasks = harbor.load(args.root, ids=args.task or None)
    load_report = json.loads(json.dumps(harbor.LAST_LOAD_REPORT))
    log(f"[ablate] loaded {len(tasks)} task(s)")
    if args.limit:
        tasks = {k: tasks[k] for k in sorted(tasks)[: args.limit]}
        log(f"[ablate] limited to {len(tasks)}: {sorted(tasks)}")

    os.makedirs("logs", exist_ok=True)
    procs = [start_worker(f"ablate-w{i}") for i in range(1, args.workers + 1)]
    pool = await db.make_pool(max_size=12)
    reaper = asyncio.create_task(reaper_loop(pool))

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "corpus": {"benchmark": "terminal-bench-v4 (Harbor)",
                         "root": args.root, "clone_sha": args.sha},
              "load_report": load_report, "tasks": {}, "surviving": [],
              "quarantine": [], "skipped_tasks": []}
    try:
        # -------- phase 1: controls, full mode, one batch for all tasks
        ctl_trials = []
        for tid, t in sorted(tasks.items()):
            blob = t
            for kind, sol in (("reference", "oracle"), ("noop", "noop")):
                ctl_trials.append({
                    "trial_id": f"mut/{tid}/__control__/{kind}",
                    "task_id": tid, "replicate": 0,
                    "spec": {"mode": "full", "solution": sol,
                             "task_blob": blob}})
        log(f"[ablate] phase 1: {len(ctl_trials)} control trials "
            f"({len(tasks)} tasks x reference,noop)")
        ctl = await submit_and_wait(
            pool, f"ablate-ctl-{args.stamp}", "ablation:controls",
            ctl_trials, args.control_timeout, log)

        live = {}
        for tid, t in sorted(tasks.items()):
            ref = ctl.get(f"mut/{tid}/__control__/reference") or {}
            nop = ctl.get(f"mut/{tid}/__control__/noop") or {}
            ref_out, nop_out = ref.get("outcome"), nop.get("outcome")
            entry = {"reference_outcome": ref_out, "noop_outcome": nop_out,
                     "reference_state": ref.get("state"),
                     "noop_state": nop.get("state"),
                     "reference_error": ref.get("last_error"),
                     "declared_artifacts": t["hb"]["artifacts"],
                     "n_cases": len(t["cases"])}
            if ref_out != "pass":
                entry["disposition"] = "skipped"
                entry["reason"] = (f"reference solution did not pass "
                                   f"(outcome={ref_out!r}, state={ref.get('state')!r}, "
                                   f"err={str(ref.get('last_error'))[:200]!r})")
                report["skipped_tasks"].append({"task": tid, **entry})
                report["tasks"][tid] = entry
                log(f"  SKIP {tid}: reference outcome={ref_out}")
                continue
            if nop_out == "pass":
                entry["disposition"] = "quarantined"
                entry["reason"] = ("no-op solution PASSED the verifier; task "
                                   "excluded from mutation statistics")
                report["quarantine"].append({"task": tid, **entry})
                report["tasks"][tid] = entry
                log(f"  QUARANTINE {tid}: noop PASSED")
                continue
            arts, missing = _arts_from(ref)
            entry["disposition"] = "included"
            entry["reference_artifact_files"] = len(arts)
            entry["reference_artifacts_missing"] = missing
            entry["reference_manifest_sha256"] = _manifest_hash(arts)
            report["tasks"][tid] = entry
            live[tid] = (t, arts, missing)
            log(f"  OK {tid}: ref=pass noop={nop_out} "
                f"({len(arts)} artifact files, {len(t['cases'])} cases)")

        log(f"[ablate] phase 2: {len(live)} task(s) eligible for mutation")

        # -------- phase 2: build mutants
        mut_trials, index = [], {}
        for tid, (t, arts, missing) in sorted(live.items()):
            declared = t["hb"]["artifacts"]
            needs_env = len(declared) >= 2
            env_state = env_image_state(t, declared) if needs_env else {}
            ref_hash = _manifest_hash(arts)
            built = 0
            for opname, fn, extra in build_operators(
                    declared, arts, args.max_files, args.max_ast):
                try:
                    new = fn(arts, declared, env_state)
                except Exception as e:
                    log(f"  [warn] {tid}/{opname}: operator error {e}")
                    continue
                if new is None:
                    continue
                mh = _manifest_hash(new)
                if mh == ref_hash:
                    continue          # ablation produced no state change
                mid = f"{opname}/{mh[:8]}"
                trial_id = f"mut/{tid}/{mid}"
                if trial_id in index:
                    continue
                enc = {k: base64.b64encode(v).decode("ascii")
                       for k, v in new.items()}
                spec = {"mode": "verify_only", "task_blob": t,
                        "outputs": [{"output": None, "artifacts": enc,
                                     "missing": _missing_for(new, declared)}]}
                mut_trials.append({"trial_id": trial_id, "task_id": tid,
                                   "replicate": 0, "spec": spec})
                changed = sorted(k for k in set(arts) | set(new)
                                 if arts.get(k) != new.get(k))
                index[trial_id] = {
                    "task": tid, "operator": opname, "mutant_id": mid,
                    "family": extra.get("kind", "artifact"),
                    "manifest_sha256": mh,
                    "reference_manifest_sha256": ref_hash,
                    "files_changed": changed,
                    "artifact_files_removed": sorted(set(arts) - set(new)),
                    "declared_missing_after": _missing_for(new, declared),
                    "bytes_before": sum(len(v) for v in arts.values()),
                    "bytes_after": sum(len(v) for v in new.values()),
                    "ast_edit": extra.get("edit"),
                    "_diff": _make_diff(opname, arts, new, extra)}
                built += 1
            report["tasks"][tid]["mutants_built"] = built
            log(f"  {tid}: {built} mutant(s)")

        log(f"[ablate] phase 3: {len(mut_trials)} mutant trials (verify_only)")
        muts = await submit_and_wait(
            pool, f"ablate-mut-{args.stamp}", "ablation:mutants",
            mut_trials, args.mutant_timeout, log)

        # -------- phase 3: tally
        for tid in report["tasks"]:
            if report["tasks"][tid].get("disposition") == "included":
                report["tasks"][tid].update(
                    {"mutants_run": 0, "survived": 0, "killed": 0,
                     "errored": 0, "survival_rate": None})
        for trial_id, meta in index.items():
            row = muts.get(trial_id) or {}
            outcome, state = row.get("outcome"), row.get("state")
            det = _detail(row)
            tid = meta["task"]
            e = report["tasks"][tid]
            rec = dict(meta)
            diff = rec.pop("_diff", None)
            rec.update({"trial_id": trial_id, "outcome": outcome,
                        "trial_state": state,
                        "verifier_cases": det.get("cases"),
                        "reward_txt": det.get("reward_txt"),
                        "artifacts_differ_from_reference":
                            meta["manifest_sha256"] != meta["reference_manifest_sha256"],
                        "storage": row.get("artifacts")})
            if meta["family"] == "ast_control":
                # Not a mutant: the unparse round-trip control for its file.
                rec["disposition"] = "ast_identity_control"
                rec["diff"] = diff
                e.setdefault("ast_identity_controls", []).append(
                    {"file": meta["files_changed"], "outcome": outcome,
                     "trial_id": trial_id})
                e.setdefault("mutant_records", []).append(rec)
                continue
            if outcome is None or outcome == "error":
                e["errored"] += 1
                rec["disposition"] = "errored"
            elif outcome == "pass":
                e["mutants_run"] += 1
                e["survived"] += 1
                rec["disposition"] = "survived"
                rec["diff"] = diff
                rec["verifier_stdout"] = fetch_verifier_stdout(row.get("artifacts"))
                report["surviving"].append(rec)
            else:
                e["mutants_run"] += 1
                e["killed"] += 1
                rec["disposition"] = "killed"
            e.setdefault("mutant_records", []).append(rec)
        for tid, e in report["tasks"].items():
            if e.get("disposition") == "included" and e.get("mutants_run"):
                e["survival_rate"] = round(e["survived"] / e["mutants_run"], 4)

        report["summary"] = {
            "tasks_loaded": len(tasks),
            "tasks_included": sum(1 for e in report["tasks"].values()
                                  if e.get("disposition") == "included"),
            "tasks_skipped_reference_failed": len(report["skipped_tasks"]),
            "tasks_quarantined_noop_passed": len(report["quarantine"]),
            "mutants_run": sum(e.get("mutants_run", 0)
                               for e in report["tasks"].values()),
            "mutants_survived": sum(e.get("survived", 0)
                                    for e in report["tasks"].values()),
            "mutants_killed": sum(e.get("killed", 0)
                                  for e in report["tasks"].values()),
            "mutants_errored": sum(e.get("errored", 0)
                                   for e in report["tasks"].values())}
        run = report["summary"]["mutants_run"]
        report["summary"]["overall_survival_rate"] = (
            round(report["summary"]["mutants_survived"] / run, 4) if run else None)
    finally:
        reaper.cancel()
        await pool.close()
        for p in procs:
            if p.poll() is None:
                p.terminate()

    report["log"] = log_lines
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    log(f"[ablate] wrote {args.out}")
    s = report.get("summary", {})
    log(f"[ablate] {s.get('tasks_included')} tasks | "
        f"{s.get('mutants_run')} mutants run | "
        f"{s.get('mutants_survived')} survived | "
        f"{s.get('mutants_killed')} killed | "
        f"{s.get('mutants_errored')} errored")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="external/harbor-tb/tasks")
    ap.add_argument("--sha", default="452bf305c6daa62fc59061d22133a7cbc7c1572e")
    ap.add_argument("--task", action="append", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-files", type=int, default=8,
                    help="per-task cap on drop_file mutants")
    ap.add_argument("--max-ast", type=int, default=8,
                    help="per-Python-artifact cap on AST mutants")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--control-timeout", type=float, default=3600)
    ap.add_argument("--mutant-timeout", type=float, default=3600)
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--out", default="mutation_report.json")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
