"""Helpers for the idempotency tests (#61): keyed respond calls and what a story committed."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.event_support import rows
from tests.respond_support import respond
from tests.story_support import count

KEY = "Idempotency-Key"
REPLAY = "Idempotent-Replay"


async def keyed(
    client: httpx.AsyncClient, story_id: str, payload: dict[str, Any], key: str
) -> httpx.Response:
    """``POST /stories/{id}/respond`` under ``key``."""
    return await respond(client, story_id, payload, headers={KEY: key})


async def committed_turns(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's committed turn operations, oldest first."""
    return await rows(
        engine,
        "SELECT operation_id, idempotency_key, narrative_turn FROM operations "
        "WHERE story_id = :s AND kind = 'turn' AND status = 'committed' ORDER BY started_at",
        s=story_id,
    )


async def turn_statuses(engine: AsyncEngine, story_id: str) -> list[str]:
    found = await rows(
        engine,
        "SELECT status FROM operations WHERE story_id = :s AND kind = 'turn' ORDER BY started_at",
        s=story_id,
    )
    return [str(r.status) for r in found]


async def key_row(engine: AsyncEngine, key: str) -> Any:
    """The ``idempotency_keys`` row of ``key`` (keys are unique across the tests' stories)."""
    (row,) = await rows(engine, "SELECT * FROM idempotency_keys WHERE key = :k", k=key)
    return row


async def operations_of(engine: AsyncEngine, story_id: str) -> int:
    return await count(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story_id)


def message_of(response: httpx.Response) -> tuple[str, str]:
    """``(message_id, text)`` of a respond answer."""
    message = response.json()["message"]
    return message["message_id"], message["text"]


__all__ = [
    "KEY",
    "REPLAY",
    "committed_turns",
    "key_row",
    "keyed",
    "message_of",
    "operations_of",
    "turn_statuses",
]
