"""Regression coverage for the Terminal-Bench adapter's fidelity guarantees.

These lock in the properties a review found broken (or at risk):
  - verify-time dependencies must not be importable during solve
  - the pip-install parser must never guess at a non-package token
  - a task whose verify env can't be reproduced is skipped, not mis-run
  - build failures are reported separately from deliberate scope skips
  - workdir fixture copies are limited to fixtures referenced by bare name

Unit tests need no Docker. The isolation test does; it is opt-in via
BW_DOCKER_TESTS=1 so the fast suite stays runnable anywhere.

    uv run pytest tests/ -q                        # unit only
    BW_DOCKER_TESTS=1 uv run pytest tests/ -q      # + container isolation
"""
import os
import textwrap

import pytest

from breakwater.adapters import tbench


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
    os.environ.get("BW_DOCKER_TESTS") != "1",
    reason="set BW_DOCKER_TESTS=1 to run container-backed checks")


@docker_only
def test_test_deps_are_not_importable_during_solve(tmp_path):
    """The fidelity property the review caught: pandas was importable by the
    solution, making tasks easier than the real benchmark."""
    from breakwater.runner import docker_client
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


# ------------------------------------------------ network sandbox semantics
#
# receipts/quarantine/tb_break_filter_js_from_html: the reference was "rejected"
# because docker's `NetworkDisabled` flag leaves /etc/hosts empty, so `localhost`
# did not resolve and selenium could not reach its own chromedriver. The fix is
# `network_mode="none"`: loopback + populated /etc/hosts, still no route out.

def test_containers_use_network_mode_none_not_network_disabled():
    import inspect
    for fn in (tbench.TBenchRuntime._solve, tbench.TBenchRuntime._verify):
        src = inspect.getsource(fn)
        assert "network_mode=NO_NETWORK" in src, fn.__name__
        assert "network_disabled" not in src, (
            f"{fn.__name__}: network_disabled=True leaves /etc/hosts empty and "
            f"breaks `localhost`; use network_mode='none'")
    assert tbench.NO_NETWORK == "none"


# ----------------------------------------------- conditions the runtime can't
# reproduce (each one produced false REFERENCE_REJECTED receipts; see
# receipts/quarantine/README.md). These are skipped at load, before any build.

SOLVE_NET_POSITIVE = {
    # real reference solutions from the quarantined receipts
    "curl uv installer (csv-to-parquet)":
        "curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh\nsource $HOME/.local/bin/env\n",
    "pip install (cprofiling-python)": "pip install requests\nwhich python > /app/python.txt\n",
    "apt-get update+install (dna-insert)":
        "apt-get update\napt-get install -y emboss primer3\n",
    "apt-get pinned (financial-document-processor)":
        "apt-get update && apt-get install -y tesseract-ocr=5.3.4-1build5\n",
    "post to a named host (ancient-puzzle)":
        'curl -X POST http://decryptor:8090/decrypt \\\n  -H "Content-Type: application/json"\n',
    # other spellings of the same thing
    "apt install": "apt install -y gcc git\n",
    "apt-get -y install (flag before verb)": "apt-get -y install nginx\n",
    "python -m pip": "python3 -m pip install --no-cache-dir openpyxl==3.1.5\n",
    "uv pip install": "uv pip install --system pandas\n",
    "uv add": "uv init\nuv add pandas pyarrow\n",
    "uv run --with": "uv run --with pandas python solve.py\n",
    "uvx": "uvx ruff check .\n",
    "npm install": "npm install express\n",
    "wget": "wget https://example.com/data.tgz\n",
    "git clone over https": "git clone https://github.com/x/y.git /app/y\n",
    "conda install": "conda install -y numpy\n",
    "install.packages": "Rscript -e 'install.packages(\"jsonlite\")'\n",
}

