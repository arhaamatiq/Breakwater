# Verifier Trust Diff

- **base** `v4.0.0` (452bf305c6da)
- **head** `origin/main` (06a92e790027)
- repeats 2 per version, contested bundles topped up to 5; outputs solved under `base`; network: task policy
- run `td-20261029-233007-83b0` generated 2026-10-29T23:32:12-0700

Every bundle is a fixed set of artifact bytes. Both verifier versions judge the same bytes; the verdict is the task's reward exactly as Harbor records it.

## Summary

| task | bundles | reference | changes | notes |
|---|---:|---|---|---|
| sound-change-cascade | 7 | PASS→PASS | none |  |

## Changes

No bundle changed verdict between versions.
## Check inventory changes (static)

Names discovered in each version's tests (pytest ids, RewardKit criteria). A rename is not a weakening by itself; use the bundle results above.

- **sound-change-cascade**: 7→7 checks; removed 7, added 7
