"""The committed-record probe of a belief transition's antecedent (FR-EPI-14 (a); #160).

A belief transition (``core.epistemic.transitions``) cites a committed record as its cause: a
fact, an event, an utterance or another belief. This module reads the cited row and answers
where it sits on the timeline (its narrative position, ``None`` for a position-less record such
as an author act or a seed) and whether it is a legitimate cause at all (on the active timeline;
a fact ``committed``). It lives outside both ``core.canon`` and ``core.epistemic`` because it
reads both sides (FR-EPI-1's ``check-epistemic-separation`` keeps those packages apart); like
``KeyedLookup`` it only answers existence and position, never content.

The positions are ``core.membership.Position``'s, built from each table's columns: the turn and
variant, ``slot_k`` and ``step`` where the table has them (0 otherwise), and the beat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import EventCategory, FactCommitState, Membership
from epistrel.core.membership import Position, active_operation
from epistrel.core.tables import events, operations
from epistrel.core.tables.beliefs import beliefs
from epistrel.core.tables.facts import facts
from epistrel.core.tables.utterances import utterances

RECORD_TABLES: dict[str, Table] = {
    "facts": facts,
    "events": events,
    "beliefs": beliefs,
    "utterances": utterances,
}
"""The tables a ``record`` antecedent may name (``antecedent_ref.table``)."""


@dataclass(frozen=True, slots=True)
class RecordAntecedent:
    """A cited record as the transition guard needs it: its table and id, its position
    (``None``: position-less, so it precedes everything), whether it is a legitimate cause
    (``usable``, else ``reason``), and the table-specific facts the kind matrix reads: an
    event's ``category`` and ``type``, an utterance's speaker, a belief's holder."""

    table: str
    id: str
    position: Position | None
    usable: bool
    reason: str | None = None
    category: str | None = None
    type: str | None = None
    speaker_id: str | None = None
    holder_id: str | None = None
    operation_id: str | None = None


def _position(row: Any, table: Table) -> Position | None:
    m = row._mapping
    if table is beliefs:
        return Position.of_columns(m, "held_from")
    turn = m.get("narrative_turn")
    if turn is None:
        return None
    return Position(
        int(turn),
        int(m.get("turn_variant") or 0),
        int(m.get("slot_k") or 0),
        int(m.get("beat") or 0),
        int(m.get("step") or 0),
    )


async def probe_record(
    conn: AsyncConnection, story_id: str, table_name: str, record_id: str, *, operation_id: str
) -> RecordAntecedent | None:
    """The record ``record_id`` of ``table_name`` in ``story_id`` (``None``: no such table or
    row). ``operation_id`` is the writing operation: its own events are usable before it
    commits (an author act cites its own control event)."""
    table = RECORD_TABLES.get(table_name)
    if table is None:
        return None
    row = (
        await conn.execute(
            select(table).where(table.c.story_id == story_id, table.c.id == record_id)
        )
    ).one_or_none()
    if row is None:
        return None
    m = row._mapping
    position = _position(row, table)
    found = RecordAntecedent(table_name, record_id, position, True)
    if table is events:
        own = str(m["operation_id"]) == operation_id
        active = own or bool(
            await conn.scalar(
                select(func.count())
                .select_from(operations)
                .where(
                    operations.c.operation_id == m["operation_id"],
                    active_operation(operations),
                )
            )
        )
        return RecordAntecedent(
            table_name,
            record_id,
            position,
            active,
            None if active else "inactive",
            category=str(m["category"]),
            type=str(m["type"]),
            speaker_id=None if m["character_id"] is None else str(m["character_id"]),
            operation_id=str(m["operation_id"]),
        )
    if str(m["membership"]) != Membership.ACTIVE.value:
        return RecordAntecedent(table_name, record_id, position, False, "inactive")
    if table is facts:
        if str(m["commit_state"]) != FactCommitState.COMMITTED.value:
            return RecordAntecedent(table_name, record_id, position, False, "not_committed")
        return found
    if table is utterances:
        return RecordAntecedent(
            table_name,
            record_id,
            position,
            True,
            speaker_id=str(m["speaker_character_id"]),
            operation_id=str(m["operation_id"]),
        )
    # beliefs
    return RecordAntecedent(
        table_name,
        record_id,
        position,
        True,
        holder_id=str(m["holder_character_id"]),
        operation_id=str(m["operation_id"]),
    )


def is_author_act(found: RecordAntecedent) -> bool:
    """Whether a cited event is an author act (a ``control`` event: an author directive or a
    seed's), the antecedent of an ``author`` transition."""
    return found.table == "events" and found.category == EventCategory.CONTROL.value


__all__ = ["RECORD_TABLES", "RecordAntecedent", "is_author_act", "probe_record"]
