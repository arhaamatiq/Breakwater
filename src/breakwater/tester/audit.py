"""The audit session: runs the full falsification pipeline for one task
through the real platform (queue -> workers -> Docker), assembles Findings.

Pipeline (adaptive; later stages consume earlier evidence):
  A baseline      reference x3 (+x7 and verify-only x8 if verdicts flip)
  B degenerates   probe portfolio, per-case accept analysis w/ coincidence check
  C attribution   verifier- vs execution-level nondeterminism (verify-only)
  D environment   verify-only under pinned env; attribute instability to a var
  E isolation     same (case, output) verified alone / reordered vs in-batch
  F equivalence   declared-equivalence perturbations (violations = FALSE_FAIL)
                  + generic form perturbations (INFO)
  G discrimination AST mutants: prescreen locally, run benchmark-equivalent
                  ones in-container, cross-examine survivors for divergence
                  inputs, shrink, validate the witness in-container

The audit is blind to `planted_flaw` labels — those are only used afterwards
by the CLI to score the tester itself."""
import asyncio
import json
import time
import uuid

from .. import db
from ..adapters.base import solution_from
from ..benchmarks.suite import TASKS
from . import probes
from .gen import Sandbox, generate_inputs, find_divergence, shrink_input, norm_equal
from .mutate import mutants
from .properties import Finding

import os as _os

# How long a stage batch waits for its trials before proceeding with whatever
# completed. 300s suits fast (python:slim) runtimes; heavy multi-container
# runtimes (e.g. Harbor's separate solve+verify with GB-sized images) need
# longer or stages silently run on partial data. Override with BW_BATCH_TIMEOUT.
BATCH_TIMEOUT = float(_os.environ.get("BW_BATCH_TIMEOUT", "300"))


