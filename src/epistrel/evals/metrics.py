"""Pure-Python scoring helpers of #157's suites (no dependency beyond the standard library).

- ``spearman(xs, ys)``: Spearman's rank correlation with average ranks for ties (E-04: the
  model's importance against a binary pivotal/trivial label).
- ``span_overlap(a, b)``: the share of the shorter of two half-open character spans the two
  have in common (E-05's salience spans and E-10's claim spans: ≥ 0.5 is a hit, so a detector's
  verb span inside a labeled claim, or a model's wider span around it, both count).
- ``recall_at(ranked, relevant, k)`` and ``reciprocal_rank(ranked, relevant)`` (Q-01, Q-03).
- ``f_beta(precision, recall, beta)``: E-05 reports F-beta with beta 3 (false negatives
  weighted x3) beside its rates.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence

HIT_OVERLAP = 0.5
"""Two spans name the same thing when they share at least half of the shorter one."""


def average_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks of ``values``, ties sharing the mean of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean
        i = j + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson's correlation; ``None`` when either side is constant or there are < 2 points."""
    if len(xs) != len(ys):
        raise ValueError("pearson: the sequences differ in length")
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman's rho: Pearson's correlation of the average ranks."""
    return pearson(average_ranks(xs), average_ranks(ys))


def span_overlap(a: tuple[int, int], b: tuple[int, int]) -> float:
    """The characters ``a`` and ``b`` share over the shorter one's length (0 for disjoint)."""
    shared = min(a[1], b[1]) - max(a[0], b[0])
    shortest = min(a[1] - a[0], b[1] - b[0])
    if shared <= 0 or shortest <= 0:
        return 0.0
    return shared / shortest


def hits(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """``span_overlap(a, b) >= HIT_OVERLAP``."""
    return span_overlap(a, b) >= HIT_OVERLAP


def recall_at(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    """The share of ``relevant`` among the first ``k`` of ``ranked`` (1.0 when none is)."""
    if not relevant:
        return 1.0
    top = set(ranked[:k])
    return sum(1 for r in relevant if r in top) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: Collection[str]) -> float:
    """1 / the rank of the first relevant item of ``ranked`` (0 when none is ranked)."""
    for position, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


Z_95 = 1.959963984540054
"""The two-sided 95 % normal quantile."""


def wilson(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """The Wilson score interval (95 % by default) of ``successes`` in ``n`` trials; ``None``
    with no trials (#158: every judge rate's confidence interval)."""
    if n <= 0:
        return None
    p = successes / n
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    # At 0 or n successes the bound is exactly 0 or 1 (no rounding residue).
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high


def mean_ci(values: Sequence[float], z: float = Z_95) -> tuple[float, float] | None:
    """A normal-approximation 95 % interval of the mean of ``values`` (``None`` below two
    values): E-13's judge scores per fingerprint (#158)."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    half = z * sd / math.sqrt(n)
    return mean - half, mean + half


def f_beta(precision: float, recall: float, beta: float = 2.0) -> float:
    """F-beta: recall counts ``beta`` times as much as precision (0 when both are 0)."""
    b2 = beta * beta
    denominator = b2 * precision + recall
    return 0.0 if denominator == 0 else (1 + b2) * precision * recall / denominator


__all__ = [
    "HIT_OVERLAP",
    "Z_95",
    "average_ranks",
    "f_beta",
    "hits",
    "mean_ci",
    "pearson",
    "recall_at",
    "reciprocal_rank",
    "span_overlap",
    "spearman",
    "wilson",
]
