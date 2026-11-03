"""#616: the protected index's canon read (``canon.canon_refs``) pays no JIT compilation and
reads only its story's propositions. Its statement, captured as it runs, is EXPLAINed under the
setting the read gives itself with ``jit_above_cost`` forced to 0 (any plan would compile):
no ``JIT`` section, and no sequential scan of ``propositions`` unbounded by the story."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_canon_refs_reads_without_jit(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, _epistrel):
        story, _ = await structured_story(client, "Wren", "Ada")
    captured: list[tuple[str, Any, str]] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if "FROM facts AS f JOIN propositions" in statement:
            cursor.execute("SELECT current_setting('jit')")
            captured.append((statement, params, cursor.fetchone()[0]))

    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL jit_above_cost = 0"))
        await conn.execute(text("SET LOCAL jit = on"))
        sync = engine.sync_engine
        event.listen(sync, "before_cursor_execute", capture)
        try:
            await canon.canon_refs(conn, story)
        finally:
            event.remove(sync, "before_cursor_execute", capture)
        ((statement, params, jit),) = captured
        assert jit == "off"
        assert await conn.scalar(text("SELECT current_setting('jit')")) == "on"  # restored
        await conn.execute(text("SET LOCAL jit = off"))
        raw = await conn.exec_driver_sql(f"EXPLAIN (ANALYZE, VERBOSE) {statement}", params)
        plan = "\n".join(r[0] for r in raw)
    assert "JIT:" not in plan, plan
    assert "propositions.story_id" in plan, plan
