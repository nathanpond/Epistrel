"""Late settlement: an outcome that arrives after its turn closed (FR-EXT-21, FR-STATE-14,
FR-STORE-31, FR-BELIEF-5; docs/02 §4.8; #217).

A late outcome comes from the outbox dispatcher (an ``asynchronous`` provider, or a
``synchronous`` row whose staged-turn wait expired, #212) or from the consumer through ``ingest
{kind: outcome}`` (``core.ingest``; a #209 consumer-applied effect names its ``effect_id``). It
is handled with separate coordinates:

- **the outcome record** (``record_operation``): an ``outbox_outcomes`` row (``core.outbox``) and
  an ``input`` ``outcome_recorded`` event, written by an ``inter_turn`` operation with **no
  narrative position** that bumps no revision: timeline-independent (a fork copies it, a
  truncation never discards it), so no rewind removes it. Its commit enqueues ``settle``
  (``enqueue``) when the record decides something (a first ``confirmed`` or ``failed``).
- **the requesting operation**, a link only: the outbox row's operation, or the
  ``requested_effect`` event's.
- **the settlement** (``settle``): an ``inter_turn`` operation through #193's slot opener
  (``core.world_clock.slots.open_inter_turn``), positioned at commit, under the story lock, in
  the next slot ``N⁺k`` after the last completed turn ``N`` (its active variant). It takes no
  turn slot, so with an ordinary turn generating it commits in the slot before that turn, and
  its revision bump makes the turn rebase or regenerate when its read set is touched (FR-CONC-4).
  While a staged turn is incomplete (``turn_stage`` not ``completed``/``abandoned``, #212) it
  waits (``WAITING``; the job comes back after ``WAIT``) and lands after it, as it does while the
  story is paused, archived or blocked by an erasure.
- **the valid-time**: the clock immediately before the slot commits (#193's
  ``clock_before_slot``: the last turn's end plus earlier slots' advances); a settlement never
  advances the clock and is never backdated.
- **knowledge arrival** (``_exposures``): with ``character_knowledge.lazy_resolution`` on, one
  exposure per in-world perceiver and settled effect at ``exposure.slot_position(N, v, k)``
  (its step 1: the witness rule, after the slot's own event),
  of the effect's ``state_value(state, scope) = new value`` proposition, sourced at the
  settlement's slot-positioned ``external_outcome`` event (never the position-less
  ``outcome_recorded``, which ``core.antecedents`` orders before everything), its source the
  State's holder (what the witness saw happen to them; none for a story State). Perceivers are
  routed by #161's perception over the scene as of turn ``N``'s end (a narration at the State
  holder's place; the player's character has no partition). No salience pass, no belief and no
  memory row at a slot (owner decision 2026-10-28): a belief forms only by a later lazy
  resolution through ``transition()``. The player learns through the next response's
  ``settled_effects``.

**The settlement's work**, in its one commit:

1. **Automatic settlement needs an active origin**: a requesting operation that left the active
   timeline lists a confirmed outbox effect ``confirmed_discarded_origin`` (#211's
   ``reconciliation_marks``) and nothing changes in the story. **Nor an expired hold** (#218,
   FR-EXT-22 (c)): a confirmation whose reservation expired and was released is not converted;
   it is re-verified (``reverify_in``) and listed ``late_success_after_expiry`` with the result,
   and nothing changes (no operation, the outcome record unsettled).
2. **Re-verification and settlement**: #216's ``contingency.settle`` at the slot's position
   converts the effect's conditional and held rows into new committed rows there (or releases
   and drops them on a failure); each new row passes the State's bounds and integrity checks
   against current state (#185, #188, as #189's checks run at commit), and #214's
   ``rebase.revalidate_settlement`` re-authorizes each render's grant under the current
   configuration (as the outcome step's settlement does). Any refusal is a
   ``settlement_conflict``: the whole settlement is withdrawn (nothing is committed, the
   operation ends ``abandoned``) and the outbox effect is listed with its confirmed outcome.
3. **The outcome at the slot**: #215's ``settle_value`` stages the ``external_outcome`` event at
   the slot (its post-effect version from the confirmation's ``resulting_values``, else one
   post-effect read, else ``Unknown``), or ``stage_outcome`` when the effect's value was never
   read; ``timeline_revision + 1``.
4. The outcome record is ``settled`` and the outbox row's ``outcome_event_id`` names the slot's
   ``external_outcome`` event.

An outcome the staged turn's own outcome step already recorded (#212: answered within the wait)
is settled there: ``settle`` only marks it. A truncation, swap or fork that excludes the
settlement's slot drops it but not the outcome record, and the outbox effect is listed
``confirmed_unsettled`` (``core.outbox``, derived: a swap back clears it).

**Explicit actions** (#218, FR-EXT-22), for an effect listed ``late_success_after_expiry``,
``confirmed_discarded_origin`` or ``settlement_conflict``:

- ``settle_now`` re-verifies the transfer at that moment (``reverify_in``: the item still the
  seller's and held by no one else, canon naming no other owner, the State bounds; #189's
  consent and co-location are a beat's checks and a slot has none). A failure is 409
  ``settle_refused`` and nothing changes; otherwise ``settle(..., explicit=)`` applies it at the
  next slot (``contingency.settle``'s explicit path: the origin's own rows, a discarded
  origin's and a released hold included) and resolves the listing in the same commit.
- ``compensate`` writes a compensating effect (#187's shape, on a write-capable provider's
  State) as a new ``intended`` outbox row (``compensates``) on a new inter-turn slot operation
  and enqueues its dispatch; the listing shows ``compensated`` until that row is confirmed.

Log lines carry ids, codes and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, states, world_clock
from epistrel.core import outbox as core_outbox
from epistrel.core import providers as core_providers
from epistrel.core.canon import facts as canon_facts
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.enums import (
    UNFINISHED_TURN_STAGES,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
    StateChangeKind,
    StateChangeStatus,
    StateOwner,
    StateScope,
)
from epistrel.core.epistemic import exposure, partitions, perception
from epistrel.core.epistemic import scene as scenes
from epistrel.core.errors import (
    EpistrelError,
    InvalidRequest,
    ProviderReadOnly,
    SettleRefused,
    StateUnpermitted,
    StoryNotFound,
)
from epistrel.core.events.operation import Operation, Withdrawn
from epistrel.core.events.types import (
    EXTERNAL_OUTCOME,
    OUTCOME_RECORDED,
    RECONCILIATION_RESOLVED,
    REQUESTED_EFFECT,
    OutcomeRecorded,
    ReconciliationResolved,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.membership import Position, active_only, active_operation
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.propositions.model import Polarity
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.propositions.store import write_proposition
from epistrel.core.propositions.vocabulary import OWNS
from epistrel.core.states import contingency
from epistrel.core.states import model as state_model
from epistrel.core.states import verify as state_verify
from epistrel.core.states.model import (
    NUMERIC_TYPES,
    StateDefinition,
    Undefined,
    UndefinedType,
    coerce,
    from_json,
    to_json,
)
from epistrel.core.stories import require_writable
from epistrel.core.tables import events, operations, stories
from epistrel.core.tables.outbox import (
    CONFIRMED,
    FAILED,
    INTENDED,
    VIA_INGEST,
    external_effect_outbox,
)
from epistrel.core.tables.states import state_changes, state_definitions
from epistrel.core.timeline import boundary_label
from epistrel.core.world_clock.slots import open_inter_turn, slot_position
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import WriteOutcome

logger = logging.getLogger(__name__)

SETTLE = "outcome_settlement"
"""The job kind (one per outcome record; coalesce key ``settle:<outcome id>``)."""
WAIT = timedelta(seconds=5)
"""How long a waiting settlement (an incomplete staged turn, a paused story) sleeps."""
WAIT_MAX = timedelta(minutes=10)
"""#655: the longest a settlement the story's state refuses (paused, archived, erasure-blocked)
sleeps between tries: its waits double from ``WAIT`` up to this."""
MOVED_RETRIES = 3
"""How often a settlement whose predicted slot another slot took first is rebuilt."""

SETTLED = "settled"
WAITING = "waiting"
CONFLICTED = "settlement_conflict"
DISCARDED = "confirmed_discarded_origin"
LATE = "late_success_after_expiry"
NOTHING = "nothing"
"""``settle``'s results: committed at a slot (or already settled), waiting, refused by
re-verification, its origin discarded, a confirmation after its reservation expired (#218:
listed, nothing changes), or nothing to settle (no such record, not a first terminal answer,
no known effect)."""
SETTLEABLE: tuple[str, ...] = (LATE, DISCARDED, CONFLICTED)
"""#218: the listings the explicit actions (settle-now, compensate) serve."""
QUEUED = "queued"
"""#218: a settle-now re-verified now whose slot waits (an incomplete staged turn): its job
applies it at the next inter-turn slot."""

