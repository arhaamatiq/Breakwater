"""Docker-isolated trial execution, v2: split solve/verify containers.

Why two containers: in v1 the solution and verifier shared /work, so the
expected answers were readable by the solution — the benchmark was gameable by
construction (a real bug the tester caught; see leaky_lookup for the planted
regression case). In v2 the solve container only ever contains the case
INPUTS and the solution; expected answers and the verifier exist solely in a
second container that runs after the outputs have been extracted.

The split also gives the tester its instruments:
  - mode="verify_only": re-run verification on FIXED outputs (isolates the
    verifier from the solution when attributing nondeterminism)
  - spec.env: environment interventions (PYTHONHASHSEED, TZ, ...) per phase
  - spec.cases: custom case lists (counterexample validation, isolation probes)
  - per-output "transform" tags: equivalence perturbations applied in-container

Trial spec fields (all optional): solution ("agent"|"oracle"|"noop"),
solution_source (inline code, wins over solution), mode ("full"|"verify_only"),
outputs (for verify_only), cases ([{input, expected}]), env ({}).

All docker-py calls are sync; workers call run_trial via asyncio.to_thread.
"""
import io
import json
import os
import tarfile
import time

import docker
from docker.errors import DockerException

from .config import CFG
from . import storage
from .adapters import base as adapters_base
from .benchmarks.suite import TASKS

SOLVE_HARNESS = """\
import json, sys, traceback

def main():
    sys.path.insert(0, '/work')
    cases = json.load(open('/work/task_public.json'))['cases']  # inputs only
    import solution
    outputs = []
    for i, case in enumerate(cases):
        try:
            out = solution.solve(case['input'])
            try:
                json.dumps(out)
                outputs.append({'output': out})
            except (TypeError, ValueError):
                outputs.append({'output': None, 'output_repr': repr(out)[:500],
                                'unserializable': True})
        except Exception:
            outputs.append({'error': traceback.format_exc(limit=2)[:800]})
        print(f"case {i}: solved" if 'error' not in outputs[-1] else f"case {i}: raised")
    with open('/work/outputs.json', 'w') as f:
        json.dump(outputs, f)

main()
"""

VERIFY_HARNESS = """\
import json, sys, traceback, unicodedata

def apply_transform(v, t):
    if t == 'as_tuple' and isinstance(v, list):
        return tuple(v)
    if t == 'append_newline' and isinstance(v, str):
        return v + '\\n'
    if t == 'float_cast' and isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    if t == 'unicode_nfd' and isinstance(v, str):
        return unicodedata.normalize('NFD', v)
    if t.startswith('float_nudge:') and isinstance(v, (int, float)) and not isinstance(v, bool):
        return v + float(t.split(':', 1)[1])
    return v

def main():
    sys.path.insert(0, '/work')
    cases = json.load(open('/work/task_private.json'))['cases']  # inputs + expected
    outputs = json.load(open('/work/outputs.json'))
    import verifier
    verdicts, any_error = [], False
    for i, case in enumerate(cases):
        entry = outputs[i] if i < len(outputs) else {'error': 'missing output'}
        out = entry.get('output')
        if 'transform' in entry:
            out = apply_transform(out, entry['transform'])
        solution_error = entry.get('error')
        try:
            if solution_error and getattr(verifier, 'FAIL_ON_SOLUTION_ERROR', False):
                ok, detail = False, 'solution raised: ' + solution_error.strip()[-160:]
            else:
                ok, detail = verifier.check(case['input'], out, case.get('expected'))
            verdicts.append({'case': i, 'ok': bool(ok), 'detail': str(detail)[:500],
                             'solution_error': solution_error})
        except Exception:
            any_error = True
            verdicts.append({'case': i, 'ok': False, 'verifier_error':
                             traceback.format_exc(limit=2)[:800],
                             'solution_error': solution_error})
        v = verdicts[-1]
        print(f"case {i}: " + ('ERROR' if 'verifier_error' in v else
                               'PASS' if v['ok'] else 'FAIL'))
    outcome = 'error' if any_error else ('pass' if all(v['ok'] for v in verdicts) else 'fail')
    with open('/work/verdicts.json', 'w') as f:
        json.dump({'outcome': outcome, 'cases': verdicts}, f)
    print('OUTCOME:', outcome)

main()
"""

