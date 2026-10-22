"""The gate rules of NFR-QUAL-8 (docs/04 §8, E-08; #134).

A metric is measured once per seed over the same labeled cases. Each case observes it as
**good** or **bad** (``True``/``False``), or not at all (``None``: the case is outside the
metric's denominator, as a supported claim is for a false-contradiction rate). A model error
is a bad observation, never an exclusion. Per seed the metric's value is the share of good
observations for an ``at_least`` floor (recall ≥ 95 %) and the share of bad ones for an
``at_most`` floor (false support ≤ 1 %); values are fractions 0-1.

- **Rate rule** (``rate``): the mean across seeds clears the floor by more than one sample
  standard deviation (n - 1): ``mean - sd > floor`` for ``at_least``, ``mean + sd < floor`` for
  ``at_most``; on the floor is not clear of it.
- **Observed-zero rule** (``zero``): no bad observation on any seed, on at least
  ``ZERO_MIN_CASES`` (300) cases per seed. The verdict reports the one-sided 95 % upper bound
  on the true rate, ``1 - 0.05^(1/n)`` (≈ 3/n): evidence of a low rate, never a claim of zero.
  One violation on any seed fails, and so does a clean run on fewer than 300 cases.
- **Relative rule** (``relative``, NFR-QUAL-14's engine-intervention floor; #158): an aggregate
  target on the ``at_most`` side, judged only on at least ``RELATIVE_MIN_PAIRS`` (300)
  observations per seed (E-12's non-tied pairs of one intervention type); it passes when the mean
  across seeds plus one sample standard deviation is **at most** the floor (NFR-QUAL-14 says "at
  most 55 %"), and fewer observations are ``insufficient_evidence``, never a pass.
- **Informational** (``informational``): no floor; the verdict is ``report_only``.

Each floored rule is ``insufficient_evidence`` with no observations or fewer than two seeds
(no standard deviation), unless the run already fails (a violation is evidence enough).
``gate`` folds verdicts: any ``fail`` or ``error`` fails, else any ``insufficient_evidence``
leaves the whole ``insufficient_evidence``, else it passes; ``report_only`` never counts.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

ZERO_MIN_CASES = 300
"""NFR-QUAL-8's minimum labeled cases per metric (per seed) for a zero-tolerance pass."""
RELATIVE_MIN_PAIRS = 300
"""NFR-QUAL-14's minimum non-tied pairs of an intervention type (per seed) before its floor
is judged (#158)."""
CONFIDENCE = 0.95
MIN_SEEDS = 2
"""A standard deviation needs two seeds; fewer is ``insufficient_evidence``."""
METRIC_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
"""Lower-case dotted ids: ``nfr_qual_2.false_support`` (an NFR id with its sub-metric)."""
REQUIREMENT_PATTERN = re.compile(r"^nfr_qual_(\d+)(?:\.|$)")


class Rule(StrEnum):
    rate = "rate"
    zero = "zero"
    relative = "relative"
    informational = "informational"


class Direction(StrEnum):
    """Which side of the floor is good: the good rate is at least it, or the bad rate at most."""

    at_least = "at_least"
    at_most = "at_most"


class Verdict(StrEnum):
    passed = "pass"
    failed = "fail"
    insufficient_evidence = "insufficient_evidence"
    report_only = "report_only"
    error = "error"
    """A suite the harness could not run (a malformed case, a harness exception)."""


@dataclass(frozen=True, slots=True)
class Metric:
    """One measured quantity: its id, gate rule, floor (a fraction; ``0`` for ``zero``), which
    side of the floor is good, and a short description for the report."""

    id: str
    rule: Rule
    floor: float | None = None
    direction: Direction = Direction.at_least
    description: str = ""

    @property
    def requirement(self) -> str | None:
        """The NFR-QUAL requirement the id names (``nfr_qual_2.x`` → ``NFR-QUAL-2``), if any."""
        found = REQUIREMENT_PATTERN.match(self.id)
        return None if found is None else f"NFR-QUAL-{int(found.group(1))}"


def metric_problems(metric: Metric) -> list[str]:
    """Why ``metric`` cannot gate as declared (empty when it can): a floored rule needs a floor
    in 0-1, a zero-tolerance metric's floor is 0 on the ``at_most`` side, an informational one
    has none, and the id is lower-case and dotted."""
    found: list[str] = []
    if not METRIC_ID_PATTERN.match(metric.id):
        found.append(f"{metric.id!r}: not a lower-case dotted metric id")
    if metric.rule is Rule.informational:
        if metric.floor is not None:
            found.append(f"{metric.id}: an informational metric has no floor")
        return found
    if metric.floor is None:
        found.append(f"{metric.id}: rule {metric.rule} but no floor")
        return found
    if not 0.0 <= metric.floor <= 1.0:
        found.append(f"{metric.id}: floor {metric.floor} is not a fraction 0-1")
    if metric.rule is Rule.zero and (metric.floor != 0.0 or metric.direction is Direction.at_least):
        found.append(f"{metric.id}: a zero-tolerance metric has floor 0 on the at_most side")
    if metric.rule is Rule.relative and metric.direction is not Direction.at_most:
        found.append(f"{metric.id}: a relative metric's floor is on the at_most side")
    return found


