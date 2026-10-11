"""Helpers for the event-store tests: stories, turns, interleaved background work, raw rows."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.stories import create_story
from epistrel.core.views import Event

STORY_SETUP_KIND = OperationKind.GENESIS
NARRATIVE_CATEGORIES = frozenset({"narrative_state", "input"})

_SEGMENT_LEVELS = itertools.count(1)


def segment_payload(text: str, through: int, *, covers_from: int | None = None) -> dict[str, Any]:
    """A synthetic ``lite_summary_segment`` payload (#72's shape) over ``covers_from`` (default
    ``through``)..``through``. Each gets its own ``level`` so repeated synthetic segments over
    the same turns never collide on the table's unique position; they name no sources."""
    start = covers_from if covers_from is not None else max(1, through)
    return {
        "text": text,
        "covers_from": start,
        "covers_to": max(start, through),
        "level": next(_SEGMENT_LEVELS),
    }


async def new_story(engine: AsyncEngine, principal: str = "p-test") -> str:
    return await create_story(engine, principal=principal, title="a test story")


async def play_turn(
    engine: AsyncEngine,
    story_id: str,
    said: str,
    *,
    principal: str = "p-test",
    turn: int | None = None,
    rejected: str | None = None,
) -> Operation:
    """Commit one turn: the player's input, optionally a rejected output, then the reply."""
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=principal, turn=turn
    ) as op:
        op.append("player_input", {"text": said}, provenance=Provenance.PLAYER)
        if rejected is not None:
            op.append("guardrail_output_rejected", {"text": rejected, "reason": "test"})
        op.append("message_rendered", {"text": f"reply to {said}"})
        await op.commit()
    return op


async def record(
    engine: AsyncEngine,
    story_id: str | None,
    kind: OperationKind,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    turn: int | None = None,
    principal: str = "system",
) -> Operation:
    """Commit a single-event operation of ``kind``."""
    async with Operation(
        engine, story_id=story_id, kind=kind, principal=principal, turn=turn
    ) as op:
        op.append(event_type, payload, provenance=Provenance.SYSTEM)
        await op.commit()
    return op


async def interleave(engine: AsyncEngine, story_id: str, after_turn: int) -> None:
    """Background, audit and control work between turns (none of it a turn)."""
    await record(
        engine,
        story_id,
        OperationKind.CONTROL,
        "config_changed",
        {"setting": "temperature", "old": 0.7, "new": 0.9},
    )
    if after_turn < 1:
        return
    await record(
        engine,
        story_id,
        OperationKind.MAINTENANCE,
        "guardrail_output_rejected",
        {"text": f"rejected after {after_turn}", "reason": "test"},
        turn=after_turn,
    )
    await record(
        engine,
        story_id,
        OperationKind.CONSOLIDATION,
        "lite_summary_segment",
        segment_payload(f"summary through {after_turn}", after_turn),
        turn=after_turn,
    )


def is_story_setup(event: Event) -> bool:
    return event.kind is STORY_SETUP_KIND


def narrative(events: list[Event]) -> list[tuple[Any, ...]]:
    """The narrative-state and input contents of a view, with positions (no seq, no ids)."""
    return [
        (e.narrative_turn, e.turn_variant, e.type, e.payload)
        for e in events
        if e.category.value in NARRATIVE_CATEGORIES
    ]


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


async def story_row(engine: AsyncEngine, story_id: str) -> tuple[Any, ...]:
    found = await rows(
        engine,
        "SELECT timeline_revision, config_revision, next_turn_index, status "
        "FROM stories WHERE id = :id",
        id=story_id,
    )
    return tuple(found[0])


async def execute_in_tx(engine: AsyncEngine, *statements: tuple[str, dict[str, Any]]) -> None:
    async with engine.begin() as conn:
        for sql, params in statements:
            await conn.execute(text(sql), params)


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)
