"""``GET /admin/stories/{id}/memories``: the story's memories, paged and filtered (#531;
FR-MEM-8, FR-MEM-10).

On #133's world on ``Structured (M4)``: ten bulk turns of Bobby's (one shared episodic memory
each), five level-1 consolidation windows of two turns (one semantic output each) and the
level-2 window over them (one reflection), and a memory Bobby holds on turn 11. One episodic
memory is retired, one pinned, one quarantined. Each filter keeps what it names (the
complement: the others drop out), the cursor pages through the whole list, each consolidation
output carries its window and its source memories (a reflection's are the level-1 outputs),
an episodic memory carries none, and the library answers what REST does. The read needs the
service credential (401) and the omniscient viewpoint (403); an unknown story is 404."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.admin import ADMIN_PREFIX
from epistrel.api.routes.admin.mutation import MemoryListBody
from epistrel.core.context import RequestContext
from epistrel.core.enums import MemoryKind
from epistrel.core.memory.consolidation import Window
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings, bearer
from tests.event_support import execute_in_tx
from tests.hot_set_support import pin_rows
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-operator"}
OMNISCIENT = {"viewpoint": "omniscient"}
UNKNOWN = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


async def test_list_memories(
    pg_url: str, engine: AsyncEngine, models: ModelAccessLayer, drained_embedding_jobs: None
) -> None:
    """Each filter, the cursor, the provenance, the library's answer and auth (see the module
    docstring)."""
    w = await cs.story(engine, cs.dials(window_memories=1_000, cadence_turns=1_000))
    story, bobby = w.story_id, w.characters["Bobby"]
    turns = await cs.bulk_turns(engine, story, 10, actor=bobby, name="Bobby")
    episodic = [t.memory_id for t in turns]
    for n in range(5):
        models.stub.script(
            "consolidation", [cs.answer(facts=[(f"Window {n} happened.", "observation", [0, 1])])]
        )
        run = await consolidate_window(engine, story, Window(2 * n + 1, 2 * n + 2), models=models)
        assert str(run.outcome) == "committed"
    models.stub.script(
        "consolidation", [cs.answer(facts=[("A habit of Bobby's.", "observation", [0, 1])])]
    )
    assert await cs.run_jobs(engine, models, story) == ["committed"]
    level1 = [r for r in await cs.outputs(engine, story) if r.level == 1]
    (reflection,) = [r for r in await cs.outputs(engine, story) if r.level == 2]
    assert len(level1) == 5
    assert reflection.kind == "reflection"
    held = await cs.private_memory(engine, story, bobby, [bobby], "Bobby keeps a secret.")
    retired, pinned, quarantined = episodic[0], episodic[1], episodic[2]
    await execute_in_tx(
        engine,
        (
            "UPDATE memories SET retrieval_tier = 'retired', retired_reason = 'floor', "
            "retired_at = now(), retired_turn = 10 WHERE id = :m",
            {"m": retired},
        ),
    )
    await pin_rows(engine, [pinned])
    everything = sorted([*episodic, *(str(r.id) for r in level1), str(reflection.id), held])

    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        epistrel,
    ):
        path = f"{ADMIN_PREFIX}/stories/{story}/memories"
        done = await client.post(
            f"{ADMIN_PREFIX}/stories/{story}/memories/{quarantined}/quarantine",
            json={"reason": "Operator check."},
            headers=WRITE,
        )
        assert done.status_code == 200, done.text

        async def listed(**params: Any) -> dict[str, Any]:
            found = await client.get(path, params={**OMNISCIENT, **params}, headers=bearer())
            assert found.status_code == 200, found.text
            body: dict[str, Any] = found.json()
            return body

        async def ids(**params: Any) -> list[str]:
            return [m["id"] for m in (await listed(**params))["memories"]]

        # No filter: every active memory, by id, on one page.
        whole = await listed()
        assert [m["id"] for m in whole["memories"]] == everything
        assert whole["next_cursor"] is None

        # Each filter keeps what it names, and drops the rest.
        level1_ids = sorted(str(r.id) for r in level1)
        assert await ids(kind="episodic") == sorted([*episodic, held])
        assert await ids(kind="semantic") == level1_ids
        assert await ids(kind="reflection") == [str(reflection.id)]
        assert await ids(retrieval_tier="retired") == [retired]
        assert retired not in await ids(retrieval_tier="hot")
        assert len(await ids(retrieval_tier="hot")) == len(everything) - 1
        assert await ids(holder_character_id=bobby) == [held]
        assert await ids(quarantined="true") == [quarantined]
        assert quarantined not in await ids(quarantined="false")
        assert len(await ids(quarantined="false")) == len(everything) - 1
        assert await ids(pinned="true") == [pinned]
        assert len(await ids(pinned="false")) == len(everything) - 1
        window_two = next(str(r.id) for r in level1 if r.narrative_turn == 4)
        assert await ids(from_turn=3, to_turn=4) == sorted([episodic[2], episodic[3], window_two])
        assert held in await ids(from_turn=11)
        assert held not in await ids(to_turn=10)
        assert await ids(kind="episodic", from_turn=1, to_turn=2, pinned="true") == [pinned]
        empty = await client.get(
            path, params={**OMNISCIENT, "from_turn": 5, "to_turn": 4}, headers=bearer()
        )
        assert empty.status_code == 422, empty.text
        assert empty.json()["error"]["code"] == "validation_error"

        # The cursor pages through the whole list, five at a time.
        paged: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            page = await listed(limit=5, **({"cursor": cursor} if cursor else {}))
            pages += 1
            paged += [m["id"] for m in page["memories"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert cursor == page["memories"][-1]["id"]
        assert paged == everything
        assert pages == -(-len(everything) // 5)

        # Provenance: each level-1 output names its window and the two episodic memories it
        # compresses; the reflection its level-2 window and the five level-1 outputs.
        by_id = {m["id"]: m for m in whole["memories"]}
        windows = {str(r.operation_id) for r in await cs.windows(engine, story)}
        for output in level1:
            made = by_id[str(output.id)]["consolidation"]
            end = int(output.narrative_turn)
            assert made == {
                "operation_id": str(output.operation_id),
                "from_turn": end - 1,
                "to_turn": end,
                "scope": "shared",
                "level": 1,
                "source_memory_ids": sorted([episodic[end - 2], episodic[end - 1]]),
            }
        assert {str(o.operation_id) for o in (*level1, reflection)} <= windows
        top = by_id[str(reflection.id)]["consolidation"]
        assert top["source_memory_ids"] == level1_ids
        assert (top["from_turn"], top["to_turn"], top["level"]) == (1, 10, 2)
        assert all(by_id[i]["consolidation"] is None for i in [*episodic, held])
        assert by_id[pinned]["pinned"] is True
        assert by_id[quarantined]["quarantined"] is True
        assert by_id[retired]["retrieval_tier"] == "retired"

        # The library answers what REST does.
        library = await epistrel.list_memories(
            RequestContext(), story, viewpoint="omniscient", kind=MemoryKind.REFLECTION
        )
        rest = await listed(kind="reflection")
        assert MemoryListBody.of(library).model_dump(mode="json") == rest

        # Auth: no credential is 401, a non-omniscient read 403; an unknown story is 404.
        assert (await client.get(path, params=OMNISCIENT)).status_code == 401
        private = await client.get(path, headers=bearer())
        assert private.status_code == 403
        assert private.json()["error"]["code"] == "omniscient_required"
        unknown = await client.get(
            f"{ADMIN_PREFIX}/stories/{UNKNOWN}/memories", params=OMNISCIENT, headers=bearer()
        )
        assert unknown.status_code == 404, unknown.text