@dataclass(frozen=True, slots=True)
class SeedTally:
    """One seed's observations of one metric. ``value`` overrides the share of good (or bad)
    observations for a metric a suite aggregates as a value (``Suite.aggregate``: a rank
    correlation over ``good`` cases); ``None`` keeps the share."""

    seed: int
    good: int = 0
    bad: int = 0
    value: float | None = None

    @property
    def n(self) -> int:
        return self.good + self.bad


def upper_bound_95(n: int) -> float:
    """The exact one-sided 95 % upper bound on a rate after 0 events in ``n`` trials:
    ``1 - 0.05^(1/n)`` (≈ 3/n); 1.0 with no trials."""
    if n <= 0:
        return 1.0
    bound: float = 1.0 - math.pow(1.0 - CONFIDENCE, 1.0 / n)
    return bound


@dataclass(frozen=True, slots=True)
class Judgement:
    """A metric's verdict and the numbers behind it (``mean``/``stdev`` of the per-seed values;
    ``sample_size`` = cases per seed, the smallest seed's; ``violations`` and
    ``upper_bound_95`` for ``zero``)."""

    verdict: Verdict
    mean: float | None
    stdev: float | None
    sample_size: int
    seed_count: int
    per_seed: tuple[float, ...]
    violations: int | None = None
    upper_bound_95: float | None = None


def _value(metric: Metric, tally: SeedTally) -> float:
    if tally.value is not None:
        return tally.value
    count = tally.good if metric.direction is Direction.at_least else tally.bad
    return count / tally.n


def judge(metric: Metric, tallies: Sequence[SeedTally]) -> Judgement:
    """``metric``'s verdict over ``tallies`` (one per seed) under its rule (module docstring)."""
    seeds = len(tallies)
    sample = min((t.n for t in tallies), default=0)
    values = tuple(_value(metric, t) for t in tallies if t.n)
    mean = statistics.fmean(values) if values else None
    stdev = statistics.stdev(values) if len(values) >= MIN_SEEDS else None
    violations: int | None = None
    bound: float | None = None
    if metric.rule is Rule.zero:
        violations = sum(t.bad for t in tallies)
        bound = upper_bound_95(sample) if violations == 0 else None
        verdict = _zero_verdict(violations, sample, seeds)
    elif metric.rule is Rule.informational or metric.floor is None:
        verdict = Verdict.report_only
    elif sample == 0 or mean is None or stdev is None:
        verdict = Verdict.insufficient_evidence
    elif metric.rule is Rule.relative:
        if sample < RELATIVE_MIN_PAIRS:
            verdict = Verdict.insufficient_evidence
        else:
            verdict = Verdict.passed if mean + stdev <= metric.floor else Verdict.failed
    elif metric.direction is Direction.at_least:
        verdict = Verdict.passed if mean - stdev > metric.floor else Verdict.failed
    else:
        verdict = Verdict.passed if mean + stdev < metric.floor else Verdict.failed
    return Judgement(verdict, mean, stdev, sample, seeds, values, violations, bound)


def _zero_verdict(violations: int, sample: int, seeds: int) -> Verdict:
    if violations:
        return Verdict.failed
    if sample == 0:
        return Verdict.insufficient_evidence
    if sample < ZERO_MIN_CASES:
        return Verdict.failed
    if seeds < MIN_SEEDS:
        return Verdict.insufficient_evidence
    return Verdict.passed


def strict(metric: Metric, per_seed: Sequence[float]) -> Verdict:
    """The stub-mode strict rule (#157's flipped-label complement): a floored metric passes only
    when every seed's value is perfect (1 for ``at_least``, 0 for ``at_most``), so a single
    mislabeled case fails it; an informational metric is ``report_only``."""
    if metric.rule is Rule.informational or metric.floor is None:
        return Verdict.report_only
    if not per_seed:
        return Verdict.insufficient_evidence
    perfect = 1.0 if metric.direction is Direction.at_least else 0.0
    return Verdict.passed if all(v == perfect for v in per_seed) else Verdict.failed


def gate(verdicts: Iterable[Verdict]) -> Verdict:
    """The verdict of a suite or a run from its metrics' (or suites') verdicts."""
    seen = set(verdicts)
    if seen & {Verdict.failed, Verdict.error}:
        return Verdict.failed
    if Verdict.insufficient_evidence in seen:
        return Verdict.insufficient_evidence
    if seen - {Verdict.report_only}:
        return Verdict.passed
    return Verdict.report_only


__all__ = [
    "CONFIDENCE",
    "MIN_SEEDS",
    "RELATIVE_MIN_PAIRS",
    "ZERO_MIN_CASES",
    "Direction",
    "Judgement",
    "Metric",
    "Rule",
    "SeedTally",
    "Verdict",
    "gate",
    "judge",
    "metric_problems",
    "strict",
    "upper_bound_95",
]
