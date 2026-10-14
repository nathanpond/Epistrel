"""I-27: genesis by capability (docs/04 §4), its M3 parts.

Every I-27 (M3) part lives in this file (traceability counts a test ID per milestone in one
file): #119's ``test_i_27__atomic`` (FR-SCN-9), and #122's ``test_i_27__lite``, the
materialisation by field class (the Lite prompt, the deferred and inactive report; its
capability names stay in ``tests/lite_genesis_support.py``). The scenario is
``tests/scenario_support.COMPLETE``, which fills every group; its later milestones' field names
stay in that fixture.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings
from tests.respond_support import body as body_of
from tests.respond_support import (
    everything_sent,
    generation_calls,
    respond,
    respond_client,
    system_of,
)
from tests.story_support import rest

pytestmark = pytest.mark.integration


async def _one(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


@pytest.mark.part("M3", "FR-SCN-9")
async def test_i_27__atomic(settings: Settings, engine: AsyncEngine) -> None:
    """A story created from a scenario that fills every group: the creation commits as one
    genesis operation; the story's snapshot of the scenario holds every field of the published
    version, exactly; turn 1 is the opening narration (an authored narration beat, no
    character, naming the characters of the roles the opening has present); truncating to 0
    leaves the genesis state, the cast and the snapshots, with turn 1 discarded. The profile it
    would run under leaves a recommended capability unmet, so the request without
    ``ignore_warnings`` creates nothing and answers the reports.

    Complements asserted: the held request wrote no row; the confirmed one did."""
    async with rest(settings, engine) as client:
        locked, default = await scenarios.cast_characters(client)
        warm = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = scenarios.complete_with(locked, default)
        scenario_id, version = await g.published_scenario(client, body)
        request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(warm)})

        counted = await g.counts(engine)
        held = await g.create_from(client, request, status=200)
        assert held["applied"] is False
        assert held["story_id"] is None
        assert held["impact"]["recommendations_unmet"]
        assert await g.counts(engine) == counted

        made = await g.create_from(client, {**request, "ignore_warnings": True})
        story_id = made["story_id"]
        assert made["applied"] is True
        assert made["timeline_revision"] == 2
        moved = await g.counts(engine)
        assert moved["stories"] == counted["stories"] + 1
        assert moved["characters"] == counted["characters"] + 4
        assert moved["character_snapshots"] == counted["character_snapshots"] + 3

        snapshot = await _one(
            engine, "SELECT scenario_snapshot FROM stories WHERE id = :s", s=story_id
        )
        published = await _one(
            engine,
            "SELECT content FROM catalog.scenario_versions WHERE catalog_id = :c AND version = :v",
            c=scenario_id,
            v=version,
        )
        assert snapshot == published
        assert isinstance(snapshot, dict)
        assert set(scenarios.GROUPS) <= set(snapshot)
        held_text = json.dumps(snapshot)
        for phrase in scenarios.PRIVATE_PHRASES:
            assert phrase in held_text, phrase
        assert (
            await _one(
                engine,
                "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'genesis'",
                s=story_id,
            )
            == 1
        )

        async with engine.connect() as conn:
            opening = (
                await conn.execute(
                    text(
                        "SELECT e.type, e.payload->>'text' AS said, e.character_id, "
                        "e.provenance, o.kind, o.narrative_turn FROM events e "
                        "JOIN operations o ON o.operation_id = e.operation_id "
                        "WHERE e.story_id = :s AND o.narrative_turn = 1"
                    ),
                    {"s": story_id},
                )
            ).all()
            row = (
                await conn.execute(
                    text(
                        "SELECT role, channel, text FROM transcript_messages "
                        "WHERE story_id = :s AND narrative_turn = 1"
                    ),
                    {"s": story_id},
                )
            ).one()
        assert [(r.type, r.said, r.character_id, r.provenance, r.kind) for r in opening] == [
            (
                "message_rendered",
                scenarios.COMPLETE["starting_state"]["opening"]["narration"],
                None,
                "system",
                "turn",
            )
        ]
        assert (row.role, row.channel) == ("narration", "narration")
        present = g.opening_cast(body, made)
        assert len(present) == 2
        assert await g.opening_present(engine, story_id) == present

        def genesis_state() -> str:
            return (
                "SELECT (SELECT json_agg(c ORDER BY c.ordinal)::text FROM characters c "
                "WHERE c.story_id = :s) || (SELECT json_agg(t ORDER BY t.character_id)::text "
                "FROM character_snapshots t WHERE t.story_id = :s) || "
                "(SELECT scenario_snapshot::text FROM stories WHERE id = :s)"
            )

        kept = await _one(engine, genesis_state(), s=story_id)
        cut = await client.post(
            f"/stories/{story_id}/truncate", json={"to_turn": 0}, headers=g.WRITE
        )
        assert cut.status_code == 200, cut.text
        assert await _one(engine, genesis_state(), s=story_id) == kept
        assert (
            await _one(
                engine,
                "SELECT membership FROM operations WHERE story_id = :s AND narrative_turn = 1",
                s=story_id,
            )
            == "discarded"
        )
        assert (
            await _one(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id) == 1
        )


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M3", "FR-SCN-2,5,9,11")
async def test_i_27__lite(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The Sunday Dinner scenario (every group filled: director guidance, hooks and objectives,
    story settings, a recommended profile, a visual style; a phrase planted in its public
    description, a label and its style) creates a Lite story in one genesis. Under ``full``
    the creation report lists every item the story cannot use as deferred, each with its
    location and the capability it needs (each knowledge seed's structured form among them),
    and never a public-card, director, recommendation or style field; the same items are the
    story's ``genesis_deferred`` rows, unmaterialised, with the scenario's original JSON. Each
    story setting for a capability the story lacks is stored in its settings and listed
    inactive; the settings Lite uses are applied. Under ``card`` the report carries the story,
    its cast as cards and only counts by class and capability with the checks that could not
    run, quoting no private field. A turn's captured prompt carries the seeds as scenario text
    and the director guidance as separately labelled author notes, in that order around the
    characters' notes; the genesis operation keeps the full report for an omniscient read.

    Complements asserted: the planted public-card, label and style phrases are in no captured
    prompt; the creation adds rows to exactly the tables genesis writes (no empty or
    placeholder record for any deferred field); a read of the genesis operation naming no
    viewpoint gets no report."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = lg.sunday_dinner(locked, default)
        scenario_id, version = await g.published_scenario(client, body)
        request = g.from_scenario(
            scenario_id,
            slots={"mother": g.catalog_slot(mother)},
            ignore_warnings=True,
            projection="full",
        )

        before = await lg.story_table_counts(engine)
        made = await g.create_from(client, request)
        story_id = made["story_id"]
        report = made["report"]
        added = lg.grown(before, await lg.story_table_counts(engine))
        expected = lg.expected_deferred(body)
        assert added == {
            "stories": 1,
            "characters": 4,
            "character_snapshots": 3,
            "genesis_deferred": len(expected),
            "operations": 2,
            "events": 6,
            "transcript_messages": 1,
            "story_heads": 1,
            "story_texts": 1,
            "policies": 1,
        }

        # The full report: every deferred item with its location and capability.
        assert report["story_id"] == story_id
        assert lg.deferred_pairs(report) == expected
        for item in report["deferred"]:
            assert item["location"]["entry"] == scenario_id
            assert item["location"]["version"] == version
            assert item["location"]["field"] == item["field_ref"]
            assert not item["field_ref"].startswith(lg.NEVER_DEFERRED), item
            assert item["field_class"] in ("knowledge_seed", "story_rule", "starting_state")
        seeds = [d for d in report["deferred"] if ".seeded_memories[" in d["field_ref"]]
        assert len(seeds) == len(body["starting_state"]["seeded_memories"]) + sum(
            len(r.get("seeded_memories", [])) for r in body["cast"]["roles"]
        )
        assert {s["payload"]["value"]["text"] for s in seeds} >= {
            f"Last winter {scenarios.ROLE_SECRET}."
        }
        counts = report["counts"]
        assert counts["conflicts"] == 0
        assert counts["deferred"]["by_capability"] == lg.counted(expected)
        assert sum(counts["deferred"]["by_class"].values()) == len(expected)
        rows = await lg.deferred_rows(engine, story_id)
        assert [{k: r[k] for k in lg.report_items(report)[0]} for r in rows] == lg.report_items(
            report
        )
        assert {r["materialized_at"] for r in rows} == {None}

        # Inactive settings: stored and listed; the used ones applied.
        filled = [c["role_id"] for c in made["characters"]]
        inactive = lg.expected_inactive(body, filled)
        assert lg.inactive_of(report) == inactive
        assert counts["inactive"]["by_class"] == {"configuration": len(inactive)}
        assert counts["inactive"]["by_capability"] == lg.counted(
            [(name, item["capability"]) for name, item in inactive.items()]
        )
        story = (await client.get(f"/stories/{story_id}", headers=g.READ)).json()
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        assert story["settings"]["inactive"] == inactive
        assert story["settings"]["turn_concurrency"] == "queue"
        assert story["settings"]["audit_retention_days"] == 30
        assert story["settings"]["default_output_viewpoint"] == f"character:{by_role['mother']}"
        assert not set(lg.UNUSED_SETTINGS) & set(story["settings"])

        # The card report: counts only, the cast as cards, no private field.
        card = await g.create_from(client, {**request, "projection": "card"})
        shown = card["report"]
        assert (shown["errors"], shown["warnings"], shown["deferred"], shown["inactive"]) == (
            None,
            None,
            None,
            None,
        )
        assert shown["counts"] == {**counts}
        assert shown["unavailable"] == list(scenarios.M3_UNAVAILABLE)
        assert [c["kind"] for c in shown["cast"]] == ["catalog", "catalog", "catalog", "player"]
        assert [c["role_id"] for c in shown["cast"]] == ["mother", "brother", "aunt", "player"]
        assert shown["cast"][1]["source"] == {
            "catalog_id": locked,
            "version": scenarios.LOCKED_VERSIONS,
        }
        quoted = json.dumps(shown)
        for phrase in (*scenarios.PRIVATE_PHRASES, "Sam, back for", "only key to the old barn"):
            assert phrase not in quoted, phrase

        # The prompt: scenario text, the characters' notes, then the director notes.
        stub.reset()
        player = by_role["player"]
        played_turn = await respond(client, story_id, body_of(player, "Is dinner ready?"))
        assert played_turn.status_code == 200, played_turn.text
        (call,) = generation_calls(stub)
        system = system_of(call)
        scenario_block = lg.section(system, lg.SCENARIO_HEADING, "### Author notes:")
        director_block = lg.section(system, lg.DIRECTOR_HEADING, "## Your part")
        assert scenarios.WORLD_SECRET in scenario_block
        assert scenarios.ROLE_SECRET in scenario_block
        assert "The farm has been in the family for four generations." in scenario_block
        assert scenario_block.rstrip().endswith(lg.PREVAIL)
        assert scenarios.GUIDANCE_SECRET in director_block
        assert scenarios.GUIDANCE_SECRET not in scenario_block
        assert "The brother forged the second sale." in director_block
        assert "The brother forged the second sale." not in scenario_block
        assert "A stranger asks for the deed." in director_block
        notes = system.index("### Author notes: June Harrow")
        assert system.index(lg.SCENARIO_HEADING) < notes < system.index(lg.DIRECTOR_HEADING)
        sent = everything_sent(stub)
        for phrase in lg.PUBLIC_PHRASES:
            assert phrase not in sent, phrase

        # The genesis operation keeps the full report, for an omniscient read only.
        async with engine.connect() as conn:
            genesis_op = await conn.scalar(
                text(
                    "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'genesis'"
                ),
                {"s": story_id},
            )
        omniscient = await client.get(
            f"/operations/{genesis_op}", params={"viewpoint": "omniscient"}, headers=g.READ
        )
        assert omniscient.status_code == 200, omniscient.text
        assert omniscient.json()["creation_report"] == report
        plain = await client.get(f"/operations/{genesis_op}", headers=g.READ)
        assert plain.json()["creation_report"] is None
