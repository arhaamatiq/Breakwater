"""Render a Trust Diff result as Markdown + JSON, export evidence, write receipts.

Every reported change ships with the exact bundle bytes (bundles/<hex>.tar)
and a receipt that re-runs both verifier versions on them, so a reader can
reproduce a claim with nothing but Docker, the benchmark repo and this
directory.
"""
from __future__ import annotations

import json
import os

from .. import cas

SEV_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "NONE"]
REPORTABLE = {"false_pass_introduced", "false_fail_introduced", "strictness_regression",
              "verifier_error_introduced", "instability_introduced", "unstable_both",
              "false_pass_both", "false_fail_both", "survives_both", "hardening",
              "false_fail_fixed", "false_pass_fixed", "verifier_error_fixed",
              "instability_removed"}
EXPLAIN = {
    "false_pass_introduced": "a known-bad output (no-op / degenerate) now PASSES",
    "false_fail_introduced": "a known-good output (reference / control) now FAILS",
    "strictness_regression": "a mutant the base verifier rejected now passes",
    "verifier_error_introduced": "the head verifier errors (timeout / no reward) where base gave a verdict",
    "instability_introduced": "verdict became nondeterministic on identical bytes",
    "unstable_both": "nondeterministic under both versions",
    "false_pass_both": "a known-bad output passes under both versions",
    "false_fail_both": "a known-good output fails under both versions",
    "survives_both": "mutant passes under both versions (candidate test gap)",
    "hardening": "a mutant the base verifier accepted is now rejected",
    "false_fail_fixed": "a known-good output that failed now passes",
    "false_pass_fixed": "a known-bad output that passed is now rejected",
    "verifier_error_fixed": "head gives a verdict where base errored",
    "instability_removed": "verdict became deterministic",
}


def _fmt_arm(a: dict) -> str:
    s = f"{a['pass']}/{a['n']} pass"
    if a.get("error"):
        s += f", {a['error']} err" + (f" ({','.join(a['error_kinds'])})" if a.get("error_kinds") else "")
    return s


