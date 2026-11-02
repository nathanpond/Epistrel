"""#614: a keyed ``respond`` replay is byte-identical to the original, a structured turn's too
(its ``call_counts`` hold several purposes, in an order JSONB would change): the keyed call
answers the result as its replay reads it back (``ResultCodec.settle``)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.idempotency import jsonb_ordered
from tests.auth_support import auth_settings
from tests.idempotency_support import REPLAY, keyed
from tests.respond_support import body, respond_client
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def test_jsonb_order() -> None:
    found = jsonb_ordered({"zz": 1, "a": {"ccc": 1, "bb": [{"yy": 1, "x": 2}]}, "b": 0})
    assert list(found) == ["a", "b", "zz"]
    assert list(found["a"]) == ["bb", "ccc"]
    assert list(found["a"]["bb"][0]) == ["x", "yy"]


async def test_a_keyed_replay_is_byte_identical(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        stub.script("generation", [render(beat("say", "Hello.", intent="sincere"))])
        request = body(ids["Wren"], "Ada?")
        first = await keyed(client, story, request, "replay-bytes")
        assert first.status_code == 200, first.text
        assert len(first.json()["call_counts"]) > 1
        again = await keyed(client, story, request, "replay-bytes")
        assert (again.status_code, again.headers.get(REPLAY)) == (200, "true")
        assert again.content == first.content
