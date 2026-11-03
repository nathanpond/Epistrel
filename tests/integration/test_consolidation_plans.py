"""#622: the consolidation scheduler's two reads inside a turn's commit are index-driven
(migration 0106): the shared scope's next window reads ``ix_memories_shared_episodic_turn`` in
turn order under its ``LIMIT`` (no sort, no scan of the story's memories), and the newest
covered turn reads ``ix_operations_consolidation_turn`` backward. Asserted on the plans of the
very statements ``next_window`` and ``covered_to`` issue (captured as they run), with the
planner's sequential and bitmap scans off so a tiny test story still shows the index path."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.memory import consolidation
from epistrel.core.memory.consolidation import Scope
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_the_scheduling_reads_use_their_indexes(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    async with respond_client(stub_settings, engine) as (client, _epistrel):
        story, _ = await structured_story(client, "Wren", "Ada")
    captured: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if "memories.narrative_turn" in statement or "operations.narrative_turn" in statement:
            captured.append((statement, params))

    sync = engine.sync_engine
    event.listen(sync, "before_cursor_execute", capture)
    try:
        async with engine.connect() as conn:
            dials = await consolidation.settings_of(conn, story)
            await consolidation.next_window(conn, story, Scope(), dials, head=10_000)
    finally:
        event.remove(sync, "before_cursor_execute", capture)
    window = next(s for s in captured if "FROM memories" in s[0])
    covered = next(s for s in captured if "FROM operations" in s[0])

    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        plans = []
        for statement, params in (window, covered):
            raw = await conn.exec_driver_sql(f"EXPLAIN {statement}", params)
            plans.append("\n".join(r[0] for r in raw))
    assert "ix_memories_shared_episodic_turn" in plans[0], plans[0]
    assert "Sort" not in plans[0], plans[0]
    assert "ix_operations_consolidation_turn" in plans[1], plans[1]
    assert "Sort" not in plans[1], plans[1]
