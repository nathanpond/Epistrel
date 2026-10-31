"""S-FAMILY: a scenario filled from the catalog (docs/04 §7), its M6 part (#202).

The M5 part lives in ``tests/scenario/test_s_family.py`` (one file per test ID and milestone).
The scenario is the Sunday Dinner of ``tests/seeded_clock_support``: the family of
``tests/seeded_partitions_support`` with a per-sibling allowance, a starting clock (Sunday
1987-10-25, 14:00) and the world's 18:00 dinner. Played through the REST app with the stub
adapter.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import genesis_support as g
from tests import seeded_clock_support as sc
from tests import seeded_partitions_support as sp
from tests.auth_support import auth_settings
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _get(client: Any, path: str, **params: str) -> Any:
    response = await client.get(path, params=params, headers=g.READ)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.part("M6", "FR-SCN-9; FR-CLOCK-3")
async def test_s_family__m6(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The family's starting clock, its State trackers and the pre-scheduled dinner come from
    the scenario, read through the story's own API right after creation.

    - **The per-sibling allowance**: ``GET /states`` lists ``allowance`` with each sibling's
      starting value (Tom 20, Ruth 15, Sam 10), the parents at the State's initial 0.
    - **The starting clock**: ``GET /clock`` reads offset 0 at Sunday 1987-10-25, 14:00, the
      scenario's skip cap (90) and ``time_awareness`` (``approximate``) among its settings.
    - **The dinner**: ``GET /scheduled-events`` lists the world's "the Sunday dinner",
      ``pending`` at offset 240 (18:00), for the whole family; the Sister's viewpoint holds it
      (she takes part), the neighbour's does not.

    Complement: the same scenario created without States and the clock has none of the three
    (its States read is 409 ``module_off``, an unanchored clock with the default cap, no event)."""
    async with respond_client(stub_settings, engine) as (client, _lib):
        characters = await sp.cast(client)
        scenario_id, _ = await g.published_scenario(client, sc.family())
        made = await g.create_from(
            client, sp.request(scenario_id, characters, await sc.seeded_profile(client))
        )
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        base = f"/stories/{story}"

        listed = await _get(client, f"{base}/states", viewpoint="omniscient")
        (allowance,) = [s for s in listed["states"] if s["name"] == "allowance"]
        values = allowance["values"]
        assert {role: values[by_role[role]] for role in sc.SIBLINGS} == sc.ALLOWANCES
        assert values[by_role["mother"]] == 0

        clock = await _get(client, f"{base}/clock")
        assert clock["offset"] == 0
        assert clock["anchor"]["display"] == "Sunday, 1987-10-25, 14:00 (afternoon)"
        assert clock["settings"]["max_unprompted_skip_minutes"] == 90
        assert clock["settings"]["time_awareness"] == "approximate"

        found = await _get(client, f"{base}/scheduled-events", viewpoint="omniscient")
        (dinner,) = found["events"]
        assert (dinner["name"], dinner["status"], dinner["planned"], dinner["owner"]) == (
            "the Sunday dinner",
            "pending",
            sc.DINNER_AT,
            "world",
        )
        family = {by_role[r] for r in (*sp.NPC_ROLES, "player")}
        assert set(dinner["participants"]) == family
        ruth = await _get(
            client, f"{base}/scheduled-events", viewpoint=f"character:{by_role['sister']}"
        )
        assert [e["event_id"] for e in ruth["events"]] == [dinner["event_id"]]
        pike = await _get(
            client, f"{base}/scheduled-events", viewpoint=f"character:{by_role['neighbour']}"
        )
        assert pike["events"] == []

        # -- complement: without the modules, none of it
        plain = await g.create_from(
            client, sp.request(scenario_id, characters, await sp.partitioned_profile(client))
        )
        other = f"/stories/{plain['story_id']}"
        clock = await _get(client, f"{other}/clock")
        assert clock["anchor"] is None
        assert clock["settings"]["max_unprompted_skip_minutes"] == 1440
        assert (await _get(client, f"{other}/scheduled-events", viewpoint="omniscient"))[
            "events"
        ] == []
        off = await client.get(
            f"{other}/states", params={"viewpoint": "omniscient"}, headers=g.READ
        )
        assert (off.status_code, off.json()["error"]["code"]) == (409, "module_off")
        assert set(await sc.definitions(engine, plain["story_id"])) == {"world_clock"}
