"""Helpers for the respond tests (#57): a REST client on the stub adapter, a two-character story,
respond calls, and reads of what the stub was sent and what the turn recorded."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.engine import Engine
from epistrel.orchestrator.models.stub import StubAdapter, StubCall
from tests.event_support import rows
from tests.story_support import WRITE, create, override, story_body

PLAYER = "Wren"
FIRST = "Ada"
SECOND = "Brann"
AUTHOR = RequestContext(principal="p-author")
WITHIN_S = 30.0
"""How long ``until`` waits for its condition: generous, so a loaded host only slows a test down
instead of failing it (#430, #432)."""


@asynccontextmanager
async def respond_client(
    settings: Settings, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    """The REST app over ``engine`` with the stub adapter; yields the client and the Engine."""
    app = create_app(settings)
    epistrel: Engine = app.state.epistrel
    epistrel.bind(engine)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, epistrel
    finally:
        # Queued turns (#63) are drained by tasks of this loop: let them finish first.
        await asyncio.wait_for(epistrel.drainer.idle(), 60)


async def new_story(
    client: httpx.AsyncClient,
    *names: str,
    overrides: dict[str, Any] | None = None,
    **fields: Any,
) -> tuple[str, dict[str, str]]:
    """A story whose first character is the player, with ``overrides`` of its engine profile's
    dials when given (#113); returns its id and ids by name."""
    created = await create(client, story_body(*(names or (PLAYER, FIRST, SECOND)), **fields))
    if overrides:
        await override(client, created["story_id"], overrides)
    return created["story_id"], {c["name"]: c["id"] for c in created["characters"]}


def body(actor: str, say: str | None = "Hello, who's there?", **fields: Any) -> dict[str, Any]:
    player_input: dict[str, Any] = {}
    if say is not None:
        player_input["say"] = say
    if "do" in fields:
        player_input["do"] = fields.pop("do")
    return {"actor_character_id": actor, "player_input": player_input, **fields}


async def respond(
    client: httpx.AsyncClient,
    story_id: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/respond", json=payload, headers={**WRITE, **(headers or {})}
    )


async def played(
    client: httpx.AsyncClient, story_id: str, payload: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    response = await respond(client, story_id, payload, **kwargs)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def assert_kept(response: httpx.Response, reply: str) -> None:
    """The turn committed the scripted ``reply`` on its first attempt (#419, #428). A 200 alone
    does not prove an armed erasure guard kept it: a refused reply is regenerated within the
    budget, the stub falls back to its default text, and the turn still commits."""
    assert response.status_code == 200, response.text
    turn = response.json()
    assert turn["message"]["text"] == reply, turn["message"]
    assert turn["call_counts"].get("generation") == 1, turn["call_counts"]


def generation_calls(stub: StubAdapter) -> list[StubCall]:
    return [call for call in stub.calls if call.purpose == "generation"]


def system_of(call: StubCall) -> str:
    assert call.messages[0].role == "system"
    return call.messages[0].content


def everything_sent(stub: StubAdapter) -> str:
    """Every message of every model call, as one text."""
    return "\n".join(m.content for call in stub.calls for m in call.messages)


async def story_events(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(engine, "SELECT * FROM events WHERE story_id = :s ORDER BY seq", s=story_id)


async def story_text(engine: AsyncEngine, story_id: str) -> str:
    """Every stored row of the story (events, transcript, operations) as text."""
    found = []
    for table in ("events", "transcript_messages", "operations"):
        found += await rows(
            engine,
            f"SELECT t::text AS row FROM {table} t WHERE story_id = :s",  # noqa: S608
            s=story_id,
        )
    return "\n".join(r.row for r in found)


async def until(
    condition: Callable[[], bool | Awaitable[bool]], what: str, within: float = WITHIN_S
) -> None:
    """Wait until ``condition()`` holds, polling; fail naming ``what`` after ``within`` seconds.
    Steps that wait on concurrent work wait on this, not on a fixed sleep or a wall-clock bound
    (#430, #432)."""

    async def holds() -> bool:
        found = condition()
        return bool(await found) if inspect.isawaitable(found) else bool(found)

    try:
        async with asyncio.timeout(within):
            while True:  # a database row or a stub's record: nothing to await, so poll
                if await holds():
                    return
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"not within {within} s: {what}")


__all__ = [
    "AUTHOR",
    "FIRST",
    "PLAYER",
    "SECOND",
    "WITHIN_S",
    "body",
    "everything_sent",
    "generation_calls",
    "new_story",
    "played",
    "respond",
    "respond_client",
    "story_events",
    "story_text",
    "system_of",
    "until",
]
