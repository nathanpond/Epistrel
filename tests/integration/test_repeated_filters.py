"""A plain string in a library list filter is one value (#324), as one query parameter is over
REST: the issue's examples against PostgreSQL. The structural half, over every such parameter
of ``Engine``, is in ``tests/test_repeated_filters.py``."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine, MediaUpload, RelationRequest, RequestContext
from epistrel.config import Settings
from epistrel.core.config_changes import ConfigScope
from epistrel.core.tables.ownership import EntityType
from tests.auth_support import auth_settings
from tests.media_support import metadata, png
from tests.respond_support import new_story, respond_client
from tests.story_support import WRITE

CTX = RequestContext(principal="p-repeated")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_config_changes_take_a_plain_setting_name(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``settings="turn_queue_max"`` lists that setting's change (it listed nothing: one
    predicate per character). Complement asserted: the list form answers the same rows."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _ = await new_story(client)
        patched = await client.patch(
            f"/stories/{story}/settings", json={"turn_queue_max": 4}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        plain = await lib.config_changes(
            CTX, scope=ConfigScope.STORY, story_id=story, settings="turn_queue_max"
        )
        listed = await lib.config_changes(
            CTX, scope=ConfigScope.STORY, story_id=story, settings=["turn_queue_max"]
        )
        assert [c.setting for c in plain.items] == ["turn_queue_max"]
        assert plain.items == listed.items


async def test_list_stories_takes_a_plain_relation_filter(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``relation="editor:p-…"`` filters by that relation (it was refused: "the colon is
    missing", from parsing ``"e"``). Complement asserted: a story without it is not listed."""
    async with respond_client(stub_settings, engine) as (client, lib):
        related, _ = await new_story(client)
        other, _ = await new_story(client)
        await lib.add_relation(
            CTX, EntityType.STORY, related, RelationRequest(principal="p-rep-ed", relation="editor")
        )
        found = {s.id for s in await lib.list_stories(CTX, relation="editor:p-rep-ed")}
        assert related in found
        assert other not in found
        assert found == {s.id for s in await lib.list_stories(CTX, relation=["editor:p-rep-ed"])}


async def test_media_content_takes_a_plain_entity_tag(
    settings: Settings, engine: AsyncEngine
) -> None:
    """``if_none_match=<the tag>`` answers ``not_modified`` (it served the bytes again: each
    character was compared with the tag). Complement asserted: another tag serves the bytes."""
    lib = Engine(settings, db=engine)
    try:
        request = MediaUpload.model_validate(metadata(caption="Repeated filters"))
        written = await lib.upload_media(CTX, request, png(5, 4), content_type="image/png")
        first = await lib.get_media_content(CTX, written.asset.id)
        etag = first.etag
        await first.aclose()
        assert etag is not None
        cached = await lib.get_media_content(CTX, written.asset.id, if_none_match=etag)
        assert cached.not_modified
        fresh = await lib.get_media_content(CTX, written.asset.id, if_none_match='"other"')
        assert not fresh.not_modified
        await fresh.aclose()
    finally:
        await lib.models.aclose()
