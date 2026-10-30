# Adapter gaps — terminal-wrench corpus (checkpoint 1)

Run date: 2026-09-10
Corpus: `https://github.com/few-sh/terminal-wrench`
Clone SHA: `d8a29613235a0ef56a8b70b3142626a533da28c2` (shallow clone, depth 1)
Corpus size on disk: 7.4 GB; 331 task ids under `tasks/`

Every gap below is an **adapter/runtime limitation, not a benchmark defect**. No
task, environment, or adapter was modified. Nothing here is a finding.

---

## Checkpoint 1 result: 3 of 3 tasks fail, on both existing adapters

Three tasks were selected from three different source datasets and staged in the
layout each adapter's `load()` expects (symlink `<stage>/<task_id>` ->
`tasks/<task_id>/<model>/original_task`; the corpus nests each task under a model
directory). Staging is a path presentation only — no file contents were altered.

| task id | source dataset | `harbor.load()` | `tbench.load()` |
|---|---|---|---|
| `1012` | seta_2026_01_29 | FAIL — `format` | FAIL — `format` |
| `analyze-fen-with-stockfish` | terminal-bench-pro | FAIL — `format` | FAIL — `format` |
| `3d-model-format-legacy` | TerminalBench-original | FAIL — `format` | FAIL — `format` |

Exact failure strings, verbatim from `LAST_LOAD_REPORT`:

- harbor: `missing a required v4 file (['tests/Dockerfile'])`
- tbench: `not a classic-format task dir`

Both failures occur during `load()`, before any container is created. No solve or
verify ran, so there is no verdict data of any kind from this corpus.

## Why it fails — this is a format-generation gap, not a per-task quirk

The 331 terminal-wrench tasks are **Terminal-Bench 2.0 / Harbor "shared" mode**.
Breakwater's harbor adapter implements **only Harbor v4 "separate" mode**. Surveyed
across all 331 tasks:

| property | count | consequence |
|---|---|---|
| `environment_mode` resolves to `shared` | 331 / 331 | harbor supports only `separate` |
| `tests/Dockerfile` present | 0 / 331 | harbor requires it (this check fires first) |
| `tests/test.sh` present | 331 / 331 | — |
| `solution/solve.sh` present | 331 / 331 | — |
| `environment/Dockerfile` present | 331 / 331 | — |
| GPU requested | 0 / 331 | — |
| dict-shaped (service-scoped) artifacts | 0 / 331 | — |
| `environment/docker-compose.yaml` present | 1 / 331 | — |

`shared` mode means the verifier runs **in the agent's own container** rather than
in a fresh container fed a declared artifact manifest. There is no `artifacts` key
to carry state across, because no state crosses a boundary. tbench.py's
docker-commit model is the closer structural match, but tbench.py keys on the
classic `task.yaml` / root `Dockerfile` / `solution.sh` layout, which none of these
331 tasks use.

The corpus was also confirmed self-consistent on this: all 331 tasks have byte-identical
`original_task/` trees across their per-model directories, so the model directory
chosen for staging does not affect any of the above.

## The second, harder gap: verify-time network

Even with a shared-mode adapter written, most of this corpus stays out of reach.
The tasks' own `tests/test.sh` bootstraps its test environment at verify time —
the dominant variant (205 of 331 tasks byte-identical; 99 distinct `test.sh`
contents overall) runs:

```
apt-get update && apt-get install -y curl
curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
uv init && uv add pytest==8.4.1
uv run pytest /tests/test_outputs.py -rA
```

Breakwater runs both solve and verify with `network_disabled=True`. Counting tasks
whose scripts reference a network install:

| | count of 331 |
|---|---|
| `tests/test.sh` needs network | 323 |
| `solution/solve.sh` needs network | 69 |
| **neither needs network (reachable under hermetic execution)** | **8** |

The 8 hermetically reachable tasks: `1217`, `164`, `buffer-overflow-exploit`,
`iot-device-registration-server`, `iris-data-loading-script`,
`sakila-sql-optimization`, `sympy-matrix-fix`, `titanic-female-survival-prediction`.

