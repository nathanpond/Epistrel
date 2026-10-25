"""Per-character belief partitions and the ``belief_state`` projection (FR-EPI-1,3,5,6,12,
FR-STORE-3; docs/02 §4.4; #159).

**What a partition is.** Each modeled character owns the ``beliefs`` rows whose
``holder_character_id`` is its id: what it holds true about the world (self-world) and about
other people (social-world), each wrapping one viewpoint-free
proposition (FR-PROP-2). A false belief is stored exactly like a true one: it is the holder's
truth (FR-EPI-3), and its provenance says how it was acquired (#160). Objective canon is another
table (``facts``), another holder, another writer: canon never reads a belief and a partition
read never reads a fact (FR-EPI-1, enforced by ``tools.gates check-epistemic-separation``). A
belief's visibility is always its holder alone (``core.visibility.private_to``, and the table's
CHECK): no other character ever reads it.

**The player has no partition** (FR-EPI-12). A belief whose holder is the player's character
(``characters.is_player``), or no character of the story, is refused with 422
``holder_has_no_partition``; other characters hold beliefs *about* the player as usual.

**Writes.** ``_insert_belief`` is the only writer, module-private: #160's ``transition()`` is its
only caller (``tools.gates check-epistemic-separation``), and passes the producing transition as
``provenance`` (NOT NULL; ADR-168). It runs inside an ``Operation``'s commit transaction (a commit
step): the erasure guard, the proposition, then the row (``tx_from`` the transaction's time,
``held_from`` the writing position unless given, valid-time from the proposition's declared bounds
unless given). The holder's open belief in the same proposition (subject, predicate, object,
qualifiers and polarity; the time is the belief's own) lends the new row its proposition and is
closed at the new row's ``held_from`` (``held_to``, written once): the holder's belief changed
there. Otherwise the proposition is stored through #131's store, as canon's are (one row per write:
the store has no shape upsert). It appends a ``derived`` ``belief_committed`` event, indexes the
row's subjects for erasure, and runs ``BELIEF_HOOKS`` in the same transaction (the ``belief_state``
projection, then #136's change index with ``holder = 'belief'``); a hook's exception aborts the
commit.

**Reads.**

- ``partition(story_id, holder, through=…)``: the holder's first-order beliefs current at a
  narrative position (``core.membership.Position``): active, not closed in transaction-time,
  held from at or before it and not closed by then (``held_to``). ``through=None`` reads the
  beliefs held now. Optionally only those about some subjects, of a predicate, of some
  propositions, or held at least at a level.
- ``believers_of(story_id, proposition_id)``: who holds it now, from ``belief_state``.
- ``belief_states``: a page of the projection, with each row's current belief, for
  ``GET /stories/{id}/inspect/beliefs``.

**Nested beliefs and speech acts** (#167; ``core.epistemic.nested`` writes them through #160's
``transition()``): ``nested_of`` reads a holder's open ``nested`` rows (what it thinks others
believe), each with its inner chain; ``speech_acts`` its own ``said`` rows (the speaker from the
row's ``speaker_character_id``). Neither is ever a first-order read: ``partition`` and every
``check(holder = belief(H))`` record set see ``first_order`` rows only. ``nested_belief_state``
projects the open nested rows as ``belief_state`` projects the first-order ones (``derive_nested``,
the same hook, follower and fold).

**The projection** (``belief_state``, FR-EPI-6, FR-STORE-3). ``derive`` is a pure function of a
set of belief rows: one state row per active, open, first-order belief held at ``suspected`` or
above (none for ``unknown``). The incremental path re-derives the touched ``(holder,
proposition)`` keys in the writing transaction (``BELIEF_HOOKS``) and after every membership flip
of derived rows (``core.membership.register_follower``: a truncate, an untruncate, a swap, a
regenerate and a turn over a rewound tail), and writes the difference; the fold
(``core.projections.beliefs``: ``rebuild``, erasure's ``rebuilt`` stage, a fork) derives every
row from the same function, so the two paths agree.

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    ColumnElement,
    delete,
    func,
    insert,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import readset
from epistrel.core.canon.errors import UnknownPredicate
from epistrel.core.enums import (
    Authority,
    BeliefConfidenceLevel,
    BeliefHoldState,
    BeliefKind,
    EventCategory,
    Membership,
    Provenance,
)
from epistrel.core.epistemic.errors import HolderHasNoPartition
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import belief_record, event_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.events.registry import EventPayload, validate
from epistrel.core.events.types import BeliefCommitted
from epistrel.core.ids import new_ulid
from epistrel.core.membership import (
    Position,
    active_only,
    included_through,
    insert_derived,
    position_key,
    position_literal,
    register_follower,
)
from epistrel.core.propositions import store
from epistrel.core.propositions.codec import proposition_from_row
from epistrel.core.propositions.model import Instant, Interval, Proposition
from epistrel.core.propositions.registry import Registry, canonical, load
from epistrel.core.tables import characters, entities, events, propositions
from epistrel.core.tables.beliefs import (
    belief_state,
    belief_transitions,
    beliefs,
    nested_belief_state,
)
from epistrel.core.visibility import Visibility, private_to

BELIEF_COMMITTED = "belief_committed"

LEVELS: tuple[BeliefConfidenceLevel, ...] = tuple(BeliefConfidenceLevel)
"""The confidence levels, weakest first."""

HELD_LEVELS: frozenset[BeliefConfidenceLevel] = frozenset(LEVELS) - {BeliefConfidenceLevel.UNKNOWN}
"""The levels ``belief_state`` holds a row for (``unknown`` is a suspended belief)."""


def at_least(level: BeliefConfidenceLevel) -> tuple[BeliefConfidenceLevel, ...]:
    """``level`` and every stronger one."""
    return LEVELS[LEVELS.index(BeliefConfidenceLevel(level)) :]


# -- models -----------------------------------------------------------------------------------


class BeliefOperation(Protocol):
    """What a belief needs of the operation writing it (an ``Operation`` inside its commit): its
    id, story, principal and narrative position."""

    operation_id: str
    story_id: str | None
    principal: str
    narrative_turn: int | None
    turn_variant: int | None


class Belief(BaseModel):
    """One belief record with its proposition (see ``core.tables.beliefs`` for the columns).
    ``closed_id`` is set on the answer of a write: the open belief it closed."""

    model_config = ConfigDict(frozen=True)

    id: str
    story_id: str
    holder_character_id: str
    kind: BeliefKind
    proposition_id: str
    proposition: Proposition
    polarity: str
    confidence_level: BeliefConfidenceLevel
    confidence_scalar: float | None
    valid_from: int | None
    valid_to: int | None
    held_from: Position
    held_to: Position | None
    tx_from: datetime
    tx_to: datetime | None
    provenance: str
    visibility: Visibility
    operation_id: str
    source_event_ids: tuple[str, ...] = ()
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None
    step: int
    membership: Membership
    membership_tx: datetime
    accessibility: float = 1.0
    state: BeliefHoldState = BeliefHoldState.HELD
    corrected_by: str | None = None
    lineage_id: str = ""
    resolution_operation_id: str | None = None
    attributed_holder_id: str | None = None
    inner_belief_id: str | None = None
    depth: int = 1
    attributed_confidence_level: BeliefConfidenceLevel | None = None
    attributed_confidence_scalar: float | None = None
    attributed_held_from: int | None = None
    attributed_held_to: int | None = None
    speaker_character_id: str | None = None
    utterance_id: str | None = None
    staged_ref: dict[str, Any] | None = None
    perceived_intent: str | None = None
    disclosure: str | None = None
    closed_id: str | None = None

    def held_at(self, position: Position) -> bool:
        """Whether the holder holds it at ``position`` (its ``held_from``/``held_to``)."""
        return self.held_from.at_or_before(position) and (
            self.held_to is None or position.before(self.held_to)
        )


@dataclass(frozen=True, slots=True)
class BeliefState:
    """One ``belief_state`` row: a belief a holder holds now, its level, scalar and polarity,
    the belief row it rests on and the position since which it holds."""

    story_id: str
    holder_character_id: str
    proposition_id: str
    belief_id: str
    polarity: str
    current_level: BeliefConfidenceLevel
    current_scalar: float | None
    since: Position


@dataclass(frozen=True, slots=True)
class BeliefChange:
    """What a write did, as ``BELIEF_HOOKS`` see it: the new belief, and the open belief of the
    same holder and proposition it closed."""

    story_id: str
    operation_id: str
    belief: Belief
    closed: Belief | None = None


BeliefHook = Callable[[AsyncConnection, BeliefChange], Awaitable[None]]

BELIEF_HOOKS: list[BeliefHook] = []
"""Run in registration order after every belief write, on the write's connection (its
transaction): the ``belief_state`` projection and #136's change index (registered below), as
``core.canon.facts.FACT_HOOKS`` for canon."""


def register_belief_hook(hook: BeliefHook) -> BeliefHook:
    """Add ``hook`` to ``BELIEF_HOOKS`` (once)."""
    if hook not in BELIEF_HOOKS:
        BELIEF_HOOKS.append(hook)
    return hook


def remove_belief_hook(hook: BeliefHook) -> None:
    if hook in BELIEF_HOOKS:
        BELIEF_HOOKS.remove(hook)


# -- reading rows -----------------------------------------------------------------------------

_P = "p_"


def _select() -> Any:
    return select(beliefs, *(c.label(f"{_P}{c.name}") for c in propositions.c)).select_from(
        beliefs.join(propositions, propositions.c.id == beliefs.c.proposition_id)
    )


def _scalar(value: Any) -> float | None:
    return None if value is None else float(value)


def _belief_of(row: Any) -> Belief:
    m: Mapping[str, Any] = row._mapping
    proposition = proposition_from_row(
        {c.name: m[f"{_P}{c.name}"] for c in propositions.c if c.name not in ("id", "story_id")}
    )
    held_from = Position.of_columns(m, "held_from")
    if held_from is None:  # pragma: no cover - NOT NULL
        raise ValueError("a belief has a held_from position")
    return Belief(
        id=str(m["id"]),
        story_id=str(m["story_id"]),
        holder_character_id=str(m["holder_character_id"]),
        kind=BeliefKind(m["kind"]),
        proposition_id=str(m["proposition_id"]),
        proposition=proposition,
        polarity=str(m["polarity"]),
        confidence_level=BeliefConfidenceLevel(m["confidence_level"]),
        confidence_scalar=_scalar(m["confidence_scalar"]),
        valid_from=m["valid_from"],
        valid_to=m["valid_to"],
        held_from=held_from,
        held_to=Position.of_columns(m, "held_to"),
        tx_from=m["tx_from"],
        tx_to=m["tx_to"],
        provenance=str(m["provenance"]),
        visibility=Visibility.model_validate(m["visibility"]),
        operation_id=str(m["operation_id"]),
        source_event_ids=tuple(str(e) for e in m["source_event_ids"] or ()),
        narrative_turn=m["narrative_turn"],
        turn_variant=m["turn_variant"],
        beat=m["beat"],
        step=int(m["step"]),
        membership=Membership(m["membership"]),
        membership_tx=m["membership_tx"],
        accessibility=float(m["accessibility"]),
        state=BeliefHoldState(m["state"]),
        corrected_by=None if m["corrected_by"] is None else str(m["corrected_by"]),
        lineage_id=str(m["lineage_id"]),
        resolution_operation_id=(
            None if m.get("resolution_operation_id") is None else str(m["resolution_operation_id"])
        ),
        attributed_holder_id=_id(m.get("attributed_holder_id")),
        inner_belief_id=_id(m.get("inner_belief_id")),
        depth=int(m.get("depth") or 1),
        attributed_confidence_level=(
            None
            if m.get("attributed_confidence_level") is None
            else BeliefConfidenceLevel(m["attributed_confidence_level"])
        ),
        attributed_confidence_scalar=_scalar(m.get("attributed_confidence_scalar")),
        attributed_held_from=m.get("attributed_held_from"),
        attributed_held_to=m.get("attributed_held_to"),
        speaker_character_id=_id(m.get("speaker_character_id")),
        utterance_id=_id(m.get("utterance_id")),
        staged_ref=m.get("staged_ref"),
        perceived_intent=m.get("perceived_intent"),
        disclosure=m.get("disclosure"),
    )


def _id(value: Any) -> str | None:
    return None if value is None else str(value)


def _open() -> list[ColumnElement[bool]]:
    """The active first-order beliefs not closed in transaction-time."""
    b = beliefs.c
    return [active_only(beliefs), b.tx_to.is_(None), b.kind == BeliefKind.FIRST_ORDER.value]


def _held_then() -> list[ColumnElement[bool]]:
    """The active first-order beliefs a past read may see: open in transaction-time, or closed
    by a correction (#160: the prior keeps its ``held_from``/``held_to`` span, so the holder can
    be shown to have held it until the correction)."""
    b = beliefs.c
    return [
        active_only(beliefs),
        or_(b.tx_to.is_(None), b.state == BeliefHoldState.CORRECTED.value),
        b.kind == BeliefKind.FIRST_ORDER.value,
    ]


def held_at_clause(position: Position | None) -> list[ColumnElement[bool]]:
    """The beliefs held at ``position`` (``None``: held now, ``held_to`` unset)."""
    b = beliefs.c
    if position is None:
        return [b.held_to_turn.is_(None)]
    at = position_literal(position)
    return [
        position_key(beliefs, "held_from") <= at,
        or_(b.held_to_turn.is_(None), position_key(beliefs, "held_to") > at),
    ]


async def partition(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    *,
    through: Position | None = None,
    subjects: Sequence[str] | None = None,
    entities_: Sequence[str] | None = None,
    predicate: str | None = None,
    proposition_ids: Sequence[str] | None = None,
    levels: Iterable[BeliefConfidenceLevel] | None = None,
    limit: int | None = None,
    order: str = "id",
) -> list[Belief]:
    """``holder``'s first-order beliefs current at ``through`` (see the module docstring),
    optionally only those whose proposition's subject is one of ``subjects``, whose subject or
    object is one of ``entities_``, of ``predicate``, among ``proposition_ids``, at one of
    ``levels``; ordered by id (``order = "confidence"``: strongest first, then the latest)."""
    b = beliefs.c
    p = propositions.c
    where: list[ColumnElement[bool]] = [
        b.story_id == story_id,
        b.holder_character_id == holder,
        *(_open() if through is None else _held_then()),
        *held_at_clause(through),
    ]
    if through is not None:
        # #164: a lazily resolved belief holds from its exposure, but is in the record only from
        # the turn that resolved it (FR-BELIEF-13,14).
        where.append(included_through(beliefs, through.turn))
    if subjects is not None:
        where.append(p.subject_id.in_(list(subjects)))
    if entities_ is not None:
        found = list(entities_)
        where.append(or_(p.subject_id.in_(found), p.object_id.in_(found)))
    if predicate is not None:
        where.append(p.predicate_id == predicate)
    if proposition_ids is not None:
        where.append(b.proposition_id.in_(list(proposition_ids)))
    if levels is not None:
        where.append(b.confidence_level.in_([BeliefConfidenceLevel(x).value for x in levels]))
    query = _select().where(*where)
    if order == "confidence":
        query = query.order_by(
            b.confidence_level.desc(),
            func.coalesce(b.confidence_scalar, -1).desc(),
            *(c.desc() for c in position_key(beliefs, "held_from").clauses),
            b.id,
        )
    else:
        query = query.order_by(b.id)
    if limit is not None:
        query = query.limit(limit)
    return [_belief_of(r) for r in await conn.execute(query)]


async def read_belief(conn: AsyncConnection, story_id: str, belief_id: str) -> Belief | None:
    found = (
        await conn.execute(
            _select().where(beliefs.c.story_id == story_id, beliefs.c.id == belief_id)
        )
    ).one_or_none()
    return None if found is None else _belief_of(found)


@dataclass(frozen=True, slots=True)
class SpeechActBelief:
    """A holder's belief about a speech act (``kind = speech_act``; #162, #167's full shape):
    that ``speaker`` said the proposition (``believes(H, said(S, X))``), with the testimony
    outcome its producing transition recorded; the utterance heard, the staged beat and the
    perceived intent are on the row (``belief``)."""

    belief: Belief
    speaker_id: str | None
    outcome: str | None


async def speech_acts(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    *,
    speaker: str | None = None,
    held: bool = True,
) -> list[SpeechActBelief]:
    """``holder``'s own speech-act beliefs (``said`` rows: not the inner content of a nested
    belief), each with its speaker (the row's ``speaker_character_id``) and its producing
    transition's outcome; only ``speaker``'s when given; ``held``: the active, open rows only. By
    id."""
    b = beliefs.c
    t = belief_transitions.c
    query = (
        _select()
        .add_columns(t.source_character_id.label("said_by"), t.outcome.label("said_outcome"))
        .join(belief_transitions, t.id == b.provenance)
        .where(
            b.story_id == story_id,
            b.holder_character_id == holder,
            b.kind == BeliefKind.SPEECH_ACT.value,
            b.attributed_holder_id.is_(None),
        )
        .order_by(b.id)
    )
    if held:
        query = query.where(active_only(beliefs), b.tx_to.is_(None), b.held_to_turn.is_(None))
    if speaker is not None:
        query = query.where(b.speaker_character_id == speaker)
    found = []
    for row in await conn.execute(query):
        m = row._mapping
        found.append(
            SpeechActBelief(
                _belief_of(row),
                _id(m["speaker_character_id"]) or _id(m["said_by"]),
                m["said_outcome"],
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class NestedBelief:
    """One open nested belief of a holder (#167): the outer ``nested`` row and its inner chain,
    outermost first (``attributed`` rows, ending in a ``speech_act`` row when the content is a
    said). ``holders`` are the attributed holders, outermost first: ``(B,)`` for
    ``believes(H, believes(B, P))``."""

    belief: Belief
    chain: tuple[Belief, ...]

    @property
    def attributed_holder_id(self) -> str:
        return str(self.belief.attributed_holder_id)

    @property
    def holders(self) -> tuple[str, ...]:
        return chain_signature(self.attributed_holder_id, self.chain)[0]

    @property
    def said_by(self) -> str | None:
        """The speaker when the innermost content is a speech act (``believes(B, said(S, P))``)."""
        return chain_signature(self.attributed_holder_id, self.chain)[1]


def chain_signature(
    attributed_holder: str, chain: Sequence[Belief]
) -> tuple[tuple[str, ...], str | None]:
    """A nested belief's attribution: its attributed holders, outermost first, and the speaker
    of an innermost speech act (``None``: the content is a belief)."""
    holders = [attributed_holder]
    for inner in chain[1:]:
        if inner.kind is BeliefKind.ATTRIBUTED and inner.attributed_holder_id is not None:
            holders.append(inner.attributed_holder_id)
    last = chain[-1] if chain else None
    said = None
    if last is not None and last.kind is BeliefKind.SPEECH_ACT:
        said = last.speaker_character_id
    return tuple(holders), said


async def _chain(conn: AsyncConnection, story_id: str, outer: Belief) -> tuple[Belief, ...]:
    found: list[Belief] = []
    current = outer.inner_belief_id
    seen: set[str] = set()
    while current is not None and current not in seen and len(found) < 8:
        seen.add(current)
        inner = await read_belief(conn, story_id, current)
        if inner is None:
            break
        found.append(inner)
        current = inner.inner_belief_id
    return tuple(found)


async def nested_of(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    *,
    attributed_holder: str | None = None,
    through: Position | None = None,
    subjects: Sequence[str] | None = None,
    predicate: str | None = None,
    held: bool = True,
) -> list[NestedBelief]:
    """``holder``'s nested beliefs (the outer ``nested`` rows, each with its inner chain): held at
    ``through`` (``None``: now; a past read sees a corrected row over its span, as
    ``partition``'s does), only those attributed to ``attributed_holder``, about ``subjects`` or
    of ``predicate`` when given; ``held`` false: every active row, closed ones included. By
    id."""
    b = beliefs.c
    p = propositions.c
    where: list[ColumnElement[bool]] = [
        b.story_id == story_id,
        b.holder_character_id == holder,
        b.kind == BeliefKind.NESTED.value,
        active_only(beliefs),
    ]
    if held:
        if through is None:
            where += [b.tx_to.is_(None), *held_at_clause(None)]
        else:
            where += [
                or_(b.tx_to.is_(None), b.state == BeliefHoldState.CORRECTED.value),
                *held_at_clause(through),
                included_through(beliefs, through.turn),
            ]
    if attributed_holder is not None:
        where.append(b.attributed_holder_id == attributed_holder)
    if subjects is not None:
        where.append(p.subject_id.in_(list(subjects)))
    if predicate is not None:
        where.append(p.predicate_id == predicate)
    rows = [_belief_of(r) for r in await conn.execute(_select().where(*where).order_by(b.id))]
    return [NestedBelief(r, await _chain(conn, story_id, r)) for r in rows]


def _state_of(row: Any) -> BeliefState:
    m: Mapping[str, Any] = row._mapping
    since = Position.of_columns(m, "since")
    assert since is not None  # noqa: S101 - NOT NULL
    return BeliefState(
        story_id=str(m["story_id"]),
        holder_character_id=str(m["holder_character_id"]),
        proposition_id=str(m["proposition_id"]),
        belief_id=str(m["belief_id"]),
        polarity=str(m["polarity"]),
        current_level=BeliefConfidenceLevel(m["current_level"]),
        current_scalar=_scalar(m["current_scalar"]),
        since=since,
    )


async def believers_of(
    conn: AsyncConnection, story_id: str, proposition_id: str
) -> list[BeliefState]:
    """Who holds ``proposition_id`` now (``belief_state``), by holder."""
    s = belief_state.c
    found = await conn.execute(
        select(belief_state)
        .where(s.story_id == story_id, s.proposition_id == proposition_id)
        .order_by(s.holder_character_id)
    )
    return [_state_of(r) for r in found]


@dataclass(frozen=True, slots=True)
class StatePage:
    """A page of ``belief_state`` rows with the belief each rests on, and ``next_after`` (the
    proposition id to continue after; ``None`` on the last page)."""

    items: tuple[tuple[BeliefState, Belief], ...]
    next_after: str | None


async def belief_states(
    conn: AsyncConnection,
    story_id: str,
    *,
    holder: str,
    proposition_id: str | None = None,
    limit: int = 100,
    after: str | None = None,
) -> StatePage:
    """``holder``'s ``belief_state`` rows by proposition id after ``after``, ``limit`` of them,
    each with its current belief (``GET /stories/{id}/inspect/beliefs``)."""
    s = belief_state.c
    where = [s.story_id == story_id, s.holder_character_id == holder]
    if proposition_id is not None:
        where.append(s.proposition_id == proposition_id)
    if after is not None:
        where.append(s.proposition_id > after)
    rows = list(
        await conn.execute(
            select(belief_state).where(*where).order_by(s.proposition_id).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    states = [_state_of(r) for r in rows[:limit]]
    found = {
        str(r.id): _belief_of(r)
        for r in await conn.execute(
            _select().where(
                beliefs.c.story_id == story_id,
                beliefs.c.id.in_([x.belief_id for x in states]),
            )
        )
    }
    items = tuple((x, found[x.belief_id]) for x in states if x.belief_id in found)
    return StatePage(items, states[-1].proposition_id if more and states else None)


INSPECT_LIMIT_DEFAULT = 100
INSPECT_LIMIT_MAX = 500


@dataclass(frozen=True, slots=True)
class BeliefInspection:
    """``GET /stories/{id}/inspect/beliefs``' answer: a page of one holder's ``belief_state``
    rows with their beliefs, the story's revision, and ``next_after``."""

    story_id: str
    holder_character_id: str
    revision: int
    items: tuple[tuple[BeliefState, Belief], ...]
    next_after: str | None
    identities: tuple[Any, ...] = ()
    """#171: the ``same_as`` the holder holds (``core.resolution.identity.Identity``): the
    canon facts its viewpoint reads and its own ``same_as`` beliefs (also among ``items``)."""


async def _states_as_of(
    conn: AsyncConnection,
    story_id: str,
    *,
    holder: str,
    as_of: int,
    proposition_id: str | None,
    limit: int,
    after: str | None,
) -> StatePage:
    """``holder``'s projection as the corrected record stood through turn ``as_of``
    (FR-BELIEF-14; #178): the beliefs held then (``partition(through=…)``, discarded rows
    excluded), one per proposition (the latest), at ``suspected`` or above, by proposition id."""
    from epistrel.core.membership import POSITION_MAX

    through = Position(as_of, 0, POSITION_MAX, POSITION_MAX, POSITION_MAX)
    held = await partition(
        conn,
        story_id,
        holder,
        through=through,
        proposition_ids=None if proposition_id is None else [proposition_id],
        levels=HELD_LEVELS,
    )
    latest: dict[str, Belief] = {}
    for b in sorted(held, key=lambda r: (r.held_from.key(), r.id)):
        latest[b.proposition_id] = b
    keys = sorted(k for k in latest if after is None or k > after)
    page = keys[:limit]
    items = tuple(
        (
            BeliefState(
                story_id=story_id,
                holder_character_id=holder,
                proposition_id=k,
                belief_id=latest[k].id,
                polarity=latest[k].polarity,
                current_level=latest[k].confidence_level,
                current_scalar=latest[k].confidence_scalar,
                since=latest[k].held_from,
            ),
            latest[k],
        )
        for k in page
    )
    return StatePage(items, page[-1] if len(keys) > limit and page else None)


async def inspect(
    db: AsyncEngine,
    story_id: str,
    *,
    character: str,
    proposition_id: str | None = None,
    limit: int = INSPECT_LIMIT_DEFAULT,
    after: str | None = None,
    as_of: int | None = None,
) -> BeliefInspection:
    """One holder's belief projection (FR-EPI-6; docs/02 §9's ``inspect/beliefs``) in one
    consistent read: 404 for an unknown story, 409 ``module_off`` when the story keeps no
    partitions (#178), 422 ``validation_error`` (``query.character``) for a character outside
    the cast. The player's character has no partition (FR-EPI-12): an empty page. ``as_of``
    reads the projection as the corrected record stood through that turn (#178)."""
    from epistrel.core.concurrency import check_expected_revision
    from epistrel.core.epistemic.inspection import check_character, require_partitions

    limit = max(1, min(INSPECT_LIMIT_MAX, limit))
    async with db.connect() as conn:
        revision = await check_expected_revision(conn, story_id, None)
        await require_partitions(conn, story_id)
        await check_character(conn, story_id, character, field="query.character")
        if await is_player(conn, story_id, character):
            return BeliefInspection(story_id, character, revision, (), None, ())
        if as_of is None:
            page = await belief_states(
                conn,
                story_id,
                holder=character,
                proposition_id=proposition_id,
                limit=limit,
                after=after,
            )
        else:
            page = await _states_as_of(
                conn,
                story_id,
                holder=character,
                as_of=as_of,
                proposition_id=proposition_id,
                limit=limit,
                after=after,
            )
        # #171: who the holder holds to be one (canon it reads, and its own beliefs).
        from epistrel.core.readset import KeyedLookup, StructuredReadSet
        from epistrel.core.resolution.identity import identities_for

        held = await identities_for(
            KeyedLookup(conn, StructuredReadSet()),
            story_id,
            character,
            beliefs=True,
            characters=(),
        )
    return BeliefInspection(story_id, character, revision, page.items, page.next_after, held.held)


# -- the projection ---------------------------------------------------------------------------

Key = tuple[str, str]
"""A ``belief_state`` key within a story: ``(holder, proposition)``."""


def derive(rows: Iterable[Belief]) -> dict[Key, dict[str, Any]]:
    """The ``belief_state`` rows of ``rows`` (pure, order-free): one per active, open,
    first-order belief held at ``suspected`` or above, keyed by ``(holder, proposition)``."""
    found: dict[Key, dict[str, Any]] = {}
    for b in sorted(rows, key=lambda r: r.id):
        if (
            b.membership is not Membership.ACTIVE
            or b.tx_to is not None
            or b.held_to is not None
            or b.kind is not BeliefKind.FIRST_ORDER
            or b.confidence_level not in HELD_LEVELS
        ):
            continue
        found[(b.holder_character_id, b.proposition_id)] = {
            "story_id": b.story_id,
            "holder_character_id": b.holder_character_id,
            "proposition_id": b.proposition_id,
            "belief_id": b.id,
            "polarity": b.polarity,
            "current_level": b.confidence_level.value,
            "current_scalar": b.confidence_scalar,
            **b.held_from.columns("since"),
        }
    return found


async def _open_rows(
    conn: AsyncConnection, story_id: str, keys: Sequence[Key] | None
) -> list[Belief]:
    b = beliefs.c
    where: list[ColumnElement[bool]] = [b.story_id == story_id, *_open(), b.held_to_turn.is_(None)]
    if keys is not None:
        if not keys:
            return []
        where.append(tuple_(b.holder_character_id, b.proposition_id).in_(list(keys)))
    return [_belief_of(r) for r in await conn.execute(_select().where(*where))]


async def fold_state(conn: AsyncConnection, story_id: str) -> list[dict[str, Any]]:
    """Every ``belief_state`` row of the story, derived from its beliefs (the rebuild)."""
    return list(derive(await _open_rows(conn, story_id, None)).values())


def _comparable(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: (None if v is None else float(v)) if k == "current_scalar" else v
        for k, v in row.items()
        if k in belief_state.c
    }


async def refresh(conn: AsyncConnection, story_id: str, keys: Iterable[Key]) -> int:
    """Re-derive the ``belief_state`` rows of ``keys`` and write the difference; answers the
    rows changed."""
    chosen = sorted(set(keys))
    if not chosen:
        return 0
    wanted = derive(await _open_rows(conn, story_id, chosen))
    s = belief_state.c
    stored = {
        (str(r.holder_character_id), str(r.proposition_id)): _comparable(dict(r._mapping))
        for r in await conn.execute(
            select(belief_state).where(
                s.story_id == story_id,
                tuple_(s.holder_character_id, s.proposition_id).in_(chosen),
            )
        )
    }
    changed = 0
    for key in chosen:
        old, new = stored.get(key), wanted.get(key)
        if old is not None and (new is None or _comparable(new) != old):
            await conn.execute(
                delete(belief_state).where(
                    s.story_id == story_id,
                    s.holder_character_id == key[0],
                    s.proposition_id == key[1],
                )
            )
            changed += 1
            old = None
        if new is not None and old is None:
            await conn.execute(insert(belief_state).values(**new))
            changed += 1
    # #167: the same keys' nested beliefs (their innermost proposition), kept alongside.
    return changed + await refresh_nested(conn, story_id, chosen)


def derive_nested(rows: Iterable[Belief]) -> dict[str, dict[str, Any]]:
    """The ``nested_belief_state`` rows of ``rows`` (pure, order-free): one per active, open
    ``nested`` belief held at ``suspected`` or above, keyed by its belief id (#167)."""
    found: dict[str, dict[str, Any]] = {}
    for b in sorted(rows, key=lambda r: r.id):
        if (
            b.membership is not Membership.ACTIVE
            or b.tx_to is not None
            or b.held_to is not None
            or b.kind is not BeliefKind.NESTED
            or b.attributed_holder_id is None
            or b.confidence_level not in HELD_LEVELS
        ):
            continue
        found[b.id] = {
            "story_id": b.story_id,
            "belief_id": b.id,
            "holder_character_id": b.holder_character_id,
            "attributed_holder_id": b.attributed_holder_id,
            "proposition_id": b.proposition_id,
            "polarity": b.polarity,
            "depth": b.depth,
            "current_level": b.confidence_level.value,
            "current_scalar": b.confidence_scalar,
            "attributed_level": (
                None
                if b.attributed_confidence_level is None
                else b.attributed_confidence_level.value
            ),
            "attributed_scalar": b.attributed_confidence_scalar,
            **b.held_from.columns("since"),
        }
    return found


async def _open_nested(
    conn: AsyncConnection, story_id: str, keys: Sequence[Key] | None
) -> list[Belief]:
    b = beliefs.c
    where: list[ColumnElement[bool]] = [
        b.story_id == story_id,
        active_only(beliefs),
        b.tx_to.is_(None),
        b.held_to_turn.is_(None),
        b.kind == BeliefKind.NESTED.value,
    ]
    if keys is not None:
        if not keys:
            return []
        where.append(tuple_(b.holder_character_id, b.proposition_id).in_(list(keys)))
    return [_belief_of(r) for r in await conn.execute(_select().where(*where))]


async def fold_nested_state(conn: AsyncConnection, story_id: str) -> list[dict[str, Any]]:
    """Every ``nested_belief_state`` row of the story, derived from its beliefs (the rebuild)."""
    return list(derive_nested(await _open_nested(conn, story_id, None)).values())


def _nested_comparable(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: (None if v is None else float(v)) if k in ("current_scalar", "attributed_scalar") else v
        for k, v in row.items()
        if k in nested_belief_state.c
    }


async def refresh_nested(conn: AsyncConnection, story_id: str, keys: Iterable[Key]) -> int:
    """Re-derive the ``nested_belief_state`` rows of the ``(holder, proposition)`` keys and write
    the difference; answers the rows changed."""
    chosen = sorted(set(keys))
    if not chosen:
        return 0
    wanted = derive_nested(await _open_nested(conn, story_id, chosen))
    n = nested_belief_state.c
    stored = {
        str(r.belief_id): _nested_comparable(dict(r._mapping))
        for r in await conn.execute(
            select(nested_belief_state).where(
                n.story_id == story_id,
                tuple_(n.holder_character_id, n.proposition_id).in_(chosen),
            )
        )
    }
    changed = 0
    for belief_id in sorted(set(stored) | set(wanted)):
        old, new = stored.get(belief_id), wanted.get(belief_id)
        if old is not None and (new is None or _nested_comparable(new) != old):
            await conn.execute(
                delete(nested_belief_state).where(n.story_id == story_id, n.belief_id == belief_id)
            )
            changed += 1
            old = None
        if new is not None and old is None:
            await conn.execute(insert(nested_belief_state).values(**new))
            changed += 1
    return changed


async def _project(conn: AsyncConnection, change: BeliefChange) -> None:
    """``BELIEF_HOOKS``: the write's key re-derived in its transaction."""
    keys = {(change.belief.holder_character_id, change.belief.proposition_id)}
    if change.closed is not None:
        keys.add((change.closed.holder_character_id, change.closed.proposition_id))
    await refresh(conn, change.story_id, keys)


async def _record_change(conn: AsyncConnection, change: BeliefChange) -> None:
    """``BELIEF_HOOKS``: #136's change index, ``holder = 'belief'`` with the holder as
    ``speaker_id``: a new belief is ``add`` (``supersede`` when it closed one), the closed one a
    ``close``."""

    def row(belief: Belief, kind: readset.ChangeKind) -> readset.Change:
        return readset.Change(
            proposition=belief.proposition,
            proposition_id=belief.proposition_id,
            change=kind,
            valid_window=(belief.valid_from, belief.valid_to),
            visibility=belief.visibility.document(),
            speaker_id=belief.holder_character_id,
            holder=readset.BELIEF,
        )

    rows = [row(change.belief, "supersede" if change.closed is not None else "add")]
    if change.closed is not None:
        rows.append(row(change.closed, "close"))
    await readset.record_change(conn, change.story_id, change.operation_id, rows)


register_belief_hook(_project)
register_belief_hook(_record_change)


async def follow_operations(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str], to: Membership
) -> None:
    """A membership flip of ``operation_ids`` (``core.membership``'s follower): the beliefs they
    wrote, and the beliefs those closed, are re-derived in the flip's transaction."""
    del to
    ids = sorted(set(operation_ids))
    if not ids:
        return
    b = beliefs.c
    where: list[ColumnElement[bool]] = [b.operation_id.in_(ids)]
    if story_id is not None:
        where.append(b.story_id == story_id)
    keys: dict[str, set[Key]] = {}
    for r in await conn.execute(
        select(b.story_id, b.holder_character_id, b.proposition_id).where(*where).distinct()
    ):
        keys.setdefault(str(r.story_id), set()).add(
            (str(r.holder_character_id), str(r.proposition_id))
        )
    for story, found in sorted(keys.items()):
        await refresh(conn, story, found)


register_follower(follow_operations)


# -- writing ----------------------------------------------------------------------------------

BELIEF_SHAPE_COLUMNS: frozenset[str] = frozenset(
    {
        "attributed_holder_id",
        "inner_belief_id",
        "depth",
        "attributed_confidence_level",
        "attributed_confidence_scalar",
        "attributed_held_from",
        "attributed_held_to",
        "speaker_character_id",
        "utterance_id",
        "staged_ref",
        "perceived_intent",
        "disclosure",
    }
)
"""The nested and speech-act columns a write may set (#167, ``_insert_belief``'s ``shape``; a
``proposition_id`` there reuses the stored proposition: a nested belief's rows share one)."""


async def _holder_ok(conn: AsyncConnection, story_id: str, holder: str) -> None:
    """422 ``holder_has_no_partition`` for the player's character or a non-character."""
    row = (
        await conn.execute(
            select(characters.c.is_player).where(
                characters.c.story_id == story_id, characters.c.id == holder
            )
        )
    ).one_or_none()
    if row is None or bool(row.is_player):
        raise HolderHasNoPartition(
            "the player's character has no belief partition (FR-EPI-12)"
            if row is not None
            else "a belief's holder is a character of the story",
            {"story_id": story_id, "holder": holder, "field": "holder"},
        )


async def is_player(conn: AsyncConnection, story_id: str, character_id: str) -> bool:
    """Whether ``character_id`` is the story's player character (``characters.is_player``)."""
    found = await conn.scalar(
        select(characters.c.is_player).where(
            characters.c.story_id == story_id, characters.c.id == character_id
        )
    )
    return bool(found)


async def _subject_characters(
    conn: AsyncConnection, story_id: str, stored: Proposition
) -> tuple[str | None, str | None]:
    """The characters the proposition's subject and object entities are (``None``: none)."""
    wanted = [i for i in (stored.subject_id, stored.object_id) if i is not None]
    found = {
        str(r.id): None if r.character_id is None else str(r.character_id)
        for r in await conn.execute(
            select(entities.c.id, entities.c.character_id).where(
                entities.c.story_id == story_id, entities.c.id.in_(wanted)
            )
        )
    }
    return found.get(stored.subject_id), (
        None if stored.object_id is None else found.get(stored.object_id)
    )


def _event_provenance(operation: BeliefOperation) -> Provenance:
    found = getattr(operation, "provenance", None)
    return found if isinstance(found, Provenance) else Provenance.ENGINE


async def _append_event(
    conn: AsyncConnection,
    story_id: str,
    operation: BeliefOperation,
    payload: EventPayload,
    beat: int | None,
    type_: str = BELIEF_COMMITTED,
) -> str:
    """Append the write's ``derived`` event (``type_``) to its operation (the caller holds the
    story lock: a commit step)."""
    body = validate(type_, payload).model_dump(mode="json")
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    event_id = new_ulid()
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=story_id,
            seq=int(last or 0) + 1,
            operation_id=operation.operation_id,
            category=EventCategory.DERIVED.value,
            narrative_turn=operation.narrative_turn,
            turn_variant=operation.turn_variant,
            beat=beat,
            principal=operation.principal,
            type=type_,
            payload=body,
            provenance=_event_provenance(operation).value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )
    await index_records(
        conn,
        [
            event_record(
                SimpleNamespace(id=event_id, story_id=story_id, character_id=None, payload=body)
            )
        ],
    )
    return event_id


def _bounds(valid_time: Interval | Instant) -> tuple[int | None, int | None]:
    lo, hi = valid_time.possible()
    return lo, hi


async def _insert_belief(
    conn: AsyncConnection,
    story_id: str,
    holder: str,
    proposition: Proposition,
    *,
    operation: BeliefOperation,
    level: BeliefConfidenceLevel,
    scalar: float | None = None,
    held_from: Position | None = None,
    valid_time: Interval | Instant | None = None,
    source_event_ids: Sequence[str] = (),
    beat: int | None = None,
    step: int = 0,
    provenance: str,
    registry: Registry | None = None,
    belief_id: str | None = None,
    lineage_id: str | None = None,
    kind: BeliefKind = BeliefKind.FIRST_ORDER,
    resolution: bool = False,
    shape: Mapping[str, Any] | None = None,
    held_to: Position | None = None,
) -> Belief:
    """Write ``proposition`` as a belief of ``holder`` (first-order unless ``kind`` says
    otherwise: a ``speech_act`` row holds that someone said it, a ``nested``/``attributed`` row
    what the holder thinks someone believes, never reusing or closing a first-order belief; their
    columns are ``shape``'s, ``BELIEF_SHAPE_COLUMNS``) in ``operation``'s commit
    transaction (see the module docstring); answers the belief, with the open belief it closed
    in ``closed_id``. 422 ``holder_has_no_partition`` for the player's character; 422
    ``unknown_predicate``; 409 ``erased_subject_referenced`` for an erased subject.

    ``held_to`` (#165's backdated hold, ``transitions``' own): the row is written already closed
    there, the earlier span of a hold whose open row starts at ``held_to``; that row is left
    open (it must start at ``held_to`` or later)."""
    if getattr(operation, "story_id", story_id) != story_id:
        raise InvalidRequest(
            "a belief is written by an operation of its own story", {"field": "operation"}
        )
    await _holder_ok(conn, story_id, holder)
    found = registry if registry is not None else await load(conn, story_id)
    if found.get(proposition.predicate_id) is None:
        raise UnknownPredicate(
            f"predicate {proposition.predicate_id} is not registered for the story",
            {"field": "proposition.predicate_id", "predicate_id": proposition.predicate_id},
        )
    stored = canonical(proposition, found)
    subject_character, object_character = await _subject_characters(conn, story_id, stored)
    extra = {k: v for k, v in (shape or {}).items() if k in BELIEF_SHAPE_COLUMNS}
    attributed = extra.get("attributed_holder_id")
    speaker = extra.get("speaker_character_id")
    references = frozenset(
        c
        for c in (holder, subject_character, object_character, attributed, speaker)
        if c is not None
    )
    await guard.check_content(
        conn, story_id, [guard.Content((), references)], base_revision=None, path=__name__
    )
    if scalar is not None and not 0 <= scalar <= 1:
        raise InvalidRequest(
            "a belief's confidence scalar is between 0 and 1",
            {"field": "confidence_scalar", "reason": "out_of_range"},
        )
    start = held_from or Position(
        turn=operation.narrative_turn or 0,
        variant=operation.turn_variant or 0,
        beat=beat or 0,
        step=step,
    )
    b = beliefs.c
    p = propositions.c
    # The holder's open belief in the same proposition (subject, predicate, object, qualifiers
    # and polarity; its time is the belief's own): the new row reuses its proposition and closes
    # it. Otherwise the proposition is stored as canon's are (#131's store).
    candidates: list[Belief] = []
    if kind is BeliefKind.FIRST_ORDER:
        candidates = [
            _belief_of(r)
            for r in await conn.execute(
                _select()
                .where(
                    b.story_id == story_id,
                    b.holder_character_id == holder,
                    p.subject_id == stored.subject_id,
                    p.predicate_id == stored.predicate_id,
                    b.polarity == stored.polarity.value,
                    *_open(),
                    b.held_to_turn.is_(None),
                )
                .order_by(b.id)
                .with_for_update(of=beliefs)
            )
        ]
    prior = next(
        (x for x in candidates if x.proposition.normalized_key() == stored.normalized_key()), None
    )
    reused = (shape or {}).get("proposition_id")
    proposition_id = (
        prior.proposition_id
        if prior is not None
        else str(reused)
        if reused is not None
        else await store.write_proposition(conn, story_id, proposition, found)
    )
    closed = prior
    if held_to is not None:
        if not start.before(held_to) or (prior is not None and prior.held_from.before(held_to)):
            raise InvalidRequest(
                "a backdated hold ends where the held belief starts, after its own start",
                {"field": "held_to", "reason": "not_before_held"},
            )
        closed = None
    if closed is not None and not closed.held_from.before(start):
        raise InvalidRequest(
            "the holder already holds this proposition from this position or a later one",
            {"field": "held_from", "reason": "held_already", "belief_id": closed.id},
        )
    lo, hi = _bounds(valid_time if valid_time is not None else stored.valid_time)
    belief_id = belief_id or new_ulid()
    event_id = await _append_event(
        conn,
        story_id,
        operation,
        BeliefCommitted(
            belief_id=belief_id,
            proposition_id=proposition_id,
            closes=None if closed is None else closed.id,
        ),
        beat,
    )
    if closed is not None:
        await conn.execute(
            update(beliefs).where(b.id == closed.id).values(**start.columns("held_to"))
        )
    now = (await conn.execute(select(func.transaction_timestamp()))).scalar_one()
    row: dict[str, Any] = {
        "id": belief_id,
        "story_id": story_id,
        "holder_character_id": holder,
        "kind": BeliefKind(kind).value,
        "proposition_id": proposition_id,
        "polarity": stored.polarity.value,
        "confidence_level": BeliefConfidenceLevel(level).value,
        "confidence_scalar": scalar,
        "valid_from": lo,
        "valid_to": hi,
        "valid_span": Range(lo, hi, bounds="[)"),
        **start.columns("held_from"),
        **({} if held_to is None else held_to.columns("held_to")),
        "tx_from": now,
        "provenance": provenance,
        "visibility": private_to(holder).document(),
        "operation_id": operation.operation_id,
        "source_event_ids": list(source_event_ids) or [event_id],
        "narrative_turn": operation.narrative_turn,
        "turn_variant": operation.turn_variant,
        "beat": beat,
        "step": step,
        "lineage_id": lineage_id or belief_id,
        # #164: a lazy resolution's inclusion operation is the needing turn that wrote it.
        "resolution_operation_id": operation.operation_id if resolution else None,
        **extra,
    }
    await insert_derived(conn, beliefs, [row])
    await index_records(
        conn,
        [
            belief_record(
                SimpleNamespace(
                    id=belief_id,
                    story_id=story_id,
                    holder_character_id=holder,
                    subject_character_id=subject_character,
                    object_character_id=object_character,
                    attributed_holder_id=attributed,
                    speaker_character_id=speaker,
                )
            )
        ],
    )
    written = await read_belief(conn, story_id, belief_id)
    assert written is not None  # noqa: S101 - just written
    if closed is not None:
        closed = await read_belief(conn, story_id, closed.id)
        written = written.model_copy(update={"closed_id": None if closed is None else closed.id})
    change = BeliefChange(story_id, operation.operation_id, written, closed)
    for hook in list(BELIEF_HOOKS):
        await hook(conn, change)
    return written


__all__ = [
    "BELIEF_COMMITTED",
    "BELIEF_HOOKS",
    "BELIEF_SHAPE_COLUMNS",
    "HELD_LEVELS",
    "INSPECT_LIMIT_DEFAULT",
    "INSPECT_LIMIT_MAX",
    "LEVELS",
    "Belief",
    "BeliefChange",
    "BeliefHook",
    "BeliefInspection",
    "BeliefOperation",
    "BeliefState",
    "Key",
    "NestedBelief",
    "SpeechActBelief",
    "StatePage",
    "at_least",
    "belief_states",
    "believers_of",
    "chain_signature",
    "derive",
    "derive_nested",
    "fold_nested_state",
    "fold_state",
    "follow_operations",
    "held_at_clause",
    "inspect",
    "is_player",
    "nested_of",
    "partition",
    "read_belief",
    "refresh",
    "refresh_nested",
    "register_belief_hook",
    "remove_belief_hook",
    "speech_acts",
]
