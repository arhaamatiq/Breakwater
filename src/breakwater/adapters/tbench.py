"""Terminal-Bench (classic task.yaml format) adapter: state semantics.

A t-bench task is an environment (Dockerfile), a shell oracle (solution.sh),
and a pytest suite that judges the resulting container state. Mapping:

  case      = one pytest test function ({"input": {"test": name}})
  solve     = run a shell solution in the task container, then `docker commit`
              — the committed image id IS the trial's output value
  verify    = fresh container from that image, inject tests, run the task's
              own pytest per selected case, parse per-test verdicts
  verify_only replays re-judge the SAME committed state (isolates flaky tests
              from flaky solutions); case subsets/reordering probe inter-test
              dependence; env deltas reach the pytest process.

Capabilities: none of local_eval/value_outputs — the tester skips value-based
stages and reports why. Probes reuse canonical names in shell form.

Faithfulness deltas (documented, deliberate):
  - pytest + any extra packages `run-tests.sh` pip-installs are prepared at
    image build instead of verify time (no network at verify), but they live in
    a dedicated venv (PXTEST_VENV) that tests run from — so neither they nor
    pytest are importable by the solution during solve, matching the official
    `uv venv .tbench-testing` isolation. Resolved versions are recorded in
    tb["deps_lock"] and in each trial's meta.json, so a receipt still pins what
    actually ran when run-tests.sh leaves a dependency unpinned.
  - tests run via `python -m pytest --junitxml` rather than the uv-venv
    ceremony in run-tests.sh; parser_name: pytest is the declared contract
  - tests are staged at /tests (the harness's own convention — some official
    test files hardcode absolute /tests/<fixture> paths for sibling fixtures);
    fixtures a test names by bare filename are additionally copied into the
    task workdir, since those resolve against pytest's cwd
  - single-service tasks only (multi-service docker-compose.yaml is detected
    and skipped, not silently mis-run); no network at solve or verify time —
    tasks whose official solution/tests require network are skipped at solve
    time (apt/pip install) or surface as a verify-time infra error, not a
    finding
  - a task whose verify environment cannot be reproduced (unresolvable
    `-r`/editable installs) or whose image fails to build is skipped and
    reported under LAST_LOAD_REPORT["skipped"], separating deliberate scope
    boundaries from infrastructure failures that shrink the audit set
"""
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET

import yaml

from ..config import CFG
from .. import storage
from ..runner import docker_client, TrialExecutionError, _tar_bytes
from . import base

# Test dependencies go in a dedicated venv, never system-wide. The official
# harness installs them at verify time inside `uv venv .tbench-testing`, so a
# solution must NOT be able to import them (or pytest) during solve — installing
# them system-wide made every task measurably easier than the real benchmark.
# --system-site-packages keeps whatever the task's own Dockerfile installed
# visible to the tests, which is what the pre-venv behavior effectively gave them.
PXTEST_VENV = "/opt/pxtest"
DEPS_LOCK = f"{PXTEST_VENV}/requirements.lock"

TEST_DEPS_LAYER = """
RUN (python3 -m venv --system-site-packages {venv} || \\
     (apt-get update && apt-get install -y --no-install-recommends python3-venv && \\
      python3 -m venv --system-site-packages {venv})) && \\
    {venv}/bin/pip install --quiet --disable-pip-version-check {pkgs} && \\
    {venv}/bin/pip freeze --local > {lock}
"""

NOOP_SHELL = "#!/bin/bash\ntrue\n"
RAISE_SHELL = "#!/bin/bash\nexit 1\n"

_PIP_INSTALL_RE = re.compile(
    r"^\s*(?:uv\s+pip|pip3?|python3?\s+-m\s+pip)\s+install\b(.*)$", re.MULTILINE)
# name, optional extras, optional ==version. Deliberately strict: anything else
# (paths, requirement files, URLs) is reported as unresolved rather than guessed.
_PKG_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9,._\-]+\])?(?:==[A-Za-z0-9._\-]+)?$")
# flags that consume the following token, so it is never mistaken for a package
_VALUE_FLAGS = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable",
                "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
                "--python", "-p", "--target", "--prefix", "--upgrade-strategy",
                "--no-binary", "--only-binary"}
