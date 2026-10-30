# Conformance: Breakwater vs official Harbor

**23/23 verifications agree** with Harbor 0.25 on identical bytes (reward and every per-check verdict) across 5 tasks; 19 of them are failing verdicts (no-ops and mutants regraded by `harbor trial regrade`). 14 Harbor trials were not comparable (Harbor recorded no reward, e.g. regrade refused a trial with a missing declared artifact).

| task | trial | Harbor reward | Breakwater reward | checks compared | agree |
|---|---|---:|---:|---:|---|
| glycan-ms2-elucidation | glycan-ms2-elucidation__6ZoLUt5 | 1.0 | 1.0 | 12 | yes |
| glycan-ms2-elucidation | glycan-ms2-elucidation__6YqQ2Dc | 0.0 | 0.0 | 12 | yes |
| glycan-ms2-elucidation | glycan-ms2-elucidation__J4GjDxP | — (RegradeError) | — | — | n/a |
| glycan-ms2-elucidation | glycan-ms2-elucidation__Jvforpq | — (RegradeError) | — | — | n/a |
| glycan-ms2-elucidation | glycan-ms2-elucidation__NXtBK4S | — (RewardFileNotFoundError) | — | — | n/a |
| glycan-ms2-elucidation | glycan-ms2-elucidation__nUdiNNJ | 0.0 | 0.0 | 12 | yes |
| glycan-ms2-elucidation | glycan-ms2-elucidation__s6se596 | — (RewardFileNotFoundError) | — | — | n/a |
| glycan-ms2-elucidation | glycan-ms2-elucidation__zPhwqTW | — (RegradeError) | — | — | n/a |
| music-harmony | music-harmony__XEYUb9H | — (VerifierTimeoutError) | — | — | n/a |
| music-harmony | music-harmony__x75wuK6 | 0.0 | 0.0 | 13 | yes |
| music-harmony | music-harmony__56VPnWS | 0.0 | 0.0 | 13 | yes |
| music-harmony | music-harmony__diKSzJ4 | — (RegradeError) | — | — | n/a |
| music-harmony | music-harmony__qVqWVtT | 0.0 | 0.0 | 13 | yes |
| production-planning | production-planning__Z5nb9o7 | 1.0 | 1.0 | 20 | yes |
| production-planning | production-planning__cC4hrGf | 0.0 | 0.0 | 20 | yes |
| production-planning | production-planning__NZF6p4e | 0.0 | 0.0 | 20 | yes |
| production-planning | production-planning__RQJasdd | — (RegradeError) | — | — | n/a |
| production-planning | production-planning__aFHmeRL | 0.0 | 0.0 | 20 | yes |
| production-planning | production-planning__b5Bk8MX | — (RegradeError) | — | — | n/a |
| production-planning | production-planning__qToh8du | — (RegradeError) | — | — | n/a |
| production-planning | production-planning__wcKQiUz | 0.0 | 0.0 | 20 | yes |
| risk-scorer-replay | risk-scorer-replay__ZQ4nLFi | 1.0 | 1.0 | 5 | yes |
| risk-scorer-replay | risk-scorer-replay__fuUEiLa | 0.0 | 0.0 | 5 | yes |
| risk-scorer-replay | risk-scorer-replay__C7v9kBT | — (RegradeError) | — | — | n/a |
| risk-scorer-replay | risk-scorer-replay__EMBCmrP | 0.0 | 0.0 | 5 | yes |
| risk-scorer-replay | risk-scorer-replay__JastLq3 | 0.0 | 0.0 | 5 | yes |
| risk-scorer-replay | risk-scorer-replay__RWW6jkG | 0.0 | 0.0 | 5 | yes |
| risk-scorer-replay | risk-scorer-replay__aB8a7Bv | 0.0 | 0.0 | 5 | yes |
| risk-scorer-replay | risk-scorer-replay__fPVZwd3 | 0.0 | 0.0 | 5 | yes |
| sound-change-cascade | sound-change-cascade__F8YrDat | 1.0 | 1.0 | 7 | yes |
| sound-change-cascade | sound-change-cascade__czo3mZB | 0.0 | 0.0 | 7 | yes |
| sound-change-cascade | sound-change-cascade__97UQSdR | — (RegradeError) | — | — | n/a |
| sound-change-cascade | sound-change-cascade__AuP6U4m | — (RegradeError) | — | — | n/a |
| sound-change-cascade | sound-change-cascade__DxKaLyV | 0.0 | 0.0 | 7 | yes |
| sound-change-cascade | sound-change-cascade__JiauxBo | 0.0 | 0.0 | 7 | yes |
| sound-change-cascade | sound-change-cascade__iHMTnSG | 0.0 | 0.0 | 7 | yes |
| sound-change-cascade | sound-change-cascade__jz93wML | — (RegradeError) | — | — | n/a |
