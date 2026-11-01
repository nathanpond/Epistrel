"""I-27: genesis by capability (docs/04 §4), its M6 part (#202).

The M3 parts live in ``tests/integration/test_i_27.py``, the M4 and M5 parts in
``test_i_27_m4.py`` and ``test_i_27_m5.py`` (traceability counts a test ID per milestone in one
file). The scenario is S-FAMILY's Sunday Dinner of ``tests/seeded_clock_support``: a
per-sibling allowance, a starting clock (Sunday 1987-10-25, 14:00) and the world's 18:00 dinner.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import states
from tests import genesis_support as g
from tests import seeded_clock_support as sc
from tests import seeded_partitions_support as sp
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.validation_support import validate

pytestmark = pytest.mark.integration

SEEDED_ROWS = {
    "starting_state.states[0]": "states",
    "cast.roles[2].state_initial_values": "states",
    "cast.roles[3].state_initial_values": "states",
    "cast.roles[5].state_initial_values": "states",
    "starting_state.clock": "clock",
    "starting_state.scheduled_events[0]": "clock",
    "starting_state.scheduled_events[1]": "clock",
}
"""The family's rows of the two capabilities (the brother, sister and player roles hold the
allowance values)."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _rows(found: dict[str, tuple[str, bool]]) -> dict[str, tuple[str, bool]]:
    return {ref: row for ref, row in found.items() if ref in SEEDED_ROWS}


