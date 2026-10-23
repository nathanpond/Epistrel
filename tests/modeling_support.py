"""Helpers for the lazy-modeling tests (#163): a street scene under per-character partitions
(perception ``full``, model salience), the scripted salience answer naming characters, and the
characters and memories read back."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from tests import perception_support as ps
from tests import testimony_support as ts
from tests.event_support import rows
from tests.structured_turn_support import structured_story


async def street(
    client: Any, engine: AsyncEngine, *names: str, salience: str | None = "rules_and_model"
) -> tuple[str, dict[str, str], str]:
    """A structured story (the first name the player, the second the rendered character) under
    partitions at perception ``full`` with everyone on the street; answers the story, the ids by
    name and the street's id."""
    story, ids = await structured_story(client, *(names or ("Wren", "Jenni")))
    await ts.enable_testimony(engine, story, "defaulted", salience=salience)
    place = await ps.place_entity(engine, story, "street")
    for character_id in ids.values():
        await ps.put(engine, story, character_id, place)
    return story, ids, place


def salient_characters(*names: str) -> str:
    """A scripted ``salience_detection`` answer marking nothing salient but ``names`` as
    characters to model."""
    return json.dumps({"salient": [], "texture": [], "characters": list(names)})


async def characters_of(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The story's characters by name, with their weight, age and where it came from, and the
    position they were modeled at."""
    found = await rows(
        engine,
        "SELECT id, name, weight, true_age, true_age_source, definition, source_catalog_id, "
        "modeled_turn, modeled_beat, is_player FROM characters WHERE story_id = :s "
        "ORDER BY ordinal",
        s=story_id,
    )
    return {str(r.name): r for r in found}


async def memory_texts(engine: AsyncEngine, story_id: str, holder: str) -> list[tuple[int, str]]:
    """``holder``'s routed memories as ``(turn, text)``."""
    return [
        (int(m.narrative_turn), str(m.text))
        for m in await ps.memories_of(engine, story_id, holder)
        if m.narrative_turn is not None
    ]


async def modeled_events(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT payload, narrative_turn, beat, category FROM events WHERE story_id = :s "
        "AND type = 'character_modeled' ORDER BY seq",
        s=story_id,
    )


__all__ = ["characters_of", "memory_texts", "modeled_events", "salient_characters", "street"]
