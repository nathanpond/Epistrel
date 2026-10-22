"""``fact_neighborhood``: the dependency cluster around an entity or a fact (FR-VERIFY-6; #142).

A repair (#151) and a reconciliation (#154) must not fix one fact and leave the facts that hang
on it contradicting the fix. ``fact_neighborhood(lookup, story_id, seed, depth=2)`` returns what
hangs together with the seed, as one flat list of ``NeighborhoodItem``s, each labelled with the
``edge`` that reached it and the ``depth`` it was reached at:

- ``seed``: the seed fact itself (a fact seed). A seed fact that is superseded, inactive or
  hidden from the viewpoint has no neighborhood (the list is empty).
- ``predicate``: facts with the seed fact's predicate about the seed's entities (same subject
  and predicate, which includes the same uniqueness key).
- ``implication``: facts whose predicate a declared implication links with the seed fact's, in
  either direction (what it implies and what implies it), and the rule's join predicate
  (#131's ``Implication.via``).
- ``closed_set``: asserted closed sets of those predicates for the seed's entities.
- ``subject`` / ``object``: every other fact that has an entity of the frontier as its subject,
  or as its object (shared subject, both directions). At depth 1 the frontier is the seed's
  entities (the seed fact's subject and objects); each further depth follows the entities those
  facts name, to ``depth``.
- ``projection``: the relationship, placement and inventory rows (#137's structured
  projections, read through ``KeyedLookup.profile``) of the seed's entities, each with the fact
  it comes from.
- ``memory``: the active memories that wrap one of the collected facts' propositions
  (``proposition_ids``), then those that come from one of their source events
  (``source_event_ids``).

**What is read.** Canon goes through ``KeyedLookup.canon_rows`` (current, active versions only,
as the viewpoint may read them: a superseded, dormant or discarded fact is never in it) and the
projections through ``KeyedLookup.profile``; both are recorded as retrieved rows of the read set
(context, never query keys). The neighbors are read as lean links (``KeyedLookup.canon_links``:
the same selection and order, ids and the columns ranking and the frontier need), every one of
them ranked, and only the facts the cap keeps are then decoded in full, in one
``canon_rows(ids=...)`` (#470: a character with hundreds of facts no longer decodes them all).
This module is a ``KEYED_READERS`` member. Memories are filtered by the same visibility rule as
canon, and read only when the cap leaves room for one (they rank last).

**Bounded.** At most ``max_facts`` items (``neighborhood_max_facts``, deployment setting,
default 50), kept in this priority: the seed; same subject and predicate; implications; closed
sets; other facts on a shared subject; projections; memories. Within a class, a lower depth
comes first, then the order the reads returned them in (canon by valid-time, then id; memories
linked by proposition before those linked by source event). ``truncated`` says whether anything
was left out.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from epistrel.core.canon.facts import Fact, FactLink, visible_to
from epistrel.core.errors import StoryEntityNotFound
from epistrel.core.memory.records import Memory, memories_linked
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.readset import KeyedLookup
from epistrel.core.settings_schema import DEFAULT_NEIGHBORHOOD_MAX_FACTS, OMNISCIENT
from epistrel.core.tables import stories
from epistrel.core.verify.rules import ClosedSetRecord, closed_sets_for

if TYPE_CHECKING:
    from epistrel.core.projections.structured import ProfileView

log = logging.getLogger(__name__)

DEFAULT_DEPTH = 2
MAX_DEPTH = 5


class Edge(StrEnum):
    """How an item was reached from the seed (see the module docstring)."""

    SEED = "seed"
    PREDICATE = "predicate"
    IMPLICATION = "implication"
    CLOSED_SET = "closed_set"
    SUBJECT = "subject"
    OBJECT = "object"
    PROJECTION = "projection"
    MEMORY = "memory"


PRIORITY: dict[Edge, int] = {
    Edge.SEED: 0,
    Edge.PREDICATE: 1,
    Edge.IMPLICATION: 2,
    Edge.CLOSED_SET: 3,
    Edge.SUBJECT: 4,
    Edge.OBJECT: 4,
    Edge.PROJECTION: 5,
    Edge.MEMORY: 6,
}
"""The truncation order: a lower number is kept first."""

ItemKind = Literal["fact", "closed_set", "projection", "memory"]


@dataclass(frozen=True, slots=True)
class Seed:
    """What a neighborhood is built around: an entity, or one fact version."""

    entity_id: str | None = None
    fact_id: str | None = None

    def __post_init__(self) -> None:
        if (self.entity_id is None) == (self.fact_id is None):
            raise ValueError("a neighborhood seed is one entity or one fact")

    @classmethod
    def entity(cls, entity_id: str) -> Seed:
        return cls(entity_id=entity_id)

    @classmethod
    def fact(cls, fact_id: str) -> Seed:
        return cls(fact_id=fact_id)

    def as_payload(self) -> dict[str, str]:
        if self.fact_id is not None:
            return {"fact_id": self.fact_id}
        return {"entity_id": str(self.entity_id)}


@dataclass(frozen=True, slots=True)
class NeighborhoodItem:
    """One member of a neighborhood: a fact, a closed set, a projection row (``projection``
    names it: ``relationship``, ``placement`` or ``inventory``; ``source_fact_id`` the fact
    behind it) or a memory."""

    kind: ItemKind
    id: str
    edge: Edge
    depth: int
    fact: Fact | None = None
    closed_set: ClosedSetRecord | None = None
    memory: Memory | None = None
    projection: str | None = None
    entity_id: str | None = None
    source_fact_id: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """Ids and labels only, no story text (invariant 3)."""
        found: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "edge": self.edge.value,
            "depth": self.depth,
        }
        if self.fact is not None:
            found |= {
                "subject_id": self.fact.subject_id,
                "predicate_id": self.fact.predicate_id,
                "proposition_id": self.fact.proposition_id,
            }
        if self.projection is not None:
            found |= {"projection": self.projection, "entity_id": self.entity_id}
        if self.source_fact_id is not None:
            found["source_fact_id"] = self.source_fact_id
        return found


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """``fact_neighborhood``'s answer (see the module docstring)."""

    seed: Seed
    depth: int
    items: tuple[NeighborhoodItem, ...] = ()
    truncated: bool = False

    def facts(self) -> list[Fact]:
        """The facts, in item order."""
        return [i.fact for i in self.items if i.fact is not None]

    def ids(self, kind: ItemKind) -> list[str]:
        return [i.id for i in self.items if i.kind == kind]

    def as_payload(self) -> dict[str, Any]:
        return {
            "seed": self.seed.as_payload(),
            "depth": self.depth,
            "items": [i.as_payload() for i in self.items],
            "truncated": self.truncated,
        }


