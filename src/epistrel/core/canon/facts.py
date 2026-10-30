"""Bitemporal objective canon: write a fact, supersede it, and ask both time questions
(FR-BITEMP-1..5, FR-STORE-9; docs/02 §4.4, §4.6; #133).

**Three coordinates, one job each** (FR-BITEMP-5). A fact version (``core.tables.facts``)
carries its **valid-time** (story-minutes: when it is true in the story), its
**transaction-time** (``tx_from``/``tx_to``: when the engine recorded and first closed it) and
its **narrative position** (``narrative_turn``/``turn_variant`` of the operation that wrote it,
never ``seq``), with a **membership** that follows that operation. Truncation, untruncation,
swap and regenerate move membership only (``core.membership``); nothing here writes valid-time
or transaction-time when they do.

**Writes** run inside an ``Operation``'s commit transaction (a commit step), so a fact commits
with the turn or control action that produced it, under the story lock:

- ``commit_fact`` stores the proposition (``propositions.store``), then the fact version with
  ``tx_from`` the transaction's time. A current version of the same uniqueness key (a
  functional predicate) or of the opposite polarity, over overlapping valid-time, is
  superseded automatically: it is closed (``tx_to``, ``superseded_by``) and what lies outside
  the new valid-time is kept as remainder versions written by the same operation (the earlier
  part continues its lineage, a later part starts its own). An identical proposition already
  current over the whole span (a ``multi`` predicate) is a no-op answering the existing
  version. It appends a ``derived`` ``fact_committed`` event and runs ``FACT_HOOKS`` (#136's
  ``proposition_changes`` writer, #137's structured projections) in the same transaction; a hook's
  exception aborts the commit.
- ``supersede`` closes one version and writes its successor in the same lineage, with the
  same uniqueness key (``KeyMismatch`` otherwise), inheriting what it is not given. A retcon is
  a supersede with a wider valid-time and ``authority = author``. Nothing is ever deleted
  (FR-BITEMP-2); the trigger refuses it outside erasure and story deletion.

**Current version.** A version is closed by a row whose ``supersedes`` names it. In the
corrected view a version is current when it is active and no **active** row closes it, so when
the operation that closed it is discarded (a truncation), it is current again without any
write, and an untruncation closes it again. ``tx_to`` records the first closure and is never
rewritten.

**Reads** (``canon``, ``canon_at``, ``canon_as_recorded``) take a viewpoint and filter by
visibility: ``objective`` and ``public`` reach every viewpoint, a ``characters`` scope only the
characters it names and ``omniscient``, and ``engine`` (true ages) only ``omniscient``.
``narrator``, ``director`` and ``player`` readers see ``objective`` and ``public``.

- ``canon_at(story_id, story_time=T)``: what is true at story-time T, as the corrected record
  says now: current active versions whose valid-time covers T (``reading="every"``: under every
  admissible reading of approximate bounds; ``"any"``: possibly), a ``permanent`` predicate's
  at every T, optionally as the corrected history through a narrative position
  (``through_position``): only versions written at or before it (position-less versions always
  qualify, FR-STORE-26), and a version closed only by a later row is current there.
- ``canon_as_recorded(story_id, record_time=T)``: what the record said at record-time T: each
  version recorded by T that no row recorded by T had closed, regardless of later supersession.
  A row that left the timeline before T (its ``membership_tx``) is not read, and one that left
  after T is (``membership.active_as_of``).

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    ColumnElement,
    and_,
    cast,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, INT8RANGE, JSONB, Range, array
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import readset
from epistrel.core.canon.errors import (
    FactInactive,
    FactNotFound,
    FactSuperseded,
    InvalidVisibility,
    KeyMismatch,
    UnknownPredicate,
)
from epistrel.core.enums import (
    Authority,
    EventCategory,
    FactAuthority,
    FactCommitState,
    HolderKind,
    Membership,
    Provenance,
    RecordProvenance,
)
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import event_record, fact_record
from epistrel.core.errors import (
    IdentityMergeNotYet,
    InvalidRequest,
    ViewpointCannotAssignTruth,
    ViewpointUnresolved,
)
from epistrel.core.events.registry import validate
from epistrel.core.events.types import FactCommitted
from epistrel.core.events.types import FactSuperseded as FactSupersededEvent
from epistrel.core.ids import new_ulid
from epistrel.core.membership import (
    Through,
    active_as_of,
    active_only,
    bound_of,
    insert_derived,
    step_of,
)
from epistrel.core.projections import structured
from epistrel.core.propositions import store
from epistrel.core.propositions.codec import proposition_from_row
from epistrel.core.propositions.model import (
    Bound,
    Instant,
    Interval,
    Predicate,
    Proposition,
    TemporalKind,
)
from epistrel.core.propositions.registry import (
    Registry,
    canonical,
    file_identity_proposal,
    load,
)
from epistrel.core.resolution.identity import pair_of
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.tables import characters, entities, events, predicates, propositions
from epistrel.core.tables.facts import facts
from epistrel.core.visibility import (
    OBJECTIVE,
    READER_VIEWPOINTS,
    SHARED_SCOPES,
    Visibility,
    VisibilityScope,
    visible_clause,
    visible_to,
)

FACT_COMMITTED = "fact_committed"
FACT_SUPERSEDED = "fact_superseded"

TICK = timedelta(microseconds=1)
"""The step a second closure in one transaction takes past its version's ``tx_from``, so no
transaction-time interval is empty."""

# -- models -----------------------------------------------------------------------------------


class FactOperation(Protocol):
    """What a fact needs of the operation writing it (an ``Operation`` inside its commit):
    its id, story, principal and narrative position."""

    operation_id: str
    story_id: str | None
    principal: str
    narrative_turn: int | None
    turn_variant: int | None


class Fact(BaseModel):
    """One fact version with its proposition (see ``core.tables.facts`` for the columns).
    ``superseded_ids`` and ``remainder_ids`` are set on the answer of a write: the versions it
    closed and the remainders it kept of them."""

    model_config = ConfigDict(frozen=True)

    id: str
    story_id: str
    lineage_id: str
    proposition_id: str
    proposition: Proposition
    subject_id: str
    predicate_id: str
    holder: HolderKind = HolderKind.CANON
    valid_from: int | None
    valid_to: int | None
    valid_from_inner: int | None
    valid_to_inner: int | None
    tx_from: datetime
    tx_to: datetime | None
    provenance: RecordProvenance
    authority: FactAuthority
    author_locked: bool = False
    visibility: Visibility = OBJECTIVE
    operation_id: str
    source_event_ids: tuple[str, ...] = ()
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None = None
    slot_k: int | None = None
    membership: Membership
    membership_tx: datetime
    superseded_by: str | None = None
    supersedes: tuple[str, ...] = ()
    anchored_to: str | None = None
    commit_state: FactCommitState = FactCommitState.COMMITTED
    thunk_id: str | None = None
    resolution_level: str | None = None
    """FR-COMMIT-9 (#154): ``consequential`` or ``incidental``; ``None`` reads as
    ``consequential``."""
    superseded_ids: tuple[str, ...] = ()
    remainder_ids: tuple[str, ...] = Field(default=())

    @property
    def valid_time(self) -> Interval:
        """The fact's own valid-time (outer and inner bounds)."""
        return Interval(
            start=None
            if self.valid_from is None
            else Bound(inner=_req(self.valid_from_inner), outer=self.valid_from),
            end=None
            if self.valid_to is None
            else Bound(inner=_req(self.valid_to_inner), outer=self.valid_to),
        )


def _req(value: int | None) -> int:
    if value is None:  # pragma: no cover - the inner_bounds CHECK
        raise ValueError("an outer bound has its inner bound")
    return value


@dataclass(frozen=True, slots=True)
class FactChange:
    """What a write did, as ``FACT_HOOKS`` see it: the new version, the versions it closed and
    the remainders it kept, in the writing operation."""

    story_id: str
    operation_id: str
    fact: Fact
    superseded_ids: tuple[str, ...]
    remainders: tuple[Fact, ...] = ()
    closed: tuple[Fact, ...] = field(default=())


FactHook = Callable[[AsyncConnection, FactChange], Awaitable[None]]

FACT_HOOKS: list[FactHook] = []
"""Run in registration order after every fact write, on the write's connection (its
transaction): #136's ``proposition_changes`` writer (``core.readset.record_fact_change``,
registered below) and #137's projection apply register here."""


def register_fact_hook(hook: FactHook) -> FactHook:
    """Add ``hook`` to ``FACT_HOOKS`` (once)."""
    if hook not in FACT_HOOKS:
        FACT_HOOKS.append(hook)
    return hook


def remove_fact_hook(hook: FactHook) -> None:
    if hook in FACT_HOOKS:
        FACT_HOOKS.remove(hook)


register_fact_hook(readset.record_fact_change)
"""#136's change index: every write's added and closed propositions, in its transaction."""

register_fact_hook(structured.on_fact_change)
"""#137's structured projections: the write's subjects' relationship and placement rows, and the
scenes they change, in its transaction."""


def default_authority(provenance: RecordProvenance) -> FactAuthority:
    """The authority a provenance implies: the author's and the seed's facts are the author's,
    an external authority's are external, everything else is the engine's."""
    if provenance in (RecordProvenance.AUTHOR, RecordProvenance.SEED):
        return FactAuthority.AUTHOR
    if provenance is RecordProvenance.EXTERNAL:
        return FactAuthority.EXTERNAL
    return FactAuthority.ENGINE


# -- bounds -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Bounds:
    """Outer (possible) and inner (definite) valid-time bounds; ``None`` is open."""

    lo: int | None
    lo_inner: int | None
    hi: int | None
    hi_inner: int | None

    @classmethod
    def of(cls, valid_time: Interval | Instant) -> _Bounds:
        if isinstance(valid_time, Instant):
            return cls(valid_time.at, valid_time.at, valid_time.until, valid_time.until)
        start, end = valid_time.start, valid_time.end
        return cls(
            None if start is None else start.outer,
            None if start is None else start.inner,
            None if end is None else end.outer,
            None if end is None else end.inner,
        )

    def columns(self) -> dict[str, Any]:
        return {
            "valid_from": self.lo,
            "valid_from_inner": self.lo_inner,
            "valid_to": self.hi,
            "valid_to_inner": self.hi_inner,
            "valid_span": Range(self.lo, self.hi, bounds="[)"),
        }

    def span(self) -> Range[int]:
        return Range(self.lo, self.hi, bounds="[)")

    def covers(self, other: _Bounds) -> bool:
        low = self.lo is None or (other.lo is not None and self.lo <= other.lo)
        high = self.hi is None or (other.hi is not None and other.hi <= self.hi)
        return low and high


def _bounds_of(fact: Fact) -> _Bounds:
    return _Bounds(fact.valid_from, fact.valid_from_inner, fact.valid_to, fact.valid_to_inner)


def _remainders(old: _Bounds, new: _Bounds) -> tuple[_Bounds | None, _Bounds | None]:
    """What is left of ``old`` before and after ``new`` (their possible spans overlap)."""
    before = None
    if new.lo is not None and (old.lo is None or old.lo < new.lo):
        start_inner = None if old.lo_inner is None else min(old.lo_inner, new.lo)
        before = _Bounds(old.lo, start_inner, new.lo, new.lo)
    after = None
    if new.hi is not None and (old.hi is None or new.hi < old.hi):
        end_inner = None if old.hi_inner is None else max(old.hi_inner, new.hi)
        after = _Bounds(new.hi, new.hi, old.hi, end_inner)
    return before, after


# -- the visibility filter ----------------------------------------------------------------------


async def _cast(conn: AsyncConnection, story_id: str) -> set[str]:
    return {
        str(c)
        for c in await conn.scalars(
            select(characters.c.id).where(characters.c.story_id == story_id)
        )
    }


async def check_viewpoint(conn: AsyncConnection, story_id: str, viewpoint: str) -> None:
    """422 ``viewpoint_unresolved`` unless ``viewpoint`` is one a canon read takes."""
    if viewpoint == OMNISCIENT or viewpoint in READER_VIEWPOINTS:
        return
    member = viewpoint.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
    if viewpoint.startswith(CHARACTER_VIEWPOINT_PREFIX) and member in await _cast(conn, story_id):
        return
    raise ViewpointUnresolved(
        "a canon read's viewpoint is omniscient, narrator, director, player or "
        "character:<id> of the story's cast",
        {"field": "viewpoint", "reason": "unknown_viewpoint"},
    )


# -- reading versions ---------------------------------------------------------------------------

_P = "p_"


def _proposition_columns() -> list[Any]:
    return [c.label(f"{_P}{c.name}") for c in propositions.c]


def _fact_of(row: Any, **extra: Any) -> Fact:
    m: Mapping[str, Any] = row._mapping
    proposition = proposition_from_row(
        {c.name: m[f"{_P}{c.name}"] for c in propositions.c if c.name not in ("id", "story_id")}
    )
    return Fact(
        id=str(m["id"]),
        story_id=str(m["story_id"]),
        lineage_id=str(m["lineage_id"]),
        proposition_id=str(m["proposition_id"]),
        proposition=proposition,
        subject_id=str(m["subject_id"]),
        predicate_id=str(m["predicate_id"]),
        holder=HolderKind(m["holder"]),
        valid_from=m["valid_from"],
        valid_to=m["valid_to"],
        valid_from_inner=m["valid_from_inner"],
        valid_to_inner=m["valid_to_inner"],
        tx_from=m["tx_from"],
        tx_to=m["tx_to"],
        provenance=RecordProvenance(m["provenance"]),
        authority=FactAuthority(m["authority"]),
        author_locked=bool(m["author_locked"]),
        visibility=Visibility.model_validate(m["visibility"]),
        operation_id=str(m["operation_id"]),
        source_event_ids=tuple(str(e) for e in m["source_event_ids"] or ()),
        narrative_turn=m["narrative_turn"],
        turn_variant=m["turn_variant"],
        beat=m["beat"],
        slot_k=m["slot_k"],
        membership=Membership(m["membership"]),
        membership_tx=m["membership_tx"],
        superseded_by=None if m["superseded_by"] is None else str(m["superseded_by"]),
        supersedes=tuple(str(s) for s in m["supersedes"] or ()),
        anchored_to=None if m["anchored_to"] is None else str(m["anchored_to"]),
        commit_state=FactCommitState(m["commit_state"]),
        thunk_id=None if m["thunk_id"] is None else str(m["thunk_id"]),
        resolution_level=m["resolution_level"],
        **extra,
    )


Position = Through
"""A narrative position bound: ``(turn, slot_k)`` (slot 0 is the turn itself, ``N``; slot k the
k-th inter-turn slot after it, ``N⁺k``), or a full ``core.membership.Position`` (#164: an
exposure frame bounds canon at its beat and step, FR-STORE-31)."""


def _position_key(t: Any) -> Any:
    return tuple_(
        t.c.narrative_turn,
        func.coalesce(t.c.slot_k, 0),
        func.coalesce(t.c.beat, 0),
        t.c.step,
    )


def _through(t: Any, position: Position) -> ColumnElement[bool]:
    """Row ``t`` was written at or before ``position`` in ``(turn, slot_k, beat, step)`` order,
    or has no position (FR-STORE-26). A ``(turn, slot_k)`` bound is the end of that turn or
    slot, so it admits every beat and step of it."""
    bound = bound_of(position)
    key = _POSITION_KEYS.get(t) if t is _CANON or t is _CLOSER else None
    if key is None:
        key = _position_key(t)
    return or_(
        t.c.narrative_turn.is_(None),
        key <= tuple_(*(literal(v) for v in bound.key())),
    )


_CLOSER = facts.alias("closer")
"""``_closed_by``'s row, built once: an alias proxies every ``facts`` column when it is first
read, and a canon read made that per call (#520)."""
_CANON = facts.alias("f")
"""``canon``'s version row, built once (#520, as ``_CLOSER``)."""


def _closed_by(
    f: Any, record_time: datetime | None, through: Position | None = None
) -> ColumnElement[bool]:
    """Some row in the timeline (at ``record_time``, or now; at or before ``through`` when
    given) closes version ``f``."""
    s = _CLOSER
    where = [
        s.c.story_id == f.c.story_id,
        s.c.supersedes.contains(cast(array([f.c.id]), ARRAY(facts.c.id.type))),
    ]
    if record_time is None:
        where.append(active_only(s))
    else:
        where += [s.c.tx_from <= record_time, active_as_of(s, record_time)]
    if through is not None:
        where.append(_through(s, through))
    return exists().where(*where)


def _open(
    f: Any, record_time: datetime | None, through: Position | None = None
) -> list[ColumnElement[bool]]:
    """Version ``f`` is in the record and not closed (now, or at ``record_time``), in the view
    through ``through`` when given (a version a later row closes is current through N)."""
    where = [~_closed_by(f, record_time, through)]
    if through is not None:
        where.append(_through(f, through))
    if record_time is None:
        return [active_only(f), *where]
    return [f.c.tx_from <= record_time, active_as_of(f, record_time), *where]


def _select(f: Any) -> Any:
    if f is _CANON:
        return _CANON_SELECT
    return _build_select(f)


def _build_select(f: Any) -> Any:
    return select(f, *_proposition_columns()).select_from(
        f.join(propositions, propositions.c.id == f.c.proposition_id)
    )


_CANON_SELECT = _build_select(_CANON)
"""``canon``'s version row with its proposition, built once (#520; ``.where`` copies it)."""
_POSITION_KEYS = {_CANON: _position_key(_CANON), _CLOSER: _position_key(_CLOSER)}
"""``_through``'s row side for the module's own aliases, built once (#520)."""


async def _load(conn: AsyncConnection, story_id: str, fact_id: str) -> tuple[Fact, bool]:
    """Version ``fact_id`` and whether it is current (open in the corrected view)."""
    f = facts.alias("f")
    row = (
        await conn.execute(
            _select(f)
            .add_columns(_closed_by(f, None).label("closed"))
            .where(f.c.story_id == story_id, f.c.id == fact_id)
        )
    ).one_or_none()
    if row is None:
        raise FactNotFound(f"fact {fact_id} does not exist", {"fact_id": fact_id})
    return _fact_of(row), not bool(row.closed)


async def _current_overlapping(
    conn: AsyncConnection, story_id: str, stored: Proposition, bounds: _Bounds
) -> list[Fact]:
    f = facts.alias("f")
    found = await conn.execute(
        _select(f)
        .where(
            f.c.story_id == story_id,
            f.c.subject_id == stored.subject_id,
            f.c.predicate_id == stored.predicate_id,
            f.c.holder == HolderKind.CANON.value,
            f.c.valid_span.op("&&")(literal(bounds.span(), INT8RANGE)),
            *_open(f, None),
        )
        .order_by(f.c.valid_from.nulls_first(), f.c.id)
    )
    return [_fact_of(r) for r in found]


def _conflicts(predicate: Predicate, stored: Proposition, current: Fact) -> bool:
    """Whether ``current`` cannot stay current beside ``stored`` over overlapping time: the
    same proposition (either polarity), or for a functional predicate, an affirmed other object
    under the same uniqueness key."""
    other = current.proposition
    if other.normalized_key() == stored.normalized_key():
        return True
    return (
        predicate.max_cardinality == 1
        and stored.polarity.value == "affirmed"
        and other.polarity.value == "affirmed"
        and predicate.uniqueness_key(other) == predicate.uniqueness_key(stored)
    )


# -- writing --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Write:
    """One version to write and what it carries over."""

    story_id: str
    proposition: Proposition
    provenance: RecordProvenance
    authority: FactAuthority
    visibility: Visibility
    operation: FactOperation
    anchored_to: str | None
    valid_time: Interval | Instant | None
    source_event_ids: tuple[str, ...]
    beat: int | None
    slot_k: int | None
    author_locked: bool = False
    commit_state: FactCommitState = FactCommitState.COMMITTED
    replaces: Fact | None = None
    thunk_id: str | None = None
    resolution_level: str | None = None


def _event_provenance(operation: FactOperation) -> Provenance:
    found = getattr(operation, "provenance", None)
    return found if isinstance(found, Provenance) else Provenance.ENGINE


async def _append_event(
    conn: AsyncConnection,
    w: _Write,
    type_: str,
    payload: FactCommitted | FactSupersededEvent,
) -> str:
    """Append the write's ``derived`` event to its operation, after the operation's own events
    (the caller holds the story lock: a commit step)."""
    model = validate(type_, payload)
    body = model.model_dump(mode="json")
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == w.story_id)
    )
    event_id = new_ulid()
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=w.story_id,
            seq=int(last or 0) + 1,
            operation_id=w.operation.operation_id,
            category=EventCategory.DERIVED.value,
            narrative_turn=w.operation.narrative_turn,
            turn_variant=w.operation.turn_variant,
            beat=w.beat,
            step=step_of(w.beat, w.operation.operation_id),
            slot_k=w.slot_k,
            principal=w.operation.principal,
            type=type_,
            payload=body,
            provenance=_event_provenance(w.operation).value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )
    await index_records(
        conn,
        [
            event_record(
                SimpleNamespace(id=event_id, story_id=w.story_id, character_id=None, payload=body)
            )
        ],
    )
    return event_id


