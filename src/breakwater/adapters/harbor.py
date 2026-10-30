"""Harbor / Terminal-Bench runtime: run tasks the way Harbor does, on Docker.

Fidelity contract (mirrors harbor 0.25, single-step `separate`-mode tasks):

  solve   the agent environment image (built from environment/, or the task's
          prebuilt image) runs `sh -c "sleep infinity"`; /logs/{agent,verifier,
          artifacts} exist chmod 777; the oracle uploads solution/ to
          /solution, `chmod +x` as root, then runs
          `(/solution/solve.sh) > /logs/agent/oracle.txt 2>&1` via `bash -c`
          as the agent user with [solution.env] + DEBIAN_FRONTEND. Then the
          [[verifier.collect]] hooks run. Network, CPU and memory follow the
          task's own [environment] (Harbor's default network is PUBLIC).
  collect every declared artifact path, plus the /logs/artifacts convention
          dir, is archived out of the live container into a content-addressed
          Bundle (breakwater.bundle) with Harbor's upload normalization.
  verify  the verifier image (tests/ build context, tests bundled at /tests)
          starts fresh; /logs/verifier is emptied; each artifact directory is
          EMPTIED and re-uploaded, each artifact file gets its parent created;
          stale reward files are removed; `(/tests/test.sh) >
          /logs/verifier/test-stdout.txt 2>&1` runs as the verifier user with
          [verifier.env]. The verdict is /logs/verifier/reward.json if it
          exists, else reward.txt — test.sh's exit code is ignored, and a
          missing reward is an error, exactly as in Harbor.

Per-test/criterion verdicts (CTRF, RewardKit reward-details.json) are
recorded as diagnostics; they never override the reward.

Known, documented deltas from Harbor: no allowlist egress emulation (such
tasks are skipped), memory requests are clamped to what the Docker VM has
(recorded per trial in `fidelity`), and logs are copied out of the
container instead of bind-mounted (bind mounts are what make RewardKit's
Worker fail closed on macOS hosts).

Trial spec (all optional): mode full|solve|snapshot|verify_only;
solution "oracle"|"noop" or solution_source (replaces solve.sh); outputs
[{bundle: "sha256:..."}] for verify_only; env {..} extra env for the
verify (and solve) exec; network "task"|"on"|"off" override; metrics
{name: regex} pulled from test-stdout.
"""
from __future__ import annotations

import ast
import base64
import contextlib
import fcntl
import io
import json
import os
import re
import tarfile
import threading
import time

from .. import cas, storage
from ..bundle import Bundle, add_archive, under, upload_tar
from ..runner import TrialExecutionError, docker_client
from . import base
from .harbor_task import (HarborTaskSpec, artifact_excludes, artifact_paths,
                          parse_task, unsupported_reason)

NOOP_SHELL = "#!/bin/bash\ntrue\n"
RAISE_SHELL = "#!/bin/bash\nexit 1\n"
ORACLE = "__harbor_oracle__"        # sentinel: run the task's own solution/ tree
# Bumped whenever a fidelity fix can change a verdict; part of every
# content-addressed trial id so results from an older runtime are never reused.
RUNTIME_VERSION = "hb2"

LAST_LOAD_REPORT: dict = {}
_INFRA_SKIPS = ("build_failed",)
_LOCK_DIR = os.path.join(cas.BW_HOME, "locks")


# ======================================================================
# loading


