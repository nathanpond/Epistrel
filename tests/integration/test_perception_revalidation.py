"""CONFIG-REVALIDATION, M5 part: perception routing (docs/04 §6; FR-CONC-16; #161).

A structured turn is held inside its render while the story's perception level changes (an
interpretive setting: ``perception_level`` is one of a structured turn's configuration
dependencies). Jenni whispers to Marcus with Bobby in the room.

- ``full`` → ``co_location`` while held: the turn re-checks and commits, routed under the new
  level (Bobby gets the words), recording it.
- ``co_location`` → ``full``: the same, Bobby now gets the envelope only.
- ``none`` → ``full`` (partitions switched on): re-checks silently and routes.
- ``full`` → ``none`` (partitions switched off): the turn was rendered for partitions that no
  longer exist, so its perception re-check fails and it regenerates; the commit writes the
  shared memories of an M4 profile (no routed memory).

Complement: an unrelated prospective change while held commits straight through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.profiles import schema
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration

WORDS = "Meet me at the pier at dawn."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _turn_row(engine: AsyncEngine, story_id: str) -> Any:
    (row,) = await rows(
        engine,
        "SELECT attempts, config_revisions FROM operations WHERE story_id = :s "
        "AND kind = 'turn' ORDER BY narrative_turn DESC LIMIT 1",
        s=story_id,
    )
    return row


@pytest.mark.part("M5", "FR-CONC-16")
async def test_config_revalidation__perception(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    perception_delivered: None,
) -> None:
    """See the module docstring."""
    assert schema.setting_tags()["perception.level"].change_class.value == "interpretive"
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        async def story_at(level: str) -> tuple[str, dict[str, str]]:
            story, ids = await structured_story(client, "Wren", "Jenni", "Marcus", "Bobby")
            await ps.perceive(engine, story, level)
            parlor = await ps.place_entity(engine, story, "parlor")
            for name in ("Wren", "Jenni", "Marcus", "Bobby"):
                await ps.put(engine, story, ids[name], parlor)
            return story, ids

        async def held(
            story: str, ids: dict[str, str], during: Callable[[], Awaitable[Any]]
        ) -> Any:
            stub.reset()
            whisper = render(beat("say", WORDS, intent="sincere", whisper_to=[ids["Marcus"]]))
            stub.script("generation", [whisper, whisper])
            latch = stub.hold("generation")
            task = asyncio.ensure_future(respond(client, story, body(ids["Wren"], "Well?")))
            await asyncio.wait_for(latch.entered.wait(), 5)
            await during()
            latch.release()
            response = await task
            assert response.status_code == 200, response.text
            return await _turn_row(engine, story)

        async def bobby(story: str, ids: dict[str, str]) -> list[tuple[str, str]]:
            return [
                (m.text, m.fidelity)
                for m in await ps.memories_of(engine, story, ids["Bobby"])
                if m.beat == 2
            ]

        # full → co_location: re-checked, committed, routed under the new level.
        story, ids = await story_at("full")
        row = await held(story, ids, lambda: ps.perceive(engine, story, "co_location"))
        assert [(a["outcome"], a.get("recheck")) for a in row.attempts] == [
            ("recheck", "passed"),
            ("commit", None),
        ]
        assert "perception_level" in row.attempts[0]["config_changed"]
        assert row.config_revisions["perception_level"] == "co_location"
        assert await bobby(story, ids) == [(f"Jenni says: {WORDS}", "content")]

        # co_location → full: Bobby now holds the envelope only.
        story, ids = await story_at("co_location")
        row = await held(story, ids, lambda: ps.perceive(engine, story, "full"))
        assert row.attempts[0]["outcome"] == "recheck"
        assert row.config_revisions["perception_level"] == "full"
        assert await bobby(story, ids) == [("Jenni whispers something to Marcus", "envelope")]

        # none → full: partitions switched on mid-turn re-check silently and route.
        story, ids = await story_at("none")
        row = await held(story, ids, lambda: ps.perceive(engine, story, "full"))
        first = row.attempts[0]
        assert (first["outcome"], first.get("recheck")) == ("recheck", "passed")
        assert await bobby(story, ids) == [("Jenni whispers something to Marcus", "envelope")]

        # full → none: partitions switched off mid-turn regenerate; no routed memory.
        story, ids = await story_at("full")
        row = await held(story, ids, lambda: ps.perceive(engine, story, "none"))
        assert [(a["outcome"], a.get("recheck")) for a in row.attempts] == [
            ("recheck", "failed"),
            ("commit", None),
        ]
        assert row.config_revisions["perception_level"] == "none"
        assert await ps.routed_memories(engine, story) == []
        shared = await rows(
            engine,
            "SELECT text FROM memories WHERE story_id = :s AND holder_character_id IS NULL "
            "AND beat = 2",
            s=story,
        )
        assert [m.text for m in shared] == [f"Jenni says: {WORDS}"]

        # Complement: a prospective change commits straight through.
        story, ids = await story_at("full")

        async def prospective() -> None:
            await set_overrides(
                engine,
                story,
                ProfileOverridesPatch(
                    settings={"verification": {"entailment_gate": 0.4}}, ignore_warnings=True
                ),
                principal="p-author",
                facts=ps.NO_FACTS,
            )

        row = await held(story, ids, prospective)
        assert [a["outcome"] for a in row.attempts] == ["commit"]
        assert row.config_revisions["perception_level"] == "full"