So the realistic ceiling for this corpus without compromising hermeticity is 8
tasks — narrower than the 32 TB 4.0 tasks the existing harbor adapter already
supports. Closing the gap the other way (enabling network at verify, or pre-baking
uv/pytest so `uv add` hits a warm cache) changes the verify environment. Under the
project's own standing rule, mutation survival measured in a modified verify
environment is a false positive until proven otherwise, which is exactly the
failure mode that produced the earlier `[[verifier.collect]]` misdiagnosis.

## Disposition

Checkpoint 1's stated rule — "if 2 of 3 fail, stop and tell me — I'd rather fall
back to ablating TB 4.0's 66 than lose the morning to adapter work" — is met at
3 of 3. Falling back to TB 4.0 per that pre-authorized instruction. The
terminal-wrench corpus is skipped in full; zero tasks from it were executed.

What would be required to reach it later, recorded so the estimate is not re-derived:
a shared-mode harbor variant (commit the solve container, run `tests/test.sh`
verbatim inside a container started from that committed state, parse the
`PytestParser` stdout or `/logs/verifier/reward.txt` it writes), plus a decision on
verify-time network that is a faithfulness question, not an engineering one.

---

# Part 2 — Runtime gaps hit during the TB 4.0 ablation run

These are limits of Breakwater's Harbor runtime, not statements about the
benchmark. Every task below is excluded from the mutation statistics.

## 2a. Adapter `load()` skips (scope boundaries, by design)

Of the 66 TB 4.0 tasks, `harbor.load()` excluded tasks before execution for the
reasons it already documents: `solve_network`, `multi_service`, `gpu`,
`no_tests`, `browser`, `unsupported_mode`. Observed during this run, for example:

- `cad-model`, `fin-saccr-rwa`, `foodstuff-beta-activity`, `roy-polymorph-cn`
  — `solve_network` (the reference `solve.sh` pip/apt-installs at solve time,
  and solve runs with networking disabled)
- `ctr-optimization`, `cumulative-layout-shift` — `multi_service`

Note `foodstuff-beta-activity` and `roy-polymorph-cn` were auditable in the
previous session but are now skipped, consistent with the `_NET_INSTALL_RE`
tightening recorded for the `uv run --with` case.

## 2b. The 300 s solve/verify ceiling (infrastructure, cost-driven)

`harbor.py` clamps both timeouts with `min(task.toml value, 300)`. Three tasks
could not complete their reference control inside it and are logged as skipped:

| task | control outcome | note |
|---|---|---|
| `hb_coq_block_bound` | reference trial `failed` | Coq image; build alone took 516 s |
| `hb_data_anonymization` | reference trial `failed` | `verify: timeout/wait failure: UnixHTTPConnectionPool ... Read timed out` |
| `hb_distributed_dedup` | reference outcome `fail` | completed, but the shipped reference did not pass its own verifier under this runtime |

`hb_distributed_dedup` is the one to look at first: it is not a timeout. The
reference solution ran to completion and the task's own verifier returned
`fail`. That is either a runtime faithfulness gap or something about the task,
and this run does not distinguish the two. It is recorded, not diagnosed.

## 2c. Expensive tasks starve a FIFO queue

`hb_batched_eval_parity` verify costs ~170–280 s per trial (its own pytest run
reports `5 passed in 247.98s`). `hb_wal_recovery_ordering` and
`hb_session_window_debug` are similar. Because `claim_trial` is
`ORDER BY created_at`, their large mutant sets blocked cheaper tasks.

To buy breadth inside the time budget, 254 **queued, never-executed** trials for
those three tasks were deleted from the queue (state `queued` only — no
completed result, no artifact, and no running trial was touched). They are
counted in the report as `mutants_built_not_run`, not as killed. Affected:
`hb_wal_recovery_ordering` 107, `hb_batched_eval_parity` 92,
`hb_session_window_debug` 55.

Consequence for reading the numbers: `hb_wal_recovery_ordering` (0 mutants with
a verdict, 28 errored, 71 not run) and `hb_session_window_debug` (2 with a
verdict) are effectively unmeasured. Their AST identity controls never ran
either, so no AST result for those two tasks would have been informative anyway.

## 2d. Mutation coverage is bounded by caps, not by the corpus

Per-task caps of 8 `drop_file` mutants and 8 AST mutants per Python artifact
were applied (deterministic evenly-spaced selection). The AST operator set comes
from the existing `projectx.tester.mutate`: comparator swap, binop swap, boolop
swap, integer ±1. `mutants_built_not_run = 278` is dominated by 2c.
