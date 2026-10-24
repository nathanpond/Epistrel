"""Regressions found by P-04's multi-character workload (#182) in the commit path of a
lazily resolved exposure (#164).

A lazy resolution decides in the exposure's frame: the witness's partition as it stood there.
A belief the witness holds only from a later position is not in that partition, so the
resolution's transition found no belief to reuse and inserted its row, and only then did
``_insert_belief`` find the later belief open and refuse (``held_already``). The resolver lets
the said belief stand on that refusal, so the transition's row was left without its belief, and
the turn's commit failed on the deferred foreign key. The refusal now comes before anything is
written."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.epistemic import transitions
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Proposition
from tests import canon_support as c
from tests import partition_support as parts

pytestmark = pytest.mark.integration


async def test_a_frame_transition_refused_as_held_already_writes_nothing(
    engine: AsyncEngine,
) -> None:
    """Ann holds "Bobby is in the garden" from turn 5; a resolution in a frame at turn 2 that
    would write the same belief is refused ``held_already`` before writing its transition, so
    the operation that lets it stand commits.

    Complement asserted: Ann's only transition is the one of the belief she holds."""
    w = await c.world(engine)
    ann = w.characters["Ann"]
    garden = Proposition(
        subject_id=w.people["Bobby"],
        predicate_id=c.LOCATED_AT,
        object_id=w.places["garden"],
        valid_time=Interval.of(None, None),
    )
    later = await parts.believe(
        engine, w.story_id, ann, garden, kind=OperationKind.CONTROL, held_from=Position(5)
    )
    settings = await parts.partitioned(engine, w.story_id)
    found: dict[str, Any] = {}
    async with Operation(
        engine,
        story_id=w.story_id,
        kind=OperationKind.CONTROL,
        principal=c.PRINCIPAL,
        bumps_revision=True,
    ) as op:
        op.append("belief_authored", {"holder_character_id": ann})

        async def step(conn: AsyncConnection) -> None:
            act = await conn.scalar(
                text("SELECT id FROM events WHERE operation_id = :o AND type = 'belief_authored'"),
                {"o": op.operation_id},
            )
            try:
                await transitions.transition(
                    conn,
                    op,
                    holder=ann,
                    kind=transitions.Kind.AUTHOR,
                    proposition=garden,
                    antecedent=transitions.Antecedent.record("events", str(act)),
                    position=Position(2, step=1),
                    frame=Position(2),
                    settings=settings,
                )
            except InvalidRequest as refused:  # the resolver lets the said belief stand
                found["reason"] = (refused.details or {}).get("reason")

        op.add_commit_step(step)
        await op.commit()
    assert found["reason"] == "held_already"
    written = await parts.rows_of(
        engine,
        "SELECT belief_id FROM belief_transitions WHERE story_id = :s AND holder_character_id = :h",
        s=w.story_id,
        h=ann,
    )
    assert [r.belief_id for r in written] == [later.id]
