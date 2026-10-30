"""Re-judge the September (v1-runtime) ablation survivors under the v2 runtime.

reports/mut_run_20260910 lists 43 mutants of Terminal-Bench v4.0.0 reference
outputs that the v1 runtime reported as passing their verifier. v1 overlaid
artifact directories instead of emptying them, forced offline solves and took
verdicts from CTRF instead of the reward file, so each "survivor" is suspect.

This rebuilds every survivor from a fresh v2 reference bundle of the same task
version (same operator: drop the same file / path, or apply the recorded AST
diff to the unparsed source) and writes bundle tars plus the matching
`breakwater diff --bundle ...` arguments.

Usage: uv run python scripts/rejudge_v1_survivors.py OUT_DIR
(needs the v4.0.0 reference solves from a prior `breakwater diff` run)
"""
import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile

from breakwater import cas, db
from breakwater.adapters.harbor_task import artifact_paths
from breakwater.bundle import Bundle
from breakwater.trustdiff.versions import Source

REPORT = "reports/mut_run_20260910/mutation_report.json"


def apply_unparsed_diff(src: str, diff: str) -> str:
    ident = ast.unparse(ast.parse(src))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.py")
        open(p, "w").write(ident)
        body = "\n".join(l for l in diff.splitlines() if not l.startswith(("--- ", "+++ ")))
        patch = f"--- f.py\n+++ f.py\n{body}\n"
        r = subprocess.run(["patch", "-s", p], input=patch.encode(), capture_output=True)
        if r.returncode:
            raise ValueError(r.stdout.decode() + r.stderr.decode())
        return open(p).read()


async def main(out):
    os.makedirs(out, exist_ok=True)
    rep = json.load(open(REPORT))
    base = Source("base", repo="external/harbor-tb", ref="v4.0.0")
    pool = await db.make_pool()
    args, rows = [], []
    for s in rep["surviving"]:
        task = s["task"][3:].replace("_", "-")
        spec = base.version(task).spec
        ref_row = await pool.fetchrow(
            "SELECT detail->>'bundle' b FROM results WHERE trial_id LIKE $1",
            f"solve/%{task}/%/oracle")
        if not ref_row:
            rows.append({"task": task, "operator": s["operator"], "status": "no v2 reference"})
            continue
        ref = Bundle.from_tar(cas.get(ref_row["b"]), artifact_paths(spec))
        op, _, rest = s["operator"].partition("::")
        try:
            if op == "drop_file":
                if rest not in ref.entries:
                    raise ValueError("file absent from v2 reference")
                b = ref.replace(drop={rest})
            elif op == "drop_path":
                b = ref.replace(drop_under=["/" + rest.strip("/")])
            elif op == "unsolved_path":
                snap = await pool.fetchrow("SELECT detail->>'bundle' b FROM results "
                                           "WHERE trial_id LIKE $1", f"snapshot/%{task}/%")
                if not snap:
                    raise ValueError("no snapshot")
                sb = Bundle.from_tar(cas.get(snap["b"]), artifact_paths(spec))
                d = "/" + rest.strip("/")
                b = ref.replace(drop_under=[d], add=sb.subtree(d))
            elif op == "ast":
                path = rest.split("::")[0]
                new = apply_unparsed_diff(ref.files()[path].decode(), s["diff"])
                b = ref.replace(set_files={path: new.encode()})
            else:
                raise ValueError(f"unknown operator {op}")
        except Exception as e:
            rows.append({"task": task, "operator": s["operator"], "status": f"not rebuilt: {e}"})
            continue
        tar = os.path.join(out, f"{task}--{b.digest[7:23]}.tar")
        with open(tar, "wb") as f:
            f.write(b.to_tar())
        label = s["operator"].replace(":", "_")
        args.append(f"--bundle={task}:v1-survivor--{label}:unknown:{tar}")
        rows.append({"task": task, "operator": s["operator"], "status": "rebuilt",
                     "bundle": b.digest, "tar": tar})
    await pool.close()
    json.dump(rows, open(os.path.join(out, "rebuilt.json"), "w"), indent=2)
    open(os.path.join(out, "diff-args.txt"), "w").write("\n".join(args) + "\n")
    print(f"{sum(r['status'] == 'rebuilt' for r in rows)}/{len(rows)} survivors rebuilt -> {out}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
