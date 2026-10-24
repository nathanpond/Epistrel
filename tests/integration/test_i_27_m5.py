"""I-27: genesis by capability (docs/04 §4), its M5 part (#173).

The M3 parts live in ``tests/integration/test_i_27.py`` and the M4 part in
``tests/integration/test_i_27_m4.py`` (traceability counts a test ID per milestone in one file).
The scenario is S-FAMILY's Sunday Dinner of ``tests/seeded_partitions_support``, whose seeds the
deterministic detectors normalize.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import testimony
from epistrel.testing.profiles import structured_m4
from tests import genesis_support as g
from tests import seeded_partitions_support as sp
from tests.auth_support import auth_settings
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M5", "FR-SCN-8,9,11")
async def test_i_27__partitions(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """The Sunday Dinner created with per-character partitions: seeds route by audience.

    - **The Mother's affair** is a belief in her partition only (``secret``), its canon fact
      visible to her alone, its memory hers alone.
    - **The Brother's false belief** that the house is not being sold survives genesis: his
      negated belief, ``believed``, with no canon fact of its own, beside the canon that the
      house is being sold, which only its holders see.
    - **The Sister's nested belief** that the Brother knows about the sale is created (#167's
      nested and attributed rows) although he holds the opposite.
    - **The opening** (turn 1) creates one exposure per stated proposition for each role
      present, at the narration beat's witness position, and none for the absent Sister, the
      neighbour or the player.
    - Every ``partitions`` row the branch consumed has ``materialized_at`` and the report defers
      nothing; every seeded belief is an ``author`` transition at story minute -1 citing its
      item's ``seed_materialized`` act. The topology and the channel are canon, the Mother's
      relationship carries its ``trust`` and her trust in Sam reads it.

    Complement: the same scenario pinned to ``Structured (M4)``, version 1 (no partitions), writes
    no belief, no exposure and keeps every ``partitions`` row unmaterialized."""
    async with respond_client(stub_settings, engine) as (client, _lib):
        profile = await sp.partitioned_profile(client)
        characters = await sp.cast(client)
        scenario_id, _ = await g.published_scenario(client, sp.family())
        made = await g.create_from(client, sp.request(scenario_id, characters, profile))
        story = made["story_id"]
        report = made["report"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        ents = await sp.entity_ids(engine, story)
        rows = await sp.beliefs(engine, story)

        # -- the affair: the Mother's partition only
        letters = ents["The love letters"]
        affair = [r for r in rows if str(r.object_id) == letters]
        assert [(str(r.holder), r.disclosure, r.confidence_level) for r in affair] == [
            (by_role["mother"], "secret", "known")
        ]
        (fact,) = await sp.rows_of(
            engine,
            "SELECT f.visibility, f.provenance FROM facts f JOIN propositions p "
            "ON p.id = f.proposition_id WHERE f.story_id = :s AND p.object_id = :o",
            s=story,
            o=letters,
        )
        assert fact.visibility == {"scope": "characters", "characters": [by_role["mother"]]}
        assert fact.provenance == "seed"
        memories = await sp.rows_of(
            engine,
            "SELECT holder_character_id, visibility FROM memories WHERE story_id = :s "
            "AND text = :t",
            s=story,
            t=sp.AFFAIR,
        )
        assert [(str(m.holder_character_id), m.visibility["characters"]) for m in memories] == [
            (by_role["mother"], [by_role["mother"]])
        ]

        # -- the Brother's false belief survives; the sale's canon is its holders' alone
        frank, contract = by_role["father"], ents["The sale contract"]
        (denied,) = sp.held(rows, by_role["brother"], frank, contract)
        assert (denied.polarity, denied.confidence_level) == ("negated", "believed")
        sale = await sp.rows_of(
            engine,
            "SELECT f.visibility FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.object_id = :o",
            s=story,
            o=contract,
        )
        assert [f.visibility["characters"] for f in sale] == [
            sorted([by_role["father"], by_role["mother"]])
        ]
        entry = sp.by_field(report)["cast.roles[2].seeded_memories[0]"]
        assert entry["outcomes"] == ["belief", "memory"]

        # -- the Sister's nested belief, although the Brother holds the opposite
        nested = sp.held(rows, by_role["sister"], frank, contract, kind="nested")
        assert [(str(r.attributed_holder_id), r.polarity) for r in nested] == [
            (by_role["brother"], "affirmed")
        ]
        assert sp.held(rows, by_role["sister"], frank, contract, kind="attributed")
        assert not sp.held(rows, by_role["sister"], frank, contract)

        # -- the opening's exposures: the roles present, not the absent ones
        exposures = await sp.rows_of(
            engine,
            "SELECT x.witness_character_id, x.source_character_id, x.kind, x.position_turn, "
            "x.position_beat, x.position_step, p.subject_id, p.object_id FROM exposures x "
            "JOIN propositions p ON p.id = x.proposition_id WHERE x.story_id = :s",
            s=story,
        )
        assert sorted(str(x.witness_character_id) for x in exposures) == sorted(
            by_role[r] for r in ("mother", "father", "brother")
        )
        assert {
            (x.source_character_id, x.kind, x.position_turn, x.position_beat, x.position_step)
            for x in exposures
        } == {(None, "content", 1, 1, 1)}
        assert {(str(x.subject_id), str(x.object_id)) for x in exposures} == {
            (by_role["sister"], ents["The casserole dish"])
        }

        # -- every consumed row materialized; every belief a seed's author act before the story
        deferred = await sp.rows_of(
            engine,
            "SELECT field_ref, capability_needed, materialized_at FROM genesis_deferred "
            "WHERE story_id = :s",
            s=story,
        )
        assert {r.materialized_at is not None for r in deferred} == {True}
        assert report["deferred"] == []
        # A belief's provenance is its transition; the act it cites says the author act was a
        # seed (#160's antecedent).
        assert {(r.transition, r.act, r.valid_at) for r in rows} == {
            ("author", "seed_materialized", -1)
        }
        assert {r.held_from_turn for r in rows} == {0}

        # -- topology, channels and the relationship's trust
        facts = await sp.rows_of(
            engine,
            "SELECT pr.name, p.subject_id, p.object_id, p.qualifiers, p.literal FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "JOIN predicates pr ON pr.id = p.predicate_id WHERE f.story_id = :s",
            s=story,
        )
        named = {(f.name, str(f.subject_id), str(f.object_id)) for f in facts}
        house, dining, kitchen = (
            ents["The farmhouse"],
            ents["The dining room"],
            ents["The kitchen"],
        )
        assert ("located_in", dining, house) in named
        assert ("located_in", kitchen, house) in named
        (adjacent,) = [f for f in facts if f.name == "adjacent_to"]
        assert {str(adjacent.subject_id), str(adjacent.object_id)} == {dining, kitchen}
        assert adjacent.qualifiers == {"barrier": "door_closed"}
        channel = {str(f.subject_id) for f in facts if f.name == "on_channel"}
        assert channel == {by_role["sister"], by_role["brother"]}
        (parent,) = [f for f in facts if f.name == "parent_of"]
        assert parent.qualifiers == {"trust": 0.9}
        async with engine.connect() as conn:
            assert await testimony.trust(conn, story, by_role["mother"], by_role["player"]) == 0.9

        # -- complement: without partitions, no belief and nothing of capability partitions
        lite = await g.create_from(
            client,
            sp.request(
                scenario_id, characters, structured_m4(), neighbour=False, engine_profile_version=1
            ),
        )
        assert await sp.beliefs(engine, lite["story_id"]) == []
        rows_lite = await sp.rows_of(
            engine,
            "SELECT capability_needed, materialized_at FROM genesis_deferred WHERE story_id = :s",
            s=lite["story_id"],
        )
        assert {r.materialized_at for r in rows_lite if r.capability_needed == "partitions"} == {
            None
        }
        assert not await sp.rows_of(
            engine, "SELECT id FROM exposures WHERE story_id = :s", s=lite["story_id"]
        )
