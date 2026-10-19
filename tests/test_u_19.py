"""U-19 (M4): resolution obligations (FR-COMMIT-14; docs/04 §3; #154). One file per test ID and
milestone; U-19's M5 part (the knowledge obligations: "Bobby knows he knocked" and the search
over his later sincere lines) joins it."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.commit import lifecycle
from epistrel.core.commit.reconcile import Conflict, Ok, obligations_of, reconcile
from epistrel.core.enums import RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.propositions.registry import load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from tests import commit_support as c
from tests import resolution_support as r

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "FR-COMMIT-14")
async def test_u_19(engine: AsyncEngine) -> None:
    """Binding the porch knock (a subject-open thunk at story-minute t) to Bobby derives, from
    the author rule and ``located_at``'s functional key, the retroactive obligations "Bobby was
    at the porch at t" (implied) and "Bobby was nowhere else at t" (exclusive); the conflict
    search finds the recorded contrary position (Bobby at the school over t), so the binding is
    a ``Conflict`` citing that fact, and ``depend`` rejects it without writing anything: the
    thunk stays open. Complement: the mailman, who has no history, binds freely: ``Ok``, and the
    dependence commits the resolution and its obligation together, ``provenance = resolution``,
    anchored to the thunk at t."""
    p = await r.porch_story(engine)
    thunk = await r.introduce_knock(engine, p)
    k = p.knock
    bobby = lifecycle.binding_of(thunk, r.knocked(p, k.bobby, r.KNOCK_AT + 300))
    assert bobby.valid_time.at == r.KNOCK_AT  # type: ignore[union-attr]  # the thunk's own time

    async with engine.connect() as conn:
        registry = await load(conn, p.story_id)
        derived = await obligations_of(
            KeyedLookup(conn, StructuredReadSet()), p.story_id, bobby, registry
        )
        found = await reconcile(conn, p.story_id, thunk, bobby)
    implied = [o.proposition for o in derived if o.kind == "implied"]
    assert [(x.subject_id, x.predicate_id, x.object_id) for x in implied] == [
        (k.bobby, r.LOCATED_AT, p.porch)
    ]
    assert implied[0].valid_time.at == r.KNOCK_AT  # type: ignore[union-attr]
    exclusive = [o.proposition for o in derived if o.kind == "exclusive"]
    assert [x.predicate_id for x in exclusive] == [r.LOCATED_AT]  # one place at a time
    assert isinstance(found, Conflict)
    assert found.conflicting_records == (p.school_fact,)
    assert found.bound_entity == k.bobby
    assert p.school_fact in found.context

    async def bind(who: str) -> lifecycle.Dependence:
        async def step(conn: AsyncConnection, op: Operation) -> lifecycle.Dependence:
            return await lifecycle.depend(
                conn, op, thunk.id, r.knocked(p, who, r.KNOCK_AT + 300), beat=2, channel="do"
            )

        found = await c.in_turn(engine, p.story_id, step)
        assert isinstance(found, lifecycle.Dependence)
        return found

    rejected = await bind(k.bobby)
    assert rejected.kind == "rejected"
    assert rejected.fact is None
    assert isinstance(rejected.reconciliation, Conflict)
    assert (await c.thunk(engine, p.story_id, thunk.id)).open
    facts = await c.rows(engine, "facts", p.story_id)
    assert all(f.thunk_id is None for f in facts)  # nothing written for the rejected binding

    # Complement: a binding with no recorded history reconciles and commits with its obligation.
    async with engine.connect() as conn:
        free = await reconcile(
            conn, p.story_id, thunk, lifecycle.binding_of(thunk, r.knocked(p, k.mailman, 900))
        )
    assert isinstance(free, Ok)
    assert [(x.subject_id, x.object_id) for x in free.to_commit] == [(k.mailman, p.porch)]
    resolved = await bind(k.mailman)
    assert resolved.kind == "committed"
    assert resolved.fact is not None
    assert resolved.fact.provenance is RecordProvenance.RESOLUTION
    assert resolved.fact.valid_from == r.KNOCK_AT
    assert len(resolved.obligations) == 1
    written = {f.id: f for f in await c.rows(engine, "facts", p.story_id)}
    obligation = written[resolved.obligations[0]]
    assert (obligation.subject_id, obligation.predicate_id) == (k.mailman, r.LOCATED_AT)
    assert str(obligation.provenance) == RecordProvenance.RESOLUTION.value
    assert obligation.thunk_id == thunk.id
    assert obligation.valid_from == r.KNOCK_AT
    assert not (await c.thunk(engine, p.story_id, thunk.id)).open


@pytest.mark.part("M4", "FR-COMMIT-14")
async def test_u_19__no_rule_no_obligation(engine: AsyncEngine) -> None:
    """Without the author rule the binding implies nothing about Bobby's position: no implied
    obligation, and with no recorded ``knocked_at`` history the binding reconciles (the rule is
    what the conflict rests on, never world knowledge)."""
    p = await r.porch_story(engine, with_rule=False)
    thunk = await r.introduce_knock(engine, p)
    binding = lifecycle.binding_of(thunk, r.knocked(p, p.knock.bobby, r.KNOCK_AT))
    async with engine.connect() as conn:
        found = await reconcile(conn, p.story_id, thunk, binding)
    assert isinstance(found, Ok)
    assert [o.kind for o in found.obligations] == []
    assert found.to_commit == ()