async def _validate(conn: AsyncConnection, w: _Write) -> None:
    operation_story = getattr(w.operation, "story_id", w.story_id)
    if operation_story != w.story_id:
        raise InvalidRequest(
            "a fact is written by an operation of its own story", {"field": "operation"}
        )
    if w.visibility.scope == "characters":
        unknown = sorted(set(w.visibility.characters or ()) - await _cast(conn, w.story_id))
        if unknown:
            raise InvalidVisibility(
                "a characters visibility scope names characters of the story's cast",
                {"field": "visibility.characters", "reason": "unknown_character"},
            )
    if w.anchored_to is not None:
        found = await conn.scalar(
            select(facts.c.id).where(facts.c.story_id == w.story_id, facts.c.id == w.anchored_to)
        )
        if found is None:
            raise InvalidRequest(
                "anchored_to names a fact of the story",
                {"field": "anchored_to", "reason": "unknown_fact"},
            )


async def _references(conn: AsyncConnection, w: _Write, stored: Proposition) -> frozenset[str]:
    """The subjects the version references: its visibility's characters and the characters its
    subject and object entities are (the resurrection guard's input)."""
    named = set(w.visibility.characters or ())
    ids = [i for i in (stored.subject_id, stored.object_id) if i is not None]
    named |= {
        str(c)
        for c in await conn.scalars(
            select(entities.c.character_id).where(
                entities.c.story_id == w.story_id,
                entities.c.id.in_(ids),
                entities.c.character_id.is_not(None),
            )
        )
    }
    return frozenset(named)


