"""Harbor / Terminal-Bench v4.x adapter: `separate`-mode tasks.

The v4 format is a different shape from classic terminal-bench:

  task.toml            metadata + declared `artifacts` (files the agent must
                       produce) + [verifier] environment_mode
  instruction.md       the task prompt
  environment/         Dockerfile + data -> the agent's container
  solution/            solve.sh (+ helper files) -> the reference solution,
                       mounted at /solution during solve
  tests/               Dockerfile (deps baked in) + test.sh (entrypoint,
                       writes /logs/verifier/{ctrf.json,reward.txt}) + test*.py

`environment_mode = "separate"` (all 66 v4.0.0 tasks): the verifier runs in a
FRESH container built from tests/Dockerfile, and ONLY the declared artifacts
cross from the solved environment into it. That maps cleanly onto Breakwater's
split solve/verify model — cleaner than the classic docker-commit path, since
the artifact set is an explicit, deterministic byte manifest.

This runtime:
  solve   build environment image, run solve.sh with /solution populated,
          commit, then tar out exactly the declared artifact paths
  verify  build tests image, drop the artifacts back at their declared paths,
          run the task's OWN tests/test.sh verbatim (maximally faithful — no
          re-derived pytest invocation), read CTRF for per-test verdicts

Capabilities: none (no local-eval oracle, outputs are container state), so the
tester runs the determinism / isolation / environment / degenerate-probe
stages and skips mutation/equivalence, same as classic terminal-bench.

Deliberately skipped (reported under LAST_LOAD_REPORT["skipped"]):
  gpu             task.toml requests gpus > 0
  no_tests        no tests/test*.py -> no per-case verdicts to reason about
  solve_network   solve.sh installs packages at solve time; solve runs with
                  networking disabled, so the reference can't run faithfully
  browser         tests image is Playwright/Selenium based; headless browser
                  does not come up under this runtime's container defaults
                  (a known, previously-characterised limitation)
  build_failed    an image failed to build (infrastructure, not a defect)
"""
import base64
import io
import json
import os
import re
import tarfile
import time
import tomllib

import yaml

from .. import storage
from ..runner import docker_client, TrialExecutionError, _tar_bytes
from . import base

NOOP_SHELL = "#!/bin/bash\ntrue\n"
RAISE_SHELL = "#!/bin/bash\nexit 1\n"

LAST_LOAD_REPORT: dict = {}
_INFRA_SKIPS = ("build_failed",)

_NET_INSTALL_RE = re.compile(
    r"\b(?:apt-get|apt)\s+install\b|\bpip3?\s+install\b|\bpip3?\s+download\b|\bpipx\s+(?:install|run)\b|"
    r"\buv\s+(?:pip\s+install|add|tool\s+install|sync)\b|\bnpm\s+(?:install|ci)\b|"
    r"\bpnpm\s+(?:install|add)\b|\byarn\s+(?:install|add)\b|\bcargo\s+(?:install|add|fetch)\b|"
    r"\bgo\s+(?:get|install)\b|\b(?:conda|mamba|micromamba)\s+install\b|"
    r"\bcurl\b[^|]*\bhttps?://|\bwget\b|"
    # `uv run --with pkg` / `uvx --with pkg` / `... --with-requirements f` pull
    # from PyPI at solve time exactly like an install (the `--with` flag may sit
    # on a continued line, so match across newlines).
    r"\buvx?\b[\s\S]{0,400}?--with(?:-requirements)?[ =\t\n]|--with-requirements[ =\t]")
_BROWSER_RE = re.compile(r"playwright|selenium|chromium|puppeteer", re.I)


def _parse_test_names(test_src: str) -> list[str]:
    """pytest ids for every test in the file: bare `test_*` functions and
    `test_*` methods inside `Test*`/`*Test` classes (reported by pytest/CTRF as
    `Class::method`)."""
    import ast
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return []
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name.startswith("test"):
            out.append(n.name)
        elif isinstance(n, ast.ClassDef) and (
                n.name.startswith("Test") or n.name.endswith("Test")):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and m.name.startswith("test"):
                    out.append(f"{n.name}::{m.name}")
    return out


