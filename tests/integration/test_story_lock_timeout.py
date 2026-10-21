"""A story-lock timeout is ``story_busy``, not ``database_unavailable`` (#472, from #452).

A commit that waits past ``lock_timeout`` for the story row another transaction holds is
refused with 423 ``story_busy`` (``details.reason`` ``lock_timeout``) and ``Retry-After``, with
nothing written, on REST and through the library. The database answered, so it is not 503.
A connection that is really lost mid-commit still answers 503 ``database_unavailable``.

The lock is taken by another transaction exactly when the commit is about to lock the story
(``set_lock_timeout`` is wrapped), after the turn gate, so only the commit waits. The lock
timeout is made small (``LOCK_TIMEOUT``); the holder lets go once the commit's wait has ended,
seen from ``pg_stat_activity``, so nothing depends on wall-clock timing beyond that setting.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.errors import StoryBusy
from epistrel.core.events import batch, operation
from epistrel.core.ingest import IngestRequest
from epistrel.orchestrator.lite.schemas import PlayerInputBody, RespondRequest
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import (
    AUTHOR,
    FIRST,
    PLAYER,
    WITHIN_S,
    body,
    new_story,
    respond,
    respond_client,
    until,
)
from tests.story_support import WRITE

pytestmark = pytest.mark.integration

LOCK_TIMEOUT = "100ms"
"""The lock timeout under test: small, so the refusal comes quickly."""

BLOCKED_BY = (
    "SELECT pid, query_start FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid))"
)

NARRATION = {"client_event_id": "lt", "kind": "narration", "text": "Rain."}

Hook = Callable[[AsyncConnection], Awaitable[None]]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _hook(monkeypatch: pytest.MonkeyPatch, before: Hook) -> None:
    """Run ``before(conn)`` once, ahead of the next commit's lock (an operation's or a
    batch's), with the lock timeout made small."""
    real = operation.set_lock_timeout
    armed = [True]

    async def hooked(conn: AsyncConnection) -> None:
        if armed[0]:
            armed[0] = False
            await before(conn)
        await real(conn)

    monkeypatch.setattr(operation, "LOCK_TIMEOUT", LOCK_TIMEOUT)
    monkeypatch.setattr(operation, "set_lock_timeout", hooked)
    monkeypatch.setattr(batch, "set_lock_timeout", hooked)


async def _blocked(engine: AsyncEngine, holder: int) -> set[tuple[int, Any]]:
    async with engine.connect() as probe:
        found = await probe.execute(text(BLOCKED_BY), {"holder": holder})
        return {(r.pid, r.query_start) for r in found}


async def _hold(engine: AsyncEngine, story: str, locked: asyncio.Event) -> None:
    """Hold the story row ``FOR UPDATE`` until the first statement it blocks stops waiting."""
    async with engine.connect() as conn, conn.begin():
        holder = int(await conn.scalar(text("SELECT pg_backend_pid()")) or 0)
        await conn.execute(text("SELECT id FROM stories WHERE id = :s FOR UPDATE"), {"s": story})
        locked.set()
        first: list[tuple[int, Any]] = []

        async def waiting() -> bool:
            first.extend(sorted(await _blocked(engine, holder), key=str)[:1])
            return bool(first)

        await until(waiting, "the commit waits for the story lock")

        async def gave_up() -> bool:
            return first[0] not in await _blocked(engine, holder)

        await until(gave_up, "the commit stops waiting for the story lock")


@asynccontextmanager
async def lock_held_at_commit(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, story: str
) -> AsyncIterator[None]:
    """Inside the block, the next commit finds the story row held by another transaction."""
    holding: list[asyncio.Future[None]] = []

    async def before(conn: AsyncConnection) -> None:
        locked = asyncio.Event()
        holding.append(asyncio.ensure_future(_hold(engine, story, locked)))
        await asyncio.wait_for(locked.wait(), WITHIN_S)

    _hook(monkeypatch, before)
    yield
    assert holding, "no commit took the story lock"
    await asyncio.wait_for(holding[0], WITHIN_S)


async def _footprint(engine: AsyncEngine, story: str) -> tuple[int, int]:
    """(events, committed operations) of the story."""
    (row,) = await rows(
        engine,
        "SELECT (SELECT count(*) FROM events WHERE story_id = :s) AS events, "
        "(SELECT count(*) FROM operations WHERE story_id = :s AND status = 'committed') AS ops",
        s=story,
    )
    return int(row.events), int(row.ops)


