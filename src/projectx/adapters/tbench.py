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
  - pytest==8.4.1 is baked into the task image at prep instead of installed
    by run-tests.sh at verify time (same pytest the harness pins; tests are
    still absent during solve, which is the leakage surface that matters)
  - tests run via `python -m pytest --junitxml` rather than the uv-venv
    ceremony in run-tests.sh; parser_name: pytest is the declared contract
  - single-service tasks only; no network at solve or verify time
"""
import io
import json
import os
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

PYTEST_LAYER = """
RUN (python3 -m pip --version >/dev/null 2>&1 || \\
     (apt-get update && apt-get install -y --no-install-recommends python3-pip)) && \\
    (python3 -m pip install --quiet --break-system-packages pytest==8.4.1 || \\
     python3 -m pip install --quiet pytest==8.4.1 || \\
     pip install --quiet pytest==8.4.1)
"""

NOOP_SHELL = "#!/bin/bash\ntrue\n"
RAISE_SHELL = "#!/bin/bash\nexit 1\n"


def _parse_test_names(test_src: str) -> list[str]:
    import ast
    return [n.name for n in ast.parse(test_src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")]


def _build_image(task_dir: str, tag: str) -> str:
    client = docker_client()
    with tempfile.TemporaryDirectory() as ctx:
        for entry in os.listdir(task_dir):
            s = os.path.join(task_dir, entry)
            d = os.path.join(ctx, entry)
            (shutil.copytree if os.path.isdir(s) else shutil.copy2)(s, d)
        with open(os.path.join(ctx, "Dockerfile"), "a") as f:
            f.write(PYTEST_LAYER)
        image, _ = client.images.build(path=ctx, tag=tag, rm=True)
    return image.id


def load(root: str, ids: list[str] | None = None) -> dict:
    """Discover and convert task dirs under `root`; builds images (cached)."""
    tasks = {}
    names = sorted(ids or [d for d in os.listdir(root)
                           if os.path.isdir(os.path.join(root, d))])
    for name in names:
        task_dir = os.path.join(root, name)
        required = ["task.yaml", "Dockerfile", "solution.sh",
                    os.path.join("tests", "test_outputs.py")]
        if any(not os.path.exists(os.path.join(task_dir, p)) for p in required):
            print(f"[tbench] skip {name}: not a classic-format task dir")
            continue
        meta = yaml.safe_load(open(os.path.join(task_dir, "task.yaml")))
        test_src = open(os.path.join(task_dir, "tests", "test_outputs.py")).read()
        test_names = _parse_test_names(test_src)
        if not test_names:
            print(f"[tbench] skip {name}: no test functions found")
            continue

        tag = f"projectx-tb-{name}"
        t0 = time.time()
        try:
            _build_image(task_dir, tag)
        except Exception as e:
            print(f"[tbench] skip {name}: image build failed: {str(e)[:200]}")
            continue
        client = docker_client()
        workdir = client.images.get(tag).attrs["Config"].get("WorkingDir") or "/app"
        print(f"[tbench] built {tag} in {time.time()-t0:.0f}s (workdir {workdir}, "
              f"{len(test_names)} tests)")

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
                   "solve_timeout": min(float(meta.get("max_agent_timeout_sec") or 90), 90),
                   "verify_timeout": min(float(meta.get("max_test_timeout_sec") or 90), 90)},
            "source": {"benchmark": "terminal-bench", "task_id": name, "path": task_dir},
            **({"planted_flaw": meta["x_planted_flaw"]}
               if meta.get("x_planted_flaw") else {}),
        }
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
            f"/px/tests/test_outputs.py::{c['input']['test']}" for c in cases)
        cmd = (f"cd {tb['workdir']} && TEST_DIR=/px/tests python3 -m pytest "
               f"{selectors} -q -p no:cacheprovider --junitxml=/px/report.xml")
        container = client.containers.create(
            state_ref, command=["bash", "-c", cmd],
            network_disabled=True, mem_limit="1g", nano_cpus=2_000_000_000,
            environment={k: str(v) for k, v in env.items()})
        try:
            container.put_archive("/", _tar_bytes(
                {f"px/tests/{fn}": content for fn, content in tb["test_files"].items()}))
            container.start()
            self._wait(container, tb["verify_timeout"], "verify")
            logs = container.logs(stdout=True, stderr=True)
            xml_raw = self._read_file(container, "/px/report.xml")
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
            "solution_exit_code": exit_code, "exec_ms": exec_ms}).encode()
        artifacts = [storage.put_bytes(f"{prefix}/{n}",
                                       d if isinstance(d, bytes) else d.encode())
                     for n, d in uploads.items()]
        detail = {"cases": verdicts["cases"], "outputs": outputs,
                  "solution_exit_code": exit_code}
        return {"outcome": verdicts["outcome"], "detail": detail,
                "exec_ms": exec_ms, "artifacts": artifacts}


base.register_runtime("tbench", TBenchRuntime())