@pytest.mark.part("M6", "FR-SCN-5,9,11; FR-STATE-3; FR-CLOCK-3,9")
async def test_i_27__m6(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Sunday Dinner created with States and the clock on.

    - **Publish** runs the State-schema validation (``state_schema`` is not ``unavailable``).
    - **The allowance trackers**: the ``allowance`` State is declared by genesis
      (``created_by = genesis``), its ``role:mother``/``role:father`` sources mapped to the
      parents' characters; each sibling's starting value is a ``set`` change with ``source =
      seed`` at position 0 of its character's scope (brother 20, sister 15, Sam 10), and the
      parents, who have none, hold the State's initial 0.
    - **The starting clock**: the ``world_clock`` carries the scenario's anchor (Sunday
      1987-10-25, 14:00), its ``max_unprompted_skip_minutes`` (90; #199) and its
      ``time_awareness`` (``approximate``; #200).
    - **The 18:00 dinner** is a ``pending`` world event planned four hours in, with every family
      member a participant and the dining room its place, its event named as the scenario does.
    - **The Mother's 15:00 errand** is her own event (owned by her character), its fact visible
      to her alone, and under partitions her ``author`` belief (``known``, at story minute -1)
      citing the errand's ``seed_materialized`` act, written through ``transition()``.
    - Every ``states`` and ``clock`` row has ``materialized_at`` and the report defers none.
    - **One operation**: a failure induced after the events and the States are written (the
      first initial value) leaves no story, no State, no event.

    Complements: a story created earlier under a profile without States and the clock keeps
    every such row deferred (#122's rows untouched, before and after the later creation), has
    only the default clock (unanchored, the 1440-minute cap) and no allowance or event."""
    async with respond_client(stub_settings, engine) as (client, _lib):
        characters = await sp.cast(client)
        body = sc.family(scheduled_events=[sc.DINNER, sc.ERRAND])
        scenario_id, version = await g.published_scenario(client, body)
        checked = (await validate(client, scenario_id, version=version)).json()
        assert checked["valid"] is True, checked
        assert "state_schema" not in checked["report"]["unavailable"], checked

        # -- complement first: no States, no clock (partitions only)
        plain_profile = await sp.partitioned_profile(client)
        plain = await g.create_from(client, sp.request(scenario_id, characters, plain_profile))
        before = _rows(await sc.deferred(engine, plain["story_id"]))
        assert before == {ref: (cap, False) for ref, cap in SEEDED_ROWS.items()}
        assert {d["field_ref"] for d in plain["report"]["deferred"]} >= set(SEEDED_ROWS)
        plain_defs = await sc.definitions(engine, plain["story_id"])
        assert set(plain_defs) == {"world_clock"}
        assert plain_defs["world_clock"].anchor is None
        assert plain_defs["world_clock"].max_unprompted_skip_minutes == 1440
        assert await sc.events(engine, plain["story_id"]) == []

        # -- with States and the clock
        profile = await sc.seeded_profile(client)
        made = await g.create_from(client, sp.request(scenario_id, characters, profile))
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        defs = await sc.definitions(engine, story)
        assert set(defs) == {"world_clock", "allowance"}
        allowance = defs["allowance"]
        assert (allowance.type, allowance.scope, allowance.created_by) == (
            "number",
            "character",
            "genesis",
        )
        assert allowance.authorization["sources"] == [
            f"character:{by_role['mother']}",
            f"character:{by_role['father']}",
            "author",
            "consumer",
        ]
        seeded = await sc.seeded_values(engine, story)
        assert seeded == {
            by_role[role]: ("allowance", "set", amount, 0) for role, amount in sc.ALLOWANCES.items()
        }
        async with engine.connect() as conn:
            for role in ("brother", "sister", "player", "mother"):
                found = await states.value_of(conn, story, "allowance", scope_key=by_role[role])
                assert isinstance(found, states.StateValue), role
                assert found.value == Decimal(sc.ALLOWANCES.get(role, 0)), role

        clock = defs["world_clock"]
        assert clock.anchor["display"] == "Sunday, 1987-10-25, 14:00 (afternoon)"
        assert clock.max_unprompted_skip_minutes == 90
        assert clock.time_awareness == "approximate"
        assert clock.stop_at_due_events is True

        dinner, errand = sorted(await sc.events(engine, story), key=lambda e: -e.planned)
        assert (errand.status.value, errand.planned, errand.owner, errand.participants) == (
            "pending",
            60,
            by_role["mother"],
            (by_role["mother"],),
        )
        (held,) = [
            b
            for b in await sp.beliefs(engine, story)
            if b.predicate == "scheduled" and str(b.object_id) != str(dinner.event_id)
        ]
        assert (str(held.holder), held.confidence_level, held.transition, held.act) == (
            by_role["mother"],
            "known",
            "author",
            "seed_materialized",
        )
        assert held.valid_at == -1
        (errand_fact,) = await sp.rows_of(
            engine,
            "SELECT f.visibility FROM facts f WHERE f.proposition_id = :p",
            p=errand.event_id,
        )
        assert errand_fact.visibility == {"scope": "characters", "characters": [by_role["mother"]]}
        assert (dinner.status.value, dinner.planned, dinner.owner) == (
            "pending",
            sc.DINNER_AT,
            "world",
        )
        assert set(dinner.participants) == set(by_role.values()) - {by_role["neighbour"]}
        entities = await sp.entity_ids(engine, story)
        assert dinner.place_id == entities["The dining room"]
        (fact,) = await sp.rows_of(
            engine,
            "SELECT p.object_id, f.provenance, f.visibility FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id WHERE p.id = :p",
            p=dinner.event_id,
        )
        assert await sc.entity_name(engine, str(fact.object_id)) == "the Sunday dinner"
        assert (fact.provenance, fact.visibility) == ("seed", {"scope": "objective"})

        rows = _rows(await sc.deferred(engine, story))
        assert rows == {ref: (cap, True) for ref, cap in SEEDED_ROWS.items()}
        assert made["report"]["deferred"] == []
        outcomes = {e["field_ref"]: e["outcomes"] for e in made["report"]["materialized"]}
        assert outcomes["starting_state.states[0]"] == ["state"]
        assert outcomes["starting_state.clock"] == ["clock"]
        assert outcomes["starting_state.scheduled_events[0]"] == ["canon", "event"]
        assert outcomes["starting_state.scheduled_events[1]"] == ["canon", "event", "belief"]

        # -- the earlier story's rows stay as they were
        assert _rows(await sc.deferred(engine, plain["story_id"])) == before

        # -- one operation: a failure after the States and the events leaves nothing
        counted = await g.counts(engine)

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("induced after the States and the events")

        monkeypatch.setattr(states, "apply_change", broken)
        body = sp.request(scenario_id, characters, profile)
        with pytest.raises(RuntimeError, match="induced"):
            await client.post("/stories", json=body, headers=g.WRITE)
        assert await g.counts(engine) == counted
        orphans = await sp.rows_of(
            engine,
            "SELECT (SELECT count(*) FROM state_definitions d WHERE NOT EXISTS "
            "(SELECT 1 FROM stories s WHERE s.id = d.story_id)) AS d, "
            "(SELECT count(*) FROM scheduled_events e WHERE NOT EXISTS "
            "(SELECT 1 FROM stories s WHERE s.id = e.story_id)) AS e",
        )
        assert (orphans[0].d, orphans[0].e) == (0, 0)
