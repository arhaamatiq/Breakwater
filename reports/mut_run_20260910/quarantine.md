# Quarantine list

Run: `reports/mut_run_20260910/` — 2026-09-10
Corpus: Terminal-Bench v4.0.0, clone SHA `452bf305c6daa62fc59061d22133a7cbc7c1572e`

## Control-violation quarantine (no-op solution passed)

**Empty. 0 tasks.**

The no-op control — a `solve.sh` of `#!/bin/bash\ntrue`, run in full mode through
the real solve/commit/extract/verify path — was executed on every task that
loaded. It returned `fail` on all 14 tasks that entered the mutation phase, and
on 2 of the 3 tasks that were skipped for other reasons. No task's verifier
accepted a solution that did nothing, so nothing was quarantined on that basis
and no task was excluded from the statistics for a control violation.

| task | reference control | no-op control |
|---|---|---|
| hb_atrx_vep_crispr | pass | fail |
| hb_batched_eval_parity | pass | fail |
| hb_biped_contact_dynamics | pass | fail |
| hb_bun_sourcemap_leak | pass | fail |
| hb_cargo_flight_dispatch | pass | fail |
| hb_embedding_drift_monitor | pass | fail |
| hb_glycan_ms2_elucidation | pass | fail |
| hb_interleaved_vigenere | pass | fail |
| hb_production_planning | pass | fail |
| hb_risk_scorer_replay | pass | fail |
| hb_session_window_debug | pass | fail |
| hb_shadow_relay | pass | fail |
| hb_sound_change_cascade | pass | fail |
| hb_wal_recovery_ordering | pass | fail |

## Skipped: reference solution did not pass (excluded, not quarantined)

Protocol is to run the unmutated reference first and skip the task if it does not
pass. Three tasks were skipped:

| task | reason | category |
|---|---|---|
| `hb_coq_block_bound` | reference trial ended `failed`; no verdict produced | runtime timeout (see adapter_gaps.md 2b) |
| `hb_data_anonymization` | reference trial ended `failed`: `verify: timeout/wait failure: ... Read timed out` | runtime timeout (see adapter_gaps.md 2b) |
| `hb_distributed_dedup` | reference **completed** with outcome `fail` | **not a timeout — needs review** |

`hb_distributed_dedup` is the only one of the three where the shipped reference
solution ran to completion and the task's own verifier returned `fail`. This run
does not establish whether that is a runtime faithfulness gap or something about
the task. It is recorded for review, and is not counted as a finding.

## Effectively unmeasured (reported, but carrying almost no data)

Not quarantined; flagged so the per-task survival rates are not over-read.

| task | mutants with a verdict | errored | built but never run |
|---|---|---|---|
| `hb_wal_recovery_ordering` | 0 | 28 | 71 |
| `hb_session_window_debug` | 2 | 21 | 30 |
| `hb_shadow_relay` | 1 | 3 | 0 |

The first two lost their queued mutants to the FIFO-starvation cancellation
described in adapter_gaps.md 2c. Their AST identity controls never ran either, so
no AST verdict for those tasks would have been informative regardless.
