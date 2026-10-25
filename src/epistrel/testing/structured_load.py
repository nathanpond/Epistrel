"""Structured turns under load (#153): what P-04's structured variant and P-08's structured and
repair-heavy rows share.

A structured load story is a story on the seeded ``Structured`` profile (``use_structured``)
with planted canon (``plant``): two items of its own, ``PLANTED_ITEM`` and ``NOVEL_ITEM``, and
for each character it names, "this character owns ``PLANTED_ITEM``" for all story-time, an
author fact committed through ``Engine.commit_fact``. Nothing in canon mentions ``NOVEL_ITEM``.

The stub's structured renders then declare claims against that canon (``render_claims``, the
``stub_render_claims`` setting): "I do not own the ``PLANTED_ITEM``" is ``contradicted`` (the
planted fact, negated), so the repair ladder runs, and with no value in the beat's text to edit
it reaches rung 3, a ``regeneration`` call; "I own the ``NOVEL_ITEM``" is ``novel``, so the
permission check runs (the first time; once permitted it is canon and later claims are
``supported``). P-04's structured variant declares each on 10% of the renders, P-08's
repair-heavy row the contradiction on half of them.

Nothing in the Engine imports this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.mutation import FactRequest
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.seeds.profiles import STRUCTURED_PROFILE_ID
from epistrel.core.tables import stories

PLANTED_ITEM = "brass lantern"
"""The item every planted character owns for all story-time."""
NOVEL_ITEM = "music box"
"""An item canon says nothing about."""
OWNS = CORE_BY_NAME["owns"].id
CONTRADICTED = {"subject": "I", "relation": "owns", "object": PLANTED_ITEM, "negated": True}
"""The rendering character denies owning what canon says it owns."""
NOVEL = {"subject": "I", "relation": "owns", "object": NOVEL_ITEM}
"""The rendering character claims what canon does not say."""
P04_SHARES = (0.10, 0.10)
"""P-04's structured variant: 10% of renders contradicted, 10% novel (the planner's mix)."""
REPAIR_HEAVY_SHARE = 0.5
"""P-08's repair-heavy row: half the renders contradict planted canon."""


def render_claims(contradicted: float, novel: float = 0.0) -> list[dict[str, Any]]:
    """The ``stub_render_claims`` setting for ``contradicted`` and ``novel`` shares of the
    renders (a share of 0 leaves that claim out)."""
    found: list[dict[str, Any]] = []
    if contradicted:
        found.append({"share": contradicted, **CONTRADICTED})
    if novel:
        found.append({"share": novel, **NOVEL})
    return found


async def use_structured(db: AsyncEngine, story_id: str) -> None:
    """Put a new story, before its first turn, on the seeded ``Structured`` profile's latest
    version (the scale fixture pins version 1: no turn has resolved its configuration yet)."""
    async with db.begin() as conn:
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(engine_profile_id=STRUCTURED_PROFILE_ID, engine_profile_version_pin=None)
        )


async def plant(
    engine: Any, ctx: RequestContext, story_id: str, characters: Iterable[str]
) -> dict[str, str]:
    """Add the two items to a structured story and commit, for each character id in
    ``characters``, that it owns ``PLANTED_ITEM`` for all story-time (``engine`` is the
    library ``Engine``). Answers the items' entity ids by name."""
    async with engine.db.begin() as conn:
        items = {
            name: await store.add_entity(conn, story_id, "item", name)
            for name in (PLANTED_ITEM, NOVEL_ITEM)
        }
    for character in characters:  # a character's entity has the character's id (#139)
        await engine.commit_fact(
            ctx,
            story_id,
            FactRequest(
                proposition=Proposition(
                    subject_id=character,
                    predicate_id=OWNS,
                    object_id=items[PLANTED_ITEM],
                    valid_time=Interval.of(None, None),
                )
            ),
        )
    return items


__all__ = [
    "CONTRADICTED",
    "NOVEL",
    "NOVEL_ITEM",
    "P04_SHARES",
    "PLANTED_ITEM",
    "REPAIR_HEAVY_SHARE",
    "plant",
    "render_claims",
    "use_structured",
]
