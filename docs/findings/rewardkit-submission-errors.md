# Malformed submissions crash migrated verifiers instead of scoring 0

**Status:** confirmed on `glycan-ms2-elucidation` with Breakwater and with the
official harness (harbor 0.25 `trial regrade`). Not yet reported upstream.

## Summary

PR [#2287](https://github.com/harbor-framework/terminal-bench/pull/2287)
("Migrate structured report verifiers to native RewardKit", merged
2026-10-22) moved eight verifiers from pytest to RewardKit criteria, "with
preserved task inputs and reward predicates". Under pytest, *any* exception
while reading a submission failed a test and the task wrote reward 0. Under
RewardKit, an exception escaping a criterion aborts the whole reward: no
`reward.json`, and Harbor records the trial as a verifier exception
(`RewardFileNotFoundError`) rather than reward 0.

The migrated criteria guard against this with an explicit allowlist,
`SUBMISSION_ERRORS`, of exception types that score 0. In
`glycan-ms2-elucidation` the allowlist omits the errors `openpyxl` raises for
a non-workbook file (`zipfile.BadZipFile`,
`openpyxl.utils.exceptions.InvalidFileException`,
`xml.etree.ElementTree.ParseError`), although the task reads a declared,
agent-writable artifact with `openpyxl.load_workbook`. The sibling task
`fin-saccr-rwa`, migrated in the same PR, includes exactly those three.

## Evidence

Same bytes, both verifier versions, `breakwater diff --repo terminal-bench
--base v4.0.0 --head 06a92e79 --tasks glycan-ms2-elucidation`:

| bundle (derived from the reference output) | v4.0.0 (pytest) | main (RewardKit) |
|---|---|---|
| reference | pass ×5 | pass ×5 |
| `zero_all` — every artifact truncated to 0 bytes | **fail ×5** (reward 0) | **error ×5** (no reward) |
| `halve_all` — every artifact truncated to half | **fail ×5** (reward 0) | **error ×5** (no reward) |
| no-op, `drop_all`, `digits_all`, three `drop_file` mutants | fail | fail |

Head-version log (RewardKit):

```
RuntimeError: Criterion 'test_metadata_identifies_negative_esi_cid' failed: File is not a zip file
RuntimeError: Reward 'checks' failed: Criterion 'test_metadata_identifies_negative_esi_cid' failed: File is not a zip file
```

Official-harness cross-check (`harbor trial regrade`, harbor 0.25, same bundle
written into a copy of a Harbor oracle trial):

| bundle | v4.0.0 task | main task |
|---|---|---|
| reference | reward 1.0 | reward 1.0 |
| `zero_all` | reward 0.0 | `RewardFileNotFoundError` |
| `halve_all` | reward 0.0 | `RewardFileNotFoundError` |

## Why it matters

- Terminal-Bench leaderboard rules count errored trials as reward 0, so
  leaderboard scores are unchanged — but the trial is reported as a verifier
  *error*: `/regrade` lists it as a regrade error instead of a reward, error
  breakdowns attribute an agent mistake to infrastructure, and RL consumers
  of Harbor rewards that drop errored rollouts never penalize the malformed
  output.
- Terminal-Bench's validation gate cannot see it: the oracle passes, and a
  no-op produces *missing* files (`FileNotFoundError`, which is allowlisted).
  It takes an output that exists but is malformed — which Breakwater's
  degenerate corpus generates mechanically.

## Suggested fix

Add `BadZipFile`, `InvalidFileException` and `ParseError` to
`glycan-ms2-elucidation/tests/checks.py`'s `SUBMISSION_ERRORS`, as
`fin-saccr-rwa` already does (or adopt `bun-sourcemap-leak`'s pattern: score
any exception from submission handling as 0 and re-raise only RewardKit's
`WorkerSetupError`).

## Reproduce

```bash
git clone https://github.com/harbor-framework/terminal-bench tb
breakwater up
breakwater diff --repo tb --base v4.0.0 --head 06a92e790027045921482f2acd23f3fa598ffe38 \
  --tasks glycan-ms2-elucidation --corpus reference,noop,degenerate
breakwater replay reports/trustdiff/<run>/receipts/glycan-ms2-elucidation--verifier_error_introduced--*.json
```
