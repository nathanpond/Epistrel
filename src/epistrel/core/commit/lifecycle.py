"""The commitment lifecycle and the promissory ledger (FR-COMMIT-1..3,5..7,10,13; docs/02 §4.4;
#146).

Everything here writes inside an ``Operation``'s commit transaction (a commit step: the turn's
one transaction, under the story lock), so a thunk, a ledger entry and the fact that resolves
one commit with the turn that made them or not at all. Gated by the ``commitment`` module
(#129): with it off (a Lite story) every entry point is inert, writes nothing and answers so,
and every fact any writer commits stays ``committed``.

**Facts** (FR-COMMIT-1). ``commit_state`` moves introduced → pending → committed → revised:
``introduced`` is a fact narrated with an open qualifier (#150's ``latent``/narration),
``pending`` one awaiting a dependence in the same turn, ``committed`` a settled one (every
other writer's, at the writing position), ``revised`` one #154's regeneration reverted.
``transition`` moves one version in place along ``TRANSITIONS`` (the facts guard, migration
0060, refuses any other edge); ``settle_fact`` commits an introduced or pending fact a
dependence leaned on at that beat (``committed_at_position``); ``demote_pending`` sends the
turn's pending facts nobody depended on back to ``introduced`` at its close. #140's verify
reads only ``committed`` facts as evidence.

**Thunks** (FR-COMMIT-5,6,10). ``introduce`` records a global undetermined reference (a
``ThunkSketch``: one open argument, no world value): a second sketch of the same reference
dedupes to the open thunk, and past ``thunks_max_open`` the oldest open thunk that is not
salient expires (it is ignored from then on). Nothing else creates thunks: #150 calls it for a
salient sketch's open slot and for a detected claim with an open argument on a ``do`` or
narration beat (never ``say``), #149 for a seed with an open slot at ``(0, 1, 0)``.

``depend`` is a dependence (FR-COMMIT-2,10): a ``think``, ``do``, narration or player-input beat
whose claim fills an open thunk's slot. It takes the value from the beat's own claim and
``commit_fact`` writes it with ``commit_state = committed``, ``committed_at_position`` the beat,
``thunk_id`` the thunk and ``valid_from`` the thunk's ``valid_time``; the thunk is resolved
(once, globally: any character's first dependence) and its ledger entries are discharged. A
``say`` beat only mentions (nothing is written: the thunk stays open with no world value). A
second dependence reads the committed fact instead (``settled``, or ``contradicted`` for another
value): two dependences never write two facts. A hidden value is not a thunk: it is committed
with a ``characters`` visibility (#133; FR-COMMIT-5's sticky, decided state).

**The ledger** (FR-COMMIT-7,13). ``make_salient`` records one entry per salient proposition: a
thunk's (the obligation to settle it before the turn closes) or, with ``reference_hidden``, a
hidden fact's existence named without its value (never a turn-close blocker, discharged by a
later reveal). A reference whose thunk already has an undischarged entry adds none.
``blockers`` are the turn's undischarged thunk entries; ``close_turn`` discharges each with the
deterministic ``minimal_commit`` (``resolution_mode = minimal``, degradation
``ledger_minimal_commit``): the most generic consistent value, an event fact
``<predicate>_occurred`` about the reference's known argument with no object (no anonymous
entity), so "a knock occurred" is committed while "who knocked" stays an open thunk with no
obligation (FR-COMMIT-13). The follow-up render that would settle a reference first is #150's.

**Membership.** A thunk and an entry follow the operation that wrote them (#130). Their
resolution and discharge are read through the resolving or discharging operation's
membership: truncating that operation makes the thunk open and the obligation undischarged
again, so it blocks the next turn's close.

Each change appends a ``derived`` event (``thunk_introduced``, ``thunk_resolved``,
``ledger_entry``, ``ledger_discharged``; identifiers only) and counts in
``epistrel_thunks_total``, ``epistrel_ledger_total`` and ``epistrel_minimal_commits_total``. A
thunk's sketch is checked by the resurrection guard and indexed for erasure in the same
transaction (``check_content(``, ``core.erasure.records.thunk_record``). Logs carry identifiers
and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    exists,
    func,
    insert,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.commit import reconcile as reconciling
from epistrel.core.commit.sketch import ThunkSketch, fills
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.config_changes import section
from epistrel.core.enums import (
    Authority,
    DischargeMode,
    EventCategory,
    FactCommitState,
    LedgerKind,
    OpenSlot,
    Provenance,
    RecordProvenance,
    ThunkState,
)
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import event_record, thunk_record
from epistrel.core.errors import InvalidRequest, ModuleOff
from epistrel.core.events.registry import EventPayload, validate
from epistrel.core.events.types import (
    LedgerDischarged,
    LedgerEntryCreated,
    ThunkIntroduced,
    ThunkResolved,
)
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_only, active_operation, insert_derived
from epistrel.core.profiles.capabilities import level_at_least, module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import (
    NONE,
    Granularity,
    Instant,
    Interval,
    Proposition,
)
from epistrel.core.propositions.registry import Registry, auto_register, load
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.resolution.time import story_time_at
from epistrel.core.settings_schema import CommitmentSettings
from epistrel.core.tables import deployment_state, entities, events, operations, predicates
from epistrel.core.tables.commitments import promissory_ledger, thunks
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.facts import facts
from epistrel.observability.metrics import LEDGER, MINIMAL_COMMITS, THUNKS

log = logging.getLogger(__name__)

COMMITMENT_MODULE = "commitment"
"""The profile module that turns commitment on (``commitment.enabled``, #106)."""

Channel = Literal["say", "do", "think", "narration", "latent", "player"]
DEPENDENCE_CHANNELS: frozenset[str] = frozenset({"think", "do", "narration", "player"})
"""The beats whose claim is a dependence (FR-COMMIT-2: any character's ``think`` or ``do``;
narration and player input count too, as the world's and the player's). A ``say`` mentions."""

TRANSITIONS: frozenset[tuple[FactCommitState, FactCommitState]] = frozenset(
    {
        (FactCommitState.INTRODUCED, FactCommitState.PENDING),
        (FactCommitState.INTRODUCED, FactCommitState.COMMITTED),
        (FactCommitState.PENDING, FactCommitState.COMMITTED),
        (FactCommitState.PENDING, FactCommitState.INTRODUCED),
        (FactCommitState.COMMITTED, FactCommitState.REVISED),
        (FactCommitState.REVISED, FactCommitState.COMMITTED),
    }
)
"""The in-place moves of a fact's ``commit_state`` (migration 0060's guard holds the same set)."""

MINIMAL_COMMIT = "ledger_minimal_commit"
"""The degradation a minimal commit reports (it marks the mechanism, not why it ran)."""
OCCURRED_SUFFIX = "_occurred"
"""A minimal commit's event predicate: ``<thunk predicate>_occurred``, object-less."""

THUNK_INTRODUCED = "thunk_introduced"
THUNK_RESOLVED = "thunk_resolved"
LEDGER_ENTRY = "ledger_entry"
LEDGER_DISCHARGED = "ledger_discharged"


class LedgerUnsettled(RuntimeError):
    """A turn tried to close with an undischarged salient entry (a bug in its caller:
    ``close_turn`` discharges every one)."""


# -- the gate ----------------------------------------------------------------------------------


def commitment_enabled(settings: ProfileSettings) -> bool:
    """Whether ``settings`` runs commitment: the ``structured`` pipeline with the
    ``commitment`` module on."""
    return level_at_least(settings, "pipeline", "structured") and module_on(
        settings, COMMITMENT_MODULE
    )


async def commitment_on(conn: AsyncConnection, story_id: str) -> bool:
    """Whether the story's effective configuration runs commitment (the one check a turn makes
    at its start; every entry point here makes it when not told)."""
    found = await effective_configuration(conn, story_id, validate=False)
    return commitment_enabled(found.settings)


async def resolve_on(conn: AsyncConnection, story_id: str, on: bool | None) -> bool:
    """``on`` when the caller already checked (the turn's one check), else the story's."""
    return await commitment_on(conn, story_id) if on is None else on


# -- views -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Thunk:
    """One thunk as the lifecycle reads it; ``state`` is the effective state (a ``resolved``
    row whose resolving operation left the timeline is ``open``)."""

    id: str
    story_id: str
    sketch: ThunkSketch
    open_slot: OpenSlot
    valid_time: int | None
    state: ThunkState
    stored_state: ThunkState
    salient: bool
    operation_id: str
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None
    resolved_fact_id: str | None
    resolution_operation_id: str | None
    introduced_event_id: str | None

    @property
    def open(self) -> bool:
        return self.state is ThunkState.OPEN


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One ledger entry; ``discharged`` is effective (its discharging operation is active)."""

    id: str
    story_id: str
    thunk_id: str | None
    proposition_id: str | None
    kind: LedgerKind
    operation_id: str
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None
    discharged_by_operation: str | None
    discharged_mode: DischargeMode | None
    discharged: bool

    @property
    def introduced_position(self) -> dict[str, int | None]:
        return {"turn": self.narrative_turn, "variant": self.turn_variant, "beat": self.beat}


def _resolver_active(t: Any) -> ColumnElement[bool]:
    o = operations.alias("resolver")
    return exists().where(o.c.operation_id == t.c.resolution_operation_id, active_operation(o))


def _discharger_active(e: Any) -> ColumnElement[bool]:
    o = operations.alias("discharger")
    return exists().where(o.c.operation_id == e.c.discharged_by_operation, active_operation(o))


def _resolution(t: Any, column: str) -> Any:
    """``column`` of thunk ``t``'s resolving fact in the timeline (its newest), as a scalar
    subquery: the fact a dependence committed with ``thunk_id`` ``t`` on the thunk's own
    predicate (#154; obligation facts carry the thunk id too, on other predicates)."""
    f = facts.alias("resolution")
    return (
        select(f.c[column])
        .where(
            f.c.story_id == t.c.story_id,
            f.c.thunk_id == t.c.id,
            f.c.predicate_id == t.c.predicate_id,
            active_only(f),
        )
        .order_by(f.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def _effectively_resolved(t: Any) -> ColumnElement[bool]:
    """Thunk ``t`` has a resolution in the timeline: its stored resolver is active, or (#154)
    another resolution of it is, after the timeline moved between variants that each resolved
    it (a swap back restores the restored chain's own resolution; the row keeps the last)."""
    return and_(
        t.c.state == ThunkState.RESOLVED.value,
        or_(_resolver_active(t), _resolution(t, "id").is_not(None)),
    )


def open_clause(t: Any) -> ColumnElement[bool]:
    """Thunk ``t`` (``thunks`` or an alias) is in the timeline and effectively open."""
    return and_(
        active_only(t), t.c.state != ThunkState.EXPIRED.value, not_(_effectively_resolved(t))
    )


def _thunk_resolved(e: Any) -> ColumnElement[bool]:
    t = thunks.alias("owed")
    return exists().where(t.c.id == e.c.thunk_id, _effectively_resolved(t))


def undischarged_clause(e: Any) -> ColumnElement[bool]:
    """Entry ``e`` is in the timeline and effectively undischarged: its discharging operation
    is not active, and its thunk (when it has one) is not effectively resolved (a resolved
    thunk owes nothing, whichever variant resolved it, #154)."""
    return and_(
        active_only(e),
        or_(e.c.discharged_by_operation.is_(None), not_(_discharger_active(e))),
        not_(and_(e.c.thunk_id.is_not(None), _thunk_resolved(e))),
    )


def thunk_columns(t: Any) -> list[Any]:
    return [
        t,
        _effectively_resolved(t).label("is_resolved"),
        case((_resolver_active(t), t.c.resolved_fact_id), else_=_resolution(t, "id")).label(
            "effective_fact_id"
        ),
        case(
            (_resolver_active(t), t.c.resolution_operation_id),
            else_=_resolution(t, "operation_id"),
        ).label("effective_operation_id"),
    ]


def thunk_of(row: Any) -> Thunk:
    m: Mapping[str, Any] = row._mapping
    stored = ThunkState(m["state"])
    if stored is ThunkState.EXPIRED:
        state = ThunkState.EXPIRED
    else:
        state = ThunkState.RESOLVED if m["is_resolved"] else ThunkState.OPEN
    fact_id, operation_id = m["resolved_fact_id"], m["resolution_operation_id"]
    if state is ThunkState.RESOLVED and m.get("effective_fact_id") is not None:
        # The resolution in the timeline (#154), which a swap may have restored.
        fact_id, operation_id = m["effective_fact_id"], m["effective_operation_id"]
    return Thunk(
        id=str(m["id"]),
        story_id=str(m["story_id"]),
        sketch=ThunkSketch.model_validate(m["proposition_sketch"]),
        open_slot=OpenSlot(m["open_slot"]),
        valid_time=m["valid_time"],
        state=state,
        stored_state=stored,
        salient=bool(m["salient"]),
        operation_id=str(m["operation_id"]),
        narrative_turn=m["narrative_turn"],
        turn_variant=m["turn_variant"],
        beat=m["beat"],
        resolved_fact_id=None if fact_id is None else str(fact_id),
        resolution_operation_id=None if operation_id is None else str(operation_id),
        introduced_event_id=None
        if m["introduced_event_id"] is None
        else str(m["introduced_event_id"]),
    )


def _entry_columns(e: Any) -> list[Any]:
    return [
        e,
        or_(
            and_(e.c.discharged_by_operation.is_not(None), _discharger_active(e)),
            and_(e.c.thunk_id.is_not(None), _thunk_resolved(e)),
        ).label("is_done"),
    ]


def _entry_of(row: Any) -> LedgerEntry:
    m: Mapping[str, Any] = row._mapping
    return LedgerEntry(
        id=str(m["id"]),
        story_id=str(m["story_id"]),
        thunk_id=None if m["thunk_id"] is None else str(m["thunk_id"]),
        proposition_id=None if m["proposition_id"] is None else str(m["proposition_id"]),
        kind=LedgerKind(m["kind"]),
        operation_id=str(m["operation_id"]),
        narrative_turn=m["narrative_turn"],
        turn_variant=m["turn_variant"],
        beat=m["beat"],
        discharged_by_operation=None
        if m["discharged_by_operation"] is None
        else str(m["discharged_by_operation"]),
        discharged_mode=None
        if m["discharged_mode"] is None
        else DischargeMode(m["discharged_mode"]),
        discharged=bool(m["is_done"]),
    )


async def read_thunk(conn: AsyncConnection, story_id: str, thunk_id: str) -> Thunk | None:
    """Thunk ``thunk_id`` of the story (any state), or ``None``."""
    t = thunks.alias("t")
    row = (
        await conn.execute(
            select(*thunk_columns(t)).where(t.c.story_id == story_id, t.c.id == thunk_id)
        )
    ).one_or_none()
    return None if row is None else thunk_of(row)


async def entries_of(
    conn: AsyncConnection, story_id: str, thunk_id: str, *, undischarged: bool = False
) -> list[LedgerEntry]:
    """The active ledger entries on ``thunk_id`` (only the undischarged ones when asked)."""
    e = promissory_ledger.alias("e")
    where: list[ColumnElement[bool]] = [
        e.c.story_id == story_id,
        e.c.thunk_id == thunk_id,
        active_only(e),
    ]
    if undischarged:
        where.append(undischarged_clause(e))
    rows = await conn.execute(select(*_entry_columns(e)).where(*where).order_by(e.c.id))
    return [_entry_of(r) for r in rows]


# -- writing -----------------------------------------------------------------------------------


def _event_provenance(operation: canon.FactOperation) -> Provenance:
    found = getattr(operation, "provenance", None)
    return found if isinstance(found, Provenance) else Provenance.ENGINE


async def _append(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    story_id: str,
    type_: str,
    payload: EventPayload,
    beat: int | None,
) -> str:
    """Append a ``derived`` event to ``operation`` (a commit step holds the story lock)."""
    model = validate(type_, payload)
    body = model.model_dump(mode="json")
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


def _story_of(operation: canon.FactOperation) -> str:
    if operation.story_id is None:
        raise InvalidRequest("a commitment is written by a story's operation", {})
    return operation.story_id


async def max_open(conn: AsyncConnection) -> int:
    """The deployment's ``thunks_max_open`` (#146; default 500)."""
    document = await conn.scalar(
        select(deployment_state.c.settings).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return section(CommitmentSettings, document).thunks_max_open


async def _prune(conn: AsyncConnection, story_id: str, keep: int) -> int:
    """Expire the oldest open thunks that are not salient beyond ``keep`` (FR-COMMIT-10's
    unforced thunks carry no obligation, so dropping the oldest loses nothing owed)."""
    t = thunks.alias("t")
    count = int(
        await conn.scalar(select(func.count()).where(t.c.story_id == story_id, open_clause(t))) or 0
    )
    excess = count - keep
    if excess <= 0:
        return 0
    victims = list(
        await conn.scalars(
            select(t.c.id)
            .where(t.c.story_id == story_id, open_clause(t), t.c.salient.is_(False))
            .order_by(t.c.id)
            .limit(excess)
        )
    )
    if not victims:
        return 0
    await conn.execute(
        update(thunks).where(thunks.c.id.in_(victims)).values(state=ThunkState.EXPIRED.value)
    )
    THUNKS.labels(event="expired").inc(len(victims))
    log.debug("thunks: %d expired past thunks_max_open in story %s", len(victims), story_id)
    return len(victims)


async def find_open(conn: AsyncConnection, story_id: str, sketch: ThunkSketch) -> Thunk | None:
    """The open thunk of the same reference as ``sketch`` (its predicate, known arguments and
    open slot), or ``None``."""
    t = thunks.alias("t")
    rows = await conn.execute(
        select(*thunk_columns(t))
        .where(
            t.c.story_id == story_id,
            t.c.predicate_id == sketch.predicate_id,
            t.c.subject_id.is_not_distinct_from(sketch.subject_id),
            t.c.object_id.is_not_distinct_from(sketch.object_id),
            t.c.open_slot == sketch.open_slot.value,
            open_clause(t),
        )
        .order_by(t.c.id)
    )
    for row in rows:
        found = thunk_of(row)
        if found.sketch.key == sketch.key:
            return found
    return None


async def introduce(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    sketch: ThunkSketch,
    *,
    valid_time: int | None = None,
    beat: int | None = None,
    salient: bool = False,
    source_event_ids: Sequence[str] = (),
    on: bool | None = None,
) -> Thunk | None:
    """Record ``sketch`` as an open global thunk of ``operation`` (see the module docstring),
    or answer the open thunk of the same reference; ``salient`` also gives it a ledger entry
    (``make_salient``). ``None`` when commitment is off."""
    story_id = _story_of(operation)
    if not await resolve_on(conn, story_id, on):
        return None
    existing = await find_open(conn, story_id, sketch)
    if existing is not None:
        if salient:
            await make_salient(conn, operation, existing, beat=beat, on=True)
            return await read_thunk(conn, story_id, existing.id)
        return existing
    texts = tuple(
        t
        for t in (
            sketch.subject_text,
            sketch.relation_text,
            sketch.object_text,
            sketch.time_text,
            *(c.text for c in sketch.candidates),
        )
        if t
    )
    references = frozenset(
        i
        for i in (sketch.subject_id, sketch.object_id, *(c.entity_id for c in sketch.candidates))
        if i is not None
    )
    await guard.check_content(
        conn, story_id, [guard.Content(texts, references)], base_revision=None, path=__name__
    )
    thunk_id = new_ulid()
    event_id = await _append(
        conn,
        operation,
        story_id,
        THUNK_INTRODUCED,
        ThunkIntroduced(thunk_id=thunk_id, open_slot=sketch.open_slot.value, salient=False),
        beat,
    )
    row = {
        "id": thunk_id,
        "story_id": story_id,
        "proposition_sketch": sketch.document(),
        "predicate_id": sketch.predicate_id,
        "subject_id": sketch.subject_id,
        "object_id": sketch.object_id,
        "open_slot": sketch.open_slot.value,
        "valid_time": valid_time,
        "introduced_event_id": event_id,
        "operation_id": operation.operation_id,
        "source_event_ids": list(source_event_ids) or [event_id],
        "narrative_turn": operation.narrative_turn,
        "turn_variant": operation.turn_variant,
        "beat": beat,
    }
    await insert_derived(conn, thunks, [row])
    await index_records(conn, [thunk_record(SimpleNamespace(**row))])
    THUNKS.labels(event="introduced").inc()
    await _prune(conn, story_id, await max_open(conn))
    found = await read_thunk(conn, story_id, thunk_id)
    if found is None:  # pragma: no cover - written above
        raise RuntimeError("the thunk just written is missing")
    if salient and found.open:
        await make_salient(conn, operation, found, beat=beat, on=True)
        found = await read_thunk(conn, story_id, thunk_id) or found
    return found


async def make_salient(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    thunk: Thunk,
    *,
    beat: int | None = None,
    on: bool | None = None,
) -> LedgerEntry | None:
    """A salient reference to ``thunk``: one ledger entry for its proposition (FR-COMMIT-7,13),
    unless it has an undischarged one already or is not open (``None`` then, and when
    commitment is off)."""
    story_id = _story_of(operation)
    if not await resolve_on(conn, story_id, on) or not thunk.open:
        return None
    if await entries_of(conn, story_id, thunk.id, undischarged=True):
        return None
    entry_id = new_ulid()
    event_id = await _append(
        conn,
        operation,
        story_id,
        LEDGER_ENTRY,
        LedgerEntryCreated(entry_id=entry_id, thunk_id=thunk.id),
        beat,
    )
    await insert_derived(
        conn,
        promissory_ledger,
        [
            {
                "id": entry_id,
                "story_id": story_id,
                "thunk_id": thunk.id,
                "kind": LedgerKind.SALIENT_REFERENCE.value,
                "operation_id": operation.operation_id,
                "source_event_ids": [event_id],
                "narrative_turn": operation.narrative_turn,
                "turn_variant": operation.turn_variant,
                "beat": beat,
            }
        ],
    )
    if not thunk.salient:
        await conn.execute(update(thunks).where(thunks.c.id == thunk.id).values(salient=True))
    LEDGER.labels(event="created").inc()
    return (await entries_of(conn, story_id, thunk.id, undischarged=True))[0]


async def reference_hidden(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    proposition_id: str,
    *,
    beat: int | None = None,
    on: bool | None = None,
) -> str | None:
    """A salient reference to a hidden fact's existence without its value ("she knows the
    answer"): a ledger entry on its proposition, never a turn-close blocker, discharged by the
    later beat that reveals it (``discharge_hidden``). Answers the entry's id (``None`` when an
    undischarged one exists or commitment is off)."""
    story_id = _story_of(operation)
    if not await resolve_on(conn, story_id, on):
        return None
    e = promissory_ledger.alias("e")
    existing = await conn.scalar(
        select(e.c.id).where(
            e.c.story_id == story_id,
            e.c.proposition_id == proposition_id,
            undischarged_clause(e),
        )
    )
    if existing is not None:
        return None
    entry_id = new_ulid()
    event_id = await _append(
        conn,
        operation,
        story_id,
        LEDGER_ENTRY,
        LedgerEntryCreated(entry_id=entry_id, proposition_id=proposition_id),
        beat,
    )
    await insert_derived(
        conn,
        promissory_ledger,
        [
            {
                "id": entry_id,
                "story_id": story_id,
                "proposition_id": proposition_id,
                "kind": LedgerKind.SALIENT_REFERENCE.value,
                "operation_id": operation.operation_id,
                "source_event_ids": [event_id],
                "narrative_turn": operation.narrative_turn,
                "turn_variant": operation.turn_variant,
                "beat": beat,
            }
        ],
    )
    LEDGER.labels(event="created").inc()
    return entry_id


async def discharge(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    entry_id: str,
    mode: DischargeMode,
    *,
    beat: int | None = None,
    fact_id: str | None = None,
) -> None:
    """Discharge entry ``entry_id`` by ``operation`` (``mode``: how it was settled)."""
    story_id = _story_of(operation)
    await _append(
        conn,
        operation,
        story_id,
        LEDGER_DISCHARGED,
        LedgerDischarged(entry_id=entry_id, mode=mode.value, fact_id=fact_id),
        beat,
    )
    await conn.execute(
        update(promissory_ledger)
        .where(promissory_ledger.c.story_id == story_id, promissory_ledger.c.id == entry_id)
        .values(discharged_by_operation=operation.operation_id, discharged_mode=mode.value)
    )
    LEDGER.labels(event="discharged").inc()


async def discharge_hidden(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    proposition_id: str,
    *,
    beat: int | None = None,
) -> int:
    """A ``say``/``do`` beat revealed a hidden fact: its proposition's undischarged entries are
    discharged (``render``). Answers how many."""
    story_id = _story_of(operation)
    e = promissory_ledger.alias("e")
    ids = list(
        await conn.scalars(
            select(e.c.id)
            .where(
                e.c.story_id == story_id,
                e.c.proposition_id == proposition_id,
                undischarged_clause(e),
            )
            .order_by(e.c.id)
        )
    )
    for entry_id in ids:
        await discharge(conn, operation, str(entry_id), DischargeMode.RENDER, beat=beat)
    return len(ids)


# -- dependence --------------------------------------------------------------------------------

DependenceKind = Literal[
    "committed",
    "settled",
    "contradicted",
    "rejected",
    "mention",
    "mismatch",
    "ignored",
    "inert",
]


@dataclass(frozen=True, slots=True)
class Dependence:
    """What ``depend`` did: ``committed`` (it resolved the thunk with ``fact``), ``settled``
    (already resolved with the same value: ``fact`` is the committed one), ``contradicted``
    (already resolved with another value), ``rejected`` (#154: the binding conflicts with
    recorded history, ``reconciliation`` says how; nothing written, the thunk stays open),
    ``mention`` (a ``say``: nothing written), ``mismatch`` (the claim does not fill the thunk),
    ``ignored`` (an expired thunk: a later matching claim is plain novelty) or ``inert``
    (commitment off). ``obligations`` are the obligation facts committed with a resolution."""

    kind: DependenceKind
    thunk_id: str
    fact: canon.Fact | None = None
    discharged: tuple[str, ...] = ()
    reconciliation: reconciling.Reconciliation | None = None
    obligations: tuple[str, ...] = ()


BeforeResolve = Callable[[AsyncConnection, Thunk, Proposition], Awaitable[None]]
BEFORE_RESOLVE: list[BeforeResolve] = []
"""Run before a dependence commits a thunk's resolution, in its transaction (after #154's
reconciliation, which ``depend`` asks itself); an exception aborts the commit."""


def _resolution_time(claim: Proposition, valid_time: int | None) -> Interval | Instant | None:
    """The resolving fact's valid-time: from the thunk's ``valid_time`` on (an instant at it
    for a punctual claim), or the claim's own when the thunk has none."""
    if valid_time is None:
        return None
    current = claim.valid_time
    if isinstance(current, Instant):
        return Instant.of(valid_time, current.granularity)
    end = None if current.end is None else current.end.outer
    return Interval.of(valid_time, end if end is not None and end > valid_time else None)


def binding_of(thunk: Thunk, claim: Proposition) -> Proposition:
    """The binding a dependence's ``claim`` makes of ``thunk`` (#154): the claim at the time the
    resolution's fact takes, the thunk's original ``valid_time`` (``_resolution_time``)."""
    resolved_at = _resolution_time(claim, thunk.valid_time)
    return claim if resolved_at is None else claim.model_copy(update={"valid_time": resolved_at})


async def depend(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    thunk_id: str,
    claim: Proposition,
    *,
    beat: int | None,
    channel: Channel,
    provenance: RecordProvenance = RecordProvenance.RESOLUTION,
    source_event_ids: Sequence[str] = (),
    registry: Registry | None = None,
    on: bool | None = None,
    resolution_level: str = "consequential",
) -> Dependence:
    """Beat ``beat`` (of ``channel``) depends on thunk ``thunk_id`` with ``claim`` (#148's
    detected claim, after the beat survived verification): see the module docstring. No
    resolution commits without #154's reconciliation (``core.commit.reconcile``): a conflict is
    ``rejected``; otherwise the resolution's fact (``provenance = resolution`` by default, at
    ``resolution_level``) and its novel obligations commit together, anchored to the thunk."""
    story_id = _story_of(operation)
    if not await resolve_on(conn, story_id, on):
        return Dependence("inert", thunk_id)
    if channel not in DEPENDENCE_CHANNELS:
        return Dependence("mention", thunk_id)
    await conn.execute(
        select(thunks.c.id)
        .where(thunks.c.story_id == story_id, thunks.c.id == thunk_id)
        .with_for_update()
    )
    thunk = await read_thunk(conn, story_id, thunk_id)
    if thunk is None:
        raise InvalidRequest("depend names a thunk of the story", {"thunk_id": thunk_id})
    if thunk.state is ThunkState.EXPIRED:
        return Dependence("ignored", thunk_id)
    if thunk.state is ThunkState.RESOLVED:
        fact = await canon.read_version(conn, story_id, str(thunk.resolved_fact_id))
        same = (
            fact.proposition.normalized_key()
            == claim.model_copy(update={"valid_time": fact.proposition.valid_time}).normalized_key()
        )
        return Dependence("settled" if same else "contradicted", thunk_id, fact)
    if not fills(thunk.sketch, claim):
        return Dependence("mismatch", thunk_id)
    resolved_at = _resolution_time(claim, thunk.valid_time)
    binding = binding_of(thunk, claim)
    registry = registry or await load(conn, story_id)
    reconciled = await reconciling.reconcile(conn, story_id, thunk, binding, registry=registry)
    if isinstance(reconciled, reconciling.Conflict):
        THUNKS.labels(event="rejected").inc()
        return Dependence("rejected", thunk_id, reconciliation=reconciled)
    for hook in list(BEFORE_RESOLVE):
        await hook(conn, thunk, claim)
    # Read before the resolution commits: once it has, the thunk's entries read as discharged.
    owed = await entries_of(conn, story_id, thunk.id, undischarged=True)
    fact = await canon.commit_fact(
        conn,
        story_id,
        claim,
        provenance=provenance,
        operation=operation,
        valid_time=resolved_at,
        source_event_ids=source_event_ids,
        beat=beat,
        commit_state=FactCommitState.COMMITTED,
        registry=registry,
        thunk_id=thunk.id,
        resolution_level=resolution_level,
    )
    obligations: list[str] = []
    for implied in reconciled.to_commit:
        written = await canon.commit_fact(
            conn,
            story_id,
            implied,
            provenance=RecordProvenance.RESOLUTION,
            operation=operation,
            source_event_ids=source_event_ids,
            beat=beat,
            commit_state=FactCommitState.COMMITTED,
            registry=registry,
            thunk_id=thunk.id,
            resolution_level=resolution_level,
        )
        obligations.append(written.id)
    await conn.execute(
        update(thunks)
        .where(thunks.c.id == thunk.id)
        .values(
            state=ThunkState.RESOLVED.value,
            resolved_fact_id=fact.id,
            resolution_operation_id=operation.operation_id,
        )
    )
    await _append(
        conn,
        operation,
        story_id,
        THUNK_RESOLVED,
        ThunkResolved(thunk_id=thunk.id, fact_id=fact.id),
        beat,
    )
    THUNKS.labels(event="resolved").inc()
    discharged = []
    for entry in owed:
        await discharge(conn, operation, entry.id, DischargeMode.DEPENDENCE, beat=beat)
        discharged.append(entry.id)
    return Dependence(
        "committed",
        thunk.id,
        fact,
        tuple(discharged),
        reconciliation=reconciled,
        obligations=tuple(obligations),
    )


# -- closing a turn ----------------------------------------------------------------------------


async def blockers(conn: AsyncConnection, story_id: str) -> list[LedgerEntry]:
    """The undischarged salient entries on open thunks: what a turn may not close with (an
    entry made this turn, or one whose discharging turn was reverted). Proposition entries
    never block."""
    e = promissory_ledger.alias("e")
    t = thunks.alias("t")
    rows = await conn.execute(
        select(*_entry_columns(e))
        .select_from(e.join(t, t.c.id == e.c.thunk_id))
        .where(e.c.story_id == story_id, undischarged_clause(e), open_clause(t))
        .order_by(e.c.id)
    )
    return [_entry_of(r) for r in rows]


async def minimal_commit(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    entry: LedgerEntry,
    *,
    registry: Registry | None = None,
) -> canon.Fact:
    """Discharge ``entry`` deterministically (``resolution_mode = minimal``): commit that the
    referenced event occurred, ``<predicate>_occurred`` about the thunk's known argument with
    no object, at the thunk's ``valid_time`` (see the module docstring). The thunk stays open."""
    story_id = _story_of(operation)
    if entry.thunk_id is None:
        raise InvalidRequest("a minimal commit discharges a thunk's entry", {"entry": entry.id})
    thunk = await read_thunk(conn, story_id, entry.thunk_id)
    if thunk is None:  # pragma: no cover - an entry names a thunk of its story
        raise InvalidRequest("the entry's thunk is missing", {"entry": entry.id})
    registry = registry or await load(conn, story_id)
    predicate = registry.get(thunk.sketch.predicate_id)
    name = (predicate.name if predicate is not None else "event") + OCCURRED_SUFFIX
    occurred = await auto_register(conn, story_id, name, object_type=NONE)
    subject = thunk.sketch.subject_id or thunk.sketch.object_id
    if subject is None:  # pragma: no cover - open_sketch keeps one known entity
        raise InvalidRequest("a minimal commit needs the reference's known argument", {})
    at = thunk.valid_time
    if at is None:
        basis = await load_time_basis(conn, story_id)
        at = story_time_at(thunk.narrative_turn or operation.narrative_turn or 0, basis)
    fact = await canon.commit_fact(
        conn,
        story_id,
        Proposition(
            subject_id=subject,
            predicate_id=occurred.id,
            valid_time=Instant.of(at, Granularity.MINUTE),
        ),
        provenance=RecordProvenance.RENDER,
        operation=operation,
        beat=entry.beat,
        registry=await load(conn, story_id),
    )
    await discharge(
        conn, operation, entry.id, DischargeMode.MINIMAL, beat=entry.beat, fact_id=fact.id
    )
    MINIMAL_COMMITS.inc()
    return fact


FollowUp = Callable[[AsyncConnection, Sequence[LedgerEntry]], Awaitable[Sequence[str]]]
"""#150's follow-up render: given the open entries, render once constrained to settle them
(its beats run through detection, verify and ``depend``) and answer the entries it settled."""


class CommitReport(BaseModel):
    """``turn_report.commit`` (#150 stores it): what the turn's commitment did, as counts."""

    thunks_introduced: int = 0
    dependences: int = 0
    ledger_created: int = 0
    ledger_discharged: int = 0
    minimal_commits: int = 0
    demoted: int = 0
    degradation: list[str] = []


@dataclass(slots=True)
class _Tally:
    minimal: int = 0
    demoted: int = 0
    degradation: list[str] = field(default_factory=list)


async def close_turn(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    *,
    follow_up: FollowUp | None = None,
    on: bool | None = None,
) -> CommitReport:
    """The turn's close (FR-COMMIT-7): ``follow_up`` (when the turn can afford it) may settle
    the open entries first; every one left is discharged by ``minimal_commit``; the turn's
    pending facts nobody depended on go back to ``introduced``. A turn never closes with an
    undischarged salient entry (``LedgerUnsettled``). Inert when commitment is off."""
    story_id = _story_of(operation)
    if not await resolve_on(conn, story_id, on):
        return CommitReport()
    tally = _Tally()
    open_entries = await blockers(conn, story_id)
    if open_entries and follow_up is not None:
        await follow_up(conn, open_entries)
        open_entries = await blockers(conn, story_id)
    if open_entries:
        registry = await load(conn, story_id)
        for entry in open_entries:
            await minimal_commit(conn, operation, entry, registry=registry)
            tally.minimal += 1
        tally.degradation.append(MINIMAL_COMMIT)
    tally.demoted = await demote_pending(conn, story_id, operation.operation_id)
    if await blockers(conn, story_id):  # pragma: no cover - discharged above
        raise LedgerUnsettled("a salient reference is still open at the turn's close")
    return await report(conn, story_id, operation.operation_id, tally)


async def report(
    conn: AsyncConnection, story_id: str, operation_id: str, tally: _Tally | None = None
) -> CommitReport:
    """What operation ``operation_id`` did to the story's commitments (counts)."""
    tally = tally or _Tally()
    t = thunks.c
    e = promissory_ledger.c
    introduced = await conn.scalar(
        select(func.count()).where(t.story_id == story_id, t.operation_id == operation_id)
    )
    resolved = await conn.scalar(
        select(func.count()).where(
            t.story_id == story_id, t.resolution_operation_id == operation_id
        )
    )
    created = await conn.scalar(
        select(func.count()).where(e.story_id == story_id, e.operation_id == operation_id)
    )
    discharged = await conn.scalar(
        select(func.count()).where(
            e.story_id == story_id, e.discharged_by_operation == operation_id
        )
    )
    minimal = await conn.scalar(
        select(func.count()).where(
            e.story_id == story_id,
            e.discharged_by_operation == operation_id,
            e.discharged_mode == DischargeMode.MINIMAL.value,
        )
    )
    return CommitReport(
        thunks_introduced=int(introduced or 0),
        dependences=int(resolved or 0),
        ledger_created=int(created or 0),
        ledger_discharged=int(discharged or 0),
        minimal_commits=int(minimal or 0),
        demoted=tally.demoted,
        degradation=list(dict.fromkeys(tally.degradation)),
    )


# -- the fact lifecycle ------------------------------------------------------------------------


def _position(operation: canon.FactOperation, beat: int | None) -> dict[str, Any] | None:
    if operation.narrative_turn is None:
        return None
    return {"turn": operation.narrative_turn, "variant": operation.turn_variant, "beat": beat}


async def transition(
    conn: AsyncConnection,
    story_id: str,
    fact_id: str,
    to: FactCommitState,
    *,
    position: dict[str, Any] | None = None,
) -> FactCommitState:
    """Move version ``fact_id``'s ``commit_state`` to ``to`` in place (``TRANSITIONS``; 422
    ``invalid_request`` for another move); a move to ``committed`` records ``position`` as its
    ``committed_at_position`` when given. Answers the state it left."""
    current = await conn.scalar(
        select(facts.c.commit_state)
        .where(facts.c.story_id == story_id, facts.c.id == fact_id)
        .with_for_update()
    )
    if current is None:
        raise InvalidRequest("a transition names a fact of the story", {"fact_id": fact_id})
    was = FactCommitState(current)
    if (was, FactCommitState(to)) not in TRANSITIONS:
        raise InvalidRequest(
            f"a fact's commit state cannot move from {was.value} to {FactCommitState(to).value}",
            {"field": "commit_state", "reason": "invalid_transition"},
        )
    values: dict[str, Any] = {"commit_state": FactCommitState(to).value}
    if to is FactCommitState.COMMITTED and position is not None:
        values["committed_at_position"] = position
    await conn.execute(update(facts).where(facts.c.id == fact_id).values(**values))
    return was


async def settle_fact(
    conn: AsyncConnection,
    operation: canon.FactOperation,
    fact_id: str,
    *,
    beat: int | None,
    channel: Channel,
) -> bool:
    """A dependence on an introduced or pending fact (FR-COMMIT-2): it commits at this beat.
    ``False`` (nothing changed) for a ``say``, or a fact already committed."""
    story_id = _story_of(operation)
    if channel not in DEPENDENCE_CHANNELS:
        return False
    current = await conn.scalar(
        select(facts.c.commit_state).where(
            facts.c.story_id == story_id, facts.c.id == fact_id, active_only(facts)
        )
    )
    # A fact a regeneration reverted (#154: ``revised``, still in the timeline because an earlier
    # turn introduced it) commits again at the dependence that forces it now.
    if current is None or FactCommitState(current) not in (
        FactCommitState.INTRODUCED,
        FactCommitState.PENDING,
        FactCommitState.REVISED,
    ):
        return False
    await transition(
        conn, story_id, fact_id, FactCommitState.COMMITTED, position=_position(operation, beat)
    )
    return True


async def demote_pending(conn: AsyncConnection, story_id: str, operation_id: str) -> int:
    """The operation's ``pending`` facts nobody depended on go back to ``introduced``."""
    result = await conn.execute(
        update(facts)
        .where(
            facts.c.story_id == story_id,
            facts.c.operation_id == operation_id,
            facts.c.commit_state == FactCommitState.PENDING.value,
        )
        .values(commit_state=FactCommitState.INTRODUCED.value)
    )
    return int(result.rowcount)


# -- reading -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Inspection:
    """``GET /stories/{id}/inspect/ledger``: a page of thunks, the ledger entries on them (and,
    on the first page, the proposition entries), and the cursor of the next page."""

    thunks: tuple[Thunk, ...]
    ledger: tuple[LedgerEntry, ...]
    next_after_id: str | None


async def inspect(
    conn: AsyncConnection,
    story_id: str,
    *,
    state: ThunkState | None = None,
    discharged: bool | None = None,
    limit: int = 50,
    after_id: str | None = None,
) -> Inspection:
    """The story's thunks in the timeline (only those in effective ``state`` when given), by
    id after ``after_id``, ``limit`` a page, with their ledger entries (only discharged or
    undischarged ones when ``discharged`` is given)."""
    t = thunks.alias("t")
    where: list[ColumnElement[bool]] = [t.c.story_id == story_id, active_only(t)]
    if state is ThunkState.OPEN:
        where.append(open_clause(t))
    elif state is ThunkState.RESOLVED:
        where.append(_effectively_resolved(t))
    elif state is ThunkState.EXPIRED:
        where.append(t.c.state == ThunkState.EXPIRED.value)
    if after_id is not None:
        where.append(t.c.id > after_id)
    rows = list(
        await conn.execute(
            select(*thunk_columns(t)).where(*where).order_by(t.c.id).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    page = [thunk_of(r) for r in rows[:limit]]
    e = promissory_ledger.alias("e")
    targets: list[ColumnElement[bool]] = [e.c.thunk_id.in_([p.id for p in page])]
    if after_id is None:
        targets.append(e.c.proposition_id.is_not(None))
    entry_where: list[ColumnElement[bool]] = [
        e.c.story_id == story_id,
        active_only(e),
        or_(*targets),
    ]
    if discharged is True:
        entry_where.append(not_(undischarged_clause(e)))
    elif discharged is False:
        entry_where.append(undischarged_clause(e))
    entries = [
        _entry_of(r)
        for r in await conn.execute(select(*_entry_columns(e)).where(*entry_where).order_by(e.c.id))
    ]
    return Inspection(tuple(page), tuple(entries), page[-1].id if more and page else None)


INSPECT_LIMIT_DEFAULT = 50
INSPECT_LIMIT_MAX = 500


@dataclass(frozen=True, slots=True)
class LedgerPage:
    """``Engine.inspect_ledger``'s answer: the inspection and the story's revision it read."""

    inspection: Inspection
    revision: int
    names: Mapping[str, str]
    """Entity and predicate names by id, for the page's thunks (their sketches name ids)."""
    levels: Mapping[str, str] = field(default_factory=dict)
    """#154: each resolved thunk's resolution level (FR-COMMIT-9), by thunk id."""
    rejected: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    """#154: the bindings of each thunk reconciliation rejected in the timeline's turns
    (``turn_report.commit.rejected_bindings``: ids only), by thunk id."""


async def inspect_ledger(
    db: AsyncEngine,
    story_id: str,
    *,
    state: ThunkState | None = None,
    discharged: bool | None = None,
    limit: int = INSPECT_LIMIT_DEFAULT,
    after_id: str | None = None,
) -> LedgerPage:
    """The story's open thunks and its ledger (FR-COMMIT-13's inspection; docs/02 §9's
    ``inspect/ledger``), one consistent read: 404 for an unknown story, 409 ``module_off`` when
    the story's configuration does not run commitment (a Lite story keeps no ledger)."""
    limit = max(1, min(INSPECT_LIMIT_MAX, limit))
    async with db.connect() as conn:
        revision = await check_expected_revision(conn, story_id, None)
        if not await commitment_on(conn, story_id):
            raise ModuleOff(
                "the story's configuration does not run commitment, so it keeps no ledger",
                {"story_id": story_id, "module": COMMITMENT_MODULE},
            )
        found = await inspect(
            conn, story_id, state=state, discharged=discharged, limit=limit, after_id=after_id
        )
        ids = sorted(
            {i for t in found.thunks for i in (t.sketch.subject_id, t.sketch.object_id) if i}
        )
        names = {
            str(r.id): str(r.canonical_name)
            for r in await conn.execute(
                select(entities.c.id, entities.c.canonical_name).where(
                    entities.c.story_id == story_id, entities.c.id.in_(ids)
                )
            )
        }
        names.update(await predicate_names(conn, [t.sketch.predicate_id for t in found.thunks]))
        levels, rejected = await _resolutions(conn, story_id, found.thunks)
    return LedgerPage(found, revision, names, levels, rejected)


async def _resolutions(
    conn: AsyncConnection, story_id: str, page: Sequence[Thunk]
) -> tuple[dict[str, str], dict[str, tuple[Mapping[str, Any], ...]]]:
    """The page's resolution levels and rejected bindings (#154; see ``LedgerPage``)."""
    resolved = {t.resolved_fact_id: t.id for t in page if t.open is False and t.resolved_fact_id}
    levels: dict[str, str] = {}
    if resolved:
        rows = await conn.execute(
            select(facts.c.id, facts.c.resolution_level).where(
                facts.c.story_id == story_id, facts.c.id.in_(list(resolved))
            )
        )
        for r in rows:
            levels[resolved[str(r.id)]] = r.resolution_level or "consequential"
    ids = {t.id for t in page}
    rejected: dict[str, list[Mapping[str, Any]]] = {}
    if ids:
        o = operations.alias("o")
        found = await conn.scalars(
            select(o.c.turn_report["commit"]["rejected_bindings"])
            .where(
                o.c.story_id == story_id,
                active_operation(o),
                o.c.turn_report["commit"]["rejected_bindings"].is_not(None),
            )
            .order_by(o.c.narrative_turn, o.c.operation_id)
        )
        for items in found:
            for item in items or ():
                if isinstance(item, dict) and item.get("thunk_id") in ids:
                    rejected.setdefault(str(item["thunk_id"]), []).append(item)
    return levels, {k: tuple(v) for k, v in rejected.items()}


async def predicate_names(conn: AsyncConnection, ids: Sequence[str]) -> dict[str, str]:
    """The registered names of predicates ``ids``."""
    if not ids:
        return {}
    rows = await conn.execute(
        select(predicates.c.id, predicates.c.name).where(predicates.c.id.in_(sorted(set(ids))))
    )
    return {str(r.id): str(r.name) for r in rows}


__all__ = [
    "BEFORE_RESOLVE",
    "COMMITMENT_MODULE",
    "DEPENDENCE_CHANNELS",
    "INSPECT_LIMIT_DEFAULT",
    "INSPECT_LIMIT_MAX",
    "LEDGER_DISCHARGED",
    "LEDGER_ENTRY",
    "MINIMAL_COMMIT",
    "OCCURRED_SUFFIX",
    "THUNK_INTRODUCED",
    "THUNK_RESOLVED",
    "TRANSITIONS",
    "BeforeResolve",
    "Channel",
    "CommitReport",
    "Dependence",
    "FollowUp",
    "Inspection",
    "LedgerEntry",
    "LedgerPage",
    "LedgerUnsettled",
    "Thunk",
    "binding_of",
    "blockers",
    "close_turn",
    "commitment_enabled",
    "commitment_on",
    "demote_pending",
    "depend",
    "discharge",
    "discharge_hidden",
    "entries_of",
    "find_open",
    "inspect",
    "inspect_ledger",
    "introduce",
    "make_salient",
    "max_open",
    "minimal_commit",
    "open_clause",
    "predicate_names",
    "read_thunk",
    "reference_hidden",
    "report",
    "resolve_on",
    "settle_fact",
    "thunk_columns",
    "thunk_of",
    "transition",
    "undischarged_clause",
]
