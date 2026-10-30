# PR #1965 (biped jump transition margin): differential evidence

[harbor-framework/terminal-bench#1965](https://github.com/harbor-framework/terminal-bench/pull/1965)
changes one line of `biped-contact-dynamics/tests/checks.py`: the jump's
flight-clearance check excludes 5 samples at each flight-phase edge instead of
3, matching the 5-sample transition margin the instruction documents
(issue [#1934](https://github.com/harbor-framework/terminal-bench/issues/1934)).

Question a reviewer needs answered: **does the change move exactly that verdict,
and nothing else?**

## Setup

`breakwater diff`, base = `main` @ `06a92e79` (task 2.1.0), head = PR head
@ `90305d7b` (task 2.2.0). All outputs solved once under the base environment;
each bundle judged ≥3× per version, interleaved; contested bundles topped up
to 5×.

Corpus:

- the reference solution's output;
- a no-op and four degenerate outputs (all artifacts deleted / emptied /
  truncated / digit-rotated);
- one **constructed candidate solution**
  ([`fixtures/biped-contact-dynamics/feet-low-inside-5-sample-margin.patch`](../../fixtures/biped-contact-dynamics/feet-low-inside-5-sample-margin.patch)):
  the reference generator, except that during flight samples 3–4 after
  takeoff and before landing the whole body is lowered so both feet sit at
  4 cm — inside the instruction's 5-sample margin, above 6 cm everywhere else.
  Because the verifier re-runs the submitted generator on hidden configs, the
  fixture is a patch to the *solution*, solved once and then judged.

## Result

| bundle | main (3-sample trim) | PR head (5-sample trim) | class |
|---|---|---|---|
| reference | pass 3/3 | pass 3/3 | unchanged |
| no-op | fail 3/3 | fail 3/3 | unchanged |
| drop_all / zero_all / halve_all / digits_all | fail 3/3 each | fail 3/3 each | unchanged |
| **instruction-compliant fixture** | **fail 0/5** | **pass 5/5** | false-fail fixed, deterministic (Fisher p = 0.008) |

Checks whose pass rate moved for the fixture: `visible_trajectories_are_feasible`,
`hidden_trajectories_are_feasible:hidden_jump.json`,
`hidden_trajectories_are_feasible:hidden_stride.json` (0.00 → 1.00). No check
moved for any other bundle.

So, on this corpus, the PR changes precisely one behaviour — a trajectory that
honours the documented margin is no longer rejected — and leaves the reference,
no-op and degenerate verdicts untouched.

## Reproduce

```bash
git clone https://github.com/harbor-framework/terminal-bench tb
git -C tb fetch origin pull/1965/head:pr-1965
breakwater up
breakwater diff --repo tb --base 06a92e790027045921482f2acd23f3fa598ffe38 --head pr-1965 \
  --tasks biped-contact-dynamics --corpus reference,noop,degenerate \
  --solution-fixture "biped-contact-dynamics:feet-low-inside-5-sample-margin:pass:fixtures/biped-contact-dynamics/feet-low-inside-5-sample-margin.patch" \
  --repeats 3 --max-repeats 5
```

Raw results: [`reports/trustdiff/pr-1965-biped/`](../../reports/trustdiff/pr-1965-biped/).
