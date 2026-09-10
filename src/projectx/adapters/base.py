"""Adapter contract.

An adapter converts one external benchmark's format into Project X's internal
task representation. A converted task is a plain dict with the same fields the
built-in suite uses, plus:

  capabilities : which tester instruments apply —
                 "local_eval"    oracle is a Python solve(inp) the sandbox can
                                 run (enables reference outputs + mutation
                                 cross-examination)
                 "value_outputs" outputs are semantic values (enables output-
                                 form/equivalence perturbations)
                 Absent field = both (built-in behavior). State-based tasks
                 (terminal environments) declare neither.
  runtime      : name of an execution runtime for tasks the built-in
                 split-container Python flow cannot execute. Runtimes register
                 here and are resolved lazily by module name, so workers need
                 no adapter imports.
  probe_solutions : task-language degenerate probes (state tasks can't run the
                 Python probe portfolio). Reuses canonical probe names.
  noop         : task-language no-op solution source.
  source       : provenance {benchmark, task_id, path}.

Trials carry the full converted task as spec["task_blob"], so a trial is
self-contained: any worker (and any receipt replay) can execute it without
having loaded the adapter.
"""
import importlib

_RUNTIMES = {}


def register_runtime(name: str, runtime) -> None:
    _RUNTIMES[name] = runtime


def runtime_for(task: dict):
    name = task.get("runtime")
    if not name:
        return None
    if name not in _RUNTIMES:
        importlib.import_module(f"projectx.adapters.{name}")  # self-registers
    return _RUNTIMES[name]


def solution_from(task: dict, kind: str) -> str:
    """Solution source for a task dict (blob-safe; no registry lookups)."""
    if kind == "noop":
        if task.get("noop") is not None:
            return task["noop"]
        from ..benchmarks.suite import NOOP_SOLUTION
        return NOOP_SOLUTION
    return task[kind]


# ---- generic planted-flaw injectors (for validating the wiring on any
# ---- value-semantics benchmark; state adapters plant at the format level)

WEAK_VERIFIER = ("def check(inp, out, expected):\n"
                 "    return True, 'accepted'   # PLANTED: accepts anything\n")
FLAKY_WRAP = ("import random as _px_random\n"
              "{orig}\n"
              "_px_orig_check = check\n"
              "def check(inp, out, expected):\n"
              "    ok, d = _px_orig_check(inp, out, expected)\n"
              "    if ok and _px_random.random() < 0.4:\n"
              "        return False, 'PLANTED flaky rejection'\n"
              "    return ok, d\n")


def plant(task: dict, kind: str) -> dict:
    """Return a labeled broken copy of a value-semantics task."""
    t = dict(task)
    if kind == "weak":
        t["verifier"] = WEAK_VERIFIER
        t["planted_flaw"] = "weak_verifier(planted)"
    elif kind == "flaky":
        t["verifier"] = FLAKY_WRAP.format(orig=task["verifier"])
        t["planted_flaw"] = "nondeterministic_verifier(planted)"
    else:
        raise ValueError(f"unknown plant kind {kind}")
    t["source"] = {**(task.get("source") or {}), "planted": kind}
    return t
