"""I-27: genesis by capability (docs/04 §4), its M4 part (#149).

The M3 parts live in ``tests/integration/test_i_27.py``; this file holds the M4 part only
(traceability counts a test ID per milestone in one file). The scenario is the Sunday Dinner
of ``tests/lite_genesis_support`` with two seeds the deterministic detectors normalize
(``tests/structured_genesis_support``), whose capability names stay in that module.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.catalog import validation
from epistrel.core.catalog.checks.base import ValidationContext
from epistrel.core.catalog.scenarios import SERVICE as SCENARIOS
from epistrel.core.config_changes import read_settings
from epistrel.core.genesis.settings_map import genesis_capabilities
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests import structured_genesis_support as sg
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.validation_support import codes, entries, validate

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _seed_ids(client: Any, scenario_id: str, version: int) -> dict[str, str]:
    """The published version's seed ids, by seed text."""
    read = await scenarios.read(client, scenario_id, version=version, projection="full")
    full = read.json()
    found = {s["text"]: s["id"] for s in full["starting_state"]["seeded_memories"]}
    for role in full["cast"]["roles"]:
        found.update({s["text"]: s["id"] for s in role.get("seeded_memories", [])})
    return found


async def _report(client: Any, engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The creation report the genesis operation keeps (an omniscient read)."""
    (row,) = await sg.rows_of(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'genesis'",
        s=story_id,
    )
    read = await client.get(
        f"/operations/{row.operation_id}", params={"viewpoint": "omniscient"}, headers=g.READ
    )
    assert read.status_code == 200, read.text
    report: dict[str, Any] = read.json()["creation_report"]
    return report


@pytest.mark.part("M4", "FR-SCN-5,9,11; FR-PROP-4,12")
async def test_i_27__structured(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The Sunday Dinner scenario, created as a structured story with shared character
    knowledge (the seeded ``Structured (M4)`` profile), and as a Lite one.

    - **Publish.** On this deployment (the structured pipeline delivered) a dry run runs seed
      normalization and the contradiction check: neither is listed unavailable, and the
      brother's seed contradicting the world's is reported (a warning: the world wins). Both are
      listed unavailable when the capabilities lack the pipeline. A scenario whose world seeds
      contradict each other, or whose author predicate takes a core predicate's name, does not
      publish (``seed_contradiction``, ``seed_unnormalizable``).
    - **Genesis.** The world seed "Tom Harrow keeps the deed" becomes a canon fact with
      provenance ``seed`` and a semantic memory; the free-text world seed an episodic memory;
      the brother's contradicting seed loses the fact merge and is dropped entirely (no fact,
      no memory), reported ``merge_conflict`` with winner, loser and rule; the scenario's author
      predicate and rule enter the predicate registry; its places and entity become entities;
      the relationships and the opening placement become canon. Every ``structured`` row it
      consumed has ``materialized_at`` set; the rows needing a later capability stay ``NULL`` and
      are the report's ``deferred[]``. ``strict`` refuses the same creation
      (``creation_conflicts``) and writes nothing.
    - **Director notes.** No fact, proposition, memory, entity, alias or predicate of the story
      names any of the director's phrases, and a retrieval for the guidance finds nothing.
    - **Lite.** The Lite story created from the same scenario keeps every ``genesis_deferred``
      row unmaterialized, its persisted report lists them all as deferred and nothing as
      materialized, and it has no fact or memory.

    Complements asserted: the director phrases are absent from every store while the scenario
    snapshot holds them; the refused strict creation leaves every count as it was."""
    async with respond_client(stub_settings, engine) as (client, _lib):
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = sg.structured_dinner(locked, default)
        scenario_id, version = await g.published_scenario(client, body)
        seed_ids = await _seed_ids(client, scenario_id, version)
        world_id, denial_id = seed_ids[sg.WORLD_CANON], seed_ids[sg.ROLE_DENIAL]

        # -- publish: the seed checks run, and are unavailable without the pipeline
        dry = (await validate(client, scenario_id)).json()
        assert dry["valid"] is True, dry
        unavailable = set(dry["report"]["unavailable"])
        assert not unavailable & {"seed_normalization", "fact_consistency"}
        (contradiction,) = entries(dry["report"], "seed_contradiction")
        assert (contradiction["location"]["seed_id"], contradiction["source"]) == (
            denial_id,
            world_id,
        )
        assert contradiction["outcome"] == "dropped"
        async with engine.connect() as conn:
            subject = SCENARIOS.subject(scenario_id, version, body)
            lite = await validation.run(
                subject,
                ValidationContext(
                    conn=conn,
                    settings=(await read_settings(conn)).as_settings(),
                    capabilities=genesis_capabilities(),
                ),
            )
        assert {"seed_normalization", "fact_consistency"} <= set(lite.unavailable)
        assert not [e for e in lite.warnings if e.code == "seed_contradiction"]

        clash = sg.structured_dinner(locked, default)
        clash["starting_state"]["seeded_memories"].append(sg.public_seed(sg.ROLE_DENIAL))
        created = await scenarios.create(client, clash)
        refused = await scenarios.publish(client, created["id"], created["draft_revision"])
        assert refused.status_code == 422, refused.text
        report = scenarios.error_of(refused)["details"]["report"]
        assert ("seed_contradiction", "starting_state.seeded_memories[2].text") in codes(
            report["errors"]
        )
        core_name = sg.structured_dinner(locked, default)
        core_name["starting_state"]["predicates"] = [{"name": "owns"}]
        created = await scenarios.create(client, core_name)
        refused = await scenarios.publish(client, created["id"], created["draft_revision"])
        assert refused.status_code == 422, refused.text
        report = scenarios.error_of(refused)["details"]["report"]
        assert codes(report["errors"]) == [
            ("seed_unnormalizable", "starting_state.predicates[0].name")
        ]

        # -- strict: the merge conflict refuses the creation, and nothing is written
        request = sg.structured_request(scenario_id, mother)
        before = await lg.story_table_counts(engine)
        status, code, details = await g.refused(client, {**request, "strict": True})
        assert (status, code) == (422, "creation_conflicts")
        assert details["report"]["counts"]["conflicts"] == 1
        assert await lg.story_table_counts(engine) == before

        # -- genesis
        made = await g.create_from(client, request)
        story_id = made["story_id"]
        report = made["report"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        materialized = sg.by_field(report)
        assert materialized["starting_state.seeded_memories[1]"]["outcomes"] == [
            "canon",
            "memory",
        ]
        assert materialized["starting_state.seeded_memories[0]"]["outcomes"] == ["memory"]
        lost = materialized["cast.roles[1].seeded_memories[1]"]
        assert lost["outcomes"] == ["merge_conflict"]
        assert (lost["winner"], lost["loser"], lost["rule"]) == (world_id, denial_id, "polarity")
        assert report["counts"]["conflicts"] == 1
        for field_ref in ("world.locations[0]", "world.locations[1]", "world.entities[0]"):
            assert materialized[field_ref]["outcomes"] == ["entity"]
        for field_ref in ("starting_state.predicates[0]", "starting_state.rules[0]"):
            assert materialized[field_ref]["outcomes"] == ["rule"]
        assert materialized["cast.roles[0].relationships[0]"]["outcomes"] == ["canon"]
        assert "seed_normalization" not in report["unavailable"]
        assert sum(report["counts"]["materialized"]["by_outcome"].values()) == sum(
            len(e["outcomes"]) for e in report["materialized"]
        )

        facts = await sg.rows_of(
            engine,
            "SELECT p.predicate_id, pr.name, p.subject_id, p.object_id, p.polarity, "
            "f.provenance, f.authority, f.author_locked, f.visibility FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "JOIN predicates pr ON pr.id = p.predicate_id WHERE f.story_id = :s",
            s=story_id,
        )
        assert {f.provenance for f in facts} == {"seed"}
        assert {(f.authority, f.author_locked) for f in facts} == {("author", False)}
        deed = [f for f in facts if f.name == "holds"]
        assert [(str(f.subject_id), f.polarity) for f in deed] == [(by_role["brother"], "affirmed")]
        named = {(f.name, str(f.subject_id)) for f in facts}
        assert ("located_at", by_role["mother"]) in named
        assert ("parent_of", by_role["mother"]) in named
        assert ("age", by_role["mother"]) in named
        ages = [f for f in facts if f.name == "age"]
        assert {f.visibility["scope"] for f in ages} == {"engine"}

        memories = await sg.rows_of(
            engine,
            "SELECT kind, text, provenance, trust_tier, proposition_ids, importance "
            "FROM memories WHERE story_id = :s ORDER BY text",
            s=story_id,
        )
        assert [(m.kind, m.text, m.provenance, m.trust_tier) for m in memories] == [
            ("episodic", sg.AUNT_PROSE, "seed", "author"),
            ("episodic", sg.WORLD_PROSE, "seed", "author"),
            ("semantic", sg.WORLD_CANON, "seed", "author"),
        ]
        assert memories[2].proposition_ids
        assert {float(m.importance) for m in memories} == {0.8}

        predicates = await sg.rows_of(
            engine, "SELECT name, layer FROM predicates WHERE story_id = :s", s=story_id
        )
        assert [(p.name, p.layer) for p in predicates] == [("inherits", "author")]
        (story,) = await sg.rows_of(
            engine, "SELECT predicate_rules FROM stories WHERE id = :s", s=story_id
        )
        assert len(story.predicate_rules) == 1
        places = await sg.rows_of(
            engine,
            "SELECT kind, canonical_name FROM entities WHERE story_id = :s "
            "AND character_id IS NULL ORDER BY canonical_name",
            s=story_id,
        )
        assert [(p.kind, p.canonical_name) for p in places] == [
            ("place", "The kitchen"),
            ("place", "The orchard"),
            ("item", "The orchard deed"),
        ]

        rows = await lg.deferred_rows(engine, story_id)
        consumed = {r["field_ref"] for r in rows if r["materialized_at"] is not None}
        assert consumed
        assert all(r["capability_needed"] == sg.STRUCTURED for r in rows if r["materialized_at"])
        later = [r for r in rows if r["capability_needed"] in sg.LATER_CAPABILITIES]
        assert later
        assert {r["materialized_at"] for r in later} == {None}
        listed = sorted((d["field_ref"], d["capability_needed"]) for d in report["deferred"])
        assert listed == sorted(
            (r["field_ref"], r["capability_needed"]) for r in rows if r["materialized_at"] is None
        )
        assert consumed <= set(materialized)
        assert await _report(client, engine, story_id) == report

        # -- the director's notes reach no store of the story
        (snapshot,) = await sg.rows_of(
            engine, "SELECT scenario_snapshot FROM stories WHERE id = :s", s=story_id
        )
        held = json.dumps(snapshot.scenario_snapshot)
        for phrase in sg.DIRECTOR_PHRASES:
            assert phrase in held, phrase
            assert await sg.story_rows_naming(engine, story_id, phrase) == {}, phrase
        retrieved = await client.get(
            f"/stories/{story_id}/retrieve",
            params={"q": scenarios.GUIDANCE_SECRET, "viewpoint": "omniscient"},
            headers=g.READ,
        )
        assert retrieved.status_code == 200, retrieved.text
        assert scenarios.GUIDANCE_SECRET not in retrieved.text
        found = await client.get(
            f"/stories/{story_id}/retrieve",
            params={"q": "four generations", "viewpoint": "omniscient"},
            headers=g.READ,
        )
        assert found.status_code == 200, found.text
        assert sg.WORLD_PROSE in found.text

        # -- the Lite story from the same scenario is unchanged
        lite_made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id,
                slots={"mother": g.catalog_slot(mother)},
                ignore_warnings=True,
                projection="full",
            ),
        )
        lite_id = lite_made["story_id"]
        lite_rows = await lg.deferred_rows(engine, lite_id)
        assert {r["materialized_at"] for r in lite_rows} == {None}
        lite_report = await _report(client, engine, lite_id)
        assert "materialized" not in lite_report
        assert "materialized" not in lite_report["counts"]
        assert lg.deferred_pairs(lite_report) == lg.expected_deferred(body)
        assert lite_report["counts"]["conflicts"] == 0
        for table in ("facts", "memories"):
            (count,) = await sg.rows_of(
                engine,
                f"SELECT count(*) AS n FROM {table} WHERE story_id = :s",  # noqa: S608
                s=lite_id,
            )
            assert count.n == 0, table
