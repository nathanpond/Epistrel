"""I-27 M7 (FR-PROF-18, FR-SCN-11, FR-STORE-23; #231): a Lite story's deferred genesis content
arrives at position 0 when the story gains the capability, under either ``upgrade_mode``.

The Lite Sunday Dinner story (``tests/lite_genesis_support.sunday_dinner``) plays two turns and
is switched to Full. Its ``structured``, ``episodic`` and ``partitions`` items are materialized
once, as ``seed`` records at narrative position 0 (a nested seed only where nested beliefs are
on). #698 adds the clock and States items (``test_i_27__m7_clock_states``), the Brother's
seed-vs-play conflict under each backfill policy (``test_i_27__m7_brother``) and the
``forward_only`` checkpoints, re-verification and post-upgrade catch
(``test_i_27__m7_checkpoints``).
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
    history, ``genesis_conflicts_unchecked``. Materializing again finds nothing: a later change
    writes no second copy."""
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


async def _family_lite(client: Any, engine: AsyncEngine, **starting: Any) -> tuple[str, str]:
    """A Lite story of S-FAMILY's Sunday Dinner with the allowance, the starting clock and the
    18:00 dinner and the Mother's errand (``tests/seeded_clock_support.family``), after two
    turns: its id and its player."""
    from epistrel.core.seeds.profiles import LITE_PRESET_ID
    from tests import seeded_clock_support as sc
    from tests import seeded_partitions_support as sp

    characters = await sp.cast(client)
    body = sc.family(scheduled_events=[sc.DINNER, sc.ERRAND], **starting)
    scenario_id, _ = await g.published_scenario(client, body)
    made = await g.create_from(client, sp.request(scenario_id, characters, LITE_PRESET_ID))
    story = made["story_id"]
    player = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
    for _ in range(2):
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(player), headers=WRITE
        )
        assert played.status_code == 200, played.text
    return story, player


