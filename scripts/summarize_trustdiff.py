"""Aggregate several Trust Diff runs into one summary (Markdown + JSON).

Usage: uv run python scripts/summarize_trustdiff.py OUT.md RUN_DIR [RUN_DIR ...]

Counts tasks, bundles and verify cells per run, tallies change classes, and
lists every reportable change with a link to its run's REPORT.md. When the
same task appears in several runs, every run is listed (they answer different
questions: base/head refs, corpora and fixtures differ).
"""
import collections
import json
import os
import sys

from breakwater.trustdiff.report import EXPLAIN, REPORTABLE, SEV_ORDER


def main(out, runs):
    rows, changes, classes = [], [], collections.Counter()
    tot_cells = tot_bundles = 0
    tasks = set()
    for rd in runs:
        d = json.load(open(os.path.join(rd, "trustdiff.json")))
        def short(v):
            r = v.get("ref") or v.get("tasks_dir") or ""
            return r[:10] if len(r) == 40 and all(c in "0123456789abcdef" for c in r) else r
        base, head = short(d["base"]), short(d["head"])
        for t in d["tasks"]:
            bs = t.get("bundles", [])
            # identical verifiers (e.g. a single-version re-judgement) run one arm
            same = t.get("verifier_changed") is False
            cells = sum(b["base"]["n"] + (0 if same else b["head"]["n"]) for b in bs)
            tot_cells += cells
            tot_bundles += len(bs)
            tasks.add(t["task"])
            c = collections.Counter(b["class"] for b in bs)
            classes.update(c)
            rows.append((t["task"], f"{base} → {head}", len(bs), cells,
                         ", ".join(f"{k}×{v}" for k, v in sorted(c.items()) if k != "unchanged") or "none",
                         os.path.relpath(os.path.join(rd, "REPORT.md"), os.path.dirname(out))))
            for b in bs:
                if b["class"] in REPORTABLE:
                    changes.append((b["severity"], t["task"], b["label"], b["class"],
                                    f"{b['base']['pass']}/{b['base']['n']}",
                                    f"{b['head']['pass']}/{b['head']['n']}",
                                    os.path.relpath(os.path.join(rd, "REPORT.md"), os.path.dirname(out))))
    changes.sort(key=lambda c: (SEV_ORDER.index(c[0]), c[1], c[2]))
    lines = ["# Trust Diff results", "",
             f"{len(tasks)} tasks · {tot_bundles} bundles · {tot_cells} verify cells "
             f"(each cell = one bundle judged once by one verifier version)", "",
             "| task | versions | bundles | cells | changes | report |", "|---|---|---:|---:|---|---|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | [link]({r[5]}) |" for r in rows]
    lines += ["", "## Classes", "", "| class | bundles | meaning |", "|---|---:|---|"]
    lines += [f"| {k} | {v} | {EXPLAIN.get(k, 'no verdict change')} |" for k, v in classes.most_common()]
    lines += ["", "## Every reportable change", "",
              "| severity | task | bundle | class | base pass | head pass | report |",
              "|---|---|---|---|---:|---:|---|"]
    lines += [f"| {c[0]} | {c[1]} | `{c[2][:60]}` | {c[3]} | {c[4]} | {c[5]} | [link]({c[6]}) |"
              for c in changes]
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(out.replace(".md", ".json"), "w") as f:
        json.dump({"tasks": len(tasks), "bundles": tot_bundles, "cells": tot_cells,
                   "classes": dict(classes), "changes": changes}, f, indent=2)
    print(f"{out}: {len(tasks)} tasks, {tot_bundles} bundles, {tot_cells} cells, "
          f"{len(changes)} reportable changes")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