# v1 combined harness, kept ONLY for tasks planted with harness="leaky":
# solution and verifier share /work and task.json includes expected answers.
LEAKY_HARNESS = """\
import json, sys, traceback

def main():
    sys.path.insert(0, '/work')
    task = json.load(open('/work/task.json'))
    import solution, verifier
    cases, outputs, all_ok = [], [], True
    for i, case in enumerate(task['cases']):
        try:
            out = solution.solve(case['input'])
            err = None
        except Exception:
            out, err = None, traceback.format_exc(limit=2)[:800]
        try:
            json.dumps(out); outputs.append({'output': out})
        except (TypeError, ValueError):
            outputs.append({'output': None, 'unserializable': True})
        try:
            ok, detail = verifier.check(case['input'], out, case.get('expected'))
        except Exception:
            ok, detail = False, 'verifier crashed'
        all_ok = all_ok and bool(ok)
        cases.append({'case': i, 'ok': bool(ok), 'detail': str(detail)[:500],
                      'solution_error': err})
        print(f"case {i}: {'PASS' if ok else 'FAIL'}")
    with open('/work/result.json', 'w') as f:
        json.dump({'outcome': 'pass' if all_ok else 'fail', 'cases': cases,
                   'outputs': outputs}, f)
    print('OUTCOME:', 'pass' if all_ok else 'fail')

main()
"""

_client = None


def _isolated_docker_config() -> str:
    """Breakwater's own (empty) Docker client config. Public benchmark images
    need no credentials, and the user's credential helper can block on a
    keychain prompt with no terminal attached — which hangs every pull and
    build silently. Point BW_DOCKER_CONFIG at a real config dir to opt in."""
    d = os.path.expanduser(os.environ.get("BW_DOCKER_CONFIG", "~/.breakwater/docker-config"))
    os.makedirs(d, exist_ok=True)
    cfg = os.path.join(d, "config.json")
    if not os.path.exists(cfg):
        with open(cfg, "w") as f:
            f.write("{}\n")
    return d


def docker_client():
    global _client
    if _client is None:
        os.environ["DOCKER_CONFIG"] = _isolated_docker_config()
        try:
            _client = docker.from_env()
            _client.ping()
        except DockerException:
            sock = os.path.expanduser("~/.docker/run/docker.sock")
            _client = docker.DockerClient(base_url=f"unix://{sock}")
            _client.ping()
    return _client


def ensure_image():
    c = docker_client()
    try:
        c.images.get(CFG.docker_image)
    except docker.errors.ImageNotFound:
        c.images.pull(CFG.docker_image)


def _tar_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _read_file_from_container(container, path: str) -> bytes | None:
    try:
        stream, _ = container.get_archive(path)
        raw = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            return tar.extractfile(tar.getmembers()[0]).read()
    except Exception:
        return None


class TrialExecutionError(Exception):
    """Infra-level failure (docker error, timeout, harness crash). Retryable."""


def _run_container(files: dict[str, str], env: dict, label: str, timeout: float,
                   fetch: str) -> tuple[bytes, bytes | None]:
    """Run one phase container; return (logs, fetched file bytes)."""
    c = docker_client()
    container = None
    try:
        container = c.containers.create(
            CFG.docker_image,
            command=["python", f"/work/{files_entry(files)}"],
            working_dir="/work",
            network_disabled=True,
            mem_limit="256m",
            nano_cpus=1_000_000_000,
            environment={k: str(v) for k, v in env.items()},
            labels={"breakwater.phase": label},
        )
        container.put_archive("/", _tar_bytes({f"work/{k}": v for k, v in files.items()}))
        container.start()
        try:
            exit_info = container.wait(timeout=timeout)
        except Exception as e:
            try:
                container.kill()
            except Exception:
                pass
            raise TrialExecutionError(f"{label}: timeout/wait failure: {e}") from e
        logs = container.logs(stdout=True, stderr=True)
        data = _read_file_from_container(container, f"/work/{fetch}")
        if exit_info.get("StatusCode", 1) != 0 or data is None:
            raise TrialExecutionError(
                f"{label}: harness failed exit={exit_info.get('StatusCode')} "
                f"logs={logs[-500:]!r}")
        return logs, data
    except TrialExecutionError:
        raise
    except DockerException as e:
        raise TrialExecutionError(f"{label}: docker error: {e}") from e
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass


