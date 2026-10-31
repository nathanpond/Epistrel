"""Changing a State's declaration (#185, owner decision 2026-10-30): ``update_definition`` is a
story configuration change. Interpretive and prospective members, the immutable ones, one
configuration revision per update and no timeline revision, the ``state_definitions``
dependency hash, and the refusals that write nothing."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import config_dependencies, states
from epistrel.core.enums import OperationKind, StateChangeSource, StateChangeStatus
from epistrel.core.errors import InvalidRequest, StateConstraintViolation, StateNotFound
from epistrel.core.membership import Position
from epistrel.core.states import model
from tests import canon_support as c
from tests import state_support as ss
from tests.event_support import rows
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration


async def _revisions(engine: AsyncEngine, story: str) -> tuple[int, int]:
    (row,) = await rows(
        engine, "SELECT config_revision, timeline_revision FROM stories WHERE id = :s", s=story
    )
    return int(row.config_revision), int(row.timeline_revision)


async def _changes(engine: AsyncEngine, story: str) -> list[Any]:
    return list(
        await rows(
            engine,
            "SELECT setting, class, old, new, config_revision FROM config_changes "
            "WHERE scope = 'story' AND scope_id = :s ORDER BY change_id",
            s=story,
        )
    )


async def _hash(engine: AsyncEngine, story: str) -> str:
    async with engine.connect() as conn:
        return await config_dependencies.state_definitions_hash(conn, story)


async def _update(engine: AsyncEngine, story: str, name: str, patch: dict[str, Any]) -> Any:
    async with engine.begin() as conn:
        return await states.update_definition(conn, story, name, patch, principal=c.PRINCIPAL)


async def test_u_state_update_definition(engine: AsyncEngine, states_delivered: None) -> None:
    """A prospective update (aliases, unit) and an interpretive one (constraints, visibility)
    each move the story's configuration revision by one and no timeline revision, and record
    one ``config_changes`` row per changed member with its class; only the interpretive one
    moves the ``state_definitions`` dependency hash, which a structured turn records. A patch
    that changes nothing writes nothing; an immutable member is ``immutable_field``, an unknown
    one ``unknown``; new bounds the current value breaks are ``state_constraint_violation``; an
    unknown State is 404. None of the refusals writes anything."""
    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(engine, story, ss.gold(constraints={"non_negative": True, "max": 100}))
    assert "state_definitions" in config_dependencies.STRUCTURED_DEPENDENCIES
    config0, timeline0 = await _revisions(engine, story)
    hash0 = await _hash(engine, story)
    logged = len(await _changes(engine, story))

    # Prospective: aliases and unit.
    updated = await _update(engine, story, "gold", {"aliases": ["coins", "crowns"], "unit": "cr"})
    assert (updated.aliases, updated.unit) == (("coins", "crowns"), "cr")
    assert await _revisions(engine, story) == (config0 + 1, timeline0)
    new = (await _changes(engine, story))[logged:]
    assert {(r.setting, r._mapping["class"], r.config_revision) for r in new} == {
        ("state:gold.aliases", "prospective", config0 + 1),
        ("state:gold.unit", "prospective", config0 + 1),
    }
    assert await _hash(engine, story) == hash0

    # Interpretive: constraints (a JSON merge patch) and visibility.
    updated = await _update(
        engine, story, "gold", {"constraints": {"max": 200}, "visibility": "hidden"}
    )
    assert updated.constraints.max == 200
    assert updated.constraints.non_negative
    assert updated.visibility.value == "hidden"
    assert await _revisions(engine, story) == (config0 + 2, timeline0)
    new = (await _changes(engine, story))[logged + 2 :]
    assert {(r.setting, r._mapping["class"]) for r in new} == {
        ("state:gold.constraints", "interpretive"),
        ("state:gold.visibility", "interpretive"),
    }
    constraints = next(r for r in new if r.setting == "state:gold.constraints")
    assert (constraints.old, constraints.new) == (
        {"max": 100, "non_negative": True},
        {"max": 200, "non_negative": True},
    )
    hash1 = await _hash(engine, story)
    assert hash1 != hash0
    async with engine.connect() as conn:
        stored = await states.definition(conn, story, "gold")
    assert stored == updated

    # Nothing changes: nothing is written.
    assert await _update(engine, story, "gold", {"unit": "cr"}) == updated
    assert await _revisions(engine, story) == (config0 + 2, timeline0)

    before = (await _revisions(engine, story), await _changes(engine, story))
    for member in model.IMMUTABLE_MEMBERS:
        with pytest.raises(InvalidRequest) as immutable:
            await _update(engine, story, "gold", {member: "x"})
        assert immutable.value.details["reason"] == "immutable_field"
    with pytest.raises(InvalidRequest) as unknown:
        await _update(engine, story, "gold", {"colour": "gold"})
    assert unknown.value.details["reason"] == "unknown"
    with pytest.raises(StateNotFound):
        await _update(engine, story, "silver", {"unit": "s"})

    # The current value already breaks the new bounds: refused, nothing written.
    await ss.change(engine, story, ss.set_to("gold", 150, scope_key=cal))
    timeline1 = (await _revisions(engine, story))[1]
    before = (await _revisions(engine, story), await _changes(engine, story))
    with pytest.raises(StateConstraintViolation) as broken:
        await _update(engine, story, "gold", {"constraints": {"max": 120}})
    assert broken.value.details["constraint"] == "max"
    assert broken.value.details["current"] == 150
    assert (await _revisions(engine, story), await _changes(engine, story)) == before
    assert await _hash(engine, story) == hash1
    # Widening is fine, and a removed bound is gone.
    updated = await _update(engine, story, "gold", {"constraints": {"max": None}})
    assert updated.constraints.max is None
    assert (await _revisions(engine, story)) == (config0 + 3, timeline1)


async def test_value_of_positions(engine: AsyncEngine, states_delivered: None) -> None:
    """``value_of(through=…)`` folds the changes at or before a position: checkpoint ``N``
    (``Position.end_of(N)``) leaves out ``N``'s inter-turn slots, ``N⁺k`` takes slots 1..k; a
    seed at position 0 and an initial value are the base; held and dropped changes never count;
    a story-scoped State takes no scope key and a per-character one needs one; an undefined
    value reads as ``Undefined``."""
    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(
        engine,
        story,
        ss.gold(),
        ss.gold(name="debt", scope="story", initial_value=None, aliases=(), unit=None),
    )
    seed = ss.set_to("gold", 7, scope_key=cal, source=StateChangeSource.SEED, position=Position(0))
    await ss.change(engine, story, seed, kind=OperationKind.CONTROL)
    op, _ = await ss.change(engine, story, ss.delta("gold", 3, scope_key=cal))
    turn = op.narrative_turn
    assert turn is not None
    slot = Position(turn, op.turn_variant or 0, 1)
    await ss.change(
        engine,
        story,
        ss.delta("gold", 10, scope_key=cal, source=StateChangeSource.CONSUMER, position=slot),
        ss.delta("gold", 100, scope_key=cal, status=StateChangeStatus.HELD, position=slot),
        ss.delta("gold", 1000, scope_key=cal, status=StateChangeStatus.DROPPED),
        kind=OperationKind.CONTROL,
        turn=turn,
    )
    assert await ss.value(engine, story, "gold", cal, through=ss.at(0)) == 7
    assert await ss.value(engine, story, "gold", cal, through=ss.at(turn)) == 10
    assert await ss.value(engine, story, "gold", cal, through=ss.at(turn, 1)) == 20
    assert await ss.value(engine, story, "gold", cal) == 20
    async with engine.connect() as conn:
        found = await states.value_of(conn, story, "gold", cal)
        assert isinstance(found, states.StateValue)
        assert (found.since, found.change_count) == (slot, 3)
        assert await states.value_of(conn, story, "debt") is states.Undefined
        with pytest.raises(InvalidRequest):
            await states.value_of(conn, story, "gold")
        with pytest.raises(InvalidRequest):
            await states.value_of(conn, story, "debt", cal)
    # A delta on an undefined value is refused.
    with pytest.raises(InvalidRequest):
        await ss.change(engine, story, ss.delta("debt", 1))
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)
