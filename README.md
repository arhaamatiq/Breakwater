# Project X — Distributed AI-Agent Evaluation Reliability Platform

A fault-tolerant distributed platform for running AI/benchmark evaluation trials:
Docker-isolated execution across concurrent workers, lease-based crash recovery,
Postgres-backed orchestration, S3 artifact provenance — and a rule-based
reliability layer that checks whether benchmark PASS/FAIL verdicts can be trusted.

**Core guarantee: at-least-once execution with exactly-once result effect.**
A trial may be executed more than once (crashes, stalls, retries), but its result
is counted exactly once — verified by invariant queries, not assumed.

## Architecture

```text
FastAPI control plane ──── reaper (lease expiry -> requeue)
        │
     Postgres  ← state machine / work queue / leases / result ledger / provenance events
        │
 worker  worker  worker ...   (claim via SELECT ... FOR UPDATE SKIP LOCKED)
        │
 Docker-isolated trials       (no network, capped cpu/mem, files injected via tar)
        │
 MinIO (S3 API)               (stdout, result.json, meta.json per attempt)
```

### Correctness mechanics
- **Claim**: `SELECT ... FOR UPDATE SKIP LOCKED` — concurrent workers dequeue without contention or double-claims.
- **Lease + heartbeat**: every running trial has `lease_expires_at`, extended by
  the owning worker every 2s. Heartbeats are fenced by `worker_id` — a stalled
  worker that lost its lease cannot extend someone else's.
- **Reaper**: requeues trials whose lease expired (crashed/stalled worker);
  after `max_attempts` the trial is marked `failed` instead of looping forever.
- **Result ledger**: `INSERT ... ON CONFLICT (trial_id) DO NOTHING`. Deterministic
  `trial_id` + primary key = one accepted result per trial, ever. A zombie worker
  that resumes and submits already-re-executed work is suppressed (and counted).

Trial states: `queued → running → completed | failed` (`failed` = infra retries
exhausted; a verifier FAIL is a *completed* trial with outcome `fail`).

## Run it

```bash
docker compose up -d          # Postgres :55432, MinIO :59000 (console :59001)
uv sync
uv run python -m projectx.setup   # schema + bucket + trial image

# chaos demo: ~96 trials, SIGKILL one worker, SIGSTOP+SIGCONT another mid-run
uv run python -m projectx.demo

# reliability probes: oracle x5 / noop / repeated agent per task + rule engine
uv run python -m projectx.reliability
```

