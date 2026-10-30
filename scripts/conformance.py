"""Differential conformance sweep: Breakwater's runtime vs the official harness.

For each task:
  1. `harbor run -a oracle` and `-a nop` produce real Harbor trials.
  2. Breakwater derives deterministic mutants from the oracle trial's bytes
     (the same corpus a Trust Diff uses), writes each into a copy of the
     oracle trial, and `harbor trial regrade` re-verifies it with Harbor's
     own verifier code.
  3. `breakwater conform` re-verifies every one of those trials with
     Breakwater's runtime on the identical bytes and compares rewards and
     per-check verdicts.

Usage: uv run python scripts/conformance.py --tasks-dir DIR --tasks a,b --harbor PATH/TO/harbor
       [--max-mutants 6] [--out reports/conformance]
Harbor must be installed separately (it needs Python >= 3.12), e.g.
`uv tool install harbor==0.25.0`.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

from breakwater.adapters.harbor_task import artifact_paths, parse_task
from breakwater.bundle import from_harbor_trial, to_harbor_trial
from breakwater.runner import _isolated_docker_config
from breakwater.trustdiff.corpus import mutants


def sh(cmd, log):
    with open(log, "a") as f:
        f.write("$ " + " ".join(cmd) + "\n")
        f.flush()
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode


def newest_trial(jobs_dir, task):
    c = sorted(glob.glob(os.path.join(jobs_dir, "*", f"{task}__*")), key=os.path.getmtime)
    return c[-1] if c else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--harbor", default="harbor")
    ap.add_argument("--max-mutants", type=int, default=6)
    ap.add_argument("--out", default="reports/conformance")
    a = ap.parse_args()
    os.environ["DOCKER_CONFIG"] = _isolated_docker_config()
    out = os.path.abspath(a.out)
    jobs = os.path.join(out, "harbor-jobs")
    regr = os.path.join(out, "harbor-regrades")
    os.makedirs(jobs, exist_ok=True)
    os.makedirs(regr, exist_ok=True)
    log = os.path.join(out, "harbor.log")
    summary = {}
    for task in a.tasks.split(","):
        tdir = os.path.join(os.path.abspath(a.tasks_dir), task)
        spec = parse_task(tdir)
        trials = []
        for agent in ("oracle", "nop"):
            sh([a.harbor, "run", "-p", tdir, "-a", agent, "-o", jobs, "--yes"], log)
            t = newest_trial(jobs, task)
            if t and t not in trials:
                trials.append(t)
        oracle = trials[0] if trials else None
        if oracle:
            ref = from_harbor_trial(oracle, artifact_paths(spec))
            items = [m for m in mutants(ref, None, max_files=4, max_ast_per_file=3,
                                        max_ast_files=1)][: a.max_mutants]
            for it in items:
                safe = it.label.replace("/", "_").replace(":", "_")[:80]
                src = os.path.join(regr, f"{task}--{safe}--src")
                if not os.path.exists(src):
                    to_harbor_trial(it.bundle, oracle, src)
                before = set(glob.glob(os.path.join(regr, "*")))
                sh([a.harbor, "trial", "regrade", src, "-p", tdir, "-o", regr], log)
                new = sorted(set(glob.glob(os.path.join(regr, "*"))) - before, key=os.path.getmtime)
                if new:
                    trials.append(new[-1])
        res = os.path.join(out, f"{task}.json")
        rc = subprocess.run([sys.executable, "-m", "breakwater.cli", "conform", *trials,
                             "--task-dir", tdir, "--json", res]).returncode
        summary[task] = {"trials": len(trials), "agree": rc == 0,
                         "rows": json.load(open(res))["rows"] if os.path.exists(res) else []}
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    n = sum(len(v["rows"]) for v in summary.values())
    ok = sum(r["agree"] for v in summary.values() for r in v["rows"])
    fails = sum(r["harbor_reward"] == 0 for v in summary.values() for r in v["rows"])
    print(f"conformance: {ok}/{n} verifications agree with Harbor across {len(summary)} tasks "
          f"({fails} of them failing verdicts)")


if __name__ == "__main__":
    main()
