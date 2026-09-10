"""Probe portfolio: solutions that know nothing about the task semantics, and
output perturbations. If any of these achieves PASS (excluding per-case
coincidences with the reference), the benchmark's PASS is not evidence of a
solution."""

LEAK_PROBE = '''\
def solve(inp):
    # Reads every file it can reach and hunts for leaked expected answers.
    import json, glob
    for p in glob.glob('/work/**/*', recursive=True) + glob.glob('/tmp/**/*', recursive=True):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        stack = [d]
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                if 'expected' in v and v.get('input') == inp:
                    return v['expected']
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
    return None
'''

RAISE_PROBE = 'def solve(inp):\n    raise RuntimeError("probe exception")\n'

# name -> solution source. "memorize_first" is built at audit time (constant
# equal to the reference output of case 0 — catches verifiers that ignore
# which case an output belongs to).
DEGENERATE_SOLUTIONS = {
    "noop_none":   "def solve(inp):\n    return None\n",
    "const_zero":  "def solve(inp):\n    return 0\n",
    "const_empty_str":  "def solve(inp):\n    return ''\n",
    "const_empty_list": "def solve(inp):\n    return []\n",
    "echo_input":  "def solve(inp):\n    return inp\n",
    "leak_probe":  LEAK_PROBE,
    "raise_probe": RAISE_PROBE,
}


def memorize_first_probe(first_reference_output) -> str:
    return f"def solve(inp):\n    return {first_reference_output!r}\n"


# Environment interventions. Verdicts must be invariant to all of these.
ENV_PROBES = {
    "PYTHONHASHSEED": ["0", "1", "2", "3", "4", "5", "6", "7"],
    "TZ": ["UTC", "Pacific/Kiritimati"],
    "LC_ALL": ["C", "en_US.UTF-8"],
}

# Generic output-form perturbations (undeclared equivalences -> INFO findings).
GENERIC_TRANSFORMS = ["as_tuple", "append_newline", "float_cast", "unicode_nfd"]


def declared_transforms(task: dict) -> list[str]:
    """Perturbations the benchmark's own declared semantics say are harmless."""
    eq = task.get("equivalence") or {}
    out = []
    if "float_atol" in eq:
        # nudge well inside the declared tolerance
        out.append(f"float_nudge:{eq['float_atol'] / 1000.0}")
        out.append(f"float_nudge:{-eq['float_atol'] / 1000.0}")
    return out
