"""The fact merge by seed layer at structured genesis (FR-SCN-7, FR-PROP-10; U-27 M4; #140).

A structured story's seeds are normalized propositions and closed sets in three layers, highest
first: the scenario's **world**, its **roles**, then each character's own fields (background
flavor). ``merge_layers`` takes every seed in that order and checks it against the seeds
already kept, with ``rules.check`` in both directions (the new seed as a claim over the kept
ones, and each kept one over the new seed). A closed set conflicts with another closed set of
the same key over overlapping time that lists different members.

- A seed **contradicted** by a kept seed of a higher layer is dropped entirely and reported
  with the winner and the rule that decided it ("only child" loses to the role's "has a
  sister": rule ``closed_set``; the world's "the father is dead" beats a role's "alive":
  ``exclusion``).
- One contradicted by a kept seed of its **own** layer loses to document order, and the rule
  is reported as ``same_layer_order``.
- A pair the rules leave ``uncertain`` (approximate bounds, world knowledge, weak semantics)
  keeps both seeds and is reported as ``merge_uncertain``.

#149 calls it over the whole normalized seed set before any ``commit_fact`` and maps the
report to its ``merge_conflict`` entries. Pure: nothing here reads the database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from epistrel.core.propositions.model import Proposition, overlaps
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import SAME_AS
from epistrel.core.verify.rules import (
    ClosedSetRecord,
    Options,
    Record,
    Verdict,
    VerdictKind,
    VerdictReason,
    check,
)


class SeedLayer(StrEnum):
    WORLD = "world"
    ROLE = "role"
    CHARACTER = "character"


LAYER_ORDER: tuple[SeedLayer, ...] = (SeedLayer.WORLD, SeedLayer.ROLE, SeedLayer.CHARACTER)
"""Highest first: a higher layer's seed wins a conflict."""

SAME_LAYER_ORDER = "same_layer_order"
CONFLICTING_UNCERTAIN = frozenset(
    {
        VerdictReason.APPROXIMATE_BOUNDS,
        VerdictReason.WORLD_KNOWLEDGE,
        VerdictReason.WEAK_SEMANTICS,
    }
)
"""The ``uncertain`` reasons that leave a possible conflict between two seeds."""


@dataclass(frozen=True, slots=True)
class SeedFact:
    """One normalized seed: a proposition or a closed set, with its id and layer."""

    seed_id: str
    layer: SeedLayer
    proposition: Proposition | None = None
    closed_set: ClosedSetRecord | None = None

    def __post_init__(self) -> None:
        if (self.proposition is None) == (self.closed_set is None):
            raise ValueError("a seed fact is a proposition or a closed set")


@dataclass(frozen=True, slots=True)
class MergeConflict:
    """A conflict the merge decided: ``winner`` kept, ``loser`` dropped (``outcome =
    dropped``), or both kept when the rules could not decide (``merge_uncertain``)."""

    winner: SeedFact
    loser: SeedFact
    rule: str
    outcome: Literal["dropped", "merge_uncertain"]
    verdict: Verdict | None = None


@dataclass(frozen=True, slots=True)
class MergeResult:
    kept: tuple[SeedFact, ...]
    dropped: tuple[MergeConflict, ...]
    uncertain: tuple[MergeConflict, ...]


def _record(seed: SeedFact) -> Record | None:
    if seed.proposition is None:
        return None
    return Record(seed.proposition, seed.seed_id)


def _closed_conflict(a: ClosedSetRecord, b: ClosedSetRecord) -> bool:
    if (a.predicate_id, a.subject_id, dict(a.key)) != (b.predicate_id, b.subject_id, dict(b.key)):
        return False
    if set(a.members) == set(b.members):
        return False
    return overlaps(a.interval(), b.interval()) is not False


def _verdict_between(
    claim: SeedFact,
    other: SeedFact,
    context: Sequence[Record],
    registry: Registry,
    options: Options,
) -> Verdict | None:
    """``claim`` checked over ``other`` (and the shared context); ``None`` when ``claim`` is a
    closed set (closed sets are checked from the other side)."""
    if claim.proposition is None:
        return None
    records = [r for r in (_record(other),) if r is not None]
    closed = [] if other.closed_set is None else [replace(other.closed_set, id=other.seed_id)]
    return check(
        claim.proposition,
        [*records, *context],
        registry=registry,
        closed_sets=closed,
        options=options,
    )


def _conflict(
    seed: SeedFact,
    kept: SeedFact,
    context: Sequence[Record],
    registry: Registry,
    options: Options,
) -> tuple[str, Verdict | None] | Literal["uncertain"] | None:
    """The rule a conflict between ``seed`` and ``kept`` rests on, ``"uncertain"`` when the
    rules cannot decide, ``None`` when they agree."""
    if seed.closed_set is not None and kept.closed_set is not None:
        return ("closed_set", None) if _closed_conflict(seed.closed_set, kept.closed_set) else None
    doubtful = False
    for claim, other in ((seed, kept), (kept, seed)):
        verdict = _verdict_between(claim, other, context, registry, options)
        if verdict is None:
            continue
        if verdict.kind is VerdictKind.CONTRADICTED and other.seed_id in verdict.cited():
            rule = sorted(r.value for r in verdict.rules)[0]
            return rule, verdict
        if verdict.kind is VerdictKind.UNCERTAIN and verdict.reason in CONFLICTING_UNCERTAIN:
            doubtful = True
    return "uncertain" if doubtful else None


def merge_layers(
    seeds_by_layer: Mapping[SeedLayer, Sequence[SeedFact]],
    *,
    registry: Registry,
    options: Options = Options(),  # noqa: B008 - frozen
) -> MergeResult:
    """Merge the seeds layer by layer (see the module docstring)."""
    kept: list[SeedFact] = []
    dropped: list[MergeConflict] = []
    undecided: list[MergeConflict] = []
    rank = {layer: i for i, layer in enumerate(LAYER_ORDER)}
    containment = {
        p.containment_of for p in registry.predicates.values() if p.containment_of is not None
    }
    for layer in LAYER_ORDER:
        for seed in seeds_by_layer.get(layer, ()):
            context = [
                Record(k.proposition, k.seed_id)
                for k in kept
                if k.proposition is not None
                and (
                    k.proposition.predicate_id == SAME_AS
                    or k.proposition.predicate_id in containment
                )
            ]
            lost: MergeConflict | None = None
            for other in kept:
                found = _conflict(seed, other, context, registry, options)
                if found is None:
                    continue
                if found == "uncertain":
                    undecided.append(MergeConflict(other, seed, "uncertain", "merge_uncertain"))
                    continue
                rule, verdict = found
                if rank[other.layer] == rank[seed.layer]:
                    rule = SAME_LAYER_ORDER
                lost = MergeConflict(other, seed, rule, "dropped", verdict)
                break
            if lost is None:
                kept.append(seed)
            else:
                dropped.append(lost)
    return MergeResult(tuple(kept), tuple(dropped), tuple(undecided))


__all__ = [
    "LAYER_ORDER",
    "SAME_LAYER_ORDER",
    "MergeConflict",
    "MergeResult",
    "SeedFact",
    "SeedLayer",
    "merge_layers",
]