def _pytest_ids(src: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
            out.append(n.name)
        elif isinstance(n, ast.ClassDef) and (n.name.startswith("Test") or n.name.endswith("Test")):
            out += [f"{n.name}::{m.name}" for m in n.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and m.name.startswith("test")]
    return out


def _criteria(src: str) -> list[str]:
    """RewardKit @criterion functions (the per-check names it reports)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                fn = d.func if isinstance(d, ast.Call) else d
                if getattr(fn, "id", getattr(fn, "attr", None)) == "criterion":
                    out.append(n.name)
    return out


def discover_cases(tests_dir: str) -> list[str]:
    """Static per-check names, best effort: pytest ids from test*.py and
    RewardKit criteria from any tests/*.py. Runtime reports may add names;
    the reward is authoritative either way."""
    names = []
    if not os.path.isdir(tests_dir):
        return names
    for fn in sorted(os.listdir(tests_dir)):
        p = os.path.join(tests_dir, fn)
        if not (fn.endswith(".py") and os.path.isfile(p)):
            continue
        src = open(p, errors="ignore").read()
        if fn.startswith("test"):
            names += [f"{fn}::{t}" for t in _pytest_ids(src)]
        names += _criteria(src)
    return names


def task_dict(spec: HarborTaskSpec) -> dict:
    """Breakwater task representation for one Harbor task version."""
    cases = discover_cases(os.path.join(spec.path, "tests")) or ["reward"]
    sol_dir = os.path.join(spec.path, "solution")
    tree = {}
    for root, _, files in os.walk(sol_dir):
        for fn in files:
            p = os.path.join(root, fn)
            with open(p, "rb") as f:
                tree[os.path.relpath(p, sol_dir)] = [
                    base64.b64encode(f.read()).decode("ascii"),
                    0o755 if os.access(p, os.X_OK) else 0o644]
    return {
        "runtime": "harbor",
        "capabilities": [],
        "cases": [{"input": {"test": c}, "expected": "pass"} for c in cases],
        "oracle": ORACLE, "agent": ORACLE, "noop": NOOP_SHELL,
        "probe_solutions": {"noop_none": NOOP_SHELL, "raise_probe": RAISE_SHELL},
        "verifier": f"harbor:{spec.name}@{spec.digests['verifier'][:12]}",
        "hb": {**spec.to_dict(), "solution_tree": tree},
        "source": {"benchmark": "harbor", "task_id": spec.name, "path": spec.path,
                   "task_name": spec.task_name, "version": spec.version,
                   "verifier_digest": spec.digests["verifier"]},
    }


def load(root: str, ids: list[str] | None = None, *, build: bool = True,
         offline_solve: bool = False) -> dict:
    """Load Harbor tasks under `root` (a tasks/ dir). Unsupported tasks are
    skipped with a reason (LAST_LOAD_REPORT); nothing is skipped silently."""
    tasks, skipped = {}, {}

    def skip(cat, name, detail):
        skipped.setdefault(cat, []).append((name, detail))
        print(f"[harbor] skip {name}: {detail}", flush=True)

    names = sorted(ids or [d for d in os.listdir(root)
                           if os.path.isfile(os.path.join(root, d, "task.toml"))])
    for name in names:
        d = os.path.join(root, name)
        need = ["task.toml", "solution/solve.sh", "tests/test.sh"]
        lacking = [p for p in need if not os.path.exists(os.path.join(d, p))]
        if lacking:
            skip("format", name, f"missing {lacking}")
            continue
        spec = parse_task(d)
        why = unsupported_reason(spec, offline_solve=offline_solve)
        if why:
            skip(why[0], name, why[1])
            continue
        if build:
            try:
                RUNTIME.ensure_images(spec)
            except Exception as e:
                skip("build_failed", name, f"image build failed: {str(e)[:300]}")
                continue
        tasks[f"hb_{spec.short_id}"] = task_dict(spec)

    LAST_LOAD_REPORT.clear()
    LAST_LOAD_REPORT.update({"loaded": sorted(tasks), "skipped": skipped})
    n_sk = sum(len(v) for v in skipped.values())
    if n_sk:
        tally = " ".join(f"{k}:{len(v)}" for k, v in sorted(skipped.items()))
        print(f"[harbor] loaded {len(tasks)}; skipped {n_sk} — {tally}", flush=True)
        for cat in _INFRA_SKIPS:
            for nm, det in skipped.get(cat, []):
                print(f"[harbor] WARNING scope reduced by infrastructure ({cat}): {nm}: {det}",
                      flush=True)
    return tasks


# ======================================================================
# docker helpers


@contextlib.contextmanager
def _file_lock(name: str):
    os.makedirs(_LOCK_DIR, exist_ok=True)
    with open(os.path.join(_LOCK_DIR, re.sub(r"[^\w.-]", "_", name)), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _image_tag(kind: str, spec_name: str, digest: str) -> str:
    return f"breakwater/{kind}-{spec_name}:{digest[:12]}"


def _ensure_image(context: str, tag: str, prebuilt: str | None) -> str:
    client = docker_client()
    if prebuilt:
        with _file_lock(prebuilt):
            try:
                client.images.get(prebuilt)
            except Exception:
                client.images.pull(prebuilt)
        return prebuilt
    with _file_lock(tag):
        try:
            client.images.get(tag)
        except Exception:
            client.images.build(path=context, tag=tag, rm=True, forcerm=True)
    return tag


def _vm_memory_mb() -> int:
    try:
        return int(docker_client().info()["MemTotal"] / (1 << 20))
    except Exception:
        return 0


def _resources(env, fidelity: dict, phase: str) -> dict:
    kw = {}
    if env.cpus:
        kw["nano_cpus"] = int(float(env.cpus) * 1e9)
    if env.memory_mb:
        mem = int(env.memory_mb)
        cap = _vm_memory_mb() - 512
        if cap > 0 and mem > cap:
            fidelity.setdefault("clamped", []).append(
                f"{phase} memory_mb {mem} -> {cap} (Docker VM limit)")
            mem = cap
        kw["mem_limit"] = f"{mem}m"
    return kw


def _no_network(task_policy: str, override: str | None) -> bool:
    if override == "off":
        return True
    if override == "on":
        return False
    return task_policy == "no_network"


def _exec(container, cmd: str, *, user=None, env=None, timeout=None) -> tuple[int | None, bytes, float]:
    """`bash -c cmd` in a running container with a wall-clock deadline.
    Returns (exit_code or None on timeout, output, seconds). On timeout the
    container is killed — the exec cannot be cancelled otherwise."""
    api = docker_client().api
    eid = api.exec_create(container.id, ["bash", "-c", cmd], user=str(user or ""),
                          environment=env or None)["Id"]
    box = {}
    t0 = time.monotonic()

    def run():
        try:
            box["out"] = api.exec_start(eid, stream=False)
        except Exception as e:     # killed container, daemon hiccup
            box["err"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    dt = time.monotonic() - t0
    if th.is_alive():
        with contextlib.suppress(Exception):
            container.kill()
        th.join(10)
        return None, b"", dt
    if "err" in box:
        raise TrialExecutionError(f"exec failed: {box['err']}")
    return api.exec_inspect(eid)["ExitCode"], box.get("out") or b"", dt


def _root(container, cmd: str):
    code, out, _ = _exec(container, cmd, user="root", timeout=120)
    if code != 0:
        raise TrialExecutionError(f"setup command failed ({code}): {cmd[:120]}: {out[-300:]!r}")


def _q(p: str) -> str:
    return "'" + p.replace("'", "'\"'\"'") + "'"


def _empty_dirs_cmd(paths) -> str:
    """Harbor's BaseEnvironment._empty_dirs_command (posix), chmod=True."""
    parts = []
    for p in paths:
        q = _q(p)
        parts += [f"if [ -L {q} ] || {{ [ -e {q} ] && [ ! -d {q} ]; }}; then rm -rf {q}; fi",
                  f"mkdir -p {q}", f"find {q} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +",
                  f"chmod 777 {q}"]
    return " && ".join(parts)


def _read(container, path: str) -> bytes | None:
    try:
        stream, _ = container.get_archive(path)
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            m = tar.getmembers()[0]
            return tar.extractfile(m).read() if m.isfile() else None
    except Exception:
        return None


def _expand(env: dict) -> dict:
    """${VAR} / ${VAR:-default} from the host, like harbor's resolve_env_vars."""
    def sub(v):
        return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}",
                      lambda m: os.environ.get(m.group(1), m.group(2) or ""), str(v))
    return {k: sub(v) for k, v in env.items()}


def _start(image: str, env_spec, fidelity: dict, phase: str, network_override=None):
    # Harbor's no-network compose uses `network_mode: none`, which keeps a
    # loopback interface and a /etc/hosts entry for localhost; docker-py's
    # network_disabled=True does not, and breaks verifiers that talk to a
    # local worker or chromedriver over localhost.
    net = {"network_mode": "none"} if _no_network(env_spec.network, network_override) else {}
    c = docker_client().containers.run(
        image, command=["sh", "-c", "sleep infinity"], detach=True, **net,
        labels={"breakwater.phase": phase},
        **_resources(env_spec, fidelity, phase))
    return c


# ======================================================================
# runtime


class HarborRuntime:
    def ensure_images(self, spec: HarborTaskSpec) -> tuple[str, str]:
        env_img = _ensure_image(spec.agent_env.context,
                                _image_tag("env", spec.name, spec.agent_env.digest),
                                spec.agent_env.docker_image)
        v = spec.verifier_env
        ver_img = _ensure_image(v.context, _image_tag("verifier", spec.name, v.digest),
                                v.docker_image)
        return env_img, ver_img

    # ---------------------------------------------------------------- solve
    def solve(self, spec: HarborTaskSpec, task: dict, solution: str, env: dict,
              fidelity: dict, network_override=None, timeout_cap: float | None = None):
        """Run a solution in the agent env; return (Bundle, exit_code, log, secs)."""
        env_img, _ = self.ensure_images(spec)
        c = _start(env_img, spec.agent_env, fidelity, "solve", network_override)
        try:
            _root(c, "mkdir -p /logs/agent /logs/verifier /logs/artifacts && "
                     "chmod 777 /logs/agent /logs/verifier /logs/artifacts")
            if solution == ORACLE:
                tree = task["hb"]["solution_tree"]
                files = {f"/solution/{rel}": (base64.b64decode(b64), mode)
                         for rel, (b64, mode) in tree.items()}
            else:
                files = {"/solution/solve.sh": (solution.encode(), 0o755)}
            from ..bundle import Entry
            c.put_archive("/", upload_tar({p: Entry("f", m, d) for p, (d, m) in files.items()}))
            _root(c, "chmod +x /solution/solve.sh")
            run_env = {**_expand(spec.solution_env_vars), **env,
                       "DEBIAN_FRONTEND": "noninteractive"}
            timeout = spec.agent_timeout_sec or 3600
            if timeout_cap:
                if timeout > timeout_cap:
                    fidelity.setdefault("clamped", []).append(
                        f"solve timeout {timeout:.0f}s -> {timeout_cap:.0f}s")
                timeout = min(timeout, timeout_cap)
            code, _, secs = _exec(c, "(/solution/solve.sh) > /logs/agent/oracle.txt 2>&1",
                                  user=spec.agent_user, env=run_env, timeout=timeout)
            log = _read(c, "/logs/agent/oracle.txt") or b""
            if code is None:
                raise TrialExecutionError(f"solve timed out after {timeout:.0f}s")
            for cmd in spec.collect:      # Harbor runs these in the agent env
                _exec(c, cmd, user="root", timeout=600)
            bundle = self._collect(c, spec)
            return bundle, code, log, secs
        finally:
            with contextlib.suppress(Exception):
                c.remove(force=True)

    def snapshot(self, spec: HarborTaskSpec, fidelity: dict) -> Bundle:
        """Declared artifact paths as the environment image ships them
        (pre-solve). Used by the `unsolved_path` ablation."""
        env_img, _ = self.ensure_images(spec)
        c = _start(env_img, spec.agent_env, fidelity, "snapshot", "off")
        try:
            return self._collect(c, spec)
        finally:
            with contextlib.suppress(Exception):
                c.remove(force=True)

    def _collect(self, container, spec: HarborTaskSpec) -> Bundle:
        paths = artifact_paths(spec)
        excl = artifact_excludes(spec)
        b = Bundle(declared=paths)
        for p in paths:
            try:
                stream, _ = container.get_archive(p)
            except Exception:
                continue          # absent artifact: Harbor skips it too
            add_archive(b, b"".join(stream), p, excl.get(p))
        return b

    # ---------------------------------------------------------------- verify
    def verify(self, spec: HarborTaskSpec, bundle: Bundle, env: dict, fidelity: dict,
               network_override=None, metrics: dict | None = None) -> dict:
        _, ver_img = self.ensure_images(spec)
        c = _start(ver_img, spec.verifier_env, fidelity, "verify", network_override)
        try:
            _root(c, _empty_dirs_cmd(["/logs/verifier"]))
            for p in artifact_paths(spec):
                sub = bundle.subtree(p)
                if not sub:
                    continue                      # missing artifact: not uploaded
                root = p.rstrip("/") or "/"
                if root in sub and sub[root].kind == "f":
                    parent = os.path.dirname(root) or "/"
                    _root(c, f"mkdir -p {_q(parent)} && chmod 777 {_q(parent)}")
                    c.put_archive("/", upload_tar({root: sub[root]}))
                else:
                    _root(c, _empty_dirs_cmd([root]))
                    c.put_archive("/", upload_tar({k: v for k, v in sub.items() if k != root}))
            _root(c, "rm -f -- /logs/verifier/reward.txt /logs/verifier/reward.json")
            _root(c, f"chmod +x {spec.test_script}")
            run_env = {**_expand(spec.verifier_env_vars), **env}
            timeout = spec.verifier_timeout_sec or 600
            code, _, secs = _exec(
                c, f"({spec.test_script}) > /logs/verifier/test-stdout.txt 2>&1",
                user=spec.verifier_user, env=run_env, timeout=timeout)
            files = {}
            if code is not None:
                for n in ("reward.json", "reward.txt", "ctrf.json", "reward-details.json",
                          "test-stdout.txt"):
                    data = _read(c, f"/logs/verifier/{n}")
                    if data is not None:
                        files[n] = data
            return _verdict(code, secs, timeout, files, metrics or {})
        finally:
            with contextlib.suppress(Exception):
                c.remove(force=True)

    # ---------------------------------------------------------------- trial
    def run_trial(self, trial: dict, task: dict) -> dict:
        spec_d = trial.get("spec") or {}
        hb = HarborTaskSpec.from_dict({k: v for k, v in task["hb"].items()
                                       if k != "solution_tree"})
        mode = spec_d.get("mode", "full")
        env = {k: str(v) for k, v in (spec_d.get("env") or {}).items()}
        net = spec_d.get("network")
        fidelity = {"runtime": "breakwater-native", "runtime_version": RUNTIME_VERSION,
                    "harbor_semantics": "0.25"}
        t0 = time.monotonic()
        uploads, detail = {}, {}

        if mode in ("full", "solve"):
            sol = spec_d.get("solution_source") or {
                "oracle": ORACLE, "agent": ORACLE, "noop": NOOP_SHELL,
            }.get(spec_d.get("solution", "oracle"), ORACLE)
            bundle, exit_code, log, secs = self.solve(
                hb, task, sol, env, fidelity, net, spec_d.get("solve_timeout_cap"))
            uploads["solve.log"] = log
            detail.update(solution_exit_code=exit_code, solve_s=round(secs, 3))
        elif mode == "snapshot":
            bundle = self.snapshot(hb, fidelity)
        else:
            out0 = spec_d["outputs"][0]
            if out0.get("bundle"):
                bundle = Bundle.from_tar(cas.get(out0["bundle"]), artifact_paths(hb))
            else:   # legacy receipts: base64 {path: bytes}
                from ..bundle import Entry
                bundle = Bundle({k: Entry("f", 0o644, base64.b64decode(v))
                                 for k, v in out0["artifacts"].items()}, artifact_paths(hb))
            detail["solution_exit_code"] = None

        digest = cas.put(bundle.to_tar())
        detail.update(bundle=digest, artifacts_missing=bundle.missing())

        if mode in ("solve", "snapshot"):
            outcome = "pass" if mode == "snapshot" or detail["solution_exit_code"] == 0 else "fail"
            detail["cases"] = []
        else:
            v = self.verify(hb, bundle, env, fidelity, net, spec_d.get("metrics"))
            v.pop("_files", None)
            raw = v.pop("_raw", {})
            for n in ("test-stdout.txt", "ctrf.json", "reward-details.json", "reward.json",
                      "reward.txt"):
                if n in raw:
                    uploads[n] = raw[n]
            v["cases"] = _align(task["cases"], v.pop("checks"))
            outcome = v.pop("outcome")
            detail.update(v)

        exec_ms = (time.monotonic() - t0) * 1000.0
        detail["outputs"] = [{"output": None, "bundle": digest} for _ in task["cases"]]
        detail["fidelity"] = fidelity
        prefix = (f"{trial['experiment_id']}/{trial['trial_id'].replace('/', '_')}"
                  f"/attempt-{trial.get('attempts', 0)}")
        uploads["meta.json"] = json.dumps({
            "trial_id": trial["trial_id"], "task_id": trial["task_id"], "runtime": "harbor",
            "mode": mode, "env": env, "network": net, "bundle": digest,
            "verifier_digest": hb.digests["verifier"], "exec_ms": exec_ms,
            "fidelity": fidelity}).encode()
        stored = [storage.put_bytes(f"{prefix}/{n}", d if isinstance(d, bytes) else d.encode())
                  for n, d in uploads.items()]
        return {"outcome": outcome, "detail": detail, "exec_ms": exec_ms, "artifacts": stored}


# ======================================================================
# verdicts


def primary_reward(rewards: dict) -> float | None:
    """Same rule as Terminal-Bench's regrade tooling: the "reward" key, or the
    only key."""
    if not isinstance(rewards, dict) or not rewards:
        return None
    v = rewards.get("reward")
    if v is None and len(rewards) == 1:
        v = next(iter(rewards.values()))
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _numeric_leaves(obj, prefix="", out=None, limit=64):
    out = {} if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _numeric_leaves(v, f"{prefix}.{k}" if prefix else str(k), out, limit)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:16]):
            _numeric_leaves(v, f"{prefix}[{i}]", out, limit)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix] = obj
    return out


def _verdict(code, secs, timeout, files: dict, metric_res: dict) -> dict:
    v = {"verifier_wall_s": round(secs, 3), "verifier_exit_code": code,
         "reward": None, "primary_reward": None, "reward_source": None,
         "error_kind": None, "checks": [], "metrics": {}, "_files": set(files),
         "_raw": files}
    if code is None:
        v.update(outcome="error", error_kind="verifier_timeout",
                 error=f"verifier exceeded {timeout:.0f}s")
        return v
    try:
        if "reward.json" in files:
            v["reward_source"] = "reward.json"
            rewards = json.loads(files["reward.json"])
            if not isinstance(rewards, dict):
                rewards = {"reward": rewards}
        elif "reward.txt" in files:
            v["reward_source"] = "reward.txt"
            rewards = {"reward": float(files["reward.txt"].decode().strip())}
        else:
            raise FileNotFoundError("no reward.json or reward.txt written")
        pr = primary_reward(rewards)
        if pr is None:
            raise ValueError(f"no primary reward in {rewards!r}")
        v.update(reward=rewards, primary_reward=pr,
                 outcome="pass" if pr >= 1.0 else "fail")
    except FileNotFoundError as e:
        v.update(outcome="error", error_kind="no_reward", error=str(e))
    except (ValueError, json.JSONDecodeError) as e:
        v.update(outcome="error", error_kind="bad_reward", error=str(e)[:300])

    if "ctrf.json" in files:
        try:
            doc = json.loads(files["ctrf.json"])
            v["ctrf_summary"] = doc.get("results", {}).get("summary")
            v["checks"] = [{"name": t.get("name", ""), "status": t.get("status"),
                            "message": (t.get("message") or "")[:300]}
                           for t in doc.get("results", {}).get("tests", [])]
        except Exception:
            v["ctrf_summary"] = {"unparseable": True}
    if not v["checks"] and "reward-details.json" in files:
        with contextlib.suppress(Exception):
            det = json.loads(files["reward-details.json"])

            def walk(node):
                if isinstance(node, dict):
                    for c in node.get("criteria", []) or []:
                        v["checks"].append({"name": c.get("name", ""),
                                            "status": "passed" if c.get("value") == 1 else "failed",
                                            "message": str(c.get("error") or c.get("reasoning") or "")[:300]})
                    for comp in node.get("components", []) or []:
                        walk(comp.get("detail"))
            for node in (det.values() if isinstance(det, dict) else []):
                walk(node)
    if "reward-details.json" in files:
        with contextlib.suppress(Exception):
            v["metrics"].update({f"details.{k}": x for k, x in _numeric_leaves(
                json.loads(files["reward-details.json"])).items()})
    stdout = files.get("test-stdout.txt", b"").decode(errors="replace")
    for name, rx in metric_res.items():
        hits = re.findall(rx, stdout)
        if hits:
            with contextlib.suppress(ValueError):
                v["metrics"][name] = float(hits[-1] if isinstance(hits[-1], str) else hits[-1][0])
    v["stdout_tail"] = stdout[-2000:]
    return v


def _align(cases: list[dict], checks: list[dict]) -> list[dict]:
    """Per-case verdict vector aligned to the static case list; report-only
    names are appended. Each entry: {case, name, ok, detail}."""
    by = {}
    for ch in checks:
        nm = ch["name"]
        by[nm] = ch
        if "::" in nm:
            by.setdefault(nm.split("::")[-1], ch)
    out, used = [], set()
    for i, c in enumerate(cases):
        want = c["input"]["test"]
        ch = by.get(want) or by.get(want.split("::")[-1])
        if ch is None:
            out.append({"case": i, "name": want, "ok": None, "detail": "not reported"})
            continue
        used.add(ch["name"])
        out.append({"case": i, "name": want, "ok": ch["status"] == "passed",
                    "detail": ch["message"] or ch["status"]})
    for ch in checks:
        if ch["name"] not in used and ch["name"] not in {c["input"]["test"] for c in cases}:
            out.append({"case": len(out), "name": ch["name"], "ok": ch["status"] == "passed",
                        "detail": ch["message"] or ch["status"], "unlisted": True})
    return out


RUNTIME = HarborRuntime()
base.register_runtime("harbor", RUNTIME)
