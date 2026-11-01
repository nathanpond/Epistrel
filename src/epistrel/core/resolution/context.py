"""Loading what a resolution reads, once per operation (FR-PROP-6,9, FR-RET-2; #139).

The resolvers (``entities``, ``time``, ``events``) are pure; this module reads their inputs on
the operation's connection:

- ``load_index``: the story's entities and aliases as an ``EntityIndex`` (the per-story alias
  map, loaded per operation);
- ``load_time_basis``: the story's ``TimeBasis``, as ``core.world_clock`` builds it (#193);
- ``load_context``: the context window (the last ``CONTEXT_BEATS`` beats at or before the
  position, #135's ``working_memory`` as the speaker perceives them), the player character,
  the scene's entities present and the relationships of the story's people (#137's
  projections, omniscient: no visibility filter on candidates in M4), read through
  ``KeyedLookup.scene``/``relationships`` (#137's carried rule: a turn-time projection read is
  recorded in the read set, as context). The relationships of every person are one keyed read
  (#442), so the statements do not grow with the cast. Given the ``text`` to resolve, only the
  relationships its relational descriptions consult are read (#451: none for a text with no
  relation noun, else those of its possessors, closed over chained possessors), so neither the
  statements nor the rows grow with the cast or the canon;
- ``subject_timeline``: the subject's ``born`` and ``age`` canon through #136's
  ``KeyedLookup`` (so a verdict resting on "when he was seven" regenerates when they change).
  This module is a keyed reader (``core.readset.KEYED_READERS``): it never reads ``facts`` or
  ``propositions`` directly.

Nothing here calls a model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

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
    relation_owners,
)
from epistrel.core.resolution.identity import Identities
from epistrel.core.resolution.time import Position, SubjectTimeline, TimeBasis, turn_of
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import characters, entities, entity_aliases, stories

if TYPE_CHECKING:
    from epistrel.core.projections.structured import RelationshipView


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
    """The story's ``TimeBasis``: ``core.world_clock.time_basis`` (#193), the world clock's
    active advances when the story runs it, M4's stand-in otherwise."""
    from epistrel.core import world_clock  # here: the clock reads States, which import this

    return await world_clock.time_basis(conn, story_id)


SceneShare = tuple[frozenset[str] | None, tuple[Relation, ...]]
SharedContext = dict[tuple[str, int], SceneShare]
"""What every speaker's context at one turn shares (#452): ``(story_id, as_of)`` to the
scene's entities present and the people's relationships, read once for a turn's speakers."""


async def load_context(
    conn: AsyncConnection,
    story_id: str,
    *,
    position: Position,
    speaker: str,
    index: EntityIndex | None = None,
    lookup: KeyedLookup | None = None,
    shared: SharedContext | None = None,
    text: str | None = None,
    identities: Identities | None = None,
) -> ResolutionContext:
    """The ``ResolutionContext`` of a text spoken by ``speaker`` (an entity id, ``narrator`` or
    ``player``) at ``position`` (see the module docstring). The scene and the relationships are
    read through ``lookup`` (#136's ``KeyedLookup``: recorded in its read set as retrieved
    rows), or through a lookup of a read set of its own when none is given. They do not depend
    on the speaker: ``shared`` keeps them for the other speakers of the same snapshot and
    lookup (a turn's detection environment, #452), so they are read once, not per speaker.

    With ``text``, the context is for resolving that text only: its ``relations`` are the rows
    touching the possessors ``relation_owners`` finds (#451), and resolving ``text`` against it
    answers what it answers against every person's relationships. Without, every person's
    relationships are read (a context reused for several texts); ``shared`` applies then.

    ``identities`` (#171): the ``same_as`` the speaker's holder holds
    (``core.resolution.identity.identities_for``), which give the mentions their
    representatives."""
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
    turn = turn_of(position)
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
        if text is None:
            known = None if shared is None else shared.get((story_id, as_of))
            if known is None:
                known = await _scene_share(lookup, story_id, as_of, index)
                if shared is not None:
                    shared[(story_id, as_of)] = known
            present, relations = known[0], list(known[1])
        else:
            try:
                scene = await lookup.scene(story_id, as_of=as_of, viewpoint=OMNISCIENT)
            except InvalidRequest:
                scene = None
            if scene is not None and scene.present:
                present = frozenset(e.entity_id for e in scene.present)
    context = ResolutionContext(
        index=index,
        beats=beats,
        present=present,
        player_id=player_id,
        relations=relations,
        identities=identities,
    )
    if text is None or head < 0:
        return context
    return await _scoped_relations(context, lookup, story_id, text, speaker, as_of)


