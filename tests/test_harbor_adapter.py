"""Harbor runtime: the pieces that decide verdicts, pinned without Docker.

Each test encodes one rule of Harbor's verifier contract that Breakwater must
reproduce exactly; a regression here is a fidelity bug, which is worse than a
crash because it silently changes rewards.
"""
import io
import json
import os
import tarfile
import textwrap

from breakwater.adapters.harbor import (_align, _criteria, _empty_dirs_cmd, _pytest_ids,
                                        _verdict, discover_cases, primary_reward)
from breakwater.adapters.harbor_task import NET_FETCH_RE, parse_task, unsupported_reason
from breakwater.bundle import Bundle, Entry, add_archive, upload_tar


# --- reward is authoritative ------------------------------------------------

def test_reward_json_wins_over_reward_txt():
    v = _verdict(1, 3.0, 900, {"reward.json": b'{"reward": 1.0}', "reward.txt": b"0\n"}, {})
    assert v["outcome"] == "pass" and v["reward_source"] == "reward.json"


def test_exit_code_ignored_reward_txt_decides():
    # test.sh exited 1 (pytest failures) but wrote reward 1 -> pass, as Harbor
    v = _verdict(1, 1.0, 900, {"reward.txt": b"1\n"}, {})
    assert v["outcome"] == "pass" and v["primary_reward"] == 1.0
    v = _verdict(0, 1.0, 900, {"reward.txt": b"0\n"}, {})
    assert v["outcome"] == "fail"


def test_missing_reward_is_error_not_fail():
    v = _verdict(0, 1.0, 900, {"ctrf.json": b'{"results":{"tests":[]}}'}, {})
    assert v["outcome"] == "error" and v["error_kind"] == "no_reward"


def test_timeout_is_error_with_kind():
    v = _verdict(None, 900.0, 900, {}, {})
    assert v["outcome"] == "error" and v["error_kind"] == "verifier_timeout"


def test_primary_reward_rules():
    assert primary_reward({"reward": 0.0, "aux": 5}) == 0.0
    assert primary_reward({"checks": 1}) == 1.0           # single key
    assert primary_reward({"a": 1, "b": 0}) is None       # ambiguous
    assert primary_reward({"reward": True}) is None       # bools are not rewards


def test_rewardkit_details_become_checks_and_metrics():
    details = {"checks": {"criteria": [
        {"name": "HC_a", "value": 1}, {"name": "HC_b", "value": 0, "error": "boom"}],
        "components": [], "score": 0.5}}
    v = _verdict(0, 2.0, 900, {"reward.json": b'{"checks": 0}',
                               "reward-details.json": json.dumps(details).encode()}, {})
    assert v["outcome"] == "fail"
    assert [(c["name"], c["status"]) for c in v["checks"]] == [("HC_a", "passed"), ("HC_b", "failed")]
    assert v["metrics"]["details.checks.score"] == 0.5


def test_stdout_metric_extraction():
    out = b"  Geomean speedup:      6123.4x\n  Threshold: 5000x\n"
    v = _verdict(0, 1.0, 900, {"reward.txt": b"1", "test-stdout.txt": out},
                 {"speedup": r"Geomean speedup:\s+([0-9.]+)x"})
    assert v["metrics"]["speedup"] == 6123.4


def test_align_keeps_static_order_and_appends_unlisted():
    cases = [{"input": {"test": "test_x.py::test_a"}}, {"input": {"test": "test_x.py::test_b"}}]
    checks = [{"name": "test_x.py::test_b", "status": "failed", "message": "m"},
              {"name": "test_x.py::test_a", "status": "passed", "message": ""},
              {"name": "test_x.py::test_c[1]", "status": "passed", "message": ""}]
    out = _align(cases, checks)
    assert [o["ok"] for o in out] == [True, False, True] and out[2]["unlisted"]


# --- static case discovery ----------------------------------------------------

def test_pytest_ids_bare_and_class():
    src = ("def test_alpha():\n    pass\nclass TestThings:\n    def test_beta(self):\n"
           "        pass\n    def helper(self):\n        pass\nclass WidgetTest:\n"
           "    def test_gamma(self):\n        pass\ndef not_a_test():\n    pass\n")
    assert set(_pytest_ids(src)) == {"test_alpha", "TestThings::test_beta", "WidgetTest::test_gamma"}


def test_rewardkit_criteria_discovery(tmp_path):
    (tmp_path / "checks.py").write_text(textwrap.dedent("""
        from rewardkit import criterion
        @criterion
        def HC_one(workspace): return True
        @criterion(weight=2)
        def HC_two(workspace): return True
        def helper(): pass
    """))
    (tmp_path / "test_outputs.py").write_text("def test_z():\n    pass\n")
    assert _criteria((tmp_path / "checks.py").read_text()) == ["HC_one", "HC_two"]
    assert discover_cases(str(tmp_path)) == ["HC_one", "HC_two", "test_outputs.py::test_z"]


# --- task.toml semantics ------------------------------------------------------