Outputs: `demo_report.json`, `reliability_report.json`, worker logs in `logs/`,
artifacts browsable in MinIO console (http://localhost:59001, projectx/projectx123).

## Measured results (this repo, one MacBook, Docker Desktop)

Chaos demo — 96 Docker-isolated trials, 4 workers x 3 concurrent trials,
SIGKILL one worker + SIGSTOP/SIGCONT another mid-run:

| metric | value |
|---|---|
| trials / accepted results | 96 / 96 |
| executions started (at-least-once) | 102 |
| requeues after lease expiry | 6 |
| trials recovered by a different worker | 6 |
| duplicate submissions suppressed by ledger | 3 |
| **lost results / duplicate result rows** | **0 / 0** (verified by SQL invariants) |
| throughput | 6.6–7.3 trials/s |
| exec latency p50 / p99 | ~0.9–1.0s / ~1.4–2.5s (container-isolated) |
| wall clock | ~14s |
| provenance artifacts in S3 | 3 per execution attempt (stdout, result, meta) |

Reliability probes (oracle x5 / noop / agent x2 across 12 tasks, 96 probe trials):
flagged exactly the 2 planted flaws (`NONDETERMINISTIC_VERIFIER` at 60% oracle
pass rate, `WEAK_VERIFIER` accepting the no-op) with 0 false positives on the
10 sound tasks.

## The chaos demo

~96 trials (12 tasks × 8 replicates) on 4 workers (3 concurrent trials each). Mid-run:

1. **SIGKILL w1** — hard crash. Its leases expire, the reaper requeues, a
   replacement worker picks the trials up.
2. **SIGSTOP w2 for ~15s, then SIGCONT** — simulated GC pause / network partition.
   Its leases expire and trials are re-executed elsewhere; when the zombie
   resumes and submits, the ledger suppresses the duplicates.

The report then *verifies* (by SQL, from the actual tables):
- `lost_results = 0` — every enqueued trial has exactly one accepted result
- `duplicate_result_rows = 0` — nothing double-counted
- plus throughput, exec/e2e p50/p99, retries, requeues, suppressed duplicates,
  and how many trials were recovered by a different worker than first claimed them.

## Tester core: verdict falsification engine

`src/projectx/tester/` treats the **verifier as the system under test**. Each
benchmark task is audited against eight explicit verdict axioms (soundness,
garbage rejection, determinism, environment invariance, per-case isolation,
non-gameability, declared-equivalence consistency, discrimination); every
finding names the axiom it falsifies and ships as a **replayable falsification
receipt** (`receipts/*.json`) that re-validates itself in fresh containers.

```bash
uv run python -m projectx.tester audit            # audit all tasks
uv run python -m projectx.tester audit --task X   # one task
uv run python -m projectx.tester replay receipts/<file>.json
```

Instruments (all run through the real distributed pipeline):
- **Probe portfolio** — no-op/constant/echo/raising solutions plus an
  answer-**leak probe** that hunts for expected outputs in reachable files;
  per-case accept analysis with Python-`==` coincidence exclusion.
- **Mutation cross-examination** — single-edit AST mutants of the reference
  are prescreened locally, benchmark-equivalent ones run in-container; for
  each surviving mutant the tester *searches for an input where it deviates
  from the reference*, shrinks it, and validates the divergence in-container:
  a concrete test case the benchmark is missing.
- **Two-level nondeterminism isolation** — `verify_only` replays re-verify
  FIXED outputs, separating verifier flakiness from execution flakiness.
- **Environment attribution** — verdicts re-checked under pinned
  PYTHONHASHSEED/TZ/LC_ALL; instability that is stable *within* a seed and
  differs *across* seeds is attributed (and the generic "flaky" finding is
  replaced by the causal one).
- **Case-isolation probes** — the same (input, output, expected) triple
  verified alone, in-batch, and in reversed order; verdict differences prove
  a stateful verifier.
- **Split solve/verify harness** — the solution container never contains the
  verifier or expected outputs (the tester caught the v1 harness leaking
  answers; `leaky_lookup` keeps the old harness as a planted regression).

Results on the 17-task suite (7 planted flaws, 10 sound tasks):
**7/7 planted flaws detected with the intended class, 0 CRITICAL/HIGH false
positives on sound tasks, 9/9 receipts replay-confirmed** — plus one genuine
unplanted find: the `binary_search` benchmark passes a mutant that *crashes
with IndexError on `([], 0)`* (no empty-array case), which the naive layer
below calls RELIABLE. The naive layer catches 3/7 of the planted flaws and
misdiagnoses hash-seed sensitivity as generic nondeterminism.

## Adapter layer: auditing real benchmarks

`src/projectx/adapters/` converts external benchmark formats into the internal
task representation; the tester core runs unchanged. A converted task declares
`capabilities` (which instruments apply), optional `probe_solutions` in the
benchmark's own solution language, provenance, and — for benchmarks the
built-in Python flow can't execute — a named `runtime`. Trials carry the
converted task inside their spec, so workers and receipts stay adapter-blind
and receipts remain self-contained.

```bash
# real HumanEval (value semantics; conversion only, no runtime code)
uv run python -m projectx.tester audit \
  --adapter "humaneval=external/HumanEval.jsonl#HumanEval/0,HumanEval/35" --adapters-only

# real Terminal-Bench classic tasks (state semantics; docker-commit runtime)
uv run python -m projectx.tester audit \
  --adapter "tbench=external/terminal-bench/original-tasks#hello-world" --adapters-only

# validate detection wiring on any loaded task
uv run python -m projectx.tester audit --adapter ... --plant he_HumanEval_0:weak
```

**humaneval** — oracle = prompt + canonical solution; cases = the benchmark's
own asserts (parsed from `check(candidate)`); the verifier replays the
original assert source against recorded outputs, and encodes the official
exception-equals-fail semantics. Conversion is validated locally per task
(146/164 problems convert; drops are counted, never silent).

**tbench** — a case per pytest test; solve runs the shell solution in the
task's own image and `docker commit`s the state; the committed image id is the
output value, so verify-only replays re-judge the identical state, case
subsets/reordering probe inter-test dependence, and env deltas reach pytest.
Tests are never present during solve. Deltas from the official harness
(pytest baked in at prep, direct pytest instead of run-tests.sh) are
documented in the adapter.

## Reliability layer (rule-based)

For each benchmark task the platform runs probe experiments and applies rules:

| Probe | Rule | Flag |
|---|---|---|
| oracle ×5 | pass rate strictly between 0 and 1 | `NONDETERMINISTIC_VERIFIER` |
| oracle ×5 | never passes | `BROKEN_TASK` |
| no-op ×1 | passes | `WEAK_VERIFIER` |
| agent ×2 | identical runs disagree | `NONDETERMINISTIC_REPEAT` |
| any | trial exhausted retries | `INFRA_FAILURE` |

The bundled suite contains 12 tasks, two with **planted, labeled flaws**
(`flaky_timing`: RNG-dependent verifier; `weak_accepts_empty`: verifier that
accepts anything) — the layer flags exactly those. The agent solution for
`binary_search` has a deliberate off-by-one, so agent runs show honest FAILs.

## Layout

```text
src/projectx/
  api.py          FastAPI control plane + reaper
  worker.py       claim / heartbeat / execute / submit loop
  db.py           all SQL: claims, leases, ledger, provenance
  runner.py       Docker-isolated execution + artifact upload
  report.py       metrics + invariant verification
  reliability.py  probe experiments + rule engine
  demo.py         chaos demo orchestrator
  benchmarks/suite.py  12-task suite (2 planted flaws)
db/schema.sql
docker-compose.yml
```

## Honest limitations / next steps
- Single-box demo: workers are processes on one machine (the coordination layer
  wouldn't change across machines — everything goes through Postgres).
- Reaper runs inside the API process; multi-instance would need leader election.
- Tasks are static Python benchmarks, not live LLM agents; the trial contract
  (solution × verifier in an isolated container) is the same either way.
- Planned Backend B: Harbor. `GET /experiments/{id}/stream` serves the provenance log as SSE.
