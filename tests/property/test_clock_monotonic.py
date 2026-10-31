"""CLOCK-MONOTONIC: the world clock never decreases along the active timeline, equals the fold
of its recorded active advances, and replay reproduces it (docs/04 §6; FR-CLOCK-1,2,12; #193).

Every CLOCK-MONOTONIC M6 function lives in this file (the traceability gate counts a test ID per
milestone in one file), one M6 marker each: #193's fold part here, and #194, #196, #197, #199
and #203 add ``__suffix`` functions for their clauses.

**In memory** (``test_clock_monotonic``, 300 examples in CI): any set of non-negative advances,
each at a position ``(turn, variant, slot_k, beat, step)``, on a timeline whose turns each have
one active variant. The story-time basis (``core.resolution.time.TimeBasis``, built from the
active advances as ``core.world_clock`` builds it) never decreases along the active timeline's
positions in order, its value after the last position is the sum of the active advances, and a
position of a turn's other variant (a regeneration in flight) leaves out the active variant's
advances of that turn. Complement: a backward advance is refused before it is written
(``clock_backward``).

**On Postgres** (``__postgres``): one seeded sequence on a Lite story running the clock at
``prompt_only``: turns that advance the clock in their commit (a pacing advance at beat 1),
consumer advances between turns (``POST /clock/advance``'s call, an inter-turn slot),
regenerations, truncations, untruncations, swaps and forks. After every step, in every story:
the clock at each active turn and slot, in order, never decreases; its value equals the sum of
the active counted ``world_clock`` changes; the ``state_values`` projection equals the fold; and
replay holds (the projection rebuilt from the recorded changes equals the stored one, and
reading the clock twice gives the same value: nothing is estimated). Complements: a backward
advance writes nothing; a truncated advance is absent from the active view and folded by
``as_recorded``.
"""

from __future__ import annotations

import os
import random
from collections import Counter
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states, world_clock
from epistrel.core.enums import StateChangeSource
from epistrel.core.errors import ClockBackward
from epistrel.core.events import Operation
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.resolution.time import TimeBasis
from tests import canon_support as c
from tests import clock_support as ck
from tests import state_support as ss
from tests.event_support import rows
from tests.fork_support import fork
from tests.projection_support import swap, truncate, untruncate

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 3_000 if NIGHTLY else 300

# -- in memory ------------------------------------------------------------------------------

advances = st.lists(
    st.tuples(
        st.integers(0, 8),  # turn
        st.integers(1, 2),  # variant
        st.integers(0, 2),  # slot_k
        st.integers(0, 3),  # beat
        st.integers(0, 2),  # step
        st.integers(0, 600),  # minutes
    ),
    max_size=25,
)


@pytest.mark.part("M6", "FR-CLOCK-1,2,12")
@settings(max_examples=EXAMPLES, deadline=None)
@given(advances, st.lists(st.integers(1, 2), min_size=9, max_size=9), st.integers(0, 10_000))
def test_clock_monotonic(
    recorded: list[tuple[int, int, int, int, int, int]], chosen: list[int], zero: int
) -> None:
    # Turn 0 (genesis and its slots) has no variant of its own.
    active_variant = {turn: (0 if turn == 0 else v) for turn, v in enumerate(chosen)}
    # A turn's other variant is dormant (a regenerate made it so): its advances are not active.
    active = [
        ((t, k, b, s), active_variant[t], m)
        for t, v, k, b, s, m in recorded
        if (0 if t == 0 else v) == active_variant[t]
    ]
    basis = TimeBasis(minute_zero=zero, advances=tuple(active))
    positions = sorted(
        {
            Position(t, active_variant[t], k, b, s)
            for t in range(9)
            for k in range(3)
            for b in range(4)
            for s in range(3)
        },
        key=Position.key,
    )
    values = [basis.at(p) for p in positions]
    assert values == sorted(values), "the clock decreased along the active timeline"
    assert basis.at(world_clock.HEAD) == zero + sum(m for _, _, m in active)
    # A position starts before its own advances (an advance ends its beat or slot).
    assert values[0] == zero
    assert [basis.through(p) for p in positions] == sorted(basis.through(p) for p in positions)
    # A regeneration in flight (the turn's other variant) reads no advance of the active one:
    # its whole turn sits at the clock the turns before it left.
    for turn in range(1, 9):
        other = 3 - active_variant[turn]
        earlier = zero + sum(m for key, _, m in active if key[0] < turn)
        assert basis.at(Position(turn, other)) == earlier
        assert basis.at(Position(turn, other, 0, POSITION_MAX, POSITION_MAX)) == earlier
        assert basis.at(Position.end_of(turn)) >= earlier


# -- Postgres ---------------------------------------------------------------------------------


async def _turn(
    engine: AsyncEngine,
    story: str,
    minutes: int,
    *,
    regenerate: bool = False,
) -> Operation:
    """A turn whose commit advances the clock ``minutes`` at beat 1 (as #194's pacing will)."""

    async def write(conn: AsyncConnection, op: Operation) -> None:
        assert op.narrative_turn is not None
        await world_clock.advance(
            conn,
            op,
            story,
            minutes=minutes,
            source=StateChangeSource.PACING,
            position=Position(op.narrative_turn, op.turn_variant or 0, 0, 1),
            reason="the exchange takes a while",
        )

    turn = None
    if regenerate:
        head = (await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))[0]
        turn = int(head.next_turn_index) - 1 or None
    op, _ = await c.run(engine, story, write, turn=turn)
    return op


