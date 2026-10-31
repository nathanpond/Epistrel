"""S-STATE-TIMETRAVEL: States rewind and fork (docs/04 §7; FR-STATE-4; #185, #187).

Every S-STATE-TIMETRAVEL M6 part lives in this file: #185's store part
(``test_s_state_timetravel__store``, driven through ``core.states``) and #187's turn part
(``test_s_state_timetravel__turn``, the same story through the render's effects channel).

The store part: the player's gold (1,000 at the start) is spent at turn 810 (a sword, -500) and
at turn 900 (a shield, -200), each turn a player message written through ``Operation`` with the
change applied in its commit. Truncating to 800 restores the as-of-800 balance; a fork at 850
holds only the 810 purchase, and spending in either story never reaches the other; after a new
turn over the truncated tail, undoing with a swap brings the old continuation back and the
balance follows it. Complement: the dormant continuation's purchase is absent from the active
value and folded by the ``as_recorded`` view.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import states
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from tests import canon_support as c
from tests import state_support as ss
from tests.event_support import rows
from tests.fork_support import fork
from tests.memory_support import use_structured
from tests.projection_support import swap, truncate

pytestmark = pytest.mark.integration

START, SWORD, SHIELD, BREAD, CLOAK, RING = 1000, 500, 200, 10, 100, 50


async def _message(engine: AsyncEngine, story: str, text: str) -> None:
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=c.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": text}, provenance=Provenance.PLAYER)
        await op.commit()


async def _messages_to(engine: AsyncEngine, story: str, turn: int) -> None:
    """Plain turns until the story's head is ``turn``."""
    found = await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
    for n in range(int(found[0].next_turn_index), turn + 1):
        await _message(engine, story, f"message {n}")


async def _spend(engine: AsyncEngine, story: str, player: str, amount: int, what: str) -> int:
    """A turn in which the player spends ``amount``; answers its turn number."""
    op, (applied,) = await ss.change(
        engine, story, ss.delta("gold", -amount, scope_key=player, reason=what)
    )
    assert not isinstance(applied, states.Violation), applied
    assert op.narrative_turn is not None
    return op.narrative_turn


@pytest.mark.part("M6", "FR-STATE-4")
async def test_s_state_timetravel__store(engine: AsyncEngine, states_delivered: None) -> None:
    w = await c.world(engine)
    story, player = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(engine, story, ss.gold(initial_value=START))

    await _messages_to(engine, story, 809)
    assert await _spend(engine, story, player, SWORD, "a sword") == 810
    await _messages_to(engine, story, 899)
    assert await _spend(engine, story, player, SHIELD, "a shield") == 900
    after = START - SWORD - SHIELD
    assert await ss.value(engine, story, "gold", player) == after
    assert await ss.value(engine, story, "gold", player, through=ss.at(800)) == START
    assert await ss.value(engine, story, "gold", player, through=ss.at(850)) == START - SWORD

    # Fork at 850: only the 810 purchase; then each story spends on its own.
    forked = await fork(engine, story, at_turn=850)
    branch = forked.story_id
    branch_player = next(f.id for f in forked.characters if f.name == "Cal")
    assert await ss.value(engine, branch, "gold", branch_player) == START - SWORD
    await _spend(engine, branch, branch_player, CLOAK, "a cloak")
    await _spend(engine, story, player, RING, "a ring")
    assert await ss.value(engine, branch, "gold", branch_player) == START - SWORD - CLOAK
    assert await ss.value(engine, story, "gold", player) == after - RING
    mine = {r.reason for r in await ss.change_rows(engine, story)}
    theirs = {r.reason for r in await ss.change_rows(engine, branch)}
    assert "a cloak" in theirs - mine
    assert "a ring" in mine - theirs
    assert not {r.id for r in await ss.change_rows(engine, story)} & {
        r.id for r in await ss.change_rows(engine, branch)
    }
    assert await ss.stored_values(engine, branch) == await ss.folded_values(engine, branch)

    # Truncate to 800: the as-of-800 balance, and the projection agrees.
    assert await truncate(engine, story, 800) is not None
    assert await ss.value(engine, story, "gold", player) == START
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)
    recorded = START - SWORD - SHIELD - RING
    assert await ss.value(engine, story, "gold", player, view="as_recorded") == recorded

    # A new turn over the tail (bread), then undo with a swap: the balance follows the active
    # continuation.
    await _spend(engine, story, player, BREAD, "bread")
    assert await ss.value(engine, story, "gold", player) == START - BREAD
    assert await swap(engine, story) is not None
    assert await ss.value(engine, story, "gold", player) == after - RING
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)
    # Complement: the dormant continuation's purchase is out of the active value and in the
    # as-recorded one.
    dormant = [r for r in await ss.change_rows(engine, story) if r.reason == "bread"]
    assert [r.membership for r in dormant] == ["dormant"]
    assert await ss.value(engine, story, "gold", player, view="as_recorded") == Decimal(
        recorded - BREAD
    )
    # The fork never saw any of it.
    assert await ss.value(engine, branch, "gold", branch_player) == START - SWORD - CLOAK
