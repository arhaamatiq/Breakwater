# Reports

Every number in the top-level README comes from a file in this directory.

## Trust Diff runs (`trustdiff/`, Harbor-faithful runtime)

| run | question | versions |
|---|---|---|
| `SUMMARY.md` | all runs below, aggregated | — |
| `tb-v4.0.0-vs-main/` | did any verdict move between Terminal-Bench 4.0 and main? (mechanical corpus: reference, no-op, degenerate, path/file/AST mutants) | v4.0.0 → main `06a92e79` |
| `glycan-ms2-elucidation/`, `fin-saccr-rwa/`, `rewardkit-2287/`, `gsea-proteomics-amd64/` | RewardKit `SUBMISSION_ERRORS` allowlists (PR #2287) on malformed outputs | v4.0.0 → main |
| `batched-eval-parity/` | stop-string at offset 0 (fixture) plus full corpus | v4.0.0 → main |
| `reference-changes/` | tasks whose reference solution changed (music-harmony, telecom-entity-resolution) | v4.0.0 → main |
| `pr-1965-biped/` | does PR #1965 move exactly the documented verdict? | main → PR head |
| `pr-2300-margins/`, `pr-2300-ks/` | PR #2300's effect on measured margins (VF2 speedup, KS relative MSE) on identical bytes | pre-#2300 → #2300 |
| `vf2-idle/` | VF2 speedup across pre-#2300 / #2300 / main on an otherwise idle host | three versions |
| `v1-rejudge/` | the September v1-runtime "survivors", re-judged under the faithful runtime | v4.0.0 |

Each run directory holds `REPORT.md`, `trustdiff.json` (every cell's
verdict, per-check vectors, margins, wall times), `receipts/` and the exact
`bundles/` behind every reported change.

## Other evidence

| path | what |
|---|---|
| `conformance/` | Breakwater vs official Harbor (`harbor run` / `harbor trial regrade`) on identical bytes |
| `known_findings/` | overlap index of every public TB 4.x verifier finding (Tokenless, Preference Model, northset, GitHub issues) |
| `chaos_demo_report.json` | queue under SIGKILL + SIGSTOP/SIGCONT: 136/136, 0 lost, 0 duplicate, 0 safety violations |
| `mut_run_20260910/`, `harbor_v4_audit_final.md`, `harbor_audit_support/` | September runs on the v1 runtime — see `scripts/legacy/README.md` for its fidelity gaps; superseded where re-measured |
| `demo_report.json`, `reliability_report.json`, `audit_*.json` | v1 chaos demo, rule-based reliability probes, built-in suite / HumanEval / classic TB audits |
