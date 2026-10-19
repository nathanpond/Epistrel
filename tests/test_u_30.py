"""U-30 (M4): resolution levels (FR-COMMIT-9, FR-STORE-29; docs/04 §3; #154). One file per test
ID and milestone."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline
from epistrel.core.canon import facts as canon
from epistrel.core.commit import boundary, lifecycle
from epistrel.core.commit.reconcile import Ok
from epistrel.core.enums import Membership, RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from tests import commit_support as c
from tests.canon_support import PRINCIPAL, fact_rows

pytestmark = pytest.mark.integration


def _handwriting(letter: str, predicate: str, value: str) -> Proposition:
    return Proposition(
        subject_id=letter,
        predicate_id=predicate,
        literal=TypedLiteral(kind=LiteralKind.STR, value=value),
        valid_time=Interval.of(0, None),
    )


@pytest.mark.part("M4", "FR-COMMIT-14; FR-STORE-29")
async def test_u_30(engine: AsyncEngine) -> None:
    """Turn 1 commits, at ``consequential``, that the drawer holds a letter and that Marcus
    wrote it: the letter's presence and author are fixed, its handwriting undetermined (no fact,
    no thunk). Turn 2 only mentions the handwriting (a ``say``): nothing is introduced or
    written. Turn 3's ``think`` depends on it: the detail becomes a thunk linked to the letter's
    fact and resolves once, through reconciliation, at ``incidental`` ("spidery"). Turn 4's
    ``do`` claiming "neat" is caught (``contradicted``: the detail has one value). Rewinding to
    turn 2 (past the resolving turn) makes the handwriting undetermined again (its fact
    ``revised`` and out of the timeline, its thunk gone with turn 3) while the letter and its
    author stay fixed: their rows are byte-identical, and a new turn 3 resolves the handwriting
    afresh ("neat")."""
    k = await c.knock_story(engine)
    story = k.story_id
    async with engine.begin() as conn:
        drawer = await store.add_entity(conn, story, "item", "drawer")
        letter = await store.add_entity(conn, story, "item", "letter")
        marcus = await store.add_entity(conn, story, "person", "Marcus")
        holds = (await registry.auto_register(conn, story, "holds_item")).id
        wrote = (await registry.auto_register(conn, story, "written_by")).id
        hand = (await registry.auto_register(conn, story, "handwriting_of", object_type="str")).id

    async def letter_facts(conn: AsyncConnection, op: Operation) -> list[canon.Fact]:
        return [
            await canon.commit_fact(
                conn,
                story,
                Proposition(
                    subject_id=s, predicate_id=p, object_id=o, valid_time=Interval.of(0, None)
                ),
                provenance=RecordProvenance.RENDER,
                operation=op,
                beat=2,
                resolution_level=boundary.CONSEQUENTIAL,
            )
            for s, p, o in ((drawer, holds, letter), (letter, wrote, marcus))
        ]

    there, author = await c.in_turn(engine, story, letter_facts)
    assert there.resolution_level == author.resolution_level == boundary.CONSEQUENTIAL

    async def detail(channel: str, value: str) -> boundary.Detail:
        async def step(conn: AsyncConnection, op: Operation) -> boundary.Detail:
            return await boundary.depend_on_detail(
                conn, op, there.id, _handwriting(letter, hand, value), beat=2, channel=channel
            )

        found = await c.in_turn(engine, story, step)
        assert isinstance(found, boundary.Detail)
        return found

    # Turn 2: a mention resolves nothing.
    mentioned = await detail("say", "spidery")
    assert mentioned.thunk_id is None
    assert await c.rows(engine, "thunks", story) == []
    assert [f for f in await c.rows(engine, "facts", story) if f.predicate_id == hand] == []

    # Turn 3: the first dependent think resolves it once, through reconciliation.
    resolved = await detail("think", "spidery")
    dependence = resolved.dependence
    assert isinstance(dependence, lifecycle.Dependence)
    assert dependence.kind == "committed"
    assert isinstance(dependence.reconciliation, Ok)
    assert dependence.fact is not None
    assert dependence.fact.resolution_level == boundary.INCIDENTAL
    assert dependence.fact.provenance is RecordProvenance.RESOLUTION
    (thunk_row,) = await c.rows(engine, "thunks", story)
    assert thunk_row.proposition_sketch["parent_fact_id"] == there.id
    assert thunk_row.narrative_turn == 3

    # Turn 4: a contradicting claim about the same detail is caught.
    caught = await detail("do", "neat")
    assert caught.thunk_id == resolved.thunk_id
    assert caught.dependence.kind == "contradicted"
    assert len([f for f in await c.rows(engine, "facts", story) if f.predicate_id == hand]) == 1

    # Rewind past the resolving turn: the detail is undetermined again; the letter stays.
    before = await fact_rows(engine, story)
    await timeline.truncate(engine, story, timeline.TruncateRequest(to_turn=2), principal=PRINCIPAL)
    after = await fact_rows(engine, story)
    for fact in (there, author):
        assert dict(after[fact.id]._mapping) == dict(before[fact.id]._mapping)
        assert after[fact.id].commit_state == "committed"
    spidery = after[dependence.fact.id]
    assert spidery.commit_state == "revised"
    assert spidery.membership == Membership.DISCARDED.value
    async with engine.connect() as conn:
        open_thunk = await lifecycle.read_thunk(conn, story, str(resolved.thunk_id))
        current: list[Any] = await canon.canon(conn, story, predicate=hand, subject=letter)
    assert open_thunk is not None
    assert open_thunk.state.value == "open"  # its row: discarded with turn 3
    assert current == []
    afresh = await detail("think", "neat")
    assert afresh.dependence.kind == "committed"
    assert afresh.thunk_id != resolved.thunk_id
    assert afresh.dependence.fact.proposition.literal.value == "neat"
