"""Batch E's per-turn read guards (#621, and the others' counts as they land): a two-primary
``sequential`` turn in the key room, its reads counted by wrapping the readers' database calls.

- #621: the predicate registry is read from the database at most once per turn attempt
  (``registry.turn_cache``; ~134 a turn at P-04 scale before)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@contextmanager
def counting(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> Iterator[list[Any]]:
    """Every call of ``module.name`` while inside, recorded (its first positional args)."""
    calls: list[Any] = []
    original = getattr(module, name)

    async def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[1:2])
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, name, counted)
        yield calls


async def _two_primary_turn(client: Any, stub: Any, room: KeyRoom) -> None:
    stub.reset()
    ms.renders_for(stub, "Bobby", ms.render(beat("say", "Is the lamp on?", intent="sincere")))
    ms.renders_for(stub, "Jenni", ms.render(beat("say", "It is.", intent="sincere")))
    answer = await respond(
        client,
        room.story_id,
        body(room.cal, "What now?", render_character_ids=[room.bobby, room.jenni]),
    )
    assert answer.status_code == 200, answer.text


async def test_a_turn_reads_the_registry_once(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        with counting(monkeypatch, registry, "_read") as reads:
            await _two_primary_turn(client, epistrel.models.stub, room)
        mine = [r for r in reads if r == (room.story_id,)]
        assert len(mine) <= 1, len(mine)