class TaskAudit:
    def __init__(self, pool, task_id: str, log=print):
        self.pool = pool
        self.task_id = task_id
        self.task = TASKS[task_id]
        self.cases = self.task["cases"]
        self.atol = (self.task.get("equivalence") or {}).get("float_atol")
        self.invariants = self.task.get("invariants") or []
        self.caps = set(self.task.get("capabilities",
                                      ["local_eval", "value_outputs"]))
        self.oracle_src = solution_from(self.task, "oracle")
        self.log = log
        self.findings: dict[str, Finding] = {}
        self.sandbox = Sandbox()
        self.trials_used = 0
        self.reference_outputs = None    # local ground truth per case
        self.baseline_outputs = None     # container outputs of first clean run
        self.baseline_case_ok = None
        self.verifier_unstable = False
        self.notes = []

    # ------------------------------------------------------------ plumbing

    async def _batch(self, stage: str, specs: dict[str, dict]) -> dict[str, dict]:
        """Run labeled trial specs through the platform; return label -> row."""
        if not specs:
            return {}
        exp_id = f"audit-{self.task_id}-{stage}-{uuid.uuid4().hex[:6]}"
        if self.task.get("source"):
            specs = {lbl: {**spec, "task_blob": self.task}
                     for lbl, spec in specs.items()}
        trials = [{"trial_id": f"{exp_id}/{label}", "task_id": self.task_id,
                   "replicate": i, "spec": spec}
                  for i, (label, spec) in enumerate(specs.items())]
        await db.create_experiment(self.pool, exp_id, f"audit:{stage}",
                                   {"audit": self.task_id}, trials)
        deadline = time.time() + BATCH_TIMEOUT
        while time.time() < deadline:
            pending = await self.pool.fetchval(
                "SELECT count(*) FROM trials WHERE experiment_id=$1 "
                "AND state IN ('queued','running')", exp_id)
            if pending == 0:
                break
            await asyncio.sleep(0.4)
        rows = await self.pool.fetch(
            """SELECT t.trial_id, t.state, r.outcome, r.detail
               FROM trials t LEFT JOIN results r USING (trial_id)
               WHERE t.experiment_id=$1""", exp_id)
        self.trials_used += len(rows)
        out = {}
        for r in rows:
            label = r["trial_id"].split("/", 1)[1]
            out[label] = {"outcome": r["outcome"], "state": r["state"],
                          "detail": r["detail"] or {}, "trial_id": r["trial_id"]}
        return out

    def _find(self, klass: str, title: str, claim: str) -> Finding:
        if klass not in self.findings:
            self.findings[klass] = Finding(self.task_id, klass, title, claim)
        return self.findings[klass]

    @staticmethod
    def _case_oks(row) -> list[bool | None]:
        return [c.get("ok") for c in row["detail"].get("cases", [])]

    def _outputs_of(self, row) -> list:
        return row["detail"].get("outputs", [])

    # ------------------------------------------------------------- stage A

    async def stage_baseline(self):
        if "local_eval" in self.caps:
            ref = self.sandbox.eval(self.oracle_src, [c["input"] for c in self.cases])
            self.reference_outputs = [e.get("output") for e in ref]
        else:
            self.reference_outputs = None   # state-based task: no value ground truth

        rows = await self._batch("base", {f"oracle-{i}": {"solution": "oracle"}
                                          for i in range(3)})
        outcomes = [r["outcome"] for r in rows.values()]
        clean = [r for r in rows.values() if r["outcome"] in ("pass", "fail")]
        if clean:
            self.baseline_outputs = self._outputs_of(clean[0])
            self.baseline_case_ok = self._case_oks(clean[0])
        vectors = {tuple(self._case_oks(r)) for r in clean}
        self.base_flipped = len(set(outcomes)) > 1 or len(vectors) > 1

        if self.base_flipped or set(outcomes) == {"fail"}:
            # more evidence before claiming either instability or stable
            # rejection: 3 uniform samples of a flaky verdict prove neither
            more = await self._batch("base2", {f"oracle-x{i}": {"solution": "oracle"}
                                               for i in range(7)})
            outcomes += [r["outcome"] for r in more.values()]
            clean2 = [r for r in more.values() if r["outcome"] in ("pass", "fail")]
            vectors |= {tuple(self._case_oks(r)) for r in clean2}
            self.base_flipped = len(set(outcomes)) > 1 or len(vectors) > 1
        self.base_outcomes = outcomes

        if set(outcomes) == {"fail"}:
            f = self._find("REFERENCE_REJECTED", "Reference solution is rejected",
                           "The benchmark's own reference solution receives FAIL.")
            f.evidence.append({"outcomes": outcomes})
            f.replay.append({"label": "oracle run", "task_id": self.task_id,
                             "spec": {"solution": "oracle"}, "n": 3,
                             "expect": {"outcome": "fail"}})
        if set(outcomes) == {"error"}:
            f = self._find("VERIFIER_CRASH", "Verifier crashes on reference outputs",
                           "Verifying the reference outputs raises an exception.")
            f.evidence.append({"outcomes": outcomes})

    # ------------------------------------------------------------- stage B

    async def stage_degenerates(self):
        probe_map = self.task.get("probe_solutions") or probes.DEGENERATE_SOLUTIONS
        specs = {name: {"solution_source": src} for name, src in probe_map.items()}
        n_distinct_expected = len({json.dumps(c.get("expected"), sort_keys=True, default=str)
                                   for c in self.cases})
        if len(self.cases) > 1 and n_distinct_expected > 1 and self.reference_outputs:
            specs["memorize_first"] = {
                "solution_source": probes.memorize_first_probe(self.reference_outputs[0])}
        rows = await self._batch("degen", specs)

        violations = {}   # probe -> {cases, trial_pass, probe_outputs}
        for name, row in rows.items():
            if row["outcome"] not in ("pass", "fail"):
                if name == "raise_probe" and row["outcome"] == "error":
                    f = self._find("VERIFIER_CRASH", "Verifier crashes on a raising solution",
                                   "A solution that raises makes verification error "
                                   "instead of producing FAIL.")
                    f.evidence.append({"probe": name, "outcome": row["outcome"]})
                continue
            probe_out = self._outputs_of(row)
            if name == "leak_probe" and row["outcome"] == "pass" and self.reference_outputs:
                recovered = [probe_out[i].get("output") if i < len(probe_out) else None
                             for i in range(len(self.reference_outputs))]
                if all(norm_equal(v, r, self.atol)
                       for v, r in zip(recovered, self.reference_outputs)):
                    f = self._find("ANSWER_LEAKAGE",
                                   "Expected answers are readable by the solution",
                                   "A solution that only reads files reachable from "
                                   "its sandbox recovers the expected answers and "
                                   "passes every case.")
                    f.witnesses.append({"probe": name, "recovered_outputs": recovered})
                    f.replay.append({"label": "leak probe", "task_id": self.task_id,
                                     "spec": {"solution_source":
                                              probes.DEGENERATE_SOLUTIONS[name]},
                                     "n": 2, "expect": {"outcome": "pass"}})
                    continue
            if self.reference_outputs is None:
                if row["outcome"] == "pass":
                    violations[name] = {"cases": [], "trial_pass": True,
                                        "outputs": self._outputs_of(row)}
                continue
            oks = self._case_oks(row)
            bad_accepts = []
            for i, ok in enumerate(oks):
                if not ok or i >= len(self.reference_outputs or []):
                    continue
                pv = probe_out[i].get("output") if i < len(probe_out) else None
                if probe_out[i].get("error"):
                    bad_accepts.append(i)     # accepted a case whose solution raised
                elif not norm_equal(pv, self.reference_outputs[i], self.atol):
                    bad_accepts.append(i)     # accepted a wrong output
            if bad_accepts:
                violations[name] = {"cases": bad_accepts,
                                    "trial_pass": row["outcome"] == "pass",
                                    "outputs": probe_out}

        if not violations:
            return
        confirm = await self._batch("degen2", {
            f"{name}-c{j}": {"solution_source":
                             probe_map.get(name) or
                             probes.memorize_first_probe(self.reference_outputs[0])}
            for name in list(violations)[:3] for j in range(2)})
        for name, v in violations.items():
            reruns = [r for lbl, r in confirm.items() if lbl.startswith(f"{name}-c")]
            good = [r for r in reruns if r["outcome"] in ("pass", "fail")]
            if v["cases"]:
                stable = all(set(self._case_oks(r)[i] for i in v["cases"]) == {True}
                             for r in good) if good else True
            else:
                stable = all(r["outcome"] == "pass" for r in good) if good else True

            if v["trial_pass"]:
                f = self._find("ACCEPTS_TRIVIAL", "Task-ignorant solutions achieve PASS",
                               "Degenerate solutions that know nothing about the task "
                               "are accepted by the verifier.")
            else:
                f = self._find("UNSOUND_ACCEPT", "Wrong outputs accepted on some cases",
                               "Specific cases accept outputs that differ from the "
                               "reference outputs.")
            f.witnesses.append({"probe": name, "accepted_cases": v["cases"],
                                "probe_outputs": [v["outputs"][i].get("output")
                                                  for i in v["cases"] if i < len(v["outputs"])],
                                "reference_outputs": [self.reference_outputs[i]
                                                      for i in v["cases"]]
                                                     if self.reference_outputs else None,
                                "stable": stable})
            src = probe_map.get(name)
            if src:
                f.replay.append({"label": f"probe {name}", "task_id": self.task_id,
                                 "spec": {"solution_source": src}, "n": 2,
                                 "expect": {"outcome": "pass"} if v["trial_pass"]
                                 else {"case_ok": v["cases"][0], "equals": True}})
        self.violating_probe_rows = rows

    # ------------------------------------------------------------- stage C

    async def stage_attribution(self):
        if not self.baseline_outputs:
            self.verify_flip_rate = None
            return
        n = 8 if self.base_flipped else 4
        rows = await self._batch("attr", {
            f"vo-{i}": {"mode": "verify_only", "outputs": self.baseline_outputs}
            for i in range(n)})
        if not self.base_flipped and len(
                {tuple(self._case_oks(r)) for r in rows.values() if r["outcome"]}) > 1:
            rows |= await self._batch("attr2", {
                f"vo-x{i}": {"mode": "verify_only", "outputs": self.baseline_outputs}
                for i in range(4)})
        good = [r for r in rows.values() if r["outcome"]]
        vo = [r["outcome"] for r in good]
        # per-case verdict vectors: a verifier can flip individual cases while
        # the trial outcome stays uniform (e.g. one flaky case among many)
        vectors = {tuple(self._case_oks(r)) for r in good}
        self.verify_flip_rate = (sum(o != vo[0] for o in vo) / len(vo)) if vo else None
        full_rate = sum(o != self.base_outcomes[0] for o in self.base_outcomes) / len(self.base_outcomes)
        if len(vectors) > 1 or len(set(vo)) > 1:
            f = self._find("NONDETERMINISTIC_VERIFIER",
                           "Verifier verdict flips on identical inputs and outputs",
                           "Re-verifying the SAME outputs against the SAME cases "
                           "yields different verdicts.")
            f.stats.update({"full_run_outcomes": self.base_outcomes,
                            "verify_only_outcomes": vo,
                            "distinct_case_verdict_vectors": len(vectors)})
            expect = ({"mixed_outcomes": True} if len(set(vo)) > 1
                      else {"mixed_case_verdicts": True})
            f.replay.append({"label": "verify-only replays", "task_id": self.task_id,
                             "spec": {"mode": "verify_only", "outputs": self.baseline_outputs},
                             "n": 8, "expect": expect})
            self.verifier_unstable = True
        elif full_rate > 0:
            f = self._find("NONDETERMINISTIC_EXECUTION",
                           "Execution nondeterminism (verifier is stable)",
                           "End-to-end verdicts flip although re-verifying fixed "
                           "outputs is stable; the solution/execution side varies.")
            f.stats.update({"full_run_outcomes": self.base_outcomes,
                            "verify_only_outcomes": vo})

    # ------------------------------------------------------------- stage D

    async def stage_environment(self):
        if not self.baseline_outputs:
            return
        specs = {}
        for seed in probes.ENV_PROBES["PYTHONHASHSEED"][:5]:
            for j in range(2):
                specs[f"seed{seed}-{j}"] = {"mode": "verify_only",
                                            "outputs": self.baseline_outputs,
                                            "env": {"PYTHONHASHSEED": seed}}
        for tz in probes.ENV_PROBES["TZ"]:
            for j in range(2):
                specs[f"tz-{tz.replace('/', '_')}-{j}"] = {
                    "mode": "verify_only", "outputs": self.baseline_outputs,
                    "env": {"TZ": tz}}
        for lc in probes.ENV_PROBES["LC_ALL"]:
            for j in range(2):
                specs[f"lc-{lc}-{j}"] = {"mode": "verify_only",
                                         "outputs": self.baseline_outputs,
                                         "env": {"LC_ALL": lc}}
        rows = await self._batch("env", specs)

        def vec(row):
            return tuple(self._case_oks(row)) if row and row.get("outcome") else None

        by_seed = {}          # seed -> (vector, outcome)
        unstable_within = False
        for seed in probes.ENV_PROBES["PYTHONHASHSEED"][:5]:
            pair = [rows.get(f"seed{seed}-{j}") for j in range(2)]
            vecs = [vec(r) for r in pair if vec(r) is not None]
            if len(set(vecs)) > 1:
                unstable_within = True   # RNG, not env: same seed disagrees
            if vecs:
                by_seed[seed] = (vecs[0], next(r["outcome"] for r in pair if vec(r)))
        differs = len({v for v, _ in by_seed.values()}) > 1
        if differs and not unstable_within:
            a_seed, b_seed = next(
                (a, b) for a in by_seed for b in by_seed
                if by_seed[a][0] != by_seed[b][0])
            a_vec, b_vec = by_seed[a_seed][0], by_seed[b_seed][0]
            flip_case = next(i for i, (x, y) in enumerate(zip(a_vec, b_vec)) if x != y)
            f = self._find("ENV_SENSITIVE_VERDICT",
                           "Verdict depends on PYTHONHASHSEED",
                           "The same outputs verify differently under different "
                           "interpreter hash seeds.")
            f.witnesses.append({"variable": "PYTHONHASHSEED",
                                "outcome_by_seed": {s: o for s, (_, o) in by_seed.items()},
                                "flipping_case": flip_case,
                                "minimal_delta": {a_seed: a_vec[flip_case],
                                                  b_seed: b_vec[flip_case]}})
            for s, v in ((a_seed, a_vec), (b_seed, b_vec)):
                f.replay.append({"label": f"PYTHONHASHSEED={s}", "task_id": self.task_id,
                                 "spec": {"mode": "verify_only",
                                          "outputs": self.baseline_outputs,
                                          "env": {"PYTHONHASHSEED": s}},
                                 "n": 1,
                                 "expect": {"case_ok": flip_case,
                                            "equals": v[flip_case]}})
            self.env_explained = True
        else:
            self.env_explained = False
        for var, values in (("TZ", probes.ENV_PROBES["TZ"]),
                            ("LC_ALL", probes.ENV_PROBES["LC_ALL"])):
            by_val, stable = {}, True
            for val in values:
                key = val.replace("/", "_")
                vecs = [vec(rows.get(f"{var[:2].lower()}-{key}-{j}")) for j in range(2)]
                vecs = [v for v in vecs if v is not None]
                if len(set(vecs)) > 1:
                    stable = False   # flips under a FIXED value: RNG, not env
                if vecs:
                    by_val[val] = vecs[0]
            if stable and len(set(by_val.values())) > 1:
                f = self._find("ENV_SENSITIVE_VERDICT", f"Verdict depends on {var}",
                               f"The same outputs verify differently under different {var}.")
                f.witnesses.append({"variable": var,
                                    "case_verdicts_by_value":
                                        {k: list(v) for k, v in by_val.items()}})

        # attribution: env explains what looked like verifier nondeterminism
        if self.env_explained and "NONDETERMINISTIC_VERIFIER" in self.findings:
            nd = self.findings.pop("NONDETERMINISTIC_VERIFIER")
            env_f = self.findings["ENV_SENSITIVE_VERDICT"]
            env_f.stats["explains_observed_flakiness"] = nd.stats
            env_f.title += " (explains observed flakiness)"

    # ------------------------------------------------------------- stage E

    async def stage_isolation(self):
        if len(self.cases) < 2:
            return
        if self.base_flipped or self.verifier_unstable:
            self.notes.append("isolation stage skipped: verdicts unstable, "
                              "batch-position contradictions would be unattributable")
            return
        output_sets = {}
        if self.baseline_outputs:
            output_sets["oracle"] = (self.baseline_outputs, self.baseline_case_ok)
        degen = getattr(self, "violating_probe_rows", {})
        if "noop_none" in degen and degen["noop_none"]["outcome"] in ("pass", "fail"):
            output_sets["noop"] = (self._outputs_of(degen["noop_none"]),
                                   self._case_oks(degen["noop_none"]))
        specs, mapping = {}, {}
        idxs = list(range(min(len(self.cases), 6)))
        for src_name, (outs, _) in output_sets.items():
            for i in idxs:
                lbl = f"solo-{src_name}-{i}"
                specs[lbl] = {"mode": "verify_only", "cases": [self.cases[i]],
                              "outputs": [outs[i]]}
                mapping[lbl] = (src_name, i)
            specs[f"rev-{src_name}"] = {"mode": "verify_only",
                                        "cases": list(reversed(self.cases)),
                                        "outputs": list(reversed(outs))}
        rows = await self._batch("iso", specs)

        contradictions = []
        for lbl, (src_name, i) in mapping.items():
            row = rows.get(lbl)
            if not row or row["outcome"] not in ("pass", "fail"):
                continue
            solo_ok = self._case_oks(row)[0]
            batch_ok = output_sets[src_name][1][i]
            if solo_ok is not None and batch_ok is not None and solo_ok != batch_ok:
                contradictions.append({"outputs_from": src_name, "case": i,
                                       "verdict_in_batch": batch_ok,
                                       "verdict_alone": solo_ok})
        for src_name, (outs, oks) in output_sets.items():
            row = rows.get(f"rev-{src_name}")
            if row and row["outcome"] in ("pass", "fail"):
                rev = list(reversed(self._case_oks(row)))
                for i, (a, b) in enumerate(zip(oks, rev)):
                    if a is not None and b is not None and a != b:
                        contradictions.append({"outputs_from": src_name, "case": i,
                                               "verdict_in_batch": a,
                                               "verdict_reversed_order": b})
        if contradictions:
            f = self._find("STATEFUL_VERIFIER",
                           "Per-case verdict depends on which cases ran before it",
                           "The identical (input, output, expected) triple receives "
                           "different verdicts depending on batch position.")
            f.witnesses.extend(contradictions[:6])
            c = next((x for x in contradictions if "verdict_alone" in x),
                     contradictions[0])
            i = c["case"]
            outs = output_sets[c["outputs_from"]][0]
            if "verdict_alone" in c:
                f.replay.append({"label": f"case {i} alone", "task_id": self.task_id,
                                 "spec": {"mode": "verify_only",
                                          "cases": [self.cases[i]],
                                          "outputs": [outs[i]]},
                                 "n": 1,
                                 "expect": {"case_ok": 0, "equals": c["verdict_alone"]}})
            else:
                j = len(self.cases) - 1 - i
                f.replay.append({"label": f"reversed batch (case {i} at position {j})",
                                 "task_id": self.task_id,
                                 "spec": {"mode": "verify_only",
                                          "cases": list(reversed(self.cases)),
                                          "outputs": list(reversed(outs))},
                                 "n": 1,
                                 "expect": {"case_ok": j,
                                            "equals": c["verdict_reversed_order"]}})
            f.replay.append({"label": "full batch", "task_id": self.task_id,
                             "spec": {"mode": "verify_only", "cases": self.cases,
                                      "outputs": outs},
                             "n": 1, "expect": {"case_ok": i, "equals": c["verdict_in_batch"]}})

    # ------------------------------------------------------------- stage F

    async def stage_equivalence(self):
        if "value_outputs" not in self.caps:
            return
        if self.verifier_unstable or self.base_flipped:
            self.notes.append("equivalence stage skipped: verdicts unstable")
            return
        if not self.baseline_outputs or self.baseline_case_ok is None \
                or not all(self.baseline_case_ok):
            return
        declared = probes.declared_transforms(self.task)
        specs = {}
        for t in declared:
            specs[f"decl~{t}"] = {"mode": "verify_only",
                                  "outputs": [{**o, "transform": t}
                                              for o in self.baseline_outputs]}
        for t in probes.GENERIC_TRANSFORMS:
            specs[f"gen~{t}"] = {"mode": "verify_only",
                                 "outputs": [{**o, "transform": t}
                                             for o in self.baseline_outputs]}
        rows = await self._batch("equiv", specs)
        for lbl, row in rows.items():
            if row["outcome"] not in ("pass", "fail"):
                continue
            kind, t = lbl.split("~", 1)
            flipped = [i for i, ok in enumerate(self._case_oks(row)) if ok is False]
            if not flipped:
                continue
            if kind == "decl":
                f = self._find("FALSE_FAIL",
                               "Verifier is stricter than the task's declared semantics",
                               "Outputs inside the benchmark's own declared tolerance "
                               "are rejected — correct solutions can FAIL.")
                f.witnesses.append({"transform": t, "rejected_cases": flipped,
                                    "declared_equivalence": self.task.get("equivalence")})
                f.replay.append({"label": f"perturb {t}", "task_id": self.task_id,
                                 "spec": {"mode": "verify_only",
                                          "outputs": [{**o, "transform": t}
                                                      for o in self.baseline_outputs]},
                                 "n": 1, "expect": {"outcome": "fail"}})
            else:
                f = self._find("OUTPUT_FORM_SENSITIVITY",
                               "Verdict sensitive to output surface form",
                               "Formally different but arguably equivalent outputs "
                               "change the verdict (no equivalence is declared, so "
                               "this is informational).")
                f.witnesses.append({"transform": t, "rejected_cases": flipped})

    # ------------------------------------------------------------- stage G

    async def stage_discrimination(self):
        if "local_eval" not in self.caps:
            self.notes.append("discrimination stage skipped: no local-eval oracle "
                              "for this runtime")
            return
        if not self.reference_outputs or self.base_flipped:
            return    # discrimination unmeasurable under an unstable verdict
        all_mutants = mutants(self.oracle_src, limit=40)
        if not all_mutants:
            self.notes.append("no mutation sites in reference")
            return
        case_inputs = [c["input"] for c in self.cases]
        same, differ = [], []
        for m in all_mutants:
            res = self.sandbox.eval(m["src"], case_inputs)
            outs = [e.get("output") for e in res]
            errs = any("error" in e for e in res)
            if not errs and all(norm_equal(o, r, self.atol)
                                for o, r in zip(outs, self.reference_outputs)):
                same.append(m)
            elif not errs:
                differ.append((m, outs))
        same = same[:12]

        specs = {f"mut{i}": {"solution_source": m["src"]} for i, m in enumerate(same)}
        for j, (m, _) in enumerate(differ[:3]):
            specs[f"ctl{j}"] = {"solution_source": m["src"]}
        rows = await self._batch("mut", specs)

        # controls: behavior-differing mutants MUST fail; a pass = unsound accept
        for j, (m, outs) in enumerate(differ[:3]):
            row = rows.get(f"ctl{j}")
            if row and row["outcome"] == "pass":
                f = self._find("UNSOUND_ACCEPT", "Wrong outputs accepted on some cases",
                               "A mutant whose outputs differ from the reference on "
                               "benchmark cases is accepted.")
                f.witnesses.append({"mutant_op": m["op"], "mutant_outputs": outs,
                                    "reference_outputs": self.reference_outputs})
                f.replay.append({"label": f"control mutant {m['op']}",
                                 "task_id": self.task_id,
                                 "spec": {"solution_source": m["src"]}, "n": 1,
                                 "expect": {"outcome": "pass"}})

        survivors = [(same[i], rows[f"mut{i}"]) for i in range(len(same))
                     if rows.get(f"mut{i}", {}).get("outcome") == "pass"]
        if not survivors:
            self.notes.append(f"all {len(same)} benchmark-equivalent mutants "
                              f"handled; {len(differ)} behavior-differing mutants prescreened")
            return

        candidates = generate_inputs([c["input"] for c in self.cases], 400,
                                     self.invariants)
        witnesses, equivalents = [], 0
        for m, _row in survivors:
            div = find_divergence(self.sandbox, self.oracle_src, m["src"],
                                  candidates, self.atol)
            if div is None:
                equivalents += 1
                continue

            def still_fails(x, _m=m):
                return find_divergence(self.sandbox, self.oracle_src, _m["src"],
                                       [x], self.atol) is not None
            small = shrink_input(div["input"], still_fails, self.invariants)
            final = find_divergence(self.sandbox, self.oracle_src, m["src"],
                                    [small], self.atol) or div
            witnesses.append({"mutant_op": m["op"], "mutant_src": m["src"],
                              "divergence": final, "shrunk_from": div["input"]})

        # validate the top witnesses through the real pipeline
        val_specs = {}
        for k, w in enumerate(witnesses[:3]):
            d = w["divergence"]
            case = [{"input": d["input"], "expected": d["reference_output"]}]
            val_specs[f"wit{k}-mut"] = {"solution_source": w["mutant_src"], "cases": case}
            val_specs[f"wit{k}-ref"] = {"solution": "oracle", "cases": case}
        vrows = await self._batch("wit", val_specs)

        confirmed = []
        for k, w in enumerate(witnesses[:3]):
            mrow, rrow = vrows.get(f"wit{k}-mut"), vrows.get(f"wit{k}-ref")
            w["validated"] = bool(mrow and rrow and mrow["outcome"] == "fail"
                                  and rrow["outcome"] == "pass")
            if w["validated"]:
                confirmed.append(w)
        if confirmed or witnesses:
            f = self._find("SHALLOW_TEST_SET",
                           "Benchmark cannot distinguish broken variants of the reference",
                           "Single-edit mutants that change behavior on valid inputs "
                           "pass every benchmark case; the divergence inputs below "
                           "are the cases the benchmark is missing.")
            f.stats.update({"mutants_total": len(all_mutants),
                            "benchmark_equivalent": len(same),
                            "survivors": len(survivors),
                            "with_divergence_witness": len(witnesses),
                            "apparently_equivalent": equivalents})
            for w in (confirmed or witnesses)[:3]:
                d = w["divergence"]
                f.witnesses.append({
                    "mutant_op": w["mutant_op"], "missing_case_input": d["input"],
                    "reference_output": d.get("reference_output"),
                    "mutant_behavior": d.get("mutant_output", d.get("mutant_error")),
                    "shrunk_from": w["shrunk_from"],
                    "validated_in_container": w.get("validated", False),
                    "mutant_diff_src": w["mutant_src"]})
                f.replay.append({"label": f"mutant {w['mutant_op']} passes benchmark",
                                 "task_id": self.task_id,
                                 "spec": {"solution_source": w["mutant_src"]},
                                 "n": 1, "expect": {"outcome": "pass"}})
                f.replay.append({"label": "same mutant fails the missing case",
                                 "task_id": self.task_id,
                                 "spec": {"solution_source": w["mutant_src"],
                                          "cases": [{"input": d["input"],
                                                     "expected": d.get("reference_output")}]},
                                 "n": 1, "expect": {"outcome": "fail"}})
        elif equivalents:
            f = self._find("EQUIVALENT_MUTANTS", "Surviving mutants appear equivalent",
                           "Some mutants pass the benchmark and no divergence input "
                           "was found within budget.")
            f.stats["apparently_equivalent"] = equivalents

    # -------------------------------------------------------------- driver

    async def run(self) -> dict:
        t0 = time.time()
        try:
            await self.stage_baseline()
            await self.stage_degenerates()
            await self.stage_attribution()
            await self.stage_environment()
            await self.stage_isolation()
            await self.stage_equivalence()
            await self.stage_discrimination()
        finally:
            self.sandbox.close()
        if self.task.get("source"):
            for f in self.findings.values():
                for item in f.replay:
                    item["spec"].setdefault("task_blob", self.task)
        return {"task_id": self.task_id,
                "findings": [f.to_dict() for f in self.findings.values()],
                "notes": self.notes,
                "stats": {"trials": self.trials_used,
                          "wall_seconds": round(time.time() - t0, 1)}}
