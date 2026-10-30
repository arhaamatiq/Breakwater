"""Harbor task specs: parse a task directory the way Harbor does.

Everything that decides a verdict is read from the task's own files, with
Harbor's defaults (harbor 0.25):

  network       PUBLIC unless `network_mode` / legacy `allow_internet = false`
                says otherwise — for the agent environment AND, separately,
                for the verifier environment. (Breakwater used to force every
                solve offline; that was stricter than the official harness and
                made 11 Terminal-Bench tasks look unrunnable.)
  verifier mode `environment_mode`, else `separate` if a verifier environment
                is configured or tests/ carries a Dockerfile, else `shared`.
  artifacts     plus the implicit convention entry /logs/artifacts.
  verdict       /logs/verifier/reward.json if present, else reward.txt. The
                test script's exit code is ignored, exactly as in Harbor.

Digests make every task version content-addressed: the verifier digest covers
tests/ plus the task.toml fields that change what the verifier sees, so two
checkouts with byte-identical verifiers share images and trial ids, and any
verifier change gets a new identity. That identity is what a Trust Diff diffs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field

import yaml

CONVENTION_ARTIFACTS = "/logs/artifacts"

# solve-time package fetches: only relevant when the task's agent network
# policy is no_network (or the caller forces offline solves)
NET_FETCH_RE = re.compile(
    r"\b(?:apt-get|apt)\s+install\b|\bpip3?\s+install\b|\bpip3?\s+download\b|\bpipx\s+(?:install|run)\b|"
    r"\buv\s+(?:pip\s+install|add|tool\s+install|sync)\b|\bnpm\s+(?:install|ci)\b|"
    r"\bpnpm\s+(?:install|add)\b|\byarn\s+(?:install|add)\b|\bcargo\s+(?:install|add|fetch)\b|"
    r"\bgo\s+(?:get|install)\b|\b(?:conda|mamba|micromamba)\s+install\b|"
    r"\bcurl\b[^|]*\bhttps?://|\bwget\b|"
    # `uv run --with pkg` / `uvx --with pkg` pull from PyPI like an install; the
    # flag may sit on a continued line, so match across newlines
    r"\buvx?\b[\s\S]{0,400}?--with(?:-requirements)?[ =\t\n]|--with-requirements[ =\t]")


@dataclass
class EnvSpec:
    """One container environment (the agent's, or the separate verifier's)."""
    context: str                  # docker build context dir
    docker_image: str | None      # prebuilt image instead of a build
    network: str                  # public | no_network | allowlist
    cpus: float | None
    memory_mb: int | None
    gpus: int
    build_timeout_sec: float | None
    digest: str                   # content digest of the build context


@dataclass
class HarborTaskSpec:
    name: str                     # directory name, e.g. "vf2-speedup-networkx"
    path: str
    task_name: str                # [task].name, e.g. "terminal-bench/vf2-..."
    version: str | None
    mode: str                     # separate | shared
    artifacts: list               # str paths or {source, service, exclude}
    agent_env: EnvSpec
    verifier_env: EnvSpec | None  # None in shared mode
    agent_timeout_sec: float | None
    agent_user: str | None
    verifier_timeout_sec: float | None
    verifier_user: str | None
    verifier_env_vars: dict
    solution_env_vars: dict
    collect: list[str]            # [[verifier.collect]] commands
    test_script: str              # path inside the verifier, /tests/test.sh
    steps: int                    # multi-step tasks (>0) are not supported
    os: str
    digests: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HarborTaskSpec":
        d = dict(d)
        d["agent_env"] = EnvSpec(**d["agent_env"])
        if d.get("verifier_env"):
            d["verifier_env"] = EnvSpec(**d["verifier_env"])
        return cls(**d)

    @property
    def short_id(self) -> str:
        return self.name.replace("-", "_")


# ------------------------------------------------------------------ digests

def tree_digest(root: str, skip=lambda rel: False) -> str:
    """sha256 over (relative path, mode-x bit, content) of every file under
    root, in sorted order. Symlinks hash their target string."""
    h = hashlib.sha256()
    if not os.path.isdir(root):
        return "0" * 64
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            if not skip(rel):
                entries.append((rel, p))
    for rel, p in sorted(entries):
        h.update(rel.encode() + b"\0")
        if os.path.islink(p):
            h.update(b"L" + os.readlink(p).encode())
        else:
            h.update(b"X" if os.access(p, os.X_OK) else b"F")
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def _json_digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ parsing

def _network(section: dict | None, default: str = "public") -> str:
    """network_mode wins; legacy allow_internet maps true->public, false->no_network."""
    section = section or {}
    mode = section.get("network_mode")
    if mode:
        return str(mode).lower()
    ai = section.get("allow_internet")
    if ai is None:
        return default
    return "public" if ai else "no_network"


def _num(v, cast=float):
    return None if v is None else cast(v)


def _service_count(env_dir: str) -> int:
    for fn in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml"):
        p = os.path.join(env_dir, fn)
        if os.path.exists(p):
            try:
                return len((yaml.safe_load(open(p)) or {}).get("services") or {})
            except Exception:
                return 2  # unparseable compose -> treat as multi-service
    return 0


def parse_task(task_dir: str) -> HarborTaskSpec:
    task_dir = os.path.abspath(task_dir)
    with open(os.path.join(task_dir, "task.toml"), "rb") as f:
        cfg = tomllib.load(f)
    name = os.path.basename(task_dir.rstrip("/"))
    env_cfg = cfg.get("environment", {}) or {}
    ver_cfg = cfg.get("verifier", {}) or {}
    ver_env_cfg = ver_cfg.get("environment") or {}
    tests_dir = os.path.join(task_dir, "tests")
    env_dir = os.path.join(task_dir, "environment")

    has_tests_df = any(os.path.isfile(os.path.join(tests_dir, n))
                       for n in ("Dockerfile", "docker-compose.yaml"))
    mode = ver_cfg.get("environment_mode") or (
        "separate" if (ver_env_cfg or has_tests_df) else "shared")

    agent_env = EnvSpec(
        context=env_dir, docker_image=env_cfg.get("docker_image"),
        network=_network(env_cfg), cpus=_num(env_cfg.get("cpus")),
        memory_mb=_num(env_cfg.get("memory_mb"), int), gpus=int(env_cfg.get("gpus") or 0),
        build_timeout_sec=_num(env_cfg.get("build_timeout_sec")),
        digest=tree_digest(env_dir))

    verifier_env = None
    if mode == "separate":
        # Harbor: [verifier.environment] is used whole when present, else a
        # copy of [environment] (no per-field fallback). The verifier phase
        # may override network on top of that baseline.
        vcfg = ver_env_cfg if "environment" in ver_cfg else env_cfg
        net = _network(ver_cfg, default=_network(vcfg))
        verifier_env = EnvSpec(
            context=tests_dir if has_tests_df else env_dir,
            docker_image=vcfg.get("docker_image") if "environment" in ver_cfg else None,
            network=net, cpus=_num(vcfg.get("cpus")),
            memory_mb=_num(vcfg.get("memory_mb"), int),
            gpus=int(vcfg.get("gpus") or 0),
            build_timeout_sec=_num(vcfg.get("build_timeout_sec")),
            digest=tree_digest(tests_dir if has_tests_df else env_dir))

    artifacts = list(cfg.get("artifacts", []) or [])
    collect = [c["command"] for c in (ver_cfg.get("collect") or [])
               if isinstance(c, dict) and c.get("command")]
    spec = HarborTaskSpec(
        name=name, path=task_dir,
        task_name=(cfg.get("task") or {}).get("name", name),
        version=(cfg.get("task") or {}).get("version"),
        mode=mode, artifacts=artifacts, agent_env=agent_env, verifier_env=verifier_env,
        agent_timeout_sec=_num((cfg.get("agent") or {}).get("timeout_sec")),
        agent_user=(cfg.get("agent") or {}).get("user"),
        verifier_timeout_sec=_num(ver_cfg.get("timeout_sec")),
        verifier_user=ver_cfg.get("user"),
        verifier_env_vars={k: str(v) for k, v in (ver_cfg.get("env") or {}).items()},
        solution_env_vars={k: str(v) for k, v in
                           ((cfg.get("solution") or {}).get("env") or {}).items()},
        collect=collect, test_script="/tests/test.sh",
        steps=len(cfg.get("steps") or []), os=str(env_cfg.get("os", "linux")).lower())

    verifier_contract = {
        "artifacts": artifacts, "mode": mode, "verifier": {
            k: v for k, v in ver_cfg.items() if k != "collect"},
        "collect": collect,
    }
    spec.digests = {
        "tests": tree_digest(tests_dir),
        "solution": tree_digest(os.path.join(task_dir, "solution")),
        "environment": agent_env.digest,
        # identity of "the verifier": its code + everything in task.toml that
        # changes what it is shown or how it runs
        "verifier": _json_digest({"tests": tree_digest(tests_dir),
                                  "contract": verifier_contract}),
        # identity of the output contract: a bundle produced for one version
        # is replayable against another iff these match
        "artifact_contract": _json_digest(sorted(map(json.dumps, artifacts))),
        "task": tree_digest(task_dir),
    }
    return spec


# ------------------------------------------------------------------ support

def unsupported_reason(spec: HarborTaskSpec, *, offline_solve: bool = False) -> tuple[str, str] | None:
    """(category, detail) when this runtime cannot run the task faithfully."""
    if spec.steps:
        return "multi_step", f"{spec.steps} steps (single-step tasks only)"
    if spec.os != "linux":
        return "os", f"environment os={spec.os}"
    if spec.mode != "separate":
        return "unsupported_mode", f"environment_mode={spec.mode!r} (separate only)"
    if spec.agent_env.gpus or (spec.verifier_env and spec.verifier_env.gpus):
        return "gpu", "task requests a GPU"
    dict_artifacts = [a for a in spec.artifacts if not isinstance(a, str)]
    side = [a for a in dict_artifacts if (a.get("service") or "main") != "main"]
    n_services = _service_count(spec.agent_env.context)
    if side or n_services > 1:
        return "multi_service", (f"sidecar services (compose services={n_services}, "
                                 f"service-scoped artifacts={len(side)}) — single-container runtime")
    if "allowlist" in (spec.agent_env.network,
                       spec.verifier_env.network if spec.verifier_env else ""):
        return "network_allowlist", "allowlist egress policies are not emulated"
    if offline_solve or spec.agent_env.network == "no_network":
        blob = ""
        sol = os.path.join(spec.path, "solution")
        for root, _, files in os.walk(sol):
            for fn in files:
                try:
                    blob += "\n" + open(os.path.join(root, fn), errors="ignore").read()
                except OSError:
                    pass
        if NET_FETCH_RE.search(blob):
            return "solve_network", ("reference solution fetches packages but the solve "
                                     "runs without network")
    return None


def artifact_paths(spec: HarborTaskSpec) -> list[str]:
    """Declared main-service artifact paths, convention entry first (as Harbor
    normalizes them). Excludes are kept on the dict form by callers."""
    out = [CONVENTION_ARTIFACTS]
    for a in spec.artifacts:
        src = a if isinstance(a, str) else a.get("source")
        if src and src.rstrip("/") != CONVENTION_ARTIFACTS:
            out.append(src)
    return out


def artifact_excludes(spec: HarborTaskSpec) -> dict[str, list[str]]:
    return {a["source"]: list(a.get("exclude") or []) for a in spec.artifacts
            if isinstance(a, dict) and a.get("source") and a.get("exclude")}
