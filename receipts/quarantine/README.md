# Quarantined receipts

These receipts claim a defect in a benchmark (`REFERENCE_REJECTED`: the
benchmark's own reference solution fails its own verifier; one
`NONDETERMINISTIC_EXECUTION`). Triage on 2026-10-29 found that **none of them
is a confirmed benchmark defect**. Every one has a harness/adapter explanation
that is evidenced in the recorded logs, and for three of them the explanation
was reproduced directly (the reference passes once the harness limitation is
removed).

Result: `CONFIRMED_BENCHMARK_DEFECT` 0, `ADAPTER_BUG` 1, `ADAPTER_LIMITATION` 7,
`INFRA` 1, `UNRESOLVED` 0.

## Quarantine policy

* Every finding is a harness/adapter false positive until proven otherwise.
  A receipt leaves the live `receipts/` directory only by being **confirmed**
  (reproduced under official-harness-equivalent conditions) or stays here.
* "Official-harness-equivalent" for classic Terminal-Bench means: network on,
  the solution and then the task's own `run-tests.sh` run in the **same**
  container (no `docker commit` between them), in the task's own image.
* A quarantined receipt keeps its original evidence untouched. Triage adds
  `status: "quarantined"`, `quarantine_reason`, and a `triage` object
  (classification, mechanism, evidence, what was and was not verified, the
  load-time guard that now prevents it, and what would close it).
* `python -m breakwater.tester replay` still runs a quarantined receipt, but
  labels it and reports `HARNESS BEHAVIOUR REPRODUCED` rather than
  `VIOLATION CONFIRMED`. (The stored `repro` strings predate the
  `projectx` -> `breakwater` rename and the move; `triage.replay_cmd` has the
  current command.)
* Un-quarantine by moving the file back to `receipts/`, removing the three
  added fields, and recording the official-equivalent reproduction in the
  commit message.

## Table

| Receipt | Classification | Mechanism | Evidence | To verify further |
|---|---|---|---|---|
| `tb_acl_permissions_inheritance--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (guard added: `state_fidelity`) | The adapter hands state from solve to verify with `docker commit`, which drops POSIX ACLs and `user.*` xattrs. The reference's whole effect is `setfacl`; mode/group/setgid survive, ACL entries do not. | **Reproduced.** Cached task image: adapter path 6 failed / 3 passed (matches the recorded log); the same oracle + pytest in one container, no commit, 9/9 passed. Probe: `setfacl` and `os.setxattr(user.*)`, commit, new container, both gone. 26/34 recorded verify logs show the missing ACL entries (the other 8 are single-test replays). | Nothing: passes 9/9 without the commit. (Dockerfile read from `docker history`; source dir not on disk.) |
| `tb_ancient_puzzle--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (multi-service; existing `multi_service` skip postdates the receipt) | The reference ends with `curl -X POST http://decryptor:8090/decrypt`, a docker-compose sidecar. One container, no network: the host does not resolve, `/app/results.txt` is never written. | 10/10 recorded solve logs `Could not resolve host: decryptor` (exit 6); 13/13 verify logs: `results.txt` missing. | Not reproduced. The compose file is not on disk, so "more than one service" is inferred from the hostname, not read. Needs `docker compose up` of client + decryptor and the official run path. |
| `tb_break_filter_js_from_html--REFERENCE_REJECTED` | **ADAPTER_BUG** (fixed in `tbench.py`) | Containers were created with docker's `NetworkDisabled` flag, which leaves `/etc/hosts` empty, so `localhost` does not resolve. selenium cannot reach its own chromedriver (`Can not connect to the Service /usr/bin/chromedriver`) and fails before any filter logic. `network_mode="none"` keeps loopback and `/etc/hosts` and still has no egress. | 10/20 recorded verify logs show that exact error (the other 10/20: an older revision that staged tests at `/px/tests`, so `/tests/filter.py` was missing). A/B through the adapter's own `_solve`/`_verify` with this receipt's reference and test files in a stand-in image (python:3.11-slim + Debian chromium 156 + chromedriver + selenium + bs4): old network config fails with the identical exception, new config passes. chromedriver starts in 0.8s with network on and fails with it disabled regardless of memory limit. | Reproduced only in a **stand-in image**; the task's Dockerfile is not on disk. Re-run in the real image. |
| `tb_cprofiling_python--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (guard: `solve_network`) | `solution.sh` starts with `pip install requests` (fails offline); its generated scripts `import requests` and download images, and the tests run them and hash the images. | 13/13 recorded solve logs: pip name-resolution failure, `No matching distribution found`. Verify: `ModuleNotFoundError: requests`; the observed image hash is exactly the hash of an empty image set (zero downloads). | Not reproduced (task dir absent). Needs network at **verify** time too (solution-produced scripts fetch URLs), which is not statically detectable. |
| `tb_csv_to_parquet--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (guard: `solve_network`) | `curl -LsSf https://astral.sh/uv/0.7.13/install.sh \| sh; uv add pandas pyarrow` at solve time. Offline: no uv, exit 127, no parquet. | **Reproduced both ways** on a freshly built image: adapter path exit 127, 2 failed; network on + same container + the task's own `run-tests.sh`: **2 passed**. 23/23 recorded solve logs show `Could not resolve host: astral.sh`. | Nothing. (arm64 only.) |
| `tb_dna_insert--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (guard: `solve_network`) | `apt-get update && apt-get install -y emboss primer3` at solve time; the verifier then shells out to `oligotm`, which only exists if that install worked. | 20/20 recorded solve logs: apt `Temporary failure resolving 'ports.ubuntu.com'` + `oligotm: command not found`; 20/20 verify logs: `FileNotFoundError: 'oligotm'`. | Not reproduced (task dir and data files absent). |
| `tb_filter_js_from_html--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (guard: `verify_network`) | `test_filter_blocks_xss` downloads its attack corpus from github.com with `urllib.request.urlretrieve` at test time; verify runs offline. (The selenium half would also have hit the `localhost` bug above.) | 24/26 recorded verify logs: `URLError ... Temporary failure in name resolution`, `1 failed, 1 passed` (the other 2 are replays of the passing test). | Not reproduced. Needs network at verify. This task also deserves a real look at whether the reference withstands the *full* downloaded corpus; nothing here suggests it does not. |
| `tb_financial_document_processor--REFERENCE_REJECTED` | **ADAPTER_LIMITATION** (guard: `solve_network`) | The reference `apt-get install`s tesseract and runs a uv inline script that must download CPython 3.13 and wheels. Offline both fail (exit 2); nothing is moved. | 9/9 recorded base solve logs show apt `Temporary failure resolving` and uv `dns error`; all 7 tests fail on missing `/app/invoices/summary.csv`. | Not reproduced (task dir absent; heavy OCR toolchain). |
| `tb_cancel_async_tasks--NONDETERMINISTIC_EXECUTION` | **INFRA** (load-induced timing; no guard possible) | Timing-based tests (SIGINT after `sleep(0.5)`, `timeout=5` around 3-second tasks) miss their windows when trials run concurrently on a shared Docker VM. | The receipt's 10 full runs map to recorded trials (`[pass, fail, fail, pass x7]`); the two failures were the slowest runs; one log shows the program's output was complete (`Task started x2, finished x2, Cleaned up x2`) yet `TimeoutExpired after 5 seconds`. All 14 recorded verify-only runs passed. The earlier 100% failures were `python: can't open file '/app/test.py'`, an adapter staging bug already fixed. | Not reproduced. Run N>=30 at `BW_WORKER_CONCURRENCY=1` on an idle host vs concurrency 3; only a failure at concurrency 1 would implicate the benchmark. The receipt is evidence-only (no replay spec). |

