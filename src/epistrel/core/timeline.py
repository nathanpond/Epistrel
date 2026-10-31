"""Truncate a story to a checkpoint, undo it, and swap continuations (FR-STORE-10,11,18,28;
docs/02 §4.6; #65, #66).

- **Truncate** to turn N is a ``control`` operation whose only mutation is timeline membership:
  every **active** committed operation positioned after N — any kind, so a Lite summary segment
  positioned at its ``covers_to`` > N goes too (#72 rebuilds), as do turn N's own inter-turn slots
  (#186's State sets and definitions, #193's advances) — becomes ``discarded`` with
  ``membership_tx`` = the truncation's transaction time. Events, rows and valid-times are
  untouched; events inherit membership from their operation, so the update is O(operations after
  N). Operations that are already ``discarded`` (a stacked truncation) or ``dormant`` keep their
  state and stamp. ``stories.next_turn_index`` becomes N + 1 and the revision moves (FR-CONC-1).
  The truncation is a control action: a turn generating meanwhile abandons with 409
  ``timeline_changed`` (#47), and turns waiting in the story's queue fail with the same error in
  the truncation's transaction. ``to_turn`` must be from 0 to the head - 1 (422 otherwise). The
  event records ``{to_turn, prior_head, operation_ids}``.
- **Untruncate** reverts the most recent truncation (LIFO): the newest ``truncate`` none of
  whose operations is active again (they are ``discarded``, or ``dormant`` once a turn was
  written over them, #66). When nothing is active after its ``to_turn`` and its tail continues
  the active turn at ``to_turn``, it reactivates exactly those operations (``membership_tx``
  re-stamped) and restores the head; the event records ``{truncate_operation_id,
  operation_ids, restored_head}``. Otherwise (newer turns over the tail, or the turn it
  continues was regenerated since) undoing it is a **swap** to the tail's chain (#66): the
  original continuation becomes active, the newer one dormant, nothing lost, and the result
  says ``swapped``. No undoable truncation, or a ``truncate_operation_id`` that is not the most
  recent one, is 409 ``nothing_to_undo`` (with ``options: ["swap", "fork"]`` and the dormant
  chains when there are any: a swap is undone by another swap).
- **Swap** (``swap``, #66) makes the chain ``to_variant_chain`` active and the active
  continuation above the branch dormant (``core.variants`` plans it). It is a control action
  like truncate (queued turns fail with ``timeline_changed``; a turn generating meanwhile
  abandons, #47). Swapping to the active chain writes nothing and answers the current
  revision. The event records ``{turn, activated_operation_ids, dormant_operation_ids,
  active_head}``.
- **Undo state** (``undo_state``, read by ``GET /stories/{id}``): ``undo_available`` whenever
  an undo (a reactivation or a swap) exists, and ``dormant_chains``: the dormant continuations
  (``core.variants`` chains), each with the truncation whose tail it is, if any.

Both edits commit against current state: the payload staged from the read phase is
recomputed under the story lock, and if the timeline moved meanwhile the same operation
re-reads and restages (up to ``EDIT_RETRY_BUDGET`` times, then 409 ``revision_conflict``).

**Membership hooks.** ``on_membership_change(hook, phase=...)`` registers a coroutine
``hook(conn, change)`` run for every membership change these edits make: ``in_transaction``
hooks run inside the commit (a failure rolls the edit back); ``post_commit`` hooks run after it
on their own connection, and a failure is logged (no content) and never undoes the edit. #72
registers the Lite summary rebuild here. The first ``in_transaction`` hook is
``core.membership``'s (#130): every registered derived record of the changed operations takes
their new membership in the same transaction.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import ColumnElement, and_, func, or_, select, update
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
    TimelineChanged,
)
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import Head, Swap, Truncate, Untruncate
from epistrel.core.membership import POSITION_MAX, Position, follow_membership_change
from epistrel.core.projections import structured
from epistrel.core.stories import require_writable
from epistrel.core.tables import events, operations, stories
from epistrel.core.variants import (
    ChainRef,
    SwapPlan,
    SwapRequest,
    Tree,
    load_tree,
    plan_path_swap,
    plan_swap,
)

logger = logging.getLogger(__name__)

EDIT_RETRY_BUDGET = 3
"""How many times a truncate or untruncate re-reads after the timeline moved under it."""

SWAP_OPTIONS: tuple[str, ...] = ("swap", "fork")
"""The alternatives a refused undo lists while dormant continuations exist (#66 swap, #67
fork)."""


# -- requests and results ---------------------------------------------------------------------


INSIDE_TURN_FIELDS = ("beat", "step")
"""A position inside a turn (FR-STORE-31): never a checkpoint boundary (#168)."""


def turn_boundary(data: Any) -> Any:
    """A truncation's or fork's body names a turn boundary (``N`` or ``N⁺k``), never a beat or
    a step (FR-STORE-31; #168): such a field is 422 ``turn_boundary_required``."""
    if isinstance(data, Mapping):
        for name in INSIDE_TURN_FIELDS:
            if name in data:
                raise PydanticCustomError(
                    TURN_BOUNDARY_REQUIRED,
                    "a checkpoint is a turn boundary (a turn, or an inter-turn slot), never a "
                    "beat or a step",
                )
    return data


TURN_BOUNDARY_REQUIRED = "turn_boundary_required"

AS_OF_PATTERN = re.compile(r"^\s*(\d{1,9})(?:\s*[+⁺ ]\s*(\d{1,9}))?\s*$")
"""``as_of``'s boundary: ``N`` or ``N+k`` (``⁺`` too, and a space: a query string's ``+``
decodes to one)."""


def parse_as_of(value: str | int, *, field: str = "query.as_of") -> Position:
    """A read's checkpoint (FR-STORE-31; #186): ``"14"`` is ``N`` (turn 14's end, without its
    inter-turn slots) and ``"14+2"`` is ``N⁺k`` (with slots 1..2), as ``Position.end_of``. A
    beat or a step is never a boundary; anything else is 422 ``validation_error``."""
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise InvalidRequest("as_of is a turn N or a slot boundary N+k", {"field": field})
    found = AS_OF_PATTERN.match(text)
    if found is None:
        raise InvalidRequest(
            "as_of is a turn N or an inter-turn slot boundary N+k",
            {"field": field, "reason": "boundary"},
        )
    turn, slot = int(found.group(1)), int(found.group(2) or 0)
    if slot > POSITION_MAX:
        raise InvalidRequest("as_of is out of range", {"field": field, "reason": "range"})
    return Position.end_of(turn, slot)


def boundary_label(turn: int, slot_k: int = 0) -> str:
    """A boundary as responses print it: ``14``, or ``14⁺2`` for an inter-turn slot."""
    return str(turn) if not slot_k else f"{turn}⁺{slot_k}"


class TruncateRequest(BaseModel):
    """``POST /stories/{id}/truncate``: keep turns 1..``to_turn`` (and, with ``slot`` k, turn
    ``to_turn``'s inter-turn slots 1..k, the boundary ``N⁺k``); ``0`` keeps only genesis. A beat
    or a step is never a boundary (422 ``turn_boundary_required``, FR-STORE-31; #168)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"to_turn": 2}]})

    to_turn: StrictInt = Field(description="The last turn to keep (0 to the head - 1).")
    slot: StrictInt | None = Field(
        default=None,
        ge=0,
        le=POSITION_MAX,
        description="The boundary `N⁺k`: keep `to_turn`'s inter-turn slots 1..k (default: "
        "none, the boundary `N`).",
    )

    _boundary = model_validator(mode="before")(turn_boundary)


class UntruncateRequest(BaseModel):
    """``POST /stories/{id}/untruncate``: optionally name the truncation being reverted; it
    must be the most recent one."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"truncate_operation_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C5"}]},
    )

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
    reactivated; ``swapped`` when undoing it was a swap (#66), with the chain it activated."""

    operation_id: str
    timeline_revision: int
    next_turn_index: int
    truncate_operation_id: str
    restored_operations: int
    swapped: bool = False
    active_chain: ChainRef | None = None
    dormant_operations: int = 0
    """How many operations a swap made dormant (0 for a reactivation)."""
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class Swapped:
    """A swap (#66): the chain now active and the story's revision. ``operation_id`` is
    ``None`` when the chain was already active and nothing was written."""

    operation_id: str | None
    timeline_revision: int
    next_turn_index: int
    active_chain: ChainRef
    activated_operations: int = 0
    dormant_operations: int = 0
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class DormantChain:
    """A dormant continuation (#66): turns ``from_turn``..``through_turn`` of the chain
    ``{from_turn, variant}``, reachable by a swap; ``truncate_operation_id`` names the
    truncation whose tail it is, when it is one that ``untruncate`` would undo."""

    from_turn: int
    variant: int
    through_turn: int
    turns: int
    truncate_operation_id: str | None = None


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
    """The control operation's event type (``truncate``, ``untruncate``, ``swap``; ``fork`` for
    the copied operations joining a fork's timeline, #67)."""
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


on_membership_change(follow_membership_change)
"""The first in-transaction hook (#130): the derived records of the changed operations follow
them in the edit's transaction (``core.membership``), before any later-registered hook reads
them."""

on_membership_change(structured.follow_membership_change)
"""The second (#137): the structured projections re-derive what the changed operations' facts
project, once those facts have followed them."""


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


async def run_post_commit_hooks(db: AsyncEngine, change: MembershipChange) -> None:
    """Run the ``post_commit`` hooks for a change another module committed (#67's fork)."""
    await _run_post_commit(db, change)


# -- reads ------------------------------------------------------------------------------------


def _committed(story_id: str) -> Any:
    return and_(
        operations.c.story_id == story_id,
        operations.c.status == OperationStatus.COMMITTED.value,
    )


def _after(to_turn: int, slot: int | None = None) -> Any:
    """Positioned after the boundary ``to_turn`` (``N``: later turns and ``to_turn``'s own
    inter-turn slots) or ``to_turn⁺slot`` (its slots after ``slot`` too; #168)."""
    later_slot: ColumnElement[bool] = operations.c.slot_k.is_not(None)
    if slot:
        later_slot = and_(later_slot, operations.c.slot_k > slot)
    return or_(
        operations.c.narrative_turn > to_turn,
        and_(operations.c.narrative_turn == to_turn, later_slot),
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
    """Every committed truncation none of whose operations is active again (each is still
    ``discarded``, or ``dormant`` under a newer turn), newest first (the LIFO stack of
    undoable truncations)."""
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
        if t.operation_ids and all(state.get(op) in _NOT_ACTIVE for op in t.operation_ids)
    ]


_NOT_ACTIVE = frozenset({Membership.DISCARDED.value, Membership.DORMANT.value})


async def undo_state(db: AsyncEngine | AsyncConnection, story_id: str) -> UndoState:
    """``undo_available`` and ``dormant_chains`` for ``GET /stories/{id}``."""
    if isinstance(db, AsyncConnection):
        return await _undo_state(db, story_id)
    async with db.connect() as conn:
        return await _undo_state(conn, story_id)


async def _undo_state(conn: AsyncConnection, story_id: str) -> UndoState:
    stack = await _undoable(conn, story_id)
    chains = await _dormant_chains(conn, story_id, stack)
    return UndoState(undo_available=bool(stack or chains), dormant_chains=chains)


async def _dormant_chains(
    conn: AsyncConnection, story_id: str, stack: Sequence[_Undoable]
) -> tuple[DormantChain, ...]:
    tree = await load_tree(conn, story_id)
    tails: dict[str, str] = {}
    for truncation in reversed(stack):  # the newest truncation naming an operation wins
        for op in truncation.operation_ids:
            tails[op] = truncation.truncate_operation_id
    found: list[DormantChain] = []
    for chain in tree.chains():
        if chain.membership is not Membership.DORMANT:
            continue
        start = tree.nodes[(chain.turn, chain.variant)]
        found.append(
            DormantChain(
                from_turn=chain.turn,
                variant=chain.variant,
                through_turn=chain.tip_turn,
                turns=chain.turn_count,
                truncate_operation_id=tails.get(start.operation_id),
            )
        )
    return tuple(found)


# -- plans ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TruncatePlan:
    payload: Truncate


@dataclass(frozen=True, slots=True)
class _UntruncatePlan:
    payload: Untruncate


@dataclass(frozen=True, slots=True)
class _UntruncateSwap:
    """Undoing the truncation ``truncate_operation_id`` is a swap to its tail's chain."""

    truncate_operation_id: str
    swap: SwapPlan


async def _plan_truncate(
    conn: AsyncConnection, story_id: str, to_turn: int, slot: int | None = None
) -> _TruncatePlan:
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
            _after(to_turn, slot),
        )
        .order_by(operations.c.narrative_turn, operations.c.operation_id)
    )
    return _TruncatePlan(
        Truncate(to_turn=to_turn, prior_head=await _head(conn, story_id), operation_ids=list(found))
    )