_REQ_FLAGS = {"-r", "--requirement"}
_PATHLIKE_SUFFIXES = (".txt", ".cfg", ".toml", ".in", ".whl", ".zip", ".gz")


def _looks_like_package(tok: str) -> bool:
    if "/" in tok or tok in (".", "..") or tok.endswith(_PATHLIKE_SUFFIXES):
        return False
    return bool(_PKG_TOKEN_RE.match(tok))


def _parse_requirements_file(task_dir: str, ref: str) -> list[str] | None:
    """Resolve a `-r <file>` reference against the task dir. None if not found."""
    for cand in (os.path.join(task_dir, ref),
                 os.path.join(task_dir, "tests", ref),
                 os.path.join(task_dir, os.path.basename(ref))):
        if os.path.isfile(cand):
            out = []
            for raw in open(cand):
                tok = raw.split("#", 1)[0].strip()
                if tok and _looks_like_package(tok):
                    out.append(tok)
            return out
    return None


def _extra_test_deps(task_dir: str) -> tuple[list[str], list[str]]:
    """Packages the task's OWN run-tests.sh pip-installs beyond pytest itself —
    e.g. csv-to-parquet needs pandas+pyarrow at verify time. Missing these
    produced false VERIFIER_CRASH findings until this parser was added.

    Returns (packages, unresolved). A non-empty `unresolved` means the verify
    environment cannot be reproduced faithfully, so the caller skips the task
    rather than risk a false finding from a missing import.
    """
    path = os.path.join(task_dir, "run-tests.sh")
    if not os.path.exists(path):
        return ["pytest==8.4.1"], []
    src = open(path).read()
    pkgs, seen, unresolved = [], set(), []

    def add(tok: str) -> None:
        name = tok.split("==")[0].split("[")[0].lower()
        if name not in seen:
            seen.add(name)
            pkgs.append(tok)

    for line in _PIP_INSTALL_RE.findall(src):
        toks = line.replace("\\", " ").split()
        i = 0
        while i < len(toks):
            tok = toks[i]
            if tok in _VALUE_FLAGS:
                value = toks[i + 1] if i + 1 < len(toks) else None
                if tok in _REQ_FLAGS and value:
                    found = _parse_requirements_file(task_dir, value)
                    if found is None:
                        unresolved.append(f"requirements file not found: {value}")
                    else:
                        for req in found:
                            add(req)
                elif tok in ("-e", "--editable"):
                    unresolved.append(f"editable install not supported: {value}")
                i += 2
                continue
            if tok.startswith("-"):          # valueless flag, e.g. --quiet
                i += 1
                continue
            if _looks_like_package(tok):
                add(tok)
            else:
                unresolved.append(f"unrecognized install target: {tok}")
            i += 1

    if not any(p.split("==")[0].lower() == "pytest" for p in pkgs):
        pkgs.insert(0, "pytest==8.4.1")
    return pkgs, unresolved


def _workdir_fixtures(test_files: dict) -> list[str]:
    """Fixture files to also drop in the task workdir: only those the test
    references by bare name (resolved against pytest's cwd). Ones referenced as
    /tests/<name> already resolve from the staged copy, so copying them into the
    workdir would write files into state the tests may enumerate."""
    src = test_files.get("test_outputs.py", "")
    return [fn for fn in test_files
            if fn != "test_outputs.py" and fn in src and f"/tests/{fn}" not in src]


