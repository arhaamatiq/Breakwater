"""Mini benchmark suite. Each task = test cases + a verifier + three candidate
solutions ("agent" = the solution under evaluation, "oracle" = known-good,
"noop" = does nothing — a probe for weak verifiers).

Two tasks have PLANTED flaws (honestly labeled) so the reliability layer has
something real to catch:
  - flaky_timing:       nondeterministic verifier (random accept/reject)
  - weak_accepts_empty: verifier accepts any output, including a no-op

The agent solution for binary_search has a deliberate off-by-one bug so results
aren't a wall of PASS.
"""

NOOP_SOLUTION = "def solve(inp):\n    return None\n"

STRICT_VERIFIER = """\
def check(inp, out, expected):
    ok = out == expected
    return ok, ("ok" if ok else f"expected {expected!r}, got {out!r}")
"""

TASKS = {
    "reverse_string": {
        "cases": [{"input": s, "expected": s[::-1]} for s in ["hello", "", "a", "racecar", "Breakwater"]],
        "verifier": STRICT_VERIFIER,
        "oracle": "def solve(inp):\n    return inp[::-1]\n",
        "agent":  "def solve(inp):\n    return ''.join(reversed(inp))\n",
    },
    "sum_list": {
        "cases": [{"input": v, "expected": sum(v)} for v in [[1, 2, 3], [], [-5, 5], [10] * 50, [0]]],
        "verifier": STRICT_VERIFIER,
        "oracle": "def solve(inp):\n    return sum(inp)\n",
        "agent":  "def solve(inp):\n    t = 0\n    for x in inp:\n        t += x\n    return t\n",
    },
    "fibonacci": {
        "cases": [{"input": n, "expected": e} for n, e in [(0, 0), (1, 1), (10, 55), (20, 6765)]],
        "verifier": STRICT_VERIFIER,
        "oracle": "def solve(inp):\n    a, b = 0, 1\n    for _ in range(inp):\n        a, b = b, a + b\n    return a\n",
        "agent":  "def solve(inp):\n    a, b = 0, 1\n    for _ in range(inp):\n        a, b = b, a + b\n    return a\n",
    },
    "dedupe_preserve_order": {
        "cases": [{"input": v, "expected": list(dict.fromkeys(v))}
                  for v in [[1, 1, 2, 3, 2], [], [5, 5, 5], ["a", "b", "a"]]],
        "verifier": STRICT_VERIFIER,
        "oracle": "def solve(inp):\n    return list(dict.fromkeys(inp))\n",
        "agent":  "def solve(inp):\n    seen = set()\n    out = []\n    for x in inp:\n        if x not in seen:\n            seen.add(x)\n            out.append(x)\n    return out\n",
    },
    "anagram_check": {
        "cases": [{"input": p, "expected": sorted(p[0]) == sorted(p[1])}
                  for p in [["listen", "silent"], ["hello", "world"], ["", ""], ["ab", "abb"]]],
        "verifier": STRICT_VERIFIER,
        "oracle": "def solve(inp):\n    return sorted(inp[0]) == sorted(inp[1])\n",
        "agent":  "def solve(inp):\n    from collections import Counter\n    return Counter(inp[0]) == Counter(inp[1])\n",
    },
    "matrix_transpose": {
        "cases": [{"input": m, "expected": [list(r) for r in zip(*m)]}
                  for m in [[[1, 2], [3, 4]], [[1, 2, 3]], [[1], [2], [3]]]],
        "verifier": STRICT_VERIFIER,
        "oracle": "def solve(inp):\n    return [list(r) for r in zip(*inp)]\n",
        "agent":  "def solve(inp):\n    return [list(r) for r in zip(*inp)]\n",
    },
    "run_length_encode": {
        "cases": [{"input": "aaabbc", "expected": [["a", 3], ["b", 2], ["c", 1]]},
                  {"input": "", "expected": []},
                  {"input": "abc", "expected": [["a", 1], ["b", 1], ["c", 1]]}],
        "verifier": STRICT_VERIFIER,
        "oracle": ("def solve(inp):\n    from itertools import groupby\n"
                   "    return [[k, len(list(g))] for k, g in groupby(inp)]\n"),
        "agent":  ("def solve(inp):\n    out = []\n    for ch in inp:\n"
                   "        if out and out[-1][0] == ch:\n            out[-1][1] += 1\n"
                   "        else:\n            out.append([ch, 1])\n    return out\n"),
    },
    "balanced_brackets": {
        "cases": [{"input": s, "expected": e}
                  for s, e in [("()[]{}", True), ("([)]", False), ("", True), ("(((", False)]],
        "verifier": STRICT_VERIFIER,
        "oracle": ("def solve(inp):\n    pairs = {')': '(', ']': '[', '}': '{'}\n    st = []\n"
                   "    for c in inp:\n        if c in '([{':\n            st.append(c)\n"
                   "        elif c in pairs:\n            if not st or st.pop() != pairs[c]:\n"
                   "                return False\n    return not st\n"),
        "agent":  ("def solve(inp):\n    pairs = {')': '(', ']': '[', '}': '{'}\n    st = []\n"
                   "    for c in inp:\n        if c in '([{':\n            st.append(c)\n"
                   "        elif c in pairs:\n            if not st or st.pop() != pairs[c]:\n"
                   "                return False\n    return not st\n"),
    },
    "word_count_top2": {
        "cases": [{"input": "a b a c a b", "expected": [["a", 3], ["b", 2]]},
                  {"input": "x", "expected": [["x", 1]]}],
        "verifier": STRICT_VERIFIER,
        "oracle": ("def solve(inp):\n    from collections import Counter\n"
                   "    return [[w, n] for w, n in Counter(inp.split()).most_common(2)]\n"),
        "agent":  ("def solve(inp):\n    from collections import Counter\n"
                   "    return [[w, n] for w, n in Counter(inp.split()).most_common(2)]\n"),
    },
    "binary_search": {
        "cases": [{"input": [[1, 3, 5, 7, 9], t], "expected": e}
                  for t, e in [(5, 2), (1, 0), (9, 4), (4, -1)]],
        "verifier": STRICT_VERIFIER,
        "oracle": ("def solve(inp):\n    arr, target = inp\n    lo, hi = 0, len(arr) - 1\n"
                   "    while lo <= hi:\n        mid = (lo + hi) // 2\n"
                   "        if arr[mid] == target:\n            return mid\n"
                   "        if arr[mid] < target:\n            lo = mid + 1\n"
                   "        else:\n            hi = mid - 1\n    return -1\n"),
        # deliberate off-by-one: agent fails this task deterministically
        "agent":  ("def solve(inp):\n    arr, target = inp\n    lo, hi = 0, len(arr) - 1\n"
                   "    while lo < hi:\n        mid = (lo + hi) // 2\n"
                   "        if arr[mid] == target:\n            return mid\n"
                   "        if arr[mid] < target:\n            lo = mid + 1\n"
                   "        else:\n            hi = mid - 1\n    return -1\n"),
    },
    # ---- planted flaw #1: nondeterministic verifier ----
    "flaky_timing": {
        "planted_flaw": "nondeterministic_verifier",
        "cases": [{"input": 42, "expected": 42}],
        "verifier": ("import random\n"
                     "def check(inp, out, expected):\n"
                     "    # PLANTED FLAW: verdict depends on RNG, not on the output\n"
                     "    if out != expected:\n        return False, 'wrong output'\n"
                     "    ok = random.random() > 0.35\n"
                     "    return ok, ('ok' if ok else 'flaky rejection')\n"),
        "oracle": "def solve(inp):\n    return inp\n",
        "agent":  "def solve(inp):\n    return inp\n",
    },
    # ---- planted flaw #2: weak verifier (accepts anything) ----
    "weak_accepts_empty": {
        "planted_flaw": "weak_verifier",
        "cases": [{"input": [3, 1, 2], "expected": [1, 2, 3]}],
        "verifier": ("def check(inp, out, expected):\n"
                     "    # PLANTED FLAW: never inspects the output\n"
                     "    return True, 'accepted'\n"),
        "oracle": "def solve(inp):\n    return sorted(inp)\n",
        "agent":  "def solve(inp):\n    return sorted(inp)\n",
    },
}