def _position(w: _Write) -> dict[str, Any] | None:
    turn = w.operation.narrative_turn
    if turn is None:
        return None
    return {"turn": turn, "variant": w.operation.turn_variant, "beat": w.beat}


async def _write(conn: AsyncConnection, w: _Write, registry: Registry | None) -> Fact:
    found = registry if registry is not None else await load(conn, w.story_id)
    predicate = found.get(w.proposition.predicate_id)
    if predicate is None:
        raise UnknownPredicate(
            f"predicate {w.proposition.predicate_id} is not registered for the story",
            {"field": "proposition.predicate_id", "predicate_id": w.proposition.predicate_id},
        )
    stored = canonical(w.proposition, found)
    old = w.replaces
    if old is not None and (
        stored.subject_id != old.subject_id
        or stored.predicate_id != old.predicate_id
        or predicate.uniqueness_key(stored) != predicate.uniqueness_key(old.proposition)
    ):
        raise KeyMismatch(
            "a supersede keeps the fact's subject, predicate and key qualifiers",
            {"fact_id": old.id, "lineage_id": old.lineage_id},
        )
    # The resurrection guard first: a reference to an erased subject is 409, whatever else.
    await guard.check_content(
        conn,
        w.story_id,
        [guard.Content((), await _references(conn, w, stored))],
        base_revision=None,
        path=__name__,
    )
    await _validate(conn, w)
    bounds = _Bounds.of(w.valid_time if w.valid_time is not None else stored.valid_time)
    overlapping = [
        c
        for c in await _current_overlapping(conn, w.story_id, stored, bounds)
        if old is None or c.id != old.id
    ]
    if old is None:
        for current in overlapping:
            same = current.proposition.normalized_key() == stored.normalized_key()
            if (
                same
                and current.proposition.polarity is stored.polarity
                and predicate.max_cardinality != 1
                and _bounds_of(current).covers(bounds)
            ):
                return current
    conflicting = [c for c in overlapping if _conflicts(predicate, stored, c)]
    closing = ([old] if old is not None else []) + conflicting

    proposition_id = await store.write_proposition(conn, w.story_id, w.proposition, found)
    now = (await conn.execute(select(func.transaction_timestamp()))).scalar_one()
    at = max([now, *(c.tx_from + TICK for c in closing)])
    fact_id = new_ulid()
    lineage_id = old.lineage_id if old is not None else fact_id
    position = _position(w)

    rows: list[dict[str, Any]] = []
    remainder_ids: list[str] = []
    sources = list(w.source_event_ids)
    for current in conflicting:
        before, after = _remainders(_bounds_of(current), bounds)
        kept_lineage = False
        for part in (before, after):
            if part is None:
                continue
            part_id = new_ulid()
            remainder_ids.append(part_id)
            rows.append(
                _row(
                    part_id,
                    w.story_id,
                    current.lineage_id if not kept_lineage else part_id,
                    current.proposition_id,
                    current.proposition,
                    part,
                    at,
                    current.provenance,
                    current.authority,
                    current.author_locked,
                    current.visibility,
                    w,
                    sources,
                    (current.id,),
                    current.anchored_to,
                    current.commit_state,
                    position,
                )
            )
            kept_lineage = True
    rows.append(
        _row(
            fact_id,
            w.story_id,
            lineage_id,
            proposition_id,
            stored,
            bounds,
            at,
            w.provenance,
            w.authority,
            w.author_locked,
            w.visibility,
            w,
            sources,
            tuple(c.id for c in closing),
            w.anchored_to,
            w.commit_state,
            position,
            thunk_id=w.thunk_id,
            resolution_level=w.resolution_level,
        )
    )
    if old is not None:
        payload: FactCommitted | FactSupersededEvent = FactSupersededEvent(
            fact_id=fact_id,
            superseded_id=old.id,
            lineage_id=lineage_id,
            proposition_id=proposition_id,
            authority=w.authority.value,
            supersedes=tuple(c.id for c in closing),
            remainders=tuple(remainder_ids),
        )
        event_id = await _append_event(conn, w, FACT_SUPERSEDED, payload)
    else:
        payload = FactCommitted(
            fact_id=fact_id,
            lineage_id=lineage_id,
            proposition_id=proposition_id,
            supersedes=tuple(c.id for c in closing),
            remainders=tuple(remainder_ids),
        )
        event_id = await _append_event(conn, w, FACT_COMMITTED, payload)
    if not sources:
        # A position-less or event-less write's source is its own derived event.
        for row in rows:
            row["source_event_ids"] = [event_id]

    for current in closing:
        await conn.execute(
            update(facts)
            .where(facts.c.id == current.id)
            .values(
                tx_to=func.coalesce(facts.c.tx_to, at),
                superseded_by=func.coalesce(facts.c.superseded_by, fact_id),
            )
        )
    await insert_derived(conn, facts, rows)
    await index_records(conn, [fact_record(SimpleNamespace(**r)) for r in rows])

    written = await _written(conn, w.story_id, [r["id"] for r in rows])
    fact = written[fact_id].model_copy(
        update={
            "superseded_ids": tuple(c.id for c in closing),
            "remainder_ids": tuple(remainder_ids),
        }
    )
    change = FactChange(
        story_id=w.story_id,
        operation_id=w.operation.operation_id,
        fact=fact,
        superseded_ids=fact.superseded_ids,
        remainders=tuple(written[r] for r in remainder_ids),
        closed=tuple(closing),
    )
    for hook in list(FACT_HOOKS):
        await hook(conn, change)
    return fact