async def _plan_untruncate(
    conn: AsyncConnection, story_id: str, named: str | None
) -> _UntruncatePlan | _UntruncateSwap:
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
        dormant = await _dormant_chains(conn, story_id, stack)
        if dormant:
            details["options"] = list(SWAP_OPTIONS)
            details["dormant_chains"] = [
                {"turn": c.from_turn, "variant": c.variant} for c in dormant
            ]
        raise NothingToUndo(message, details)
    latest = stack[0]
    tree = await load_tree(conn, story_id)
    swap = await _tail_swap(conn, story_id, tree, latest)
    if swap is not None:
        return _UntruncateSwap(latest.truncate_operation_id, swap)
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


async def untruncate_activates(
    conn: AsyncConnection, story_id: str, named: str | None
) -> list[str]:
    """The committed operations undoing the most recent truncation would make active (a
    reactivation's or a swap's), as ``untruncate`` would plan it now: 409 ``nothing_to_undo``
    as it would answer (#120's guard reads this before the edit runs)."""
    planned = await _plan_untruncate(conn, story_id, named)
    if isinstance(planned, _UntruncateSwap):
        return list(planned.swap.payload.activated_operation_ids)
    return list(planned.payload.operation_ids)


async def _tail_swap(
    conn: AsyncConnection, story_id: str, tree: Tree, truncation: _Undoable
) -> SwapPlan | None:
    """The swap that undoes ``truncation`` (#66), or ``None`` when reactivating its tail is
    enough: nothing is active after its ``to_turn`` and the tail continues the active turn."""
    leaf = tree.leaf_of(truncation.operation_ids)
    if leaf is None:
        return None
    path = tree.path(leaf)
    if not path.nodes:  # pragma: no cover - an undoable tail has no active turn
        return None
    if path.branch == truncation.to_turn:
        newer = await conn.scalar(
            select(func.count()).where(
                _committed(story_id),
                operations.c.membership == Membership.ACTIVE.value,
                operations.c.narrative_turn > truncation.to_turn,
            )
        )
        if not newer:
            return None
    return await plan_path_swap(conn, story_id, tree, path)


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
        return await _plan_truncate(conn, story_id, request.to_turn, request.slot)

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
        for change in _changes(story_id, op, planned.payload):
            await _run_in_transaction(conn, change)

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
    for change in _changes(story_id, op, planned.payload):
        await _run_post_commit(db, change)
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
    """Revert the most recent truncation of ``story_id``, by a reactivation or a swap; see the
    module docstring."""
    named = request.truncate_operation_id if request is not None else None
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    failed_queue: list[str] = []

    async def plan(conn: AsyncConnection) -> _UntruncatePlan | _UntruncateSwap:
        return await _plan_untruncate(conn, story_id, named)

    def stage(op: Operation, planned: _UntruncatePlan | _UntruncateSwap) -> None:
        if isinstance(planned, _UntruncateSwap):
            op.append("swap", planned.swap.payload)
        else:
            op.append("untruncate", planned.payload)

    async def apply(
        op: Operation, conn: AsyncConnection, planned: _UntruncatePlan | _UntruncateSwap
    ) -> None:
        if isinstance(planned, _UntruncateSwap):
            failed_queue[:] = await _apply_swap(op, conn, story_id, planned.swap)
            return
        await _flip(conn, story_id, planned.payload.operation_ids, Membership.ACTIVE)
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(next_turn_index=planned.payload.restored_head.next_turn_index)
        )
        for change in _changes(story_id, op, planned.payload):
            await _run_in_transaction(conn, change)

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
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    if isinstance(planned, _UntruncateSwap):
        for change in _changes(story_id, op, planned.swap):
            await _run_post_commit(db, change)
        payload = planned.swap.payload
        return Untruncation(
            operation_id=op.operation_id,
            timeline_revision=op.timeline_revision,
            next_turn_index=payload.active_head.next_turn_index,
            truncate_operation_id=planned.truncate_operation_id,
            restored_operations=len(payload.activated_operation_ids),
            swapped=True,
            active_chain=planned.swap.active_chain,
            dormant_operations=len(payload.dormant_operation_ids),
        )
    for change in _changes(story_id, op, planned.payload):
        await _run_post_commit(db, change)
    return Untruncation(
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
        next_turn_index=planned.payload.restored_head.next_turn_index,
        truncate_operation_id=planned.payload.truncate_operation_id,
        restored_operations=len(planned.payload.operation_ids),
    )


