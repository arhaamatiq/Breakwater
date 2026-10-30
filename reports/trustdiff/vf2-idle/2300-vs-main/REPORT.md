# Verifier Trust Diff

- **base** `985ba26b124e6a3308607681e55c42a8d47a9340` (985ba26b124e)
- **head** `origin/main` (06a92e790027)
- repeats 8 per version, contested bundles topped up to 8; outputs solved under `base`; network: task policy
- run `td-20261030-034310-fe61` generated 2026-10-30T03:56:03-0700

Every bundle is a fixed set of artifact bytes. Both verifier versions judge the same bytes; the verdict is the task's reward exactly as Harbor records it.

## Summary

| task | bundles | reference | changes | notes |
|---|---:|---|---|---|
| vf2-speedup-networkx | 1 | FAIL→FAIL | false_fail_both×1 |  |

## Changes

### [HIGH] vf2-speedup-networkx: `reference` — false_fail_both

a known-good output fails under both versions. Bundle kind `reference`, expected `pass`.

- base: 0/8 pass · head: 0/8 pass · Fisher p = 1.0000
- metric `speedup`: head/base = 1.306 (95% CI 1.056–1.580)
- verifier wall time head/base = 4.33 (95% CI 4.25–4.42)

Replay: `breakwater replay receipts/vf2-speedup-networkx--false_fail_both--3876e667.json`

