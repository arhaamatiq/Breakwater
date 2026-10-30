# Legacy scripts (v1 Harbor runtime)

These produced `reports/mut_run_20260910/` and `reports/harbor_audit_support/`
against the **v1** Harbor runtime, which differed from the official harness in
ways that matter for verdicts:

- solves always ran offline (Harbor's default network is public), so 11 tasks
  were wrongly classed unrunnable and some references failed for lack of PyPI;
- every timeout was clamped to 300 s;
- declared artifact directories were overlaid instead of emptied before upload,
  so a deleted artifact that also existed in the verifier image still looked
  present (this inflates `drop_file` / `drop_path` survival);
- file modes were not preserved; verdicts were taken from CTRF test names, not
  the reward file Harbor records.

They are kept for provenance only and no longer run against the current
runtime. Their replacement is `breakwater diff` (`src/breakwater/trustdiff/`),
which re-measures the same ablation operators through the faithful runtime.
