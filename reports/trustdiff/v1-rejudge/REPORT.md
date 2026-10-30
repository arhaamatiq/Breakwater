# Verifier Trust Diff

- **base** `v4.0.0` (452bf305c6da)
- **head** `v4.0.0` (452bf305c6da)
- repeats 2 per version, contested bundles topped up to 3; outputs solved under `base`; network: task policy
- run `td-20261030-024239-4490` generated 2026-10-30T03:25:26-0700

Every bundle is a fixed set of artifact bytes. Both verifier versions judge the same bytes; the verdict is the task's reward exactly as Harbor records it.

## Summary

| task | bundles | reference | changes | notes |
|---|---:|---|---|---|
| batched-eval-parity | 14 | PASS→PASS | survives_both×13 |  |
| bun-sourcemap-leak | 3 | PASS→PASS | survives_both×2 |  |
| cargo-flight-dispatch | 4 | PASS→PASS | survives_both×3 |  |
| embedding-drift-monitor | 7 | PASS→PASS | survives_both×6 |  |
| interleaved-vigenere | 4 | PASS→PASS | survives_both×3 |  |
| risk-scorer-replay | 8 | PASS→PASS | survives_both×7 |  |

## Changes

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/byte_stops.py__bin(Sub)#3` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 1.00–1.00)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--b630929f.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/byte_stops.py__cmp(Eq)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.94–1.07)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--1729e23b.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/byte_stops.py__int+1(0)#1` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.94–1.06)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--4ba1559b.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/byte_stops.py__int-1(1)#7` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.93–1.08)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--af47df84.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/evaluate.py__cmp(Eq)#7` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.97–1.03)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--957a3eeb.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/evaluate.py__int+1(1)#6` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 1.00–1.00)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--381f2cc7.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/evaluate.py__int-1(1)#3` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.98–1.03)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--4947fa3f.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--ast__/app/evalbench/generation.py__int+1(0)#6` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.98–1.02)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--bf843ba0.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--drop_file__/app/evalbench/__init__.py` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.97–1.03)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--97307623.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--drop_file__/app/evalbench/__pycache__/generation.cpython-311.pyc` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.88–1.14)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--e892c078.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--drop_file__/app/evalbench/__pycache__/prefix_cache.cpython-311.pyc` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.91–1.10)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--018e7ae0.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--drop_file__/app/evalbench/__pycache__/scoring.cpython-311.pyc` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.70–1.43)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--bdedcbf8.json`

### [LOW] batched-eval-parity: `bundle::v1-survivor--drop_file__/app/evalbench/reference.py` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.93–1.07)

Replay: `breakwater replay receipts/batched-eval-parity--survives_both--e713a65c.json`

### [LOW] bun-sourcemap-leak: `bundle::v1-survivor--drop_file__/app/src/generated/prompt-template.ts` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.89–1.12)

Replay: `breakwater replay receipts/bun-sourcemap-leak--survives_both--0ae72f69.json`

### [LOW] bun-sourcemap-leak: `bundle::v1-survivor--drop_file__/app/tsconfig.json` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.67–1.49)

Replay: `breakwater replay receipts/bun-sourcemap-leak--survives_both--c6575d4d.json`

### [LOW] cargo-flight-dispatch: `bundle::v1-survivor--ast__/app/aircraft.py__int+1(36089)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.95–1.05)

Replay: `breakwater replay receipts/cargo-flight-dispatch--survives_both--8cbc4e2a.json`

### [LOW] cargo-flight-dispatch: `bundle::v1-survivor--drop_file__/app/apt-packages.txt` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.96–1.04)

Replay: `breakwater replay receipts/cargo-flight-dispatch--survives_both--52b8353f.json`

### [LOW] cargo-flight-dispatch: `bundle::v1-survivor--drop_file__/app/requirements.txt` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.91–1.09)

Replay: `breakwater replay receipts/cargo-flight-dispatch--survives_both--54e4f4dd.json`

### [LOW] embedding-drift-monitor: `bundle::v1-survivor--ast__/app/drift_monitor/calibration.py__int+1(100)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.45–2.21)

Replay: `breakwater replay receipts/embedding-drift-monitor--survives_both--89c82f79.json`

### [LOW] embedding-drift-monitor: `bundle::v1-survivor--ast__/app/drift_monitor/distance.py__bool(Or)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 1.00–1.00)

Replay: `breakwater replay receipts/embedding-drift-monitor--survives_both--6bf04b0d.json`

### [LOW] embedding-drift-monitor: `bundle::v1-survivor--ast__/app/drift_monitor/monitor.py__int+1(100)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.92–1.09)

Replay: `breakwater replay receipts/embedding-drift-monitor--survives_both--49374968.json`

### [LOW] embedding-drift-monitor: `bundle::v1-survivor--ast__/app/drift_monitor/monitor.py__int-1(100)#1` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.79–1.26)

Replay: `breakwater replay receipts/embedding-drift-monitor--survives_both--e7f7817c.json`

### [LOW] embedding-drift-monitor: `bundle::v1-survivor--ast__/app/drift_monitor/statistical_tests.py__int+1(10)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.92–1.09)

Replay: `breakwater replay receipts/embedding-drift-monitor--survives_both--07c3b607.json`

### [LOW] embedding-drift-monitor: `bundle::v1-survivor--drop_file__/app/data/.canary` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 1.00–1.00)

Replay: `breakwater replay receipts/embedding-drift-monitor--survives_both--d60c8434.json`

### [LOW] interleaved-vigenere: `bundle::v1-survivor--ast__/app/cracker.py__cmp(Lt)#5` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.90–1.11)

Replay: `breakwater replay receipts/interleaved-vigenere--survives_both--2da06f47.json`

### [LOW] interleaved-vigenere: `bundle::v1-survivor--ast__/app/cracker.py__int+1(26)#7` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.97–1.03)

Replay: `breakwater replay receipts/interleaved-vigenere--survives_both--a41cd22b.json`

### [LOW] interleaved-vigenere: `bundle::v1-survivor--drop_file__/app/requirements.txt` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.82–1.21)

Replay: `breakwater replay receipts/interleaved-vigenere--survives_both--55ddd140.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--ast__/app/parityctl/cli.py__cmp(Eq)#0` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.98–1.02)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--3eb4f0a6.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--ast__/app/parityctl/cli.py__cmp(Gt)#7` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.93–1.08)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--67a6ff6e.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--drop_file__/app/docs/review_ops_digest.md` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.97–1.03)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--aef22d21.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--drop_file__/app/output/parity_scores.csv` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.84–1.18)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--d7b07203.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--drop_file__/app/output/scorer_audit.sqlite` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.98–1.02)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--c50be055.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--drop_file__/app/parityctl/__pycache__/__init__.cpython-312.pyc` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.99–1.01)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--8c24e56d.json`

### [LOW] risk-scorer-replay: `bundle::v1-survivor--drop_file__/app/parityctl/__pycache__/cli.cpython-312.pyc` — survives_both

mutant passes under both versions (candidate test gap). Bundle kind `fixture`, expected `unknown`.

- base: 2/2 pass · head: 2/2 pass · Fisher p = 1.0000
- verifier wall time head/base = 1.00 (95% CI 0.99–1.01)

Replay: `breakwater replay receipts/risk-scorer-replay--survives_both--7dc5a32b.json`

