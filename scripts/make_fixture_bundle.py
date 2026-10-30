"""Build a fixture bundle from a task's solved reference: one targeted edit to one
artifact file, written as a tar for `breakwater diff --bundle`.

  oversized-csv-field   first data row's first field becomes 200,000 chars
                        (Python's csv module raises csv.Error past 131,072)
  not-a-zip             replace an .xlsx/.zip artifact with plain text
  empty                 truncate the file to zero bytes

Usage: uv run python scripts/make_fixture_bundle.py TASK FILE MODE OUT.tar [--ref v4.0.0]
"""
import argparse
import asyncio

from breakwater import cas, db
from breakwater.adapters.harbor_task import artifact_paths
from breakwater.bundle import Bundle
from breakwater.trustdiff.versions import Source


def edit(data: bytes, mode: str) -> bytes:
    if mode == "oversized-csv-field":
        lines = data.split(b"\n")
        i = 1 if len(lines) > 1 else 0
        fields = lines[i].split(b",")
        fields[0] = b"x" * 200_000
        lines[i] = b",".join(fields)
        return b"\n".join(lines)
    if mode == "not-a-zip":
        return b"this is not a spreadsheet\n"
    if mode == "empty":
        return b""
    raise SystemExit(f"unknown mode {mode}")


async def main(a):
    spec = Source("base", repo="external/harbor-tb", ref=a.ref).version(a.task).spec
    pool = await db.make_pool()
    row = await pool.fetchrow(
        "SELECT detail->>'bundle' b FROM results WHERE trial_id LIKE $1 "
        "AND (detail->>'solution_exit_code')::int = 0 ORDER BY submitted_at DESC",
        f"solve/{a.rt}%{a.task}/%/oracle")
    await pool.close()
    if not row:
        raise SystemExit("no reference solve for this task yet")
    ref = Bundle.from_tar(cas.get(row["b"]), artifact_paths(spec))
    if a.file not in ref.files():
        raise SystemExit(f"{a.file} not in reference bundle: {sorted(ref.files())[:10]}")
    b = ref.replace(set_files={a.file: edit(ref.files()[a.file], a.mode)})
    with open(a.out, "wb") as f:
        f.write(b.to_tar())
    print(a.out, b.digest)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("file")
    ap.add_argument("mode")
    ap.add_argument("out")
    ap.add_argument("--ref", default="v4.0.0")
    ap.add_argument("--rt", default="", help="runtime tag prefix of the solve, e.g. hb2-amd64/")
    asyncio.run(main(ap.parse_args()))
