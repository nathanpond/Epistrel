"""``GET /stories`` filters by engine profile and names each story's profile (#527).

A fresh profile runs three stories of one fresh owner: one follows the profile's latest version,
one is pinned to its first, and a third runs under the default profile. The list names each
story's ``engine_profile`` ``{id, version, pinned}``; ``engine_profile`` filters at any version
and ``engine_profile_version`` at one; the library answers what REST does.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.stories import StoryList
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import InvalidRequest
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from tests.auth_support import auth_settings, bearer
from tests.integration.ownership_fixtures import OWNER, create_story, headers, ok
from tests.profile_support import PROFILES, profile_body
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

EDIT = {"name": "Brisk Lite", "settings": {"pins": {"max_pinned": 10}}, "ignore_warnings": True}


def _profiles(listed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item["engine_profile"] for item in listed["items"]}


async def test_stories_list_by_engine_profile(pg_url: str, engine: AsyncEngine) -> None:
    settings: Settings = auth_settings(pg_url, model_adapter="stub")
    owner = f"p-profile-list-{secrets.token_hex(4)}"
    async with respond_client(settings, engine) as (client, lib):
        profile = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(OWNER)), 201)
        )["id"]
        follows = (await create_story(client, OWNER, owner)).id
        pinned = (await create_story(client, OWNER, owner)).id
        default = (await create_story(client, OWNER, owner)).id
        for story in (follows, pinned):
            await ok(
                await client.put(
                    f"/stories/{story}/engine-profile",
                    json={"profile_id": profile, "ignore_warnings": True},
                    headers=headers(OWNER),
                )
            )
        edited = await ok(
            await client.put(f"{PROFILES}/{profile}", json=EDIT, headers=headers(OWNER))
        )
        assert (edited["applied"], edited["result"]["version"]) == (True, 2)
        # A pin is what an unavailable upgrade leaves (#113); set it directly.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET engine_profile_version_pin = 1 WHERE id = :s"),
                {"s": pinned},
            )

        async def listed(**params: Any) -> dict[str, Any]:
            return await ok(await client.get("/stories", params=params, headers=bearer()))

        everything = _profiles(await listed(owner=owner))
        assert everything[follows] == {"id": profile, "version": 2, "pinned": False}
        assert everything[pinned] == {"id": profile, "version": 1, "pinned": True}
        assert everything[default]["id"] == LITE_PRESET_ID
        assert everything[default]["pinned"] is False

        any_version = await listed(engine_profile=profile)
        assert [item["id"] for item in any_version["items"]] == [pinned, follows], "newest first"
        assert _profiles(any_version) == {k: everything[k] for k in (follows, pinned)}
        assert [
            i["id"]
            for i in (await listed(engine_profile=profile, engine_profile_version=1))["items"]
        ] == [pinned]
        assert [
            i["id"]
            for i in (await listed(engine_profile=profile, engine_profile_version=2))["items"]
        ] == [follows]
        assert (await listed(engine_profile=profile, engine_profile_version=3))["items"] == []
        # The filter combines with the others; every filter must hold.
        combined = await listed(engine_profile=profile, owner=owner, status="active")
        assert [item["id"] for item in combined["items"]] == [pinned, follows]
        assert (await listed(engine_profile=profile, owner="p-nobody"))["items"] == []

        refused = await client.get(
            "/stories", params={"engine_profile_version": 1}, headers=bearer()
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["details"]["field"] == "query.engine_profile_version"
        for bad in (
            {"engine_profile": "not-a-ulid"},
            {"engine_profile": profile, "engine_profile_version": 0},
        ):
            assert (await client.get("/stories", params=bad, headers=bearer())).status_code == 422

        # The library answers what REST does (FR-API-3).
        ctx = RequestContext(principal=OWNER)
        for params in (
            {"engine_profile": profile},
            {"engine_profile": profile, "engine_profile_version": 1},
        ):
            rest = await listed(**params)
            library = StoryList.of(await lib.list_stories(ctx, **params)).model_dump(mode="json")
            assert library == rest, params
        with pytest.raises(InvalidRequest) as caught:
            await lib.list_stories(ctx, engine_profile_version=1)
        assert caught.value.details["field"] == "query.engine_profile_version"
        single = StoryList.of(
            await lib.list_stories(ctx, status=StoryStatus.ACTIVE, engine_profile=profile)
        )
        assert {item.id for item in single.items} == {follows, pinned}
