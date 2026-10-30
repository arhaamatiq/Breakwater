"""Falsification receipts: self-contained, replayable evidence files.

A receipt carries everything needed to re-check a finding on any machine with
Docker: the violated property, minimized witnesses, and a list of replay
trials with expected outcomes. `replay()` re-executes them against the real
pipeline (no queue needed) and reports whether the violation still reproduces."""
import hashlib
import json
import os
import platform
import time

from ..config import CFG
from ..runner import run_trial


def _fingerprint() -> dict:
    return {"image": CFG.docker_image, "platform": platform.platform(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def write_receipt(finding: dict, out_dir: str = "receipts") -> str:
    os.makedirs(out_dir, exist_ok=True)
    body = {"receipt_version": 1, **finding, "fingerprint": _fingerprint()}
    rid = hashlib.sha256(json.dumps(
        {k: body[k] for k in ("task_id", "class", "witnesses")},
        sort_keys=True, default=str).encode()).hexdigest()[:12]
    body["receipt_id"] = rid
    body["repro"] = (f"uv run python -m breakwater.tester replay "
                     f"{out_dir}/{finding['task_id']}--{finding['class']}.json")
    path = os.path.join(out_dir, f"{finding['task_id']}--{finding['class']}.json")
    with open(path, "w") as f:
        json.dump(body, f, indent=2, default=str)
    return path


def _run(spec: dict, task_id: str, n: int) -> list[str]:
    outcomes = []
    for i in range(n):
        r = run_trial({"trial_id": f"replay/{task_id}/{i}", "experiment_id": "replay",
                       "task_id": task_id, "attempts": 0, "spec": spec})
        outcomes.append((r["outcome"], r["detail"]))
    return outcomes


def _check(expect: dict, runs: list) -> bool:
    outcomes = [o for o, _ in runs]
    if "outcome" in expect:
        return all(o == expect["outcome"] for o in outcomes)
    if "mixed_outcomes" in expect:
        return len(set(outcomes)) > 1
    if "mixed_case_verdicts" in expect:
        vectors = {tuple(c.get("ok") for c in d.get("cases", [])) for _, d in runs}
        return len(vectors) > 1
    if "case_ok" in expect:
        i = expect["case_ok"]
        return all(len(d.get("cases", [])) > i and
                   d["cases"][i].get("ok") == expect["equals"] for _, d in runs)
    return False


def replay(path: str, verbose=print) -> bool:
    receipt = json.load(open(path))
    verbose(f"receipt {receipt['receipt_id']}: {receipt['task_id']} "
            f"[{receipt['class']}] — {receipt['title']}")
    verbose(f"  property violated: {receipt['property']}: {receipt['property_text']}")
    quarantined = receipt.get("status") == "quarantined"
    if quarantined:
        # Triaged as a harness/adapter false positive (see receipts/quarantine/
        # README.md). Replaying still works, but what reproduces is the harness's
        # behaviour, not a defect in the benchmark.
        triage = receipt.get("triage") or {}
        verbose(f"  QUARANTINED [{triage.get('classification', '?')}]: "
                f"{receipt.get('quarantine_reason', '')}")
    if not receipt.get("replay"):
        verbose("  (no replay spec recorded — evidence-only receipt)")
        return True
    all_ok = True
    for item in receipt["replay"]:
        runs = _run(item["spec"], item["task_id"], item.get("n", 1))
        ok = _check(item["expect"], runs)
        all_ok = all_ok and ok
        verbose(f"  {'REPRODUCED' if ok else 'DID NOT REPRODUCE'}: {item['label']} "
                f"-> {[o for o, _ in runs]} (expected {item['expect']})")
    if quarantined:
        verbose(f"  => {'HARNESS BEHAVIOUR REPRODUCED' if all_ok else 'NOT REPRODUCED'} "
                f"(quarantined: not evidence of a benchmark defect)")
    else:
        verbose(f"  => {'VIOLATION CONFIRMED' if all_ok else 'NOT CONFIRMED'}")
    return all_ok
