"""Regression coverage for the Terminal-Bench adapter's fidelity guarantees.

These lock in the properties a review found broken (or at risk):
  - verify-time dependencies must not be importable during solve
  - the pip-install parser must never guess at a non-package token
  - a task whose verify env can't be reproduced is skipped, not mis-run
  - build failures are reported separately from deliberate scope skips
  - workdir fixture copies are limited to fixtures referenced by bare name

Unit tests need no Docker. The isolation test does; it is opt-in via
PX_DOCKER_TESTS=1 so the fast suite stays runnable anywhere.

    uv run pytest tests/ -q                        # unit only
    PX_DOCKER_TESTS=1 uv run pytest tests/ -q      # + container isolation
"""
import os
import textwrap

import pytest

from projectx.adapters import tbench


def write_task(tmp_path, name="t", run_tests=None, compose=None, tests=None,
               extra_files=None):
    d = tmp_path / name
    (d / "tests").mkdir(parents=True)
    (d / "task.yaml").write_text("instruction: x\n")
    (d / "Dockerfile").write_text("FROM python:3.13-slim-bookworm\nWORKDIR /app\n")
    (d / "solution.sh").write_text("#!/bin/bash\ntrue\n")
    (d / "tests" / "test_outputs.py").write_text(
        tests if tests is not None else "def test_x():\n    assert True\n")
    if run_tests is not None:
        (d / "run-tests.sh").write_text(textwrap.dedent(run_tests))
    if compose is not None:
        (d / "docker-compose.yaml").write_text(textwrap.dedent(compose))
    for fn, content in (extra_files or {}).items():
        (d / fn).write_text(content)
    return str(d)


# --------------------------------------------------------------- dep parsing

def test_pinned_deps_are_extracted_with_pytest_first(tmp_path):
    d = write_task(tmp_path, run_tests="""
        uv venv .tbench-testing
        uv pip install pytest==8.4.1 pandas==2.3.0 pyarrow==20.0.0
    """)
    pkgs, unresolved = tbench._extra_test_deps(d)
    assert pkgs == ["pytest==8.4.1", "pandas==2.3.0", "pyarrow==20.0.0"]
    assert unresolved == []


def test_pytest_is_added_when_run_tests_omits_it(tmp_path):
    d = write_task(tmp_path, run_tests="pip install numpy==2.3.0\n")
    pkgs, unresolved = tbench._extra_test_deps(d)
    assert pkgs[0] == "pytest==8.4.1" and "numpy==2.3.0" in pkgs
    assert unresolved == []


def test_missing_run_tests_falls_back_to_pytest_only(tmp_path):
    pkgs, unresolved = tbench._extra_test_deps(write_task(tmp_path))
    assert (pkgs, unresolved) == (["pytest==8.4.1"], [])


def test_unpinned_deps_are_kept_verbatim(tmp_path):
    d = write_task(tmp_path, run_tests="uv pip install pytest==8.4.1 selenium bs4\n")
    pkgs, _ = tbench._extra_test_deps(d)
    assert pkgs == ["pytest==8.4.1", "selenium", "bs4"]


def test_value_taking_flags_never_become_packages(tmp_path):
    d = write_task(tmp_path, run_tests="""
        pip install --quiet --index-url https://example.invalid/simple pytest==8.4.1
    """)
    pkgs, unresolved = tbench._extra_test_deps(d)
    assert pkgs == ["pytest==8.4.1"], pkgs
    assert unresolved == []


def test_requirements_file_is_resolved_when_present(tmp_path):
    d = write_task(tmp_path, run_tests="pip install -r requirements.txt\n",
                   extra_files={"requirements.txt":
                                "pandas==2.3.0\n# comment\n\npyarrow==20.0.0\n"})
    pkgs, unresolved = tbench._extra_test_deps(d)
    assert unresolved == []
    assert pkgs == ["pytest==8.4.1", "pandas==2.3.0", "pyarrow==20.0.0"]


def test_missing_requirements_file_is_unresolved_not_guessed(tmp_path):
    """The old parser turned `requirements.txt` into a package name, which failed
    the build and silently dropped the task from the audit."""
    d = write_task(tmp_path, run_tests="pip install -r requirements.txt\n")
    pkgs, unresolved = tbench._extra_test_deps(d)
    assert "requirements.txt" not in " ".join(pkgs)
    assert any("requirements file not found" in u for u in unresolved)


def test_editable_install_is_unresolved(tmp_path):
    d = write_task(tmp_path, run_tests="pip install pytest==8.4.1 -e .\n")
    pkgs, unresolved = tbench._extra_test_deps(d)
    assert "." not in pkgs
    assert any("editable" in u for u in unresolved)


def test_path_targets_are_unresolved(tmp_path):
    d = write_task(tmp_path, run_tests="pip install ./wheels/foo-1.0.whl\n")
    _, unresolved = tbench._extra_test_deps(d)
    assert any("unrecognized install target" in u for u in unresolved)


def test_commented_install_lines_are_ignored(tmp_path):
    d = write_task(tmp_path, run_tests="# pip install evil==1.0\npip install pytest==8.4.1\n")
    pkgs, _ = tbench._extra_test_deps(d)
    assert pkgs == ["pytest==8.4.1"]