def _row(
    fact_id: str,
    story_id: str,
    lineage_id: str,
    proposition_id: str,
    proposition: Proposition,
    bounds: _Bounds,
    at: datetime,
    provenance: RecordProvenance,
    authority: FactAuthority,
    author_locked: bool,
    visibility: Visibility,
    w: _Write,
    sources: Sequence[str],
    supersedes: Sequence[str],
    anchored_to: str | None,
    commit_state: FactCommitState,
    position: dict[str, Any] | None,
    *,
    thunk_id: str | None = None,
    resolution_level: str | None = None,
) -> dict[str, Any]:
    return {
        "id": fact_id,
        "story_id": story_id,
        "lineage_id": lineage_id,
        "proposition_id": proposition_id,
        "subject_id": proposition.subject_id,
        "predicate_id": proposition.predicate_id,
        "holder": HolderKind.CANON.value,
        **bounds.columns(),
        "tx_from": at,
        "provenance": provenance.value,
        "authority": authority.value,
        "author_locked": author_locked,
        "visibility": visibility.document(),
        "operation_id": w.operation.operation_id,
        "source_event_ids": list(sources),
        "narrative_turn": w.operation.narrative_turn,
        "turn_variant": w.operation.turn_variant,
        "beat": w.beat,
        "slot_k": w.slot_k,
        "supersedes": list(supersedes),
        "anchored_to": anchored_to,
        "commit_state": commit_state.value,
        "committed_at_position": position,
        "thunk_id": thunk_id,
        "resolution_level": resolution_level,
    }


