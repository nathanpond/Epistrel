"""Truncate a story to a checkpoint and undo it (FR-STORE-10,11,18; docs/02 §4.6; #65).

- **Truncate** to turn N is a ``control`` operation whose only mutation is timeline
  membership: every **active** committed operation positioned after N — any kind, so a Lite
  summary segment positioned at its ``covers_to`` > N goes too (#72 rebuilds), as do turn N's
  own inter-turn slots (none in M2) — becomes ``discarded`` with ``membership_tx`` = the
  truncation's transaction time. Events, rows and valid-times are untouched; events inherit
  membership from their operation, so the update is O(operations after N). Operations that are
  already ``discarded`` (a stacked truncation) or ``dormant`` keep their state and stamp.
  ``stories.next_turn_index`` becomes N + 1 and the revision moves (FR-CONC-1). The truncation
  is a control action: a turn generating meanwhile abandons with 409 ``timeline_changed``
  (#47), and turns waiting in the story's queue fail with the same error in the truncation's
  transaction. ``to_turn`` must be from 0 to the head - 1 (422 otherwise). The event records
  ``{to_turn, prior_head, operation_ids}``.
- **Untruncate** reverts the most recent truncation (LIFO): the newest ``truncate`` whose
  discarded operations are all still ``discarded``. With no active turn positioned after its
  ``to_turn`` it reactivates exactly those operations (``membership_tx`` re-stamped) and
  restores the head; with newer turns over the tail it is a swap, which is 409
  ``swap_required`` (``options: ["swap", "fork"]``) until #66. No undoable truncation, or a
  ``truncate_operation_id`` that is not the most recent one, is 409 ``nothing_to_undo``. The
  event records ``{truncate_operation_id, operation_ids, restored_head}``.
- **Undo state** (``undo_state``, read by ``GET /stories/{id}``): ``undo_available`` whenever
  an undo (a reactivation or a swap) exists, and ``dormant_chains``: the discarded tails that
  have newer turns over them.

Both edits commit against current state: the payload staged from the read phase is
recomputed under the story lock, and if the timeline moved meanwhile the same operation
re-reads and restages (up to ``EDIT_RETRY_BUDGET`` times, then 409 ``revision_conflict``).

**Membership hooks.** ``on_membership_change(hook, phase=...)`` registers a coroutine
``hook(conn, change)`` run for every membership change these edits make: ``in_transaction``
hooks run inside the commit (a failure rolls the edit back); ``post_commit`` hooks run after it
on their own connection, and a failure is logged (no content) and never undoes the edit. #72
registers the Lite summary rebuild here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import turn_gate
from epistrel.core.concurrency import (
    Regenerate,
    RevalidationRequired,
    RevalidationState,
    attempt_record,
    check_expected_revision,
)
from epistrel.core.enums import Membership, OperationKind, OperationStatus, Provenance
from epistrel.core.errors import (
    InvalidRequest,
    NothingToUndo,
    RevisionConflict,
    StoryNotFound,
    SwapRequired,
    TimelineChanged,
)
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import Head, Truncate, Untruncate
from epistrel.core.stories import require_writable
from epistrel.core.tables import events, operations, stories

logger = logging.getLogger(__name__)

EDIT_RETRY_BUDGET = 3
"""How many times a truncate or untruncate re-reads after the timeline moved under it."""

SWAP_OPTIONS: tuple[str, ...] = ("swap", "fork")
"""What ``swap_required`` offers instead of a reactivation (#66 swap, #67 fork)."""


# -- requests and results ---------------------------------------------------------------------


class TruncateRequest(BaseModel):
    """``POST /stories/{id}/truncate``: keep turns 1..``to_turn``; ``0`` keeps only genesis."""

    model_config = ConfigDict(extra="forbid")

    to_turn: int = Field(description="The last turn to keep (0 to the head - 1).")


class UntruncateRequest(BaseModel):
    """``POST /stories/{id}/untruncate``: optionally name the truncation being reverted; it
    must be the most recent one."""

    model_config = ConfigDict(extra="forbid")

    truncate_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class Truncation:
    """A committed truncation: its operation, the story's revision and next turn index after
    it, and how many operations it discarded."""

    operation_id: str
    timeline_revision: int
    next_turn_index: int
    to_turn: int
    discarded_operations: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class Untruncation:
    """A committed untruncation: the truncation it reverted and how many operations it
    reactivated."""

    operation_id: str
    timeline_revision: int
    next_turn_index: int
    truncate_operation_id: str
    restored_operations: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class DormantChain:
    """A discarded tail with newer turns written over it: reachable by a swap (#66)."""

    truncate_operation_id: str
    from_turn: int
    through_turn: int
    operations: int


@dataclass(frozen=True, slots=True)
class UndoState:
    undo_available: bool
    dormant_chains: tuple[DormantChain, ...] = ()


# -- membership hooks -------------------------------------------------------------------------


class HookPhase(StrEnum):
    IN_TRANSACTION = "in_transaction"
    POST_COMMIT = "post_commit"


@dataclass(frozen=True, slots=True)
class MembershipChange:
    """Operations whose membership an edit changed, from ``old`` to ``new``, and why."""

    story_id: str
    operation_ids: tuple[str, ...]
    old: Membership
    new: Membership
    cause: str
    """The control operation's event type (``truncate``, ``untruncate``)."""
    control_operation_id: str


MembershipHook = Callable[[AsyncConnection, MembershipChange], Awaitable[None]]

_HOOKS: dict[HookPhase, list[MembershipHook]] = {phase: [] for phase in HookPhase}


def on_membership_change(
    hook: MembershipHook, *, phase: HookPhase = HookPhase.IN_TRANSACTION
) -> MembershipHook:
    """Register ``hook`` for every membership change (see the module docstring)."""
    if hook not in _HOOKS[phase]:
        _HOOKS[phase].append(hook)
    return hook


def remove_membership_hook(hook: MembershipHook) -> None:
    for hooks in _HOOKS.values():
        if hook in hooks:
            hooks.remove(hook)


async def _run_in_transaction(conn: AsyncConnection, change: MembershipChange) -> None:
    for hook in list(_HOOKS[HookPhase.IN_TRANSACTION]):
        await hook(conn, change)


async def _run_post_commit(db: AsyncEngine, change: MembershipChange) -> None:
    for hook in list(_HOOKS[HookPhase.POST_COMMIT]):
        try:
            async with db.connect() as conn:
                await hook(conn, change)
                if conn.in_transaction():
                    await conn.commit()
        except Exception as exc:
            logger.warning(
                "membership hook %s failed after %s %s: %s",
                getattr(hook, "__qualname__", "hook"),
                change.cause,
                change.control_operation_id,
                type(exc).__name__,
            )


# -- reads ------------------------------------------------------------------------------------


def _committed(story_id: str) -> Any:
    return and_(
        operations.c.story_id == story_id,
        operations.c.status == OperationStatus.COMMITTED.value,
    )


def _after(to_turn: int) -> Any:
    """Positioned after ``to_turn``: later turns, and ``to_turn``'s own inter-turn slots."""
    return or_(
        operations.c.narrative_turn > to_turn,
        and_(operations.c.narrative_turn == to_turn, operations.c.slot_k.is_not(None)),
    )


async def _head(conn: AsyncConnection, story_id: str) -> Head:
    found = await conn.execute(
        select(operations.c.narrative_turn, operations.c.turn_variant)
        .where(
            _committed(story_id),
            operations.c.kind == OperationKind.TURN.value,
            operations.c.membership == Membership.ACTIVE.value,
        )
        .order_by(operations.c.narrative_turn)
    )
    variants = {str(r.narrative_turn): int(r.turn_variant) for r in found}
    highest = max((int(t) for t in variants), default=0)
    return Head(next_turn_index=highest + 1, active_variants=variants)


async def _next_turn_index(conn: AsyncConnection, story_id: str) -> int:
    found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    if found is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return int(found)


@dataclass(frozen=True, slots=True)
class _Undoable:
    truncate_operation_id: str
    to_turn: int
    operation_ids: tuple[str, ...]


async def _undoable(conn: AsyncConnection, story_id: str) -> list[_Undoable]:
    """Every committed truncation whose discarded operations are all still ``discarded``,
    newest first (the LIFO stack of undoable truncations)."""
    found = await conn.execute(
        select(events.c.operation_id, events.c.payload)
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id,
            events.c.type == "truncate",
            operations.c.status == OperationStatus.COMMITTED.value,
        )
        .order_by(events.c.seq.desc())
    )
    truncations = [
        _Undoable(
            row.operation_id, int(row.payload["to_turn"]), tuple(row.payload["operation_ids"])
        )
        for row in found
    ]
    named = {op for t in truncations for op in t.operation_ids}
    state: dict[str, str] = {}
    if named:
        rows = await conn.execute(
            select(operations.c.operation_id, operations.c.membership).where(
                operations.c.operation_id.in_(sorted(named))
            )
        )
        state = {r.operation_id: r.membership for r in rows}
    return [
        t
        for t in truncations
        if t.operation_ids
        and all(state.get(op) == Membership.DISCARDED.value for op in t.operation_ids)
    ]


