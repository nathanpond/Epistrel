"""#620 (the owner's decision of 2026-11-05, "(b) now, (a) later"): a turn's lazy resolutions are
capped by the statements their transaction spends, not only by their number. No resolution
starts once the transaction has spent ``Pass.statements`` on them; the spend is shared by every
pass in one commit; the exposures left over stay unresolved, and the pass reports ``capped``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.epistemic import exposure
from epistrel.core.events import Operation
from tests import canon_support as c
from tests import exposure_support as xs
from tests import partition_support as parts
from tests.memory_support import use_structured
from tests.profile_support import store_overrides

pytestmark = pytest.mark.integration

PARTITIONED = {
    "character_knowledge": {"partitions": True, "lazy_resolution": True, "testimony": "defaulted"},
    "perception": {"level": "co_location"},
}


async def _exposed(engine: AsyncEngine) -> tuple[str, str, list[str], Any]:
    """Ann, told by Bobby where Cal was three times (one functional key): the story, Ann, the
    three exposure ids and the claim a later need names."""
    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    await store_overrides(engine, w.story_id, PARTITIONED)
    ann, bobby, cal = w.characters["Ann"], w.characters["Bobby"], w.people["Cal"]
    ids = [
        (
            await xs.expose(
                engine, w.story_id, ann, c.at(cal, w.places[place], None, None), source=bobby
            )
        )[0]
        for place in ("kitchen", "garden", "school")
    ]
    return w.story_id, ann, ids, c.at(cal, w.places["garden"], None, None)


async def _need(
    engine: AsyncEngine, story: str, witness: str, claim: Any, passes: list[exposure.Pass]
) -> int:
    """A turn whose commit runs ``passes`` in order on ``witness``'s need for ``claim``. Answers
    the statements the transaction spent on resolutions."""
    settings = await parts.partitioned(engine, story)
    spent: list[int] = []
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=c.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "where was Cal?"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "Let me think."})

        async def step(conn: AsyncConnection) -> None:
            for budget in passes:
                await exposure.resolve_needed(
                    conn, op, witness, [claim], budget=budget, settings=settings
                )
            spent.append(exposure.resolution_statements(conn))

        op.add_commit_step(step)
        await op.commit()
    return spent[0]


async def _unresolved(engine: AsyncEngine, story: str, ids: list[str]) -> int:
    return sum([await xs.state_of(engine, story, i) == "unresolved" for i in ids])


async def test_no_resolution_starts_past_the_statement_budget(engine: AsyncEngine) -> None:
    """A budget of one statement lets the first resolution start and no other. A second pass in
    the same commit shares the spend, so it resolves nothing either. Both report ``capped``.

    Complement asserted: with the default budget and a count cap of three, all three resolve,
    and the default budget then still has room."""
    story, ann, ids, claim = await _exposed(engine)
    first, second = exposure.Pass(statements=1), exposure.Pass(statements=1)
    spent = await _need(engine, story, ann, claim, [first, second])
    assert (len(first.made), first.capped) == (1, True)
    assert (len(second.made), second.capped) == (0, True)
    assert spent > 1  # the one resolution's own statements, counted after it ran
    assert await _unresolved(engine, story, ids) == 2

    story, ann, ids, claim = await _exposed(engine)
    free = exposure.Pass(cap=3)
    spent = await _need(engine, story, ann, claim, [free])
    assert (len(free.made), free.capped) == (3, False)
    assert 0 < spent < exposure.MAX_LAZY_RESOLUTION_STATEMENTS_PER_TURN
    assert await _unresolved(engine, story, ids) == 0


async def test_a_zero_budget_resolves_nothing(engine: AsyncEngine) -> None:
    """``statements=0``: nothing resolves, nothing is spent, and the pass is capped."""
    story, ann, ids, claim = await _exposed(engine)
    none = exposure.Pass(statements=0)
    assert await _need(engine, story, ann, claim, [none]) == 0
    assert (none.made, none.capped) == ([], True)
    assert await _unresolved(engine, story, ids) == 3
