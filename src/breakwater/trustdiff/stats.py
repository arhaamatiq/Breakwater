"""Small, dependency-free statistics for verdict and margin comparisons.

Verdicts are Bernoulli: a verifier version either passes a fixed bundle or
not, run after run. Wilson intervals bound each pass rate; Fisher's exact
test asks whether two versions differ on the SAME bundle. Margins (e.g. a
measured speedup) are compared by the ratio of medians with a seeded
bootstrap CI — a CI that excludes 1.0 is a systematic measurement shift,
not noise.
"""
from __future__ import annotations

import math
import random
from statistics import median


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _hypergeom(a: int, b: int, c: int, d: int) -> float:
    n = a + b + c + d
    return (math.comb(a + b, a) * math.comb(c + d, c)) / math.comb(n, a + c)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for [[a, b], [c, d]] (sum of tables at most as
    likely as the observed one)."""
    r1, c1, n = a + b, a + c, a + b + c + d
    if n == 0:
        return 1.0
    p_obs = _hypergeom(a, b, c, d)
    p = 0.0
    for x in range(max(0, c1 - (n - r1)), min(r1, c1) + 1):
        px = _hypergeom(x, r1 - x, c1 - x, n - r1 - c1 + x)
        if px <= p_obs * (1 + 1e-9):
            p += px
    return min(1.0, p)


def bootstrap_ratio_ci(base: list[float], head: list[float], iters: int = 4000,
                       seed: int = 0, alpha: float = 0.05) -> tuple[float, float, float] | None:
    """median(head)/median(base) with a percentile bootstrap CI."""
    base = [x for x in base if x is not None and x > 0]
    head = [x for x in head if x is not None and x > 0]
    if len(base) < 2 or len(head) < 2:
        return None
    rng = random.Random(seed)
    point = median(head) / median(base)
    samples = []
    for _ in range(iters):
        b = median(rng.choices(base, k=len(base)))
        h = median(rng.choices(head, k=len(head)))
        samples.append(h / b)
    samples.sort()
    lo = samples[int(alpha / 2 * iters)]
    hi = samples[int((1 - alpha / 2) * iters) - 1]
    return (point, lo, hi)


def mann_whitney_p(x: list[float], y: list[float]) -> float | None:
    """Two-sided Mann-Whitney U, normal approximation with tie correction.
    Fine for the n>=5 per side this is used with; reported, not gated on."""
    x = [v for v in x if v is not None]
    y = [v for v in y if v is not None]
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        return None
    allv = sorted([(v, 0) for v in x] + [(v, 1) for v in y])
    ranks = [0.0] * len(allv)
    i, ties = 0, 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    n = n1 + n2
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1))))
    if sigma == 0:
        return 1.0
    zv = (abs(u - mu) - 0.5) / sigma
    return max(0.0, min(1.0, math.erfc(zv / math.sqrt(2))))