SOLVE_NET_NEGATIVE = {
    "plain script": "#!/bin/bash\nset -e\npython3 solve.py\n",
    "heredoc python": "python3 - <<'EOF'\nprint('hi')\nEOF\n",
    "curl to localhost": "curl -s http://localhost:8080/health\n",
    "curl to 127.0.0.1": "curl -sf http://127.0.0.1:5000/x | grep ok\n",
    "wget to loopback": "wget -qO- http://0.0.0.0:9000/\n",
    "commented install": "# pip install requests\n  # apt-get install -y curl\ntrue\n",
    "offline wheel install": "pip install --no-index --find-links /wheels foo\n",
    "pip list/freeze are not installs": "pip list\npip freeze > r.txt\n",
    "apt list": "apt list --installed | head\n",
    "git clone of a local path": "git clone /srv/repo.git /app/work\n",
    "cargo build": "cargo build --release --offline\n",
    "make": "make -C /app\n",
}


@pytest.mark.parametrize("name", sorted(SOLVE_NET_POSITIVE))
def test_solve_network_use_is_detected(name):
    assert tbench._solve_network_use(SOLVE_NET_POSITIVE[name]), name


@pytest.mark.parametrize("name", sorted(SOLVE_NET_NEGATIVE))
def test_solve_network_use_not_flagged(name):
    assert tbench._solve_network_use(SOLVE_NET_NEGATIVE[name]) is None, name


def test_solve_network_task_is_skipped_before_build(tmp_path):
    write_task(tmp_path, name="needsnet")
    (tmp_path / "needsnet" / "solution.sh").write_text(
        "#!/bin/bash\ncurl -LsSf https://astral.sh/uv/install.sh | sh\n")
    tasks = tbench.load(str(tmp_path), ids=["needsnet"])
    assert tasks == {}
    skipped = tbench.LAST_LOAD_REPORT["skipped"]
    assert [n for n, _ in skipped["solve_network"]] == ["needsnet"]
    assert "build_failed" not in skipped          # decided before any docker build
    assert "solve_network" in tbench._SCOPE_SKIPS


def test_solve_network_skip_can_be_opted_out(tmp_path, monkeypatch):
    """The predicate can't see that an image already satisfies an install, so
    there is an explicit opt-out; the task then proceeds to the build stage."""
    write_task(tmp_path, name="maybeoffline")
    (tmp_path / "maybeoffline" / "solution.sh").write_text("apt-get install -y curl\n")
    monkeypatch.setenv(tbench.ALLOW_SOLVE_NETWORK_ENV, "1")

    def no_docker(*a, **kw):
        raise RuntimeError("build reached")
    monkeypatch.setattr(tbench, "_build_image", no_docker)
    assert tbench.load(str(tmp_path), ids=["maybeoffline"]) == {}
    skipped = tbench.LAST_LOAD_REPORT["skipped"]
    assert "solve_network" not in skipped
    assert "build reached" in skipped["build_failed"][0][1]


def test_acl_xattr_task_is_skipped_as_state_fidelity(tmp_path):
    """docker commit drops POSIX ACLs and user.* xattrs (verified empirically),
    so judging a setfacl solution on the committed state is a false rejection
    (receipts/quarantine/tb_acl_permissions_inheritance)."""
    write_task(tmp_path, name="acl")
    (tmp_path / "acl" / "solution.sh").write_text(
        "mkdir -p /srv/shared\nsetfacl -m u:alice:rwx /srv/shared\n")
    assert tbench.load(str(tmp_path), ids=["acl"]) == {}
    assert [n for n, _ in tbench.LAST_LOAD_REPORT["skipped"]["state_fidelity"]] == ["acl"]
    assert "build_failed" not in tbench.LAST_LOAD_REPORT["skipped"]


def test_state_loss_ignores_commented_lines():
    assert tbench._state_loss_use("# setfacl -m u:a:rwx /x\nchmod 2770 /x\n") is None
    assert tbench._state_loss_use("getfacl /x\nsetfattr -n user.a -v b /x\n")


