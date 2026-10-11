"""Helpers for the structured-output and provider-failure tests (#53): deployment settings for
structured results, and what a story's operations and events look like afterwards."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.orchestrator.models.registry import GuardrailCheckInputs, GuardrailVerdict

CHECK = GuardrailCheckInputs(text="the text being checked", policy_prompt="no dragons")
POLICY_PROMPT = "Refuse anything about dragons."


def nothing_conclusive() -> GuardrailVerdict | None:
    return None


@asynccontextmanager
async def structured_settings(engine: AsyncEngine, **values: Any) -> AsyncIterator[None]:
    """Set keys of ``deployment_state.settings`` (moving ``config_revision`` so no cache keeps
    the old values) and remove them afterwards: the tests share one database."""

    async def write(document: str, keys: list[str]) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE deployment_state SET settings = (settings - CAST(:keys AS text[])) "
                    "|| CAST(:doc AS jsonb), config_revision = config_revision + 1"
                ),
                {"keys": keys, "doc": document},
            )

    await write(json.dumps(values), [])
    try:
        yield
    finally:
        await write("{}", list(values))


@dataclass(frozen=True)
class StoryState:
    timeline_revision: int
    statuses: dict[str, str]
    """operation id → status, for every non-genesis operation."""
    events: dict[str, list[str]]
    """operation id → its events' types."""
    payloads: str
    """Every event payload of the story, as text."""


async def story_state(engine: AsyncEngine, story_id: str) -> StoryState:
    async with engine.connect() as conn:
        revision = await conn.scalar(
            text("SELECT timeline_revision FROM stories WHERE id = :s"), {"s": story_id}
        )
        statuses = {
            row.operation_id: row.status
            for row in await conn.execute(
                text(
                    "SELECT operation_id, status::text AS status FROM operations "
                    "WHERE story_id = :s AND kind <> 'genesis'"
                ),
                {"s": story_id},
            )
        }
        events: dict[str, list[str]] = {}
        payloads: list[str] = []
        for row in await conn.execute(
            text(
                "SELECT operation_id, type, payload::text AS payload FROM events "
                "WHERE story_id = :s ORDER BY seq"
            ),
            {"s": story_id},
        ):
            events.setdefault(row.operation_id, []).append(row.type)
            payloads.append(row.payload)
    return StoryState(int(revision), statuses, events, "\n".join(payloads))
