"""Likely duplicates among a turn's new places and objects (FR-PROP-7; #481).

Names that refer to one new place or object but share no resolvable word ("the tavern", then
"the inn") are minted as two entities (``core.verify.introduce``). The engine does not merge
them on a guess: the turn report carries a **duplicate-candidate notice** naming both ids, and
the author merges them (``POST /stories/{id}/entities/merge``, an amendment with the normal
cascade) or leaves them.

A new entity is a candidate when (``candidates``):

- it is a **place** minted while the scene's location is untracked (the acting character has
  no current ``located_at``: ``untracked_scene_location``, naming the new id only) or generic
  (its name's descriptors are all generic place words: ``generic_scene_location``, naming the
  scene's place);
- or its descriptors **overlap** an existing entity's of the same kind, a place, an object or
  an ``event`` (``descriptor_overlap``: "the inn" and "Crossroads Inn"; "the errand" and "the
  bank errand").

A notice only: nothing is merged.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.propositions.vocabulary import LOCATED_AT
from epistrel.core.resolution.entities import descriptors_of
from epistrel.core.tables import entities
from epistrel.core.verify.introduce import MINTED_KINDS

GENERIC_PLACES = frozenset(
    {
        "place",
        "room",
        "area",
        "spot",
        "somewhere",
        "house",
        "building",
        "street",
        "road",
        "town",
        "city",
        "outside",
        "inside",
        "here",
        "there",
    }
)
"""Place words that locate a scene without naming it."""


@dataclass(frozen=True, slots=True)
class Known:
    """An entity as the rule reads it: its id, kind and name."""

    id: str
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A duplicate-candidate notice: the new entity, the one it may be (``None`` for an
    untracked scene location) and why."""

    new_id: str
    existing_id: str | None
    reason: str

    def report(self) -> dict[str, Any]:
        return {
            "kind": "duplicate_candidate",
            "entity_id": self.new_id,
            "candidate_id": self.existing_id,
            "reason": self.reason,
        }


def candidates(
    new: Sequence[Known], existing: Sequence[Known], scene: Known | None
) -> list[Candidate]:
    """The notices for ``new`` against ``existing`` (each pair once) and the scene's place
    (see the module docstring). Pure."""
    found: list[Candidate] = []
    pairs: set[frozenset[str]] = set()
    for entity in new:
        if entity.kind not in MINTED_KINDS:
            continue
        if entity.kind == "place":
            if scene is None:
                found.append(Candidate(entity.id, None, "untracked_scene_location"))
            elif scene.id != entity.id and set(descriptors_of(scene.name)) <= GENERIC_PLACES:
                found.append(Candidate(entity.id, scene.id, "generic_scene_location"))
        words = set(descriptors_of(entity.name))
        for other in existing:
            if other.id == entity.id or other.kind != entity.kind:
                continue
            pair = frozenset({entity.id, other.id})
            if pair in pairs or not words & set(descriptors_of(other.name)):
                continue
            pairs.add(pair)
            found.append(Candidate(entity.id, other.id, "descriptor_overlap"))
    return found


async def _known(conn: AsyncConnection, story_id: str, ids: Iterable[str] | None) -> list[Known]:
    e = entities.c
    query = select(e.id, e.kind, e.canonical_name).where(
        e.story_id == story_id, e.merged_into.is_(None), e.kind.in_(sorted(MINTED_KINDS))
    )
    if ids is not None:
        query = query.where(e.id.in_(list(ids)))
    return [Known(str(r.id), str(r.kind), str(r.canonical_name)) for r in await conn.execute(query)]


async def scene_place(conn: AsyncConnection, story_id: str, character_id: str) -> Known | None:
    """The acting character's current place in canon (``None``: untracked)."""
    from epistrel.core.canon import facts as canon

    e = entities.c
    entity_id = await conn.scalar(
        select(e.id).where(e.story_id == story_id, e.character_id == character_id)
    )
    if entity_id is None:
        return None
    held = await canon.canon(conn, story_id, subject=str(entity_id), predicate=LOCATED_AT)
    current = [f for f in held if f.valid_to is None and f.proposition.object_id is not None]
    if not current:
        return None
    place = str(current[-1].proposition.object_id)
    row = (
        await conn.execute(select(e.id, e.kind, e.canonical_name).where(e.id == place))
    ).one_or_none()
    return None if row is None else Known(str(row.id), str(row.kind), str(row.canonical_name))


async def find(
    conn: AsyncConnection, story_id: str, new_ids: Sequence[str], *, character_id: str | None
) -> list[dict[str, Any]]:
    """The turn report's duplicate-candidate notices for the entities the turn created."""
    if not new_ids:
        return []
    new = await _known(conn, story_id, new_ids)
    existing = await _known(conn, story_id, None)
    scene = None if character_id is None else await scene_place(conn, story_id, character_id)
    return [c.report() for c in candidates(new, existing, scene)]


__all__ = ["GENERIC_PLACES", "Candidate", "Known", "candidates", "find", "scene_place"]
