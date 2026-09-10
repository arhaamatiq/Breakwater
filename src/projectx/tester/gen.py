"""Local instruments for cross-examination: sandboxed solution evaluation,
type/shape-driven input generation, divergence search, and witness shrinking.

Local (out-of-container) evaluation is used ONLY to search for candidate
counterexample inputs quickly; every witness that ends up in a receipt is
re-validated through the real Docker pipeline."""
import json
import random
import signal
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutTimeout


# ------------------------------------------------------------- sandbox eval

def _child_eval(src: str, inputs: list, per_timeout: float) -> list:
    def _alarm(sig, frame):
        raise TimeoutError("per-input timeout")
    results = []
    try:
        ns = {}
        exec(src, ns)
        solve = ns["solve"]
    except Exception as e:
        return [{"error": f"compile: {e}"} for _ in inputs]
    old = signal.signal(signal.SIGALRM, _alarm)
    for inp in inputs:
        try:
            signal.setitimer(signal.ITIMER_REAL, per_timeout)
            out = solve(json.loads(json.dumps(inp)))
            signal.setitimer(signal.ITIMER_REAL, 0)
            out = json.loads(json.dumps(out))  # normalize like the wire format
            results.append({"output": out})
        except Exception as e:
            signal.setitimer(signal.ITIMER_REAL, 0)
            results.append({"error": f"{type(e).__name__}: {e}"[:200]})
    signal.signal(signal.SIGALRM, old)
    return results


class Sandbox:
    """Batched subprocess evaluator; survives hangs by recycling the pool."""

    def __init__(self, per_timeout: float = 0.25):
        self.per_timeout = per_timeout
        self._pool = None

    def _ensure(self):
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=1)

    def eval(self, src: str, inputs: list) -> list:
        if not inputs:
            return []
        self._ensure()
        budget = 3.0 + self.per_timeout * len(inputs) * 1.5
        try:
            return self._pool.submit(_child_eval, src, inputs, self.per_timeout) \
                             .result(timeout=budget)
        except (FutTimeout, Exception):
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
            return [{"error": "sandbox timeout/crash"} for _ in inputs]

    def close(self):
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None


def norm_equal(a, b, atol: float | None = None) -> bool:
    """Equality as a Python == verifier would see it (0==False, 1==1.0), with
    optional float tolerance. Anything stricter than this would let the tester
    claim "wrong output accepted" for outputs the language itself calls equal."""
    if isinstance(a, (bool, int, float)) and isinstance(b, (bool, int, float)):
        if atol is not None and not (isinstance(a, bool) or isinstance(b, bool)):
            return abs(a - b) <= atol
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(norm_equal(x, y, atol) for x, y in zip(a, b))
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ------------------------------------------------------- input generation

INT_POOL = [0, 1, -1, 2, 3, 5, 7, 10, -3, 100]


def _gen_scalar(example, rng):
    if isinstance(example, bool):
        return rng.choice([True, False])
    if isinstance(example, int):
        return rng.choice(INT_POOL + [rng.randint(-50, 50)])
    if isinstance(example, float):
        return rng.choice([0.0, 1.0, -1.5, 0.1, 2.5, rng.uniform(-10, 10)])
    if isinstance(example, str):
        alphabet = sorted(set(example)) or list("abc")
        n = rng.choice([0, 1, 2, 3, 4, 6])
        return "".join(rng.choice(alphabet + list("abxy")) for _ in range(n))
    return example


def _tuple_like(observed: list) -> bool:
    """Fixed-arity heterogeneous lists ([[1,3,5],2]-style args)."""
    if not all(isinstance(o, list) and len(o) == len(observed[0]) and o for o in observed):
        return False
    kinds = [{type(o[i]).__name__ for o in observed} for i in range(len(observed[0]))]
    return len(observed[0]) <= 3 and any(len(k) == 1 for k in kinds) and \
        len({json.dumps(sorted(k)) for k in kinds}) > 1


def _generate_from(example, observed, rng, depth=0):
    if isinstance(example, list):
        if observed and _tuple_like(observed):
            return [_generate_from(example[i], [o[i] for o in observed], rng, depth + 1)
                    for i in range(len(example))]
        elem = example[0] if example else (observed_elem(observed) if observed else 0)
        n = rng.choice([0, 1, 2, 3, 4, 5, 7])
        return [_generate_from(elem, None, rng, depth + 1) for _ in range(n)]
    return _gen_scalar(example, rng)


