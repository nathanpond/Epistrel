"""``story_heads``: one row per story with the next turn index and each turn's active variant.

The row is created at genesis. A ``turn`` operation's events set ``active_variants[turn]`` (a
regenerated variant replaces the earlier one); ``truncate`` drops the turns after ``to_turn``;
``untruncate`` and ``swap`` install the head their payload records. ``next_turn_index`` is
always the highest active turn + 1. ``stories.next_turn_index`` stays authoritative for
positioning; this row mirrors it for reads.

**Cost (#887).** The map has an entry per turn, so neither path touches all of it for a turn.
``apply`` changes the turn's entry in place, keeping the map in turn order by appending (a
re-sort only when a turn lower than the last is added). A turn's ``next_turn_index`` is
``max(next_turn_index, turn + 1)``: every head has ``next_turn_index`` = its highest turn + 1
(``head_row``, and the heads ``truncate``, ``untruncate`` and ``swap`` record), so this is the
highest turn + 1 without reading the map. A commit of turn events only loads
``next_turn_index`` and the touched turns' entries, and writes ``active_variants || delta``;
one with a ``truncate``, ``untruncate`` or ``swap`` loads and writes the whole head.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import cast, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import OperationKind
from epistrel.core.events.types import Head, Swap, Truncate, Untruncate
from epistrel.core.projections.base import Key, LogEvent, Projection, Row, Rows
from epistrel.core.tables import story_heads
from epistrel.core.tables.stories import INITIAL_TURN_INDEX

INSTALLING = (Truncate, Untruncate, Swap)
"""The payloads that replace or cut the map: their commits load and write the whole head."""


def head_row(story_id: str, active_variants: dict[str, int]) -> Row:
    ordered = dict(sorted(active_variants.items(), key=lambda item: int(item[0])))
    highest = max((int(turn) for turn in ordered), default=INITIAL_TURN_INDEX - 1)
    return {"story_id": story_id, "next_turn_index": highest + 1, "active_variants": ordered}


def _installed(story_id: str, head: Head) -> Row:
    row = head_row(story_id, dict(head.active_variants))
    row["next_turn_index"] = head.next_turn_index
    return row


def _positioned(row: Row, turn: int, variant: int) -> bool:
    """Set ``row``'s variant of ``turn`` in place, keeping its map in turn order; answers
    whether the row changed."""
    variants: dict[str, int] = row["active_variants"]
    name = str(turn)
    following = max(int(row["next_turn_index"]), turn + 1)
    if variants.get(name) == variant and row["next_turn_index"] == following:
        return False
    in_order = name in variants or not variants or int(next(reversed(variants))) < turn
    variants[name] = variant
    if not in_order:
        row["active_variants"] = dict(sorted(variants.items(), key=lambda item: int(item[0])))
    row["next_turn_index"] = following
    return True


class HeadsProjection(Projection):
    table = story_heads

    def apply(self, rows: Rows, event: LogEvent) -> None:
        story_id = event.story_id
        current = rows.get((story_id,))
        payload = event.payload
        if isinstance(payload, Untruncate):
            rows.put(_installed(story_id, payload.restored_head))
        elif isinstance(payload, Swap):
            rows.put(_installed(story_id, payload.active_head))
        elif isinstance(payload, Truncate):
            variants: dict[str, int] = current["active_variants"] if current else {}
            kept = {t: v for t, v in variants.items() if int(t) <= payload.to_turn}
            rows.put(head_row(story_id, kept))
        elif (
            event.kind is OperationKind.TURN
            and event.narrative_turn is not None
            and event.turn_variant is not None
        ):
            if current is None:
                rows.put(head_row(story_id, {str(event.narrative_turn): event.turn_variant}))
            elif _positioned(current, event.narrative_turn, event.turn_variant):
                rows.put(current)
        elif current is None and event.kind is OperationKind.GENESIS:
            rows.put(head_row(story_id, {}))

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        if any(isinstance(e.payload, INSTALLING) for e in events):
            found = await conn.execute(
                select(story_heads).where(story_heads.c.story_id == story_id)
            )
            return Rows(self.table, (dict(row._mapping) for row in found))
        turns = sorted(
            {
                str(e.narrative_turn)
                for e in events
                if e.kind is OperationKind.TURN and e.narrative_turn is not None
            }
        )
        variants = story_heads.c.active_variants
        entries: Any = (
            func.jsonb_strip_nulls(
                func.jsonb_build_object(*(arg for t in turns for arg in (t, variants[t])))
            )
            if turns
            else cast(literal("{}"), JSONB)
        )
        found = await conn.execute(
            select(
                story_heads.c.story_id,
                story_heads.c.next_turn_index,
                entries.label("active_variants"),
            ).where(story_heads.c.story_id == story_id)
        )
        return Rows(self.table, (dict(row._mapping) for row in found))

    async def write(self, conn: AsyncConnection, rows: Rows, before: dict[Key, Row]) -> None:
        """Insert a new head; update a changed one with only its changed entries
        (``active_variants || delta``), or the whole map when entries were removed (a
        ``truncate``, ``untruncate`` or ``swap``, whose commit loaded the whole head)."""
        for key, row in rows.snapshot().items():
            old = before.get(key)
            if old is None:
                await conn.execute(insert(story_heads).values(**row))
                continue
            if old == row:
                continue
            new: dict[str, int] = row["active_variants"]
            values: dict[str, Any] = {}
            if row["next_turn_index"] != old["next_turn_index"]:
                values["next_turn_index"] = row["next_turn_index"]
            previous: dict[str, int] = old["active_variants"]
            if previous.keys() - new.keys():
                values["active_variants"] = new
            else:
                delta = {t: v for t, v in new.items() if previous.get(t) != v}
                if delta:
                    values["active_variants"] = story_heads.c.active_variants.op("||")(
                        cast(delta, JSONB)
                    )
            if values:
                await conn.execute(
                    update(story_heads).where(story_heads.c.story_id == key[0]).values(**values)
                )
