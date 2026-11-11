"""P-11: large casts (docs/04 §11), the M9 part (FR-MULTI-12; #829). Documented figures, not a
gate: no timing bound is asserted.

``test_p_11`` measures what a large cast costs per turn on the stub model at 0 ms
(``tests/perf/cast_support.py``). It runs casts of 10, 20, 50 and 100 characters, each with the
player, 3 fixed primaries and every other character fixed ``silent``, plus a 50-character
variant whose non-primaries are ``reactive``. Everyone stands in one room, under per-character
partitions and perception ``co_location``. Each cast plays 3 warm-up turns and then 20 measured
ones from an empty story. The test records the engine-only turn time (p50, p95), the prompt size
(the mean primary render and all of a turn's calls, in characters and in budgeter tokens), the
model calls per turn and the commit statements per turn (mean, max). It also records a linear
fit over the silent casts, extrapolated to 200, the most ``max_story_cast`` allows. The figures
go to ``build/perf/p_11.json`` and the page to ``build/perf/p_11.md``. The operator commits them
as ``perf/results/<date>-casts.{json,md}``; README's "Large casts" section quotes them.

A failed, lost or reduced turn fails the test. The test is ``perf_weekly``, run by
``perf.yml``'s weekly job on the GitHub-hosted runner, which reports it and gates nothing on its
figures. ``EPISTREL_TEST_CASTS_TURNS`` shortens a local run:

    EPISTREL_TEST_PERF_HOST=local-mac uv run pytest tests/perf/test_p_11.py -m perf_weekly -s

``test_p_11__builder`` runs in PR CI. It checks that the scenario's builder makes a 50-character
story with exactly 3 primaries. Its complement plays one turn and checks, in the stub's call
log, that no silent character was rendered.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.tables.characters import characters
from tests.auth_support import auth_settings
from tests.perf import cast_support as cs
from tests.perf.scale_support import RESULT_DIR, host_info, record, server_info
from tests.respond_support import respond_client

TURNS_VAR = "EPISTREL_TEST_CASTS_TURNS"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def roomy(stub_settings: Settings, engine: AsyncEngine) -> AsyncIterator[None]:
    """``max_story_cast`` raised to the largest cast measured; the value found is put back."""
    async with respond_client(stub_settings, engine) as (client, _):
        before = await cs.set_cast_limit(client, max(cs.SIZES))
        try:
            yield
        finally:
            await cs.set_cast_limit(client, before)


@pytest.mark.perf_weekly
@pytest.mark.part("M9", "FR-MULTI-12")
async def test_p_11(
    stub_settings: Settings, engine: AsyncEngine, roomy: None, drained_embedding_jobs: None
) -> None:
    """See the module docstring. No figure is gated; every turn of every cast committed with its
    three primaries rendered once each and none reduced or deferred.

    Complement asserted: the casts measured are exactly the sizes asked for, and the silent casts'
    commit statements do not fall as the cast grows (each measures the larger cast)."""
    turns = int(os.environ.get(TURNS_VAR, str(cs.TURNS)))
    plan = [(size, "silent") for size in cs.SIZES] + [(cs.REACTIVE_SIZE, "reactive")]
    measured: list[cs.Measured] = []
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for size, others in plan:
            cast = await cs.build(client, engine, size, others)
            measured.append(await cs.measure(client, engine, stub, cast, turns=turns))
            print(f"[P-11] {cs._row(measured[-1])}", flush=True)
    found = cs.document(measured, host_info(await server_info(engine)))
    found = json.loads(record("p_11", found).read_text())
    (RESULT_DIR / "p_11.md").write_text(cs.page(found, measured))
    assert [(m.size, m.others_role) for m in measured] == plan
    for m in measured:
        assert m.turns == turns == len(m.samples_ms)
        assert m.turn_ms["p50"] <= m.turn_ms["p95"]
        assert m.calls_per_turn["mean"] >= cs.PRIMARIES
    silent = [m.commit_statements["mean"] for m in measured if m.others_role == "silent"]
    assert silent[-1] >= silent[0], silent
    assert set(found["fit"]) == set(cs.FITTED)


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_p_11__builder(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """P-11's builder makes a 50-character story with exactly 3 primaries, the player and 46
    silent characters, all fixed.

    Complement asserted: one turn renders each primary once and no silent character at all
    (the stub's per-character call log)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        cast = await cs.build(client, engine, cs.REACTIVE_SIZE)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        characters.c.id,
                        characters.c.is_player,
                        characters.c.participation_role,
                        characters.c.participation_fixed,
                    ).where(characters.c.story_id == cast.story_id)
                )
            ).all()
        assert len(rows) == cs.REACTIVE_SIZE == len(cast.names)
        by_role: dict[str | None, set[str]] = {}
        for row in rows:
            by_role.setdefault(row.participation_role, set()).add(row.id)
            assert row.participation_fixed or row.is_player, row
        assert by_role["primary"] == set(cast.primaries)
        assert len(by_role["primary"]) == 3
        assert by_role["silent"] == set(cast.others)
        assert len(by_role["silent"]) == cs.REACTIVE_SIZE - 4
        assert {r.id for r in rows if r.is_player} == {cast.player}
        stub = epistrel.models.stub
        stub.reset()
        await cs.play(client, cast)
        for primary in cast.primaries:
            assert len(cs.renders_of(stub, cast.names[primary])) == 1
        rendered = [o for o in cast.others if cs.renders_of(stub, cast.names[o])]
        assert rendered == []
