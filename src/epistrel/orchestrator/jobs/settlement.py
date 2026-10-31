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
   ``reconciliation_marks``; #218 owns settle-now and compensate) and nothing changes in the
   story.
2. **Re-verification and settlement**: #216's ``contingency.settle`` at the slot's position
   converts the effect's conditional and held rows into new committed rows there (or releases
   and drops them on a failure); each new row passes the State's bounds and integrity checks
   against current state (#185, #188, as #189's checks run at commit). Any refusal is a
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

Log lines carry ids, codes and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, cast

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, states, world_clock
from epistrel.core import outbox as core_outbox
from epistrel.core import providers as core_providers
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.enums import (
    UNFINISHED_TURN_STAGES,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
    StateScope,
)
from epistrel.core.epistemic import exposure, partitions, perception
from epistrel.core.epistemic import scene as scenes
from epistrel.core.errors import EpistrelError
from epistrel.core.events.operation import Operation, Withdrawn
from epistrel.core.events.types import (
    EXTERNAL_OUTCOME,
    OUTCOME_RECORDED,
    REQUESTED_EFFECT,
    OutcomeRecorded,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.membership import Position, active_operation
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.propositions.registry import load as load_registry
from epistrel.core.propositions.store import write_proposition
from epistrel.core.states import contingency
from epistrel.core.states import verify as state_verify
from epistrel.core.states.model import StateDefinition, from_json
from epistrel.core.tables import events, operations, stories
from epistrel.core.tables.outbox import (
    CONFIRMED,
    FAILED,
    VIA_INGEST,
    external_effect_outbox,
)
from epistrel.core.tables.states import state_changes
from epistrel.core.world_clock.slots import open_inter_turn, slot_position
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import WriteOutcome

logger = logging.getLogger(__name__)

SETTLE = "outcome_settlement"
"""The job kind (one per outcome record; coalesce key ``settle:<outcome id>``)."""
WAIT = timedelta(seconds=5)
"""How long a waiting settlement (an incomplete staged turn, a paused story) sleeps."""
MOVED_RETRIES = 3
"""How often a settlement whose predicted slot another slot took first is rebuilt."""

SETTLED = "settled"
WAITING = "waiting"
CONFLICTED = "settlement_conflict"
DISCARDED = "confirmed_discarded_origin"
NOTHING = "nothing"
"""``settle``'s results: committed at a slot (or already settled), waiting, refused by
re-verification, its origin discarded, or nothing to settle (no such record, not a first
terminal answer, no known effect)."""

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
    """An ``outcome_settlement`` job: the outcome record to settle."""

    model_config = ConfigDict(extra="forbid")

    outcome_id: str = Field(pattern=ULID_PATTERN)


async def enqueue(
    conn: AsyncConnection, story_id: str, outcome_id: str, *, after: timedelta | None = None
) -> str:
    """Enqueue the settlement of ``outcome_id`` in ``conn``'s transaction (once per record)."""
    return await queue.enqueue(
        conn,
        SETTLE,
        SettlePayload(outcome_id=outcome_id),
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


async def settle(
    db: AsyncEngine,
    outcome_id: str,
    *,
    registry: InProcessRegistry | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Settlement:
    """Settle outcome record ``outcome_id`` at the next inter-turn slot (see the module
    docstring)."""
    for _ in range(MOVED_RETRIES):
        try:
            return await _settle_once(db, outcome_id, registry=registry, transport=transport)
        except _Moved:
            continue
    return Settlement(WAITING)


async def _settle_once(
    db: AsyncEngine,
    outcome_id: str,
    *,
    registry: InProcessRegistry | None,
    transport: httpx2.AsyncBaseTransport | None,
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
        if not await _origin_active(conn, origin.operation_id):
            await _discarded(conn, record)
            return Settlement(DISCARDED)
        predicted = await next_slot(conn, story_id)
        definitions = {d.name: d for d in await states.definitions(conn, story_id)}
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER if record.received_via == VIA_INGEST else Provenance.SYSTEM,
        principal=SYSTEM_PRINCIPAL,
    )
    done = Settlement(SETTLED, operation_id=op.operation_id, position=predicted)
    try:
        await op.begin()
    except EpistrelError as refused:
        # A paused, archived or erasure-blocked story: settled once it takes work again.
        logger.info("settlement %s waits: %s", outcome_id, refused.code)
        return Settlement(WAITING)
    try:
        await _stage_outcome(
            db, op, record, origin, definitions.get(origin.state or ""), predicted, registry
        )

        async def step(conn: AsyncConnection) -> None:
            await _commit_step(conn, op, record, origin, predicted, settings, done)

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
        async with db.begin() as conn:
            if record.outbox_id is not None and record.kind == CONFIRMED:
                await core_outbox.mark(conn, story_id, record.outbox_id, CONFLICTED)
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


async def _discarded(conn: AsyncConnection, record: core_outbox.OutcomeRecord) -> None:
    """FR-EXT-21: a confirmation whose origin was discarded is listed; nothing else changes (a
    consumer-applied effect has no outbox row to list: it stays recorded, unsettled)."""
    if record.kind == CONFIRMED and record.outbox_id is not None:
        await core_outbox.mark(conn, record.story_id, record.outbox_id, DISCARDED)


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
    if not await _origin_active(conn, origin.operation_id):
        raise _Discarded
    savepoint = await conn.begin_nested()
    decided = await contingency.settle(conn, op, {record.effect: record.kind}, position)
    if any(s.result == contingency.CONFLICT for s in decided):
        await savepoint.rollback()
        raise _Conflict
    await savepoint.commit()
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


# -- the job --------------------------------------------------------------------------------------


async def _run(ctx: JobContext) -> JobOutput:
    from epistrel.orchestrator.jobs.handlers import outbox as dispatcher  # here: it imports this

    payload = cast(SettlePayload, ctx.payload)
    found = await settle(ctx.db, payload.outcome_id, registry=dispatcher.bound())
    if found.result == WAITING:
        async with ctx.db.begin() as conn:
            record = await core_outbox.outcome(conn, payload.outcome_id)
            if record is not None:
                await enqueue(conn, record.story_id, payload.outcome_id, after=WAIT)
    return JobOutput(result={"outcome": found.result})


SETTLE_HANDLER = JobHandler(kind=SETTLE, payload=SettlePayload, run=_run, records_operation=False)


def new_outcome_id() -> str:
    """A fresh outcome record id (the ``outcome_recorded`` event names it before the record is
    written in the same commit)."""
    return new_ulid()


__all__ = [
    "CONFLICTED",
    "DECIDING",
    "DISCARDED",
    "NOTHING",
    "SETTLE",
    "SETTLED",
    "SETTLE_HANDLER",
    "WAIT",
    "WAITING",
    "Origin",
    "SettlePayload",
    "Settlement",
    "coalesce_key",
    "decides",
    "enqueue",
    "new_outcome_id",
    "next_slot",
    "origin_of",
    "record_operation",
    "requested_event",
    "settle",
    "stage_recorded",
    "unfinished_staged",
]
