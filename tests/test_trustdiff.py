"""Trust Diff logic without Docker: statistics, classification, corpus, fixtures,
report/receipt rendering. A bug here mislabels a verifier change, so each
classification rule is pinned explicitly."""
import json
import math

import pytest

from breakwater import storage
from breakwater.bundle import Bundle, Entry
from breakwater.trustdiff import stats
from breakwater.trustdiff.corpus import CorpusItem, mutants, patched
from breakwater.trustdiff.engine import _transition, classify, patch_solution
from breakwater.trustdiff.report import export, render


# --------------------------------------------------------------- statistics

def test_wilson_bounds():
    lo, hi = stats.wilson(5, 5)
    assert 0.5 < lo < 0.6 and hi == 1.0
    assert stats.wilson(0, 0) == (0.0, 1.0)


def test_fisher_exact_known_values():
    # all-vs-none at 5 per side: 2 / C(10,5)
    assert math.isclose(stats.fisher_exact(5, 0, 0, 5), 2 / 252, rel_tol=1e-9)
    assert stats.fisher_exact(3, 2, 3, 2) == pytest.approx(1.0)
    assert stats.fisher_exact(2, 0, 0, 2) == pytest.approx(1 / 3)


def test_bootstrap_ratio_detects_shift_and_not_noise():
    base = [100, 102, 98, 101, 99, 100, 103]
    shifted = [x * 0.7 for x in base]
    point, lo, hi = stats.bootstrap_ratio_ci(base, shifted)
    assert point == pytest.approx(0.7) and hi < 1.0
    point, lo, hi = stats.bootstrap_ratio_ci(base, list(reversed(base)))
    assert lo <= 1.0 <= hi


def test_mann_whitney():
    assert stats.mann_whitney_p([1, 2, 3, 4, 5], [6, 7, 8, 9, 10]) < 0.02
    assert stats.mann_whitney_p([1, 2, 3], [1, 2, 3]) > 0.5


# --------------------------------------------------------------- classification

@pytest.mark.parametrize("expect,b,h,klass", [
    ("pass", "PASS", "PASS", "unchanged"),
    ("pass", "PASS", "FAIL", "false_fail_introduced"),
    ("pass", "FAIL", "PASS", "false_fail_fixed"),
    ("fail", "FAIL", "PASS", "false_pass_introduced"),
    ("fail", "PASS", "FAIL", "false_pass_fixed"),
    ("fail", "PASS", "PASS", "false_pass_both"),
    ("pass", "FAIL", "FAIL", "false_fail_both"),
    (None, "FAIL", "PASS", "strictness_regression"),
    (None, "PASS", "FAIL", "hardening"),
    (None, "PASS", "PASS", "survives_both"),
    (None, "FAIL", "FAIL", "unchanged"),
    ("fail", "FAIL", "ERROR", "verifier_error_introduced"),
    ("fail", "ERROR", "FAIL", "verifier_error_fixed"),
    ("pass", "PASS", "MIXED", "instability_introduced"),
    ("pass", "MIXED", "PASS", "instability_removed"),
    ("pass", "MIXED", "MIXED", "unstable_both"),
    ("pass", "NONE", "PASS", "incomplete"),
])
def test_transition_table(expect, b, h, klass):
    assert _transition(expect, b, h)[0] == klass


def _rows(outs, metric=None, checks=None):
    rows = []
    for i, o in enumerate(outs):
        d = {"cases": [{"name": n, "ok": ok} for n, ok in (checks or {}).items()],
             "verifier_wall_s": 1.0 + i * 0.01,
             "error_kind": "no_reward" if o == "error" else None}
        if metric is not None:
            d["metrics"] = {"speedup": metric[i]}
        rows.append({"state": "completed", "outcome": o, "detail": d})
    return rows


def test_classify_deterministic_flip_with_check_attribution():
    item = CorpusItem("noop", "noop", Bundle({}, ["/a"]), {})
    r = classify(item, _rows(["fail"] * 5, checks={"t_exists": False}),
                 _rows(["pass"] * 5, checks={"t_exists": True}))
    assert r["class"] == "false_pass_introduced" and r["severity"] == "CRITICAL"
    assert r["deterministic"] is True and r["fisher_p"] < 0.01
    assert r["checks_moved"] == [{"check": "t_exists", "base_pass_rate": 0.0,
                                  "head_pass_rate": 1.0}]


def test_classify_needs_five_per_side_to_call_deterministic():
    item = CorpusItem("reference", "reference", Bundle({}, ["/a"]), {})
    r = classify(item, _rows(["pass"] * 2), _rows(["fail"] * 2))
    assert r["class"] == "false_fail_introduced" and r["deterministic"] is False


