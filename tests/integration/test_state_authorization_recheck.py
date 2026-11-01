"""#188's rebase re-check (``state_authorization``; FR-STATE-10, FR-CONC-16): a structured turn's
effects are authorized again at commit under the commit-time definitions. Unmarked: the gated
oracle of the commit path is ``test_config_revalidation__m6``; this pins the entry itself."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import states
from epistrel.core.readset import REBASE_CHECKS, StructuredReadSet
from epistrel.core.turns.beats import DeclaredEffect, TurnBeat
from epistrel.orchestrator.structured import effects
from tests import canon_support as c
from tests import state_support as ss
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration


async def test_state_authorization_recheck(engine: AsyncEngine, states_delivered: None) -> None:
    """The entry is registered beside #187's. It passes while the turn's effects are still
    authorized, and fails (the turn regenerates) once the State's sources no longer admit the
    asserting character, or a cap lowered under the turn would cut its effect."""
    assert effects.recheck_authorization in REBASE_CHECKS
    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    (gold,) = await ss.define_all(
        engine, story, ss.gold(authorization={"sources": ["any_character", "director"]})
    )
    beat = TurnBeat(
        kind="do",
        beat=2,
        text="Cal pays for the meal.",
        character_id=cal,
        effects=(DeclaredEffect("gold", "a meal", scope_key=cal, delta=-30),),
    )
    async with engine.connect() as conn:
        values = await states.current_values(conn, story, [gold])
    book = effects.Book(story, states.ON, {"gold": gold}, values)
    read_set = StructuredReadSet()
    effects.remember(read_set, book, [beat])
    op: Any = SimpleNamespace(read_set_source=read_set)

    async def recheck() -> bool:
        async with engine.connect() as conn:
            return await effects.recheck_authorization(conn, op)

    async def update(patch: dict[str, Any]) -> None:
        async with engine.begin() as conn:
            await states.update_definition(conn, story, "gold", patch, principal=c.PRINCIPAL)

    await ss.change(engine, story, ss.set_to("gold", 100, scope_key=cal, reason="a purse"))
    assert await recheck()
    await update({"authorization": {"sources": ["any_character"], "caps": {"per_effect": 10}}})
    assert not await recheck()  # a cap that would now cut the effect
    await update({"authorization": {"caps": None}})  # a merge patch: the cap removed
    assert await recheck()
    await update({"authorization": {"sources": ["director"]}})
    assert not await recheck()  # the character's source revoked