def solution_source(task_id: str, kind: str) -> str:
    if kind == "noop":
        return NOOP_SOLUTION
    return TASKS[task_id][kind]

# --------------------------------------------------------------------------
# Adversarial planted tasks for the tester core. Each models a REAL benchmark
# failure class, and each is invisible to the naive oracle-x5/noop layer.
# Optional task fields the tester understands:
#   harness: "leaky"          -> old single-container harness (expected visible
#                                to the solution) instead of the split harness
#   equivalence: {...}        -> semantics the benchmark itself declares
#   invariants: [...]         -> declared input invariants for generated inputs
#   planted_flaw: str         -> ground-truth label (for tester calibration)

TASKS["verifier_fatigue"] = {
    # Stateful verifier: accepts ANYTHING from the 3rd check() call onward.
    # Oracle passes, noop still fails case 0 -> old layer sees nothing wrong.
    "planted_flaw": "stateful_verifier",
    "cases": [{"input": [i, i + 1], "expected": i + i + 1} for i in range(4)],
    "verifier": ("_calls = []\n"
                 "def check(inp, out, expected):\n"
                 "    _calls.append(1)\n"
                 "    if len(_calls) >= 3:\n"
                 "        return True, 'accepted'   # PLANTED: fatigue after 2 checks\n"
                 "    ok = out == expected\n"
                 "    return ok, ('ok' if ok else f'expected {expected!r}, got {out!r}')\n"),
    "oracle": "def solve(inp):\n    return inp[0] + inp[1]\n",
    "agent":  "def solve(inp):\n    return inp[0] + inp[1]\n",
}