async def swap(
    db: AsyncEngine,
    story_id: str,
    request: SwapRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> Swapped:
    """Make ``request.to_variant_chain`` the active chain of ``story_id``; see the module
    docstring. Swapping to the active chain writes nothing."""
    target = request.to_variant_chain
    await require_writable(db, story_id)
    current = await check_expected_revision(db, story_id, expected_revision)
    async with db.connect() as conn:
        first = await plan_swap(conn, story_id, target)
        if first is None:
            head = await _next_turn_index(conn, story_id)
    if first is None:
        return Swapped(
            operation_id=None, timeline_revision=current, next_turn_index=head, active_chain=target
        )
    failed_queue: list[str] = []

    async def plan(conn: AsyncConnection) -> SwapPlan | None:
        return await plan_swap(conn, story_id, target)

    def stage(op: Operation, planned: SwapPlan | None) -> None:
        if planned is None:  # it became the active chain meanwhile: record a no-op swap
            raise _AlreadyActive
        op.append("swap", planned.payload)

    async def apply(op: Operation, conn: AsyncConnection, planned: SwapPlan | None) -> None:
        assert planned is not None  # noqa: S101 - stage refused None
        failed_queue[:] = await _apply_swap(op, conn, story_id, planned)

    try:
        op, planned = await _edit(
            db,
            story_id,
            principal=principal,
            idempotency_key=idempotency_key,
            plan=plan,
            stage=stage,
            apply=apply,
        )
    except _AlreadyActive:
        async with db.connect() as conn:
            row = (
                await conn.execute(
                    select(stories.c.timeline_revision, stories.c.next_turn_index).where(
                        stories.c.id == story_id
                    )
                )
            ).one()
        return Swapped(
            operation_id=None,
            timeline_revision=int(row.timeline_revision),
            next_turn_index=int(row.next_turn_index),
            active_chain=target,
        )
    assert planned is not None  # noqa: S101 - _edit returns the committed plan
    if failed_queue:
        turn_gate.wakeups.wake(story_id)
    for change in _changes(story_id, op, planned):
        await _run_post_commit(db, change)
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return Swapped(
        operation_id=op.operation_id,
        timeline_revision=op.timeline_revision,
        next_turn_index=planned.payload.active_head.next_turn_index,
        active_chain=planned.active_chain,
        activated_operations=len(planned.payload.activated_operation_ids),
        dormant_operations=len(planned.payload.dormant_operation_ids),
    )


class _AlreadyActive(Exception):
    """The swap's target became the active chain between the first read and the edit."""


async def _apply_swap(
    op: Operation, conn: AsyncConnection, story_id: str, planned: SwapPlan
) -> list[str]:
    """A swap's membership flips (dormant first: one active variant per index holds after
    every statement), the head, the queue, and the in-transaction hooks."""
    payload = planned.payload
    await _flip(conn, story_id, payload.dormant_operation_ids, Membership.DORMANT)
    await _flip(conn, story_id, payload.activated_operation_ids, Membership.ACTIVE)
    await conn.execute(
        update(stories)
        .where(stories.c.id == story_id)
        .values(next_turn_index=payload.active_head.next_turn_index)
    )
    revision = await conn.scalar(
        select(stories.c.timeline_revision).where(stories.c.id == story_id)
    )
    failed = await turn_gate.fail_queued(
        conn,
        story_id,
        TimelineChanged(
            "a swap changed the timeline before the queued turn started",
            {
                "current_revision": int(revision or 0) + 1,
                "control_operation_id": op.operation_id,
            },
        ),
    )
    for change in _changes(story_id, op, planned):
        await _run_in_transaction(conn, change)
    return failed


def _changes(
    story_id: str, op: Operation, payload: Truncate | Untruncate | SwapPlan
) -> list[MembershipChange]:
    """The membership changes an edit made, one per (old, new) pair."""

    def change(ids: Sequence[str], old: Membership, new: Membership, cause: str) -> Any:
        return MembershipChange(
            story_id=story_id,
            operation_ids=tuple(ids),
            old=old,
            new=new,
            cause=cause,
            control_operation_id=op.operation_id,
        )

    if isinstance(payload, Truncate):
        return [change(payload.operation_ids, Membership.ACTIVE, Membership.DISCARDED, "truncate")]
    if isinstance(payload, Untruncate):
        return [
            change(payload.operation_ids, Membership.DISCARDED, Membership.ACTIVE, "untruncate")
        ]
    swapped: Swap = payload.payload
    found = [change(swapped.dormant_operation_ids, Membership.ACTIVE, Membership.DORMANT, "swap")]
    for old in (Membership.DORMANT, Membership.DISCARDED):
        ids = [op_id for op_id, was in payload.previous if was is old]
        if ids:
            found.append(change(ids, old, Membership.ACTIVE, "swap"))
    return found


__all__ = [
    "EDIT_RETRY_BUDGET",
    "SWAP_OPTIONS",
    "DormantChain",
    "HookPhase",
    "MembershipChange",
    "MembershipHook",
    "SwapRequest",
    "Swapped",
    "TruncateRequest",
    "Truncation",
    "UndoState",
    "UntruncateRequest",
    "Untruncation",
    "boundary_label",
    "on_membership_change",
    "parse_as_of",
    "remove_membership_hook",
    "run_post_commit_hooks",
    "swap",
    "truncate",
    "undo_state",
    "untruncate",
    "untruncate_activates",
]
