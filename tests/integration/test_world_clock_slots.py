"""The shared inter-turn slot opener (#193; ``core.world_clock.slots``).

Two slots opened after turn 2 get ``slot_k`` 1 and 2, on turn 2's active variant, and their
events carry the slot; a truncation to turn 2 discards both (and the boundary ``2⁺1`` keeps the
first). Complements: a slot before the first turn sits after turn 0, a slot number is never
reused after a truncation, and only an ``inter_turn`` operation may be a slot.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.events.types import ClockAdvanced
from epistrel.core.membership import Position
from epistrel.core.world_clock.slots import open_inter_turn, slot_position
from tests import canon_support as c
from tests.event_support import rows

pytestmark = pytest.mark.integration


async def _slot(engine: AsyncEngine, story: str) -> tuple[Operation, Position]:
    found: list[Position] = []
    op = open_inter_turn(engine, story, source=Provenance.CONSUMER, principal=c.PRINCIPAL)
    async with op:
        op.append(
            "clock_advanced",
            ClockAdvanced(source="consumer", minutes=0),
            provenance=Provenance.CONSUMER,
        )

        async def step(conn: object) -> None:
            found.append(slot_position(op))

        op.add_commit_step(step)
        await op.commit()
    return op, found[0]


async def _turn(engine: AsyncEngine, story: str) -> Operation:
    async def nothing(conn: object, op: Operation) -> None:
        return None

    op, _ = await c.run(engine, story, nothing)
    return op


async def _membership(engine: AsyncEngine, *operation_ids: str) -> list[str]:
    found = {
        r.operation_id: r.membership
        for r in await rows(
            engine,
            "SELECT operation_id, membership::text AS membership FROM operations "
            "WHERE operation_id = ANY(:ids)",
            ids=list(operation_ids),
        )
    }
    return [found[i] for i in operation_ids]


async def test_inter_turn_slots(engine: AsyncEngine) -> None:
    world = await c.world(engine)
    story = world.story_id

    # Before the first turn: a slot after turn 0.
    early, at_zero = await _slot(engine, story)
    assert at_zero == Position(0, 0, 1, 0, 0)

    await _turn(engine, story)
    second = await _turn(engine, story)
    first_slot, one = await _slot(engine, story)
    second_slot, two = await _slot(engine, story)
    assert (one.turn, one.slot_k, two.turn, two.slot_k) == (2, 1, 2, 2)
    assert one.variant == two.variant == second.turn_variant
    stored = await rows(
        engine,
        "SELECT o.operation_id, o.kind::text AS kind, o.narrative_turn, o.slot_k, "
        "e.slot_k AS event_slot FROM operations o JOIN events e USING (operation_id) "
        "WHERE o.operation_id = ANY(:ids) ORDER BY o.slot_k",
        ids=[first_slot.operation_id, second_slot.operation_id],
    )
    assert [(r.kind, r.narrative_turn, r.slot_k, r.event_slot) for r in stored] == [
        ("inter_turn", 2, 1, 1),
        ("inter_turn", 2, 2, 2),
    ]

    # A truncation to 2⁺1 keeps the first slot; one to 2 discards both (a later turn makes 2
    # a turn before the head).
    await _turn(engine, story)
    await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=2, slot=1), principal=c.PRINCIPAL
    )
    assert await _membership(engine, first_slot.operation_id, second_slot.operation_id) == [
        "active",
        "discarded",
    ]
    await _turn(engine, story)
    await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=2), principal=c.PRINCIPAL
    )
    assert await _membership(engine, first_slot.operation_id, second_slot.operation_id) == [
        "discarded",
        "discarded",
    ]
    assert await _membership(engine, early.operation_id) == ["active"]

    # Complement: a new slot after turn 2 takes a number no slot there has had.
    _, third = await _slot(engine, story)
    assert (third.turn, third.slot_k) == (2, 3)

    # Complement: only an inter_turn operation of a story may be a slot.
    with pytest.raises(InvalidRequest):
        Operation(
            engine, story_id=story, kind=OperationKind.CONTROL, principal=c.PRINCIPAL, slot=True
        )