async def _active_clock_sum(engine: AsyncEngine, story: str) -> int:
    found = await rows(
        engine,
        "SELECT coalesce(sum(c.delta), 0) AS total FROM state_changes c "
        "JOIN state_definitions d ON d.id = c.state_id WHERE c.story_id = :s "
        "AND d.name = 'world_clock' AND c.membership = 'active' "
        "AND c.status IN ('committed', 'converted')",
        s=story,
    )
    return int(found[0].total)


async def _active_positions(engine: AsyncEngine, story: str) -> list[Position]:
    """The ends of every active turn and inter-turn slot, in timeline order."""
    found = await rows(
        engine,
        "SELECT narrative_turn, turn_variant, coalesce(slot_k, 0) AS slot_k FROM operations "
        "WHERE story_id = :s AND status = 'committed' AND membership = 'active' "
        "AND narrative_turn IS NOT NULL",
        s=story,
    )
    return sorted(
        {
            Position(
                int(r.narrative_turn),
                int(r.turn_variant or 0),
                int(r.slot_k),
                POSITION_MAX,
                POSITION_MAX,
            )
            for r in found
        },
        key=Position.key,
    )


async def _clock_values(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        clock = await world_clock.clock_definition(conn, story)
    return [r for r in await ss.stored_values(engine, story) if r["state_id"] == clock.id]


async def _folded_clock(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        clock = await world_clock.clock_definition(conn, story)
    return [r for r in await ss.folded_values(engine, story) if r["state_id"] == clock.id]


async def _check(engine: AsyncEngine, story: str) -> None:
    values = [await ck.offset(engine, story, p) for p in await _active_positions(engine, story)]
    assert values == sorted(values), f"the clock decreased along the active timeline: {values}"
    now = await ck.offset(engine, story)
    assert now == await _active_clock_sum(engine, story)
    assert not values or values[-1] <= now
    # Replay: the projection rebuilt from the recorded advances is the stored one, and a
    # second read of the clock gives the same value (nothing estimated).
    assert await _clock_values(engine, story) == await _folded_clock(engine, story)
    assert await ck.offset(engine, story) == now


SEED = 193
STEPS = 30


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-1,2,12")
async def test_clock_monotonic__postgres(engine: AsyncEngine, clock_delivered: None) -> None:
    w = await c.world(engine)
    source = w.story_id
    await ck.set_level(engine, source, "prompt_only")
    chosen = random.Random(SEED)  # noqa: S311 - a seeded sequence, not a secret
    stories = [source]
    done: Counter[str] = Counter()
    await _turn(engine, source, 5)
    for _ in range(STEPS):
        story = stories[-1]
        roll = chosen.random()
        if roll < 0.4:
            regenerate = roll < 0.1
            await _turn(engine, story, chosen.randrange(0, 90), regenerate=regenerate)
            done["regenerate" if regenerate else "turn"] += 1
        elif roll < 0.55:
            await ck.advance(engine, story, chosen.randrange(0, 600))
            done["advance"] += 1
        elif roll < 0.7:
            head = (
                await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
            )[0]
            cut = chosen.randrange(0, max(1, int(head.next_turn_index) - 1))
            done["truncate"] += await truncate(engine, story, cut) is not None
        elif roll < 0.8:
            done["untruncate"] += await untruncate(engine, story) is not None
        elif roll < 0.9:
            done["swap"] += await swap(engine, story) is not None
        elif len(stories) < 3:
            forked = await fork(engine, story)
            await ck.set_level(engine, forked.story_id, "prompt_only")
            stories.append(forked.story_id)
            done["fork"] += 1
        for each in stories:
            await _check(engine, each)
    assert {"turn", "regenerate", "advance", "truncate", "untruncate", "swap", "fork"} <= {
        k for k, v in done.items() if v
    }, done

    # Complement: a backward advance is refused and writes nothing.
    story = stories[-1]
    before = await ck.clock_changes(engine, story)
    now = await ck.offset(engine, story)
    with pytest.raises(ClockBackward):
        await ck.advance(engine, story, to=now - 1)
    with pytest.raises(ClockBackward):
        await ck.advance(engine, story, -5)

    async def backward(conn: AsyncConnection, op: Operation) -> None:
        assert op.narrative_turn is not None
        await world_clock.advance(
            conn,
            op,
            story,
            to_offset=now - 1,
            source=StateChangeSource.CONSUMER,
            position=Position(op.narrative_turn, op.turn_variant or 0, 0, 1),
            reason="back",
        )

    with pytest.raises(ClockBackward):
        await c.run(engine, story, backward)
    assert await ck.clock_changes(engine, story) == before
    assert await ck.offset(engine, story) == now

    # Complement: a truncated advance leaves the active view and stays in as_recorded.
    await _turn(engine, story, 7)
    await _turn(engine, story, 11)
    head = (await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))[0]
    active_before = await ck.offset(engine, story)
    assert await truncate(engine, story, int(head.next_turn_index) - 2) is not None
    await _check(engine, story)
    assert await ck.offset(engine, story) == active_before - 11
    assert await ck.offset(engine, story, view=states.View.AS_RECORDED) >= active_before