async def _written(conn: AsyncConnection, story_id: str, ids: Iterable[str]) -> dict[str, Fact]:
    f = facts.alias("f")
    found = await conn.execute(_select(f).where(f.c.story_id == story_id, f.c.id.in_(list(ids))))
    return {str(r.id): _fact_of(r) for r in found}


def refuse_holder(holder: str | None, field_name: str = "holder") -> None:
    """FR-EPI-8 (#169): canon has one holder. A write naming any other (a character's id, a
    viewpoint) is 422 ``viewpoint_cannot_assign_truth``: a character's divergent view is an owned
    belief, never a per-character value of the fact."""
    if holder is None or holder == HolderKind.CANON.value:
        return
    raise ViewpointCannotAssignTruth(
        "a viewpoint cannot assign a canon fact's truth: a character's view is a belief (FR-EPI-8)",
        {"field": field_name, "holder": holder},
    )


async def commit_fact(
    conn: AsyncConnection,
    story_id: str,
    proposition: Proposition,
    *,
    provenance: RecordProvenance,
    visibility: Visibility = OBJECTIVE,
    operation: FactOperation,
    anchored_to: str | None = None,
    authority: FactAuthority | None = None,
    valid_time: Interval | Instant | None = None,
    source_event_ids: Sequence[str] = (),
    beat: int | None = None,
    slot_k: int | None = None,
    author_locked: bool = False,
    commit_state: FactCommitState = FactCommitState.COMMITTED,
    registry: Registry | None = None,
    thunk_id: str | None = None,
    resolution_level: str | None = None,
    holder: str = HolderKind.CANON.value,
) -> Fact:
    """Write ``proposition`` as a new fact version in ``operation``'s commit transaction (see
    the module docstring); answers the version, with the versions it closed in
    ``superseded_ids``.

    ``authority`` defaults from ``provenance`` (``default_authority``); ``valid_time``, when
    given, replaces the proposition's declared bounds; ``source_event_ids`` are the events that
    caused the fact (its own ``fact_committed`` event when none); ``beat`` and ``slot_k``
    refine its narrative position, which is the operation's; ``thunk_id`` names the thunk a
    dependence resolved with it (#146's ``core.commit.lifecycle.depend``); ``resolution_level`` is
    FR-COMMIT-9's level (#154; ``None`` reads as ``consequential``). ``holder`` is always
    ``canon``: any other is 422 ``viewpoint_cannot_assign_truth`` (``refuse_holder``, #169).

    An affirmed ``same_as`` between two entities (FR-PROP-7, #171) files an identity proposal
    for the pair in the same transaction (``registry.file_identity_proposal``: the engine never
    merges them itself); one between two of the story's characters is 422
    ``identity_merge_not_yet`` (merging two modeled characters is M7's amendment)."""
    refuse_holder(holder)
    pair = pair_of(proposition)
    if pair is not None:
        await refuse_character_merge(conn, story_id, pair)
    fact = await _write(
        conn,
        _Write(
            story_id=story_id,
            proposition=proposition,
            provenance=RecordProvenance(provenance),
            authority=default_authority(RecordProvenance(provenance))
            if authority is None
            else FactAuthority(authority),
            visibility=visibility,
            operation=operation,
            anchored_to=anchored_to,
            valid_time=valid_time,
            source_event_ids=tuple(source_event_ids),
            beat=beat,
            slot_k=slot_k,
            author_locked=author_locked,
            commit_state=commit_state,
            thunk_id=thunk_id,
            resolution_level=resolution_level,
        ),
        registry,
    )
    if pair is not None:
        await file_identity_proposal(
            conn, story_id, pair, reason="canon_same_as", fact_ids=[fact.id]
        )
    return fact


