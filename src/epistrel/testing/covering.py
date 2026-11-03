"""C-17's module combinations (FR-PROF-2,4; #243): schema-driven factors, a seeded pairwise
covering array, and the analysis of which pairs a valid configuration can cover.

**Factors** (``factors``) are read from ``ProfileSettings``, never listed by hand: every setting
of a delivered module (``schema.DELIVERED``) that bears a level. A boolean contributes both
values, an enum or ``Literal`` every value, and a bounded integer dial that selects capability
(``CAPABILITY_DIALS``) its minimum, default and maximum. Every other numeric dial stays at its
default (``held``). A level the schema gains therefore enters the array with no code change.

**The array** (``pairwise``) is a deterministic, greedy IPOG-style construction: factors are
taken in order; each new factor extends the rows already built with the level covering the
most uncovered pairs (ties broken by a seeded shuffle), then new rows are added for the pairs
still uncovered. The same seed gives the same array. A candidate may be invalid: validity is
decided by ``validate_profile``.

**Feasibility** (``complete``): a pair is coverable by a valid configuration when some
completion of it validates. Starting from the defaults with the pair set, every dependency the
validator names missing is switched on at a level it accepts, until the configuration is valid
or a requirement falls on one of the pair's own settings or an undelivered value (``None``:
``infeasible``). ``cover`` adds such a repair configuration for every pair the array's valid
rows leave uncovered, so no ``uncovered_valid`` pair remains.
"""

from __future__ import annotations

import enum
import random
import types
import typing
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.profiles.validate import validate_profile
from epistrel.core.settings_schema import leaves

CAPABILITY_DIALS: Mapping[str, str] = types.MappingProxyType(
    {
        "character_knowledge.nested_beliefs.depth": "nesting depth (when nested beliefs are on)",
        "repair.max_attempts": "the repair module's switch (0 is off)",
    }
)
"""Bounded integer dials that select capability: each contributes {min, default, max}."""
SIZE_MAX = 60
"""The most configurations the array may hold for the current schema (C-17's CI bound)."""
COMPLETION_STEPS = 12

Factors = dict[str, list[Any]]
Row = dict[str, Any]
Pair = tuple[str, Any, str, Any]


def _bounds(info: Any) -> tuple[int, int] | None:
    low = next((m.ge for m in info.metadata if hasattr(m, "ge")), None)
    high = next((m.le for m in info.metadata if hasattr(m, "le")), None)
    return (int(low), int(high)) if low is not None and high is not None else None


def _levels(path: str, info: Any) -> list[Any] | None:
    annotation = info.annotation
    if annotation is bool:
        return [True, False]
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return [member.value for member in annotation]
    if typing.get_origin(annotation) is typing.Literal:
        return list(typing.get_args(annotation))
    if path in CAPABILITY_DIALS and annotation is int:
        bounds = _bounds(info)
        if bounds is not None:
            return list(dict.fromkeys([bounds[0], info.default, bounds[1]]))
    return None


def _delivered(path: str) -> bool:
    owner = schema.owner_of(path)
    return owner is not None and owner[0] in schema.DELIVERED


@dataclass(frozen=True, slots=True)
class FactorSet:
    """The array's factors (path → levels) and the settings held at their defaults."""

    factors: Factors
    held: tuple[str, ...]


def factors() -> FactorSet:
    """Every level-bearing setting of a delivered module, with its levels (see the module
    docstring), and the settings held at their defaults."""
    found: Factors = {}
    held: list[str] = []
    for path, info in leaves(ProfileSettings):
        if not _delivered(path):
            continue
        levels = _levels(path, info)
        if levels is None:
            held.append(path)
        else:
            found[path] = levels
    return FactorSet(found, tuple(held))


def _pair(a: str, va: Any, b: str, vb: Any, order: Mapping[str, int]) -> Pair:
    return (a, va, b, vb) if order[a] < order[b] else (b, vb, a, va)


