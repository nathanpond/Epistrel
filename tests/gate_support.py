"""Helpers for the input-gate tests (#56): the fixture caller over REST, story-scoped input
policies, and ``audit_log`` reads."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine
from epistrel.core.context import RequestContext
from epistrel.core.policies.model import Policy
from epistrel.orchestrator.policies.schemas import StoryPolicyItem
from tests.event_support import rows
from tests.policy_support import unique
from tests.story_support import WRITE

AUTHOR = RequestContext(principal="p-author")
ENTRY_POINTS = ("respond", "ingest", "pinned_memories", "custom_instructions")


async def gated(
    client: httpx.AsyncClient,
    story_id: str,
    entry_point: str,
    texts: list[str],
    **fields: Any,
) -> httpx.Response:
    """The fixture caller (``POST /_test/stories/{id}/gated-input``)."""
    headers = {**WRITE, **fields.pop("headers", {})}
    return await client.post(
        f"/_test/stories/{story_id}/gated-input",
        json={"entry_point": entry_point, "texts": texts, **fields},
        headers=headers,
    )


async def input_policies(lib: Engine, story_id: str, *items: dict[str, Any]) -> tuple[Policy, ...]:
    """Story-scoped policies applying to text input (each item's fields over the defaults)."""
    parsed = [
        StoryPolicyItem.model_validate(
            {"name": unique("input"), "applies_to": ["text_input"], "message": "No.", **item}
        )
        for item in items
    ]
    return (await lib.policies_set_story(AUTHOR, story_id, parsed)).policies


async def audit_rows(engine: AsyncEngine, *, story_id: str | None = None) -> list[Any]:
    """``audit_log`` rows (all, or one story's), oldest first."""
    if story_id is None:
        return await rows(engine, "SELECT * FROM audit_log ORDER BY at, id")
    return await rows(
        engine, "SELECT * FROM audit_log WHERE story_id = :s ORDER BY at, id", s=story_id
    )


async def audit_count(engine: AsyncEngine) -> int:
    return len(await audit_rows(engine))


__all__ = [
    "AUTHOR",
    "ENTRY_POINTS",
    "audit_count",
    "audit_rows",
    "gated",
    "input_policies",
]
