"""Story views over the event log (FR-STORE-25; docs/02 §4.6).

The category filter is the only thing that keeps audit records out of story views:

- ``corrected_history`` reads events of committed, **active** operations in the categories
  ``narrative_state``, ``input``, ``derived`` and ``amendment``. The genesis operation's events
  (``story_created``, ``character_defined``: ``control``) are always included. Through turn N
  it keeps positions ≤ N, excluding N's inter-turn slots, plus position-less records
  (FR-STORE-26 admits derived records by provenance; #72 refines the rule). An event #221's
  active overlay parts correct reads corrected (``replace``) or not at all (``drop``).
- ``audit_view`` reads every category of every operation, discarded ones included.
- ``as_originally_recorded`` (#222, FR-STORE-23) reads a checkpoint ``N``/``N⁺k`` as it stood
  at the checkpoint's commit boundary (``boundary_tx``): the events, in every category, of the
  operations committed by then whose membership *at the boundary* was active (``membership_at``
  folds the operations' commits with the ``truncate``/``untruncate``/``swap`` events up to it,
  never the current column), at positions up to the checkpoint and recorded by then, with the
  amendment overlays in effect then (later ones ignored), and the erasure markers (an erasure
  is the one change it cannot undo). Only an omniscient read reaches it, and only the history
  read calls it (``tools/gates/as_recorded.py``).

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


# -- as originally recorded (#222) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A checkpoint: turn ``turn``'s end, with its inter-turn slots ``1..slot_k``."""

    turn: int
    slot_k: int = 0


@dataclass(frozen=True, slots=True)
class HistoryView:
    """A checkpoint as originally recorded (see the module docstring): the events, the boundary,
    the erasure markers (``{erasure_id, at}``) and the canon facts as recorded then."""

    checkpoint: Checkpoint
    boundary_tx: datetime
    events: list[Event]
    erasure_markers: list[dict[str, Any]]
    facts: list[Any]