def every_pair(found: Factors) -> set[Pair]:
    """Every pair of levels of every pair of factors."""
    order = {name: i for i, name in enumerate(found)}
    return {
        _pair(a, va, b, vb, order)
        for a, b in combinations(found, 2)
        for va in found[a]
        for vb in found[b]
    }


def pairs_of(row: Mapping[str, Any], found: Factors) -> set[Pair]:
    """The pairs one configuration covers (its values of the factors)."""
    order = {name: i for i, name in enumerate(found)}
    names = [n for n in found if n in row]
    return {_pair(a, row[a], b, row[b], order) for a, b in combinations(names, 2)}


def covered(rows: Iterable[Mapping[str, Any]], found: Factors) -> set[Pair]:
    seen: set[Pair] = set()
    for row in rows:
        seen |= pairs_of(row, found)
    return seen


def pairwise(found: Factors, seed: int = 0) -> list[Row]:
    """A pairwise covering array over ``found`` (deterministic for ``seed``)."""
    names = list(found)
    rng = random.Random(seed)  # noqa: S311 - a reproducible tie-break, not a secret
    if not names:
        return []
    rows: list[Row] = [{names[0]: level} for level in found[names[0]]]
    order = {name: i for i, name in enumerate(names)}
    for index in range(1, len(names)):
        name = names[index]
        earlier = names[:index]
        wanted = {
            _pair(other, vo, name, level, order)
            for other in earlier
            for vo in found[other]
            for level in found[name]
        }
        for row in rows:
            choices = list(found[name])
            rng.shuffle(choices)
            best = max(
                choices,
                key=lambda level: len(
                    {_pair(other, row[other], name, level, order) for other in earlier} & wanted
                ),
            )
            row[name] = best
            wanted -= {_pair(other, row[other], name, best, order) for other in earlier}
        while wanted:
            first = min(wanted, key=lambda p: (order[p[0]], order[p[2]], repr(p[1]), repr(p[3])))
            other, vo, _, level = first
            row = {other: vo, name: level}
            for candidate in wanted.copy():
                a, va, b, vb = candidate
                if b != name or vb != level or a in row:
                    continue
                row[a] = va
            for prior in earlier:
                row.setdefault(prior, found[prior][0])
            rows.append(row)
            wanted -= {_pair(o, row[o], name, row[name], order) for o in earlier}
    return rows


def document(row: Mapping[str, Any]) -> dict[str, Any]:
    """A configuration's settings document (nested by dotted path)."""
    found: dict[str, Any] = {}
    for path, value in row.items():
        target = found
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return found


@dataclass(frozen=True, slots=True)
class Checked:
    """A candidate and what validation said: ``valid``, or the missing dependencies and
    undelivered settings that refused it."""

    row: Row
    valid: bool
    missing: tuple[str, ...] = ()
    undelivered: tuple[str, ...] = ()

    @property
    def refused_for_a_reason(self) -> bool:
        """A refusal counts as a pass only when it names a dependency or an undelivered level."""
        return not self.valid and bool(self.missing or self.undelivered)


def check(row: Mapping[str, Any]) -> Checked:
    """``row`` through ``validate_profile`` (the pure validator #106 ships)."""
    found = validate_profile(document(row))
    return Checked(
        dict(row),
        found.valid,
        tuple(d.module for d in found.missing_dependencies),
        tuple(u.setting for u in found.undelivered),
    )


def _switch_value(requirement: Any) -> tuple[str, Any] | None:
    module = schema.MODULE.get(requirement.module)
    if module is None or module.switch is None:
        return None
    if module.positive:
        return module.switch, 1
    for value, level in module.levels:
        if not requirement.levels or level in requirement.levels:
            return module.switch, value
    return None


