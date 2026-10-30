"""Single-edit AST mutants of the reference solution.

The point: a mutant that changes real behavior yet still PASSes the benchmark
is a measured hole in the benchmark's discrimination power — and the input on
which it deviates from the reference (found by cross-examination) is the
concrete test case the benchmark is missing."""
import ast
import copy

_CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
             ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
_BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add}
_BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}


def _sites(tree: ast.AST) -> list[tuple]:
    """Enumerate (path-id, op-kind, detail) mutation sites."""
    sites = []
    for i, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                and type(node.ops[0]) in _CMP_SWAP:
            sites.append((i, "cmp", type(node.ops[0]).__name__))
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_SWAP:
            sites.append((i, "bin", type(node.op).__name__))
        if isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            sites.append((i, "bool", type(node.op).__name__))
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool) and abs(node.value) < 10**6:
            sites.append((i, "int+1", node.value))
            sites.append((i, "int-1", node.value))
        if isinstance(node, ast.AugAssign) and type(node.op) in _BIN_SWAP:
            sites.append((i, "aug", type(node.op).__name__))
    return sites


def _apply(tree: ast.AST, site: tuple) -> ast.AST | None:
    idx, kind, _ = site
    tree = copy.deepcopy(tree)
    for i, node in enumerate(ast.walk(tree)):
        if i != idx:
            continue
        if kind == "cmp":
            node.ops = [_CMP_SWAP[type(node.ops[0])]()]
        elif kind == "bin":
            node.op = _BIN_SWAP[type(node.op)]()
        elif kind == "bool":
            node.op = _BOOL_SWAP[type(node.op)]()
        elif kind == "aug":
            node.op = _BIN_SWAP[type(node.op)]()
        elif kind == "int+1":
            node.value = node.value + 1
        elif kind == "int-1":
            node.value = node.value - 1
        return tree
    return None


def mutants(src: str, limit: int = 40) -> list[dict]:
    """[{src, op, describes}] — compiled-checked, deduped, original excluded."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    original = ast.dump(ast.parse(src))
    out, seen = [], {original}
    for site in _sites(tree):
        mutated = _apply(tree, site)
        if mutated is None:
            continue
        ast.fix_missing_locations(mutated)
        dump = ast.dump(mutated)
        if dump in seen:
            continue
        seen.add(dump)
        try:
            new_src = ast.unparse(mutated)
            compile(new_src, "<mutant>", "exec")
        except Exception:
            continue
        _, kind, detail = site
        out.append({"src": new_src, "op": f"{kind}({detail})"})
        if len(out) >= limit:
            break
    return out