VERIFY_FETCH_TEST = '''
import urllib.request
def download_vectors():
    url = "https://github.com/davidwagner/html-sanitizer-testbed/archive/refs/heads/master.zip"
    urllib.request.urlretrieve(url, "/tmp/x.zip")
def test_x():
    assert True
'''


def test_verify_time_fetch_is_detected():
    """The shape of filter-js-from-html's test_filter_blocks_xss."""
    assert tbench._verify_network_use({"test_outputs.py": VERIFY_FETCH_TEST})


@pytest.mark.parametrize("src", [
    # local service: fetch call but no remote URL
    'import requests\ndef test_x():\n    assert requests.get("http://localhost:8080").ok\n',
    # URL mentioned as data only: remote URL but no fetching call
    'def test_x():\n    assert "https://example.com/a" in open("/app/out.txt").read()\n',
    # XML namespace identifier next to a (local) fetch
    'import urllib.request\nNS = "http://www.w3.org/2000/svg"\n'
    'def test_x():\n    urllib.request.urlopen("http://127.0.0.1:9/")\n',
    # commented-out fetch
    '# urllib.request.urlretrieve("https://github.com/x/y.zip", "/tmp/z")\ndef test_x():\n    pass\n',
])
def test_verify_fetch_not_flagged(src):
    assert tbench._verify_network_use({"test_outputs.py": src}) is None


def test_verify_network_task_is_skipped_before_build(tmp_path):
    write_task(tmp_path, name="fetchy", tests=VERIFY_FETCH_TEST)
    assert tbench.load(str(tmp_path), ids=["fetchy"]) == {}
    assert [n for n, _ in tbench.LAST_LOAD_REPORT["skipped"]["verify_network"]] == ["fetchy"]
    assert "build_failed" not in tbench.LAST_LOAD_REPORT["skipped"]


def test_quarantined_references_are_classified_up_front():
    """Every quarantined receipt whose cause is a load-time-detectable class is
    now caught by the matching predicate, so it cannot be produced again."""
    import glob
    import json
    expected = {
        "tb_acl_permissions_inheritance": "state",
        "tb_ancient_puzzle": "solve",
        "tb_cprofiling_python": "solve",
        "tb_csv_to_parquet": "solve",
        "tb_dna_insert": "solve",
        "tb_filter_js_from_html": "verify",
        "tb_financial_document_processor": "solve",
    }
    root = os.path.join(os.path.dirname(__file__), "..", "receipts", "quarantine")
    seen = set()
    for path in glob.glob(os.path.join(root, "tb_*--REFERENCE_REJECTED.json")):
        r = json.load(open(path))
        kind = expected.get(r["task_id"])
        if kind is None:
            continue
        blob = r["replay"][0]["spec"]["task_blob"]
        got = {"solve": tbench._solve_network_use(blob["oracle"]),
               "state": tbench._state_loss_use(blob["oracle"]),
               "verify": tbench._verify_network_use(blob["tb"]["test_files"])}[kind]
        assert got, f"{r['task_id']} should be caught by the {kind} predicate"
        seen.add(r["task_id"])
    assert seen == set(expected), f"missing quarantined receipts: {set(expected) - seen}"


@docker_only
def test_sandbox_resolves_localhost_but_has_no_egress():
    """The behavioural half of the network_mode='none' fix: `localhost` must work
    (selenium/chromedriver, local servers) while nothing can leave the box."""
    from breakwater.runner import docker_client
    probe = (
        "import socket, urllib.request\n"
        "print('LOCALHOST', socket.getaddrinfo('localhost', 80)[0][4][0])\n"
        "try:\n"
        "    urllib.request.urlopen('http://1.1.1.1/', timeout=5); print('EGRESS')\n"
        "except Exception:\n"
        "    print('NOEGRESS')\n")
    out = docker_client().containers.run(
        "python:3.11-slim", ["python", "-c", probe], remove=True,
        network_mode=tbench.NO_NETWORK).decode()
    assert "LOCALHOST" in out and "NOEGRESS" in out and "EGRESS\n" not in out.replace("NOEGRESS", "")