async def _newer_turns(conn: AsyncConnection, story_id: str, to_turn: int) -> int:
    """How many active turns are positioned after ``to_turn``."""
    found = await conn.scalar(
        select(func.count()).where(
            _committed(story_id),
            operations.c.kind == OperationKind.TURN.value,
            operations.c.membership == Membership.ACTIVE.value,
            operations.c.narrative_turn > to_turn,
        )
    )
    return int(found or 0)


async def undo_state(db: AsyncEngine | AsyncConnection, story_id: str) -> UndoState:
    """``undo_available`` and ``dormant_chains`` for ``GET /stories/{id}``."""
    if isinstance(db, AsyncConnection):
        return await _undo_state(db, story_id)
    async with db.connect() as conn:
        return await _undo_state(conn, story_id)


async def _undo_state(conn: AsyncConnection, story_id: str) -> UndoState:
    stack = await _undoable(conn, story_id)
    chains: list[DormantChain] = []
    for truncation in stack:
        if not await _newer_turns(conn, story_id, truncation.to_turn):
            continue
        through = await conn.scalar(
            select(func.max(operations.c.narrative_turn)).where(
                operations.c.operation_id.in_(truncation.operation_ids)
            )
        )
        chains.append(
            DormantChain(
                truncate_operation_id=truncation.truncate_operation_id,
                from_turn=truncation.to_turn + 1,
                through_turn=int(through if through is not None else truncation.to_turn + 1),
                operations=len(truncation.operation_ids),
            )
        )
    return UndoState(undo_available=bool(stack), dormant_chains=tuple(chains))


