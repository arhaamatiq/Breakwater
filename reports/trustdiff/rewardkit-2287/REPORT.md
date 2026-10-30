# Verifier Trust Diff

- **base** `v4.0.0` (452bf305c6da)
- **head** `origin/main` (06a92e790027)
- repeats 2 per version, contested bundles topped up to 5; outputs solved under `base`; network: task policy
- run `td-20261030-015848-f10b` generated 2026-10-30T02:33:09-0700

Every bundle is a fixed set of artifact bytes. Both verifier versions judge the same bytes; the verdict is the task's reward exactly as Harbor records it.

## Summary

| task | bundles | reference | changes | notes |
|---|---:|---|---|---|
| gsea-proteomics | 0 | — | none | superseded: on this arm64 host the task needs linux/amd64 (x86-64-only binaries); re-measured in ../gsea-proteomics-amd6 |
| foodstuff-beta-activity | 5 | PASS→PASS | none |  |
| roy-polymorph-cn | 5 | PASS→PASS | none |  |
| hof-topology-interpenetration | 5 | PASS→PASS | none |  |
| protein-autointerp-disulfide | 5 | PASS→PASS | none |  |
| fin-saccr-rwa | 6 | PASS→PASS | none |  |

## Changes

No bundle changed verdict between versions.