@dataclass
class _Collector:
    found: dict[str, tuple[int, int, int, NeighborhoodItem]] = field(default_factory=dict)
    order: int = 0

    def add(self, item: NeighborhoodItem) -> None:
        rank = (PRIORITY[item.edge], item.depth)
        key = f"{item.kind}:{item.id}"
        held = self.found.get(key)
        if held is not None and held[:2] <= rank:
            return
        self.order += 1
        self.found[key] = (*rank, self.order, item)

    def fact_ids(self) -> list[str]:
        return [i.id for *_, i in self.found.values() if i.kind == "fact"]

    def items(self) -> list[NeighborhoodItem]:
        return [entry[3] for entry in sorted(self.found.values(), key=lambda e: e[:3])]


def _entities_of(fact: Fact) -> list[str]:
    p = fact.proposition
    found = [fact.subject_id]
    if p.object_id is not None:
        found.append(p.object_id)
    found += list(p.object_set or ())
    return found


def _entities_of_link(link: FactLink) -> list[str]:
    """``_entities_of`` for a lean link: the same entities, in the same order."""
    found = [link.subject_id]
    if link.object_id is not None:
        found.append(link.object_id)
    found += list(link.object_set)
    return found


def _linked_predicates(registry: Registry, predicates: Iterable[str]) -> set[str]:
    """The predicates a declared implication links with ``predicates``, both ways, and the
    rules' join predicates."""
    wanted = set(predicates)
    linked: set[str] = set()
    for rule in registry.rules():
        if rule.when in wanted or rule.then in wanted:
            linked |= {rule.when, rule.then}
            if rule.via is not None:
                linked.add(rule.via)
    return linked - wanted