def _busy(response: httpx.Response, story: str) -> None:
    assert response.status_code == 423, response.text
    error = response.json()["error"]
    assert error["code"] == "story_busy"
    assert error["details"] == {"story_id": story, "reason": "lock_timeout"}
    assert response.headers["Retry-After"] == str(operation.LOCK_RETRY_AFTER_S)


async def test_rest_lock_timeout_is_story_busy(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#472: respond, ingest (a batch commit) and a control action (truncate) whose commit
    times out on the story lock answer 423 ``story_busy`` with ``Retry-After``, nothing is
    written, and the story plays on once the lock is free."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        first = await respond(client, story, body(ids[PLAYER], "One."))
        assert first.status_code == 200, first.text

        calls: list[tuple[str, Callable[[], Awaitable[httpx.Response]]]] = [
            ("respond", lambda: respond(client, story, body(ids[PLAYER], "Two."))),
            (
                "ingest",
                lambda: client.post(
                    f"/stories/{story}/ingest",
                    json={"events": [NARRATION]},
                    headers=WRITE,
                ),
            ),
            (
                "truncate",
                lambda: client.post(
                    f"/stories/{story}/truncate", json={"to_turn": 0}, headers=WRITE
                ),
            ),
        ]
        for name, call in calls:
            before = await _footprint(engine, story)
            async with lock_held_at_commit(monkeypatch, engine, story):
                refused = await asyncio.wait_for(call(), WITHIN_S)
            _busy(refused, story)
            assert await _footprint(engine, story) == before, name
            monkeypatch.undo()
            again = await call()
            assert again.status_code in (200, 201), (name, again.text)


async def test_library_lock_timeout_is_story_busy(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#472: ``Engine.respond`` and ``Engine.ingest`` raise ``StoryBusy`` (``lock_timeout``,
    ``retry_after``), not the driver's ``OperationalError``, with nothing written."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        turn = RespondRequest(
            actor_character_id=ids[PLAYER], player_input=PlayerInputBody(say="Hi.")
        )
        recorded = IngestRequest.model_validate(
            {"events": [{"client_event_id": "lib-lt", "kind": "narration", "text": "Wind."}]}
        )
        calls: list[Callable[[], Awaitable[Any]]] = [
            lambda: lib.respond(AUTHOR, story, turn),
            lambda: lib.ingest(AUTHOR, story, recorded),
        ]
        for call in calls:
            before = await _footprint(engine, story)
            with pytest.raises(StoryBusy) as raised:
                async with lock_held_at_commit(monkeypatch, engine, story):
                    await asyncio.wait_for(call(), WITHIN_S)
            assert raised.value.details == {"story_id": story, "reason": "lock_timeout"}
            assert raised.value.retry_after == operation.LOCK_RETRY_AFTER_S
            assert isinstance(raised.value.__cause__, OperationalError)
            assert await _footprint(engine, story) == before
            monkeypatch.undo()
            await call()


async def _terminate(engine: AsyncEngine, conn: AsyncConnection) -> None:
    """End the commit's own server process: the connection is lost, as in an outage."""
    pid = int(await conn.scalar(text("SELECT pg_backend_pid()")) or 0)
    async with engine.connect() as probe:
        await probe.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})


async def test_lost_connection_is_still_database_unavailable(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#472, NFR-REL-3: a commit whose connection is lost is still 503 ``database_unavailable``
    on REST and the driver's ``OperationalError`` in the library, not ``story_busy``."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client, PLAYER, FIRST)
        before = await _footprint(engine, story)

        async def lost(conn: AsyncConnection) -> None:
            await _terminate(engine, conn)

        _hook(monkeypatch, lost)
        answer = await respond(client, story, body(ids[PLAYER], "Gone."))
        assert answer.status_code == 503, answer.text
        assert answer.json()["error"]["code"] == "database_unavailable"
        assert "Retry-After" not in answer.headers

        _hook(monkeypatch, lost)
        turn = RespondRequest(
            actor_character_id=ids[PLAYER], player_input=PlayerInputBody(say="Gone again.")
        )
        with pytest.raises(OperationalError):
            await lib.respond(AUTHOR, story, turn)
        assert await _footprint(engine, story) == before