def _service_count(env_dir: str) -> int:
    """Services declared in environment/docker-compose.yaml (0 if none)."""
    for fn in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml"):
        p = os.path.join(env_dir, fn)
        if os.path.exists(p):
            try:
                c = yaml.safe_load(open(p)) or {}
                return len(c.get("services") or {})
            except Exception:
                return 2  # unparseable compose -> assume multi-service, skip
    return 0


def _build(context_dir: str, tag: str) -> str:
    image, _ = docker_client().images.build(path=context_dir, tag=tag, rm=True,
                                            forcerm=True)
    return image.id


def _read_solution_tree(sol_dir: str) -> dict:
    """Every file under solution/ keyed by path relative to that dir, base64'd
    (the task dict is persisted as JSONB, so no raw bytes). Staged at
    /solution/<rel> during solve — solve.sh references e.g. /solution/solve.py,
    /solution/assets/bin/...."""
    out = {}
    for root, _, files in os.walk(sol_dir):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, sol_dir)
            with open(p, "rb") as f:
                out[rel] = base64.b64encode(f.read()).decode("ascii")
    return out


def load(root: str, ids: list[str] | None = None) -> dict:
    tasks = {}
    skipped: dict[str, list[tuple[str, str]]] = {}

    def skip(cat: str, name: str, detail: str) -> None:
        skipped.setdefault(cat, []).append((name, detail))
        print(f"[harbor] skip {name}: {detail}", flush=True)

    names = sorted(ids or [d for d in os.listdir(root)
                           if os.path.isdir(os.path.join(root, d))
                           and os.path.exists(os.path.join(root, d, "task.toml"))])
    for name in names:
        d = os.path.join(root, name)
        need = ["task.toml", "environment/Dockerfile", "solution/solve.sh",
                "tests/Dockerfile", "tests/test.sh"]
        if any(not os.path.exists(os.path.join(d, p)) for p in need):
            skip("format", name, "missing a required v4 file "
                 f"({[p for p in need if not os.path.exists(os.path.join(d, p))]})")
            continue
        with open(os.path.join(d, "task.toml"), "rb") as f:
            cfg = tomllib.load(f)

        mode = cfg.get("verifier", {}).get("environment_mode", "shared")
        if mode != "separate":
            skip("unsupported_mode", name, f"environment_mode={mode!r} (only separate)")
            continue
        if int(cfg.get("environment", {}).get("gpus", 0) or 0) > 0:
            skip("gpu", name, "task requests a GPU")
            continue

        raw_artifacts = list(cfg.get("artifacts", []))
        # dict artifacts ({source, service}) come from a compose sidecar; a
        # multi-service environment/docker-compose.yaml means the same. This
        # runtime runs one container, so the reference can't run faithfully.
        n_services = _service_count(os.path.join(d, "environment"))
        if any(not isinstance(a, str) for a in raw_artifacts) or n_services > 1:
            skip("multi_service", name,
                 f"multi-service task (services={n_services}, "
                 f"dict-artifacts={any(not isinstance(a, str) for a in raw_artifacts)}) "
                 "— single-container runtime can't start sidecars")
            continue

        tests_dir = os.path.join(d, "tests")
        test_files = {fn: open(os.path.join(tests_dir, fn)).read()
                      for fn in os.listdir(tests_dir)
                      if fn.startswith("test") and fn.endswith(".py")
                      and os.path.isfile(os.path.join(tests_dir, fn))}
        cases = []
        for fn, src in sorted(test_files.items()):
            for tn in _parse_test_names(src):
                cases.append(f"{fn}::{tn}")
        if not cases:
            skip("no_tests", name, "no tests/test*.py with test functions")
            continue

        solve_sh = open(os.path.join(d, "solution", "solve.sh")).read()
        # Scan the whole solution tree, not just solve.sh: some tasks put the
        # package pull in a helper script that solve.sh calls.
        solve_blob = solve_sh
        for _sr, _sd, _sf in os.walk(os.path.join(d, "solution")):
            for _fn in _sf:
                if _fn == "solve.sh":
                    continue
                try:
                    solve_blob += "\n" + open(os.path.join(_sr, _fn),
                                              errors="ignore").read()
                except OSError:
                    pass
        if _NET_INSTALL_RE.search(solve_blob):
            skip("solve_network", name,
                 "solution installs/fetches packages at solve time; solve runs "
                 "with networking disabled so the reference cannot run faithfully")
            continue
        if _BROWSER_RE.search(test_files.get("__dockerfile__", "") +
                              open(os.path.join(tests_dir, "Dockerfile")).read()):
            skip("browser", name, "Playwright/Selenium verifier — headless browser "
                 "does not start under this runtime's container defaults")
            continue

        env_tag = f"projectx-hb-env-{name}"
        test_tag = f"projectx-hb-test-{name}"
        t0 = time.time()
        try:
            _build(os.path.join(d, "environment"), env_tag)
            _build(tests_dir, test_tag)
        except Exception as e:
            skip("build_failed", name, f"image build failed: {str(e)[:200]}")
            continue
        client = docker_client()
        env_workdir = client.images.get(env_tag).attrs["Config"].get("WorkingDir") or "/app"
        print(f"[harbor] built {name} in {time.time()-t0:.0f}s "
              f"({len(cases)} tests, {len(cfg.get('artifacts', []))} artifacts)", flush=True)

        tid = f"hb_{name.replace('-', '_')}"
        tasks[tid] = {
            "runtime": "harbor",
            "capabilities": [],
            "cases": [{"input": {"test": c}, "expected": "pass"} for c in cases],
            "oracle": solve_sh,
            "agent": solve_sh,
            "noop": NOOP_SHELL,
            "probe_solutions": {"noop_none": NOOP_SHELL, "raise_probe": RAISE_SHELL},
            "verifier": "\n\n".join(f"# {fn}\n{src}" for fn, src in test_files.items()),
            "hb": {
                "env_image": env_tag, "test_image": test_tag,
                "env_workdir": env_workdir,
                "artifacts": list(cfg.get("artifacts", [])),
                # [[verifier.collect]]: commands Harbor runs in the agent
                # container after the agent, before artifact collection — e.g.
                # shadow-relay diffs /app into /tmp/agent.patch. Run them at the
                # end of solve so the committed state has the collected files.
                "collect": [c["command"] for c in
                            (cfg.get("verifier", {}).get("collect") or [])
                            if isinstance(c, dict) and c.get("command")],
                "solution_tree": _read_solution_tree(os.path.join(d, "solution")),
                "solve_timeout": min(float(cfg.get("agent", {}).get("timeout_sec") or 300), 300),
                "verify_timeout": min(float(cfg.get("verifier", {}).get("timeout_sec") or 300), 300),
                "env_env": dict(cfg.get("environment", {}).get("env", {}) or {}),
                "verifier_env": dict(cfg.get("verifier", {}).get("env", {}) or {}),
            },
            "source": {"benchmark": "terminal-bench-v4", "task_id": name, "path": d,
                       "task_name": cfg.get("task", {}).get("name", name)},
        }

    LAST_LOAD_REPORT.clear()
    LAST_LOAD_REPORT.update({"loaded": sorted(tasks), "skipped": skipped})
    n_sk = sum(len(v) for v in skipped.values())
    if n_sk:
        tally = " ".join(f"{k}:{len(v)}" for k, v in sorted(skipped.items()))
        print(f"[harbor] loaded {len(tasks)}; skipped {n_sk} — {tally}", flush=True)
        for cat in _INFRA_SKIPS:
            for nm, det in skipped.get(cat, []):
                print(f"[harbor] WARNING scope reduced by infrastructure "
                      f"({cat}): {nm}: {det}", flush=True)
    return tasks