DECIDING = frozenset({CONFIRMED, FAILED})
"""An outcome record's kinds a settlement decides: a first terminal answer (FR-EXT-22)."""


# -- the outcome record (intake) -----------------------------------------------------------------


def record_operation(
    db: AsyncEngine | AsyncConnection,
    story_id: str,
    *,
    via: str,
    principal: str = SYSTEM_PRINCIPAL,
    operation_id: str | None = None,
) -> Operation:
    """The position-less operation that records a late outcome (not begun): an
    ``inter_turn`` operation of no slot and no revision bump (FR-EXT-21 (b); never refused by a
    pause or an archive: an outcome is recorded on arrival, FR-CONC-6). The caller begins it,
    calls ``stage_recorded`` and adds the commit step that writes the record (``core.outbox``)
    and ``enqueue``s the settlement."""
    return Operation(
        db,
        story_id=story_id,
        kind=OperationKind.INTER_TURN,
        principal=principal,
        provenance=Provenance.CONSUMER if via == VIA_INGEST else Provenance.SYSTEM,
        bumps_revision=False,
        origin="ingested" if via == VIA_INGEST else "generated",
        operation_id=operation_id,
    )


def stage_recorded(
    op: Operation,
    *,
    outcome_id: str,
    status: str,
    via: str,
    outbox_id: str | None = None,
    effect_id: str | None = None,
) -> None:
    """Stage the ``outcome_recorded`` event (ids and codes only)."""
    op.append(
        OUTCOME_RECORDED,
        OutcomeRecorded(
            outcome_id=outcome_id,
            outbox_id=outbox_id,
            effect_id=effect_id,
            status=cast(Any, status),
            received_via=cast(Any, via),
        ),
        character_id=None,
    )


def coalesce_key(outcome_id: str) -> str:
    return f"settle:{outcome_id}"


class SettlePayload(BaseModel):
    """An ``outcome_settlement`` job: the outcome record to settle (#218: ``explicit``, the
    author's settle-now waiting for its slot, with who asked)."""

    model_config = ConfigDict(extra="forbid")

    outcome_id: str = Field(pattern=ULID_PATTERN)
    explicit: str | None = Field(default=None, exclude_if=lambda v: v is None)
    waits: int = Field(default=0, ge=0, exclude_if=lambda v: v == 0)
    """#655: how many times in a row the story's state refused it (its backoff)."""


def wait_after(waits: int) -> timedelta:
    """#655: how long a settlement the story's state refused ``waits`` times before sleeps:
    ``WAIT`` doubled per earlier refusal, at most ``WAIT_MAX``."""
    doubled: timedelta = WAIT * (1 << min(waits, 16))
    return min(doubled, WAIT_MAX)


async def enqueue(
    conn: AsyncConnection,
    story_id: str,
    outcome_id: str,
    *,
    after: timedelta | None = None,
    explicit: str | None = None,
    waits: int = 0,
) -> str:
    """Enqueue the settlement of ``outcome_id`` in ``conn``'s transaction (once per record;
    ``explicit``: the principal of the author's settle-now, #218; ``waits``: #655's count of
    refusals so far)."""
    # Imported here: ``jobs.queue`` imports the handler registry, which imports this module.
    from epistrel.orchestrator.jobs import queue

    return await queue.enqueue(
        conn,
        SETTLE,
        SettlePayload(outcome_id=outcome_id, explicit=explicit, waits=waits),
        story_id=story_id,
        coalesce_key=coalesce_key(outcome_id),
        due_at=None if after is None else clock.now() + after,
    )


def decides(kind: str) -> bool:
    """Whether an outcome record of ``kind`` is one a settlement decides."""
    return kind in DECIDING


