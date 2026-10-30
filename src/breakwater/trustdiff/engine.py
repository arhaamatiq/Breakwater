"""Verifier Trust Diff: judge a fixed output corpus with two verifier versions.

For every task whose verifier changed between two versions:

  1. Corpus. Solve the reference (and a no-op) once in the BASE version's
     environment, snapshot the pre-solve state, and derive deterministic
     mutants (corpus.py). Every output is a content-addressed bundle.
  2. Matrix. Verify every bundle with both versions, R times each, through
     the distributed queue. Trial ids are content-addressed —
     td/<verifier digest>/<bundle digest>/r<k> — so a cell is computed once
     ever (exactly-once result accounting dedupes reruns, and a later diff
     against a third version reuses every base cell). Base and head trials
     are interleaved per bundle so host drift hits both arms equally.
  3. Adapt. Any bundle whose verdicts differ (across versions or within one)
     is topped up to `max_repeats` per version before classification, so a
     "deterministic" claim always rests on >= max_repeats agreeing runs per
     side (all-vs-none at 5 per side: Fisher p = 0.008).
  4. Classify each bundle's transition against what the bundle is expected to
     score (reference -> pass, no-op/degenerate -> fail, mutant -> unknown),
     attribute it to the checks whose pass rates moved, and compare any
     numeric margins (bootstrap CI of the median ratio).

The verdict is always the task's reward, as Harbor records it.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field

from .. import cas, db
from ..adapters import harbor
from ..bundle import Bundle
from ..adapters.harbor_task import artifact_paths
from . import stats
from .corpus import CorpusItem, mutants, patched
from .versions import Source, TaskVersion

TERMINAL = ("completed", "failed")


def _hex(d: str, n: int = 16) -> str:
    return d.split(":", 1)[-1][:n]


@dataclass
class DiffConfig:
    repeats: int = 3
    max_repeats: int = 5
    corpus: tuple[str, ...] = ("reference", "noop", "degenerate", "path", "file", "ast")
    max_files: int = 8
    max_ast_per_file: int = 6
    max_ast_files: int = 3
    metrics: dict = field(default_factory=dict)       # name -> regex over test-stdout
    network: str | None = None                        # None=task policy | on | off
    solve_from: str = "base"
    solve_timeout_cap: float | None = None
    trial_timeout: float = 6 * 3600
    priority: int = 0
    prune_images: bool = False     # remove a task's images once its diff is done
    fixtures: list = field(default_factory=list)   # [(task, name, expect, patch_text)]
    # [(task, name, expect, patch_text)] applied to solution/ before solving:
    # a constructed candidate solution, solved once, judged by both versions
    solution_fixtures: list = field(default_factory=list)
    # [(task, dir)] recorded Harbor trials (harbor run / harbor trial download);
    # each becomes a bundle expected to keep the reward Harbor recorded
    imports: list = field(default_factory=list)


class Matrix:
    """Collects verify results per (bundle digest, version label)."""

    def __init__(self):
        self.cells: dict[tuple[str, str], list[dict]] = {}

    def add(self, bundle: str, version: str, row: dict):
        self.cells.setdefault((bundle, version), []).append(row)

    def get(self, bundle: str, version: str) -> list[dict]:
        return self.cells.get((bundle, version), [])


async def wait_for(pool, trial_ids: list[str], timeout: float, label: str = "",
                   log=print) -> dict[str, dict]:
    """Block until every trial is terminal; return trial_id -> row."""
    deadline = time.time() + timeout
    last = None
    while True:
        rows = await pool.fetch(
            """SELECT t.trial_id, t.state, r.outcome, r.detail, r.exec_ms
               FROM trials t LEFT JOIN results r USING (trial_id)
               WHERE t.trial_id = ANY($1::text[])""", trial_ids)
        done = sum(r["state"] in TERMINAL for r in rows)
        if done != last:
            log(f"[diff] {label}: {done}/{len(trial_ids)} trials done")
            last = done
        if done >= len(trial_ids) or time.time() > deadline:
            return {r["trial_id"]: dict(r) for r in rows}
        await asyncio.sleep(1.0)


async def submit(pool, experiment_id: str, name: str, trials: list[dict], priority: int = 0):
    """Enqueue trials; existing trial ids are reused, not re-run."""
    try:
        await db.create_experiment(pool, experiment_id, name, {"trustdiff": True}, trials,
                                   priority=priority)
    except TypeError:          # queue without priority support
        await db.create_experiment(pool, experiment_id, name, {"trustdiff": True}, trials)


class TaskDiff:
    """One task: build corpus, run matrix, classify."""

    def __init__(self, pool, run_id: str, base: TaskVersion, head: TaskVersion,
                 cfg: DiffConfig, log=print):
        self.pool, self.run_id, self.cfg, self.log = pool, run_id, cfg, log
        self.base, self.head = base, head
        self.versions = {"base": base, "head": head}
        self.specs = {k: v.spec for k, v in self.versions.items()}
        self.tasks = {k: harbor.task_dict(s) for k, s in self.specs.items()}
        self.corpus: list[CorpusItem] = []
        self.matrix = Matrix()
        self.notes: list[str] = []

    # ------------------------------------------------------------ corpus
    async def build_corpus(self) -> list[CorpusItem]:
        src = self.cfg.solve_from
        spec, task = self.specs[src], self.tasks[src]
        tag = (f"{harbor.RUNTIME_VERSION}/{spec.name}/{spec.digests['environment'][:12]}-"
               f"{spec.digests['solution'][:12]}")
        wanted = {"reference": "oracle", "noop": "noop"}
        trials = []
        for kind, sol in wanted.items():
            if kind in self.cfg.corpus:
                trials.append({"trial_id": f"solve/{tag}/{sol}", "task_id": f"hb_{spec.short_id}",
                               "replicate": 0, "spec": {"mode": "solve", "solution": sol,
                                                        "network": self.cfg.network,
                                                        "solve_timeout_cap": self.cfg.solve_timeout_cap,
                                                        "task_blob": task}})
        if "path" in self.cfg.corpus:
            trials.append({"trial_id": f"snapshot/{harbor.RUNTIME_VERSION}/{spec.name}/"
                                       f"{spec.digests['environment'][:12]}",
                           "task_id": f"hb_{spec.short_id}", "replicate": 0,
                           "spec": {"mode": "snapshot", "task_blob": task}})
        sol_fx = {}
        for task_name, fname, expect, text in self.cfg.solution_fixtures:
            if task_name != spec.name:
                continue
            blob = patch_solution(task, text)
            tree_d = hashlib.sha256(json.dumps(blob["hb"]["solution_tree"], sort_keys=True)
                                    .encode()).hexdigest()[:12]
            tid = (f"solve/{harbor.RUNTIME_VERSION}/{spec.name}/"
                   f"{spec.digests['environment'][:12]}-{tree_d}/oracle")
            sol_fx[tid] = (fname, expect, text)
            trials.append({"trial_id": tid, "task_id": f"hb_{spec.short_id}", "replicate": 0,
                           "spec": {"mode": "solve", "solution": "oracle",
                                    "network": self.cfg.network,
                                    "solve_timeout_cap": self.cfg.solve_timeout_cap,
                                    "task_blob": blob}})
        await submit(self.pool, f"{self.run_id}/{spec.name}/corpus", f"corpus:{spec.name}",
                     trials, self.cfg.priority)
        rows = await wait_for(self.pool, [t["trial_id"] for t in trials], self.cfg.trial_timeout,
                              f"{spec.name} corpus", self.log)

        def bundle_of(tid):
            r = rows.get(tid) or {}
            d = r.get("detail") or {}
            if r.get("state") != "completed" or not d.get("bundle"):
                return None, d
            return Bundle.from_tar(cas.get(d["bundle"]), artifact_paths(spec)), d

        ref, ref_d = bundle_of(f"solve/{tag}/oracle")
        if ref is None:
            self.notes.append("reference solve produced no bundle; corpus is empty")
            return []
        if ref_d.get("solution_exit_code") not in (0, None):
            self.notes.append(f"reference solve.sh exited {ref_d.get('solution_exit_code')}")
        self.corpus.append(CorpusItem("reference", "reference", ref,
                                      {"solve_exit": ref_d.get("solution_exit_code")}))
        if "noop" in self.cfg.corpus:
            nb, _ = bundle_of(f"solve/{tag}/noop")
            if nb is not None:
                self.corpus.append(CorpusItem("noop", "noop", nb, {}))
        snap = None
        if "path" in self.cfg.corpus:
            snap, _ = bundle_of(f"snapshot/{harbor.RUNTIME_VERSION}/{spec.name}/"
                                f"{spec.digests['environment'][:12]}")
        include = tuple(k for k in ("degenerate", "path", "file", "ast") if k in self.cfg.corpus)
        self.corpus += mutants(ref, snap, max_files=self.cfg.max_files,
                               max_ast_per_file=self.cfg.max_ast_per_file,
                               max_ast_files=self.cfg.max_ast_files, include=include)
        for task_name, fname, expect, text in self.cfg.fixtures:
            if task_name == spec.name:
                self.corpus.append(patched(ref, fname, expect, text))
        for tid, (fname, expect, text) in sol_fx.items():
            fb, fd = bundle_of(tid)
            if fb is None:
                self.notes.append(f"solution fixture {fname}: solve produced no bundle")
                continue
            self.corpus.append(CorpusItem(f"solution_fixture::{fname}", "fixture", fb,
                                          {"expect": expect, "diff": text[:4000],
                                           "solve_exit": fd.get("solution_exit_code")}))
        for task_name, root in self.cfg.imports:
            if task_name == spec.name:
                self.corpus += imported_trials(root, spec)
        # one entry per distinct bundle (a no-op can equal drop_all, etc.)
        seen, uniq = set(), []
        for it in self.corpus:
            if it.bundle.digest not in seen:
                seen.add(it.bundle.digest)
                uniq.append(it)
        self.corpus = uniq
        for it in self.corpus:
            cas.put(it.bundle.to_tar())
        return self.corpus

    # ------------------------------------------------------------ matrix
    def _trial(self, item: CorpusItem, vlabel: str, k: int) -> dict:
        spec = self.specs[vlabel]
        return {"trial_id": f"td/{harbor.RUNTIME_VERSION}/{spec.digests['verifier'][:16]}/"
                            f"{_hex(item.bundle.digest)}/r{k}",
                "task_id": f"hb_{spec.short_id}", "replicate": k,
                "spec": {"mode": "verify_only", "outputs": [{"bundle": item.bundle.digest}],
                         "metrics": self.cfg.metrics, "network": self.cfg.network,
                         "task_blob": self.tasks[vlabel]}}

    async def run_matrix(self, ks: dict[str, range], items: list[CorpusItem], stage: str):
        same = self.specs["base"].digests["verifier"] == self.specs["head"].digests["verifier"]
        trials, arm = [], {}
        for it in items:
            for k in ks[it.bundle.digest]:
                pair = [("base", self._trial(it, "base", k))]
                if not same:     # identical verifiers share trial ids: one arm only
                    pair.append(("head", self._trial(it, "head", k)))
                if k % 2:
                    pair.reverse()   # alternate which arm runs first
                for v, t in pair:
                    arm[t["trial_id"]] = v
                    trials.append(t)
        if not trials:
            return
        name = self.specs["head"].name
        await submit(self.pool, f"{self.run_id}/{name}/{stage}", f"trustdiff:{name}:{stage}",
                     trials, self.cfg.priority)
        rows = await wait_for(self.pool, [t["trial_id"] for t in trials], self.cfg.trial_timeout,
                              f"{name} {stage}", self.log)
        for t in trials:
            self.matrix.add(t["spec"]["outputs"][0]["bundle"], arm[t["trial_id"]],
                            rows.get(t["trial_id"], {}))

    async def run(self) -> dict:
        try:
            return await self._run()
        finally:
            if self.cfg.prune_images:
                await asyncio.to_thread(self._prune)

    def _prune(self):
        """Disk is the binding constraint on a laptop sweep: drop this task's
        content-tagged images (they are rebuilt from the same digest on demand)."""
        from ..runner import docker_client
        client = docker_client()
        for s in self.specs.values():
            for env in (s.agent_env, s.verifier_env):
                if env and not env.docker_image:
                    kind = "env" if env is s.agent_env else "verifier"
                    with contextlib.suppress(Exception):
                        client.images.remove(harbor._image_tag(kind, s.name, env.digest))
        with contextlib.suppress(Exception):
            client.images.prune(filters={"dangling": True})

    async def _run(self) -> dict:
        b, h = self.specs["base"], self.specs["head"]
        if b.digests["artifact_contract"] != h.digests["artifact_contract"]:
            self.notes.append("artifact contract changed between versions: outputs recorded "
                              "under base are not a faithful input to head (Harbor would refuse "
                              "to regrade); reported but not classified")
        await self.build_corpus()
        if not self.corpus:
            return self.result()
        R, M = self.cfg.repeats, self.cfg.max_repeats
        await self.run_matrix({it.bundle.digest: range(R) for it in self.corpus},
                              self.corpus, "pass1")
        contested = [it for it in self.corpus if self._contested(it.bundle.digest)]
        if contested and M > R:
            self.log(f"[diff] {h.name}: {len(contested)} contested bundle(s); topping up to {M}")
            await self.run_matrix({it.bundle.digest: range(R, M) for it in contested},
                                  contested, "topup")
        return self.result()

    def _contested(self, digest: str) -> bool:
        outs = {v: [r.get("outcome") for r in self.matrix.get(digest, v)] for v in ("base", "head")}
        allv = outs["base"] + outs["head"]
        return len(set(allv)) > 1

    # ------------------------------------------------------------ classify
    def result(self) -> dict:
        items = [classify(it, self.matrix.get(it.bundle.digest, "base"),
                          self.matrix.get(it.bundle.digest, "head")
                          if self.specs["base"].digests["verifier"] != self.specs["head"].digests["verifier"]
                          else self.matrix.get(it.bundle.digest, "base"))
                 for it in self.corpus]
        return {
            "task": self.specs["head"].name,
            "base": _vinfo(self.base, self.specs["base"]),
            "head": _vinfo(self.head, self.specs["head"]),
            "verifier_changed": self.specs["base"].digests["verifier"] != self.specs["head"].digests["verifier"],
            "artifact_contract_changed": self.specs["base"].digests["artifact_contract"] != self.specs["head"].digests["artifact_contract"],
            "check_inventory": _inventory(self.tasks),
            "notes": self.notes,
            "bundles": items,
        }


def imported_trials(root: str, spec) -> list[CorpusItem]:
    """Recorded Harbor trial dirs under `root` (or `root` itself) as corpus items.
    The expected verdict is the one Harbor recorded, so a version change that
    flips it is reported as a regression or a fix like any other."""
    from ..bundle import from_harbor_trial
    dirs = [root] if os.path.isfile(os.path.join(root, "result.json")) else sorted(
        os.path.join(root, d) for d in os.listdir(root)
        if os.path.isfile(os.path.join(root, d, "result.json")))
    out = []
    for d in dirs:
        res = json.load(open(os.path.join(d, "result.json")))
        pr = harbor.primary_reward((res.get("verifier_result") or {}).get("rewards") or {})
        b = from_harbor_trial(d, artifact_paths(spec))
        if not b.entries:
            continue
        out.append(CorpusItem(f"imported::{os.path.basename(d.rstrip('/'))}", "imported", b,
                              {"expect": None if pr is None else ("pass" if pr >= 1 else "fail"),
                               "recorded_reward": pr, "source": d}))
    return out


def patch_solution(task: dict, patch_text: str) -> dict:
    """Task blob whose solution/ tree has a unified diff applied (paths
    relative to the task dir: `--- a/solution/x.py`)."""
    import subprocess
    import tempfile
    blob = copy.deepcopy(task)
    tree = blob["hb"]["solution_tree"]
    with tempfile.TemporaryDirectory() as tmp:
        for rel, (b64, mode) in tree.items():
            dst = os.path.join(tmp, "solution", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(base64.b64decode(b64))
        r = subprocess.run(["patch", "-p1", "-s", "-d", tmp, "--no-backup-if-mismatch"],
                           input=patch_text.encode(), capture_output=True)
        if r.returncode != 0:
            raise ValueError(f"solution patch failed: {(r.stdout + r.stderr).decode()[:400]}")
        new = {}
        for root, _, fns in os.walk(os.path.join(tmp, "solution")):
            for fn in fns:
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, os.path.join(tmp, "solution"))
                with open(p, "rb") as f:
                    new[rel] = [base64.b64encode(f.read()).decode("ascii"),
                                tree.get(rel, [None, 0o644])[1]]
    blob["hb"]["solution_tree"] = new
    return blob


def _vinfo(v: TaskVersion, spec) -> dict:
    return {"label": v.label, "commit": v.commit, "repo": v.repo_url, "path": v.path,
            "task_version": spec.version, "verifier_digest": spec.digests["verifier"]}


def _inventory(tasks: dict) -> dict:
    # compare bare check names: a pytest test that became a RewardKit criterion
    # of the same name (`test_x.py::test_a` -> `test_a`) is not a removal
    names = {k: [c["input"]["test"].split("::")[-1] for c in t["cases"]]
             for k, t in tasks.items()}
    b, h = set(names["base"]), set(names["head"])
    return {"base": len(b), "head": len(h), "removed": sorted(b - h), "added": sorted(h - b)}


# ---------------------------------------------------------------- verdicts

def _arm(rows: list[dict]) -> dict:
    outs = [r.get("outcome") or ("infra_failed" if r.get("state") == "failed" else "missing")
            for r in rows]
    n = len(outs)
    k = outs.count("pass")
    nf = outs.count("fail")
    ne = n - k - nf
    state = ("NONE" if n == 0 else "PASS" if k == n else "FAIL" if nf == n
             else "ERROR" if ne == n else "MIXED")
    checks: dict[str, list] = {}
    metrics: dict[str, list] = {}
    walls, errors = [], []
    for r in rows:
        d = r.get("detail") or {}
        for c in d.get("cases", []):
            if c.get("ok") is not None:
                checks.setdefault(c["name"], []).append(bool(c["ok"]))
        for mk, mv in (d.get("metrics") or {}).items():
            metrics.setdefault(mk, []).append(mv)
        if d.get("verifier_wall_s") is not None:
            walls.append(d["verifier_wall_s"])
        if d.get("error_kind"):
            errors.append(d["error_kind"])
    return {"n": n, "pass": k, "fail": nf, "error": ne, "state": state, "outcomes": outs,
            "pass_rate_ci": stats.wilson(k, n), "checks": checks, "metrics": metrics,
            "wall_s": walls, "error_kinds": sorted(set(errors)),
            "rewards": [(r.get("detail") or {}).get("primary_reward") for r in rows]}


# (expected, base state, head state) -> (class, severity)
def _transition(expect: str | None, bs: str, hs: str) -> tuple[str, str]:
    if "NONE" in (bs, hs):
        return "incomplete", "INFO"
    if bs == hs and bs in ("PASS", "FAIL", "ERROR"):
        if expect is None and bs == "PASS":
            return "survives_both", "LOW"
        if expect == "fail" and bs == "PASS":
            return "false_pass_both", "HIGH"
        if expect == "pass" and bs == "FAIL":
            return "false_fail_both", "HIGH"
        return "unchanged", "NONE"
    if "MIXED" in (bs, hs):
        if bs == "MIXED" and hs == "MIXED":
            return "unstable_both", "MEDIUM"
        return ("instability_introduced", "MEDIUM") if hs == "MIXED" else ("instability_removed", "INFO")
    if hs == "ERROR":
        return "verifier_error_introduced", "HIGH"
    if bs == "ERROR":
        return "verifier_error_fixed", "INFO"
    # stable -> different stable verdict
    if bs == "FAIL" and hs == "PASS":
        return {"pass": ("false_fail_fixed", "INFO"),
                "fail": ("false_pass_introduced", "CRITICAL")}.get(
            expect, ("strictness_regression", "HIGH"))
    return {"pass": ("false_fail_introduced", "HIGH"),
            "fail": ("false_pass_fixed", "INFO")}.get(expect, ("hardening", "INFO"))


def classify(item: CorpusItem, base_rows: list[dict], head_rows: list[dict]) -> dict:
    b, h = _arm(base_rows), _arm(head_rows)
    klass, sev = _transition(item.expect, b["state"], h["state"])
    p = stats.fisher_exact(b["pass"], b["n"] - b["pass"], h["pass"], h["n"] - h["pass"])
    moved = []
    for name in sorted(set(b["checks"]) | set(h["checks"])):
        bv, hv = b["checks"].get(name), h["checks"].get(name)
        br = sum(bv) / len(bv) if bv else None
        hr = sum(hv) / len(hv) if hv else None
        if br != hr:
            moved.append({"check": name, "base_pass_rate": br, "head_pass_rate": hr})
    shifts = {}
    for m in sorted(set(b["metrics"]) & set(h["metrics"])):
        ci = stats.bootstrap_ratio_ci(b["metrics"][m], h["metrics"][m])
        if ci:
            shifts[m] = {"ratio": ci[0], "ci95": [ci[1], ci[2]],
                         "systematic": not (ci[1] <= 1.0 <= ci[2]),
                         "mann_whitney_p": stats.mann_whitney_p(b["metrics"][m], h["metrics"][m])}
    wall = stats.bootstrap_ratio_ci(b["wall_s"], h["wall_s"])
    deterministic = (b["state"] in ("PASS", "FAIL") and h["state"] in ("PASS", "FAIL")
                     and b["n"] >= 5 and h["n"] >= 5)
    return {
        "label": item.label, "kind": item.kind, "expect": item.expect,
        "bundle": item.bundle.digest, "meta": {k: v for k, v in item.meta.items()},
        "class": klass, "severity": sev, "fisher_p": p,
        "deterministic": deterministic if b["state"] != h["state"] else None,
        "base": {k: v for k, v in b.items() if k not in ("checks", "metrics")},
        "head": {k: v for k, v in h.items() if k not in ("checks", "metrics")},
        "checks_moved": moved, "metric_shifts": shifts,
        "wall_ratio": {"ratio": wall[0], "ci95": [wall[1], wall[2]]} if wall else None,
    }


# ---------------------------------------------------------------- top level

async def run_diff(pool, base: Source, head: Source, tasks: list[str], cfg: DiffConfig,
                   parallel: int = 2, log=print) -> dict:
    run_id = f"td-{time.strftime('%Y%m%d-%H%M%S')}-{random.randrange(16**4):04x}"
    sem = asyncio.Semaphore(parallel)
    results = {}

    async def one(t):
        async with sem:
            bv, hv = base.version(t), head.version(t)
            if bv is None or hv is None:
                results[t] = {"task": t, "notes": [f"missing in {'base' if bv is None else 'head'}"],
                              "bundles": []}
                return
            log(f"[diff] {t}: base {bv.describe()} vs head {hv.describe()}")
            try:
                results[t] = await TaskDiff(pool, run_id, bv, hv, cfg, log).run()
            except Exception as e:            # one task's infra failure must not sink the run
                log(f"[diff] {t}: FAILED {type(e).__name__}: {e}")
                results[t] = {"task": t, "notes": [f"diff failed: {type(e).__name__}: {e}"],
                              "bundles": []}

    await asyncio.gather(*(one(t) for t in tasks))
    return {"run_id": run_id, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "base": {"label": base.label, "ref": base.ref, "commit": base.commit,
                     "repo": base.repo_url, "tasks_dir": base.tasks_dir},
            "head": {"label": head.label, "ref": head.ref, "commit": head.commit,
                     "repo": head.repo_url, "tasks_dir": head.tasks_dir},
            "config": {k: v for k, v in cfg.__dict__.items()},
            "tasks": [results[t] for t in tasks]}


def dump(result: dict, path: str):
    with open(path, "w") as f:
        json.dump(result, f, indent=2, default=str)
