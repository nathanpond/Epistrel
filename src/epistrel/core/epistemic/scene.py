"""The authoritative scene perception routing reads (FR-TURN-10; #161).

``load_scene_state(conn, story_id, turn)`` answers ``core.epistemic.perception.SceneState`` as
of the start of narrative turn ``turn`` (``turn_moves`` the placement changes the turn's own
beats committed, by beat), from authoritative state only, never from a consumer's roster:

- **placements**: #137's ``placement`` projection, the latest current row of each character's
  entity at a location written before the turn (``through`` the turn's start; valid at its
  story-time, ``story_time_at``); a row the turn itself wrote is a move at its ``since_beat``,
  and a row the turn's own negated ``located_at`` closed with no new placement is an exit with no
  destination (unplaced, ``None``) at the negation's beat (#175, carried from #161);
- **the topology**: canon's current ``located_in`` (containment) and ``adjacent_to`` (adjacency,
  with its ``barrier`` qualifier, ``door_open`` when none is declared; the latest declaration of a
  pair is its barrier) facts, and **the channels**: current ``on_channel`` facts, each a
  participant and a channel token. These are read through ``core.readset.KeyedLookup`` (keyed in
  the caller's read set when one is given), since the epistemic model never reads ``facts``
  itself (FR-EPI-1);
- the story's characters, the languages each understands (``characters.languages``; ``NULL``:
  every language) and its places (``place`` entities), and the scene's location: the player
  character's.

The stored scene is omniscient: a ``latent`` placement counts, since routing is authoritative.
"""

from __future__ import annotations

import weakref
from collections import defaultdict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

if TYPE_CHECKING:
    from epistrel.core.events import Operation

from epistrel.core.epistemic.perception import CO_LOCATION, FULL, NONE, Level, SceneState, pair
from epistrel.core.profiles.capabilities import level_at_least, module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.projections.structured import covers, through
from epistrel.core.propositions.vocabulary import (
    ADJACENT_TO,
    BARRIERS,
    DEFAULT_BARRIER,
    LOCATED_AT,
    LOCATED_IN,
    ON_CHANNEL,
)
from epistrel.core.readset import KeyedLookup, StructuredReadSet, register_rebase_check
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.resolution.time import story_time_at
from epistrel.core.tables import characters, entities, placement, story_heads

PARTITIONS_MODULE = "partitions"
PERCEPTION_MODULE = "perception"


def level_of(settings: ProfileSettings) -> Level:
    """The perception level routing runs at under ``settings``: ``co_location`` or ``full``
    only with per-character partitions on and that level delivered; ``none`` otherwise (every
    M4 profile: nothing is routed, #129)."""
    if not module_on(settings, PARTITIONS_MODULE):
        return NONE
    if level_at_least(settings, PERCEPTION_MODULE, FULL):
        return FULL
    if level_at_least(settings, PERCEPTION_MODULE, CO_LOCATION):
        return CO_LOCATION
    return NONE


async def current_level(conn: AsyncConnection, story_id: str) -> Level:
    """The story's perception level under its effective configuration now (a commit reads it
    under the story lock, so a level changed mid-turn routes under the new one: FR-CONC-16)."""
    found = await effective_configuration(conn, story_id, validate=False)
    return level_of(found.settings)


START_SLOT = 32_767
"""Every inter-turn slot after the previous turn: a turn's start is ``(turn - 1, START_SLOT)``."""

Moves = Mapping[int, tuple[tuple[str, str | None], ...]]