# -- what settle reads ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Origin:
    """The requesting operation of an effect, and what the effect changes (its State by name,
    its scope key; ``None`` for a row with none)."""

    operation_id: str
    state: str | None
    scope_key: str | None
    provider: str | None


async def origin_of(conn: AsyncConnection, record: core_outbox.OutcomeRecord) -> Origin | None:
    """The requesting operation of ``record``'s effect: the outbox row's, or (a consumer-applied
    effect) its ``requested_effect`` event's; ``None`` when the story knows no such effect."""
    if record.outbox_id is not None:
        o = external_effect_outbox.c
        row = (
            await conn.execute(
                select(o.operation_id, o.effect, o.scope_key, o.provider_name).where(
                    o.id == record.outbox_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        effect = dict(row.effect or {})
        return Origin(
            str(row.operation_id),
            None if effect.get("state") is None else str(effect["state"]),
            None if row.scope_key is None else str(row.scope_key),
            str(row.provider_name),
        )
    found = await requested_event(conn, record.story_id, record.effect)
    if found is None:
        return None
    payload = dict(found.payload or {})
    return Origin(
        str(found.operation_id),
        None if payload.get("state") is None else str(payload["state"]),
        None if payload.get("scope_key") is None else str(payload["scope_key"]),
        None,
    )


async def requested_event(conn: AsyncConnection, story_id: str, effect_id: str) -> Any:
    """The ``requested_effect`` event (#209) whose ``effect_id`` is ``effect_id``, or ``None``."""
    e = events.c
    return (
        await conn.execute(
            select(e.id, e.operation_id, e.payload)
            .where(
                e.story_id == story_id,
                e.type == REQUESTED_EFFECT,
                e.payload["effect_id"].astext == effect_id,
            )
            .order_by(e.seq)
            .limit(1)
        )
    ).one_or_none()


async def unfinished_staged(conn: AsyncConnection, story_id: str) -> bool:
    """Whether the story has an incomplete staged turn on its active timeline (#212)."""
    o = operations.c
    found = await conn.scalar(
        select(o.operation_id)
        .where(
            o.story_id == story_id,
            o.kind == OperationKind.TURN.value,
            o.status == OperationStatus.COMMITTED.value,
            o.turn_stage.in_([s.value for s in UNFINISHED_TURN_STAGES]),
            active_operation(operations),
        )
        .limit(1)
    )
    return found is not None


async def _origin_active(conn: AsyncConnection, operation_id: str) -> bool:
    return await core_outbox.origin_active(conn, operation_id)


async def _at_outcome_step(conn: AsyncConnection, origin: Origin, effect: str) -> bool:
    """Whether the requesting staged turn's own outcome step recorded this effect's answer
    (#212: answered within the wait)."""
    e = events.c
    found = await conn.scalar(
        select(e.id)
        .where(
            e.operation_id == origin.operation_id,
            e.type == EXTERNAL_OUTCOME,
            e.payload["outbox_id"].astext == effect,
        )
        .limit(1)
    )
    return found is not None


async def next_slot(conn: AsyncConnection, story_id: str) -> Position:
    """The slot the next inter-turn operation will take (``Operation._slot_position``'s rule):
    after the last completed turn ``N`` (its active variant), the next free ``k``."""
    next_turn = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    turn = int(next_turn or 1) - 1
    o = operations.c
    variant = 0
    if turn >= 1:
        active = await conn.scalar(
            select(func.max(o.turn_variant)).where(
                o.story_id == story_id,
                o.narrative_turn == turn,
                o.kind == OperationKind.TURN.value,
                o.status == OperationStatus.COMMITTED.value,
                o.membership == Membership.ACTIVE.value,
            )
        )
        variant = int(active or 0)
    last = await conn.scalar(
        select(func.coalesce(func.max(o.slot_k), 0)).where(
            o.story_id == story_id,
            o.narrative_turn == turn,
            o.slot_k.is_not(None),
            o.status == OperationStatus.COMMITTED.value,
        )
    )
    return Position(turn, variant, int(last or 0) + 1, 0, 0)


# -- settle ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class Settlement:
    """What ``settle`` did: its result, and for a committed settlement the slot operation, its
    position and valid-time (the clock's offset before the slot; ``None`` with the clock off),
    the contingent rows it decided (#216's ``Settled``) and the exposures it wrote."""

    result: str
    operation_id: str | None = None
    position: Position | None = None
    valid_time: int | None = None
    settled: list[contingency.Settled] = field(default_factory=list)
    exposures: int = 0
    blocked: bool = False
    """#655: a ``WAITING`` the story's state caused (paused, archived, erasure-blocked)."""


class _Moved(Withdrawn):
    """Another slot took the predicted one first: rebuild."""


class _Wait(Withdrawn):
    """A staged turn became incomplete meanwhile."""


class _Done(Withdrawn):
    """Settled meanwhile (another worker)."""


class _Discarded(Withdrawn):
    """The origin left the active timeline meanwhile."""


class _Conflict(Withdrawn):
    """Re-verification refused a settled change (``settlement_conflict``)."""


@dataclass(frozen=True, slots=True)
class Explicit:
    """#218: an explicit settlement (the author's settle-now): who asked, under which key."""

    principal: str
    idempotency_key: str | None = None


async def settle(
    db: AsyncEngine,
    outcome_id: str,
    *,
    registry: InProcessRegistry | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    explicit: Explicit | None = None,
) -> Settlement:
    """Settle outcome record ``outcome_id`` at the next inter-turn slot (see the module
    docstring). ``explicit`` (#218): the author's settle-now, which settles a discarded
    origin's effect and an expired hold too (``contingency.settle``'s explicit path) and
    resolves the effect's listing in the same commit."""
    for _ in range(MOVED_RETRIES):
        try:
            return await _settle_once(
                db, outcome_id, registry=registry, transport=transport, explicit=explicit
            )
        except _Moved:
            continue
    return Settlement(WAITING)


async def _settle_once(
    db: AsyncEngine,
    outcome_id: str,
    *,
    registry: InProcessRegistry | None,
    transport: httpx2.AsyncBaseTransport | None,
    explicit: Explicit | None,
) -> Settlement:
    async with db.begin() as conn:
        record = await core_outbox.outcome(conn, outcome_id)
        if record is None or record.settled or not decides(record.kind):
            return Settlement(SETTLED if record is not None and record.settled else NOTHING)
        origin = await origin_of(conn, record)
        if origin is None:
            return Settlement(NOTHING)
        story_id = record.story_id
        if await _at_outcome_step(conn, origin, record.effect):
            await core_outbox.mark_settled(conn, outcome_id, outbox_id=None, event_id=None)
            return Settlement(SETTLED)
        if await unfinished_staged(conn, story_id):
            return Settlement(WAITING)
        if explicit is None and not await _origin_active(conn, origin.operation_id):
            await _discarded(conn, record)
            return Settlement(DISCARDED)
        if explicit is None and await _expired(conn, record):
            await _late(conn, record)
            return Settlement(LATE)
        predicted = await next_slot(conn, story_id)
        definitions = {d.name: d for d in await states.definitions(conn, story_id)}
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER
        if explicit is not None or record.received_via == VIA_INGEST
        else Provenance.SYSTEM,
        principal=SYSTEM_PRINCIPAL if explicit is None else explicit.principal,
        idempotency_key=None if explicit is None else explicit.idempotency_key,
    )
    done = Settlement(SETTLED, operation_id=op.operation_id, position=predicted)
    try:
        await op.begin()
    except EpistrelError as refused:
        # A paused, archived or erasure-blocked story: settled once it takes work again.
        logger.info("settlement %s waits: %s", outcome_id, refused.code)
        return Settlement(WAITING, blocked=True)
    try:
        await _stage_outcome(
            db, op, record, origin, definitions.get(origin.state or ""), predicted, registry
        )

        async def step(conn: AsyncConnection) -> None:
            await _commit_step(conn, op, record, origin, predicted, settings, done, explicit)

        op.add_commit_step(step)
        await op.commit()
    except _Moved:
        raise
    except _Wait:
        return Settlement(WAITING)
    except _Done:
        return Settlement(SETTLED)
    except _Discarded:
        async with db.begin() as conn:
            await _discarded(conn, record)
        return Settlement(DISCARDED)
    except _Conflict:
        if explicit is not None:
            # #218: the author's settle-now refused at its commit: nothing changes, the effect
            # stays listed as it was.
            logger.info("settlement %s (explicit) refused by re-verification", outcome_id)
            return Settlement(CONFLICTED)
        async with db.begin() as conn:
            if record.kind == CONFIRMED:
                await _mark(conn, record, CONFLICTED)
        logger.info("settlement %s refused by re-verification", outcome_id)
        return Settlement(CONFLICTED)
    finally:
        if op.in_flight:
            await op.abandon()
    logger.info(
        "settlement %s at %s+%s: %d decided, %d exposures",
        outcome_id,
        predicted.turn,
        predicted.slot_k,
        len(done.settled),
        done.exposures,
    )
    return done


async def _mark(
    conn: AsyncConnection,
    record: core_outbox.OutcomeRecord,
    reason: str,
    detail: Any = None,
) -> None:
    """List ``record``'s effect with ``reason``: its outbox row, or (#661) the consumer-applied
    effect's request."""
    if record.outbox_id is not None:
        await core_outbox.mark(conn, record.story_id, record.outbox_id, reason, detail=detail)
    else:
        await core_outbox.mark(
            conn, record.story_id, None, reason, detail=detail, request_id=record.effect
        )


async def _expired(conn: AsyncConnection, record: core_outbox.OutcomeRecord) -> bool:
    """#218 (FR-EXT-22 (c)): a confirmation for an external effect (an outbox row, #661: or a
    consumer-applied one) whose reservation expired and was released (#216's
    ``reservation_expiry``) while nothing still waits on it."""
    if record.kind != CONFIRMED:
        return False
    c = state_changes.c
    waiting = await conn.scalar(
        select(c.id)
        .where(
            c.story_id == record.story_id,
            c.status.in_(sorted(contingency.WAITING)),
            active_only(state_changes),
            c.prerequisite_effect_ids.overlap([record.effect]),
        )
        .limit(1)
    )
    if waiting is not None:
        return False
    return bool(await core_outbox.expired_holds(conn, record.story_id, [record.effect]))


async def _late(conn: AsyncConnection, record: core_outbox.OutcomeRecord) -> None:
    """#218: list a late success with its re-verification against current state; nothing in
    the story changes and the outcome record stays unsettled until the author acts."""
    checked = await reverify_in(conn, record.story_id, record.effect)
    await _mark(conn, record, LATE, checked.model_dump(mode="json"))
    logger.info("settlement %s: late success after expiry (%s)", record.id, checked.reverification)


async def _discarded(conn: AsyncConnection, record: core_outbox.OutcomeRecord) -> None:
    """FR-EXT-21: a confirmation whose origin was discarded is listed; nothing else changes
    (#661: a consumer-applied effect is listed by its request id)."""
    if record.kind == CONFIRMED:
        await _mark(conn, record, DISCARDED)


async def _stage_outcome(
    db: AsyncEngine,
    op: Operation,
    record: core_outbox.OutcomeRecord,
    origin: Origin,
    definition: StateDefinition | None,
    position: Position,
    registry: InProcessRegistry | None,
) -> None:
    """The slot's ``external_outcome`` event, with #215's post-effect version of the value."""
    story_id = record.story_id
    answer = WriteOutcome(
        status=cast(Any, record.kind),
        raw_hash=record.raw_hash,
        resulting_values=record.payload.get("resulting_values"),
    )
    outcome = pinned.Outcome(record.effect, record.effect, answer, record.received_at)
    target = None
    provider = None
    if definition is not None and definition.provider_id is not None:
        target = await pinned.outcome_target(db, story_id, definition, origin.scope_key, position)
        async with db.connect() as conn:
            provider = await core_providers.find_provider(conn, definition.provider_id)
    if target is None or provider is None:
        pinned.stage_outcome(op, outcome, position)
        return
    reads = pinned.PinnedReads(db=db, story_id=story_id, in_process=registry or InProcessRegistry())
    await pinned.settle_value(op, reads, target, outcome, provider)


async def _commit_step(
    conn: AsyncConnection,
    op: Operation,
    record: core_outbox.OutcomeRecord,
    origin: Origin,
    predicted: Position,
    settings: Any,
    done: Settlement,
    explicit: Explicit | None = None,
) -> None:
    story_id = record.story_id
    position = slot_position(op)
    if position != predicted:
        raise _Moved
    if await unfinished_staged(conn, story_id):
        raise _Wait
    locked = await core_outbox.outcome(conn, record.id, lock=True)
    if locked is None or locked.settled:
        raise _Done
    if explicit is None and not await _origin_active(conn, origin.operation_id):
        raise _Discarded
    # here: ``structured`` imports the job handlers, which import this module
    from epistrel.orchestrator.structured import rebase

    savepoint = await conn.begin_nested()
    # #214 (FR-CONC-16): the same settlement check as the outcome step's, so a grant #188 no
    # longer admits under the current configuration is refused here too (a conflict); #218: an
    # explicit settle-now goes through it as well.
    decided = await contingency.settle(
        conn,
        op,
        {record.effect: record.kind},
        position,
        check=rebase.revalidate_settlement,
        operation_id=None if explicit is None else origin.operation_id,
    )
    if any(s.result == contingency.CONFLICT for s in decided):
        await savepoint.rollback()
        raise _Conflict
    await savepoint.commit()
    if explicit is not None and record.outbox_id is not None:
        # #218: the listing the author settled is resolved with the settlement (with #211's
        # ``reconciliation_required`` of a discarded origin: the effect is settled now).
        await core_outbox.resolve(conn, story_id, record.outbox_id, core_outbox.RESOLVED_SETTLE_NOW)
    done.settled = decided
    event_id = await conn.scalar(
        select(events.c.id).where(
            events.c.operation_id == op.operation_id, events.c.type == EXTERNAL_OUTCOME
        )
    )
    if exposure.lazy_on(settings) and event_id is not None:
        done.exposures = await _exposures(conn, op, decided, str(event_id), position)
    if await world_clock.clock_level(conn, story_id) is not None:
        before = await world_clock.clock_before_slot(conn, story_id, position.turn, position.slot_k)
        done.valid_time = before.offset_minutes
    await core_outbox.mark_settled(
        conn,
        record.id,
        outbox_id=record.outbox_id,
        event_id=None if event_id is None else str(event_id),
    )


async def _exposures(
    conn: AsyncConnection,
    op: Operation,
    decided: Sequence[contingency.Settled],
    event_id: str,
    position: Position,
) -> int:
    """Knowledge arrival (FR-EXT-21 (e), FR-BELIEF-5): one exposure per in-world perceiver and
    settled effect, of its ``state_value`` proposition, at the slot (see the module
    docstring)."""
    story_id = op.story_id
    assert story_id is not None  # noqa: S101 - a settlement belongs to a story
    converted = [s for s in decided if s.result == contingency.CONVERTED and s.settlement_id]
    if not converted:
        return 0
    by_name = {d.name: d for d in await states.definitions(conn, story_id)}
    c = state_changes.c
    values = {
        str(r.id): r.new_value
        for r in await conn.execute(
            select(c.id, c.new_value).where(c.id.in_([str(s.settlement_id) for s in converted]))
        )
    }
    scene = await scenes.load_scene_state(conn, story_id, position.turn + 1)
    level = await scenes.current_level(conn, story_id)
    registry = await load_registry(conn, story_id)
    exposed: list[exposure.Exposed] = []
    # The slot's exposure position, at step 1: #162's witness rule (a witness is exposed at the
    # step after what it perceived), so the slot's own event, at step 0, strictly precedes it
    # and a lazy resolution may cite it (FR-EPI-14's order rule).
    at = replace(exposure.slot_position(position.turn, position.variant, position.slot_k), step=1)
    for item in converted:
        definition = by_name.get(item.state)
        if definition is None or definition.id is None:
            continue
        value = from_json(definition.type, values.get(str(item.settlement_id)))
        if value is None:
            continue
        holder = item.scope_key
        beat = perception.RouteBeat(
            kind="narration",
            beat=0,
            actor=None,
            qualifiers=perception.Qualifiers(location=scene.at(holder)),
        )
        perceivers = sorted(
            {
                p.character_id
                for p in perception.route_perception(beat, scene, None, level)
                if p.fidelity == perception.CONTENT
            }
        )
        subject = state_verify.TrackedState.of(definition).subject(holder)
        if subject is None:
            continue
        proposition = state_verify.proposition(
            str(definition.id), subject, state_verify.value_literal(value)
        )
        # Whose change it is: the character the State belongs to (what a witness saw happen to
        # them); none for a story State.
        source = holder if definition.scope is StateScope.CHARACTER else None
        stored: str | None = None
        for witness in perceivers:
            if await partitions.is_player(conn, story_id, witness):
                continue
            if stored is None:
                stored = await write_proposition(
                    conn, story_id, proposition, registry, base_revision=op.base_revision
                )
            exposed.append(
                exposure.Exposed(
                    witness=witness,
                    proposition_id=stored,
                    proposition=proposition,
                    source=source,
                    source_event_id=event_id,
                    position=at,
                )
            )
    return len(await exposure.record_exposures(conn, op, exposed))


# -- explicit reconciliation actions (#218, FR-EXT-22) ---------------------------------------------


AVAILABILITY = "precondition:availability"
OWNERSHIP = "precondition:ownership"
BOUNDS = "bounds:"
"""Re-verification's reasons: the item is no longer the seller's to give (its value no longer
covers the decrease, or another hold has it), canon says another party owns it (#133's
``owns``), or a State bound the change would now break (``bounds:<constraint>``)."""
PASSED: Literal["passed"] = "passed"
FAILED_CHECK: Literal["failed"] = "failed"


async def _intent_rows(conn: AsyncConnection, story_id: str, outbox_id: str) -> list[Any]:
    """The requesting operation's contingent rows that wait (or waited) on outbox row
    ``outbox_id``: ``held``, ``pending`` or ``released``, whatever their membership."""
    o = external_effect_outbox.c
    origin = await conn.scalar(
        select(o.operation_id).where(o.id == outbox_id, o.story_id == story_id)
    )
    if origin is None:
        # #661: a consumer-applied effect's requesting operation is its request's.
        found = await requested_event(conn, story_id, outbox_id)
        origin = None if found is None else found.operation_id
    if origin is None:
        return []
    c = state_changes.c
    return list(
        await conn.execute(
            select(state_changes)
            .where(
                c.story_id == story_id,
                c.operation_id == origin,
                c.status.in_(sorted(contingency.EXPLICIT)),
                c.prerequisite_effect_ids.overlap([outbox_id]),
            )
            .order_by(c.narrative_turn, c.slot_k, c.beat, c.step, c.id)
        )
    )


async def _held_by_another(conn: AsyncConnection, story_id: str, item: str, ours: set[str]) -> bool:
    """Whether an active hold other than ``ours`` keeps ``item`` (#216's reservation)."""
    c, d = state_changes.c, state_definitions.c
    found = await conn.scalar(
        select(c.id)
        .select_from(state_changes.join(state_definitions, d.id == c.state_id))
        .where(
            c.story_id == story_id,
            c.status == StateChangeStatus.HELD.value,
            active_only(state_changes),
            d.entity_id == item,
            c.delta < 0,
            c.id.not_in(sorted(ours) or [""]),
        )
        .limit(1)
    )
    return found is not None


async def _owners(conn: AsyncConnection, story_id: str, item: str) -> set[str]:
    """Who canon says owns ``item`` now (#133's ``owns``, the open-ended affirmed versions)."""
    found = await canon_facts.canon(conn, story_id, predicate=OWNS, object=item)
    return {
        f.subject_id
        for f in found
        if f.valid_to is None and f.proposition.polarity is Polarity.AFFIRMED
    }


async def reverify_in(
    conn: AsyncConnection, story_id: str, outbox_id: str
) -> core_outbox.Reverification:
    """#218 (FR-EXT-22 (c)): re-verify outbox effect ``outbox_id``'s transfer against current
    state, read in ``conn``: for each contingent row its requesting turn wrote (#216), whether
    the item is still available to it (the seller's value still covers the decrease, no other
    hold keeps it: #189's ``availability``), whether canon names another owner (#189's
    ``ownership``), and whether the change keeps the State's bounds (#185). #189's consent and
    co-location are a beat's checks: a slot has no beat, so they are not re-run (as #217's
    settlement re-verification). Codes only."""
    rows = await _intent_rows(conn, story_id, outbox_id)
    by_id = {d.id: d for d in await states.definitions(conn, story_id)}
    ours = {str(r.id) for r in rows}
    reasons: list[str] = []
    for row in rows:
        definition = by_id.get(str(row.state_id))
        if definition is None:
            continue
        scope_key = None if row.scope_key is None else str(row.scope_key)
        found = await states.value_of(conn, story_id, definition.name, scope_key)
        current: Any = Undefined if isinstance(found, UndefinedType) else found.value
        delta = None if row.delta is None else Decimal(row.delta)
        value = None if row.value is None else from_json(definition.type, row.value)
        attempted = state_model.step(current, StateChangeKind(row.kind), value, delta)
        item = definition.entity_id
        if item is not None and delta is not None and delta < 0:
            covered = isinstance(current, Decimal) and current + delta >= 0
            if not covered or await _held_by_another(conn, story_id, item, ours):
                reasons.append(AVAILABILITY)
            owners = await _owners(conn, story_id, item)
            if owners and scope_key not in owners:
                reasons.append(OWNERSHIP)
            if not covered:
                continue
        broken = state_model.violation(definition, attempted)
        if broken is not None:
            reasons.append(f"{BOUNDS}{broken}")
    found_reasons = list(dict.fromkeys(reasons))
    return core_outbox.Reverification(
        reverification=FAILED_CHECK if found_reasons else PASSED, reasons=found_reasons
    )


async def reverify(db: AsyncEngine, story_id: str, outbox_id: str) -> core_outbox.Reverification:
    """``reverify_in`` in a connection of its own."""
    async with db.connect() as conn:
        return await reverify_in(conn, story_id, outbox_id)


class NoListing(StoryNotFound):
    """The story lists no such effect for an explicit action (404 ``not_found``, ``reason:
    no_listing``): an unknown outbox row, or one not listed ``late_success_after_expiry``,
    ``confirmed_discarded_origin`` or ``settlement_conflict`` (or, for compensate and resolve,
    ``imported_unsettled``, #251)."""


@dataclass(frozen=True, slots=True)
class SettledNow:
    """An author's settle-now: the effect, its confirmed outcome record, the re-verification
    at that moment, whether it is ``settled`` (at ``position``, ``N⁺k``; ``slot`` the full
    position, by ``operation_id``) or ``queued`` (its slot waits for an incomplete staged turn),
    and the story's revision after it."""

    outbox_id: str
    outcome_id: str
    result: str
    reverification: str
    reasons: list[str]
    timeline_revision: int
    operation_id: str | None = None
    position: str | None = None
    slot: dict[str, int] | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class Compensated:
    """An author's compensate: the new outbox row (``intended``, dispatched like any other)
    that compensates ``compensates``, its slot and operation, and the story's revision."""

    outbox_id: str
    compensates: str
    status: str
    position: str
    slot: dict[str, int]
    operation_id: str
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


async def _listed_for_action(
    conn: AsyncConnection, story_id: str, outbox_id: str, *, parked: bool = False
) -> list[str]:
    """The settleable reasons outbox row ``outbox_id`` is listed with (404 ``no_listing``
    when none). ``parked`` (#251, owner decision 5: ``compensate`` only): an imported row
    parked ``imported_unsettled`` (never executable) with that mark open is listed too."""
    o = external_effect_outbox.c
    found = (
        await conn.execute(
            select(o.id, o.executable, o.status).where(o.id == outbox_id, o.story_id == story_id)
        )
    ).one_or_none()
    marks = [] if found is None else await core_outbox.open_marks(conn, story_id, outbox_id)
    reasons: list[str] = []
    if found is not None and found.executable is True:
        reasons = [r for r in marks if r in SETTLEABLE]
    elif (
        found is not None
        and parked
        and str(found.status) == IMPORTED_UNSETTLED
        and IMPORTED_UNSETTLED in marks
    ):
        reasons = [IMPORTED_UNSETTLED]
    if not reasons:
        raise NoListing(
            f"the story lists no effect {outbox_id} for settle-now or compensate",
            {"story_id": story_id, "outbox_id": outbox_id, "reason": "no_listing"},
        )
    return reasons


async def _revision(db: AsyncEngine, story_id: str) -> int:
    async with db.connect() as conn:
        found = await conn.scalar(
            select(stories.c.timeline_revision).where(stories.c.id == story_id)
        )
    return int(found or 0)


async def settle_now(
    db: AsyncEngine,
    story_id: str,
    outbox_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
    registry: InProcessRegistry | None = None,
) -> SettledNow:
    """#218 (FR-EXT-22): the author's settle-now for a listed effect. It re-verifies the
    transfer now (``reverify_in``); a failure is 409 ``settle_refused {reasons}`` and nothing
    changes. Otherwise #217's ``settle`` applies it at the next inter-turn slot (``source =
    settlement``), with the effect's listing resolved in the same commit; a refusal at that
    commit (the State bounds, #217's re-verification) is ``settle_refused`` too. While a staged
    turn is incomplete the settlement is queued for its slot."""
    await require_writable(db, story_id)
    async with db.connect() as conn:
        await states.require_module(conn, story_id)
        await _listed_for_action(conn, story_id, outbox_id)
        outcome_id = await core_outbox.settled_outcome(conn, story_id, outbox_id)
        kind = await core_outbox.settled_kind(conn, story_id, outbox_id)
        if outcome_id is None or kind != CONFIRMED:
            raise SettleRefused(
                f"effect {outbox_id} has no confirmed outcome to settle",
                {"outbox_id": outbox_id, "reasons": ["not_confirmed"]},
            )
        checked = await reverify_in(conn, story_id, outbox_id)
    if checked.reverification != PASSED:
        raise SettleRefused(
            f"effect {outbox_id} fails re-verification",
            {"outbox_id": outbox_id, "reasons": list(checked.reasons)},
        )
    done = await settle(
        db,
        outcome_id,
        registry=registry,
        explicit=Explicit(principal, idempotency_key),
    )
    if done.result == CONFLICTED:
        raise SettleRefused(
            f"effect {outbox_id} fails re-verification at its slot",
            {"outbox_id": outbox_id, "reasons": [CONFLICTED]},
        )
    if done.result == WAITING:
        async with db.begin() as conn:
            await enqueue(conn, story_id, outcome_id, after=WAIT, explicit=principal)
        return SettledNow(
            outbox_id=outbox_id,
            outcome_id=outcome_id,
            result=QUEUED,
            reverification=checked.reverification,
            reasons=list(checked.reasons),
            timeline_revision=await _revision(db, story_id),
        )
    position = done.position
    return SettledNow(
        outbox_id=outbox_id,
        outcome_id=outcome_id,
        result=SETTLED,
        reverification=checked.reverification,
        reasons=list(checked.reasons),
        timeline_revision=await _revision(db, story_id),
        operation_id=done.operation_id,
        position=None if position is None else boundary_label(position.turn, position.slot_k),
        slot=None if position is None else position.document(),
    )


async def compensate(
    db: AsyncEngine,
    story_id: str,
    outbox_id: str,
    request: core_outbox.CompensateRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> Compensated:
    """#218 (FR-EXT-22): the author's compensate for a listed effect: a compensating external
    effect (a refund) written as a new ``intended`` outbox row naming it (``compensates``) on a
    new inter-turn slot operation (#193's ``open_inter_turn``), and its dispatch enqueued in the
    same commit. The effect is #187's shape on an externally owned State whose provider is
    write-capable (else 422 ``provider_read_only``). The original stays listed, ``status:
    compensated``, until the compensating row is confirmed."""
    from epistrel.orchestrator.jobs.handlers import outbox as dispatcher  # it imports this

    effect = request.effect
    await require_writable(db, story_id)
    async with db.connect() as conn:
        await states.require_module(conn, story_id)
        await _listed_for_action(conn, story_id, outbox_id, parked=True)
        definition = await states.definition(conn, story_id, effect.state)
        if definition.owner is not StateOwner.EXTERNAL or definition.provider_id is None:
            raise StateUnpermitted(
                f"State {effect.state!r} is not owned by an external provider",
                {"state": effect.state, "reason": "not_external"},
            )
        provider = await core_providers.find_provider(conn, definition.provider_id)
        if provider is None or not provider.write_capable:
            raise ProviderReadOnly(
                f"the provider of State {effect.state!r} cannot apply effects",
                {"state": effect.state, "provider": definition.provider_id},
            )
    scope_key = effect.scope_key
    if definition.scope is StateScope.CHARACTER and scope_key is None:
        raise InvalidRequest(
            f"State {effect.state!r} is per character: name the character",
            {"field": "body.effect.scope_key", "reason": "required"},
        )
    if definition.scope is StateScope.STORY and scope_key is not None:
        raise InvalidRequest(
            f"State {effect.state!r} belongs to the story: it takes no scope key",
            {"field": "body.effect.scope_key", "reason": "story_scoped"},
        )
    document: dict[str, Any] = {"kind": "change", "state": effect.state, "reason": effect.reason}
    if effect.delta is not None:
        if definition.type not in NUMERIC_TYPES:
            raise InvalidRequest(
                f"State {effect.state!r} is not a number: give a value",
                {"field": "body.effect.delta", "reason": "not_a_number"},
            )
        document["delta"] = effect.delta
    else:
        document["value"] = to_json(
            coerce(definition.type, effect.value, field="body.effect.value")
        )
    new_id = new_ulid()
    written: list[Position] = []

    async def write(conn: AsyncConnection) -> None:
        position = slot_position(op)
        await core_outbox.write_intended(
            conn,
            op,
            [
                core_outbox.Intended(
                    id=new_id,
                    provider_name=str(definition.provider_id),
                    value_name=definition.external_value_name or definition.name,
                    scope_key=scope_key,
                    effect=document,
                    beat=0,
                    slot_k=position.slot_k,
                    compensates=outbox_id,
                )
            ],
        )
        await dispatcher.enqueue(conn, new_id, story_id)
        written.append(position)

    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER,
        principal=principal,
        idempotency_key=idempotency_key,
    )
    async with op:
        op.add_commit_step(write)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    position = written[0]
    logger.info("compensation %s for %s at %s", new_id, outbox_id, position.slot_k)
    return Compensated(
        outbox_id=new_id,
        compensates=outbox_id,
        status=INTENDED,
        position=boundary_label(position.turn, position.slot_k),
        slot=position.document(),
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
    )


@dataclass(frozen=True, slots=True)
class Resolved:
    """#661: an author's resolve of a listed consumer-applied effect: its request id, the
    resolution, the reasons it was listed under, its slot and operation, and the story's
    revision."""

    request_id: str
    resolution: str
    reasons: list[str]
    position: str
    slot: dict[str, int]
    operation_id: str
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


IMPORTED_UNSETTLED = "imported_unsettled"
"""#251: an imported outbox row parked with no terminal outcome; the resolve action settles
its listing (the row itself is never dispatched)."""


async def _listed_request(conn: AsyncConnection, story_id: str, request_id: str) -> list[str]:
    """The reasons consumer-applied effect ``request_id`` is listed under, or (#251) the
    ``imported_unsettled`` mark of a parked imported outbox row of that id (404 ``no_listing``
    when it is not listed: another outbox row, an unknown id or a resolved one)."""
    listed: list[str] = [
        listing.reason
        for listing in await core_outbox.consumer_listings(conn, story_id)
        if listing.request_id == request_id
    ]
    if not listed and IMPORTED_UNSETTLED in await core_outbox.open_marks(
        conn, story_id, request_id
    ):
        listed = [IMPORTED_UNSETTLED]
    if not listed:
        raise NoListing(
            f"the story lists no consumer-applied effect {request_id} to resolve",
            {"story_id": story_id, "listing": request_id, "reason": "no_listing"},
        )
    return listed


async def resolve_listing(
    db: AsyncEngine,
    story_id: str,
    request_id: str,
    request: core_outbox.ResolveRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> Resolved:
    """#661 (FR-EXT-22): the author's resolve-only action for a listed consumer-applied effect:
    nothing is dispatched (the consumer applied it, or it never will be), so it only closes the
    listing. An inter-turn slot operation (#193's ``open_inter_turn``, like #218's actions)
    resolves its open marks with the ``resolution`` (a derived ``reservation_expired`` listing
    gets a resolved mark), recording when and the operation (#777: the outbox list's
    ``resolution`` of a parked ``imported_unsettled`` row), and stages a
    ``reconciliation_resolved`` audit event; it writes no outcome record and calls no
    provider."""
    await require_writable(db, story_id)
    async with db.connect() as conn:
        await states.require_module(conn, story_id)
        reasons = await _listed_request(conn, story_id, request_id)
    written: list[Position] = []

    async def step(conn: AsyncConnection) -> None:
        # Still listed under the story lock (a concurrent resolve answers 404).
        await _listed_request(conn, story_id, request_id)
        if reasons == [IMPORTED_UNSETTLED]:
            # #251: a parked imported row's mark; the row stays parked, never dispatched.
            await core_outbox.resolve(
                conn,
                story_id,
                request_id,
                request.resolution,
                reasons=[IMPORTED_UNSETTLED],
                operation_id=op.operation_id,
            )
        else:
            await core_outbox.resolve(
                conn,
                story_id,
                request_id,
                request.resolution,
                request=True,
                operation_id=op.operation_id,
            )
        if "reservation_expired" in reasons:
            await core_outbox.mark(
                conn,
                story_id,
                None,
                "reservation_expired",
                request_id=request_id,
                resolved_by=request.resolution,
                resolved_operation_id=op.operation_id,
            )
        written.append(slot_position(op))

    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER,
        principal=principal,
        idempotency_key=idempotency_key,
    )
    async with op:
        op.append(
            RECONCILIATION_RESOLVED,
            ReconciliationResolved(
                request_id=request_id, resolution=request.resolution, reasons=list(reasons)
            ),
            provenance=Provenance.CONSUMER,
        )
        op.add_commit_step(step)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    position = written[0]
    logger.info("listing %s resolved (%s)", request_id, request.resolution)
    return Resolved(
        request_id=request_id,
        resolution=request.resolution,
        reasons=list(reasons),
        position=boundary_label(position.turn, position.slot_k),
        slot=position.document(),
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
    )


# -- the job --------------------------------------------------------------------------------------


async def _run(ctx: JobContext) -> JobOutput:
    from epistrel.orchestrator.jobs.handlers import outbox as dispatcher  # here: it imports this

    payload = cast(SettlePayload, ctx.payload)
    explicit = None if payload.explicit is None else Explicit(payload.explicit)
    found = await settle(ctx.db, payload.outcome_id, registry=dispatcher.bound(), explicit=explicit)
    if found.result == WAITING:
        # #655: a story that refuses work backs off (doubling, capped); an incomplete staged
        # turn's wait ends with the turn, so it is tried again soon.
        waits = payload.waits + 1 if found.blocked else 0
        async with ctx.db.begin() as conn:
            record = await core_outbox.outcome(conn, payload.outcome_id)
            if record is not None:
                await enqueue(
                    conn,
                    record.story_id,
                    payload.outcome_id,
                    after=wait_after(payload.waits) if found.blocked else WAIT,
                    explicit=payload.explicit,
                    waits=waits,
                )
    return JobOutput(result={"outcome": found.result})


SETTLE_HANDLER = JobHandler(kind=SETTLE, payload=SettlePayload, run=_run, records_operation=False)


def new_outcome_id() -> str:
    """A fresh outcome record id (the ``outcome_recorded`` event names it before the record is
    written in the same commit)."""
    return new_ulid()


__all__ = [
    "AVAILABILITY",
    "BOUNDS",
    "CONFLICTED",
    "DECIDING",
    "DISCARDED",
    "LATE",
    "NOTHING",
    "OWNERSHIP",
    "QUEUED",
    "SETTLE",
    "SETTLEABLE",
    "SETTLED",
    "SETTLE_HANDLER",
    "WAIT",
    "WAITING",
    "WAIT_MAX",
    "Compensated",
    "Explicit",
    "NoListing",
    "Origin",
    "Resolved",
    "SettlePayload",
    "SettledNow",
    "Settlement",
    "coalesce_key",
    "compensate",
    "decides",
    "enqueue",
    "new_outcome_id",
    "next_slot",
    "origin_of",
    "record_operation",
    "requested_event",
    "resolve_listing",
    "reverify",
    "reverify_in",
    "settle",
    "settle_now",
    "stage_recorded",
    "unfinished_staged",
    "wait_after",
]