class HarborRuntime:
    def _wait(self, container, timeout, label):
        try:
            return container.wait(timeout=timeout)
        except Exception as e:
            try:
                container.kill()
            except Exception:
                pass
            raise TrialExecutionError(f"{label}: timeout/wait failure: {e}") from e

    def _mkparents(self, paths: list[str]) -> str:
        dirs = sorted({os.path.dirname(p) for p in paths if os.path.dirname(p)})
        return " && ".join(f"mkdir -p {d}" for d in dirs) or "true"

    def _solve(self, task, solution_src: str, env: dict):
        hb = task["hb"]
        client = docker_client()
        payload = {f"solution/{rel}": base64.b64decode(b64)
                   for rel, b64 in hb["solution_tree"].items()}
        payload["solution/solve.sh"] = (
            solution_src.encode() if isinstance(solution_src, str) else solution_src)
        # solve.sh, then the [[verifier.collect]] hooks (Harbor runs them in the
        # agent container before collecting artifacts); solve.sh's exit code is
        # what gets reported.
        script = "bash /solution/solve.sh; __px_rc=$?\n"
        for cmd in hb.get("collect", []):
            script += f"( {cmd} ) || true\n"
        script += "exit $__px_rc\n"
        c = client.containers.create(
            hb["env_image"], command=["bash", "-c", script],
            working_dir=hb["env_workdir"], network_disabled=True,
            mem_limit="4g", nano_cpus=2_000_000_000,
            environment={**{k: str(v) for k, v in hb["env_env"].items()},
                         **{k: str(v) for k, v in env.items()}})
        try:
            c.put_archive("/", _tar_bytes_bin(payload))
            c.start()
            info = self._wait(c, hb["solve_timeout"], "solve")
            logs = c.logs(stdout=True, stderr=True)
            state = c.commit()
            return state.id, logs, info.get("StatusCode", -1)
        finally:
            try:
                c.remove(force=True)
            except Exception:
                pass

    def _extract_artifacts(self, state_ref: str, paths: list[str]) -> tuple[dict, list]:
        """Tar out each declared artifact path from the solved container.
        `docker get_archive(p)` names members relative to p's PARENT, so an
        absolute destination is join(dirname(p without trailing /), member).
        Handles a single file, a directory (`/app/`), and a nested directory
        (`/app/app/`) identically."""
        client = docker_client()
        c = client.containers.create(state_ref, command=["true"])
        got, missing = {}, []
        try:
            for p in paths:
                parent = os.path.dirname(p.rstrip("/")) or "/"
                try:
                    stream, _ = c.get_archive(p)
                    raw = b"".join(stream)
                    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                        n_files = 0
                        for m in tar.getmembers():
                            if m.isfile() or m.issym() or m.islnk():
                                dest = os.path.normpath(os.path.join(parent, m.name))
                                got[dest] = tar.extractfile(m).read() if m.isfile() else b""
                                n_files += 1
                        if n_files == 0:
                            missing.append(p)  # path existed but held nothing usable
                except Exception:
                    missing.append(p)
        finally:
            try:
                c.remove(force=True)
            except Exception:
                pass
        return got, missing

    def _verify(self, task, artifacts: dict, env: dict):
        hb = task["hb"]
        client = docker_client()
        mkparents = self._mkparents(hb["artifacts"])
        cmd = (f"mkdir -p /logs/verifier && {mkparents} && "
               f"cd / && bash /tests/test.sh; "
               f"echo __PX_TESTSH_EXIT=$? ")
        c = client.containers.create(
            hb["test_image"], command=["bash", "-c", cmd],
            network_disabled=True, mem_limit="4g", nano_cpus=2_000_000_000,
            environment={**{k: str(v) for k, v in hb["verifier_env"].items()},
                         **{k: str(v) for k, v in env.items()}})
        try:
            payload = {}
            for path, data in artifacts.items():
                payload[path.lstrip("/")] = data if isinstance(data, (bytes, bytearray)) \
                    else data.encode()
            if payload:
                c.put_archive("/", _tar_bytes_bin(payload))
            c.start()
            self._wait(c, hb["verify_timeout"], "verify")
            logs = c.logs(stdout=True, stderr=True)
            ctrf = self._read_file(c, "/logs/verifier/ctrf.json")
            reward = self._read_file(c, "/logs/verifier/reward.txt")
        finally:
            try:
                c.remove(force=True)
            except Exception:
                pass

        verdicts = self._parse_ctrf(ctrf, task["cases"], logs, reward)
        return verdicts, logs, ctrf

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
    def _parse_ctrf(ctrf_raw, cases, logs, reward_raw):
        want = [c["input"]["test"] for c in cases]
        if ctrf_raw is None:
            # No CTRF at all -> infrastructure failure, not a benchmark verdict.
            raise TrialExecutionError(
                f"verify: no CTRF report produced; logs tail={logs[-500:]!r}")
        try:
            doc = json.loads(ctrf_raw)
            tests = doc["results"]["tests"]
        except Exception as e:
            raise TrialExecutionError(f"verify: unparseable CTRF ({e})")

        by_name = {}
        for t in tests:
            nm = t.get("name", "")
            by_name[nm] = t
            if "::" in nm:
                by_name.setdefault(nm.split("::", 1)[1], t)  # bare test name too

        verdicts, any_err = [], False
        for i, w in enumerate(want):
            t = by_name.get(w) or by_name.get(w.split("::", 1)[1] if "::" in w else w)
            if t is None:
                any_err = True
                verdicts.append({"case": i, "ok": False,
                                 "verifier_error": "test not in CTRF report"})
                continue
            st = t.get("status")
            ok = st == "passed"
            v = {"case": i, "ok": ok, "detail": (t.get("message") or st or "")[:400]}
            if st not in ("passed", "failed"):
                any_err = True
                v["verifier_error"] = f"status={st}"
            verdicts.append(v)

        reward = None
        if reward_raw is not None:
            reward = reward_raw.decode(errors="replace").strip()
        outcome = ("error" if any_err
                   else "pass" if all(v["ok"] for v in verdicts) else "fail")
        return {"outcome": outcome, "cases": verdicts, "reward_txt": reward}

    def run_trial(self, trial: dict, task: dict) -> dict:
        spec = trial.get("spec") or {}
        env = spec.get("env") or {}
        mode = spec.get("mode", "full")
        hb = task["hb"]
        t0 = time.monotonic()
        uploads = {}

        if mode == "full":
            src = spec.get("solution_source") or base.solution_from(
                task, spec.get("solution", "agent"))
            state_ref, solve_logs, exit_code = self._solve(task, src, env)
            uploads["solve.log"] = solve_logs
            artifacts, missing = self._extract_artifacts(state_ref, hb["artifacts"])
            uploads["artifact_manifest.json"] = json.dumps(
                {"present": sorted(artifacts), "missing": missing}).encode()
        else:
            artifacts = {k: base64.b64decode(v)
                         for k, v in spec["outputs"][0]["artifacts"].items()}
            missing = spec["outputs"][0].get("missing", [])
            exit_code = None

        verdicts, verify_logs, ctrf = self._verify(task, artifacts, env)
        uploads["verify.log"] = verify_logs
        if ctrf is not None:
            uploads["ctrf.json"] = ctrf

        exec_ms = (time.monotonic() - t0) * 1000.0
        # artifacts travel in `outputs` (base64) so verify_only replays re-judge
        # the exact bytes the solve produced
        art_out = {k: base64.b64encode(
                       v if isinstance(v, (bytes, bytearray)) else v.encode()
                   ).decode("ascii")
                   for k, v in artifacts.items()}
        outputs = [{"output": None, "artifacts": art_out, "missing": missing}
                   for _ in task["cases"]]
        prefix = (f"{trial['experiment_id']}/{trial['trial_id'].replace('/', '_')}"
                  f"/attempt-{trial.get('attempts', 0)}")
        uploads["meta.json"] = json.dumps({
            "trial_id": trial["trial_id"], "task_id": trial["task_id"],
            "runtime": "harbor", "mode": mode, "env": env,
            "artifacts_present": sorted(artifacts), "artifacts_missing": missing,
            "solution_exit_code": exit_code, "reward_txt": verdicts.get("reward_txt"),
            "exec_ms": exec_ms}).encode()
        stored = [storage.put_bytes(f"{prefix}/{n}",
                                    v if isinstance(v, (bytes, bytearray)) else v.encode())
                  for n, v in uploads.items()]

        detail = {"cases": verdicts["cases"], "outputs": outputs,
                  "solution_exit_code": exit_code,
                  "artifacts_missing": missing,
                  "reward_txt": verdicts.get("reward_txt")}
        return {"outcome": verdicts["outcome"], "detail": detail,
                "exec_ms": exec_ms, "artifacts": stored}


def _tar_bytes_bin(files: dict) -> bytes:
    """Like runner._tar_bytes but values are already bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


base.register_runtime("harbor", HarborRuntime())
