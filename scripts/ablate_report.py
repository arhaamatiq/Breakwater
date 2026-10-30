"""Assemble mutation_report.json from every ablation trial in the result ledger.

Reads across ALL ablation runs (trial ids are deterministic, so a mutant that
ran in an earlier sweep is the same row), rebuilds each mutant's metadata by
re-deriving it from the same operator code, and emits the deliverable.

Nothing here re-executes anything or judges a benchmark. It reports what the
verifier returned.
"""
import argparse
import asyncio
import base64
import json
import time

from projectx import db
from projectx.adapters import harbor
from ablate import (_manifest_hash, _missing_for, _make_diff, build_operators,
                    fetch_verifier_stdout, env_image_state, _detail, _arts_from)


async def main_async(args):
    tasks = harbor.load(args.root, ids=args.task or None)
    load_report = json.loads(json.dumps(harbor.LAST_LOAD_REPORT))
    pool = await db.make_pool(max_size=6)

    rows = await pool.fetch(
        """SELECT t.trial_id, t.state, t.attempts, r.outcome, r.detail,
                  r.artifacts, r.exec_ms
           FROM trials t LEFT JOIN results r USING (trial_id)
           WHERE t.trial_id LIKE 'mut/%'""")
    by_id = {r["trial_id"]: dict(r) for r in rows}
    await pool.close()

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "corpus": {
            "benchmark": "Terminal-Bench v4.0.0 (Harbor)",
            "repo": "harbor-framework/terminal-bench",
            "clone_sha": args.sha,
            "root": args.root,
            "tasks_in_corpus": 66,
        },
        "attack": {
            "method": "deterministic ablation of the shipped reference solution",
            "scoring": "each task's own tests/test.sh, run verbatim; CTRF parsed",
            "no_llm_in_loop": True,
            "mutant_id_scheme":
                "mut/<task>/<operator>/<sha8 of mutant artifact manifest> — "
                "deterministic, so reruns dedupe through the exactly-once path",
        },
        "load_report": load_report,
        "tasks": {}, "surviving": [], "quarantine": [], "skipped_tasks": [],
    }

    for tid, t in sorted(tasks.items()):
        declared = t["hb"]["artifacts"]
        ref = by_id.get(f"mut/{tid}/__control__/reference") or {}
        nop = by_id.get(f"mut/{tid}/__control__/noop") or {}
        e = {"reference_outcome": ref.get("outcome"),
             "reference_trial_state": ref.get("state"),
             "noop_outcome": nop.get("outcome"),
             "noop_trial_state": nop.get("state"),
             "declared_artifacts": declared,
             "n_cases": len(t["cases"])}
        report["tasks"][tid] = e

        if ref.get("outcome") != "pass":
            e["disposition"] = "skipped"
            e["reason"] = (f"reference solution did not pass "
                           f"(outcome={ref.get('outcome')!r}, "
                           f"trial_state={ref.get('state')!r})")
            report["skipped_tasks"].append({"task": tid, **e})
            continue
        if nop.get("outcome") == "pass":
            e["disposition"] = "quarantined"
            e["reason"] = ("CONTROL VIOLATION: the empty/no-op solution PASSED "
                           "this task's verifier. Excluded from all mutation "
                           "statistics; needs review before any use.")
            report["quarantine"].append({"task": tid, **e})
            continue

        arts, missing = _arts_from(ref)
        e["disposition"] = "included"
        e["reference_artifact_files"] = len(arts)
        e["reference_manifest_sha256"] = _manifest_hash(arts)
        e.update({"mutants_built": 0, "mutants_run": 0, "survived": 0,
                  "killed": 0, "errored": 0, "not_run": 0,
                  "survival_rate": None})

        env_state = env_image_state(t, declared) if len(declared) >= 2 else {}
        ref_hash = _manifest_hash(arts)
        seen = set()
        for opname, fn, extra in build_operators(declared, arts,
                                                 args.max_files, args.max_ast):
            try:
                new = fn(arts, declared, env_state)
            except Exception:
                continue
            if new is None:
                continue
            mh = _manifest_hash(new)
            if mh == ref_hash:
                continue
            trial_id = f"mut/{tid}/{opname}/{mh[:8]}"
            if trial_id in seen:
                continue
            seen.add(trial_id)
            row = by_id.get(trial_id)
            if row is None:
                e["not_run"] += 1
                continue
            e["mutants_built"] += 1
            det = _detail(row)
            outcome = row.get("outcome")
            rec = {"task": tid, "operator": opname, "trial_id": trial_id,
                   "family": extra.get("kind", "artifact"),
                   "ast_edit": extra.get("edit"),
                   "manifest_sha256": mh,
                   "reference_manifest_sha256": ref_hash,
                   "files_changed": sorted(k for k in set(arts) | set(new)
                                           if arts.get(k) != new.get(k)),
                   "artifact_files_removed": sorted(set(arts) - set(new)),
                   "declared_missing_after": _missing_for(new, declared),
                   "bytes_before": sum(len(v) for v in arts.values()),
                   "bytes_after": sum(len(v) for v in new.values()),
                   "outcome": outcome, "trial_state": row.get("state"),
                   "exec_ms": row.get("exec_ms"),
                   "verifier_cases": det.get("cases"),
                   "reward_txt": det.get("reward_txt"),
                   # Recorded per protocol; NOT used to classify anything.
                   "artifacts_differ_from_reference": mh != ref_hash}

            if extra.get("kind") == "ast_control":
                e["mutants_built"] -= 1
                rec["disposition"] = "ast_identity_control"
                e.setdefault("ast_identity_controls", []).append(
                    {"file": extra.get("file"), "outcome": outcome,
                     "trial_id": trial_id,
                     "note": ("PASS means the ast.unparse round-trip is faithful "
                              "for this file, so its AST mutants are informative. "
                              "Any other outcome means they are not.")})
                continue
            if outcome is None or outcome == "error":
                e["errored"] += 1
                rec["disposition"] = "errored"
            elif outcome == "pass":
                e["mutants_run"] += 1
                e["survived"] += 1
                rec["disposition"] = "survived"
                rec["diff"] = _make_diff(opname, arts, new, extra)
                rec["verifier_stdout"] = fetch_verifier_stdout(row.get("artifacts"))
                report["surviving"].append(rec)
            else:
                e["mutants_run"] += 1
                e["killed"] += 1
                rec["disposition"] = "killed"
            e.setdefault("mutant_records", []).append(rec)

        if e["mutants_run"]:
            e["survival_rate"] = round(e["survived"] / e["mutants_run"], 4)

    inc = [v for v in report["tasks"].values() if v.get("disposition") == "included"]
    run = sum(v["mutants_run"] for v in inc)
    report["summary"] = {
        "tasks_loaded_by_adapter": len(tasks),
        "tasks_included": len(inc),
        "tasks_skipped_reference_failed": len(report["skipped_tasks"]),
        "tasks_quarantined_noop_passed": len(report["quarantine"]),
        "mutants_run": run,
        "mutants_survived": sum(v["survived"] for v in inc),
        "mutants_killed": sum(v["killed"] for v in inc),
        "mutants_errored": sum(v["errored"] for v in inc),
        "mutants_built_not_run": sum(v["not_run"] for v in inc),
        "overall_survival_rate": (round(sum(v["survived"] for v in inc) / run, 4)
                                  if run else None),
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(json.dumps(report["summary"], indent=1))
    print(f"-> {args.out}")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="external/harbor-tb/tasks")
    ap.add_argument("--sha", default="452bf305c6daa62fc59061d22133a7cbc7c1572e")
    ap.add_argument("--task", action="append", default=None)
    ap.add_argument("--max-files", type=int, default=8)
    ap.add_argument("--max-ast", type=int, default=8)
    ap.add_argument("--out", default="mutation_report.json")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
