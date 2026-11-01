"""A scenario's knowledge as propositions: the seed reader that structured genesis and the
publish-time seed checks share (FR-SCN-5, FR-PROP-12; docs/02 §4.10; #149). Pure: nothing here
reads the database or calls a model.

A ``SeedWorld`` is what a seed is read against: a registry (the core vocabulary and the
scenario's author predicates, ``AuthorLayer.registry``) and an entity index (the cast, the
world's places and entities, and at publish a placeholder for each role no character fills
yet). ``read_seed`` runs #148's ``detect_seed`` over the text with the holder named in the
context window, so a third-person seed ("She keeps the deed") binds its pronoun to the holder,
and answers:

- ``propositions``: each normalized claim (#140's ``normalize``). A seed states background
  knowledge, so a claim that names no time of its own holds from an open start (``[-inf, end)``)
  rather than from position 0;
- ``unresolved``: the detail code of each claim whose entity did not resolve (an alias the
  character dropped: ``unknown_name``). A ``canon`` seed with one cannot be canon;
- ``tolerated``: how many claims stayed ``uncertain`` for another reason (an unregistered
  relation in free text, an unparsed quantity), which a seed may hold.

``seed_facts`` turns a reading into #140's ``SeedFact`` inputs of ``merge_layers``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from epistrel.core.propositions.model import Instant, Interval, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.resolution.entities import (
    ContextBeat,
    EntityEntry,
    EntityIndex,
    EntitySpec,
    ResolutionContext,
)
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.verify.detect import DetectionEnv, detect_seed
from epistrel.core.verify.merge import SeedFact, SeedLayer
from epistrel.core.verify.rules import VerdictReason
from epistrel.core.world_clock import clock_off_basis

CANON = "canon"
"""A seed's default kind: the author's statement of fact."""


def entry_of(entity_id: str, spec: EntitySpec) -> EntityEntry:
    """``spec`` as the resolver's index holds it, under ``entity_id``."""
    return EntityEntry(
        id=entity_id,
        kind=spec.kind,
        canonical_name=spec.canonical_name,
        gender=spec.gender,
        descriptors=spec.descriptors,
        character_id=spec.character_id,
        aliases=spec.aliases,
    )


@dataclass(frozen=True, slots=True)
class SeedWorld:
    """What seeds are read against (see the module docstring)."""

    registry: Registry
    index: EntityIndex
    basis: TimeBasis = field(default_factory=clock_off_basis)

    @classmethod
    def of(cls, registry: Registry, entities: Iterable[tuple[str, EntitySpec]]) -> SeedWorld:
        return cls(registry, EntityIndex([entry_of(i, s) for i, s in entities]))

    def env(self, holder: str | None) -> DetectionEnv:
        """The detection environment of a seed held by entity ``holder`` (``None``: the world):
        the holder is the one mention in the context window."""
        entry = None if holder is None else self.index.get(holder)
        beats = () if entry is None else (ContextBeat(entry.canonical_name, holder),)
        context = ResolutionContext(index=self.index, beats=beats)
        return DetectionEnv(
            registry=self.registry, basis=self.basis, context_for=lambda _speaker: context
        )


@dataclass(frozen=True, slots=True)
class SeedReading:
    """One seed's text as propositions (see the module docstring)."""

    propositions: tuple[Proposition, ...] = ()
    unresolved: tuple[str, ...] = ()
    tolerated: int = 0


def _background(proposition: Proposition, timed: bool) -> Proposition:
    """A claim with no time expression of its own holds from an open start."""
    valid = proposition.valid_time
    if timed or isinstance(valid, Instant) or not isinstance(valid, Interval):
        return proposition
    if valid.start is None:
        return proposition
    return proposition.model_copy(update={"valid_time": Interval(start=None, end=valid.end)})


def read_seed(text: str, world: SeedWorld, *, holder: str | None = None) -> SeedReading:
    """``text`` read against ``world`` with ``holder`` (an entity id) as the seed's holder."""
    detection = detect_seed(text, world.env(holder))
    found: list[Proposition] = []
    unresolved: list[str] = []
    tolerated = 0
    for claim in detection.claims:
        if claim.proposition is not None:
            timed = claim.sketch is not None and bool(claim.sketch.time_text)
            proposition = _background(claim.proposition, timed)
            if proposition not in found:
                found.append(proposition)
        elif claim.unnormalized is not None:
            if claim.unnormalized.reason is VerdictReason.UNRESOLVED_ENTITY:
                unresolved.append(claim.unnormalized.detail or "unresolved_entity")
            else:
                tolerated += 1
    return SeedReading(tuple(found), tuple(unresolved), tolerated)


def seed_facts(
    seed_id: str, layer: SeedLayer, propositions: Sequence[Proposition]
) -> list[SeedFact]:
    """One ``SeedFact`` per proposition, each under ``seed_id`` with its index when there are
    several (``<seed id>#<n>``), so a merge report names the seed."""
    if len(propositions) == 1:
        return [SeedFact(seed_id, layer, proposition=propositions[0])]
    return [SeedFact(f"{seed_id}#{n}", layer, proposition=p) for n, p in enumerate(propositions)]


def seed_of(fact_id: str) -> str:
    """The seed a ``SeedFact`` id names (its ``#<n>`` suffix dropped)."""
    return fact_id.split("#", 1)[0]


__all__ = [
    "CANON",
    "SeedReading",
    "SeedWorld",
    "entry_of",
    "read_seed",
    "seed_facts",
    "seed_of",
]
