"""``GET /stories`` lists newest first, and its published schema says so (#314)."""

from __future__ import annotations

import json
import secrets

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings, bearer
from tests.conftest import REPO_ROOT
from tests.integration.ownership_fixtures import OWNER, create_story, ok
from tests.respond_support import respond_client


async def test_stories_list_newest_first(pg_url: str, engine: AsyncEngine) -> None:
    """Two stories of one fresh owner list the later one first, as the route's description and
    ``StoryList``'s description (``docs/openapi.json``) both say."""
    settings: Settings = auth_settings(pg_url, model_adapter="stub")
    owner = f"p-order-{secrets.token_hex(4)}"
    async with respond_client(settings, engine) as (client, _):
        first = await create_story(client, OWNER, owner)
        second = await create_story(client, OWNER, owner)
        listed = await ok(await client.get("/stories", params={"owner": owner}, headers=bearer()))
    assert [s["id"] for s in listed["items"]] == [second.id, first.id]

    schema = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert schema["components"]["schemas"]["StoryList"]["description"] == (
        "The stories, newest first."
    )