def _since(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
    turn = row["since_turn"]
    return (
        -1 if turn is None else int(turn),
        int(row["since_slot"] or 0),
        int(row["since_beat"] or 0),
        str(row["source_fact_id"]),
    )


async def _cast(conn: AsyncConnection, story_id: str) -> tuple[dict[str, str], dict[str, Any]]:
    """The story's characters by entity id, and their rows by character id."""
    found = await conn.execute(
        select(
            characters.c.id,
            characters.c.is_player,
            characters.c.ordinal,
            characters.c.languages,
            entities.c.id.label("entity_id"),
        )
        .select_from(characters.outerjoin(entities, entities.c.character_id == characters.c.id))
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    by_entity: dict[str, str] = {}
    rows: dict[str, Any] = {}
    for row in found:
        rows[str(row.id)] = row
        if row.entity_id is not None:
            by_entity[str(row.entity_id)] = str(row.id)
    return by_entity, rows


async def load_scene(
    conn: AsyncConnection,
    story_id: str,
    turn: int,
    *,
    read_set: StructuredReadSet | None = None,
) -> tuple[SceneState, Moves]:
    """The scene at the start of ``turn`` and the moves the turn's own beats committed (see
    the module docstring)."""
    by_entity, cast = await _cast(conn, story_id)
    now = story_time_at(turn, await load_time_basis(conn, story_id))
    start = (turn - 1, START_SLOT)
    rows = [
        dict(r._mapping)
        for r in await conn.execute(
            select(placement).where(
                placement.c.story_id == story_id,
                placement.c.location_id.is_not(None),
                placement.c.entity_id.in_(list(by_entity)),
            )
        )
    ]
    current: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if through(row, start) and covers(row, now):
            held = current.get(str(row["entity_id"]))
            if held is None or _since(row) > _since(held):
                current[str(row["entity_id"])] = row
    placements = {by_entity[e]: str(r["location_id"]) for e, r in current.items()}
    moves: dict[int, list[tuple[str, str | None]]] = defaultdict(list)
    for row in sorted(rows, key=_since):
        if row["since_turn"] == turn and not row["since_slot"] and row["closed_by_fact_id"] is None:
            moves[int(row["since_beat"] or 0)].append(
                (by_entity[str(row["entity_id"])], str(row["location_id"]))
            )
    lookup = KeyedLookup(conn, read_set if read_set is not None else StructuredReadSet())
    position = (turn, 0)
    # #175 (from #161): a character the turn's own negated ``located_at`` closed with no new
    # placement left mid-turn with no destination: unplaced from the closing beat's next beat.
    moved_to = {str(r["source_fact_id"]) for r in rows if r["since_turn"] == turn}
    exits = {
        str(r["closed_by_fact_id"]): by_entity[str(r["entity_id"])]
        for r in rows
        if r["closed_turn"] == turn
        and not r["closed_slot"]
        and r["closed_by_fact_id"] is not None
        and str(r["closed_by_fact_id"]) not in moved_to
    }
    if exits:
        entity_of = {c: e for e, c in by_entity.items()}
        closers = await lookup.canon_at(
            story_id,
            predicate=LOCATED_AT,
            subjects=sorted({entity_of[c] for c in exits.values() if c in entity_of}),
            through_position=position,
        )
        for fact in sorted(closers, key=lambda f: (f.beat or 0, str(f.id))):
            who = exits.get(str(fact.id))
            if who is not None and fact.narrative_turn == turn:
                moves[int(fact.beat or 0)].append((who, None))

    async def current_facts(predicate: str) -> list[Any]:
        found = await lookup.canon_at(
            story_id,
            predicate=predicate,
            subjects=None,
            story_time=now,
            through_position=position,
        )
        return [f for f in found if f.proposition.polarity.value == "affirmed"]

    containment: dict[str, list[str]] = defaultdict(list)
    for fact in await current_facts(LOCATED_IN):
        if fact.proposition.object_id is not None:
            containment[str(fact.subject_id)].append(str(fact.proposition.object_id))
    adjacency: dict[tuple[str, str], tuple[Any, str]] = {}
    for fact in await current_facts(ADJACENT_TO):
        other = fact.proposition.object_id
        if other is None:
            continue
        barrier = str(fact.proposition.qualifiers.get("barrier") or DEFAULT_BARRIER)
        if barrier not in BARRIERS:
            barrier = DEFAULT_BARRIER
        key = pair(str(fact.subject_id), str(other))
        order = (fact.narrative_turn or 0, fact.slot_k or 0, fact.beat or 0, str(fact.id))
        declared = adjacency.get(key)
        if declared is None or order > declared[0]:
            adjacency[key] = (order, barrier)
    channels: dict[str, set[str]] = defaultdict(set)
    for fact in await current_facts(ON_CHANNEL):
        literal = fact.proposition.literal
        member = by_entity.get(str(fact.subject_id), str(fact.subject_id))
        if literal is not None and member in cast:
            channels[str(literal.value)].add(member)
    places = frozenset(
        str(p)
        for p in await conn.scalars(
            select(entities.c.id).where(entities.c.story_id == story_id, entities.c.kind == "place")
        )
    )
    player = next((c for c, row in cast.items() if row.is_player), None)
    languages = {
        c: frozenset(str(code).casefold() for code in row.languages)
        for c, row in cast.items()
        if row.languages is not None
    }
    scene = SceneState(
        location=placements.get(player) if player is not None else None,
        placements=placements,
        containment={k: tuple(dict.fromkeys(v)) for k, v in containment.items()},
        adjacency={k: barrier for k, (_, barrier) in adjacency.items()},
        channels={k: frozenset(v) for k, v in channels.items()},
        languages=languages,
        characters=frozenset(cast),
        places=places,
    )
    return scene, {beat: tuple(found) for beat, found in moves.items()}


async def load_scene_state(
    conn: AsyncConnection,
    story_id: str,
    turn: int,
    *,
    read_set: StructuredReadSet | None = None,
) -> SceneState:
    """The scene at the start of ``turn`` (``load_scene`` without the turn's moves)."""
    scene, _ = await load_scene(conn, story_id, turn, read_set=read_set)
    return scene


_ASSUMED: dict[int, tuple[weakref.ref[StructuredReadSet], int, Level]] = {}


def remember_level(read_set: StructuredReadSet, level: Level) -> None:
    """Record the perception level a structured turn's current attempt generated under (its
    qualifiers were validated at it), for ``recheck``."""
    key = id(read_set)
    _ASSUMED[key] = (
        weakref.ref(read_set, lambda _: _ASSUMED.pop(key, None)),
        read_set.attempt,
        level,
    )


async def recheck(conn: AsyncConnection, op: Operation) -> bool:
    """The rebase re-check of perception (``core.readset.REBASE_CHECKS``; FR-CONC-16): a turn
    whose attempt generated under per-character partitions (``co_location`` or ``full``) and
    whose story has none now (perception ``none``) regenerates, since its beats were rendered
    for partitions that no longer exist. Any other level change passes: the commit routes
    under the level current then (``current_level``)."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet) or op.story_id is None:
        return True
    entry = _ASSUMED.get(id(source))
    if entry is None or entry[0]() is not source or entry[1] != source.attempt:
        return True
    return not (entry[2] != NONE and await current_level(conn, op.story_id) == NONE)


register_rebase_check(recheck)


async def placed_elsewhere(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    """The characters authoritatively placed somewhere other than the scene's location at the
    story's next turn (#55's ``present`` set, FR-GUARD-11: everyone else is present). Empty
    when routing is off (perception ``none``: everyone is present, as under Lite) or the scene
    has no location; an unplaced character counts as present, so the minors policy errs toward
    applying."""
    if await current_level(conn, story_id) == NONE:
        return frozenset()
    turn = await conn.scalar(
        select(story_heads.c.next_turn_index).where(story_heads.c.story_id == story_id)
    )
    if turn is None:
        return frozenset()
    scene = await load_scene_state(conn, story_id, int(turn))
    if scene.location is None:
        return frozenset()
    return frozenset(c for c, where in scene.placements.items() if where != scene.location)


__all__ = [
    "PARTITIONS_MODULE",
    "PERCEPTION_MODULE",
    "START_SLOT",
    "Moves",
    "current_level",
    "level_of",
    "load_scene",
    "load_scene_state",
    "placed_elsewhere",
    "recheck",
    "remember_level",
]
