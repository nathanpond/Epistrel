"""I-27 M7 (FR-PROF-18, FR-SCN-11, FR-STORE-23; #231): a Lite story's deferred genesis content
arrives at position 0 when the story gains the capability, under either ``upgrade_mode``.

The Lite Sunday Dinner story (``tests/lite_genesis_support.sunday_dinner``) plays two turns and
is switched to Full. Its ``structured``, ``episodic`` and ``partitions`` items are materialized
once, as ``seed`` records at narrative position 0 (a nested seed only where nested beliefs are
on); its clock and States items wait (#231's follow-up).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.genesis.deferred import MATERIALIZED_HERE
from epistrel.core.seeds.profiles import FULL_PRESET_ID
from epistrel.orchestrator.jobs.handlers import backfill as backfill_job
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.integration.test_i_19 import run_backfill
from tests.respond_support import body as respond_body
from tests.respond_support import respond_client

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

WRITE = {**bearer(), "X-Epistrel-Principal": "p-i27-m7"}
READ = bearer()


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _dinner(client: Any, engine: AsyncEngine) -> tuple[str, str]:
    """The Lite Sunday Dinner story after two turns; its id and its player."""
    locked, default = await scenarios.cast_characters(client)
    mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
    scenario_id, _ = await g.published_scenario(client, lg.sunday_dinner(locked, default))
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
        ),
    )
    story = made["story_id"]
    player = str(
        await scalar(
            engine,
            "SELECT id FROM characters WHERE story_id = :s ORDER BY is_player DESC, ordinal "
            "LIMIT 1",
            s=story,
        )
    )
    for _ in range(2):
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(player), headers=WRITE
        )
        assert played.status_code == 200, played.text
    return story, player


async def _deferred(engine: AsyncEngine, story: str) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}
    for r in await rows(
        engine,
        "SELECT capability_needed, materialized_at, materialized_operation_id "
        "FROM genesis_deferred WHERE story_id = :s",
        s=story,
    ):
        found.setdefault(str(r.capability_needed), []).append(r)
    return found


async def _seeds(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT narrative_turn, membership::text AS m FROM facts "
        "WHERE story_id = :s AND provenance = 'seed'",
        s=story,
    )


def _assign(upgrade_mode: str) -> dict[str, Any]:
    return {"profile_id": FULL_PRESET_ID, "ignore_warnings": True, "upgrade_mode": upgrade_mode}


@pytest.mark.part("M7", "FR-PROF-18; FR-STORE-23")
async def test_i_27__m7_forward_only(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Switched to Full ``forward_only``, the story's deferred structured, episodic and
    partition items are materialized at once (active, at position 0, provenance ``seed``), each
    stamped with the operation that wrote it; the report names the counts and, the story having
    history, ``genesis_conflicts_unchecked``. The clock and States items stay deferred.
    Materializing again finds nothing: a later change writes no second copy."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, player = await _dinner(client, engine)
        before = await _deferred(engine, story)
        assert any(c in before for c in MATERIALIZED_HERE)
        assert all(r.materialized_at is None for rs in before.values() for r in rs)
        switched = await client.put(
            f"/stories/{story}/engine-profile", json=_assign("forward_only"), headers=WRITE
        )
        assert switched.status_code == 200, switched.text
        (entry,) = switched.json()["impact"]["affected_stories"]
        counts = entry["deferred_to_materialize"]["by_capability"]
        assert counts == {c: len(before[c]) for c in MATERIALIZED_HERE if c in before}
        assert entry["genesis_conflicts_unchecked"] is True
        after = await _deferred(engine, story)
        for capability, found in after.items():
            done = [r.materialized_at is not None for r in found]
            if capability in MATERIALIZED_HERE:
                # A nested seed waits for nested beliefs, which Full keeps off.
                assert any(done), capability
                stamped = {str(r.materialized_operation_id) for r in found if r.materialized_at}
                assert len(stamped) == 1
            else:
                assert not any(done), capability
        seeds = await _seeds(engine, story)
        assert seeds
        assert {(int(s.narrative_turn or 0), s.m) for s in seeds} == {(0, "active")}
        count = len(seeds)
        again = await client.post(
            f"/stories/{story}/respond", json=respond_body(player), headers=WRITE
        )
        assert again.status_code == 200, again.text
        assert len(await _seeds(engine, story)) == count


@pytest.mark.part("M7", "FR-PROF-18; FR-SCN-11")
async def test_i_27__m7_backfill(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Switched to Full with ``backfill``, the items are materialized under the backfill's
    operation when its run begins, dormant until it completes (then active). Cancelled
    mid-run instead, the seeds are discarded and the items deferred again."""
    async with respond_client(stub_settings, engine) as (client, lib):
        backfill_job.bind(lib.models)
        story, _ = await _dinner(client, engine)
        started = await client.put(
            f"/stories/{story}/engine-profile", json=_assign("backfill"), headers=WRITE
        )
        assert started.status_code == 202, started.text
        await run_backfill(engine, story, steps=1)
        operation = await scalar(
            engine, "SELECT operation_id FROM story_backfills WHERE story_id = :s", s=story
        )
        found = await _deferred(engine, story)
        assert {
            str(r.materialized_operation_id)
            for c in MATERIALIZED_HERE
            for r in found.get(c, [])
            if r.materialized_at is not None
        } == {str(operation)}
        assert {s.m for s in await _seeds(engine, story)} == {"dormant"}
        assert await run_backfill(engine, story) > 0
        assert {s.m for s in await _seeds(engine, story)} == {"active"}

        other, _ = await _dinner(client, engine)
        started = await client.put(
            f"/stories/{other}/engine-profile", json=_assign("backfill"), headers=WRITE
        )
        assert started.status_code == 202, started.text
        await run_backfill(engine, other, steps=1)
        assert await _seeds(engine, other)
        cancelled = await client.delete(f"/stories/{other}/backfill", headers=WRITE)
        assert cancelled.status_code == 200, cancelled.text
        assert {s.m for s in await _seeds(engine, other)} == {"discarded"}
        reset = await _deferred(engine, other)
        assert all(r.materialized_at is None for rs in reset.values() for r in rs)
