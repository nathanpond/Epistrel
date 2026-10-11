"""Helpers for the projection tests: synthetic timeline control through ``Operation``, and
canonical snapshots of the projection rows.

Truncate, untruncate and swap belong to #65/#66. Until they land, these helpers write the same
control events (with the settled payloads) and flip ``operations.membership`` in the same
transaction, so the fold has the membership changes it must reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import Membership, OperationKind, OperationStatus, Provenance
from epistrel.core.events import Operation
from epistrel.core.tables import events, operations, stories

CONTROL_PRINCIPAL = "p-control"


@dataclass(frozen=True)
class OpRow:
    operation_id: str
    kind: str
    narrative_turn: int | None
    turn_variant: int | None
    membership: str


@dataclass(frozen=True)
class Snapshot:
    """Every projection row of a story as canonical JSON text, in primary-key order."""

    transcript: tuple[str, ...]
    heads: tuple[str, ...]


async def snapshot(engine: AsyncEngine, story_id: str) -> Snapshot:
    async with engine.connect() as conn:
        transcript = await conn.scalars(
            text(
                "SELECT to_jsonb(t)::text FROM transcript_messages t "
                "WHERE story_id = :s ORDER BY message_id"
            ),
            {"s": story_id},
        )
        transcript_rows = tuple(transcript)
        heads = await conn.scalars(
            text(
                "SELECT to_jsonb(h)::text FROM story_heads h WHERE story_id = :s ORDER BY story_id"
            ),
            {"s": story_id},
        )
        return Snapshot(transcript=transcript_rows, heads=tuple(heads))


async def committed_operations(engine: AsyncEngine, story_id: str) -> list[OpRow]:
    async with engine.connect() as conn:
        found = await conn.execute(
            select(
                operations.c.operation_id,
                operations.c.kind,
                operations.c.narrative_turn,
                operations.c.turn_variant,
                operations.c.membership,
            )
            .where(
                operations.c.story_id == story_id,
                operations.c.status == OperationStatus.COMMITTED.value,
            )
            .order_by(operations.c.committed_at, operations.c.operation_id)
        )
        return [OpRow(*row) for row in found]


def head_of(ops: list[OpRow], membership: dict[str, str] | None = None) -> dict[str, Any]:
    """The head the active turn operations make, after the ``membership`` overrides."""
    membership = membership or {}
    variants: dict[str, int] = {}
    for op in ops:
        state = membership.get(op.operation_id, op.membership)
        if op.kind == OperationKind.TURN.value and state == Membership.ACTIVE.value:
            assert op.narrative_turn is not None
            assert op.turn_variant is not None
            variants[str(op.narrative_turn)] = op.turn_variant
    ordered = dict(sorted(variants.items(), key=lambda item: int(item[0])))
    highest = max((int(t) for t in ordered), default=0)
    return {"next_turn_index": highest + 1, "active_variants": ordered}


async def _control(
    engine: AsyncEngine,
    story_id: str,
    event_type: str,
    payload: dict[str, Any],
    flips: dict[str, str],
    next_turn_index: int,
) -> Operation:
    async def step(conn: AsyncConnection) -> None:
        # Retire before activating: one active variant per turn holds after every statement
        # (`uq_operations_active_turn` is not deferrable).
        for membership in sorted(set(flips.values()), key=lambda m: m == Membership.ACTIVE.value):
            ids = [op for op, m in flips.items() if m == membership]
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id.in_(ids))
                .values(membership=membership, membership_tx=func.transaction_timestamp())
            )
        await conn.execute(
            update(stories).where(stories.c.id == story_id).values(next_turn_index=next_turn_index)
        )

    async with Operation(
        engine,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=CONTROL_PRINCIPAL,
        bumps_revision=True,
        control_action=True,
    ) as op:
        op.append(event_type, payload, provenance=Provenance.CONSUMER)
        op.add_commit_step(step)
        await op.commit()
    return op


async def truncate(engine: AsyncEngine, story_id: str, to_turn: int) -> Operation:
    """Discard every active operation positioned after ``to_turn``."""
    ops = await committed_operations(engine, story_id)
    discarded = [
        op.operation_id
        for op in ops
        if op.membership == Membership.ACTIVE.value
        and op.narrative_turn is not None
        and op.narrative_turn > to_turn
    ]
    flips = dict.fromkeys(discarded, Membership.DISCARDED.value)
    after = head_of(ops, flips)
    payload = {"to_turn": to_turn, "prior_head": head_of(ops), "operation_ids": discarded}
    return await _control(engine, story_id, "truncate", payload, flips, after["next_turn_index"])


async def _latest_undoable_truncate(
    engine: AsyncEngine, story_id: str, ops: list[OpRow]
) -> tuple[str, int, list[str]] | None:
    """The latest truncation whose discarded operations are all still discarded."""
    state = {op.operation_id: op.membership for op in ops}
    async with engine.connect() as conn:
        found = await conn.execute(
            select(events.c.operation_id, events.c.payload)
            .where(events.c.story_id == story_id, events.c.type == "truncate")
            .order_by(events.c.seq.desc())
        )
        for operation_id, payload in found:
            ids = list(payload["operation_ids"])
            if ids and all(state.get(op) == Membership.DISCARDED.value for op in ids):
                return operation_id, int(payload["to_turn"]), ids
    return None


async def untruncate(engine: AsyncEngine, story_id: str) -> Operation | None:
    """Reactivate the latest truncation's operations when no new turn was written over them."""
    ops = await committed_operations(engine, story_id)
    found = await _latest_undoable_truncate(engine, story_id, ops)
    if found is None:
        return None
    truncate_id, to_turn, ids = found
    if _newer_active(ops, to_turn):
        return None
    flips = dict.fromkeys(ids, Membership.ACTIVE.value)
    head = head_of(ops, flips)
    payload = {"truncate_operation_id": truncate_id, "operation_ids": ids, "restored_head": head}
    return await _control(engine, story_id, "untruncate", payload, flips, head["next_turn_index"])


async def swap(engine: AsyncEngine, story_id: str) -> Operation | None:
    """Reactivate the latest truncation's tail and make the newer tail over it dormant."""
    ops = await committed_operations(engine, story_id)
    found = await _latest_undoable_truncate(engine, story_id, ops)
    if found is None:
        return None
    _, to_turn, ids = found
    newer = _newer_active(ops, to_turn)
    if not newer:
        return None
    flips = {
        **dict.fromkeys(newer, Membership.DORMANT.value),
        **dict.fromkeys(ids, Membership.ACTIVE.value),
    }
    head = head_of(ops, flips)
    payload = {
        "turn": to_turn + 1,
        "activated_operation_ids": ids,
        "dormant_operation_ids": newer,
        "active_head": head,
    }
    return await _control(engine, story_id, "swap", payload, flips, head["next_turn_index"])


def _newer_active(ops: list[OpRow], to_turn: int) -> list[str]:
    return [
        op.operation_id
        for op in ops
        if op.membership == Membership.ACTIVE.value
        and op.narrative_turn is not None
        and op.narrative_turn > to_turn
    ]