async def characters_among(
    conn: AsyncConnection, story_id: str, entity_ids: Iterable[str]
) -> frozenset[str]:
    """Which of ``entity_ids`` are the story's characters (#171)."""
    wanted = sorted(set(entity_ids))
    if not wanted:
        return frozenset()
    rows = await conn.scalars(
        select(characters.c.id).where(
            characters.c.story_id == story_id, characters.c.id.in_(wanted)
        )
    )
    return frozenset(str(r) for r in rows)


async def refuse_character_merge(
    conn: AsyncConnection, story_id: str, pair: tuple[str, str]
) -> None:
    """422 ``identity_merge_not_yet`` when both of ``pair`` are the story's characters (#171)."""
    if len(await characters_among(conn, story_id, pair)) == 2:
        raise IdentityMergeNotYet(
            "two characters are one referent only through a merge, which is not served yet",
            {"entities": list(pair)},
        )


async def supersede(
    conn: AsyncConnection,
    story_id: str,
    fact_id: str,
    new_proposition: Proposition,
    *,
    operation: FactOperation,
    authority: FactAuthority | None = None,
    provenance: RecordProvenance | None = None,
    visibility: Visibility | None = None,
    valid_time: Interval | Instant | None = None,
    source_event_ids: Sequence[str] = (),
    beat: int | None = None,
    slot_k: int | None = None,
    registry: Registry | None = None,
    author_locked: bool | None = None,
    holder: str = HolderKind.CANON.value,
) -> Fact:
    """Close version ``fact_id`` in transaction-time and write ``new_proposition`` as its
    successor in the same lineage (never a delete, FR-BITEMP-2). Provenance, visibility,
    ``anchored_to`` and the lock (``author_locked``; #145's lock and unlock flip it) carry over
    unless given; ``authority`` defaults to the given provenance's, else the old version's.
    404 ``fact_not_found``; 409 ``fact_inactive`` for a discarded or dormant version; 409
    ``fact_superseded`` for a closed one (supersede the lineage's current version); 422
    ``key_mismatch`` when the uniqueness key changes; 422 ``viewpoint_cannot_assign_truth`` for
    a ``holder`` other than ``canon`` (#169)."""
    refuse_holder(holder)
    old, current = await _load(conn, story_id, fact_id)
    if old.membership is not Membership.ACTIVE:
        raise FactInactive(
            f"fact {fact_id} is not in the active timeline",
            {"fact_id": fact_id, "membership": old.membership.value},
        )
    if not current:
        raise FactSuperseded(
            f"fact {fact_id} is superseded; supersede its lineage's current version",
            {"fact_id": fact_id, "lineage_id": old.lineage_id},
        )
    given = None if provenance is None else RecordProvenance(provenance)
    if authority is not None:
        chosen = FactAuthority(authority)
    elif given is not None:
        chosen = default_authority(given)
    else:
        chosen = old.authority
    return await _write(
        conn,
        _Write(
            story_id=story_id,
            proposition=new_proposition,
            provenance=given or old.provenance,
            authority=chosen,
            visibility=visibility or old.visibility,
            operation=operation,
            anchored_to=old.anchored_to,
            valid_time=valid_time,
            source_event_ids=tuple(source_event_ids),
            beat=beat,
            slot_k=slot_k,
            author_locked=old.author_locked if author_locked is None else author_locked,
            commit_state=old.commit_state,
            replaces=old,
        ),
        registry,
    )


# -- the two time questions -------------------------------------------------------------------

Reading = Literal["every", "any"]


