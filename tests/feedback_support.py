"""Helpers for the message-feedback tests (#112): the feedback routes over REST, a played turn's
message, a scratch database for the tests that change the deployment's feedback settings, and
the stored rows."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.db import make_engine
from epistrel.engine import Engine
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.event_support import rows
from tests.respond_support import body, new_story, respond_client

RATER = "p-rater-a"
OTHER_RATER = "p-rater-b"
SEEDED_TAGS = (
    "out_of_character",
    "repetitive",
    "forgot_something",
    "factual_error",
    "ignored_my_action",
    "too_long",
    "too_short",
    "should_have_been_blocked",
    "other",
)
"""FR-FB-1's nine reason tags, as the requirement lists them."""


def as_principal(principal: str | None) -> dict[str, str]:
    """The credential, and ``principal`` as the requesting principal (none: no header)."""
    return bearer() | ({"X-Epistrel-Principal": principal} if principal is not None else {})


def feedback_path(story_id: str, message_id: str) -> str:
    return f"/stories/{story_id}/messages/{message_id}/feedback"


async def rate(
    client: httpx.AsyncClient,
    story_id: str,
    message_id: str,
    payload: Any,
    principal: str | None = RATER,
    **headers: str,
) -> httpx.Response:
    return await client.put(
        feedback_path(story_id, message_id),
        json=payload,
        headers=as_principal(principal) | headers,
    )


async def read(
    client: httpx.AsyncClient, story_id: str, message_id: str, principal: str | None = RATER
) -> httpx.Response:
    return await client.get(feedback_path(story_id, message_id), headers=as_principal(principal))


async def retract(
    client: httpx.AsyncClient, story_id: str, message_id: str, principal: str | None = RATER
) -> httpx.Response:
    return await client.delete(feedback_path(story_id, message_id), headers=as_principal(principal))


def answered(response: httpx.Response, status: int) -> dict[str, Any]:
    """The JSON body of a response that must have ``status``."""
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


def error_of(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


@dataclass(frozen=True)
class Played:
    """A story with a player character and one played turn: the turn's respond answer."""

    story_id: str
    actor: str
    cast: dict[str, str]
    turn: dict[str, Any]

    @property
    def message_id(self) -> str:
        found: str = self.turn["message"]["message_id"]
        return found


async def play(
    client: httpx.AsyncClient, story_id: str, actor: str, say: str = "Is the ferry running?"
) -> dict[str, Any]:
    """One turn of ``story_id`` played by ``actor``: the respond answer."""
    response = await client.post(
        f"/stories/{story_id}/respond",
        json=body(actor, say),
        headers=as_principal("p-author"),
    )
    return answered(response, 200)


async def played_story(client: httpx.AsyncClient, **fields: Any) -> Played:
    """A new three-character story (the first the player's) with one turn played."""
    story_id, cast = await new_story(client, **fields)
    actor = next(iter(cast.values()))
    return Played(story_id, actor, cast, await play(client, story_id, actor))


@asynccontextmanager
async def scratch_feedback(
    pg_url: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine, AsyncEngine]]:
    """The REST app and its Engine on a fresh database migrated to head (dropped afterwards),
    for a test that changes the deployment's feedback settings or ages its rows: the shared
    database keeps FR-FB-1's nine tags and the default retention."""
    with migrated_database(pg_url, f"epistrel_feedback_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            async with respond_client(settings, db) as (client, lib):
                yield client, lib, db
        finally:
            await db.dispose()


async def feedback_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's ratings, by turn, variant and principal."""
    return await rows(
        engine,
        "SELECT * FROM message_feedback WHERE story_id = :s ORDER BY turn, variant, principal",
        s=story_id,
    )


async def history_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's rating history, oldest replacement first."""
    return await rows(
        engine,
        "SELECT * FROM feedback_history WHERE story_id = :s ORDER BY replaced_at, rated_at",
        s=story_id,
    )


__all__ = [
    "OTHER_RATER",
    "RATER",
    "SEEDED_TAGS",
    "Played",
    "answered",
    "as_principal",
    "error_of",
    "feedback_path",
    "feedback_rows",
    "history_rows",
    "play",
    "played_story",
    "rate",
    "read",
    "retract",
    "scratch_feedback",
]
