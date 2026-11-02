"""Batch E's per-turn read guards (#621, and the others' counts as they land): a two-primary
``sequential`` turn in the key room, its reads counted by wrapping the readers' database calls.

- #621: the predicate registry is read from the database at most once per turn attempt
  (``registry.turn_cache``; ~134 a turn at P-04 scale before);
- #615: the characters' private records (their belief statements) are read at most once per
  turn attempt (``core.turn_memo``; ~35 whole-partition reads a turn at P-04 scale before);
- #619: the commit's story-constant reads (the erasure index's story subjects, the erasure
  ledger, each character's ``is_player``) once per turn, and the whole turn's statement count
  under a recorded ceiling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.erasure import guard
from epistrel.core.erasure import index as erasure_index
from epistrel.core.propositions import registry
from epistrel.core.verify import protected
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


async def test_a_turn_reads_the_private_records_once(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        with counting(monkeypatch, protected, "_private_records") as reads:
            await _two_primary_turn(client, epistrel.models.stub, room)
        assert 1 <= len(reads) <= 1, len(reads)


TURN_STATEMENTS_MAX = 800
"""#619: the ceiling on a two-primary key-room turn's statements: 755 measured after batch E's
reads moved to the per-turn memo (764 before #619, with #615 and #621 in), with room for
noise; a regression that re-reads per record or per transition crosses it."""


async def test_a_turn_reads_story_constants_once(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event

    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        statements: list[str] = []

        def count(*args: Any) -> None:
            statements.append(str(args[2]))

        sync = engine.sync_engine
        with (
            counting(monkeypatch, erasure_index, "story_subjects") as subjects,
            counting(monkeypatch, guard, "_story_erasures") as ledger,
        ):
            event.listen(sync, "before_cursor_execute", count)
            try:
                await _two_primary_turn(client, epistrel.models.stub, room)
            finally:
                event.remove(sync, "before_cursor_execute", count)
        assert len(subjects) <= 1, len(subjects)
        assert len(ledger) <= 1, len(ledger)
        players = [s for s in statements if s.startswith("SELECT characters.is_player")]
        assert len(players) <= 3, len(players)  # Cal, Bobby, Jenni: once each
        assert len(statements) <= TURN_STATEMENTS_MAX, len(statements)
