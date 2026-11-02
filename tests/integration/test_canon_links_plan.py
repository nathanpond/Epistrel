"""#617: the object side of a canon link read (``canon_links(object=…)``, a neighborhood's) is
index-driven (migration 0107): its statement, captured as it runs, plans from the story's
propositions naming the entity (``ix_propositions_story_object``, an index condition, not
a filter over every fact of the story), with ``ix_facts_proposition`` there to reach their
facts at scale. Sequential scans are off so a tiny
test story still shows the index path."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_the_object_side_uses_its_indexes(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    async with respond_client(stub_settings, engine) as (client, _epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
    inn = await ps.place_entity(engine, story, "the inn")
    await ps.put(engine, story, ids["Ada"], inn)
    captured: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        if "FROM facts AS f JOIN propositions" in statement:
            captured.append((statement, params))

    sync = engine.sync_engine
    async with engine.connect() as conn:
        event.listen(sync, "before_cursor_execute", capture)
        try:
            found = await canon.canon_links(conn, story, object=inn)
        finally:
            event.remove(sync, "before_cursor_execute", capture)
        assert [link.object_id for link in found] == [inn]
        ((statement, params),) = captured
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        raw = await conn.exec_driver_sql(f"EXPLAIN {statement}", params)
        plan = "\n".join(r[0] for r in raw)
    # The entity is an index condition on the story's propositions, not a filter applied to
    # every fact of the story.
    assert "ix_propositions_story_object" in plan, plan
    assert "Filter: ((propositions.object_id" not in plan, plan
    async with engine.connect() as conn:
        indexes = set(
            await conn.scalars(
                text(
                    "SELECT indexname FROM pg_indexes WHERE tablename IN ('facts', 'propositions')"
                )
            )
        )
    # At scale the planner reaches the matched propositions' facts by this index.
    assert {"ix_facts_proposition", "ix_propositions_object_set"} <= indexes