def _canon_where(
    f: Any,
    story_id: str,
    *,
    story_time: int | None = None,
    record_time: datetime | None = None,
    through_position: Position | None = None,
    viewpoint: str = OMNISCIENT,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    reading: Reading = "every",
    ids: Sequence[str] | None = None,
    written_in: tuple[int, int] | None = None,
    member: str | None = None,
    overlapping: tuple[int | None, int | None] | None = None,
) -> list[ColumnElement[bool]]:
    """``canon``'s selection on ``f`` (a ``facts`` alias joined to ``propositions``), shared by
    every read that must select exactly what ``canon`` does (``canon_links``, #470)."""
    where: list[ColumnElement[bool]] = [
        f.c.story_id == story_id,
        *_open(f, record_time, through_position),
    ]
    if story_time is not None:
        t = literal(story_time, BigInteger)
        permanent = predicates.c.temporal_kind == TemporalKind.PERMANENT.value
        if reading == "every":
            covered = and_(
                f.c.valid_span.op("@>")(t),
                or_(f.c.valid_from_inner.is_(None), f.c.valid_from_inner <= t),
                or_(f.c.valid_to_inner.is_(None), t < f.c.valid_to_inner),
            )
        else:
            covered = f.c.valid_span.op("@>")(t)
        where.append(or_(covered, exists().where(predicates.c.id == f.c.predicate_id, permanent)))
    visible = visible_clause(f, viewpoint)
    if visible is not None:
        where.append(visible)
    if subject is not None:
        where.append(f.c.subject_id == subject)
    if predicate is not None:
        where.append(f.c.predicate_id == predicate)
    if object is not None:
        where.append(propositions.c.object_id == object)
    if member is not None:
        # The object is ``member``, or a set object holds it (#542).
        where.append(
            or_(
                propositions.c.object_id == member,
                propositions.c.object_set.contains(cast([member], JSONB)),
            )
        )
    if ids is not None:
        where.append(f.c.id.in_(list(ids)))
    if written_in is not None:
        where.append(f.c.narrative_turn.between(*written_in))
    if overlapping is not None:
        lo, hi = overlapping
        if hi is not None:
            where.append(or_(f.c.valid_from.is_(None), f.c.valid_from < hi))
        if lo is not None:
            where.append(or_(f.c.valid_to.is_(None), f.c.valid_to > lo))
    return where


async def canon(
    conn: AsyncConnection,
    story_id: str,
    *,
    story_time: int | None = None,
    record_time: datetime | None = None,
    through_position: Position | None = None,
    viewpoint: str = OMNISCIENT,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    reading: Reading = "every",
    limit: int | None = None,
    ids: Sequence[str] | None = None,
    written_in: tuple[int, int] | None = None,
    member: str | None = None,
    viewpoint_checked: bool = False,
    overlapping: tuple[int | None, int | None] | None = None,
) -> list[Fact]:
    """Fact versions of ``story_id`` as ``viewpoint`` may read them; each coordinate filters
    only when given (see the module docstring): ``story_time`` (valid-time covering it),
    ``record_time`` (the record as it stood then; ``None``: the corrected record now) and
    ``through_position`` (``(turn, slot_k)``: written at or before it); ``ids`` keeps only those
    versions (#142's neighborhood seed); ``written_in`` (``(first, last)`` turns, inclusive)
    keeps only versions written at a turn in that window (a consolidation window's facts, #452:
    read in the query, not filtered from the whole canon); ``member`` keeps versions whose object
    is that entity or a set object holding it. Ordered by ``valid_from``, then id.
    ``viewpoint_checked``: the caller has validated ``viewpoint`` (``KeyedLookup``, once per
    viewpoint, #520). ``overlapping`` (``[lo, hi)``, ``None`` open) keeps versions whose
    valid-time may meet it (#520: a keyed lookup's window, read in the query)."""
    if not viewpoint_checked:
        await check_viewpoint(conn, story_id, viewpoint)
    f = _CANON
    where = _canon_where(
        f,
        story_id,
        story_time=story_time,
        record_time=record_time,
        through_position=through_position,
        viewpoint=viewpoint,
        subject=subject,
        predicate=predicate,
        object=object,
        reading=reading,
        ids=ids,
        written_in=written_in,
        member=member,
        overlapping=overlapping,
    )
    query = _select(f).where(*where).order_by(f.c.valid_from.nulls_first(), f.c.id)
    if limit is not None:
        query = query.limit(limit)
    return [_fact_of(r) for r in await conn.execute(query)]


@dataclass(frozen=True, slots=True)
class FactRef:
    """A fact version as the protected-content index needs it (#452): its id, record time,
    visibility and the entities its proposition names (subject, object, object set and the
    string qualifiers), without decoding the rest of the version."""

    id: str
    tx_from: datetime
    visibility: Visibility
    entities: tuple[str, ...]


def _visibility_key(raw: Any) -> Any:
    """A hashable key equal for equal visibility documents: the usual ``{scope}`` /
    ``{scope, characters}`` shapes as a tuple, anything else as its canonical JSON."""
    if isinstance(raw, dict) and set(raw) <= {"scope", "characters"}:
        characters = raw.get("characters", _ABSENT)
        if isinstance(characters, list) and all(isinstance(c, str) for c in characters):
            characters = tuple(characters)
        if isinstance(raw.get("scope"), str) and (
            characters is _ABSENT or characters is None or isinstance(characters, tuple)
        ):
            return (raw.get("scope"), characters)
    return json.dumps(raw, sort_keys=True)


_ABSENT = object()


async def canon_refs(
    conn: AsyncConnection, story_id: str, *, through_position: Position | None = None
) -> list[FactRef]:
    """``canon``'s omniscient read through ``through_position`` (the corrected record now), as
    ``FactRef``s in the same order. The cost per version is a few columns, not a ``Fact``: a
    turn reads the whole canon this way for its protected indexes (#452), and decodes in full
    only the versions a viewpoint cannot read. Each distinct visibility document is validated
    once."""
    f = facts.alias("f")
    p = propositions
    query = (
        select(
            f.c.id,
            f.c.tx_from,
            f.c.visibility,
            p.c.subject_id,
            p.c.object_id,
            p.c.object_set,
            p.c.qualifiers,
        )
        .select_from(f.join(p, p.c.id == f.c.proposition_id))
        .where(f.c.story_id == story_id, *_open(f, None, through_position))
        .order_by(f.c.valid_from.nulls_first(), f.c.id)
    )
    parsed: dict[Any, Visibility] = {}
    found: list[FactRef] = []
    for row in await conn.execute(query):
        raw = row.visibility
        key = _visibility_key(raw)
        visibility = parsed.get(key)
        if visibility is None:
            visibility = parsed[key] = Visibility.model_validate(raw)
        entities = [str(row.subject_id)]
        if row.object_id is not None:
            entities.append(str(row.object_id))
        entities += [str(x) for x in row.object_set or ()]
        entities += [v for v in dict(row.qualifiers).values() if isinstance(v, str)]
        found.append(FactRef(str(row.id), row.tx_from, visibility, tuple(entities)))
    return found