# -- plans ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TruncatePlan:
    payload: Truncate


@dataclass(frozen=True, slots=True)
class _UntruncatePlan:
    payload: Untruncate


async def _plan_truncate(conn: AsyncConnection, story_id: str, to_turn: int) -> _TruncatePlan:
    head = await _next_turn_index(conn, story_id) - 1
    if to_turn < 0 or to_turn >= head:
        raise InvalidRequest(
            "to_turn must be a turn before the story's head (0 keeps only the story's setup)",
            {"field": "body.to_turn", "head": head},
        )
    found = await conn.scalars(
        select(operations.c.operation_id)
        .where(
            _committed(story_id),
            operations.c.membership == Membership.ACTIVE.value,
            _after(to_turn),
        )
        .order_by(operations.c.narrative_turn, operations.c.operation_id)
    )
    return _TruncatePlan(
        Truncate(to_turn=to_turn, prior_head=await _head(conn, story_id), operation_ids=list(found))
    )


async def _plan_untruncate(
    conn: AsyncConnection, story_id: str, named: str | None
) -> _UntruncatePlan:
    stack = await _undoable(conn, story_id)
    if not stack or (named is not None and named != stack[0].truncate_operation_id):
        details: dict[str, Any] = {"story_id": story_id}
        if named is not None:
            details["truncate_operation_id"] = named
        message = (
            "there is no truncation to undo"
            if not stack
            else "only the most recent truncation can be undone"
        )
        raise NothingToUndo(message, details)
    latest = stack[0]
    if await _newer_turns(conn, story_id, latest.to_turn):
        raise SwapRequired(
            "newer turns were written after the truncation; undoing it is a swap",
            {
                "truncate_operation_id": latest.truncate_operation_id,
                "to_turn": latest.to_turn,
                "options": list(SWAP_OPTIONS),
            },
        )
    head = await _head(conn, story_id)
    restored = dict(head.active_variants)
    found = await conn.execute(
        select(operations.c.narrative_turn, operations.c.turn_variant).where(
            operations.c.operation_id.in_(latest.operation_ids),
            operations.c.kind == OperationKind.TURN.value,
        )
    )
    for row in found:
        restored[str(row.narrative_turn)] = int(row.turn_variant)
    ordered = dict(sorted(restored.items(), key=lambda item: int(item[0])))
    highest = max((int(t) for t in ordered), default=0)
    return _UntruncatePlan(
        Untruncate(
            truncate_operation_id=latest.truncate_operation_id,
            operation_ids=list(latest.operation_ids),
            restored_head=Head(next_turn_index=highest + 1, active_variants=ordered),
        )
    )