TASKS["env_hash_order"] = {
    # Verdict depends on the interpreter's string-hash seed. Looks like random
    # flakiness; the tester should ATTRIBUTE it to PYTHONHASHSEED by pinning.
    "planted_flaw": "env_sensitive_verifier",
    "cases": [{"input": ["a", "b", "c"], "expected": 3}],
    "verifier": ("def check(inp, out, expected):\n"
                 "    if out != expected:\n"
                 "        return False, 'wrong output'\n"
                 "    # PLANTED: iteration-order dependence via randomized str hashing\n"
                 "    first = next(iter(set(inp)))\n"
                 "    ok = first == sorted(inp)[0]\n"
                 "    return ok, ('ok' if ok else f'set order gave {first!r}')\n"),
    "oracle": "def solve(inp):\n    return len(inp)\n",
    "agent":  "def solve(inp):\n    return len(inp)\n",
}

TASKS["float_exact_mean"] = {
    # Task DECLARES a tolerance, verifier compares exactly -> correct
    # alternative implementations get FALSE FAILs.
    "planted_flaw": "stricter_than_declared_spec",
    "spec_text": "Return the arithmetic mean; answers within 1e-6 are correct.",
    "equivalence": {"float_atol": 1e-6},
    "cases": [{"input": [0.1, 0.2, 0.3], "expected": (0.1 + 0.2 + 0.3) / 3},
              {"input": [1.0, 2.0], "expected": 1.5},
              {"input": [7.5], "expected": 7.5}],
    "verifier": ("def check(inp, out, expected):\n"
                 "    ok = out == expected   # PLANTED: exact == despite declared 1e-6\n"
                 "    return ok, ('ok' if ok else f'expected {expected!r}, got {out!r}')\n"),
    "oracle": "def solve(inp):\n    return sum(inp) / len(inp)\n",
    "agent":  "def solve(inp):\n    return sum(inp) / len(inp)\n",
}

TASKS["sort_shallow"] = {
    # Sound verifier, worthless test set: every case is already sorted, so
    # broken sorts pass. The tester should SURVIVE mutants and produce a
    # concrete distinguishing input as the receipt.
    "planted_flaw": "shallow_test_set",
    "cases": [{"input": [], "expected": []},
              {"input": [7], "expected": [7]},
              {"input": [1, 2, 3], "expected": [1, 2, 3]}],
    "verifier": STRICT_VERIFIER,
    "oracle": ("def solve(inp):\n"
               "    out = list(inp)\n"
               "    for i in range(1, len(out)):\n"
               "        j = i\n"
               "        while j > 0 and out[j - 1] > out[j]:\n"
               "            out[j - 1], out[j] = out[j], out[j - 1]\n"
               "            j -= 1\n"
               "    return out\n"),
    "agent": "def solve(inp):\n    return sorted(inp)\n",
}

TASKS["leaky_lookup"] = {
    # Runs under the OLD single-container harness: the expected answers sit in
    # /work/task.json readable by the solution. A leak probe should PASS here
    # (gameable) and FAIL on every split-harness task (regression proof).
    "planted_flaw": "answer_leakage",
    "harness": "leaky",
    "cases": [{"input": s, "expected": s.upper()} for s in ["ab", "xyz", "Q"]],
    "verifier": STRICT_VERIFIER,
    "oracle": "def solve(inp):\n    return inp.upper()\n",
    "agent":  "def solve(inp):\n    return inp.upper()\n",
}

# binary_search inputs must keep the array sorted; declare it so generated
# counterexample inputs stay in-spec.
TASKS["binary_search"]["invariants"] = ["arg0_sorted"]