def _is_person(index: EntityIndex, entity_id: str) -> bool:
    entry = index.get(entity_id)
    return entry is not None and entry.kind == "person"


def _add_relations(
    relations: list[Relation],
    seen: set[tuple[str, str, str]],
    index: EntityIndex,
    owners: list[str],
    found: dict[str, tuple[RelationshipView, ...]],
) -> None:
    """Append each owner's relationship rows to ``relations`` once, as ``(subject, predicate,
    object)``: a row is kept when either end is a person, as every person's read keeps it."""
    for owner in owners:
        owner_person = _is_person(index, owner)
        for rel in found.get(owner, ()):
            other = rel.other.entity_id
            if not owner_person and not _is_person(index, other):
                continue
            subject, obj = (owner, other) if rel.direction == "out" else (other, owner)
            key = (subject, rel.predicate, obj)
            if key not in seen:
                seen.add(key)
                relations.append(Relation(subject, rel.predicate, obj))


async def _scoped_relations(
    context: ResolutionContext,
    lookup: KeyedLookup,
    story_id: str,
    text: str,
    speaker: str,
    as_of: int,
) -> ResolutionContext:
    """``context`` with the relationships of the possessors resolving ``text`` consults (#451):
    read the new possessors' rows (one keyed read), resolve again, until no new one appears. A
    round adds at least one possessor, so there are at most as many rounds as relation nouns."""
    relations: list[Relation] = []
    seen: set[tuple[str, str, str]] = set()
    loaded: set[str] = set()
    while True:
        new = [o for o in relation_owners(text, speaker, context) if o not in loaded]
        if not new:
            return context
        found = await lookup.relationships(story_id, new, as_of=as_of, viewpoint=OMNISCIENT)
        loaded.update(new)
        _add_relations(relations, seen, context.index, new, found)
        context = replace(context, relations=list(relations))


async def _scene_share(
    lookup: KeyedLookup, story_id: str, as_of: int, index: EntityIndex
) -> SceneShare:
    """The scene's entities present and the relationships of the story's people as of
    ``as_of`` (omniscient), in the order the people are indexed."""
    present: frozenset[str] | None = None
    try:
        scene = await lookup.scene(story_id, as_of=as_of, viewpoint=OMNISCIENT)
    except InvalidRequest:
        scene = None
    if scene is not None and scene.present:
        present = frozenset(e.entity_id for e in scene.present)
    people = [e.id for e in index.entries.values() if e.kind == "person"]
    found = (
        await lookup.relationships(story_id, people, as_of=as_of, viewpoint=OMNISCIENT)
        if people
        else {}
    )
    relations: list[Relation] = []
    seen: set[tuple[str, str, str]] = set()
    for person in people:
        for rel in found.get(person, ()):
            subject, obj = (
                (person, rel.other.entity_id)
                if rel.direction == "out"
                else (rel.other.entity_id, person)
            )
            key = (subject, rel.predicate, obj)
            if key not in seen:
                seen.add(key)
                relations.append(Relation(subject, rel.predicate, obj))
    return present, tuple(relations)


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


__all__ = [
    "SceneShare",
    "SharedContext",
    "load_context",
    "load_index",
    "load_time_basis",
    "subject_timeline",
]
