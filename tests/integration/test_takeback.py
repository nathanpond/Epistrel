"""The M5 helpers take back what they leave on the shared database (#517): the story overrides of
``perceive``, ``enable_testimony``, ``enable_nested`` and ``per_character``, and the profiles
``partitioned_profile`` creates, which ``tests/conftest.py`` hands to ``takeback_support
.take_back`` after every test."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_sync_engine
from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID
from tests import canon_support as c
from tests import multi_support as ms
from tests import nested_support as ns
from tests import perception_support, takeback_support
from tests import seeded_partitions_support as sp
from tests import testimony_support as ts
from tests.event_support import execute_in_tx, rows
from tests.memory_support import use_structured
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


async def test_m5_helpers_record_and_take_back(
    engine: AsyncEngine, settings: Settings, pg_url: str
) -> None:
    """Each helper records what it wrote; ``take_back`` removes the override keys (keeping the
    story's others), moves the profile's stories to ``Structured`` and deletes the profile, and
    clears the record."""
    stories = []
    for _ in range(3):
        story = (await c.world(engine)).story_id
        await use_structured(engine, story)
        stories.append(story)
    perceived, testified, nested = stories
    await perception_support.perceive(engine, perceived, "co_location")
    await ts.enable_testimony(engine, testified, "defaulted", salience="rules", lazy=False)
    await ns.enable_nested(engine, nested)
    await ms.per_character(engine, nested)
    await execute_in_tx(
        engine,
        (
            "UPDATE story_profile_overrides SET settings = settings || "
            '\'{"pins": {"max_pinned": 10}}\'::jsonb WHERE story_id = :s',
            {"s": nested},
        ),
    )
    async with respond_client(settings, engine) as (client, _):
        profile = await sp.partitioned_profile(client, "Taken back (#517)")
    await execute_in_tx(
        engine,
        ("UPDATE stories SET engine_profile_id = :p WHERE id = :s", {"p": profile, "s": nested}),
    )

    overrides, profiles = takeback_support.pending()
    assert overrides == {
        perceived: {"character_knowledge", "perception"},
        testified: {"character_knowledge", "perception", "commitment"},
        nested: {"character_knowledge", "perception", "multi_character"},
    }
    assert profiles == {profile}

    sync = make_sync_engine(pg_url)
    try:
        with sync.begin() as conn:
            takeback_support.take_back(conn)
    finally:
        sync.dispose()

    assert takeback_support.pending() == ({}, set())
    left = await rows(
        engine,
        "SELECT story_id, settings FROM story_profile_overrides WHERE story_id = ANY(:s)",
        s=stories,
    )
    assert {str(r.story_id): dict(r.settings) for r in left} == {
        perceived: {},
        testified: {},
        nested: {"pins": {"max_pinned": 10}},
    }
    (moved,) = await rows(
        engine,
        "SELECT engine_profile_id, engine_profile_version_pin FROM stories WHERE id = :s",
        s=nested,
    )
    assert (moved.engine_profile_id, moved.engine_profile_version_pin) == (
        STRUCTURED_M4_PROFILE_ID,
        None,
    )
    (deleted,) = await rows(
        engine, "SELECT deleted_at FROM engine_profiles WHERE id = :p", p=profile
    )
    assert deleted.deleted_at is not None