def _parse_test_names(test_src: str) -> list[str]:
    import ast
    return [n.name for n in ast.parse(test_src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")]


def _service_count(task_dir: str) -> int:
    """Declared compose services (0 when none/absent); >1 means sidecars we
    cannot start."""
    path = os.path.join(task_dir, "docker-compose.yaml")
    if not os.path.exists(path):
        return 0
    compose = yaml.safe_load(open(path)) or {}
    return len(compose.get("services") or {})


def _build_image(task_dir: str, tag: str, extra_pkgs: list[str]) -> str:
    client = docker_client()
    with tempfile.TemporaryDirectory() as ctx:
        for entry in os.listdir(task_dir):
            s = os.path.join(task_dir, entry)
            d = os.path.join(ctx, entry)
            (shutil.copytree if os.path.isdir(s) else shutil.copy2)(s, d)
        with open(os.path.join(ctx, "Dockerfile"), "a") as f:
            f.write(TEST_DEPS_LAYER.format(venv=PXTEST_VENV, lock=DEPS_LOCK,
                                           pkgs=" ".join(extra_pkgs)))
        image, _ = client.images.build(path=ctx, tag=tag, rm=True)
    return image.id


def _read_deps_lock(tag: str) -> str | None:
    """Resolved versions actually installed, so receipts record what ran even
    when run-tests.sh leaves a dependency unpinned."""
    try:
        # entrypoint="" so a task-defined ENTRYPOINT can't swallow the command
        out = docker_client().containers.run(tag, ["cat", DEPS_LOCK], remove=True,
                                            entrypoint="")
        return out.decode(errors="replace").strip() or None
    except Exception:
        return None


# Populated by load(). Skips are categorised because they are not equivalent:
# a format/multi-service skip is a deliberate scope boundary, whereas a build
# failure silently shrinks the audit set and must be read as an infra problem.
LAST_LOAD_REPORT: dict = {}
_SCOPE_SKIPS = ("format", "multi_service", "no_tests")
_INFRA_SKIPS = ("unresolved_deps", "build_failed")


def load(root: str, ids: list[str] | None = None) -> dict:
    """Discover and convert task dirs under `root`; builds images (cached)."""
    tasks = {}
    skipped: dict[str, list[tuple[str, str]]] = {}

    def skip(category: str, name: str, detail: str) -> None:
        skipped.setdefault(category, []).append((name, detail))
        print(f"[tbench] skip {name}: {detail}")

    names = sorted(ids or [d for d in os.listdir(root)
                           if os.path.isdir(os.path.join(root, d))])
    for name in names:
        task_dir = os.path.join(root, name)
        required = ["task.yaml", "Dockerfile", "solution.sh",
                    os.path.join("tests", "test_outputs.py")]
        if any(not os.path.exists(os.path.join(task_dir, p)) for p in required):
            skip("format", name, "not a classic-format task dir")
            continue
        n_services = _service_count(task_dir)
        if n_services > 1:
            skip("multi_service", name,
                 f"multi-service task ({n_services} services in docker-compose.yaml) "
                 f"— single-container runtime can't start sidecars, would silently "
                 f"mis-run rather than skip")
            continue
        meta = yaml.safe_load(open(os.path.join(task_dir, "task.yaml")))
        test_src = open(os.path.join(task_dir, "tests", "test_outputs.py")).read()
        test_names = _parse_test_names(test_src)
        if not test_names:
            skip("no_tests", name, "no test functions found")
            continue

        extra_pkgs, unresolved = _extra_test_deps(task_dir)
        if unresolved:
            skip("unresolved_deps", name,
                 f"cannot reproduce the verify environment ({'; '.join(unresolved)}) "
                 f"— skipping rather than risk a false VERIFIER_CRASH")
            continue

        tag = f"breakwater-tb-{name}"
        t0 = time.time()
        try:
            _build_image(task_dir, tag, extra_pkgs)
        except Exception as e:
            skip("build_failed", name, f"image build failed: {str(e)[:200]}")
            continue
        client = docker_client()
        workdir = client.images.get(tag).attrs["Config"].get("WorkingDir") or "/app"
        extra_str = f", deps: {' '.join(extra_pkgs)}" if len(extra_pkgs) > 1 else ""
        print(f"[tbench] built {tag} in {time.time()-t0:.0f}s (workdir {workdir}, "
              f"{len(test_names)} tests{extra_str})")

        test_files = {}
        tests_dir = os.path.join(task_dir, "tests")
        for fn in os.listdir(tests_dir):
            p = os.path.join(tests_dir, fn)
            if os.path.isfile(p):
                test_files[fn] = open(p).read()

        tid = f"tb_{name.replace('-', '_')}"
        tasks[tid] = {
            "runtime": "tbench",
            "capabilities": [],
            "cases": [{"input": {"test": t}, "expected": "pass"} for t in test_names],
            "oracle": open(os.path.join(task_dir, "solution.sh")).read(),
            "agent": open(os.path.join(task_dir, "solution.sh")).read(),
            "noop": NOOP_SHELL,
            "probe_solutions": {"noop_none": NOOP_SHELL, "raise_probe": RAISE_SHELL},
            "verifier": test_src,           # informational; execution uses files
            "tb": {"image": tag, "workdir": workdir, "test_files": test_files,
                   "workdir_fixtures": _workdir_fixtures(test_files),
                   "test_deps": extra_pkgs, "deps_lock": _read_deps_lock(tag),
                   "solve_timeout": min(float(meta.get("max_agent_timeout_sec") or 90), 90),
                   "verify_timeout": min(float(meta.get("max_test_timeout_sec") or 90), 90)},
            "source": {"benchmark": "terminal-bench", "task_id": name, "path": task_dir},
            **({"planted_flaw": meta["x_planted_flaw"]}
               if meta.get("x_planted_flaw") else {}),
        }

    LAST_LOAD_REPORT.clear()
    LAST_LOAD_REPORT.update({"loaded": sorted(tasks), "skipped": skipped})
    n_skipped = sum(len(v) for v in skipped.values())
    if n_skipped:
        tally = " ".join(f"{k}:{len(v)}" for k, v in sorted(skipped.items()))
        print(f"[tbench] loaded {len(tasks)} task(s); skipped {n_skipped} — {tally}")
        for category in _INFRA_SKIPS:
            for nm, detail in skipped.get(category, []):
                print(f"[tbench] WARNING audit scope reduced by infrastructure "
                      f"({category}): {nm}: {detail}")
    return tasks


class TBenchRuntime:
    def _wait(self, container, timeout, label):
        try:
            return container.wait(timeout=timeout)
        except Exception as e:
            try:
                container.kill()
            except Exception:
                pass
            raise TrialExecutionError(f"{label}: timeout/wait failure: {e}") from e

    def _solve(self, task, solution_src: str, env: dict) -> tuple[str, bytes, int]:
        """Run the solution in the task container; commit the state."""
        tb = task["tb"]
        client = docker_client()
        container = client.containers.create(
            tb["image"], command=["bash", "/px/solution.sh"],
            working_dir=tb["workdir"], network_disabled=True,
            mem_limit="1g", nano_cpus=2_000_000_000,
            environment={k: str(v) for k, v in env.items()})
        try:
            container.put_archive("/", _tar_bytes({"px/solution.sh": solution_src}))
            container.start()
            exit_info = self._wait(container, tb["solve_timeout"], "solve")
            logs = container.logs(stdout=True, stderr=True)
            state = container.commit()
            return state.id, logs, exit_info.get("StatusCode", -1)
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass

    def _verify(self, task, state_ref: str, cases: list, env: dict):
        tb = task["tb"]
        client = docker_client()
        selectors = " ".join(
            f"/tests/test_outputs.py::{c['input']['test']}" for c in cases)
        # Tests run from the test venv, which is where their dependencies live.
        # The presence check (not an error-retry) keeps receipts whose images
        # predate the venv replayable.
        cmd = (f"PY={PXTEST_VENV}/bin/python; [ -x \"$PY\" ] || PY=python3; "
               f"cd {tb['workdir']} && TEST_DIR=/tests $PY -m pytest "
               f"{selectors} -q -p no:cacheprovider --junitxml=/tests/px-report.xml")
        container = client.containers.create(
            state_ref, command=["bash", "-c", cmd],
            network_disabled=True, mem_limit="1g", nano_cpus=2_000_000_000,
            environment={k: str(v) for k, v in env.items()})
        try:
            # Stage at the harness's own /tests convention (some official tests
            # hardcode absolute /tests/<fixture> paths). Fixtures a test names by
            # bare filename resolve against pytest's cwd instead, so those — and
            # only those — are also copied into the workdir, keeping writes out of
            # state the tests may enumerate. Older receipts lack the precomputed
            # list; fall back to their original all-fixtures behavior.
            workdir_rel = tb["workdir"].lstrip("/")
            fixtures = tb.get("workdir_fixtures")
            if fixtures is None:
                fixtures = [fn for fn in tb["test_files"] if fn != "test_outputs.py"]
            files = {f"tests/{fn}": content for fn, content in tb["test_files"].items()}
            files.update({f"{workdir_rel}/{fn}": tb["test_files"][fn]
                          for fn in fixtures})
            container.put_archive("/", _tar_bytes(files))
            container.start()
            self._wait(container, tb["verify_timeout"], "verify")
            logs = container.logs(stdout=True, stderr=True)
            xml_raw = self._read_file(container, "/tests/px-report.xml")
            if xml_raw is None:
                raise TrialExecutionError(
                    f"verify: pytest produced no report; logs={logs[-400:]!r}")
            return self._parse_junit(xml_raw, cases), logs, xml_raw
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass

    @staticmethod
    def _read_file(container, path):
        try:
            stream, _ = container.get_archive(path)
            raw = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                return tar.extractfile(tar.getmembers()[0]).read()
        except Exception:
            return None

    @staticmethod
    def _parse_junit(xml_raw: bytes, cases: list) -> dict:
        by_name = {}
        for tc in ET.fromstring(xml_raw).iter("testcase"):
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if error is not None:
                st, detail = "error", (error.get("message") or "")[:400]
            elif failure is not None:
                st, detail = "fail", (failure.get("message") or "")[:400]
            elif skipped is not None:
                st, detail = "error", "skipped"
            else:
                st, detail = "pass", "ok"
            by_name[tc.get("name")] = (st, detail)
        verdicts, any_err = [], False
        for i, c in enumerate(cases):
            st, detail = by_name.get(c["input"]["test"], ("error", "missing from report"))
            any_err = any_err or st == "error"
            v = {"case": i, "ok": st == "pass", "detail": detail}
            if st == "error":
                v["verifier_error"] = detail
            verdicts.append(v)
        outcome = ("error" if any_err
                   else "pass" if all(v["ok"] for v in verdicts) else "fail")
        return {"outcome": outcome, "cases": verdicts}

    def run_trial(self, trial: dict, task: dict) -> dict:
        spec = trial.get("spec") or {}
        cases = spec.get("cases") or task["cases"]
        env = spec.get("env") or {}
        mode = spec.get("mode", "full")
        t0 = time.monotonic()
        uploads = {}

        if mode == "full":
            src = spec.get("solution_source") or base.solution_from(
                task, spec.get("solution", "agent"))
            state_ref, solve_logs, exit_code = self._solve(task, src, env)
            uploads["solve.log"] = solve_logs
        else:
            state_ref = spec["outputs"][0]["output"]
            exit_code = None

        verdicts, verify_logs, xml_raw = self._verify(task, state_ref, cases, env)
        uploads["verify.log"] = verify_logs
        uploads["report.xml"] = xml_raw

        exec_ms = (time.monotonic() - t0) * 1000.0
        outputs = [{"output": state_ref} for _ in cases]
        prefix = (f"{trial['experiment_id']}/{trial['trial_id'].replace('/', '_')}"
                  f"/attempt-{trial.get('attempts', 0)}")
        uploads["meta.json"] = json.dumps({
            "trial_id": trial["trial_id"], "task_id": trial["task_id"],
            "runtime": "tbench", "mode": mode, "env": env, "state": state_ref,
            "solution_exit_code": exit_code, "exec_ms": exec_ms,
            "test_deps": task["tb"].get("test_deps"),
            "deps_lock": task["tb"].get("deps_lock")}).encode()
        artifacts = [storage.put_bytes(f"{prefix}/{n}",
                                       d if isinstance(d, bytes) else d.encode())
                     for n, d in uploads.items()]
        detail = {"cases": verdicts["cases"], "outputs": outputs,
                  "solution_exit_code": exit_code}
        return {"outcome": verdicts["outcome"], "detail": detail,
                "exec_ms": exec_ms, "artifacts": artifacts}


base.register_runtime("tbench", TBenchRuntime())