def _projection_items(view: ProfileView, depth: int) -> list[NeighborhoodItem]:
    entity = view.entity.entity_id
    found: list[NeighborhoodItem] = []
    for r in view.relationships:
        found.append(
            NeighborhoodItem(
                "projection",
                f"relationship:{entity}:{r.predicate_id}:{r.other.entity_id}:{r.source_fact_id}",
                Edge.PROJECTION,
                depth,
                projection="relationship",
                entity_id=entity,
                source_fact_id=r.source_fact_id,
            )
        )
    if view.placement is not None:
        found.append(
            NeighborhoodItem(
                "projection",
                f"placement:{entity}:{view.placement.source_fact_id}",
                Edge.PROJECTION,
                depth,
                projection="placement",
                entity_id=entity,
                source_fact_id=view.placement.source_fact_id,
            )
        )
    for item in view.inventory:
        found.append(
            NeighborhoodItem(
                "projection",
                f"inventory:{entity}:{item.item.entity_id}:{item.source_fact_id}",
                Edge.PROJECTION,
                depth,
                projection="inventory",
                entity_id=entity,
                source_fact_id=item.source_fact_id,
            )
        )
    return found


async def fact_neighborhood(
    lookup: KeyedLookup,
    story_id: str,
    seed: Seed,
    *,
    depth: int = DEFAULT_DEPTH,
    viewpoint: str = OMNISCIENT,
    through_position: tuple[int, int] | None = None,
    max_facts: int = DEFAULT_NEIGHBORHOOD_MAX_FACTS,
    registry: Registry | None = None,
) -> Neighborhood:
    """The dependency cluster around ``seed`` as ``viewpoint`` may read it, through
    ``through_position`` when given (see the module docstring)."""
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"a neighborhood's depth is 1 to {MAX_DEPTH}")
    if max_facts < 1:
        raise ValueError("a neighborhood holds at least one item")
    registry = registry or await load(lookup.conn, story_id)
    out = _Collector()

    async def rows(**filters: Any) -> list[Fact]:
        return await lookup.canon_rows(
            story_id, viewpoint=viewpoint, through_position=through_position, **filters
        )

    seed_predicates: set[str] = set()
    if seed.fact_id is not None:
        found = await rows(ids=[seed.fact_id])
        if not found:
            return Neighborhood(seed, depth)
        (fact,) = found
        out.add(NeighborhoodItem("fact", fact.id, Edge.SEED, 0, fact=fact))
        start = _entities_of(fact)
        seed_predicates = {fact.predicate_id}
    else:
        start = [str(seed.entity_id)]
    linked = _linked_predicates(registry, seed_predicates)

    def edge_of(predicate_id: str, level: int, direction: Edge) -> Edge:
        if level == 1 and predicate_id in seed_predicates and direction is Edge.SUBJECT:
            return Edge.PREDICATE
        if predicate_id in linked:
            return Edge.IMPLICATION
        return direction

    async def links(**filters: Any) -> list[FactLink]:
        return await lookup.canon_links(
            story_id, viewpoint=viewpoint, through_position=through_position, **filters
        )

    # Every neighbor is ranked from its lean link (#470): the frontier, the closed sets'
    # predicates and the memories' links need them all, but only the kept ones are decoded.
    found_links: dict[str, FactLink] = {}
    frontier = list(dict.fromkeys(start))
    visited: set[str] = set()
    first_level: list[FactLink] = []
    for level in range(1, depth + 1):
        following: list[str] = []
        for entity in frontier:
            if entity in visited:
                continue
            visited.add(entity)
            for link in await links(subject=entity):
                found_links.setdefault(link.id, link)
                out.add(
                    NeighborhoodItem(
                        "fact", link.id, edge_of(link.predicate_id, level, Edge.SUBJECT), level
                    )
                )
                following += _entities_of_link(link)
                if level == 1:
                    first_level.append(link)
            for link in await links(object=entity):
                found_links.setdefault(link.id, link)
                out.add(
                    NeighborhoodItem(
                        "fact", link.id, edge_of(link.predicate_id, level, Edge.OBJECT), level
                    )
                )
                following.append(link.subject_id)
        frontier = [e for e in dict.fromkeys(following) if e not in visited]
        if not frontier:
            break

    keys = set(start)
    predicates = seed_predicates | linked | {f.predicate_id for f in first_level}
    for predicate_id in sorted(predicates):
        for closed in await closed_sets_for(lookup.conn, story_id, predicate_id):
            if closed.subject_id in keys:
                out.add(
                    NeighborhoodItem("closed_set", closed.id, Edge.CLOSED_SET, 1, closed_set=closed)
                )

    as_of = None if through_position is None else through_position[0]
    if as_of is not None:
        head = await lookup.conn.scalar(
            select(stories.c.next_turn_index).where(stories.c.id == story_id)
        )
        if head is not None and as_of > int(head) - 1 >= 0:
            # The turn being produced is past the head: its projections are the head's (#148).
            as_of = None
    for entity in dict.fromkeys(start):
        try:
            view = await lookup.profile(
                story_id,
                entity,
                as_of=as_of,
                viewpoint=viewpoint,
            )
        except StoryEntityNotFound:
            continue  # not one of the story's entities (a stale id): no projections
        for item in _projection_items(view, 1):
            out.add(item)

    def links_of(item: NeighborhoodItem) -> tuple[str, Sequence[str]]:
        if item.fact is not None:
            return item.fact.proposition_id, item.fact.source_event_ids
        link = found_links[item.id]
        return link.proposition_id, link.source_event_ids

    ranked = out.items()
    if len(ranked) <= max_facts:
        # Memories rank last: with more items than the cap already, none could be kept (and
        # the neighborhood is truncated either way), so they are read only when there is room.
        collected = [links_of(i) for i in ranked if i.kind == "fact"]
        memories = await memories_linked(
            lookup.conn,
            story_id,
            proposition_ids=_unique(p for p, _ in collected),
            source_event_ids=_unique(e for _, events in collected for e in events),
            limit=max_facts,
        )
        for memory in memories:
            if visible_to(memory.visibility, viewpoint):
                out.add(NeighborhoodItem("memory", memory.id, Edge.MEMORY, 1, memory=memory))

    items = out.items()
    kept = await _decoded(items[:max_facts], rows)
    log.debug("neighborhood: %d items, %d kept", len(items), len(kept))
    return Neighborhood(seed, depth, kept, truncated=len(items) > max_facts)


async def _decoded(
    kept: list[NeighborhoodItem],
    rows: Callable[..., Awaitable[list[Fact]]],
) -> tuple[NeighborhoodItem, ...]:
    """``kept`` with each fact decoded in full, read in one statement (#470). A fact a
    concurrent commit closed between the reads (a read-committed caller; a repeatable-read
    snapshot never sees one) is left out, as a read after that commit would have."""
    wanted = [i.id for i in kept if i.kind == "fact" and i.fact is None]
    decoded = {f.id: f for f in await rows(ids=wanted)} if wanted else {}
    return tuple(
        replace(i, fact=decoded[i.id]) if i.kind == "fact" and i.fact is None else i
        for i in kept
        if i.kind != "fact" or i.fact is not None or i.id in decoded
    )


def _unique(values: Iterable[str]) -> Sequence[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "DEFAULT_DEPTH",
    "MAX_DEPTH",
    "PRIORITY",
    "Edge",
    "Neighborhood",
    "NeighborhoodItem",
    "Seed",
    "fact_neighborhood",
]
