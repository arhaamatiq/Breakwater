"""Re-verify every recorded Harbor trial under reports/conformance with
Breakwater's runtime and write reports/conformance/SUMMARY.md.

Harbor's own verdicts come from `harbor run` (oracle, nop) and `harbor trial
regrade` (Breakwater mutants written into copies of the oracle trial). Trials
Harbor refused to score (e.g. regrade of a trial with a declared artifact
missing) are listed as not comparable.

Usage: uv run python scripts/conformance_report.py TASKS_DIR
"""
import glob
import json
import os
import subprocess
import sys

ROOT = "reports/conformance"


def main(tasks_dir):
    dirs = sorted(glob.glob(f"{ROOT}/harbor-jobs/*/*__*") + glob.glob(f"{ROOT}/harbor-regrades/*__*"))
    by_task = {}
    for d in dirs:
        if os.path.isfile(os.path.join(d, "result.json")):
            by_task.setdefault(os.path.basename(d).split("__")[0], []).append(d)
    rows = []
    for task, ds in sorted(by_task.items()):
        out = f"{ROOT}/{task}.json"
        subprocess.run([sys.executable, "-m", "breakwater.cli", "conform", *ds,
                        "--task-dir", os.path.join(tasks_dir, task), "--json", out])
        if os.path.exists(out):
            rows += [dict(r, task=task) for r in json.load(open(out))["rows"]]
    scored = [r for r in rows if r.get("agree") is not None]
    agree = sum(bool(r["agree"]) for r in scored)
    fails = sum(r["harbor_reward"] == 0 for r in scored)
    lines = ["# Conformance: Breakwater vs official Harbor", "",
             f"**{agree}/{len(scored)} verifications agree** with Harbor 0.25 on identical bytes "
             f"(reward and every per-check verdict) across {len({r['task'] for r in scored})} tasks; "
             f"{fails} of them are failing verdicts (no-ops and mutants regraded by "
             f"`harbor trial regrade`). {len(rows) - len(scored)} Harbor trials were not "
             f"comparable (Harbor recorded no reward, e.g. regrade refused a trial with a "
             f"missing declared artifact).", "",
             "| task | trial | Harbor reward | Breakwater reward | checks compared | agree |",
             "|---|---|---:|---:|---:|---|"]
    for r in rows:
        if r.get("agree") is None:
            lines.append(f"| {r['task']} | {r['trial']} | — ({r.get('harbor_exception')}) | — | — | n/a |")
        else:
            lines.append(f"| {r['task']} | {r['trial']} | {r['harbor_reward']} | "
                         f"{r['breakwater_reward']} | {r['checks_compared']} | "
                         f"{'yes' if r['agree'] else 'NO ' + ', '.join(r['check_mismatches'][:3])} |")
    with open(f"{ROOT}/SUMMARY.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(lines[2])


if __name__ == "__main__":
    main(sys.argv[1])
