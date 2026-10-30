"""Output corpora: the fixed bundles every verifier version is judged against.

A verifier regression test needs inputs whose correct verdict is known, or at
least fixed. Breakwater builds them mechanically, with no model in the loop:

  reference      the task's own solution, solved once        expected PASS
  noop           an empty solve                              expected FAIL
  degenerate     whole-output damage: drop / truncate /      expected FAIL
                 halve every file, rotate every digit
  mutant         fine-grained damage: drop one declared      unknown — a
                 path, revert it to its pre-solve state,     mutant that
                 drop one file, or one AST edit (comparator, passes is a
                 operator, boolean, integer ±1) in one       *candidate*
                 Python artifact                              gap, never a
                                                              finding by itself
  ast_identity   the unparse round-trip of each mutated file expected PASS
                 (control: AST mutants are emitted via ast.unparse, so a
                 mutant only means something if its identity control passes)

Every mutant is derived from the reference bundle by a pure function, so the
corpus is reproducible from (task, solution digest, operator list) alone and
each bundle's identity is its content digest.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from ..bundle import Bundle, under

TEXTISH = (".json", ".csv", ".tsv", ".txt", ".md", ".yaml", ".yml", ".xml", ".py", ".log",
           ".v", ".scala", ".cu", ".c", ".h", ".cpp", ".js", ".ts", ".html", ".ini", ".cfg",
           ".toml", ".sh", ".sql", ".patch", ".step", ".stp", ".svg", ".rs", ".go", ".java",
           ".lean", ".cc", ".hpp", ".tsx", ".jsx", ".r", ".jl")
DIGIT_ROT = bytes.maketrans(b"0123456789", b"1234567890")
EXPECT = {"reference": "pass", "noop": "fail", "degenerate": "fail",
          "ast_identity": "pass", "mutant": None, "imported": None}


@dataclass
class CorpusItem:
    label: str            # stable human name, e.g. "ast::/app/x.py::cmp(Lt)#3"
    kind: str             # reference | noop | degenerate | mutant | ast_identity | imported
    bundle: Bundle
    meta: dict

    @property
    def expect(self) -> str | None:
        return EXPECT.get(self.kind)


def _textish(path: str, data: bytes) -> bool:
    return b"\x00" not in data[:8192] and path.lower().endswith(TEXTISH)


def _spread(seq, k):
    if len(seq) <= k:
        return list(seq)
    step = len(seq) / k
    return [seq[int(i * step)] for i in range(k)]


def mutants(reference: Bundle, snapshot: Bundle | None, *, max_files: int = 8,
            max_ast_per_file: int = 8, max_ast_files: int = 4,
            include: tuple[str, ...] = ("degenerate", "path", "file", "ast")) -> list[CorpusItem]:
    """Deterministic ablations of a reference bundle."""
    out: list[CorpusItem] = []
    files = reference.files()
    declared = [d for d in reference.declared if d != "/logs/artifacts"]

    def add(label, kind, b, **meta):
        if b.digest != reference.digest:      # an op that changes nothing is skipped
            out.append(CorpusItem(label, kind, b, meta))

    if "degenerate" in include and files:
        add("drop_all", "degenerate", reference.replace(drop_under=declared))
        add("zero_all", "degenerate", reference.replace(set_files={p: b"" for p in files}))
        add("halve_all", "degenerate",
            reference.replace(set_files={p: d[: len(d) // 2] for p, d in files.items()}))
        rot = {p: d.translate(DIGIT_ROT) for p, d in files.items() if _textish(p, d)}
        if any(rot[p] != files[p] for p in rot):
            add("digits_all", "degenerate", reference.replace(set_files=rot))

    if "path" in include and len(declared) >= 2:
        for d in declared:
            tag = d.strip("/")
            add(f"drop_path::{tag}", "mutant", reference.replace(drop_under=[d]), path=d)
            if snapshot is not None:
                pre = snapshot.subtree(d)
                add(f"unsolved_path::{tag}", "mutant",
                    reference.replace(drop_under=[d], add=pre), path=d)

    if "file" in include and len(files) > 1:
        for p in _spread(sorted(files), max_files):
            add(f"drop_file::{p}", "mutant", reference.replace(drop={p}), file=p)

    if "ast" in include:
        from ..tester.mutate import mutants as ast_mutants
        py = [p for p in sorted(files) if p.endswith(".py")]
        for p in _spread(py, max_ast_files):
            try:
                src = files[p].decode()
                ident = ast.unparse(ast.parse(src))
            except Exception:
                continue
            muts = ast_mutants(src, limit=60)
            if not muts:
                continue
            ident_b = reference.replace(set_files={p: ident.encode()})
            out.append(CorpusItem(f"ast_identity::{p}", "ast_identity", ident_b,
                                  {"file": p}))
            for j, m in enumerate(_spread(muts, max_ast_per_file)):
                add(f"ast::{p}::{m['op']}#{j}", "mutant",
                    reference.replace(set_files={p: m["src"].encode()}),
                    file=p, edit=m["op"], control=ident_b.digest,
                    diff=_unified(ident, m["src"], p))
    return out


def _unified(a: str, b: str, path: str, n: int = 2) -> str:
    import difflib
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                        f"reference{path}", f"mutant{path}", n=n))[:4000]
