# Reports

| path | what | runtime |
|---|---|---|
| `trustdiff/tb-v4.0.0-vs-main/` | Verifier Trust Diff, Terminal-Bench v4.0.0 vs main (06a92e79) | v2 (Harbor-faithful) |
| `known_findings/` | overlap index of every public TB 4.x verifier finding (Tokenless, Preference Model, northset, GitHub issues) | — |
| `mut_run_20260910/` | first reference-ablation sweep of TB v4.0.0 | v1 — see `scripts/legacy/README.md` for its fidelity gaps |
| `harbor_v4_audit_final.md`, `harbor_audit_support/` | single-version falsification audit of 10 TB v4.0.0 tasks | v1 |
| `demo_report.json`, `reliability_report.json`, `audit_*.json` | chaos demo, rule-based reliability probes, built-in suite / HumanEval / classic TB audits | queue + built-in runner |

Reports produced by the v1 Harbor runtime are kept as historical records; where
the v2 runtime re-measured the same thing, the v2 result supersedes them.