def test_classify_metric_shift_flags_systematic_change():
    item = CorpusItem("reference", "reference", Bundle({}, ["/a"]), {})
    r = classify(item, _rows(["pass"] * 6, metric=[100, 101, 99, 100, 102, 98]),
                 _rows(["pass"] * 6, metric=[60, 61, 59, 60, 62, 58]))
    assert r["class"] == "unchanged"
    assert r["metric_shifts"]["speedup"]["systematic"]
    assert r["metric_shifts"]["speedup"]["ratio"] == pytest.approx(0.6, rel=0.02)


def test_fixture_expectation_overrides_kind_default():
    item = CorpusItem("fixture::x", "fixture", Bundle({}, ["/a"]), {"expect": "fail"})
    assert item.expect == "fail"
    assert classify(item, _rows(["fail"] * 5), _rows(["pass"] * 5))["class"] == "false_pass_introduced"


# --------------------------------------------------------------- corpus

def _ref():
    return Bundle({
        "/app": Entry("d", 0o755),
        "/app/solve.py": Entry("f", 0o644, b"def f(x):\n    return x < 3\n"),
        "/app/out.json": Entry("f", 0o644, b'{"n": 12}'),
        "/app/notes.txt": Entry("f", 0o644, b"ok"),
    }, ["/logs/artifacts", "/app/"])


def test_mutants_are_deterministic_and_distinct():
    a, b = mutants(_ref(), None), mutants(_ref(), None)
    assert [m.bundle.digest for m in a] == [m.bundle.digest for m in b]
    assert len({m.bundle.digest for m in a}) == len(a)
    labels = {m.label for m in a}
    assert {"drop_all", "zero_all", "halve_all", "digits_all"} <= labels
    assert any(lbl.startswith("ast::/app/solve.py") for lbl in labels)
    ident = [m for m in a if m.kind == "ast_identity"]
    assert ident and all(m.meta["control"] == ident[0].bundle.digest
                         for m in a if m.label.startswith("ast::"))


def test_patched_fixture_applies_unified_diff():
    diff = ("--- a/app/out.json\n+++ b/app/out.json\n@@ -1 +1 @@\n"
            '-{"n": 12}\n\\ No newline at end of file\n+{"n": 13}\n\\ No newline at end of file\n')
    it = patched(_ref(), "off-by-one", "fail", diff)
    assert it.bundle.files()["/app/out.json"] == b'{"n": 13}'
    assert it.expect == "fail" and it.kind == "fixture"


def test_patch_solution_rewrites_tree():
    import base64
    task = {"hb": {"solution_tree": {"solve.sh": [base64.b64encode(b"echo 1\n").decode(), 0o755]}}}
    diff = "--- a/solution/solve.sh\n+++ b/solution/solve.sh\n@@ -1 +1 @@\n-echo 1\n+echo 2\n"
    out = patch_solution(task, diff)
    assert base64.b64decode(out["hb"]["solution_tree"]["solve.sh"][0]) == b"echo 2\n"
    assert out["hb"]["solution_tree"]["solve.sh"][1] == 0o755
    assert base64.b64decode(task["hb"]["solution_tree"]["solve.sh"][0]) == b"echo 1\n"


# --------------------------------------------------------------- report + receipts

def test_export_writes_bundle_receipt_and_report(tmp_path):
    storage.configure(f"file://{tmp_path / 'store'}")
    try:
        ref = _ref()
        storage.put_blob(ref.to_tar())
        item = CorpusItem("reference", "reference", ref, {})
        b = classify(item, _rows(["pass"] * 5), _rows(["error"] * 5))
        result = {
            "run_id": "td-test", "generated_at": "now",
            "base": {"ref": "v1", "commit": "a" * 40}, "head": {"ref": "v2", "commit": "b" * 40},
            "config": {"repeats": 2, "max_repeats": 5, "solve_from": "base", "network": None},
            "tasks": [{"task": "t1", "base": {"commit": "a" * 40, "verifier_digest": "x"},
                       "head": {"commit": "b" * 40, "verifier_digest": "y"},
                       "check_inventory": {"base": 1, "head": 1, "removed": [], "added": []},
                       "notes": [], "bundles": [b]}],
        }
        meta = export(result, str(tmp_path / "out"))
        rec = json.load(open(tmp_path / "out" / meta["receipts"][0]))
        assert rec["class"] == "verifier_error_introduced"
        assert rec["replay"]["expect"] == {"base": "PASS", "head": "ERROR"}
        tar = (tmp_path / "out" / rec["bundle"]["file"]).read_bytes()
        assert storage.put_blob(tar) == rec["bundle"]["digest"]
        md = (tmp_path / "out" / "REPORT.md").read_text()
        assert "verifier_error_introduced" in md and "breakwater replay" in md
        assert render(result).startswith("# Verifier Trust Diff")
    finally:
        storage.configure(None)
