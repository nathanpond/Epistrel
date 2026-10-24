"""The structured projections: who relates to whom, who and what is where, and each turn's scene
(FR-STORE-3, FR-RET-6's standing profile; docs/02 §3; #137).

Three caches over **canon** (``facts``, #133), not over the event log:

- ``relationships``: a row per active canon fact version of a predicate whose ``projection``
  is ``relationships`` (kinship, relationships, membership, employment) or ``scene``
  (``present_at``), one per member of a set object.
- ``placement``: a row per active canon fact version of a ``placement`` predicate: the subject
  at a location (``located_at``), or, for a predicate whose object is an ``item`` (``holds``),
  the item in its holder (the container: an inventory).
- ``scene_state``: a row per active turn whose scene is not empty: the location of the player
  character at the turn's end and the entities placed (or ``present_at``) there.

**What projects.** The predicate row's ``projection`` decides (core predicates declare it in
``core.propositions.vocabulary``; an author predicate may too; an auto predicate never does).
Only active (``membership.active_only``), canon, affirmed facts with an object that is one of
the story's entities make rows: a negated fact closes the affirmed row it supersedes and never
makes one, and a literal, unresolved or closed-set object is skipped silently. A symmetric
predicate's row is in canonical direction (the lower id is the subject) and is inverted at read.
``qualifiers`` keeps the key qualifiers only (ids and typed literals, never prose), and a
relationship's numeric ``trust`` (``PROJECTED_QUALIFIERS``, #173: what #162's seeded trust reads).
Valid-time is the fact's outer bounds (approximate bounds collapse to them).

**Closed, never deleted.** A row carries its fact's narrative position (``since_turn``,
``since_slot``) and the active version that closed it (``closed_by_fact_id`` and its position,
the earliest when several did; a position-less closer closes it at every position). A read as
of turn N takes the rows written through N that no closer through N closed, exactly as
``canon(through_position=(N, 0))`` does, so a supersede or a retcon later than N leaves N's
answer alone. "Now" at turn N is N's story-time, ``story_time_at(N)`` (``stories.story_minute_zero``
plus N story-turns; #139's rule, so the scene and profile reads agree with the resolver, #438)
until the world clock lands (M6).

**One fold, two paths.** ``derive`` is a pure, order-free function of a set of active fact
versions; ``scene`` of the placement rows. The incremental path (``on_fact_change``, a
``FACT_HOOKS`` hook run once per ``commit_fact``/``supersede`` in its transaction;
``follow_membership_change``, an in-transaction ``core.timeline`` hook; ``after_turn``, called by
``Operation`` once a turn is committed, for its own scene and for the operations a regenerate or
a turn over a rewound tail made dormant) re-derives only the rows of the touched subjects and
the scenes from the first turn they can change, and writes the difference (inserts, updates and
deletes). The fold (``fold_state``: ``rebuild``, erasure's ``rebuilt`` stage, a fork) derives
every row from the same function, so rebuilt rows equal the incremental ones byte for byte.

**Reads** (``scene_view``, ``profile_view``) take a viewpoint: a character sees the rows whose
fact visibility admits it (``canon.facts.visible_to``), so an entity placed by a ``latent`` fact
of another character is not present in that character's scene, while the stored scene is the
omniscient one. ``source_fact_id`` is returned to ``omniscient`` only.

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ColumnElement,
    Table,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    true,
    tuple_,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import HolderKind, OperationKind, OperationStatus
from epistrel.core.errors import InvalidRequest, StoryEntityNotFound, StoryNotFound
from epistrel.core.membership import Position as NarrativePosition
from epistrel.core.membership import active_only, active_operation
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.resolution.time import TimeBasis, story_time_at
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import (
    characters,
    entities,
    operations,
    placement,
    predicates,
    propositions,
    relationships,
    scene_state,
    stories,
)
from epistrel.core.tables.facts import facts

if TYPE_CHECKING:
    from epistrel.core.canon.facts import FactChange
    from epistrel.core.propositions.registry import Registry
    from epistrel.core.timeline import MembershipChange

RELATIONSHIPS = "relationships"
PLACEMENT = "placement"
SCENE = "scene"
PROJECTIONS: tuple[str, ...] = (RELATIONSHIPS, PLACEMENT, SCENE)
"""The ``projection`` values a predicate may declare that make rows; any other is ``none``."""

CONTAINER_OBJECT_TYPE = "item"
"""A ``placement`` predicate whose object is an item places the object in its subject (``holds``:
the holder is the container); any other places the subject at its object (``located_at``)."""

Position = tuple[int, int] | NarrativePosition
"""A projection read's bound: ``(turn, slot_k)`` (canon's), or the shared
``core.membership.Position``, read at its turn and slot (#164)."""


def _turn_slot(position: Position) -> tuple[int, int]:
    if isinstance(position, NarrativePosition):
        return (position.turn, position.slot_k)
    return position


AFTER_WRITE: list[Callable[[str], None]] = []
"""Test-only fault point: called with the story id after every incremental projection write,
inside the transaction (an exception aborts the write's whole commit)."""


# -- the pure fold ----------------------------------------------------------------------------


PROJECTED_QUALIFIERS: tuple[str, ...] = ("trust",)
"""The non-key qualifiers a row keeps beside the key ones: a relationship's ``trust`` (0 to 1),
which ``core.epistemic.testimony.seed_trust`` reads (#162, #173)."""


@dataclass(frozen=True, slots=True)
class FactVersion:
    """What ``derive`` reads of one active canon fact version of a projecting predicate."""

    id: str
    subject_id: str
    predicate_id: str
    projection: str
    container: bool
    affirmed: bool
    object_ids: tuple[str, ...]
    key_qualifiers: Mapping[str, Any]
    valid_from: int | None
    valid_to: int | None
    turn: int | None
    slot: int | None
    visibility: Mapping[str, Any]
    supersedes: tuple[str, ...] = ()
    beat: int | None = None
    """The beat of its turn the version was committed at (``placement.since_beat``, #161)."""


def _position_key(turn: int | None, slot: int | None) -> tuple[int, int, int]:
    """Earliest first: a position-less version closes at every position."""
    if turn is None:
        return (0, 0, 0)
    return (1, turn, slot or 0)


@dataclass(slots=True)
class Derived:
    """The rows ``derive`` makes, keyed by each table's primary key."""

    relationships: dict[tuple[Any, ...], Row] = field(default_factory=dict)
    placement: dict[tuple[Any, ...], Row] = field(default_factory=dict)


def derive(story_id: str, versions: Iterable[FactVersion], known: frozenset[str]) -> Derived:
    """The ``relationships`` and ``placement`` rows of ``versions`` (active fact versions of
    projecting predicates; ``known`` the story's entity ids): order-free, so the incremental
    path and the fold agree."""
    found = list(versions)
    closers: dict[str, list[FactVersion]] = defaultdict(list)
    for version in found:
        for closed in version.supersedes:
            closers[closed].append(version)
    out = Derived()
    for version in found:
        if not version.affirmed:
            continue
        objects = [o for o in version.object_ids if o in known]
        if not objects:
            continue
        closer = min(
            closers.get(version.id, ()),
            key=lambda c: (_position_key(c.turn, c.slot), c.id),
            default=None,
        )
        common: Row = {
            "story_id": story_id,
            "source_fact_id": version.id,
            "valid_from": version.valid_from,
            "valid_to": version.valid_to,
            "since_turn": version.turn,
            "since_slot": None if version.turn is None else (version.slot or 0),
            "closed_by_fact_id": None if closer is None else closer.id,
            "closed_turn": None if closer is None else closer.turn,
            "closed_slot": None if closer is None or closer.turn is None else (closer.slot or 0),
            "visibility": dict(version.visibility),
        }
        if version.projection == PLACEMENT:
            target = objects[0]
            row = {
                **common,
                "entity_id": target if version.container else version.subject_id,
                "location_id": None if version.container else target,
                "container_id": version.subject_id if version.container else None,
                "since_beat": None if version.turn is None else version.beat,
            }
            out.placement[(story_id, version.id)] = row
            continue
        for target in sorted(set(objects)):
            out.relationships[(story_id, version.id, target)] = {
                **common,
                "object_id": target,
                "subject_id": version.subject_id,
                "predicate_id": version.predicate_id,
                "qualifiers": dict(version.key_qualifiers),
            }
    return out


def through(row: Mapping[str, Any], position: Position) -> bool:
    """Whether ``row`` is in the record as of ``position``: written at or before it (or
    position-less) and closed by no version at or before it."""
    turn, slot = _turn_slot(position)
    since = row["since_turn"]
    if since is not None and (since, row["since_slot"] or 0) > (turn, slot):
        return False
    if row["closed_by_fact_id"] is None:
        return True
    closed = row["closed_turn"]
    return closed is not None and (closed, row["closed_slot"] or 0) > (turn, slot)


def covers(row: Mapping[str, Any], now: int) -> bool:
    """Whether ``row``'s valid-time (outer bounds) covers story-time ``now``."""
    lo, hi = row["valid_from"], row["valid_to"]
    return (lo is None or lo <= now) and (hi is None or now < hi)


def _latest(rows: Iterable[Row]) -> Row | None:
    return max(
        rows,
        key=lambda r: (
            _position_key(r["since_turn"], r["since_slot"]),
            -1 if r["valid_from"] is None else r["valid_from"],
            r["source_fact_id"],
        ),
        default=None,
    )


@dataclass(frozen=True, slots=True)
class Scene:
    location_id: str | None
    present: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return self.location_id is None and not self.present


def scene(
    placements: Iterable[Row],
    presences: Iterable[Row],
    actor: str | None,
    position: Position,
    now: int,
    visible: Callable[[Mapping[str, Any]], bool] = lambda _: True,
) -> Scene:
    """The scene as of ``position``: ``actor``'s location (its latest current placement at a
    location) and the entities placed, or ``present_at``, there, sorted by id."""
    current = [r for r in placements if through(r, position) and covers(r, now) and visible(r)]
    own = _latest(r for r in current if r["entity_id"] == actor and r["location_id"] is not None)
    if own is None:
        return Scene(None, ())
    location = str(own["location_id"])
    present = {str(r["entity_id"]) for r in current if r["location_id"] == location}
    present |= {
        str(r["subject_id"])
        for r in presences
        if r["object_id"] == location and through(r, position) and covers(r, now) and visible(r)
    }
    return Scene(location, tuple(sorted(present)))


def turn_position(turn: int) -> Position:
    """As of turn N: the turn itself, before its inter-turn slots (``canon``'s ``(N, 0)``)."""
    return (turn, 0)


def first_turn(row: Mapping[str, Any]) -> int:
    """The first turn whose scene ``row`` can change: where it was written, or closed (an
    inter-turn slot after turn N first shows at N + 1; a position-less row at every turn)."""
    since = row["since_turn"]
    if since is None:
        return 1
    turns: list[int] = [int(since) + (1 if (row["since_slot"] or 0) > 0 else 0)]
    if row["closed_by_fact_id"] is not None:
        closed = row["closed_turn"]
        if closed is None:
            return 1
        turns.append(int(closed) + (1 if (row["closed_slot"] or 0) > 0 else 0))
    return max(1, min(turns))


# -- loading ------------------------------------------------------------------------------------


def _keyed(rows: Iterable[Any], table: Table) -> dict[tuple[Any, ...], Row]:
    names = [c.name for c in table.primary_key.columns]
    out: dict[tuple[Any, ...], Row] = {}
    for row in rows:
        mapped = dict(row._mapping)
        out[tuple(mapped[n] for n in names)] = mapped
    return out


async def _versions(
    conn: AsyncConnection, story_id: str, subjects: Sequence[str] | None
) -> list[FactVersion]:
    f = facts.alias("f")
    query = (
        select(
            f.c.id,
            f.c.subject_id,
            f.c.predicate_id,
            f.c.valid_from,
            f.c.valid_to,
            f.c.narrative_turn,
            f.c.slot_k,
            f.c.beat,
            f.c.visibility,
            f.c.supersedes,
            propositions.c.object_id,
            propositions.c.object_set,
            propositions.c.qualifiers,
            propositions.c.polarity,
            predicates.c.projection,
            predicates.c.object_type,
            predicates.c.key_qualifiers,
        )
        .select_from(
            f.join(propositions, propositions.c.id == f.c.proposition_id).join(
                predicates, predicates.c.id == f.c.predicate_id
            )
        )
        .where(
            f.c.story_id == story_id,
            active_only(f),
            f.c.holder == HolderKind.CANON.value,
            predicates.c.projection.in_(PROJECTIONS),
        )
    )
    if subjects is not None:
        query = query.where(f.c.subject_id.in_(list(subjects)))
    found = []
    for r in await conn.execute(query):
        objects: tuple[str, ...] = ()
        if r.object_id is not None:
            objects = (str(r.object_id),)
        elif r.object_set is not None:
            objects = tuple(str(o) for o in r.object_set)
        qualifiers = dict(r.qualifiers or {})
        found.append(
            FactVersion(
                id=str(r.id),
                subject_id=str(r.subject_id),
                predicate_id=str(r.predicate_id),
                projection=str(r.projection),
                container=r.object_type == CONTAINER_OBJECT_TYPE,
                affirmed=r.polarity == "affirmed",
                object_ids=objects,
                key_qualifiers={
                    k: qualifiers[k]
                    for k in (*(r.key_qualifiers or ()), *PROJECTED_QUALIFIERS)
                    if k in qualifiers
                },
                valid_from=r.valid_from,
                valid_to=r.valid_to,
                turn=r.narrative_turn,
                slot=r.slot_k,
                visibility=dict(r.visibility),
                supersedes=tuple(str(s) for s in r.supersedes or ()),
                beat=r.beat,
            )
        )
    return found


async def _known(
    conn: AsyncConnection, story_id: str, versions: Sequence[FactVersion]
) -> frozenset[str]:
    wanted = sorted({o for v in versions for o in v.object_ids})
    if not wanted:
        return frozenset()
    found = await conn.scalars(
        select(entities.c.id).where(entities.c.story_id == story_id, entities.c.id.in_(wanted))
    )
    return frozenset(str(i) for i in found)


async def _stored(conn: AsyncConnection, story_id: str, subjects: Sequence[str] | None) -> Derived:
    r, p = relationships, placement
    rel_query = select(r).where(r.c.story_id == story_id)
    place_query = select(p).where(p.c.story_id == story_id)
    if subjects is not None:
        ids = list(subjects)
        rel_query = rel_query.where(r.c.subject_id.in_(ids))
        place_query = place_query.where(
            or_(
                and_(p.c.location_id.is_not(None), p.c.entity_id.in_(ids)),
                p.c.container_id.in_(ids),
            )
        )
    return Derived(
        relationships=_keyed(await conn.execute(rel_query), r),
        placement=_keyed(await conn.execute(place_query), p),
    )


async def _basis(conn: AsyncConnection, story_id: str) -> TimeBasis:
    """The story's time basis: "now" at turn N is ``story_time_at(N, basis)`` (#139, #438)."""
    found = await conn.scalar(select(stories.c.story_minute_zero).where(stories.c.id == story_id))
    return TimeBasis(minute_zero=int(found or 0))


async def _actor(conn: AsyncConnection, story_id: str) -> str | None:
    """The player character's entity (the first player by cast order that has one)."""
    found = await conn.scalar(
        select(entities.c.id)
        .select_from(entities.join(characters, characters.c.id == entities.c.character_id))
        .where(entities.c.story_id == story_id, characters.c.is_player)
        .order_by(characters.c.ordinal, entities.c.id)
        .limit(1)
    )
    return None if found is None else str(found)


async def _active_turns(
    conn: AsyncConnection, story_id: str, from_turn: int = 1
) -> list[tuple[int, int, str]]:
    found = await conn.execute(
        select(operations.c.narrative_turn, operations.c.turn_variant, operations.c.operation_id)
        .where(
            operations.c.story_id == story_id,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
            active_operation(operations),
            operations.c.narrative_turn >= from_turn,
        )
        .order_by(operations.c.narrative_turn)
    )
    return [(int(r.narrative_turn), int(r.turn_variant), str(r.operation_id)) for r in found]


async def _scene_predicates(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    """The ids of the predicates (core, or the story's) whose ``projection`` is ``scene``."""
    found = await conn.scalars(
        select(predicates.c.id).where(
            predicates.c.projection == SCENE,
            or_(predicates.c.story_id.is_(None), predicates.c.story_id == story_id),
        )
    )
    return frozenset(str(i) for i in found)


def _from_turn_on(table: Table, from_turn: int, basis: TimeBasis) -> ColumnElement[bool]:
    """The rows ``scene`` can keep at some turn from ``from_turn`` on (#153): not closed at or
    before ``(from_turn, 0)`` (``through``), and valid past that turn's story time (``covers``;
    story time never decreases with the turn). The rest are dropped by every such turn."""
    start = story_time_at(from_turn, basis)
    closed = tuple_(table.c.closed_turn, func.coalesce(table.c.closed_slot, 0))
    return and_(
        or_(
            table.c.closed_by_fact_id.is_(None),
            and_(table.c.closed_turn.is_not(None), closed > tuple_(from_turn, 0)),
        ),
        or_(table.c.valid_to.is_(None), table.c.valid_to > start),
    )


async def _scene_inputs(
    conn: AsyncConnection,
    story_id: str,
    from_turn: int | None = None,
    basis: TimeBasis | None = None,
) -> tuple[list[Row], list[Row]]:
    """The story's placement rows and its ``scene`` relationship rows; with ``from_turn``, only
    those a scene from that turn on can use, so a write at the head reads the rows still open
    there rather than the story's whole history."""
    if from_turn is not None and basis is None:
        basis = await _basis(conn, story_id)

    def bounded(table: Table) -> ColumnElement[bool]:
        return (
            true() if from_turn is None or basis is None else _from_turn_on(table, from_turn, basis)
        )

    places = [
        dict(r._mapping)
        for r in await conn.execute(
            select(placement).where(placement.c.story_id == story_id, bounded(placement))
        )
    ]
    scene_ids = await _scene_predicates(conn, story_id)
    presences = (
        [
            dict(r._mapping)
            for r in await conn.execute(
                select(relationships).where(
                    relationships.c.story_id == story_id,
                    relationships.c.predicate_id.in_(sorted(scene_ids)),
                    bounded(relationships),
                )
            )
        ]
        if scene_ids
        else []
    )
    return places, presences


async def _scenes(
    conn: AsyncConnection,
    story_id: str,
    places: Sequence[Row],
    presences: Sequence[Row],
    from_turn: int = 1,
    basis: TimeBasis | None = None,
) -> dict[tuple[Any, ...], Row]:
    """The ``scene_state`` rows of the active turns from ``from_turn`` on, from ``places`` and
    ``presences`` (the story's placement rows and its ``scene`` relationship rows)."""
    if not places:
        return {}
    turns = await _active_turns(conn, story_id, from_turn)
    if not turns:
        return {}
    actor = await _actor(conn, story_id)
    basis = basis or await _basis(conn, story_id)
    out: dict[tuple[Any, ...], Row] = {}
    for turn, variant, operation_id in turns:
        now = story_time_at(turn, basis)
        found = scene(places, presences, actor, turn_position(turn), now)
        if found.empty:
            continue
        out[(story_id, turn, variant)] = {
            "story_id": story_id,
            "turn": turn,
            "variant": variant,
            "location_id": found.location_id,
            "present_entity_ids": list(found.present),
            "source_operation_id": operation_id,
        }
    return out


# -- writing --------------------------------------------------------------------------------


def _where_key(table: Table, key: tuple[Any, ...]) -> ColumnElement[bool]:
    names = [c.name for c in table.primary_key.columns]
    return and_(*(table.c[n] == v for n, v in zip(names, key, strict=True)))


async def _sync(
    conn: AsyncConnection,
    table: Table,
    stored: Mapping[tuple[Any, ...], Row],
    wanted: Mapping[tuple[Any, ...], Row],
) -> list[Row]:
    """Make ``table``'s rows ``stored`` into ``wanted``; answers every row that changed (old and
    new versions)."""
    changed: list[Row] = []
    gone = sorted(stored.keys() - wanted.keys())
    if gone:
        names = [c.name for c in table.primary_key.columns]
        await conn.execute(delete(table).where(tuple_(*(table.c[n] for n in names)).in_(gone)))
        changed += [stored[k] for k in gone]
    added = sorted(wanted.keys() - stored.keys())
    if added:
        await conn.execute(insert(table), [dict(wanted[k]) for k in added])
        changed += [wanted[k] for k in added]
    for key in sorted(stored.keys() & wanted.keys()):
        old, new = stored[key], wanted[key]
        diff = {k: v for k, v in new.items() if old.get(k) != v}
        if diff:
            await conn.execute(update(table).where(_where_key(table, key)).values(**diff))
            changed += [old, new]
    return changed


async def _has_canon(conn: AsyncConnection, story_id: str) -> bool:
    found = await conn.scalar(select(exists().where(facts.c.story_id == story_id)))
    return bool(found)


async def refresh(
    conn: AsyncConnection,
    story_id: str,
    *,
    subjects: Iterable[str] | None = None,
    from_turn: int | None = None,
) -> None:
    """Re-derive the rows of ``subjects`` (every subject: ``None``) and write the difference, then
    the scenes from the first turn a changed row (or ``from_turn``) can change."""
    chosen = None if subjects is None else sorted(set(subjects))
    if chosen is not None and not chosen and from_turn is None:
        return
    first = from_turn
    if chosen is None or chosen:
        versions = await _versions(conn, story_id, chosen)
        wanted = derive(story_id, versions, await _known(conn, story_id, versions))
        stored = await _stored(conn, story_id, chosen)
        changed = await _sync(conn, placement, stored.placement, wanted.placement)
        moved = await _sync(conn, relationships, stored.relationships, wanted.relationships)
        scene_ids = await _scene_predicates(conn, story_id) if moved else frozenset()
        changed += [r for r in moved if r["predicate_id"] in scene_ids]
        for row in changed:
            turn = first_turn(row)
            first = turn if first is None else min(first, turn)
    if first is not None:
        stored_scenes = _keyed(
            await conn.execute(
                select(scene_state).where(
                    scene_state.c.story_id == story_id, scene_state.c.turn >= first
                )
            ),
            scene_state,
        )
        basis = await _basis(conn, story_id)
        places, presences = await _scene_inputs(conn, story_id, first, basis)
        await _sync(
            conn,
            scene_state,
            stored_scenes,
            await _scenes(conn, story_id, places, presences, first, basis),
        )
    for hook in list(AFTER_WRITE):
        hook(story_id)


async def on_fact_change(conn: AsyncConnection, change: FactChange) -> None:
    """``FACT_HOOKS`` (#133): a canon write re-derives its subjects' rows in its transaction."""
    subjects = {change.fact.subject_id}
    subjects |= {f.subject_id for f in change.remainders}
    subjects |= {f.subject_id for f in change.closed}
    await refresh(conn, change.story_id, subjects=subjects)


async def _subjects_of(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> list[str]:
    if not operation_ids:
        return []
    found = await conn.scalars(
        select(facts.c.subject_id)
        .where(facts.c.story_id == story_id, facts.c.operation_id.in_(list(operation_ids)))
        .distinct()
    )
    return [str(s) for s in found]


async def _first_turn_of(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> int | None:
    if not operation_ids:
        return None
    found = await conn.execute(
        select(operations.c.narrative_turn).where(
            operations.c.story_id == story_id,
            operations.c.operation_id.in_(list(operation_ids)),
        )
    )
    turns = [r.narrative_turn for r in found]
    if not turns:
        return None
    if any(t is None for t in turns):
        return 1
    return max(1, min(int(t) for t in turns))


async def follow_operations(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> None:
    """Operations changed membership in this transaction: their facts' subjects and the scenes
    from their first turn are re-derived."""
    ids = sorted(set(operation_ids))
    if not ids or not await _has_canon(conn, story_id):
        return
    await refresh(
        conn,
        story_id,
        subjects=await _subjects_of(conn, story_id, ids),
        from_turn=await _first_turn_of(conn, story_id, ids),
    )


async def follow_membership_change(conn: AsyncConnection, change: MembershipChange) -> None:
    """``core.timeline``'s in-transaction hook, after #130's flip: a truncate, an untruncate, a
    swap (and a fork's flattening) re-derive what the changed operations' facts project."""
    await follow_operations(conn, change.story_id, change.operation_ids)


async def after_turn(
    conn: AsyncConnection, story_id: str, turn: int, followed: Sequence[str] = ()
) -> None:
    """A turn committed at ``turn`` (``Operation``, after it is marked committed): its own scene,
    and what the operations it made dormant (a regenerate, a turn over a rewound tail) project.
    A turn that made nothing dormant reads ``placement`` only, never ``facts``: a story with no
    placement row has no scene to write (the Lite path issues no statement on ``facts``)."""
    if not followed:
        has_placement = await conn.scalar(select(exists().where(placement.c.story_id == story_id)))
        if not has_placement:
            return
    elif not await _has_canon(conn, story_id):
        return
    await refresh(
        conn,
        story_id,
        subjects=await _subjects_of(conn, story_id, sorted(set(followed))),
        from_turn=turn,
    )


# -- the registered projections ---------------------------------------------------------------


async def _derive_all(conn: AsyncConnection, story_id: str) -> Derived:
    versions = await _versions(conn, story_id, None)
    return derive(story_id, versions, await _known(conn, story_id, versions))


class _CanonProjection(Projection):
    """A structured projection: no event changes it; the fold derives it from canon."""

    def apply(self, rows: Rows, event: LogEvent) -> None:
        del rows, event

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        del conn, story_id, events
        return Rows(self.table)


class RelationshipsProjection(_CanonProjection):
    table = relationships

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows:
        return Rows(self.table, (await _derive_all(conn, story_id)).relationships.values())


class PlacementProjection(_CanonProjection):
    table = placement

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows:
        return Rows(self.table, (await _derive_all(conn, story_id)).placement.values())


class SceneStateProjection(_CanonProjection):
    """Registered after ``placement`` and ``relationships``: it reads their folded rows."""

    table = scene_state

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows:
        found = await _derive_all(conn, story_id)
        scene_ids = await _scene_predicates(conn, story_id)
        presences = [r for r in found.relationships.values() if r["predicate_id"] in scene_ids]
        rows = await _scenes(conn, story_id, list(found.placement.values()), presences)
        return Rows(self.table, rows.values())


# -- reads ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityRef:
    entity_id: str
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class SceneView:
    """A turn's scene for one viewpoint: the location and the entities present."""

    turn: int
    variant: int | None
    location: EntityRef | None
    present: tuple[EntityRef, ...]
    viewpoint: str


@dataclass(frozen=True, slots=True)
class RelationshipView:
    """One relationship of an entity, as seen from it (``direction``: ``out`` when the entity
    is the subject; a symmetric predicate's row always reads ``out``)."""

    predicate_id: str
    predicate: str
    direction: str
    other: EntityRef
    qualifiers: Mapping[str, Any]
    valid_from: int | None
    valid_to: int | None
    source_fact_id: str | None


@dataclass(frozen=True, slots=True)
class PlacementView:
    """Where an entity is: at a location (with the places containing it, ``located_in``
    transitively) or in a container."""

    location: EntityRef | None
    container: EntityRef | None
    within: tuple[EntityRef, ...]
    valid_from: int | None
    valid_to: int | None
    source_fact_id: str | None


@dataclass(frozen=True, slots=True)
class InventoryItem:
    item: EntityRef
    source_fact_id: str | None


@dataclass(frozen=True, slots=True)
class ProfileView:
    """An entity's standing profile as of a turn for one viewpoint (FR-RET-6)."""

    entity: EntityRef
    as_of: int
    relationships: tuple[RelationshipView, ...]
    placement: PlacementView | None
    inventory: tuple[InventoryItem, ...]
    viewpoint: str


@dataclass(frozen=True, slots=True)
class _ReadContext:
    head: int
    now: int
    viewpoint: str


WITHIN_DEPTH = 8
"""How many ``located_in`` steps a profile's placement follows up the containment hierarchy."""


async def _read_context(
    conn: AsyncConnection, story_id: str, as_of: int | None, viewpoint: str | None
) -> tuple[_ReadContext, int]:
    from epistrel.core.settings_schema import StorySettings
    from epistrel.core.viewpoint import resolve_viewpoint

    row = (
        await conn.execute(
            select(
                stories.c.next_turn_index, stories.c.story_minute_zero, stories.c.settings
            ).where(stories.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    head = int(row.next_turn_index) - 1
    cast = [
        str(c)
        for c in await conn.scalars(
            select(characters.c.id).where(characters.c.story_id == story_id)
        )
    ]
    resolved = resolve_viewpoint(
        viewpoint,
        actor_character_id=None,
        story_default=StorySettings.from_row(row.settings).default_output_viewpoint,
        cast_ids=cast,
        field="query.viewpoint",
    )
    through_turn = head if as_of is None else as_of
    if through_turn < 0 or through_turn > head:
        raise InvalidRequest(
            "as_of must be a turn of the story, from 0 to its head",
            {"field": "query.as_of", "head": head},
        )
    now = story_time_at(through_turn, TimeBasis(minute_zero=int(row.story_minute_zero)))
    return _ReadContext(head=head, now=now, viewpoint=resolved), through_turn


def _visible(viewpoint: str) -> Callable[[Mapping[str, Any]], bool]:
    from epistrel.core.canon.facts import Visibility, visible_to

    def check(row: Mapping[str, Any]) -> bool:
        return visible_to(Visibility.model_validate(row["visibility"]), viewpoint)

    return check


async def _refs(conn: AsyncConnection, story_id: str, ids: Iterable[str]) -> dict[str, EntityRef]:
    wanted = sorted({i for i in ids if i is not None})
    if not wanted:
        return {}
    found = await conn.execute(
        select(entities.c.id, entities.c.kind, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.id.in_(wanted)
        )
    )
    return {
        str(r.id): EntityRef(entity_id=str(r.id), kind=str(r.kind), name=str(r.canonical_name))
        for r in found
    }


def _ref(refs: Mapping[str, EntityRef], entity_id: str) -> EntityRef:
    return refs.get(entity_id) or EntityRef(entity_id=entity_id, kind="other", name="")


async def _variant_at(conn: AsyncConnection, story_id: str, turn: int) -> int | None:
    if turn < 1:
        return None
    found = await _active_turns(conn, story_id, turn)
    return next((variant for t, variant, _ in found if t == turn), None)


async def scene_view(
    conn: AsyncConnection,
    story_id: str,
    *,
    as_of: int | None = None,
    viewpoint: str | None = None,
) -> SceneView:
    """The scene as of turn ``as_of`` (default the head) for ``viewpoint`` (default the story's
    default output viewpoint; 422 ``viewpoint_unresolved`` when none resolves). The omniscient
    read answers the stored row; a character's presence is recomputed from the rows its
    visibility admits. 422 beyond the head."""
    ctx, turn = await _read_context(conn, story_id, as_of, viewpoint)
    variant = await _variant_at(conn, story_id, turn)
    found: Scene | None = None
    if ctx.viewpoint == OMNISCIENT and variant is not None:
        stored = (
            await conn.execute(
                select(scene_state).where(
                    scene_state.c.story_id == story_id,
                    scene_state.c.turn == turn,
                    scene_state.c.variant == variant,
                )
            )
        ).one_or_none()
        found = (
            Scene(None, ())
            if stored is None
            else Scene(
                None if stored.location_id is None else str(stored.location_id),
                tuple(str(i) for i in stored.present_entity_ids),
            )
        )
    if found is None:
        places, presences = await _scene_inputs(conn, story_id, turn)
        found = scene(
            places,
            presences,
            await _actor(conn, story_id),
            turn_position(turn),
            ctx.now,
            _visible(ctx.viewpoint),
        )
    refs = await _refs(
        conn, story_id, [*found.present, *([found.location_id] if found.location_id else [])]
    )
    return SceneView(
        turn=turn,
        variant=variant,
        location=None if found.location_id is None else _ref(refs, found.location_id),
        present=tuple(_ref(refs, i) for i in found.present),
        viewpoint=ctx.viewpoint,
    )


async def _within(
    conn: AsyncConnection, story_id: str, location: str, turn: int, ctx: _ReadContext
) -> list[str]:
    """The places containing ``location`` through ``located_in``, nearest first."""
    from epistrel.core.canon import facts as canon
    from epistrel.core.propositions.vocabulary import LOCATED_IN

    chain: list[str] = []
    seen = {location}
    current = location
    for _ in range(WITHIN_DEPTH):
        found = await canon.canon(
            conn,
            story_id,
            story_time=ctx.now,
            through_position=turn_position(turn),
            viewpoint=ctx.viewpoint,
            subject=current,
            predicate=LOCATED_IN,
        )
        parents = sorted(
            {
                str(f.proposition.object_id)
                for f in found
                if f.proposition.object_id is not None
                and f.proposition.polarity.value == "affirmed"
            }
            - seen
        )
        if not parents:
            break
        current = parents[0]
        seen.add(current)
        chain.append(current)
    return chain


def _current(
    registry: Registry, ctx: _ReadContext, turn: int
) -> Callable[[Mapping[str, Any], str | None], bool]:
    """Whether a projection row holds as of ``turn`` for ``ctx``'s viewpoint: in the record
    through the turn, visible, and (unless its predicate is ``permanent``) valid at ``ctx.now``
    (the turn's story-time, #438)."""
    from epistrel.core.propositions.model import TemporalKind

    visible = _visible(ctx.viewpoint)
    position = turn_position(turn)

    def current(row: Mapping[str, Any], predicate_id: str | None = None) -> bool:
        if not (through(row, position) and visible(row)):
            return False
        predicate = registry.get(predicate_id) if predicate_id is not None else None
        if predicate is not None and predicate.temporal_kind is TemporalKind.PERMANENT:
            return True
        return covers(row, ctx.now)

    return current


def _relationship_views(
    entity_id: str,
    rows: Iterable[Mapping[str, Any]],
    registry: Registry,
    refs: Mapping[str, EntityRef],
    omniscient: bool,
) -> tuple[RelationshipView, ...]:
    """``entity_id``'s relationships as seen from it, from its current ``relationships`` rows
    (either direction), in the profile's order."""
    views = []
    for row in rows:
        predicate = registry.get(str(row["predicate_id"]))
        outgoing = row["subject_id"] == entity_id
        symmetric = predicate is not None and predicate.symmetric
        other = str(row["object_id"]) if outgoing else str(row["subject_id"])
        views.append(
            RelationshipView(
                predicate_id=str(row["predicate_id"]),
                predicate="" if predicate is None else predicate.name,
                direction="out" if outgoing or symmetric else "in",
                other=_ref(refs, other),
                qualifiers=dict(row["qualifiers"] or {}),
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                source_fact_id=str(row["source_fact_id"]) if omniscient else None,
            )
        )
    views.sort(key=lambda v: (v.predicate, v.direction, v.other.entity_id, v.source_fact_id or ""))
    return tuple(views)


def _other(row: Mapping[str, Any], entity_id: str) -> str:
    return str(row["object_id"]) if row["subject_id"] == entity_id else str(row["subject_id"])


async def relationships_views(
    conn: AsyncConnection,
    story_id: str,
    entity_ids: Sequence[str],
    *,
    as_of: int | None = None,
    viewpoint: str | None = None,
) -> tuple[int, dict[str, tuple[RelationshipView, ...]]]:
    """The turn read (``as_of``, default the head) and, for each of ``entity_ids``, its
    relationships exactly as ``profile_view`` answers them, in a fixed number of statements
    whatever the number of entities (#442): the read context, the registry once, one keyed read
    of the ``relationships`` rows touching any of them, and one read of the entity refs. The ids
    are the story's entities (the caller's ``EntityIndex``); none is checked one by one."""
    from epistrel.core.propositions.registry import load

    ctx, turn = await _read_context(conn, story_id, as_of, viewpoint)
    wanted = list(dict.fromkeys(entity_ids))
    if not wanted:
        return turn, {}
    registry = await load(conn, story_id)
    current = _current(registry, ctx, turn)
    rows = [
        dict(r._mapping)
        for r in await conn.execute(
            select(relationships).where(
                relationships.c.story_id == story_id,
                or_(relationships.c.subject_id.in_(wanted), relationships.c.object_id.in_(wanted)),
            )
        )
    ]
    rows = [r for r in rows if current(r, str(r["predicate_id"]))]
    touching: dict[str, list[dict[str, Any]]] = {e: [] for e in wanted}
    for row in rows:
        for end in dict.fromkeys((str(row["subject_id"]), str(row["object_id"]))):
            if end in touching:
                touching[end].append(row)
    refs = await _refs(
        conn, story_id, [_other(r, e) for e, found in touching.items() for r in found]
    )
    omniscient = ctx.viewpoint == OMNISCIENT
    return turn, {
        e: _relationship_views(e, found, registry, refs, omniscient)
        for e, found in touching.items()
    }


async def profile_view(
    conn: AsyncConnection,
    story_id: str,
    entity_id: str,
    *,
    as_of: int | None = None,
    viewpoint: str | None = None,
) -> ProfileView:
    """``entity_id``'s standing profile as of turn ``as_of`` for ``viewpoint``: its
    relationships in both directions, where it is, and what it holds. 404 ``not_found`` for an
    entity that is not the story's; a placement of ``null`` when it has none."""
    ctx, turn = await _read_context(conn, story_id, as_of, viewpoint)
    entity = (await _refs(conn, story_id, [entity_id])).get(entity_id)
    if entity is None:
        raise StoryEntityNotFound(
            f"entity {entity_id} is not one of the story's",
            {"story_id": story_id, "entity_id": entity_id},
        )
    return (await _profiles(conn, story_id, ctx, turn, {entity_id: entity}))[entity_id]


async def profile_views(
    conn: AsyncConnection,
    story_id: str,
    entity_ids: Sequence[str],
    *,
    as_of: int | None = None,
    viewpoint: str | None = None,
) -> dict[str, ProfileView]:
    """The standing profile of each of ``entity_ids`` that is one of the story's, exactly as
    ``profile_view`` answers it, in a number of statements that does not grow with the number
    of entities (#451): the read context, the entities, the registry once, one keyed read each
    of the ``relationships`` and ``placement`` rows touching any of them, the containing places
    of each distinct location (``WITHIN_DEPTH`` canon reads at most), and the entity refs. An
    id that is not the story's entity is left out (``profile_view`` answers 404 for it)."""
    ctx, turn = await _read_context(conn, story_id, as_of, viewpoint)
    wanted = list(dict.fromkeys(entity_ids))
    if not wanted:
        return {}
    known = await _refs(conn, story_id, wanted)
    return await _profiles(conn, story_id, ctx, turn, {e: known[e] for e in wanted if e in known})


async def _profiles(
    conn: AsyncConnection,
    story_id: str,
    ctx: _ReadContext,
    turn: int,
    entities_by_id: Mapping[str, EntityRef],
) -> dict[str, ProfileView]:
    """The profiles of ``entities_by_id`` (known entities) as of ``turn`` for ``ctx``: the one
    builder ``profile_view`` and ``profile_views`` share, so both answer identical views."""
    from epistrel.core.propositions.registry import load

    wanted = list(entities_by_id)
    if not wanted:
        return {}
    registry = await load(conn, story_id)
    omniscient = ctx.viewpoint == OMNISCIENT
    current = _current(registry, ctx, turn)

    rel_rows = [
        dict(r._mapping)
        for r in await conn.execute(
            select(relationships).where(
                relationships.c.story_id == story_id,
                or_(relationships.c.subject_id.in_(wanted), relationships.c.object_id.in_(wanted)),
            )
        )
    ]
    rel_rows = [r for r in rel_rows if current(r, str(r["predicate_id"]))]
    place_rows = [
        dict(r._mapping)
        for r in await conn.execute(
            select(placement).where(
                placement.c.story_id == story_id,
                or_(placement.c.entity_id.in_(wanted), placement.c.container_id.in_(wanted)),
            )
        )
    ]
    place_rows = [r for r in place_rows if current(r, None)]
    touching: dict[str, list[dict[str, Any]]] = {e: [] for e in wanted}
    for row in rel_rows:
        for end in dict.fromkeys((str(row["subject_id"]), str(row["object_id"]))):
            if end in touching:
                touching[end].append(row)
    own: dict[str, Row | None] = {
        e: _latest(r for r in place_rows if str(r["entity_id"]) == e) for e in wanted
    }
    held = {
        e: sorted(
            (
                r
                for r in place_rows
                if r["container_id"] is not None and str(r["container_id"]) == e
            ),
            key=lambda r: (str(r["entity_id"]), str(r["source_fact_id"])),
        )
        for e in wanted
    }
    within_of: dict[str, list[str]] = {}
    for found in own.values():
        if found is not None and found["location_id"] is not None:
            place = str(found["location_id"])
            if place not in within_of:
                within_of[place] = await _within(conn, story_id, place, turn, ctx)
    refs = await _refs(
        conn,
        story_id,
        [
            *(_other(r, e) for e, rows in touching.items() for r in rows),
            *(i for chain in within_of.values() for i in chain),
            *(str(r["entity_id"]) for rows in held.values() for r in rows),
            *(str(o["location_id"] or o["container_id"]) for o in own.values() if o is not None),
        ],
    )
    views: dict[str, ProfileView] = {}
    for e in wanted:
        mine = own[e]
        placed = None
        if mine is not None:
            location = None if mine["location_id"] is None else str(mine["location_id"])
            placed = PlacementView(
                location=None if location is None else _ref(refs, location),
                container=(
                    None if mine["container_id"] is None else _ref(refs, str(mine["container_id"]))
                ),
                within=tuple(_ref(refs, i) for i in within_of.get(location or "", [])),
                valid_from=mine["valid_from"],
                valid_to=mine["valid_to"],
                source_fact_id=str(mine["source_fact_id"]) if omniscient else None,
            )
        views[e] = ProfileView(
            entity=entities_by_id[e],
            as_of=turn,
            relationships=_relationship_views(e, touching[e], registry, refs, omniscient),
            placement=placed,
            inventory=tuple(
                InventoryItem(
                    item=_ref(refs, str(r["entity_id"])),
                    source_fact_id=str(r["source_fact_id"]) if omniscient else None,
                )
                for r in held[e]
            ),
            viewpoint=ctx.viewpoint,
        )
    return views


__all__ = [
    "AFTER_WRITE",
    "PROJECTIONS",
    "Derived",
    "EntityRef",
    "FactVersion",
    "InventoryItem",
    "PlacementProjection",
    "PlacementView",
    "ProfileView",
    "RelationshipView",
    "RelationshipsProjection",
    "Scene",
    "SceneStateProjection",
    "SceneView",
    "after_turn",
    "covers",
    "derive",
    "follow_membership_change",
    "follow_operations",
    "on_fact_change",
    "profile_view",
    "profile_views",
    "refresh",
    "relationships_views",
    "scene",
    "scene_view",
    "through",
]