# -- the edits --------------------------------------------------------------------------------


async def _flip(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str], membership: Membership
) -> None:
    if operation_ids:
        await conn.execute(
            update(operations)
            .where(operations.c.operation_id.in_(list(operation_ids)))
            .values(membership=membership.value, membership_tx=func.transaction_timestamp())
        )


def _moved(op: Operation, revision: int) -> RevalidationRequired:
    """The signal that the plan staged from the read phase is no longer the plan under the
    lock: the commit rolls back and the same operation re-reads."""
    state = RevalidationState(
        attempt=len(op.attempts) + 1,
        base_revision=op.base_revision or 0,
        current_revision=revision,
        control_since_base=False,
        control_operation_id=None,
        touched=("timeline",),
    )
    outcome = Regenerate(reason="timeline_moved", touched=("timeline",))
    op.attempts.append(attempt_record(state, outcome))
    return RevalidationRequired(outcome, state)


async def _edit[P](
    db: AsyncEngine,
    story_id: str,
    *,
    principal: str,
    idempotency_key: str | None,
    plan: Callable[[AsyncConnection], Awaitable[P]],
    stage: Callable[[Operation, P], None],
    apply: Callable[[Operation, AsyncConnection, P], Awaitable[None]],
) -> tuple[Operation, P]:
    """Run one control edit: plan in the read phase, stage it, and commit only if the plan
    under the story lock is the same; otherwise re-read (``EDIT_RETRY_BUDGET`` times)."""
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        control_action=True,
        idempotency_key=idempotency_key,
    )
    try:
        planned = await op.begin(read=plan)
        for attempt in range(EDIT_RETRY_BUDGET + 1):
            stage(op, planned)
            staged = planned

            async def step(conn: AsyncConnection, staged: P = staged) -> None:
                if await plan(conn) != staged:
                    revision = await conn.scalar(
                        select(stories.c.timeline_revision).where(stories.c.id == story_id)
                    )
                    raise _moved(op, int(revision or 0))
                await apply(op, conn, staged)

            op.add_commit_step(step)
            try:
                await op.commit()
            except RevalidationRequired as signal:
                if attempt >= EDIT_RETRY_BUDGET:
                    await op.abandon()
                    raise RevisionConflict(
                        "the timeline kept changing under the edit; retry budget spent",
                        {
                            "current_revision": signal.state.current_revision,
                            "attempts": len(op.attempts),
                            "reason": "timeline_moved",
                        },
                    ) from None
                planned = await op.restart(read=plan)
                continue
            return op, staged
    except BaseException as exc:
        if op.in_flight:
            await op.fail(exc)
        raise
    raise AssertionError("unreachable")  # pragma: no cover


