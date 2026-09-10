"""HumanEval/MBPP-style adapter: value semantics, pure conversion.

Mapping:
  oracle   = prompt + canonical_solution + a solve(args) wrapper
  cases    = the benchmark's own asserts, parsed from `def check(candidate)`;
             case input = the literal argument list of the single candidate()
             call inside the assert
  verifier = replays the ORIGINAL assert source with candidate() substituted
             by a function returning the recorded output — the benchmark's own
             comparison semantics (==, abs()<eps, sorted(), ...) run verbatim.
             Any exception scores as FAIL, matching the official harness.
             Tester-generated synthetic cases (unknown inputs) fall back to
             exact match against the tester-computed expected value.

Conversion policy (reported, never silent): a task converts only if every
top-level assert in check() contains exactly one candidate(...) call with
literal, JSON-safe args, and the translated oracle passes the translated
verifier locally (drops conversion artifacts, so the audit never blames the
benchmark for our translation).
"""
import ast
import gzip
import json

VERIFIER_TEMPLATE = '''\
import json as _json
_ASSERTS = _PLACEHOLDER_
FAIL_ON_SOLUTION_ERROR = True   # official harness: candidate exception = fail

def check(inp, out, expected):
    key = _json.dumps(inp, sort_keys=True)
    src = _ASSERTS.get(key)
    if src is None:
        ok = out == expected
        return ok, ('synthetic case (exact-match fallback): '
                    + ('ok' if ok else f'expected {expected!r}, got {out!r}'))
    def candidate(*a, **k):
        return out
    try:
        exec(src, {'candidate': candidate})
        return True, 'benchmark assert passed'
    except AssertionError as e:
        return False, f'benchmark assert failed: {e}'
    except Exception as e:
        return False, f'benchmark assert raised {type(e).__name__}: {e}'
'''


def _parse_asserts(test_src: str) -> tuple[list, list]:
    """-> ([(args_list, assert_source)], skip_reasons)"""
    tree = ast.parse(test_src)
    check_fn = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "check"), None)
    if check_fn is None:
        return [], ["no check() function"]
    out, skipped = [], []
    for stmt in check_fn.body:
        if not isinstance(stmt, ast.Assert):
            continue
        calls = [n for n in ast.walk(stmt)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "candidate"]
        if len(calls) != 1:
            skipped.append(f"{len(calls)} candidate calls")
            continue
        call = calls[0]
        if call.keywords:
            skipped.append("kwargs")
            continue
        try:
            args = [ast.literal_eval(a) for a in call.args]
            json.dumps(args)
        except (ValueError, TypeError):
            skipped.append("non-literal args")
            continue
        out.append((args, ast.unparse(stmt)))
    return out, skipped


def _validate_locally(oracle_src: str, verifier_src: str, cases: list) -> bool:
    """Translated oracle must pass the translated verifier (conversion check)."""
    try:
        s_ns, v_ns = {}, {}
        exec(oracle_src, s_ns)
        exec(verifier_src, v_ns)
        for c in cases:
            out = s_ns["solve"](json.loads(json.dumps(c["input"])))
            out = json.loads(json.dumps(out))
            ok, _ = v_ns["check"](c["input"], out, None)
            if not ok:
                return False
        return True
    except Exception:
        return False


def load(path: str, ids: list[str] | None = None, limit: int | None = None) -> dict:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        problems = [json.loads(line) for line in f if line.strip()]
    if ids:
        problems = [p for p in problems if p["task_id"] in ids]

    tasks, stats = {}, {"converted": 0, "no_asserts": 0, "unfaithful": 0,
                        "asserts_skipped": 0}
    for p in problems:
        if limit and stats["converted"] >= limit:
            break
        parsed, skipped = _parse_asserts(p["test"])
        stats["asserts_skipped"] += len(skipped)
        if not parsed:
            stats["no_asserts"] += 1
            continue
        oracle = (p["prompt"] + p["canonical_solution"]
                  + f"\n\ndef solve(inp):\n    return {p['entry_point']}(*inp)\n")
        asserts_map = {json.dumps(args, sort_keys=True): src for args, src in parsed}
        verifier = VERIFIER_TEMPLATE.replace("_PLACEHOLDER_", repr(asserts_map))
        cases = [{"input": args, "expected": None} for args, _ in parsed]
        if not _validate_locally(oracle, verifier, cases):
            stats["unfaithful"] += 1
            continue
        tid = "he_" + p["task_id"].replace("/", "_")
        tasks[tid] = {
            "cases": cases, "verifier": verifier, "oracle": oracle, "agent": oracle,
            "source": {"benchmark": "humaneval", "task_id": p["task_id"],
                       "entry_point": p["entry_point"], "path": path},
        }
        stats["converted"] += 1
    print(f"[humaneval] {stats['converted']} converted, "
          f"{stats['no_asserts']} skipped (no parseable asserts), "
          f"{stats['unfaithful']} dropped (conversion validation failed), "
          f"{stats['asserts_skipped']} individual asserts skipped")
    return tasks