## What changed in the adapter

`src/breakwater/adapters/tbench.py` (tests in `tests/test_tbench_adapter.py`):

* **Fixed:** solve and verify containers use `network_mode="none"` instead of
  `network_disabled=True`. Loopback and `localhost` now work; there is still no
  egress (verified).
* **New load-time skips** (scope boundaries, reported under
  `LAST_LOAD_REPORT["skipped"]`, decided before any image build):
  * `solve_network`: the reference solution installs or fetches at solve time
    (`apt/pip/uv add/uvx/npm/cargo/go/...`, `curl`/`wget` to a non-local URL,
    `git clone` over the network). Mirrors `harbor.py`'s `_NET_INSTALL_RE` but is
    per-line, ignores comments, `--no-index`/`--offline`, and local URLs.
    Escape hatch: `BW_TBENCH_ALLOW_SOLVE_NETWORK=1`.
  * `verify_network`: test code both calls a fetching API and names a non-local,
    dotted-hostname URL.
  * `state_fidelity`: the solution uses `setfacl`/`setfattr`.
* Before this, the module docstring claimed solve-time network tasks were
  skipped, but no such code existed.

Precision/recall notes (measured on what is on disk):

* Over the 67 task dirs in the current `original-tasks/` checkout the new
  predicates skip 21 as `solve_network` (31%), plus the 3 existing
  `multi_service`; `verify_network` and `state_fidelity` fire on none. The
  skip is conservative: some of those 21 would pass offline because the image
  already provides the package (e.g. `apt-get install -y curl` with curl baked
  in), which is why the opt-out exists. Positives and negatives are pinned in
  the tests; the predicates also catch all 7 matching quarantined references.
* Not statically detectable, so still possible: verify-time network reached
  through solution-produced code (cprofiling-python), tight wall-clock windows
  (cancel-async-tasks), and a run-tests.sh that `apt-get install`s real test
  dependencies other than curl/uv.

## Caveats about this triage

* The classic task directories for **8 of the 9** receipts are not on disk
  (`external/tb-core-v011/` is empty and the `original-tasks/` checkout holds
  67 other tasks). Only `csv-to-parquet` could be rebuilt and re-run from
  source; `acl-permissions-inheritance` was re-run from its cached image.
  For the rest, the Dockerfile, `run-tests.sh`, `task.yaml` and data files were
  not inspected; the oracle, test files and dependency list came from each
  receipt's embedded `task_blob`.
* The original per-trial `solve.log` / `verify.log` artifacts were recovered
  read-only from the volume of the exited `projectx-minio` container (the
  receipts themselves record only outcomes). They are not committed.
* All reproductions ran on Docker Desktop 29.4.3 (containerd snapshotter),
  aarch64, `ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624` for
  csv-to-parquet.
* `harbor.py` and `runner.py` still use `network_disabled=True`; they were out
  of scope here. The Harbor adapter's `browser` skip ("headless browser does not
  come up under this runtime's container defaults") is plausibly the same
  empty-`/etc/hosts` bug (same flag, same symptom class; not tested against
  Harbor tasks), and `runner.py`'s HumanEval containers share the flag.
* Operational note: in this environment `docker pull` and the Python SDK's
  `images.build` hang on `docker-credential-desktop` (macOS keychain). Running
  with `DOCKER_CONFIG` pointed at a directory containing `{}` avoids it for
  public images.
