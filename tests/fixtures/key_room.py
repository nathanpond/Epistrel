"""The key room fixture (docs/04 §7's S-SEQUENTIAL; #174).

A structured story with Cal, the player, and Bobby and Jenni, the two primaries, all three in
the study (perception ``co_location``), switched to per-character renders
(``multi_character.renders = per_character``) under per-character partitions with testimony at
``defaulted`` (#162). The study holds the brass key and the desk drawer (items). Canon says
nothing about who holds the key: the turn's own beats decide it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from tests import multi_support as ms
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.structured_turn_support import structured_story

from epistrel.core.propositions import store


@dataclass(frozen=True)
class KeyRoom:
    story_id: str
    cal: str
    bobby: str
    jenni: str
    study: str
    key: str
    drawer: str


async def key_room(
    client: Any, engine: AsyncEngine, *, model_calls: int | None = None, **settings: Any
) -> KeyRoom:
    """The story (see the module docstring); ``model_calls``: its ``budgets.model_calls``."""
    story, ids = await structured_story(client, "Cal", "Bobby", "Jenni")
    await ts.enable_testimony(engine, story, "defaulted", level="co_location")
    await ms.per_character(engine, story, model_calls=model_calls, **settings)
    study = await ps.place_entity(engine, story, "study")
    for name in ("Cal", "Bobby", "Jenni"):
        await ps.put(engine, story, ids[name], study)
    async with engine.begin() as conn:
        key = await store.add_entity(conn, story, "item", "the brass key")
        drawer = await store.add_entity(conn, story, "item", "the desk drawer")
    return KeyRoom(story, ids["Cal"], ids["Bobby"], ids["Jenni"], study, key, drawer)


__all__ = ["KeyRoom", "key_room"]
