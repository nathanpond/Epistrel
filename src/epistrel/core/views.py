"""Story views over the event log (FR-STORE-25; docs/02 §4.6).

The category filter is the only thing that keeps audit records out of story views:

- ``corrected_history`` reads events of committed, **active** operations in the categories
  ``narrative_state``, ``input``, ``derived`` and ``amendment``. The genesis operation's events
  (``story_created``, ``character_defined``: ``control``) are always included. Through turn N
  it keeps positions ≤ N, excluding N's inter-turn slots, plus position-less records
  (FR-STORE-26 admits derived records by provenance; #72 refines the rule). An event #221's
  active overlay parts correct reads corrected (``replace``) or not at all (``drop``).
- ``audit_view`` reads every category of every operation, discarded ones included.

Both return typed ``Event``s ordered by ``(narrative_turn, turn_variant, seq)``, with
position-less rows first. ``seq`` is never used as a narrative position (FR-STORE-8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import (
    Authority,
    EventCategory,
    Membership,
    OperationKind,
    OperationStatus,
    Provenance,
)
from epistrel.core.events.registry import EventPayload, validate
from epistrel.core.tables import events, operations

STORY_CATEGORIES: tuple[EventCategory, ...] = (
    EventCategory.NARRATIVE_STATE,
    EventCategory.INPUT,
    EventCategory.DERIVED,
    EventCategory.AMENDMENT,
)
"""The categories a corrected story view reads (FR-STORE-25)."""


@dataclass(frozen=True, slots=True)
class Event:
    """One event row with its decoded payload and its operation's kind, membership and status."""

    id: str
    story_id: str | None
    seq: int
    operation_id: str
    category: EventCategory
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None
    step: int | None
    slot_k: int | None
    character_id: str | None
    principal: str
    type: str
    payload: EventPayload
    valid_time: datetime
    tx_time: datetime
    provenance: Provenance
    authority: Authority
    kind: OperationKind
    membership: Membership
    status: OperationStatus


async def corrected_history(
    db: AsyncEngine | AsyncConnection, story_id: str, through_turn: int | None = None
) -> list[Event]:
    """The current corrected history of ``story_id``, optionally through turn ``through_turn``."""
    story_content = and_(
        operations.c.membership == Membership.ACTIVE.value,
        events.c.category.in_([c.value for c in STORY_CATEGORIES]),
    )
    query = _base(story_id).where(
        operations.c.status == OperationStatus.COMMITTED.value,
        or_(operations.c.kind == OperationKind.GENESIS.value, story_content),
    )
    if through_turn is not None:
        query = query.where(
            or_(
                events.c.narrative_turn.is_(None),
                events.c.narrative_turn < through_turn,
                and_(events.c.narrative_turn == through_turn, events.c.slot_k.is_(None)),
            )
        )
    found = await _fetch(db, query)
    return await _overlaid(db, story_id, found)


async def _overlaid(
    db: AsyncEngine | AsyncConnection, story_id: str, found: list[Event]
) -> list[Event]:
    """``found`` with #221's active event overlays applied, in record order: a ``replace``
    merges its patch into the payload (``null`` deletes a key), a ``drop`` hides the event. A
    part follows its event's membership (the event is read only while active)."""
    from epistrel.core.amendments.store import merged_payload
    from epistrel.core.tables import amendment_parts

    p = amendment_parts.c
    query = (
        select(p.record_id, p.action, p.patch_after)
        .where(
            p.story_id == story_id,
            p.kind == "record",
            p.record_table == "events",
            p.status == "active",
        )
        .order_by(p.tx_from, p.amendment_id, p.ordinal)
    )
    async with _connection(db) as conn:
        parts = (await conn.execute(query)).all()
    if not parts:
        return found
    by_event: dict[str, list[Any]] = {}
    for part in parts:
        by_event.setdefault(str(part.record_id), []).append(part)
    out: list[Event] = []
    for event in found:
        applied = by_event.get(event.id)
        if not applied:
            out.append(event)
            continue
        payload: dict[str, Any] | None = event.payload.model_dump(mode="json")
        for part in applied:
            if part.action == "drop":
                payload = None
            elif payload is not None:
                payload = merged_payload(payload, part.patch_after)
            else:
                payload = merged_payload(event.payload.model_dump(mode="json"), part.patch_after)
        if payload is not None:
            out.append(replace(event, payload=validate(event.type, payload)))
    return out


async def audit_view(
    db: AsyncEngine | AsyncConnection, story_id: str, through_turn: int | None = None
) -> list[Event]:
    """Every event of ``story_id`` in every category, discarded operations included."""
    query = _base(story_id)
    if through_turn is not None:
        query = query.where(
            or_(events.c.narrative_turn.is_(None), events.c.narrative_turn <= through_turn)
        )
    return await _fetch(db, query)


def _base(story_id: str) -> Select[Any]:
    return (
        select(
            events,
            operations.c.kind,
            operations.c.membership,
            operations.c.status,
        )
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(events.c.story_id == story_id)
        .order_by(
            events.c.narrative_turn.asc().nulls_first(),
            events.c.turn_variant.asc().nulls_first(),
            events.c.seq,
        )
    )


async def _fetch(db: AsyncEngine | AsyncConnection, query: Select[Any]) -> list[Event]:
    async with _connection(db) as conn:
        rows: Sequence[Any] = (await conn.execute(query)).all()
    return [
        Event(
            id=row.id,
            story_id=row.story_id,
            seq=row.seq,
            operation_id=row.operation_id,
            category=EventCategory(row.category),
            narrative_turn=row.narrative_turn,
            turn_variant=row.turn_variant,
            beat=row.beat,
            step=0 if row.step is None else row.step,  # a row written before 0082 (#548)
            slot_k=row.slot_k,
            character_id=row.character_id,
            principal=row.principal,
            type=row.type,
            payload=validate(row.type, row.payload),
            valid_time=row.valid_time,
            tx_time=row.tx_time,
            provenance=Provenance(row.provenance),
            authority=Authority(row.authority),
            kind=OperationKind(row.kind),
            membership=Membership(row.membership),
            status=OperationStatus(row.status),
        )
        for row in rows
    ]


@asynccontextmanager
async def _connection(db: AsyncEngine | AsyncConnection) -> AsyncIterator[AsyncConnection]:
    if isinstance(db, AsyncConnection):
        yield db
        return
    async with db.connect() as conn:
        yield conn
