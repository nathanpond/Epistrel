"""``lite_summary_segments``: one row per Lite rolling-summary segment (FR-LITE-2,
FR-STORE-26,27; #72).

A ``lite_summary_segment`` event becomes one row keyed by the event id. Its operation (a
``consolidation``) is positioned at the segment's ``covers_to``, so the row follows that
operation's membership exactly as transcript rows follow theirs: ``truncate``, ``untruncate``
and ``swap`` flip the rows of the operations they name, a regenerated turn makes the active rows
at or after its index ``dormant`` (as its commit does to their operations), and every turn makes
the still-``discarded`` rows at or after its index ``dormant``.

On top of the operation's membership a row can be ``superseded``: while a merged segment (level
> 0) whose own operation is active names the row's operation among its sources. The state is
recomputed after every change (``settle``), so a child goes back to ``active`` when its parent
is discarded by a truncation and is superseded again when an untruncate restores the parent.
The fold replays the same events through the same ``apply``, so a rebuild equals the
incrementally maintained table (FR-STORE-4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership
from epistrel.core.events.types import LiteSummarySegment
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.projections.transcript import (
    membership_changes,
    regenerated_turn,
    shelved_from,
)
from epistrel.core.tables import lite_summary_segments

ACTIVE = Membership.ACTIVE.value
SUPERSEDED = Membership.SUPERSEDED.value


def segment_row(event: LogEvent, segment: LiteSummarySegment) -> Row:
    return {
        "segment_id": event.id,
        "story_id": event.story_id,
        "seq": event.seq,
        "operation_id": event.operation_id,
        "covers_from": segment.covers_from,
        "covers_to": segment.covers_to,
        "level": segment.level,
        "turn_variant": event.turn_variant,
        "text": segment.text,
        "membership": event.membership.value,
        "source_versions": [s.model_dump(mode="json") for s in segment.source_versions],
        "model": segment.model,
        "prompt_hash": segment.prompt_hash,
    }


def operation_membership(row: Row) -> str:
    """The row's operation's membership (``superseded`` is a row state over ``active``)."""
    membership = str(row["membership"])
    return ACTIVE if membership == SUPERSEDED else membership


def settle(rows: Rows) -> None:
    """Recompute ``superseded``: a row whose operation is active is superseded exactly when an
    active merged row names that operation among its sources."""
    current = list(rows)
    held = {
        str(source["operation_id"])
        for row in current
        if row["level"] > 0 and operation_membership(row) == ACTIVE
        for source in row["source_versions"] or ()
    }
    for row in current:
        base = operation_membership(row)
        wanted = SUPERSEDED if base == ACTIVE and row["operation_id"] in held else base
        if row["membership"] != wanted:
            rows.put({**row, "membership": wanted})


class LiteSummaryProjection(Projection):
    table = lite_summary_segments

    def apply(self, rows: Rows, event: LogEvent) -> None:
        changed = False
        turn = regenerated_turn(event)
        if turn is not None:
            changed |= _move(rows, turn, (ACTIVE, SUPERSEDED), Membership.DORMANT)
        shelved = shelved_from(event)
        if shelved is not None:
            changed |= _move(rows, shelved, (Membership.DISCARDED.value,), Membership.DORMANT)
        if isinstance(event.payload, LiteSummarySegment):
            rows.put(segment_row(event, event.payload))
            changed = True
        for operation_ids, membership in membership_changes(event):
            changed |= _flip(rows, operation_ids, membership)
        if changed:
            settle(rows)

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        t = lite_summary_segments
        whole = any(
            isinstance(e.payload, LiteSummarySegment) or membership_changes(e) for e in events
        )
        if not whole:
            turns = [
                turn
                for e in events
                for turn in (regenerated_turn(e), shelved_from(e))
                if turn is not None
            ]
            if not turns:
                return Rows(self.table)
            reached = await conn.scalar(
                select(
                    exists().where(
                        t.c.story_id == story_id,
                        t.c.covers_to >= min(turns),
                        t.c.membership != Membership.DORMANT.value,
                    )
                )
            )
            if not reached:
                return Rows(self.table)
        found = await conn.execute(select(t).where(t.c.story_id == story_id))
        return Rows(self.table, (dict(row._mapping) for row in found))


def _move(rows: Rows, turn: int, current: tuple[str, ...], membership: Membership) -> bool:
    moved = False
    for row in rows:
        if row["covers_to"] >= turn and row["membership"] in current:
            rows.put({**row, "membership": membership.value})
            moved = True
    return moved


def _flip(rows: Rows, operation_ids: Iterable[str], membership: Membership) -> bool:
    targets = set(operation_ids)
    flipped = False
    for row in rows:
        if row["operation_id"] in targets and row["membership"] != membership.value:
            rows.put({**row, "membership": membership.value})
            flipped = True
    return flipped


__all__ = ["LiteSummaryProjection", "operation_membership", "segment_row", "settle"]