def observed_elem(observed):
    for o in observed or []:
        if isinstance(o, list) and o:
            return o[0]
    return 0


def _mutate_input(inp, rng):
    v = json.loads(json.dumps(inp))
    if isinstance(v, list) and v:
        op = rng.choice(["tweak", "drop", "dup", "swap", "append", "reverse"])
        i = rng.randrange(len(v))
        if op == "tweak":
            v[i] = _generate_from(v[i], None, rng)
        elif op == "drop":
            v.pop(i)
        elif op == "dup":
            v.insert(i, json.loads(json.dumps(v[i])))
        elif op == "swap" and len(v) > 1:
            j = rng.randrange(len(v))
            v[i], v[j] = v[j], v[i]
        elif op == "append":
            v.append(_generate_from(v[i], None, rng))
        else:
            v.reverse()
        return v
    return _gen_scalar(v, rng) if not isinstance(v, list) else v


def _repair(inp, invariants: list[str]):
    v = json.loads(json.dumps(inp))
    for inv in invariants:
        if inv == "arg0_sorted" and isinstance(v, list) and v \
                and isinstance(v[0], list):
            try:
                v[0] = sorted(v[0])
            except TypeError:
                pass
    return v


def generate_inputs(observed: list, n: int, invariants: list[str], seed: int = 7) -> list:
    """Candidate inputs: mutations of observed cases + fresh shape-driven ones.
    Declared invariants are always enforced (repair)."""
    rng = random.Random(seed)
    out, seen = [], {json.dumps(o, sort_keys=True) for o in observed}
    attempts = 0
    while len(out) < n and attempts < n * 20:
        attempts += 1
        base = rng.choice(observed) if observed and rng.random() < 0.55 else None
        cand = _mutate_input(base, rng) if base is not None else \
            _generate_from(rng.choice(observed), observed, rng) if observed else None
        if cand is None:
            break
        cand = _repair(cand, invariants)
        key = json.dumps(cand, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out


# ------------------------------------------------------- divergence + shrink

def find_divergence(sandbox: Sandbox, oracle_src: str, mutant_src: str,
                    candidates: list, atol: float | None = None,
                    batch: int = 40) -> dict | None:
    """First input where mutant's behavior deviates from the reference:
    different output, or mutant crashes/hangs where the reference succeeds."""
    for i in range(0, len(candidates), batch):
        chunk = candidates[i:i + batch]
        ref = sandbox.eval(oracle_src, chunk)
        mut = sandbox.eval(mutant_src, chunk)
        for inp, r, m in zip(chunk, ref, mut):
            if "error" in r:
                continue  # input out of the reference's domain — not evidence
            if "error" in m:
                return {"input": inp, "kind": "mutant_crash",
                        "reference_output": r["output"], "mutant_error": m["error"]}
            if not norm_equal(r["output"], m["output"], atol):
                return {"input": inp, "kind": "output_divergence",
                        "reference_output": r["output"], "mutant_output": m["output"]}
    return None


def _shrink_candidates(v):
    if isinstance(v, list) and v:
        n = len(v)
        yield []
        if n > 1:
            yield v[:n // 2]
            yield v[n // 2:]
        for i in range(n):
            yield v[:i] + v[i + 1:]
        for i, e in enumerate(v):
            for se in list(_shrink_candidates(e))[:3]:
                yield v[:i] + [se] + v[i + 1:]
    elif isinstance(v, bool):
        return
    elif isinstance(v, int):
        for c in (0, v // 2, v - (1 if v > 0 else -1)):
            if c != v:
                yield c
    elif isinstance(v, float):
        for c in (0.0, round(v, 1), float(int(v))):
            if c != v:
                yield c
    elif isinstance(v, str) and v:
        yield ""
        yield v[:len(v) // 2]
        yield v[1:]
        yield v[:-1]


def shrink_input(inp, still_fails, invariants: list[str], budget: int = 150):
    """Greedy structural shrink: keep applying the first simplification that
    still reproduces (per still_fails), until fixpoint or budget."""
    current, spent = inp, 0
    improved = True
    while improved and spent < budget:
        improved = False
        for cand in _shrink_candidates(current):
            cand = _repair(cand, invariants)
            if json.dumps(cand, sort_keys=True) == json.dumps(current, sort_keys=True):
                continue
            spent += 1
            if spent >= budget:
                break
            if still_fails(cand):
                current = cand
                improved = True
                break
    return current
