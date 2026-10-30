# Harbor / Terminal-Bench v4.0.0 — Breakwater Audit: Final Report

**Date:** 2026-09-10
**Dataset:** `harbor-framework/terminal-bench` @ tag `v4.0.0` (commit `452bf30`, 66 tasks)
**Adapter:** `src/projectx/adapters/harbor.py` (new this session)
**Tester core:** unchanged except one config knob (`PX_BATCH_TIMEOUT`, default 300s — no verdict logic touched)

---

## TL;DR

**Nothing strong survived. No replay-confirmed, undocumented benchmark defect on any supported v4.0.0 task.**

Ten supported tasks were audited to completion (31–34 trials each, six falsification stages).
Every one: oracle passes cleanly, every degenerate / perturbed / reordered probe is correctly
rejected, zero findings of any class.

Two HIGH `REFERENCE_REJECTED` "findings" were raised during the session and **both were traced
to adapter bugs, not benchmark defects** — one fixed mid-session (artifact-path doubling), one
fixed as part of this triage (`solve.sh` uses `uv run --with …`, which pulls from PyPI at solve
time; Breakwater's solve sandbox has no network, so the reference can't run). Neither is
maintainer-actionable and neither is in the final finding count.

The honest caveat: the tester's mutation/discrimination stage — the one that produced the
`SHALLOW_TEST_SET` findings on HumanEval — **cannot run** against Harbor's containerized
runtime (no in-process oracle). So this audit answers "are the verifiers sound, deterministic,
environment-independent, stateless, and robust to output form?" (yes, on everything tested) but
**not** "are the test sets deep enough to catch subtle wrong answers?"

---

## 1. Task counts — all 66 v4.0.0 tasks