# ------------------------------------------------------------ service count

@pytest.mark.parametrize("compose, expected", [
    (None, 0),
    ("services:\n  client:\n    image: x\n", 1),
    ("services:\n  client:\n    image: x\n  decryptor:\n    image: y\n", 2),
    ("services: {}\n", 0),
])
def test_service_count(tmp_path, compose, expected):
    d = write_task(tmp_path, compose=compose)
    assert tbench._service_count(d) == expected


# -------------------------------------------------------- workdir fixtures

def test_bare_name_fixture_is_copied_to_workdir():
    files = {"test_outputs.py": 'subprocess.run(["python", "test.py"])',
             "test.py": "x"}
    assert tbench._workdir_fixtures(files) == ["test.py"]


def test_absolute_path_fixture_is_not_copied_to_workdir():
    """It already resolves from /tests, so copying it would write into state the
    tests may enumerate."""
    files = {"test_outputs.py": 'open("/tests/expected_output.txt")',
             "expected_output.txt": "x"}
    assert tbench._workdir_fixtures(files) == []


def test_unreferenced_fixture_is_not_copied():
    files = {"test_outputs.py": "def test_x():\n    assert True\n", "unused.dat": "x"}
    assert tbench._workdir_fixtures(files) == []


# ------------------------------------------------- isolation contract (text)

def test_deps_layer_installs_into_venv_not_system():
    layer = tbench.TEST_DEPS_LAYER.format(
        venv=tbench.PXTEST_VENV, lock=tbench.DEPS_LOCK, pkgs="pandas==2.3.0")
    assert f"{tbench.PXTEST_VENV}/bin/pip install" in layer
    assert "--break-system-packages" not in layer
    assert "pip freeze" in layer


def test_verify_runs_pytest_from_the_venv():
    import inspect
    src = inspect.getsource(tbench.TBenchRuntime._verify)
    assert f"{tbench.PXTEST_VENV}/bin/python" in src or "PXTEST_VENV" in src
    assert "TEST_DIR=/tests" in src


# ----------------------------------------------------- load() skip taxonomy

def test_format_skip_is_recorded_as_scope_not_infra(tmp_path):
    (tmp_path / "broken").mkdir()          # no task.yaml/Dockerfile/solution.sh
    tasks = tbench.load(str(tmp_path), ids=["broken"])
    assert tasks == {}
    assert [n for n, _ in tbench.LAST_LOAD_REPORT["skipped"]["format"]] == ["broken"]
    assert "build_failed" not in tbench.LAST_LOAD_REPORT["skipped"]


def test_multi_service_task_is_skipped(tmp_path):
    write_task(tmp_path, name="multi",
               compose="services:\n  client:\n    image: x\n  side:\n    image: y\n")
    tasks = tbench.load(str(tmp_path), ids=["multi"])
    assert tasks == {}
    assert "multi_service" in tbench.LAST_LOAD_REPORT["skipped"]


def test_unresolvable_deps_skip_the_task_before_building(tmp_path):
    write_task(tmp_path, name="needsreq", run_tests="pip install -r missing.txt\n")
    tasks = tbench.load(str(tmp_path), ids=["needsreq"])
    assert tasks == {}
    assert "unresolved_deps" in tbench.LAST_LOAD_REPORT["skipped"]


# ------------------------------------------------- container isolation (opt-in)

docker_only = pytest.mark.skipif(
    os.environ.get("PX_DOCKER_TESTS") != "1",
    reason="set PX_DOCKER_TESTS=1 to run container-backed checks")


@docker_only
def test_test_deps_are_not_importable_during_solve(tmp_path):
    """The fidelity property the review caught: pandas was importable by the
    solution, making tasks easier than the real benchmark."""
    from projectx.runner import docker_client
    d = write_task(tmp_path, name="isolated",
                   run_tests="uv pip install pytest==8.4.1 pandas==2.3.0\n")
    tasks = tbench.load(str(tmp_path), ids=["isolated"])
    assert tasks, tbench.LAST_LOAD_REPORT
    tb = tasks["tb_isolated"]["tb"]
    client = docker_client()

    def run(cmd):
        c = client.containers.create(tb["image"], command=["bash", "-c", cmd],
                                     network_disabled=True)
        try:
            c.start()
            return c.wait(timeout=60).get("StatusCode"), c.logs().decode(errors="replace")
        finally:
            c.remove(force=True)

    solve_code, _ = run("python3 -c 'import pandas'")
    assert solve_code != 0, "pandas must NOT be importable by the solve interpreter"
    solve_pytest, _ = run("python3 -c 'import pytest'")
    assert solve_pytest != 0, "pytest must NOT be importable by the solve interpreter"
    verify_code, out = run(f"{tbench.PXTEST_VENV}/bin/python -c "
                           f"'import pandas, pytest; print(pandas.__version__)'")
    assert verify_code == 0, f"tests must see their deps in the venv: {out}"
    assert tb["deps_lock"] and "pandas==" in tb["deps_lock"]
