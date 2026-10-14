"""I-27: genesis by capability (docs/04 §4), its M3 parts.

Every I-27 (M3) part lives in this file (traceability counts a test ID per milestone in one
file): #119's ``test_i_27__atomic`` (FR-SCN-9) here, and #122's materialisation by capability
(the Lite prompt, the deferred and inactive report) joins it. The scenario is
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
from tests import scenario_support as scenarios
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