def _task(tmp_path, toml: str, tests_dockerfile=True, solve="#!/bin/bash\ntrue\n"):
    d = tmp_path / "t1"
    for sub in ("environment", "solution", "tests"):
        (d / sub).mkdir(parents=True)
    (d / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    if tests_dockerfile:
        (d / "tests" / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY . /tests/\n")
    (d / "tests" / "test.sh").write_text("#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n")
    (d / "solution" / "solve.sh").write_text(solve)
    (d / "task.toml").write_text(textwrap.dedent(toml))
    return str(d)


def test_network_defaults_public_and_allow_internet_false(tmp_path):
    s = parse_task(_task(tmp_path, """
        artifacts = ["/app/out.txt"]
        [verifier]
        environment_mode = "separate"
        [verifier.environment]
        allow_internet = false
        cpus = 1
        [environment]
        cpus = 2
        memory_mb = 4096
    """))
    assert s.agent_env.network == "public"            # Harbor default
    assert s.verifier_env.network == "no_network"
    # [verifier.environment] is used whole: memory does NOT fall back
    assert s.verifier_env.cpus == 1 and s.verifier_env.memory_mb is None


def test_verifier_env_copies_environment_when_absent(tmp_path):
    s = parse_task(_task(tmp_path, """
        artifacts = ["/app/"]
        [verifier]
        environment_mode = "separate"
        [environment]
        cpus = 2
        memory_mb = 8192
    """))
    assert s.mode == "separate"
    assert (s.verifier_env.cpus, s.verifier_env.memory_mb) == (2, 8192)
    assert s.verifier_env.context.endswith("/tests")


def test_mode_inference_and_unsupported(tmp_path):
    s = parse_task(_task(tmp_path, 'artifacts = ["/a"]\n', tests_dockerfile=False))
    assert s.mode == "shared"
    assert unsupported_reason(s)[0] == "unsupported_mode"


def test_solve_network_only_matters_offline(tmp_path):
    d = _task(tmp_path, 'artifacts=["/a"]\n[verifier]\nenvironment_mode="separate"\n',
              solve="#!/bin/bash\npip install requests\n")
    s = parse_task(d)
    assert unsupported_reason(s) is None                      # public network: runnable
    assert unsupported_reason(s, offline_solve=True)[0] == "solve_network"


def test_verifier_digest_tracks_tests_and_contract_only(tmp_path):
    d = _task(tmp_path, 'artifacts=["/a"]\n[verifier]\nenvironment_mode="separate"\n')
    a = parse_task(d)
    with open(os.path.join(d, "solution", "solve.sh"), "a") as f:
        f.write("# solution edit\n")
    b = parse_task(d)
    assert a.digests["verifier"] == b.digests["verifier"]
    assert a.digests["solution"] != b.digests["solution"]
    with open(os.path.join(d, "tests", "test.sh"), "a") as f:
        f.write("# verifier edit\n")
    assert parse_task(d).digests["verifier"] != a.digests["verifier"]


def test_net_fetch_regex():
    assert NET_FETCH_RE.search("uv run \\\n  --with pandas==2.2.3 \\\n python x.py")
    assert NET_FETCH_RE.search("apt-get update && apt-get install -y curl")
    assert not NET_FETCH_RE.search("uv run --no-project python /solution/solve.py")
    assert not NET_FETCH_RE.search("cargo build --release --offline")


# --- bundles: Harbor's upload normalization -------------------------------------

def _docker_style_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, mode, payload in members:
            ti = tarfile.TarInfo(name)
            ti.mode = mode
            if kind == "d":
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)
            elif kind == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, payload
                tar.addfile(ti)
            else:
                ti.size = len(payload)
                tar.addfile(ti, io.BytesIO(payload))
    return buf.getvalue()


def test_add_archive_paths_modes_and_link_filter():
    raw = _docker_style_tar([
        ("evalbench", "d", 0o775, None),
        ("evalbench/run.sh", "f", 0o4777, b"#!/bin/sh\n"),       # setuid + o+w
        ("evalbench/data.txt", "f", 0o600, b"x"),
        ("evalbench/ok_link", "l", 0o777, "data.txt"),
        ("evalbench/abs_link", "l", 0o777, "/etc/passwd"),         # refused
        ("evalbench/escape", "l", 0o777, "../../../etc/passwd"),   # refused
    ])
    b = Bundle(declared=["/app/evalbench/"])
    add_archive(b, raw, "/app/evalbench/")
    assert set(b.entries) == {"/app/evalbench", "/app/evalbench/run.sh",
                              "/app/evalbench/data.txt", "/app/evalbench/ok_link"}
    assert b.entries["/app/evalbench/run.sh"].mode == 0o755   # setuid + o+w cleared
    assert b.entries["/app/evalbench/data.txt"].mode == 0o600
    assert b.missing() == []


def test_bundle_digest_is_deterministic_and_content_addressed():
    a = Bundle({"/app/x": Entry("f", 0o644, b"1")}, ["/app/x"])
    b = Bundle({"/app/x": Entry("f", 0o644, b"1")}, ["/app/x"])
    c = a.replace(set_files={"/app/x": b"2"})
    assert a.digest == b.digest != c.digest
    assert Bundle.from_tar(a.to_tar(), ["/app/x"]).digest == a.digest


def test_upload_tar_creates_parents():
    raw = upload_tar({"/a/b/c.txt": Entry("f", 0o644, b"z")})
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        assert [m.name for m in tar.getmembers()] == ["a", "a/b", "a/b/c.txt"]


def test_empty_dirs_matches_harbor_shape():
    cmd = _empty_dirs_cmd(["/app/"])
    assert "find '/app/' -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +" in cmd
    assert cmd.endswith("chmod 777 '/app/'")
