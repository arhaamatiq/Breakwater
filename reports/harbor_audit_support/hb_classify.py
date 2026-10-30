"""Static classification of all 66 harbor v4.0.0 tasks against the adapter's skip rules.
Does NOT build images (fast). Mirrors harbor.load()'s skip predicates."""
import os, re, tomllib, glob
try:
    import yaml
except Exception:
    yaml = None

ROOT = "external/harbor-tb/tasks"
import sys as _sys
_sys.path.insert(0, "src")
from breakwater.adapters.harbor import _NET_INSTALL_RE, _BROWSER_RE  # noqa: E402

def service_count(env_dir):
    for name in ("docker-compose.yaml","docker-compose.yml","compose.yaml","compose.yml"):
        p = os.path.join(env_dir, name)
        if os.path.exists(p):
            if not yaml: return 2
            try:
                d = yaml.safe_load(open(p)) or {}
                return len(d.get("services", {}) or {})
            except Exception:
                return 2
    return 1

def count_tests(tests_dir):
    n = 0
    for f in glob.glob(os.path.join(tests_dir, "test*.py")):
        try:
            src = open(f).read()
        except Exception:
            continue
        n += len(re.findall(r"^\s*def (test_\w+)", src, re.M))
        n += len(re.findall(r"^\s{4}def (test_\w+)", src, re.M))
    return n

rows = {}
for d in sorted(os.listdir(ROOT)):
    td = os.path.join(ROOT, d)
    if not os.path.isdir(td): continue
    toml_p = os.path.join(td, "task.toml")
    need = [toml_p, os.path.join(td,"environment/Dockerfile"),
            os.path.join(td,"solution/solve.sh"), os.path.join(td,"tests/Dockerfile"),
            os.path.join(td,"tests/test.sh")]
    if not all(os.path.exists(p) for p in need):
        missing = [os.path.relpath(p, td) for p in need if not os.path.exists(p)]
        rows[d] = ("format", "missing " + ",".join(missing)); continue
    try:
        cfg = tomllib.load(open(toml_p, "rb"))
    except Exception as e:
        rows[d] = ("format", f"toml parse: {e}"); continue
    v = cfg.get("verifier", {})
    if v.get("environment_mode") != "separate":
        rows[d] = ("unsupported_mode", str(v.get("environment_mode"))); continue
    env = cfg.get("environment", {})
    if (env.get("gpus") or 0) > 0:
        rows[d] = ("gpu", f"gpus={env.get('gpus')}"); continue
    arts = cfg.get("artifacts", [])
    if any(not isinstance(a, str) for a in arts):
        rows[d] = ("multi_service", "non-str artifact (service-scoped)"); continue
    sc = service_count(os.path.join(td, "environment"))
    if sc > 1:
        rows[d] = ("multi_service", f"{sc} compose services"); continue
    ntests = count_tests(os.path.join(td, "tests"))
    if ntests == 0:
        rows[d] = ("no_tests", "no test_* funcs"); continue
    solve = ""
    for sp in glob.glob(os.path.join(td, "solution", "**", "*"), recursive=True):
        if os.path.isfile(sp):
            try: solve += "\n" + open(sp, errors="ignore").read()
            except Exception: pass
    if _NET_INSTALL_RE.search(solve):
        m = _NET_INSTALL_RE.search(solve)
        rows[d] = ("solve_network", solve[max(0,m.start()-20):m.start()+40].replace("\n"," ")); continue
    try:
        tdf = open(os.path.join(td, "tests/Dockerfile"), errors="ignore").read()
    except Exception:
        tdf = ""
    if _BROWSER_RE.search(tdf):
        rows[d] = ("browser", "browser stack in tests/Dockerfile"); continue
    rows[d] = ("SUPPORTED", f"{ntests} tests, artifacts={arts}")

from collections import Counter
cats = Counter(v[0] for v in rows.values())
print("=== TOTAL:", len(rows), "tasks ===")
for c, n in cats.most_common():
    print(f"  {c:18} {n}")
print()
for cat in ["SUPPORTED"] + [c for c in cats if c!="SUPPORTED"]:
    print(f"--- {cat} ---")
    for d,(c,detail) in sorted(rows.items()):
        if c == cat:
            print(f"  {d:40} {detail[:70]}")
