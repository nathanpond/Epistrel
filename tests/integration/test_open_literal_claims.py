"""A declared claim whose literal object is an open phrase is never canon (#453; FR-COMMIT-5).

A render's beat declares ``{coin, coin_face, something}`` on a predicate whose object is a
``str`` literal. "something" says the value is undetermined, so it gets no world value: the turn
canonizes nothing and no ``coin_face(coin, "something")`` fact exists. A definite face in the
same shape still commits (the complement)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry, store
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, played, respond_client
from tests.structured_turn_support import beat, claim, render, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _coin_story(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    story, ids = await structured_story(client, "Wren", "Ada")
    async with engine.begin() as conn:
        await store.add_entity(conn, story, "item", "coin")
        await registry.register_author(
            conn,
            story,
            registry.ScenarioRules.parse(
                {"predicates": [{"name": "coin_face", "object_type": "str"}]}
            ),
        )
    return story, ids


async def _faces(engine: AsyncEngine, story: str) -> list[Any]:
    return [
        r.literal["value"]
        for r in await rows(
            engine,
            "SELECT p.literal FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "JOIN predicates q ON q.id = f.predicate_id WHERE f.story_id = :s "
            "AND q.name = 'coin_face'",
            s=story,
        )
    ]


async def _canonized(engine: AsyncEngine, story: str) -> int:
    (report,) = await rows(
        engine,
        "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn'",
        s=story,
    )
    return int(report.turn_report["commit"]["canonized"])


async def test_an_open_literal_is_never_canonized(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for face, expected in (("something", []), ("heads", ["heads"])):
            story, ids = await _coin_story(client, engine)
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "do",
                            "Ada catches the coin.",
                            propositions=[claim("coin", "coin_face", face)],
                        )
                    )
                ],
            )
            await played(client, story, body(ids["Wren"], "Ada, flip the coin."))
            assert await _faces(engine, story) == expected, face
            assert (await _canonized(engine, story) > 0) is bool(expected), face