@dataclass(frozen=True, slots=True)
class FactLink:
    """A fact version as a neighborhood ranks it (#470): its id, record time, subject,
    predicate and proposition, the entities its object names and its source events, without
    decoding the rest of the version."""

    id: str
    tx_from: datetime
    subject_id: str
    predicate_id: str
    proposition_id: str
    object_id: str | None
    object_set: tuple[str, ...]
    source_event_ids: tuple[str, ...]


async def canon_links(
    conn: AsyncConnection,
    story_id: str,
    *,
    through_position: Position | None = None,
    viewpoint: str = OMNISCIENT,
    subject: str | None = None,
    object: str | None = None,
) -> list[FactLink]:
    """``canon``'s read with these filters (the same selection, the same order), as
    ``FactLink``s: a neighborhood (#142) ranks every neighbor this way and decodes in full only
    the ones it keeps (#470). The caller has validated ``viewpoint`` (``check_viewpoint``)."""
    f = facts.alias("f")
    p = propositions
    where = _canon_where(
        f,
        story_id,
        through_position=through_position,
        viewpoint=viewpoint,
        subject=subject,
        object=object,
    )
    query = (
        select(
            f.c.id,
            f.c.tx_from,
            f.c.subject_id,
            f.c.predicate_id,
            f.c.proposition_id,
            f.c.source_event_ids,
            p.c.object_id,
            p.c.object_set,
        )
        .select_from(f.join(p, p.c.id == f.c.proposition_id))
        .where(*where)
        .order_by(f.c.valid_from.nulls_first(), f.c.id)
    )
    return [
        FactLink(
            str(row.id),
            row.tx_from,
            str(row.subject_id),
            str(row.predicate_id),
            str(row.proposition_id),
            None if row.object_id is None else str(row.object_id),
            tuple(sorted({str(x) for x in row.object_set or ()})),  # as ``Proposition`` holds it
            tuple(str(e) for e in row.source_event_ids or ()),
        )
        for row in await conn.execute(query)
    ]


async def canon_at(
    conn: AsyncConnection,
    story_id: str,
    *,
    story_time: int,
    through_position: Position | None = None,
    viewpoint: str = OMNISCIENT,
    reading: Reading = "every",
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    limit: int | None = None,
) -> list[Fact]:
    """What was true at story-time ``story_time``, as the corrected record says now
    (FR-BITEMP-3): the current active versions whose valid-time covers it."""
    return await canon(
        conn,
        story_id,
        story_time=story_time,
        through_position=through_position,
        viewpoint=viewpoint,
        reading=reading,
        subject=subject,
        predicate=predicate,
        object=object,
        limit=limit,
    )


async def canon_as_recorded(
    conn: AsyncConnection,
    story_id: str,
    *,
    record_time: datetime,
    story_time: int | None = None,
    viewpoint: str = OMNISCIENT,
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
    limit: int | None = None,
) -> list[Fact]:
    """What the record said at record-time ``record_time`` (FR-BITEMP-3): each version open
    then, regardless of later supersession (optionally only those covering ``story_time``)."""
    return await canon(
        conn,
        story_id,
        story_time=story_time,
        record_time=record_time,
        viewpoint=viewpoint,
        subject=subject,
        predicate=predicate,
        object=object,
        limit=limit,
    )


async def hidden_from(
    conn: AsyncConnection, story_id: str, viewpoint: str, *, story_time: int | None = None
) -> list[Fact]:
    """The current facts ``viewpoint`` may not read (#148's protected-content index): every
    current version the omniscient read returns that the viewpoint's read does not."""
    every = await canon(conn, story_id, story_time=story_time)
    return [fact for fact in every if not visible_to(fact.visibility, viewpoint)]


async def read_version(conn: AsyncConnection, story_id: str, fact_id: str) -> Fact:
    """Version ``fact_id`` of the story (any state); 404 ``fact_not_found`` when there is none."""
    found, _ = await _load(conn, story_id, fact_id)
    return found


async def versions(
    conn: AsyncConnection,
    story_id: str,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    story_time: int | None = None,
) -> list[Fact]:
    """Every version in the story's active timeline, current or closed (the omniscient audit
    of a fact's history, #145's ``include_superseded``), optionally about ``subject``, of
    ``predicate``, or covering ``story_time`` (possibly). Ordered by id."""
    f = facts.alias("f")
    where: list[ColumnElement[bool]] = [f.c.story_id == story_id, active_only(f)]
    if subject is not None:
        where.append(f.c.subject_id == subject)
    if predicate is not None:
        where.append(f.c.predicate_id == predicate)
    if story_time is not None:
        where.append(f.c.valid_span.op("@>")(literal(story_time, BigInteger)))
    return [_fact_of(r) for r in await conn.execute(_select(f).where(*where).order_by(f.c.id))]


async def locked_keys(conn: AsyncConnection, story_id: str) -> list[tuple[str, str, str]]:
    """The current ``author_locked`` versions as ``(fact id, subject, predicate)``, ordered:
    the locks a structured turn's permission checks depend on (FR-VERIFY-11, FR-CONC-16;
    #144's ``author_locks`` configuration member, hashed by ``core.config_dependencies``)."""
    f = facts.alias("f")
    rows = await conn.execute(
        select(f.c.id, f.c.subject_id, f.c.predicate_id)
        .where(f.c.story_id == story_id, f.c.author_locked.is_(True), *_open(f, None))
        .order_by(f.c.id)
    )
    return [(str(r.id), str(r.subject_id), str(r.predicate_id)) for r in rows]


__all__ = [
    "FACT_COMMITTED",
    "FACT_HOOKS",
    "FACT_SUPERSEDED",
    "OBJECTIVE",
    "READER_VIEWPOINTS",
    "SHARED_SCOPES",
    "Fact",
    "FactChange",
    "FactHook",
    "FactLink",
    "FactOperation",
    "FactRef",
    "Reading",
    "Visibility",
    "VisibilityScope",
    "canon",
    "canon_as_recorded",
    "canon_at",
    "canon_links",
    "canon_refs",
    "check_viewpoint",
    "commit_fact",
    "default_authority",
    "hidden_from",
    "locked_keys",
    "read_version",
    "refuse_holder",
    "register_fact_hook",
    "remove_fact_hook",
    "supersede",
    "versions",
    "visible_clause",
    "visible_to",
]