def export(result: dict, out_dir: str, cli: str = "breakwater") -> dict:
    os.makedirs(os.path.join(out_dir, "bundles"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "receipts"), exist_ok=True)
    # a re-run into the same directory replaces the evidence, it never mixes runs
    for name in os.listdir(os.path.join(out_dir, "receipts")):
        os.remove(os.path.join(out_dir, "receipts", name))
    keep = {b["bundle"].split(":")[1][:16] + ".tar" for t in result["tasks"]
            for b in t.get("bundles", []) if b["class"] in REPORTABLE}
    for name in os.listdir(os.path.join(out_dir, "bundles")):
        if name not in keep:
            os.remove(os.path.join(out_dir, "bundles", name))
    receipts = []
    for t in result["tasks"]:
        for b in t.get("bundles", []):
            if b["class"] not in REPORTABLE:
                continue
            hexd = b["bundle"].split(":")[1]
            tar_path = os.path.join(out_dir, "bundles", f"{hexd[:16]}.tar")
            if not os.path.exists(tar_path):
                with open(tar_path, "wb") as f:
                    f.write(cas.get(b["bundle"]))
            rec = {
                "receipt_version": 2, "kind": "trustdiff",
                "task": t["task"], "class": b["class"], "severity": b["severity"],
                "label": b["label"], "bundle_kind": b["kind"], "expect": b["expect"],
                "claim": EXPLAIN.get(b["class"], b["class"]),
                "bundle": {"digest": b["bundle"], "file": f"bundles/{hexd[:16]}.tar"},
                "versions": {"base": t["base"], "head": t["head"]},
                "observed": {"base": b["base"]["outcomes"], "head": b["head"]["outcomes"],
                             "fisher_p": b["fisher_p"]},
                "checks_moved": b["checks_moved"][:20], "metric_shifts": b["metric_shifts"],
                "meta": b["meta"],
                "replay": {"expect": {"base": b["base"]["state"], "head": b["head"]["state"]},
                           "n": max(3, min(5, b["base"]["n"]))},
            }
            name = f"{t['task']}--{b['class']}--{hexd[:8]}.json"
            path = os.path.join(out_dir, "receipts", name)
            with open(path, "w") as f:
                json.dump(rec, f, indent=2, default=str)
            receipts.append(os.path.relpath(path, out_dir))
            b["receipt"] = receipts[-1]
    with open(os.path.join(out_dir, "trustdiff.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    with open(os.path.join(out_dir, "REPORT.md"), "w") as f:
        f.write(render(result, cli))
    return {"receipts": receipts}


def summarize(result: dict) -> list[dict]:
    rows = []
    for t in result["tasks"]:
        bs = t.get("bundles", [])
        counts = {}
        for b in bs:
            counts[b["class"]] = counts.get(b["class"], 0) + 1
        ref = next((b for b in bs if b["kind"] == "reference"), None)
        rows.append({"task": t["task"], "bundles": len(bs), "counts": counts,
                     "reference": (f"{ref['base']['state']}→{ref['head']['state']}" if ref else "—"),
                     "notes": t.get("notes", []),
                     "inventory": t.get("check_inventory")})
    return rows


def render(result: dict, cli: str = "breakwater") -> str:
    b, h = result["base"], result["head"]
    cfg = result["config"]
    lines = [
        "# Verifier Trust Diff",
        "",
        f"- **base** `{b.get('ref') or b.get('tasks_dir')}` ({(b.get('commit') or '')[:12]})",
        f"- **head** `{h.get('ref') or h.get('tasks_dir')}` ({(h.get('commit') or '')[:12]})",
        f"- repeats {cfg['repeats']} per version, contested bundles topped up to "
        f"{cfg['max_repeats']}; outputs solved under `{cfg['solve_from']}`; network: "
        f"{cfg['network'] or 'task policy'}",
        f"- run `{result['run_id']}` generated {result['generated_at']}",
        "",
        "Every bundle is a fixed set of artifact bytes. Both verifier versions judge the "
        "same bytes; the verdict is the task's reward exactly as Harbor records it.",
        "",
        "## Summary",
        "",
        "| task | bundles | reference | changes | notes |",
        "|---|---:|---|---|---|",
    ]
    for r in summarize(result):
        ch = ", ".join(f"{k}×{v}" for k, v in sorted(r["counts"].items())
                       if k not in ("unchanged",)) or "none"
        note = "; ".join(r["notes"])[:120]
        lines.append(f"| {r['task']} | {r['bundles']} | {r['reference']} | {ch} | {note} |")
    findings = [(t, x) for t in result["tasks"] for x in t.get("bundles", [])
                if x["class"] in REPORTABLE]
    findings.sort(key=lambda p: (SEV_ORDER.index(p[1]["severity"]), p[0]["task"], p[1]["label"]))
    lines += ["", "## Changes", ""]
    if not findings:
        lines.append("No bundle changed verdict between versions.")
    for t, x in findings:
        det = " — deterministic" if x.get("deterministic") else ""
        lines += [
            f"### [{x['severity']}] {t['task']}: `{x['label']}` — {x['class']}",
            "",
            f"{EXPLAIN.get(x['class'], '')}. Bundle kind `{x['kind']}`, expected "
            f"`{x['expect'] or 'unknown'}`.",
            "",
            f"- base: {_fmt_arm(x['base'])} · head: {_fmt_arm(x['head'])} · Fisher p = "
            f"{x['fisher_p']:.4f}{det}",
        ]
        if x["checks_moved"]:
            mv = ", ".join(f"`{c['check']}` {_r(c['base_pass_rate'])}→{_r(c['head_pass_rate'])}"
                           for c in x["checks_moved"][:8])
            lines.append(f"- checks that moved: {mv}")
        for m, s in x["metric_shifts"].items():
            if s["systematic"]:
                lines.append(f"- metric `{m}`: head/base = {s['ratio']:.3f} "
                             f"(95% CI {s['ci95'][0]:.3f}–{s['ci95'][1]:.3f})")
        if x.get("wall_ratio"):
            w = x["wall_ratio"]
            lines.append(f"- verifier wall time head/base = {w['ratio']:.2f} "
                         f"(95% CI {w['ci95'][0]:.2f}–{w['ci95'][1]:.2f})")
        if x["meta"].get("diff"):
            lines += ["", "```diff", x["meta"]["diff"].strip()[:1500], "```"]
        if x.get("receipt"):
            lines += ["", f"Replay: `{cli} replay {x['receipt']}`"]
        lines.append("")
    inv = [(t["task"], t["check_inventory"]) for t in result["tasks"]
           if t.get("check_inventory") and (t["check_inventory"]["removed"] or t["check_inventory"]["added"])]
    if inv:
        lines += ["## Check inventory changes (static)", "",
                  "Names discovered in each version's tests (pytest ids, RewardKit criteria). "
                  "A rename is not a weakening by itself; use the bundle results above.", ""]
        for name, i in inv:
            lines.append(f"- **{name}**: {i['base']}→{i['head']} checks; removed "
                         f"{len(i['removed'])}, added {len(i['added'])}")
    return "\n".join(lines) + "\n"


def _r(x):
    return "—" if x is None else f"{x:.2f}"
