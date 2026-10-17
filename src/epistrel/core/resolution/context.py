"""Loading what a resolution reads, once per operation (FR-PROP-6,9, FR-RET-2; #139).

The resolvers (``entities``, ``time``, ``events``) are pure; this module reads their inputs on
the operation's connection:

- ``load_index``: the story's entities and aliases as an ``EntityIndex`` (the per-story alias
  map, loaded per operation);
- ``load_time_basis``: ``stories.story_minute_zero`` and ``story_epoch`` as a ``TimeBasis``;
- ``load_context``: the context window (the last ``CONTEXT_BEATS`` beats at or before the
  position, #135's ``working_memory`` as the speaker perceives them), the player character,
  the scene's entities present and the relationships of the story's people (#137's
  projections, omniscient: no visibility filter on candidates in M4), read through
  ``KeyedLookup.scene``/``profile`` (#137's carried rule: a turn-time projection read is
  recorded in the read set, as context);
- ``subject_timeline``: the subject's ``born`` and ``age`` canon through #136's
  ``KeyedLookup`` (so a verdict resting on "when he was seven" regenerates when they change).
  This module is a keyed reader (``core.readset.KEYED_READERS``): it never reads ``facts`` or
  ``propositions`` directly.

Nothing here calls a model.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.errors import InvalidRequest
from epistrel.core.memory.records import working_memory
from epistrel.core.propositions.model import Instant, Interval, Polarity
from epistrel.core.propositions.vocabulary import AGE, BORN
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution.entities import (
    CONTEXT_BEATS,
    NARRATOR,
    PLAYER,
    ContextBeat,
    EntityEntry,
    EntityIndex,
    Relation,
    ResolutionContext,
)
from epistrel.core.resolution.time import Position, SubjectTimeline, TimeBasis
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import characters, entities, entity_aliases, stories


async def load_index(conn: AsyncConnection, story_id: str) -> EntityIndex:
    """The story's ``EntityIndex``."""
    aliases: dict[str, list[tuple[str, str]]] = {}
    for row in await conn.execute(
        select(entity_aliases.c.entity_id, entity_aliases.c.alias, entity_aliases.c.source)
        .where(entity_aliases.c.story_id == story_id)
        .order_by(entity_aliases.c.id)
    ):
        aliases.setdefault(str(row.entity_id), []).append((row.alias, row.source))
    rows = await conn.execute(
        select(entities).where(entities.c.story_id == story_id).order_by(entities.c.id)
    )
    return EntityIndex(
        EntityEntry(
            id=str(r.id),
            kind=r.kind,
            canonical_name=r.canonical_name,
            gender=r.gender,
            descriptors=tuple(r.descriptors or ()),
            character_id=None if r.character_id is None else str(r.character_id),
            aliases=tuple(aliases.get(str(r.id), ())),
        )
        for r in rows
    )


async def load_time_basis(conn: AsyncConnection, story_id: str) -> TimeBasis:
    """The story's ``TimeBasis`` (M4's stand-in for the basis)."""
    row = (
        await conn.execute(
            select(stories.c.story_minute_zero, stories.c.story_epoch).where(
                stories.c.id == story_id
            )
        )
    ).one()
    return TimeBasis(minute_zero=int(row.story_minute_zero), epoch=row.story_epoch)


async def load_context(
    conn: AsyncConnection,
    story_id: str,
    *,
    position: Position,
    speaker: str,
    index: EntityIndex | None = None,
    lookup: KeyedLookup | None = None,
) -> ResolutionContext:
    """The ``ResolutionContext`` of a text spoken by ``speaker`` (an entity id, ``narrator`` or
    ``player``) at ``position`` (see the module docstring). The scene and the relationships are
    read through ``lookup`` (#136's ``KeyedLookup``: recorded in its read set as retrieved
    rows), or through a lookup of a read set of its own when none is given."""
    lookup = lookup or KeyedLookup(conn, StructuredReadSet())
    index = index or await load_index(conn, story_id)
    player = await conn.scalar(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.is_player.is_(True)
        )
    )
    player_id = None if player is None else str(player)
    perceiver = player_id if speaker == PLAYER else None if speaker == NARRATOR else speaker
    if perceiver is not None and index.get(perceiver) is None:
        perceiver = None
    beats = [
        ContextBeat(b.text, b.character_id)
        for b in await working_memory(conn, story_id, perceiver, position, CONTEXT_BEATS)
    ]
    turn = position if isinstance(position, int) else position[0]
    head = (
        int(
            await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
            or 0
        )
        - 1
    )
    present: frozenset[str] | None = None
    relations: list[Relation] = []
    if head >= 0:
        as_of = min(max(turn, 0), head)
        try:
            scene = await lookup.scene(story_id, as_of=as_of, viewpoint=OMNISCIENT)
        except InvalidRequest:
            scene = None
        if scene is not None and scene.present:
            present = frozenset(e.entity_id for e in scene.present)
        seen: set[tuple[str, str, str]] = set()
        for entry in index.entries.values():
            if entry.kind != "person":
                continue
            profile = await lookup.profile(story_id, entry.id, as_of=as_of, viewpoint=OMNISCIENT)
            for rel in profile.relationships:
                subject, obj = (
                    (entry.id, rel.other.entity_id)
                    if rel.direction == "out"
                    else (rel.other.entity_id, entry.id)
                )
                key = (subject, rel.predicate, obj)
                if key not in seen:
                    seen.add(key)
                    relations.append(Relation(subject, rel.predicate, obj))
    return ResolutionContext(
        index=index, beats=beats, present=present, player_id=player_id, relations=relations
    )


async def subject_timeline(
    lookup: KeyedLookup,
    story_id: str,
    subject_id: str,
    *,
    viewpoint: str = OMNISCIENT,
    through_position: tuple[int, int] | None = None,
) -> SubjectTimeline:
    """The subject's ``born`` and ``age`` canon as ``viewpoint`` may read it, through the
    keyed lookup (each read is a query key of the turn's read set)."""
    born_facts = await lookup.canon_at(
        story_id,
        predicate=BORN,
        subjects=subject_id,
        viewpoint=viewpoint,
        through_position=through_position,
    )
    age_facts = await lookup.canon_at(
        story_id,
        predicate=AGE,
        subjects=subject_id,
        viewpoint=viewpoint,
        through_position=through_position,
    )
    born: Interval | Instant | None = None
    for fact in born_facts:
        if fact.proposition.polarity is Polarity.AFFIRMED:
            born = fact.proposition.valid_time
            break
    ages: list[tuple[int, Interval | Instant]] = []
    for fact in age_facts:
        literal = fact.proposition.literal
        if (
            fact.proposition.polarity is Polarity.AFFIRMED
            and literal is not None
            and isinstance(literal.value, int)
            and not isinstance(literal.value, bool)
        ):
            ages.append((int(literal.value), fact.proposition.valid_time))
    return SubjectTimeline(born=born, ages=tuple(ages))


__all__ = ["load_context", "load_index", "load_time_basis", "subject_timeline"]
