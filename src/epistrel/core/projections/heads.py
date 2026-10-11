"""``story_heads``: one row per story with the next turn index and each turn's active variant.

The row is created at genesis. A ``turn`` operation's events set ``active_variants[turn]`` (a
regenerated variant replaces the earlier one); ``truncate`` drops the turns after ``to_turn``;
``untruncate`` and ``swap`` install the head their payload records. ``next_turn_index`` is
always the highest active turn + 1. ``stories.next_turn_index`` stays authoritative for
positioning; this row mirrors it for reads.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import OperationKind
from epistrel.core.events.types import Head, Swap, Truncate, Untruncate
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.tables import story_heads
from epistrel.core.tables.stories import INITIAL_TURN_INDEX


def head_row(story_id: str, active_variants: dict[str, int]) -> Row:
    ordered = dict(sorted(active_variants.items(), key=lambda item: int(item[0])))
    highest = max((int(turn) for turn in ordered), default=INITIAL_TURN_INDEX - 1)
    return {"story_id": story_id, "next_turn_index": highest + 1, "active_variants": ordered}


def _installed(story_id: str, head: Head) -> Row:
    row = head_row(story_id, dict(head.active_variants))
    row["next_turn_index"] = head.next_turn_index
    return row


class HeadsProjection(Projection):
    table = story_heads

    def apply(self, rows: Rows, event: LogEvent) -> None:
        story_id = event.story_id
        current = rows.get((story_id,))
        variants: dict[str, int] = dict(current["active_variants"]) if current else {}
        payload = event.payload
        if isinstance(payload, Untruncate):
            rows.put(_installed(story_id, payload.restored_head))
        elif isinstance(payload, Swap):
            rows.put(_installed(story_id, payload.active_head))
        elif isinstance(payload, Truncate):
            kept = {t: v for t, v in variants.items() if int(t) <= payload.to_turn}
            rows.put(head_row(story_id, kept))
        elif (
            event.kind is OperationKind.TURN
            and event.narrative_turn is not None
            and event.turn_variant is not None
        ):
            variants[str(event.narrative_turn)] = event.turn_variant
            rows.put(head_row(story_id, variants))
        elif current is None and event.kind is OperationKind.GENESIS:
            rows.put(head_row(story_id, {}))

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        found = await conn.execute(select(story_heads).where(story_heads.c.story_id == story_id))
        return Rows(self.table, (dict(row._mapping) for row in found))
