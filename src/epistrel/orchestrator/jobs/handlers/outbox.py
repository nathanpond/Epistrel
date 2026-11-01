"""The outbox dispatcher (FR-EXT-10,12,16..18, NFR-REL-5; docs/02 §4.8; #210).

A turn writes each effect on a value a write-capable provider owns as an ``intended``
``external_effect_outbox`` row and, in the same transaction, enqueues one ``outbox_dispatch``
job for it (``coalesce_key`` ``outbox:<id>``; the enqueue's NOTIFY wakes a worker). The job:

1. ``claim`` — one transaction: the row ``FOR UPDATE SKIP LOCKED`` (``core.outbox.claimable``),
   then its operation's ``membership`` re-read in the same transaction. No longer ``active``
   (``discarded``, ``dormant``, ``superseded``): ``cancelled``, never sent. A provider no
   longer registered or no longer write-capable (#212, FR-CONC-16: the dispatcher re-reads the
   registration at claim), or an effect #188's authorization no longer admits under the current
   configuration (#214, ``orchestrator.structured.rebase.revalidate_claim``: its State's
   sources, or its State gone), cancels the row too, listed ``reconciliation_required`` (a
   staged turn's outcome step narrates the attempt as not carried out). A story
   ``blocked_by_erasure`` (#125), an ``in_process`` provider with no write callable in this
   process (#204) or a story with no binding to it leave the row ``intended`` (the recovery
   sweep tries again). Otherwise the row
   is ``claimed`` with a lease (``CLAIM_LEASE``) and an ``outbox_lease`` timer is set for the
   lease's end; the commit is the in-flight point (FR-EXT-12).
2. ``send`` — after that commit, the adapter's ``write`` with the row id as the idempotency key
   (``Effect.idempotency_key``; FR-EXT-18). Only the adapter's answer decides ``confirmed`` or
   ``failed`` (the manifest's ``outcome_map``, or the in-process ``WriteOutcome.status``); a
   timeout, a transport error, a non-2xx answer or an unmapped value is ``unknown`` (#204's
   adapters; ``outcome_status`` here), and anything else the adapter raises once the request
   may have left is ``unknown`` too. ``ProviderUnavailable`` means nothing was sent: the claim
   is released (``intended`` again).
3. The outcome is recorded in its own transaction (``core.outbox.record_outcome``): an
   ``outbox_outcomes`` row and the row's status. First the row is re-read ``FOR UPDATE``: when
   an erasure redacted it (``erased_at``) or a story-scope erasure was applied after the claim
   (#211; the erasure ledger, never ``timeline_revision``), the dispatch is **abandoned**: the
   row becomes ``unknown`` with an outcome record that holds no payload, stays listed
   redacted, and is never sent again (the lease timer is cancelled, and ``expire`` refuses
   it). A terminal outcome cancels the lease timer; an ``unknown`` one keeps it for a provider
   that deduplicates (re-sent at the lease's end) and cancels it for one that does not (the row
   stays ``unknown``, listed, never re-sent).

``outbox_lease`` (the lease timer, ``expire``): a row still ``claimed`` (or ``unknown``) when
its lease lapses is re-sent under the same key when the provider deduplicates (at most
``MAX_ATTEMPTS`` sends), or moved to ``unknown`` and listed when it does not (FR-EXT-18). The
timer is a row and the move is one transaction, so it happens once across worker restarts.

``outbox_recovery`` (every ``RECOVERY_INTERVAL``): enqueues a dispatch job for every ``intended``
executable row with no live job (a notification lost, a job that ended without claiming), and
(#213) resumes every staged turn whose slot lease lapsed short of its completion
(``orchestrator.structured.staged.resume_stalled``).

The dispatcher bumps no revision: the outcome is a record on the row (timeline-independent),
written with #217's position-less ``outcome_recorded`` event by an operation of no slot
(``orchestrator.jobs.settlement.record_operation``), whose commit enqueues the settlement of a
first ``confirmed`` or ``failed`` answer at the next inter-turn slot (FR-EXT-21). The in-process
callables are the process's ``Engine``'s: ``Engine.start_workers`` binds its registry here
(``bind``); with none bound an ``in_process`` row stays ``intended``.

Log lines carry ids, kinds and counts only (invariant 3).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock
from epistrel.core import outbox as core_outbox
from epistrel.core import providers as core_providers
from epistrel.core.errors import EpistrelError
from epistrel.core.events.operation import Withdrawn
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.tables import jobs
from epistrel.core.tables.outbox import (
    CLAIMED,
    INTENDED,
    UNKNOWN,
    VIA_DISPATCHER,
    external_effect_outbox,
)
from epistrel.core.timeline_effects import RECONCILIATION_REQUIRED
from epistrel.orchestrator.jobs import queue, settlement, timers
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import (
    OUTCOMES,
    Binding,
    Effect,
    ExternalProvider,
    ProviderUnavailable,
    WriteOutcome,
)
from epistrel.orchestrator.providers.registry import provider_for
from epistrel.orchestrator.structured import rebase
from epistrel.testing import crash

logger = logging.getLogger(__name__)

OUTBOX_DISPATCH = "outbox_dispatch"
OUTBOX_LEASE = "outbox_lease"
OUTBOX_RECOVERY = "outbox_recovery"
CLAIM_LEASE = timedelta(seconds=60)
"""How long a claim holds before its lease timer fires (the planner's default)."""
RECOVERY_INTERVAL = timedelta(seconds=60)
MAX_ATTEMPTS = 5
"""The most sends of one row to a deduplicating provider; after that an ``unknown`` row stays
listed and is not re-sent again."""
RECOVERY_LIMIT = 500
"""The most rows one recovery run enqueues (the next run takes the rest)."""


def coalesce_key(outbox_id: str) -> str:
    """The dispatch job's coalesce key for a row."""
    return f"outbox:{outbox_id}"


class DispatchPayload(BaseModel):
    """An ``outbox_dispatch`` job: the row to dispatch."""

    model_config = ConfigDict(extra="forbid")

    outbox_id: str = Field(pattern=ULID_PATTERN)


class LeasePayload(BaseModel):
    """An ``outbox_lease`` timer's job: ``target`` is the row id."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=ULID_PATTERN)


_bound: list[InProcessRegistry] = []


def bind(registry: InProcessRegistry | None) -> None:
    """Make ``registry`` (the process's ``Engine.in_process_providers``) the one the dispatcher
    resolves in-process providers with; ``None`` unbinds."""
    _bound.clear()
    if registry is not None:
        _bound.append(registry)


def bound() -> InProcessRegistry | None:
    return _bound[0] if _bound else None


def _registry(registry: InProcessRegistry | None) -> InProcessRegistry:
    return registry if registry is not None else (bound() or InProcessRegistry())


# -- the outcome mapping ---------------------------------------------------------------------


def outcome_status(outcome: WriteOutcome | Any) -> str:
    """The outbox status an adapter's answer decides: its ``WriteOutcome.status`` when that is
    ``confirmed``, ``failed`` or ``unknown``; anything else is ``unknown`` (FR-EXT-16)."""
    status = getattr(outcome, "status", None)
    return str(status) if status in OUTCOMES else UNKNOWN


def outcome_payload(outcome: WriteOutcome) -> dict[str, Any]:
    """What an outcome record keeps of an answer: its resulting values, as JSON."""
    if not outcome.resulting_values:
        return {}
    return {"resulting_values": json.loads(json.dumps(dict(outcome.resulting_values), default=str))}


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def effect_of(row: Any) -> Effect:
    """The port's ``Effect`` for an outbox row: its value, delta or new value, scope key, and
    the row id as the idempotency key."""
    effect: Mapping[str, Any] = row.effect or {}
    value = effect.get("value")
    return Effect(
        value=str(row.value_name),
        idempotency_key=str(row.id),
        delta=_number(effect.get("delta")),
        new_value=value if isinstance(value, bool | str) or value is None else _number(value),
        scope_key=row.scope_key,
    )


# -- claim, send, record -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claimed:
    """A claimed row, ready to send: its id and story, the claim's attempt, the effect, the
    resolved adapter and the story's binding, and whether the provider deduplicates."""

    outbox_id: str
    story_id: str
    attempts: int
    effect: Effect
    provider: ExternalProvider
    binding: Binding
    dedupes: bool


HELD = "held"
SKIPPED = "skipped"
CANCELLED = "cancelled"
RELEASED = "released"
ABANDONED = "abandoned"


async def _resolve(
    conn: Any,
    row: Any,
    registry: InProcessRegistry,
    transport: httpx2.AsyncBaseTransport | None,
) -> tuple[ExternalProvider, Binding, bool] | None:
    """The adapter, binding and dedupe flag a send of ``row`` needs, or ``None`` when it cannot
    be sent from here now (no provider, not write-capable, no in-process callable, no binding)."""
    name = str(row.provider_name)
    found = await core_providers.find_provider(conn, name)
    if found is None or not found.write_capable:
        return None
    if found.transport == "in_process":
        callables = registry.get(name)
        if callables is None or callables.write is None:
            return None
    binding = await core_providers.find_binding(conn, str(row.story_id), name)
    if binding is None:
        return None
    provider = await provider_for(conn, name, in_process=registry, transport=transport)
    return (
        provider,
        Binding(str(row.story_id), name, dict(binding.external_ids)),
        bool(found.dedupes_dispatch),
    )


async def claim(
    db: AsyncEngine,
    outbox_id: str,
    *,
    registry: InProcessRegistry | None = None,
    lease: timedelta = CLAIM_LEASE,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Claimed | str:
    """Claim row ``outbox_id`` (see the module docstring); answers the claim, or why there is
    none: ``skipped`` (not ``intended``, or another worker holds it), ``cancelled`` (its
    operation is no longer active, or its provider no longer write-capable) or ``held`` (left
    ``intended``)."""
    found = _registry(registry)
    async with db.begin() as conn:
        row = await core_outbox.claimable(conn, outbox_id)
        if row is None:
            return SKIPPED
        story_id = str(row.story_id)
        if await core_outbox.story_blocked(conn, story_id):
            return HELD
        # FR-EXT-12: the origin's membership, read in the claim's own transaction.
        if not await core_outbox.origin_active(conn, str(row.operation_id)):
            await core_outbox.cancel(conn, outbox_id)
            logger.info("outbox row %s cancelled: its operation is no longer active", outbox_id)
            return CANCELLED
        refused = await rebase.revalidate_claim(conn, row)
        if refused is not None:
            # #212, #214 (FR-CONC-16): the registration and #188's authorization, re-read at
            # claim under the current configuration, no longer let the effect be sent:
            # cancelled, never sent, and listed for reconciliation.
            await core_outbox.cancel(conn, outbox_id)
            await core_outbox.mark(conn, story_id, outbox_id, RECONCILIATION_REQUIRED)
            logger.info("outbox row %s cancelled at claim: %s", outbox_id, refused)
            return CANCELLED
        resolved = await _resolve(conn, row, found, transport)
        if resolved is None:
            return HELD
        provider, binding, dedupes = resolved
        now = clock.now()
        attempts = await core_outbox.mark_claimed(conn, outbox_id, now, lease)
        await timers.schedule(conn, OUTBOX_LEASE, outbox_id, now + lease, story_id=story_id)
    return Claimed(outbox_id, story_id, attempts, effect_of(row), provider, binding, dedupes)


async def send(db: AsyncEngine, claimed: Claimed) -> str:
    """Send a claimed row and record the answer in its own transaction (see the module
    docstring); answers the row's status after it, or ``released``."""
    started = clock.now()
    try:
        outcome = await claimed.provider.write(claimed.effect, claimed.binding)
    except ProviderUnavailable as refused:
        # Nothing was sent (the adapter refused before any request): the claim is released.
        async with db.begin() as conn:
            if await core_outbox.release(conn, claimed.outbox_id, claimed.attempts):
                await timers.cancel(conn, OUTBOX_LEASE, claimed.outbox_id)
        logger.info("outbox row %s released: %s", claimed.outbox_id, refused.reason)
        return RELEASED
    except Exception as exc:
        # Raised once the request may have left: ambiguous (FR-EXT-16).
        logger.warning("outbox row %s send raised %s", claimed.outbox_id, type(exc).__name__)
        outcome = WriteOutcome(status="unknown")
    # #213: the provider answered, the answer not recorded.
    crash.hit("staged.after_send")
    status = outcome_status(outcome)
    answer = Answered(
        outcome_id=settlement.new_outcome_id(),
        status=status,
        payload=outcome_payload(outcome) if isinstance(outcome, WriteOutcome) else {},
        raw_hash=getattr(outcome, "raw_hash", "") or "",
    )
    # #217 (FR-EXT-21 (b)): the outcome record and its position-less ``outcome_recorded``
    # event commit together, and a deciding answer enqueues its settlement.
    op = settlement.record_operation(db, claimed.story_id, via=VIA_DISPATCHER)
    try:
        await op.begin()
    except EpistrelError:
        # The story takes no work now (an erasure block): the record alone; settled later.
        async with db.begin() as conn:
            return await _record(conn, claimed, started, answer)
    found: list[str] = []

    async def step(conn: Any) -> None:
        if await _erased_meanwhile(conn, claimed):
            raise _Abandoned
        found.append(await _record(conn, claimed, started, answer))

    try:
        settlement.stage_recorded(
            op,
            outcome_id=answer.outcome_id,
            status=status,
            via=VIA_DISPATCHER,
            outbox_id=claimed.outbox_id,
        )
        op.add_commit_step(step)
        await op.commit()
    except _Abandoned:
        # #211 (FR-ADMIN-9, FR-EXT-21): an erasure landed while the dispatch was in
        # flight. Its answer is not recorded; the row is unknown, listed redacted, and
        # never sent again.
        async with db.begin() as conn:
            await core_outbox.mark_sent(conn, claimed.outbox_id, started)
            await core_outbox.abandon(conn, claimed.outbox_id, clock.now())
            await timers.cancel(conn, OUTBOX_LEASE, claimed.outbox_id)
        logger.info("outbox row %s abandoned: erased mid-flight", claimed.outbox_id)
        return ABANDONED
    finally:
        if op.in_flight:
            await op.abandon()
    logger.info("outbox row %s: %s", claimed.outbox_id, found[0])
    return found[0]


@dataclass(frozen=True, slots=True)
class Answered:
    """An adapter's answer as its outcome record keeps it (#217: with the record's id)."""

    outcome_id: str
    status: str
    payload: dict[str, Any]
    raw_hash: str


class _Abandoned(Withdrawn):
    """An erasure reached the row while its dispatch was in flight (#211)."""


async def _record(conn: Any, claimed: Claimed, started: Any, answer: Answered) -> str:
    """Record ``answer`` for the claimed row in ``conn``'s transaction (``record_outcome``),
    settle its lease timer and enqueue its settlement when it decides one (#217); answers the
    row's status."""
    await core_outbox.mark_sent(conn, claimed.outbox_id, started)
    recorded = await core_outbox.record_outcome(
        conn,
        claimed.outbox_id,
        answer.status,
        payload=answer.payload,
        raw_hash=answer.raw_hash,
        now=clock.now(),
        outcome_id=answer.outcome_id,
    )
    if recorded.status != UNKNOWN or not claimed.dedupes or claimed.attempts >= MAX_ATTEMPTS:
        await timers.cancel(conn, OUTBOX_LEASE, claimed.outbox_id)
    if settlement.decides(recorded.kind):
        await settlement.enqueue(conn, claimed.story_id, answer.outcome_id)
    return recorded.status


async def _erased_meanwhile(conn: Any, claimed: Claimed) -> bool:
    """The erasure-specific mid-flight check (#211), on the row locked ``FOR UPDATE``: the
    erasure pass redacted it (``erased_at``), or a story-scope erasure was applied after the
    claim (its ledger row is newer than ``claimed_at``). Never ``timeline_revision``, which
    every ordinary turn moves."""
    row = await core_outbox.locked(conn, claimed.outbox_id)
    if row is None or row.erased_at is not None:
        return True
    return await core_outbox.erased_since(conn, claimed.story_id, row.claimed_at)


async def dispatch(
    db: AsyncEngine,
    outbox_id: str,
    *,
    registry: InProcessRegistry | None = None,
    lease: timedelta = CLAIM_LEASE,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> str:
    """``claim`` then, when claimed, ``send``: the dispatch job's work."""
    claimed = await claim(db, outbox_id, registry=registry, lease=lease, transport=transport)
    if isinstance(claimed, str):
        return claimed
    return await send(db, claimed)


async def expire(
    db: AsyncEngine,
    outbox_id: str,
    *,
    registry: InProcessRegistry | None = None,
    lease: timedelta = CLAIM_LEASE,
    transport: httpx2.AsyncBaseTransport | None = None,
    force: bool = False,
) -> str:
    """The lease timer (see the module docstring): answers what it did, ``resent:<status>``,
    ``unknown``, ``held`` or ``settled`` (nothing to do). ``force`` (#213: a staged turn's
    ``resume``, which took the turn over from a dead process): the claim is treated as lapsed
    whatever its lease says, and only a ``claimed`` row (sent or not, never answered) is
    handled."""
    found = _registry(registry)
    claimed: Claimed | None = None
    async with db.begin() as conn:
        row = await core_outbox.locked(conn, outbox_id)
        if row is None or not row.executable or str(row.status) not in (CLAIMED, UNKNOWN):
            return "settled"
        if force and str(row.status) != CLAIMED:
            return "settled"
        now = clock.now()
        if not force and (row.claim_lease_until is None or row.claim_lease_until > now):
            return "settled"
        if row.erased_at is not None or await core_outbox.erased_since(
            conn, str(row.story_id), row.claimed_at
        ):
            # #211: an erasure reached the row, or landed after its claim: never re-sent.
            if str(row.status) == CLAIMED:
                await core_outbox.abandon(conn, outbox_id, now)
            return UNKNOWN
        provider = await core_providers.find_provider(conn, str(row.provider_name))
        if provider is None or not provider.dedupes_dispatch:
            if str(row.status) == CLAIMED:
                await core_outbox.mark_unknown(conn, outbox_id, now, "lease_expired")
                logger.info("outbox row %s: lease lapsed, unknown", outbox_id)
            return UNKNOWN
        if int(row.attempts) >= MAX_ATTEMPTS:
            if str(row.status) == CLAIMED:
                await core_outbox.mark_unknown(conn, outbox_id, now, "attempts_exhausted")
            return UNKNOWN
        resolved = await _resolve(conn, row, found, transport)
        if resolved is None:
            # Not sendable from here now: try again after another lease.
            await timers.schedule(
                conn, OUTBOX_LEASE, outbox_id, now + lease, story_id=str(row.story_id)
            )
            return HELD
        adapter, binding, dedupes = resolved
        attempts = await core_outbox.mark_claimed(conn, outbox_id, now, lease)
        await timers.schedule(
            conn, OUTBOX_LEASE, outbox_id, now + lease, story_id=str(row.story_id)
        )
        claimed = Claimed(
            outbox_id, str(row.story_id), attempts, effect_of(row), adapter, binding, dedupes
        )
    logger.info("outbox row %s: lease lapsed, re-sent under the same key", outbox_id)
    return f"resent:{await send(db, claimed)}"


async def recover(db: AsyncEngine) -> int:
    """Enqueue a dispatch job for every ``intended`` executable row with no live one; answers
    how many it enqueued."""
    o = external_effect_outbox.c
    live = exists().where(
        and_(
            jobs.c.coalesce_key == func.concat("outbox:", o.id),
            jobs.c.status.in_((queue.QUEUED, queue.RUNNING)),
        )
    )
    async with db.begin() as conn:
        found = list(
            await conn.execute(
                select(o.id, o.story_id)
                .where(o.status == INTENDED, o.executable.is_(True), ~live)
                .order_by(o.created_at, o.id)
                .limit(RECOVERY_LIMIT)
            )
        )
        for row in found:
            await enqueue(conn, str(row.id), str(row.story_id))
    if found:
        logger.info("outbox recovery: %d dispatch job(s) enqueued", len(found))
    return len(found)


async def enqueue(conn: Any, outbox_id: str, story_id: str) -> str:
    """Enqueue the dispatch job of row ``outbox_id`` in ``conn``'s transaction (one per row)."""
    return await queue.enqueue(
        conn,
        OUTBOX_DISPATCH,
        {"outbox_id": outbox_id},
        story_id=story_id,
        coalesce_key=coalesce_key(outbox_id),
    )


# -- the job kinds -------------------------------------------------------------------------------


async def _dispatch(ctx: JobContext) -> JobOutput:
    payload = cast(DispatchPayload, ctx.payload)
    return JobOutput(result={"outcome": await dispatch(ctx.db, payload.outbox_id)})


async def _lease(ctx: JobContext) -> JobOutput:
    payload = cast(LeasePayload, ctx.payload)
    return JobOutput(result={"outcome": await expire(ctx.db, payload.target)})


async def _recover(ctx: JobContext) -> JobOutput:
    enqueued = await recover(ctx.db)
    # #213 (FR-EXT-17): the staged turns whose process died, resumed from their last stage.
    from epistrel.orchestrator.structured import staged  # here: staged imports this module

    resumed = await staged.resume_stalled(ctx.db, registry=bound())
    return JobOutput(result={"enqueued": enqueued, "resumed": resumed})


OUTBOX_DISPATCH_HANDLER = JobHandler(
    kind=OUTBOX_DISPATCH, payload=DispatchPayload, run=_dispatch, records_operation=False
)
OUTBOX_LEASE_HANDLER = JobHandler(
    kind=OUTBOX_LEASE, payload=LeasePayload, run=_lease, records_operation=False
)
OUTBOX_RECOVERY_HANDLER = JobHandler(
    kind=OUTBOX_RECOVERY,
    payload=NoPayload,
    run=recording(_recover),
    max_attempts=1,
    records_operation=False,
)
OUTBOX_RECOVERY_TASK = RecurringTask(OUTBOX_RECOVERY, RECOVERY_INTERVAL)


__all__ = [
    "ABANDONED",
    "CLAIM_LEASE",
    "MAX_ATTEMPTS",
    "OUTBOX_DISPATCH",
    "OUTBOX_DISPATCH_HANDLER",
    "OUTBOX_LEASE",
    "OUTBOX_LEASE_HANDLER",
    "OUTBOX_RECOVERY",
    "OUTBOX_RECOVERY_HANDLER",
    "OUTBOX_RECOVERY_TASK",
    "RECOVERY_INTERVAL",
    "Claimed",
    "DispatchPayload",
    "LeasePayload",
    "bind",
    "bound",
    "claim",
    "coalesce_key",
    "dispatch",
    "effect_of",
    "enqueue",
    "expire",
    "outcome_payload",
    "outcome_status",
    "recover",
    "send",
]
