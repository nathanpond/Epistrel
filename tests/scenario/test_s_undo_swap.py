"""S-UNDO-SWAP, M2 part: undo a rewind after new turns were written (docs/04 §7; #66). One
turn per message: player-only turns written through ``Operation``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline, variants
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.variants import ChainRef, SwapRequest
from tests.concurrency_support import assert_single_chain, chain
from tests.event_support import new_story, rows
from tests.projection_support import snapshot

MESSAGES = 1_000
CHECKPOINT = 800
PRIMES = 50
PRINCIPAL = "p-player"


async def _message(engine: AsyncEngine, story: str, text: str) -> None:
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("player_input", {"text": text}, provenance=Provenance.PLAYER)
        await op.commit()


async def _operations(engine: AsyncEngine, story: str) -> dict[str, dict[str, Any]]:
    """Every committed non-control operation of the story, without ``membership_tx`` (a flip
    re-stamps it; the swap's row diff is about membership)."""
    found = await rows(
        engine,
        "SELECT * FROM operations WHERE story_id = :s AND status = 'committed' "
        "AND kind <> 'control'",
        s=story,
    )
    return {
        r.operation_id: {k: v for k, v in r._mapping.items() if k != "membership_tx"} for r in found
    }


async def _memberships(engine: AsyncEngine, story: str, variant: int) -> set[str]:
    found = await rows(
        engine,
        "SELECT DISTINCT membership FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND turn_variant = :v AND narrative_turn > :c",
        s=story,
        v=variant,
        c=CHECKPOINT,
    )
    return {r.membership for r in found}


async def _swap(engine: AsyncEngine, story: str, turn: int, variant: int) -> timeline.Swapped:
    return await timeline.swap(
        engine,
        story,
        SwapRequest(to_variant_chain=ChainRef(turn=turn, variant=variant)),
        principal=PRINCIPAL,
    )


@pytest.mark.part("M2", "FR-STORE-28")
async def test_s_undo_swap(engine: AsyncEngine) -> None:
    """Play to 1,000, truncate to 800, write 801'-850'; undoing the truncation defaults to a
    swap: 801-1,000 active, 801'-850' dormant, one active variant per position in one chain.
    Swapping back restores the exact prior state (row diff of operations and projections),
    and either chain can be selected for a fork. Complement: an undo with no new turns written
    is a plain reactivation, not a swap."""
    story = await new_story(engine)
    for n in range(1, MESSAGES + 1):
        await _message(engine, story, f"message {n}")
    await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=CHECKPOINT), principal=PRINCIPAL
    )
    for n in range(CHECKPOINT + 1, CHECKPOINT + PRIMES + 1):
        await _message(engine, story, f"message {n}'")
    await assert_single_chain(engine, story)
    assert await _memberships(engine, story, 1) == {"dormant"}  # shelved under 801'
    primes_ops = await _operations(engine, story)
    primes_projections = await snapshot(engine, story)

    undone = await timeline.untruncate(engine, story, principal=PRINCIPAL)
    assert undone.swapped
    assert undone.active_chain == ChainRef(turn=CHECKPOINT + 1, variant=1)
    assert (undone.restored_operations, undone.dormant_operations) == (MESSAGES - CHECKPOINT, 50)
    assert undone.next_turn_index == MESSAGES + 1
    await assert_single_chain(engine, story)
    assert await chain(engine, story) == [(n, 1) for n in range(1, MESSAGES + 1)]
    assert await _memberships(engine, story, 1) == {"active"}
    assert await _memberships(engine, story, 2) == {"dormant"}
    state = await timeline.undo_state(engine, story)
    assert state.undo_available  # a swap is undone by another swap
    assert [(c.from_turn, c.variant, c.through_turn) for c in state.dormant_chains] == [
        (CHECKPOINT + 1, 2, CHECKPOINT + PRIMES)
    ]

    back = await _swap(engine, story, CHECKPOINT + 1, 2)
    assert back.active_chain == ChainRef(turn=CHECKPOINT + 1, variant=2)
    assert back.next_turn_index == CHECKPOINT + PRIMES + 1
    await assert_single_chain(engine, story)
    assert await _operations(engine, story) == primes_ops
    assert await snapshot(engine, story) == primes_projections
    again = await _swap(engine, story, CHECKPOINT + 1, 2)  # already active: nothing written
    assert again.operation_id is None
    assert again.timeline_revision == back.timeline_revision

    # Promotion by fork (#67 copies): either chain is selectable, complete through its tip.
    original, through = await variants.fork_selection(
        engine, story, ChainRef(turn=CHECKPOINT + 1, variant=1), None
    )
    assert original is not None
    assert through == MESSAGES
    assert (original.branch, len(original.operation_ids)) == (CHECKPOINT, MESSAGES - CHECKPOINT)
    assert not set(original.operation_ids) & {
        op for op, row in primes_ops.items() if row["membership"] == "active"
    }
    active, through = await variants.fork_selection(engine, story, None, None)
    assert (active, through) == (None, CHECKPOINT + PRIMES)
    for wrong in (CHECKPOINT, MESSAGES + 1):
        with pytest.raises(InvalidRequest):
            await variants.fork_selection(
                engine, story, ChainRef(turn=CHECKPOINT + 1, variant=1), wrong
            )

    # Complement: no new turns over the tail, so undoing the truncation reactivates it.
    await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=CHECKPOINT + 10), principal=PRINCIPAL
    )
    plain = await timeline.untruncate(engine, story, principal=PRINCIPAL)
    assert (plain.swapped, plain.active_chain, plain.restored_operations) == (
        False,
        None,
        PRIMES - 10,
    )
    await assert_single_chain(engine, story)
    assert await _operations(engine, story) == primes_ops
