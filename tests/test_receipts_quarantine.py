"""Quarantined receipts must never read as confirmed benchmark defects.

`receipts/quarantine/` holds findings triaged as harness/adapter false
positives. Replaying one still works (it is useful evidence of the harness
behaviour), but the verdict line must not say VIOLATION CONFIRMED.
"""
import glob
import json
import os

from breakwater.tester import receipts

QUARANTINE = os.path.join(os.path.dirname(__file__), "..", "receipts", "quarantine")


def _receipt(tmp_path, **extra):
    body = {"receipt_id": "abc", "task_id": "t", "class": "REFERENCE_REJECTED",
            "title": "Reference solution is rejected", "property": "P1",
            "property_text": "reference passes",
            "replay": [{"label": "oracle run", "task_id": "t", "n": 1,
                        "spec": {}, "expect": {"outcome": "fail"}}], **extra}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(body))
    return str(p)


def _replay(path, monkeypatch):
    monkeypatch.setattr(receipts, "_run", lambda spec, task_id, n: [("fail", {})] * n)
    lines = []
    ok = receipts.replay(path, verbose=lines.append)
    return ok, "\n".join(lines)


def test_normal_receipt_still_reports_violation_confirmed(tmp_path, monkeypatch):
    ok, out = _replay(_receipt(tmp_path), monkeypatch)
    assert ok and "VIOLATION CONFIRMED" in out and "QUARANTINED" not in out


def test_quarantined_receipt_is_flagged_and_not_confirmed(tmp_path, monkeypatch):
    path = _receipt(tmp_path, status="quarantined",
                    quarantine_reason="adapter false positive",
                    triage={"classification": "ADAPTER_LIMITATION"})
    ok, out = _replay(path, monkeypatch)
    assert ok                       # the harness behaviour does reproduce...
    assert "QUARANTINED [ADAPTER_LIMITATION]: adapter false positive" in out
    assert "VIOLATION CONFIRMED" not in out    # ...but it is not a finding
    assert "not evidence of a benchmark defect" in out


def test_every_quarantined_receipt_is_fully_annotated():
    paths = glob.glob(os.path.join(QUARANTINE, "*.json"))
    assert paths, "receipts/quarantine/ should not be empty"
    allowed = {"ADAPTER_LIMITATION", "ADAPTER_BUG", "INFRA", "UNRESOLVED"}
    for path in paths:
        r = json.load(open(path))
        name = os.path.basename(path)
        assert r.get("status") == "quarantined", name
        assert r.get("quarantine_reason"), name
        triage = r.get("triage") or {}
        # CONFIRMED_BENCHMARK_DEFECT receipts do not belong here
        assert triage.get("classification") in allowed, name
        for key in ("mechanism", "evidence", "reproduced", "not_verified"):
            assert triage.get(key), f"{name}: triage.{key} missing"


def test_no_quarantined_receipt_is_left_in_the_live_receipts_dir():
    live = os.path.join(QUARANTINE, "..")
    for path in glob.glob(os.path.join(live, "*.json")):
        assert json.load(open(path)).get("status") != "quarantined", path
