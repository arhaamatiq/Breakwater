"""Regression coverage for the Harbor (Terminal-Bench v4) adapter.

Focused on the load-time classification predicates that decide whether a task
can be run faithfully offline. The tester core is exercised elsewhere; these
tests only pin adapter behaviour that a real audit depends on.
"""

from breakwater.adapters.harbor import _NET_INSTALL_RE, _parse_test_names


# --- solve-time network detection -------------------------------------------
#
# A task whose reference solution fetches packages at solve time cannot run
# under Breakwater's network-disabled solve sandbox: the oracle fails, and the
# audit then (correctly, given what it sees) raises REFERENCE_REJECTED. Such
# tasks must be classified `solve_network` and skipped instead.

NET_POSITIVE = {
    "pip install": "pip install requests==2.32.0",
    "python -m pip install": "python3 -m pip install --no-cache-dir openpyxl==3.1.5",
    "apt-get install": "apt-get update && apt-get install -y libnetcdf-dev",
    "uv pip install": "uv pip install --quiet pandas==2.2.2 scipy==1.13.1",
    "uvx --with": 'uvx --with pandas==2.2.3 python3 /solution/solve.py',
    "conda install": "conda install -y -c conda-forge numpy",
    "mamba install": "mamba install -y -n base -c conda-forge pymeep=1.29",
    "curl a url": "curl -LsSf https://example.com/x.tgz -o x.tgz",
    # `uv run --with` pulls from PyPI at solve time exactly like an install;
    # the flag routinely sits on a shell continuation line.
    "uv run --with (multiline)": (
        "#!/bin/bash\nset -euo pipefail\n\n"
        "uv run \\\n  --with pandas==2.2.3 \\\n  --with xlrd==2.0.1 \\\n"
        "  --no-project --isolated \\\n  python /solution/solve.py\n"
    ),
    "uv run --with-requirements": (
        "uv run --with-requirements /solution/reqs.txt python /solution/solve.py"
    ),
}

NET_NEGATIVE = {
    "plain python": "#!/bin/bash\nset -e\npython3 /solution/solve.py\n",
    "uv run no --with": "#!/bin/bash\nuv run --no-project python /solution/solve.py\n",
    "heredoc python": (
        "#!/bin/bash\nset -euo pipefail\npython3 - <<'EOF'\nprint('hi')\nEOF\n"
    ),
    "make": "#!/bin/bash\nmake -C /app build\n",
    "cargo build": "#!/bin/bash\ncargo build --release --offline\n",
}


def test_net_install_positive():
    for name, src in NET_POSITIVE.items():
        assert _NET_INSTALL_RE.search(src), f"should be flagged solve_network: {name}"


def test_net_install_negative():
    for name, src in NET_NEGATIVE.items():
        assert not _NET_INSTALL_RE.search(src), f"should NOT be flagged: {name}"


# --- pytest id extraction --------------------------------------------------

def test_parse_test_names_bare_and_class():
    src = (
        "import pytest\n"
        "def test_alpha():\n    assert True\n"
        "class TestThings:\n"
        "    def test_beta(self):\n        assert True\n"
        "    def helper(self):\n        return 1\n"
        "class WidgetTest:\n"
        "    def test_gamma(self):\n        assert True\n"
        "def not_a_test():\n    pass\n"
    )
    names = set(_parse_test_names(src))
    assert names == {"test_alpha", "TestThings::test_beta", "WidgetTest::test_gamma"}