def files_entry(files: dict) -> str:
    for name in ("solve_harness.py", "verify_harness.py", "harness.py"):
        if name in files:
            return name
    raise ValueError("no harness in file set")


def run_trial(trial: dict) -> dict:
    """Execute one trial. Returns {outcome, detail, exec_ms, artifacts}.
    Raises TrialExecutionError on infra failure (retried by the worker)."""
    spec = trial.get("spec") or {}
    task_id = trial["task_id"]
    # adapter tasks travel inside the trial spec, so workers stay adapter-blind
    task = spec.get("task_blob") or TASKS[task_id]
    runtime = adapters_base.runtime_for(task)
    if runtime is not None:
        return runtime.run_trial(trial, task)
    cases = spec.get("cases") or task["cases"]
    env = spec.get("env") or {}
    mode = spec.get("mode", "full")
    src = spec.get("solution_source") or adapters_base.solution_from(
        task, spec.get("solution", "agent"))
    harness = task.get("harness", "split")

    t0 = time.monotonic()
    uploads = {}

    if harness == "leaky" and mode == "full":
        logs, raw = _run_container(
            {"harness.py": LEAKY_HARNESS, "solution.py": src, "verifier.py": task["verifier"],
             "task.json": json.dumps({"task_id": task_id, "cases": cases})},
            env, "leaky", CFG.trial_timeout, "result.json")
        result = json.loads(raw)
        outputs = result.get("outputs", [])
        verdicts = {"outcome": result["outcome"], "cases": result["cases"]}
        uploads["combined.log"] = logs
    else:
        if mode == "full":
            public = json.dumps({"cases": [{"input": c["input"]} for c in cases]})
            solve_logs, raw_out = _run_container(
                {"solve_harness.py": SOLVE_HARNESS, "solution.py": src,
                 "task_public.json": public},
                env, "solve", CFG.trial_timeout, "outputs.json")
            outputs = json.loads(raw_out)
            uploads["solve.log"] = solve_logs
        else:
            outputs = spec["outputs"]

        private = json.dumps({"cases": cases})
        verify_logs, raw_verdicts = _run_container(
            {"verify_harness.py": VERIFY_HARNESS, "verifier.py": task["verifier"],
             "task_private.json": private, "outputs.json": json.dumps(outputs)},
            env, "verify", CFG.trial_timeout, "verdicts.json")
        verdicts = json.loads(raw_verdicts)
        uploads["verify.log"] = verify_logs

    exec_ms = (time.monotonic() - t0) * 1000.0
    prefix = (f"{trial['experiment_id']}/{trial['trial_id'].replace('/', '_')}"
              f"/attempt-{trial.get('attempts', 0)}")
    uploads["outputs.json"] = json.dumps(outputs).encode()
    uploads["verdicts.json"] = json.dumps(verdicts).encode()
    uploads["meta.json"] = json.dumps({
        "trial_id": trial["trial_id"], "task_id": task_id, "mode": mode, "env": env,
        "harness": harness, "attempt": trial.get("attempts", 0),
        "image": CFG.docker_image, "exec_ms": exec_ms, "ts": time.time()}).encode()
    artifacts = [storage.put_bytes(f"{prefix}/{name}", data if isinstance(data, bytes)
                                   else data.encode())
                 for name, data in uploads.items()]

    detail = {"cases": verdicts["cases"],
              "outputs": json.loads(json.dumps(outputs))}  # normalized copy
    return {"outcome": verdicts["outcome"], "detail": detail,
            "exec_ms": exec_ms, "artifacts": artifacts}