Classified by `reports/harbor_audit_support/hb_classify.py` (uses the adapter's own predicates):

| Category | N | Why not audited |
|---|---:|---|
| **Supported** | **32** | adapter runs these unchanged |
| `multi_service` | 13 | service-scoped artifacts or multi-service `docker-compose` — adapter is single-container |
| `solve_network` | 11 | `solution/` installs/fetches packages at solve time (`pip install`, `uv pip install`, `uv run --with`, `apt install`, `conda/mamba`); Breakwater's solve sandbox has no network |
| `no_tests` | 5 | no pytest `test_*` functions (FreeCAD GUI tasks, `music-harmony`, `react-lead-form`) |
| `gpu` | 3 | `[environment] gpus > 0` |
| `browser` | 2 | Playwright/Selenium baked into `tests/Dockerfile` |

**Supported (32):** atrx-vep-crispr, batched-eval-parity, biped-contact-dynamics,
bun-sourcemap-leak, cargo-flight-dispatch, coq-block-bound, data-anonymization,
distributed-dedup, embedding-drift-monitor, formal-crypto, glycan-ms2-elucidation,
hof-topology-interpenetration, interleaved-vigenere, ks-solver-cpp, layout-config-recreation2,
mp-checkpoint-consolidation, mvcc-lsm-compaction, ontology-kg-querying,
photonic-waveguide-routing, pretrain-shard-corruption, production-planning, retro-console-soc,
risk-scorer-replay, session-window-debug, sglang-qwen-burst, shadow-relay, sound-change-cascade,
takens-embedding-lean, uefi-bootkit, vf2-speedup-networkx, wal-recovery-ordering, wdm-design.

---

## 2. Audited this session — 10 supported tasks, all clean

| Task | Trials | Oracle (base stage) | Degenerate (noop) | Env perturbations¹ | Findings |
|---|---:|---|---|---|:--:|
| interleaved-vigenere | 34 | reward=1, CTRF 6/6 | reward=0, 2/6 | 18/18 reward=1 | none |
| session-window-debug | 34 | reward=1, CTRF 7/7 | reward=0, 2/7 | 18/18 reward=1 | none |
| production-planning | 34 | reward=1, CTRF 20/20 | reward=0, 1/20 | 18/18 reward=1 | none |
| risk-scorer-replay | 33 | reward=1, CTRF 5/5 | reward=0, 2/5 | 18/18 reward=1 | none |
| wal-recovery-ordering | 34 | reward=1, CTRF 97/97² | reward=0, 44/97 | 2/18 done³ | none |
| shadow-relay | 34 | reward=1, CTRF 8/8 | reward=0, 1/8 | 18/18 reward=1 | none |
| glycan-ms2-elucidation | 34 | reward=1, CTRF 12/12 | reward=0, 2/12 | 18/18 reward=1 | none |
| sound-change-cascade | 34 | reward=1, CTRF 7/7 | reward=0, 0/7 | 18/18 reward=1 | none |
| batched-eval-parity | 33 | reward=1, CTRF 5/5 | reward=0, 0/5 | 3/18 done³ | none |
| roy-polymorph-cn⁴ | 31 | reward=1, CTRF 3/3 | reward=0, 0/3 | 18/18 reward=1 | none |

¹ Environment stage = 18 perturbations/task: `LC_ALL` ∈ {C, en_US.UTF-8}, `TZ` ∈ {UTC,
Pacific/Kiritimati}, `PYTHONHASHSEED` seeds 0–4, ×2 replicates. Every one produced reward=1
with a full CTRF pass.
² pytest counts parametrized cases; "97" here is CTRF test rows, "50"/"25" elsewhere are test
functions.
³ On the two heaviest tasks the environment stage lost most of its trials to lease-starvation
(heavy dual-container verify under `--parallel 4`): `wal-recovery-ordering` completed 2/18,
`batched-eval-parity` 3/18 — **every completed env trial passed (reward=1)**, but the env axis
is under-powered for these two. Their base / isolation / degenerate / attribution stages ran to
completion and are clean. A dedicated re-run at `--parallel 1` would close this gap.
⁴ `roy-polymorph-cn` was audited clean, and its oracle demonstrably ran RDKit for real offline
(`solve.log` shows RDKit conformer warnings) because its environment image pre-warms the `uv`
cache with the exact pinned versions the solve then requests. The classifier was subsequently
tightened to skip `uv run --with …` conservatively (it can't cheaply prove the cache is warm at
load time), so this task moves to `solve_network` going forward. The completed audit stands.

### Not audited / infra-limited

| Task | Reason |
|---|---|
| foodstuff-beta-activity | oracle `solve.sh` does `uv run --with pandas …` with **no** pre-warmed cache → PyPI fetch fails offline → **adapter limitation, not a defect** (see §3). Reclassified `solve_network`. |
| wdm-design | environment image build fails — `mamba install … pymeep` needs network / heavy solve |
| telecom-entity-resolution | environment build hit a transient Docker Hub token failure; also `solve_network` (`uvx --with pandas`) |
| formal-crypto | not attempted — heavy SageMath environment, 5400s verifier budget |

---

## 3. Findings — ranked by impact + novelty

**None.**

No finding of any class (`ACCEPTS_TRIVIAL`, `UNSOUND_ACCEPT`, `SHALLOW_TEST_SET`,
`STATEFUL_VERIFIER`, `NONDETERMINISTIC_VERIFIER`, `FALSE_FAIL`, `ANSWER_LEAKAGE`,
`ENV_SENSITIVE_VERDICT`, `OUTPUT_FORM_SENSITIVITY`, `VERIFIER_CRASH`, `REFERENCE_REJECTED`)
survived replay and semantic inspection on any supported task.

### The two HIGH `REFERENCE_REJECTED` alerts raised this session (both withdrawn)

| # | Task | Root cause | Status |
|---|---|---|---|
| 1 | risk-scorer-replay | Adapter bug: `_extract_artifacts` doubled the path prefix for directory artifacts (`artifacts=["/app/"]` → members landed at `/app/app/…`), so the oracle's own artifacts never reached the verifier. | Fixed mid-session; re-audited **clean** (33 trials, run 5). |
| 2 | foodstuff-beta-activity | Adapter limitation: oracle `solve.sh` runs `uv run --with pandas==2.2.3 --with xlrd --with pdfplumber`; the env image does not pre-populate the `uv` cache, so the fetch hits PyPI and fails under the network-disabled solve sandbox. `solve.log`: `error: Failed to fetch … dns error`. The verifier then fails all 13 CTRF rows because `/app/results.txt` was never produced. | Classifier tightened to catch `uv run --with` / `uvx --with` / `--with-requirements`; task now skipped `solve_network`. Receipt deleted. |

Both are Breakwater-side. Neither is a Harbor bug.

### What the audit positively confirmed, per audited task

- **Trivial-solution rejection** — every no-op / `exit 1` / empty-artifact probe → reward=0.
  The only tests such probes pass are deliberate *negative* assertions (e.g. `shadow-relay` and
  `production-planning` each have one test that checks a file was **not** modified). The tester
  correctly did **not** flag these — expected test design, not a defect.
- **Determinism** — 3× oracle replicates per task, identical verdicts; no task needed the
  flaky-verdict follow-up sampling.
- **Environment independence** — see §2 note ¹.
- **No statefulness** — reversed-order and solo-in-fresh-container oracle runs all pass.
- **No output-form sensitivity / false-fail** — generic + declared output transforms on the
  baseline artifacts produced no verdict flip.
- **No answer leakage** — `verify_only` replays with only the declared artifacts reproduce the
  verdict.

---

## 4. Replay evidence

Artifacts are in MinIO (`artifacts` bucket, `127.0.0.1:59000`), keyed
`{experiment_id}/{experiment_id}_{label}/attempt-{n}/{meta.json,ctrf.json,reward.txt,solve.log,verify.log}`.
Verbatim from stored `meta.json` + `ctrf.json`:

```
audit-hb_production_planning-base-577bd8_oracle-0      reward=1  CTRF 20/20   rc=0
audit-hb_production_planning-degen-3c8185_noop_none    reward=0  CTRF  1/20   (3 artifacts missing)
audit-hb_wal_recovery_ordering-base-10c668_oracle-0    reward=1  CTRF 97/97   rc=0
audit-hb_wal_recovery_ordering-degen-41961e_noop_none  reward=0  CTRF 44/97
audit-hb_shadow_relay-base-*_oracle-0                  reward=1  CTRF  8/8    rc=0   (collect-hook produced /tmp/agent.patch)
audit-hb_shadow_relay-degen-*_noop_none                reward=0  CTRF  1/8
audit-hb_roy_polymorph_cn-base-*_oracle-0              reward=1  CTRF  3/3    rc=0   (RDKit ran offline via warm uv cache)
audit-hb_glycan_ms2_elucidation-base-*_oracle-0        reward=1  CTRF 12/12   rc=0
audit-hb_interleaved_vigenere-env-ab7d45  (×18 perturbations)   reward=1  CTRF 6/6   each
```

Withdrawn-finding evidence:

```
audit-hb_foodstuff_beta_activity-base-63f6a4_oracle-0  reward=0  rc=2
  solve.log:  error: Failed to fetch: `https://pypi.org/simple/pandas/`
              Caused by: dns error: Temporary failure in name resolution
```

No receipt files remain (`receipts/hb_*` — none). `replay` on the withdrawn foodstuff receipt
would reproduce `outcome: fail`, but the cause is the offline solve sandbox, not the verifier.

---

## 5. Maintainer-actionable?

**No.** There is nothing to report to the Harbor maintainers. The supported tasks that were
audited have sound, deterministic, environment-independent, stateless verifiers that reject
degenerate solutions. Several are notably well-hardened — e.g. `interleaved-vigenere`'s verifier
carries explicit anti-ptrace defenses (`PR_SET_DUMPABLE=0`, core-dump suppression, dropping the
cracker subprocess to `nobody`) with inline comments citing the PR-review trial that motivated
each one.

---

## 6. Limitations of this audit (stated plainly)

1. **The mutation / discrimination stage did not run** on any Harbor task. It needs an
   in-process, Python-callable oracle to build single-edit mutants and search for divergence
   inputs; Harbor oracles are shell scripts in containers, so the tester logs
   `discrimination stage skipped: no local-eval oracle for this runtime`. The
   `SHALLOW_TEST_SET` / `EQUIVALENT_MUTANTS` classes — the productive ones on HumanEval — are
   unreachable here. **A clean Harbor audit therefore does not certify test-set depth.**
2. **Coverage is a sample:** 10 of 32 supported tasks. The rest share the format and the
   `tests/test.sh` contract and are expected to behave similarly, but were not run.
3. **~50% of the benchmark is structurally out of scope for this single-container adapter:**
   13 `multi_service` + 11 `solve_network` tasks are entirely unaudited. Auditing them needs
   adapter work (multi-container orchestration; a solve-time package cache or a network-allowed
   solve sandbox with egress controls).
4. **Two tasks' env axis is under-powered** (`wal-recovery-ordering`, `batched-eval-parity` —
   §2 note ³). Every completed env trial passed; the sample is just small.

---

## 7. Adapter status

**Trustworthy for the finding classes it can reach.** Six fidelity bugs were found and fixed
this session — **all adapter bugs, none benchmark defects** — each caught by triage before (or,
for #6, during) being reported:

| # | Bug | Symptom | Fix |
|---|---|---|---|
| 1 | Raw bytes in task dict | `asyncpg DataError` (task dict is Postgres JSONB) | base64-encode solution tree + artifact bytes |
| 2 | Class-based tests skipped | `ctr-optimization`, `shadow-relay`, `wal-recovery-ordering` wrongly `no_tests` | `_parse_test_names` descends into `Test*`/`*Test` classes |
| 3 | Artifact path doubling | `artifacts=["/app/"]` → members at `/app/app/…` → spurious `REFERENCE_REJECTED` on risk-scorer-replay | `parent = os.path.dirname(p.rstrip("/")) or "/"` |
| 4 | Multi-service not skipped | `ctr-optimization` dict-artifact + 2-service compose ran as one container | `load()` skips non-str artifacts / `_service_count > 1` |
| 5 | `[[verifier.collect]]` ignored | `shadow-relay` oracle failed — `/tmp/agent.patch` never produced | `_solve` appends collect-hook commands to the solve script; re-audited clean |
| 6 | `uv run --with` not caught | `foodstuff-beta-activity` spurious `REFERENCE_REJECTED` (solve needs PyPI) | `_NET_INSTALL_RE` extended; full solution tree scanned, not just `solve.sh` |

Regression coverage: `tests/test_harbor_adapter.py` (3 tests — net-install detection incl. the
multiline `uv run --with` case, pytest-id extraction) + `tests/test_tbench_adapter.py` (22).
All pass (`25 passed, 1 skipped`).

---

## Appendix — reproduce

```bash
uv run python reports/harbor_audit_support/hb_classify.py    # task classification

PYTHONUNBUFFERED=1 PX_BATCH_TIMEOUT=1500 PX_WORKER_CONCURRENCY=2 \
  uv run python -m projectx.tester audit \
  --adapter "harbor=external/harbor-tb/tasks#session-window-debug,shadow-relay" \
  --adapters-only --parallel 4 --workers 8 --out audit.json
```

Raw audit outputs: `reports/harbor_audit_support/audit_harbor_v4_run5.json` (5 tasks),
`audit_harbor_v4c.json` (6 tasks). Adapter regression tests: `uv run pytest tests/test_harbor_adapter.py`.