def _completed(row: Row, fixed: Mapping[str, Any]) -> Row | None:
    """``row`` with every dependency the validator names missing switched on, leaving
    ``fixed`` as it is; ``None`` when no such completion validates."""
    row = {**row, **fixed}
    for _ in range(COMPLETION_STEPS):
        result = validate_profile(document(row))
        if result.valid:
            return row
        if result.undelivered:
            return None
        changed = False
        for missing in result.missing_dependencies:
            for need in missing.requires:
                target = _switch_value(need)
                if target is None or target[0] in fixed:
                    return None
                path, value = target
                if row.get(path) != value:
                    row[path] = value
                    changed = True
        if not changed:
            return None
    return None


def complete(pair: Pair, found: Factors) -> Row | None:
    """A valid configuration covering ``pair``, or ``None`` when none exists (see the module
    docstring)."""
    a, va, b, vb = pair
    return _completed({name: _default(name) for name in found}, {a: va, b: vb})


def _default(path: str) -> Any:
    info = dict(leaves(ProfileSettings))[path]
    return info.default


@dataclass
class Coverage:
    """The array's configurations, how each validated, the pairs no completion can cover
    (``infeasible``) and the pairs a valid configuration covers."""

    rows: list[Row]
    checked: list[Checked] = field(default_factory=list)
    infeasible: list[Pair] = field(default_factory=list)
    repairs: int = 0

    @property
    def valid(self) -> list[Row]:
        return [c.row for c in self.checked if c.valid]

    def uncovered_valid(self, found: Factors) -> list[Pair]:
        """Pairs neither covered by a valid configuration nor infeasible (C-17 wants none)."""
        seen = covered(self.valid, found)
        return sorted(
            (p for p in every_pair(found) if p not in seen and p not in self.infeasible), key=repr
        )


def cover(found: Factors, seed: int = 0) -> Coverage:
    """The array, validated, then repair configurations for the pairs its valid rows leave
    uncovered: each starts from a completion of one such pair and packs in as many further
    uncovered pairs as still validate (greedy, in a fixed order). A pair no completion covers
    is ``infeasible``."""
    rows = pairwise(found, seed)
    result = Coverage(rows, [check(row) for row in rows])
    seen = covered(result.valid, found)
    wanted = sorted(every_pair(found) - seen, key=repr)
    while wanted:
        first = wanted.pop(0)
        a, va, b, vb = first
        fixed: dict[str, Any] = {a: va, b: vb}
        row = complete(first, found)
        if row is None:
            result.infeasible.append(first)
            continue
        for pair in wanted:
            c_, vc, d, vd = pair
            if fixed.get(c_, vc) != vc or fixed.get(d, vd) != vd:
                continue
            trial = _completed(row, {**fixed, c_: vc, d: vd})
            if trial is not None:
                row = trial
                fixed.update({c_: vc, d: vd})
        result.rows.append(row)
        result.checked.append(check(row))
        result.repairs += 1
        gained = pairs_of(row, found)
        seen |= gained
        wanted = [p for p in wanted if p not in gained]
    return result


def summary(found: FactorSet, result: Coverage) -> dict[str, Any]:
    """The run summary C-17 writes: the factors covered and held, the array's size, how many
    were valid, refused (with a reason) or repairs, and the infeasible pairs."""
    return {
        "factors": {name: list(levels) for name, levels in found.factors.items()},
        "held_at_default": list(found.held),
        "configurations": len(result.rows),
        "valid": len(result.valid),
        "refused": sum(1 for c in result.checked if c.refused_for_a_reason),
        "repairs": result.repairs,
        "infeasible": [list(p) for p in result.infeasible],
        "uncovered_valid": [list(p) for p in result.uncovered_valid(found.factors)],
    }


def lost_pairs(rows: Sequence[Row], found: Factors) -> set[Pair]:
    """``every_pair`` minus what ``rows`` cover (empty for a covering array)."""
    return every_pair(found) - covered(rows, found)


__all__ = [
    "CAPABILITY_DIALS",
    "SIZE_MAX",
    "Checked",
    "Coverage",
    "FactorSet",
    "check",
    "complete",
    "cover",
    "covered",
    "document",
    "every_pair",
    "factors",
    "lost_pairs",
    "pairs_of",
    "pairwise",
    "summary",
]