@pytest.mark.part("M7", "FR-PROF-18; FR-SCN-11; FR-STATE-3; FR-CLOCK-3,9")
async def test_i_27__m7_clock_states(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#698: the Lite Sunday Dinner (allowance, starting clock, dinner and errand all deferred)
    switched to Full ``forward_only`` materializes its clock and States items too, under the
    configuration change's control operation: the ``allowance`` State with each sibling's
    starting value (``seed``, position 0), the scenario's anchor as the clock's first anchor fact
    (Sunday 1987-10-25, 14:00), its time awareness, and the dinner and the errand as pending
    events. A clock setting the story had changed (the skip cap, 30 against the scenario's 90)
    is ``kept_existing``. The operation's report: its card to a read naming no viewpoint (counts
    by capability and outcome, no entry), every entry to an omniscient read (#111's shape)."""
    from epistrel.core import states
    from epistrel.core.states.model import WORLD_CLOCK
    from epistrel.core.world_clock import HEAD, anchor_at
    from tests import seeded_clock_support as sc

    async with respond_client(stub_settings, engine) as (client, _):
        story, _player = await _family_lite(client, engine)
        before = await sc.deferred(engine, story)
        assert {cap for cap, done in before.values() if not done} >= {"states", "clock"}
        async with engine.begin() as conn:
            await states.update_definition(
                conn, story, WORLD_CLOCK, {"max_unprompted_skip_minutes": 30}, principal="p"
            )
        switched = await client.put(
            f"/stories/{story}/engine-profile", json=_assign("forward_only"), headers=WRITE
        )
        assert switched.status_code == 200, switched.text
        (entry,) = switched.json()["impact"]["affected_stories"]
        assert {"states", "clock"} <= set(entry["deferred_to_materialize"]["by_capability"])

        after = await sc.deferred(engine, story)
        for ref, (capability, done) in after.items():
            if capability in ("states", "clock"):
                assert done, ref
        defs = await sc.definitions(engine, story)
        assert set(defs) >= {"world_clock", "allowance"}
        seeded = await sc.seeded_values(engine, story)
        assert {v[0] for v in seeded.values()} == {"allowance"}
        assert sorted(v[2] for v in seeded.values()) == sorted(sc.ALLOWANCES.values())
        assert {v[3] for v in seeded.values()} == {0}
        clock = defs["world_clock"]
        assert clock.max_unprompted_skip_minutes == 30  # the story's own, kept
        assert clock.time_awareness == "approximate"
        async with engine.connect() as conn:
            anchor = await anchor_at(conn, story, HEAD)
        assert anchor.revision == 1
        assert str(anchor.display(0)).startswith("Sunday, 1987-10-25, 14:00")
        events = await sc.events(engine, story)
        assert sorted((e.status.value, e.planned) for e in events) == [
            ("pending", 60),
            ("pending", sc.DINNER_AT),
        ]

        operation = str(
            await scalar(
                engine,
                "SELECT materialized_operation_id FROM genesis_deferred WHERE story_id = :s "
                "AND capability_needed = 'clock' LIMIT 1",
                s=story,
            )
        )
        plain = (await client.get(f"/operations/{operation}", headers=READ)).json()
        card = plain["materialization"]
        assert set(card) == {"counts", "by_outcome", "conflicts"}
        assert card["counts"]["states"] >= 1
        assert card["counts"]["clock"] >= 1
        assert card["by_outcome"]["kept_existing"] == 1
        omniscient = (
            await client.get(
                f"/operations/{operation}", params={"viewpoint": "omniscient"}, headers=READ
            )
        ).json()["materialization"]
        by_ref = {e["field_ref"]: e for e in omniscient["entries"]}
        assert by_ref["starting_state.clock"]["outcomes"] == ["kept_existing"]
        assert "state" in by_ref["starting_state.states[0]"]["outcomes"]
        assert all("location" in e for e in omniscient["entries"])


AT_TABLE = "Tom passes the potatoes across the dining table."
"""The player's line each turn: the Brother at the table (play contradicting his seed)."""


def _away(body: dict[str, Any]) -> None:
    """The Brother seeded as away: at his college dorm, not among the opening's present."""
    body["world"]["locations"].append({"id": "college", "name": "The college dorm"})
    for role in body["cast"]["roles"]:
        if role["id"] == "brother":
            role["starting_location"] = "college"
    opening = body["starting_state"]["opening"]
    opening["roles_present"] = [r for r in opening["roles_present"] if r != "brother"]


async def _brother_story(
    client: Any, engine: AsyncEngine, lib: Any, policy: str
) -> tuple[str, str]:
    """The Lite family story with the Brother seeded away and played at the table, under the
    backfill ``policy``, switched to Full with ``backfill`` and its pass run."""
    import json

    from epistrel.core.seeds.profiles import LITE_PRESET_ID
    from epistrel.orchestrator.models.registry import EXTRACTION_BACKSTOP
    from tests import seeded_partitions_support as sp

    characters = await sp.cast(client)
    body = sp.family()
    _away(body)
    scenario_id, _ = await g.published_scenario(client, body)
    made = await g.create_from(client, sp.request(scenario_id, characters, LITE_PRESET_ID))
    story = made["story_id"]
    player = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
    for _ in range(2):
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(player, AT_TABLE), headers=WRITE
        )
        assert played.status_code == 200, played.text
    stub = lib.models.stub
    stub.when(
        AT_TABLE,
        json.dumps(
            {
                "claims": [
                    {
                        "beat": 0,
                        "span": "Tom passes the potatoes",
                        "subject": "Tom",
                        "predicate": "located_at",
                        "object": "The dining room",
                        "time": None,
                        "polarity": "affirmed",
                        "confidence": 0.95,
                    }
                ]
            }
        ),
        purpose=EXTRACTION_BACKSTOP.name,
    )
    backfill_job.bind(lib.models)
    patched = await client.patch(
        f"/stories/{story}/settings", json={"backfill_conflict_policy": policy}, headers=WRITE
    )
    assert patched.status_code == 200, patched.text
    started = await client.put(
        f"/stories/{story}/engine-profile", json=_assign("backfill"), headers=WRITE
    )
    assert started.status_code == 202, started.text
    await run_backfill(engine, story)
    tom = next(c["id"] for c in made["characters"] if c["role_id"] == "brother")
    return story, tom


async def _where_is(engine: AsyncEngine, story: str, character: str) -> set[str]:
    """Where canon has the character now (the live, open-ended ``located_at`` versions)."""
    found = await rows(
        engine,
        "SELECT e.canonical_name FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "JOIN entities e ON e.id = p.object_id JOIN predicates pr ON pr.id = f.predicate_id "
        "WHERE f.story_id = :s AND f.subject_id = :c AND pr.name = 'located_at' "
        "AND f.tx_to IS NULL AND f.valid_to IS NULL AND f.membership = 'active'",
        s=story,
        c=character,
    )
    return {str(r.canonical_name) for r in found}


@pytest.mark.part("M7", "FR-PROF-18; FR-PROF-10")
async def test_i_27__m7_brother(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#698 (I-27 M7, the Brother): the Lite family story seeds the Brother away (his college
    dorm) while play has him at the dining table. Switched to Full with ``backfill``, the seed
    is materialized at turn 0 under the backfill's operation and meets play as its earliest
    statement (``source: seed``); the story's ``backfill_conflict_policy`` settles it:

    - ``last_mention``: the dining room is canon; one conflict, resolved ``last_mention``.
    - ``first_mention``: the seed stands (the dorm), play's lines become in-world claims.
    - ``ask``: the pass ends ``paused_for_conflicts`` with the conflict listed (the seed and
      both lines, the recommendation the last line) and the seeds still dormant."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, tom = await _brother_story(client, engine, lib, "last_mention")
        found = await _backfill(client, story)
        assert found["status"] == "completed"
        assert found["report"]["conflicts"]["by_resolution"] == {"last_mention": 1}
        (conflict,) = found["conflict_list"]
        seed = conflict["statements"][0]
        assert (seed["turn"], seed["source"], seed["quote"]) == (0, "seed", None)
        assert {s["quote"] for s in conflict["statements"][1:]} == {AT_TABLE}
        assert await _where_is(engine, story, tom) == {"The dining room"}

        story, tom = await _brother_story(client, engine, lib, "first_mention")
        found = await _backfill(client, story)
        assert found["status"] == "completed"
        assert set(found["report"]["conflicts"]["by_resolution"]) == {"first_mention"}
        assert await _where_is(engine, story, tom) == {"The college dorm"}
        claims = await rows(
            engine,
            "SELECT narrative_turn FROM utterances WHERE story_id = :s "
            "AND objective_relation = 'false' AND membership = 'active'",
            s=story,
        )
        assert sorted(int(c.narrative_turn) for c in claims) == [2, 3]

        story, tom = await _brother_story(client, engine, lib, "ask")
        found = await _backfill(client, story)
        assert found["status"] == "paused_for_conflicts"
        (conflict,) = found["conflict_list"]
        assert conflict["status"] == "open"
        assert [s["source"] for s in conflict["statements"]] == ["seed", "transcript", "transcript"]
        assert conflict["recommended"] == {
            "choose": conflict["statements"][-1]["statement_id"],
            "reason": "last_mention",
        }
        assert {s.m for s in await _seeds(engine, story)} == {"dormant"}


async def _backfill(client: Any, story: str) -> dict[str, Any]:
    got = await client.get(f"/stories/{story}/backfill", params={"conflicts": "all"}, headers=READ)
    assert got.status_code == 200, got.text
    found: dict[str, Any] = got.json()
    return found


DEED = "Tom Harrow keeps the deed."
"""A world seed the possession detector normalizes: ``holds(Tom, the deed)``."""


@pytest.mark.part("M7", "FR-PROF-18; FR-STORE-23")
async def test_i_27__m7_checkpoints(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#698 (I-27 M7, ``forward_only``): the Lite family story with a world seed ("Tom Harrow
    keeps the deed") plays five turns and is switched to Full ``forward_only``.

    - **Corrected history**: canon at checkpoint 5 holds the materialized seeds (``seed``, at
      position 0), the deed among them.
    - **As originally recorded**: checkpoint 5's canon (``views.as_originally_recorded``) holds
      none of them: they were recorded after it.
    - **No re-verification**: the switch makes no model call, and turns 1 to 5 keep no
      verification.
    - **From then on**: the next turn's claim that Tom never kept the deed is ``contradicted``
      (rule ``polarity``), its evidence the seed's proposition."""
    from epistrel.core import views
    from epistrel.core.canon.facts import canon
    from epistrel.core.membership import Position
    from epistrel.core.seeds.profiles import LITE_PRESET_ID
    from epistrel.core.settings_schema import OMNISCIENT
    from tests import seeded_partitions_support as sp
    from tests.structured_turn_support import beat, claim, render, turn_report

    async with respond_client(stub_settings, engine) as (client, lib):
        characters = await sp.cast(client)
        body = sp.family()
        body["world"]["entities"].append({"id": "deed", "name": "The deed", "kind": "item"})
        body["starting_state"]["seeded_memories"].append(sp.seed(DEED, "world", ["public"]))
        scenario_id, _ = await g.published_scenario(client, body)
        made = await g.create_from(client, sp.request(scenario_id, characters, LITE_PRESET_ID))
        story = made["story_id"]
        player = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
        for _ in range(5):
            played = await client.post(
                f"/stories/{story}/respond", json=respond_body(player), headers=WRITE
            )
            assert played.status_code == 200, played.text
        stub = lib.models.stub
        stub.reset()
        switched = await client.put(
            f"/stories/{story}/engine-profile", json=_assign("forward_only"), headers=WRITE
        )
        assert switched.status_code == 200, switched.text
        async with engine.connect() as conn:
            corrected = await canon(conn, story, through_position=Position(5), viewpoint=OMNISCIENT)
        seeds = {str(f.proposition_id) for f in corrected if str(f.provenance) == "seed"}
        assert seeds
        recorded = await views.as_originally_recorded(engine, story, views.Checkpoint(5))
        assert not [f for f in recorded.facts if str(f.provenance) == "seed"]
        assert stub.calls == []
        verified = await rows(
            engine,
            "SELECT narrative_turn FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND turn_report ? 'verify'",
            s=story,
        )
        assert verified == []
        denial = render(
            beat(
                "say",
                "Tom never kept any deed.",
                intent="sincere",
                propositions=[claim("Tom Harrow", "holds", "The deed", negated=True)],
            )
        )
        stub.script("generation", [denial] * 4)
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(player), headers=WRITE
        )
        assert played.status_code == 200, played.text
        (verdict,) = (await turn_report(engine, story))["verify"]["verdicts"]
        assert (verdict["kind"], verdict["rule"]) == ("contradicted", "polarity")
        assert set(verdict["evidence_ids"]) <= seeds