async def boundary_tx(
    conn: AsyncConnection, story_id: str, checkpoint: Checkpoint, variant: int | None = None
) -> datetime:
    """When the checkpoint committed (see the module docstring): turn N's completion in the chain
    ``variant`` names (default: N's active variant, else the one committed last), a staged
    turn's completion or abandonment
    (409 ``checkpoint_incomplete`` while it is under way), or slot k's operation; 404
    ``checkpoint_not_found`` for one that never committed."""
    from epistrel.core.errors import CheckpointIncomplete, CheckpointNotFound

    o = operations.c
    if checkpoint.turn == 0:
        where: list[Any] = [o.kind == OperationKind.GENESIS.value]
    elif checkpoint.slot_k:
        where = [o.narrative_turn == checkpoint.turn, o.slot_k == checkpoint.slot_k]
    else:
        where = [
            o.narrative_turn == checkpoint.turn,
            o.slot_k.is_(None),
            o.kind == OperationKind.TURN.value,
        ]
        if variant is not None:
            where.append(o.turn_variant == variant)
    # N's active variant, else (a truncated tail) the variant committed last.
    active_first = (o.membership == Membership.ACTIVE.value).desc()
    row = (
        await conn.execute(
            select(o.committed_at, o.turn_stage, o.stage_log, o.status)
            .where(o.story_id == story_id, o.status == OperationStatus.COMMITTED.value, *where)
            .order_by(active_first, o.committed_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None or row.committed_at is None:
        raise CheckpointNotFound(
            "no committed checkpoint there",
            {"turn": checkpoint.turn, "slot_k": checkpoint.slot_k},
        )
    stage = (
        None if row.turn_stage is None else str(getattr(row.turn_stage, "value", row.turn_stage))
    )
    if stage is None:
        return row.committed_at  # type: ignore[no-any-return]
    if stage not in ("completed", "abandoned"):
        raise CheckpointIncomplete(
            "the turn is still under way", {"turn": checkpoint.turn, "turn_stage": stage}
        )
    ended = [
        entry
        for entry in (row.stage_log or [])
        if isinstance(entry, dict) and entry.get("stage") in ("completed", "abandoned")
    ]
    if not ended or not ended[-1].get("at"):
        return row.committed_at  # type: ignore[no-any-return]
    return datetime.fromisoformat(str(ended[-1]["at"]))


async def membership_at(conn: AsyncConnection, story_id: str, at: datetime) -> dict[str, bool]:
    """Whether each operation committed by ``at`` was on the active timeline at ``at``: a commit
    makes its operation active (a regenerated turn's other variants dormant), and each
    ``truncate``, ``untruncate`` and ``swap`` event recorded by then moves the operations it
    names, in record order."""
    o, e = operations.c, events.c
    ops = list(
        await conn.execute(
            select(
                o.operation_id, o.kind, o.narrative_turn, o.turn_variant, o.slot_k, o.committed_at
            )
            .where(
                o.story_id == story_id,
                o.status == OperationStatus.COMMITTED.value,
                o.committed_at <= at,
            )
            .order_by(o.committed_at, o.operation_id)
        )
    )
    moves = list(
        await conn.execute(
            select(e.type, e.payload, e.tx_time)
            .where(
                e.story_id == story_id,
                e.type.in_(("truncate", "untruncate", "swap")),
                e.tx_time <= at,
            )
            .order_by(e.tx_time, e.seq)
        )
    )
    timeline: list[tuple[datetime, int, Any]] = [(r.committed_at, 0, r) for r in ops]
    timeline += [(m.tx_time, 1, m) for m in moves]
    timeline.sort(key=lambda item: (item[0], item[1]))
    active: dict[str, bool] = {}
    for _, kind, item in timeline:
        if kind == 0:
            active[str(item.operation_id)] = True
            if item.kind == OperationKind.TURN.value and item.narrative_turn is not None:
                for other in ops:
                    if (
                        other.kind == OperationKind.TURN.value
                        and other.narrative_turn == item.narrative_turn
                        and other.operation_id != item.operation_id
                        and other.slot_k is None
                        and item.slot_k is None
                        and str(other.operation_id) in active
                    ):
                        active[str(other.operation_id)] = False
            continue
        payload = dict(item.payload or {})
        if item.type == "truncate":
            for op_id in payload.get("operation_ids", []):
                active[str(op_id)] = False
        elif item.type == "untruncate":
            for op_id in payload.get("operation_ids", []):
                active[str(op_id)] = True
        else:
            for op_id in payload.get("activated_operation_ids", []):
                active[str(op_id)] = True
            for op_id in payload.get("dormant_operation_ids", []):
                active[str(op_id)] = False
    return active


def _in_checkpoint(event: Event, checkpoint: Checkpoint) -> bool:
    if event.narrative_turn is None:
        return True
    if event.narrative_turn < checkpoint.turn:
        return True
    if event.narrative_turn > checkpoint.turn:
        return False
    return event.slot_k is None or event.slot_k <= checkpoint.slot_k


async def _overlaid_at(
    conn: AsyncConnection, story_id: str, found: list[Event], at: datetime
) -> list[Event]:
    """``found`` with the overlay parts in effect at ``at`` (recorded by then and not yet
    reverted), newest last."""
    from epistrel.core.amendments.store import merged_payload
    from epistrel.core.tables import amendment_parts

    p = amendment_parts.c
    parts = list(
        await conn.execute(
            select(p.record_id, p.action, p.patch_after)
            .where(
                p.story_id == story_id,
                p.kind == "record",
                p.record_table == "events",
                p.tx_from <= at,
                or_(p.tx_to.is_(None), p.tx_to > at),
            )
            .order_by(p.tx_from, p.amendment_id, p.ordinal)
        )
    )
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
                break
            payload = merged_payload(payload or {}, part.patch_after)
        if payload is not None:
            out.append(replace(event, payload=validate(event.type, payload)))
    return out


async def erasure_markers(conn: AsyncConnection, story_id: str) -> list[dict[str, Any]]:
    """The story's erasures, each ``{erasure_id, at}`` once (no subject ids)."""
    rows = await conn.execute(
        select(events.c.payload, events.c.tx_time)
        .where(events.c.story_id == story_id, events.c.type == "erasure_marker")
        .order_by(events.c.tx_time, events.c.seq)
    )
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        erasure_id = str((row.payload or {}).get("erasure_id") or "")
        if erasure_id and erasure_id not in found:
            found[erasure_id] = {"erasure_id": erasure_id, "at": row.tx_time.isoformat()}
    return list(found.values())


async def as_originally_recorded(
    db: AsyncEngine | AsyncConnection,
    story_id: str,
    checkpoint: Checkpoint,
    *,
    variant: int | None = None,
) -> HistoryView:
    """The checkpoint as originally recorded (see the module docstring)."""
    from epistrel.core.canon import facts as canon

    async with _connection(db) as conn:
        boundary = await boundary_tx(conn, story_id, checkpoint, variant)
        active = await membership_at(conn, story_id, boundary)
        found = await _fetch(conn, _base(story_id).where(events.c.tx_time <= boundary))
        kept = [
            e
            for e in found
            if (
                e.category is EventCategory.AUDIT
                or e.kind is OperationKind.GENESIS
                or active.get(e.operation_id, False)
            )
            and _in_checkpoint(e, checkpoint)
        ]
        kept = await _overlaid_at(conn, story_id, kept, boundary)
        markers = await erasure_markers(conn, story_id)
        recorded = await canon.canon_as_recorded(conn, story_id, record_time=boundary)
    return HistoryView(checkpoint, boundary, kept, markers, list(recorded))
