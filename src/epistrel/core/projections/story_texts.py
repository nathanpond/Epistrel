"""``story_texts``: one row per story with its pinned memories and custom instructions (#60).

The row is created at genesis with no pins and ``""`` instructions. A ``pinned_memories_set``
event installs its ordered snapshot (``[{id, text}]``) and a ``custom_instructions_set`` its
text; each event carries the whole value, so the latest one wins and nothing is re-read from
the log. Events of an operation that is not ``active`` are skipped, as every Lite prompt reader
skips them.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership, OperationKind
from epistrel.core.events.types import CustomInstructionsSet, PinnedMemoriesSet
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.tables import story_texts


def empty_row(story_id: str) -> Row:
    return {"story_id": story_id, "pinned": [], "custom_instructions": ""}


class StoryTextsProjection(Projection):
    table = story_texts

    def apply(self, rows: Rows, event: LogEvent) -> None:
        story_id = event.story_id
        current = rows.get((story_id,))
        payload = event.payload
        if isinstance(payload, PinnedMemoriesSet | CustomInstructionsSet):
            if event.membership is not Membership.ACTIVE:
                return
            row = current or empty_row(story_id)
            if isinstance(payload, PinnedMemoriesSet):
                row["pinned"] = [item.model_dump(mode="json") for item in payload.items]
            else:
                row["custom_instructions"] = payload.text
            rows.put(row)
        elif current is None and event.kind is OperationKind.GENESIS:
            rows.put(empty_row(story_id))

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        found = await conn.execute(select(story_texts).where(story_texts.c.story_id == story_id))
        return Rows(self.table, (dict(row._mapping) for row in found))
