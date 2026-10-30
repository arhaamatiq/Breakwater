# Breakwater

**Regression tests for AI-benchmark verifiers.** When a benchmark changes how
it grades, Breakwater tells you which verdicts changed, whether the change is
deterministic or noise, and which check caused it — with replayable evidence.

[![ci](https://github.com/arhaamatiq/Breakwater/actions/workflows/ci.yml/badge.svg)](https://github.com/arhaamatiq/Breakwater/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

<!-- RESULTS -->

---

## Why

Agent benchmarks such as [Terminal-Bench](https://github.com/harbor-framework/terminal-bench)
grade with code — test suites, numeric thresholds, sandboxed workers — and that
code changes constantly. In October 2026 alone, Terminal-Bench migrated 57 task
verifiers to RewardKit. The standard safety check is to **regrade** recorded
agent trials with the new verifier and look for changed rewards.

Regrading has a blind spot: historical trials are mostly *passes* and honest
*failures*, so it can show a good output that now fails, but it cannot show a
**bad output that now passes**, or a verifier that now **crashes** where it
used to score. Those need inputs whose correct verdict is known to be *fail*.

Breakwater builds those inputs mechanically — no-ops, degenerate outputs,
single-edit mutants of the reference solution, and hand-written fixtures — and
judges every one of them with both verifier versions, repeatedly, on
byte-identical inputs.

## Quickstart

Requires Docker and Python ≥ 3.11.

```bash
uv tool install git+https://github.com/arhaamatiq/Breakwater   # or: pip install git+…
breakwater up        # Postgres in Docker + schema (the queue)
breakwater doctor    # docker / postgres / disk checks

git clone https://github.com/harbor-framework/terminal-bench tb
breakwater tasks tb/tasks                      # what can run here, and why not
breakwater check tb/tasks/sound-change-cascade # oracle must pass, no-op must fail

# diff two verifier versions of a task
breakwater diff --repo tb --base v4.0.0 --head origin/main --tasks glycan-ms2-elucidation
breakwater replay reports/trustdiff/<run>/receipts/<receipt>.json
```

`breakwater diff` with no `--tasks` diffs every task whose verifier changed.
Add hand-written inputs with `--fixture TASK:NAME:EXPECT:PATCH` (a diff applied
to the reference output) or `--solution-fixture` (a diff applied to the
reference *solution*, solved once). Extract numeric margins with
`--metric 'speedup=Geomean speedup:\s+([0-9.]+)x'`. In CI, use the bundled
[GitHub Action](action.yml) ([example workflow](examples/verifier-diff.yml)).

## How it works

```
 task @ base ──┐                                   ┌──> verdict matrix ──> classify ──> REPORT.md
               ├─ solve reference + no-op once ──> │    bundle × {base, head} × R      trustdiff.json
 task @ head ──┘  derive mutants / fixtures        │    (interleaved, topped up to 5    receipts/ + bundle tars
                  = content-addressed bundles  ────┘     when contested)
```

* **A Harbor-faithful runtime.** Tasks run the way the official harness runs
  them — reward file is the verdict, declared artifacts are the only bytes the
  verifier sees, the task's own network/CPU/memory policy applies — and every
  output is a content-addressed **bundle**. Fidelity is checked against
  official Harbor on identical bytes (`breakwater conform`).
* **A distributed queue.** Every (bundle, verifier version, repeat) is a trial
  in Postgres, claimed by Docker workers with leases and fencing tokens, and
  accounted exactly once. Trial ids are content addresses, so re-running a diff,
  adding a third version, or adding one fixture only computes what is new.
* **Classification against known expectations.** A reference that starts
  failing is a false-fail regression; a no-op or degenerate output that starts
  passing is a false-pass regression; a mutant the old verifier rejected and
  the new one accepts is a strictness regression; a verdict that becomes
  nondeterministic on identical bytes is instability. Each change is attributed
  to the checks whose pass rates moved, with Fisher exact p-values, and numeric
  margins get bootstrap confidence intervals.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Distributed systems engineering

The queue is the part that has to be right even when everything else fails.

| guarantee | mechanism | evidence |
|---|---|---|
| no double claims | `FOR UPDATE SKIP LOCKED`, a claim never waits while holding a lock | 32 concurrent claimers × 600 trials, `tests/test_queue.py` |
| crashed / stalled workers don't lose work | 6 s leases, 2 s heartbeats, reaper requeues expired leases | real lease expiry and re-claim in tests; chaos demo SIGKILLs a worker |
| a zombie can't corrupt a re-claimed trial | `lease_epoch` fencing token on heartbeat/release (worker id alone is not a fence) | same-worker-id re-claim test |
| results counted exactly once | ledger keyed by trial id, `INSERT … ON CONFLICT DO NOTHING`; stale submissions flagged | 30 concurrent submits → 1 winner; seeded fault injection (300 trials × 16 workers: stalls, crashes, double and late submits) |
| no starvation | fair claim: strict priority, then fewest-running experiment, then least-recently-claimed | a 1-trial experiment behind a 200-trial one is served within 2 claims (FIFO: position 198) |
| provable accounting | every state change and its event commit together; `verify_invariants()` reconciles ledger ↔ events by SQL | used by tests and the chaos demo |
| schema upgrades in place | idempotent DDL shipped as package data | v1 database with live rows upgraded twice ≡ fresh install |

<!-- CHAOS -->

What this does **not** claim: exactly-once *execution* (impossible with
crash-stop workers), or that the winning execution is the "right" one —
verdict trust is what the analyses are for.

## Single-version audits

`breakwater audit` treats one verifier version as the system under test:
degenerate probes, AST-mutant cross-examination with divergence search,
verify-only replays that separate verifier from execution nondeterminism,
environment attribution (`PYTHONHASHSEED`, `TZ`, locale), and case-isolation
probes. On a 17-task validation suite it detected 7/7 planted verifier flaws
with the intended class and 0 CRITICAL/HIGH false positives on 10 sound tasks;
on real HumanEval it independently rediscovered known test-depth gaps
(e.g. HumanEval/0's `<`→`<=` boundary, HumanEval/35's singleton list) and three
truthiness-only checks (HumanEval/52, /56, /61). Receipts: [`receipts/`](receipts/).

Findings that turned out to be harness limitations are not deleted; they are
quarantined with the evidence that explains them
([`receipts/quarantine/`](receipts/quarantine/README.md)).

<!-- UPSTREAM -->

## Limitations

<!-- LIMITATIONS -->

## Repository map

```
src/breakwater/
  cli.py                  breakwater CLI
  adapters/harbor.py      Harbor-faithful Docker runtime (solve / collect / verify)
  adapters/harbor_task.py task.toml semantics, content digests, support classification
  bundle.py               content-addressed artifact bundles (Harbor upload normalization)
  trustdiff/              corpus, engine, statistics, versions, reports + receipts
  db.py  schema.sql       queue: fair claims, leases + fencing, ledger, provenance
  worker.py  report.py    workers; SQL invariant verification
  tester/                 single-version falsification audit + receipts
  adapters/humaneval.py tbench.py   other benchmark formats
reports/                  every result quoted in this README, with raw data
scripts/conformance.py    differential conformance sweep vs official Harbor
fixtures/                 hand-written Trust Diff inputs
```

MIT licensed.