async def truncate(
    db: AsyncEngine,
    story_id: str,
    request: TruncateRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> Truncation:
    """Truncate ``story_id`` to ``request.to_turn``; see the module docstring."""
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    failed_queue: list[str] = []

    async def plan(conn: AsyncConnection) -> _TruncatePlan:
        return await _plan_truncate(conn, story_id, request.to_turn)

    def stage(op: Operation, planned: _TruncatePlan) -> None:
        op.append("truncate", planned.payload)

    async def apply(op: Operation, conn: AsyncConnection, planned: _TruncatePlan) -> None:
        ids = planned.payload.operation_ids
        await _flip(conn, story_id, ids, Membership.DISCARDED)
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(next_turn_index=planned.payload.to_turn + 1)
        )
        revision = await conn.scalar(
            select(stories.c.timeline_revision).where(stories.c.id == story_id)
        )
        failed_queue[:] = await turn_gate.fail_queued(
            conn,
            story_id,
            TimelineChanged(
                "a truncation changed the timeline before the queued turn started",
                {
                    "current_revision": int(revision or 0) + 1,
                    "control_operation_id": op.operation_id,
                },
            ),
        )
        await _run_in_transaction(conn, _change(story_id, op, planned.payload))

    op, planned = await _edit(
        db,
        story_id,
        principal=principal,
        idempotency_key=idempotency_key,
        plan=plan,
        stage=stage,
        apply=apply,
    )
    if failed_queue:
        turn_gate.wakeups.wake(story_id)
    await _run_post_commit(db, _change(story_id, op, planned.payload))
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return Truncation(
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
        next_turn_index=planned.payload.to_turn + 1,
        to_turn=planned.payload.to_turn,
        discarded_operations=len(planned.payload.operation_ids),
    )


async def untruncate(
    db: AsyncEngine,
    story_id: str,
    request: UntruncateRequest | None = None,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> Untruncation:
    """Revert the most recent truncation of ``story_id``; see the module docstring."""
    named = request.truncate_operation_id if request is not None else None
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)

    async def plan(conn: AsyncConnection) -> _UntruncatePlan:
        return await _plan_untruncate(conn, story_id, named)

    def stage(op: Operation, planned: _UntruncatePlan) -> None:
        op.append("untruncate", planned.payload)

    async def apply(op: Operation, conn: AsyncConnection, planned: _UntruncatePlan) -> None:
        await _flip(conn, story_id, planned.payload.operation_ids, Membership.ACTIVE)
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(next_turn_index=planned.payload.restored_head.next_turn_index)
        )
        await _run_in_transaction(conn, _change(story_id, op, planned.payload))

    op, planned = await _edit(
        db,
        story_id,
        principal=principal,
        idempotency_key=idempotency_key,
        plan=plan,
        stage=stage,
        apply=apply,
    )
    await _run_post_commit(db, _change(story_id, op, planned.payload))
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return Untruncation(
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
        next_turn_index=planned.payload.restored_head.next_turn_index,
        truncate_operation_id=planned.payload.truncate_operation_id,
        restored_operations=len(planned.payload.operation_ids),
    )


def _change(story_id: str, op: Operation, payload: Truncate | Untruncate) -> MembershipChange:
    if isinstance(payload, Truncate):
        old, new, cause = Membership.ACTIVE, Membership.DISCARDED, "truncate"
    else:
        old, new, cause = Membership.DISCARDED, Membership.ACTIVE, "untruncate"
    return MembershipChange(
        story_id=story_id,
        operation_ids=tuple(payload.operation_ids),
        old=old,
        new=new,
        cause=cause,
        control_operation_id=op.operation_id,
    )


__all__ = [
    "EDIT_RETRY_BUDGET",
    "SWAP_OPTIONS",
    "DormantChain",
    "HookPhase",
    "MembershipChange",
    "MembershipHook",
    "TruncateRequest",
    "Truncation",
    "UndoState",
    "UntruncateRequest",
    "Untruncation",
    "on_membership_change",
    "remove_membership_hook",
    "truncate",
    "undo_state",
    "untruncate",
]
