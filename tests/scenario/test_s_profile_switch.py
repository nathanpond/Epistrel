"""S-PROFILE-SWITCH (docs/04 §7; FR-PROF-6,7,8,9,12,18, FR-LITE-6, FR-CONC-14,15,16; #232): one
story moves Lite → Full → (a behavioral edit) → Standard → Full, and every response reports the
profile version it ran under.

Reduced from the plan's 200-turn diner run (logged in ``.n8/decisions.md``): the story plays its
Lite turns by direct commits (``tests/fixtures/long_lite_story``) and its later turns through
``respond`` on the stub. Full and Standard are copies of the presets, so the behavioral edit
touches no other story.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.seeds.profiles import FULL_PRESET_ID, STANDARD_PRESET_ID
from epistrel.orchestrator.jobs.handlers import backfill as backfill_job
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.fixtures import long_lite_story as lls
from tests.integration.test_i_19 import run_backfill
from tests.respond_support import body as respond_body
from tests.respond_support import new_story, respond_client

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

WRITE = {**bearer(), "X-Epistrel-Principal": "p-profile-switch"}
READ = bearer()
PROFILES = "/engine-profiles"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


async def _copy(client: Any, preset: str, name: str) -> tuple[str, dict[str, Any]]:
    copied = await _ok(
        await client.post(f"{PROFILES}/{preset}/copy", json={"name": name}, headers=WRITE), 201
    )
    return copied["id"], copied["settings"]


async def _play(client: Any, story: str, actor: str) -> dict[str, Any]:
    """One turn; its reported profile and fingerprint are the story's effective ones."""
    played = await _ok(
        await client.post(f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE)
    )
    shown = await _ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
    assert played["profile"] == {
        "id": shown["profile"]["id"],
        "version": shown["profile"]["version"],
    }
    assert played["fingerprint"] == shown["fingerprint"]
    return played


async def _backfills(engine: AsyncEngine, story: str) -> int:
    return int(
        await scalar(engine, "SELECT count(*) FROM story_backfills WHERE story_id = :s", s=story)
    )


@pytest.mark.part("M7", "FR-PROF-6,7,8,9,12,18; FR-LITE-6; FR-CONC-14,15,16")
async def test_s_profile_switch(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Lite (twenty turns) → Full: the report lists the capabilities gained and the backfill
    estimate; confirmed, the story pauses, backfills and resumes under Full. A temperature
    edit to Full applies at the next turn with no backfill. Standard (nested beliefs off) keeps
    the existing nested-belief rows and writes no backfill. Back to Full after more turns: the
    backfill covers only the turns since the drop. Every reply reports the version it ran
    under."""
    async with respond_client(stub_settings, engine) as (client, lib):
        full, full_settings = await _copy(client, FULL_PRESET_ID, "Switch Full")
        standard, _ = await _copy(client, STANDARD_PRESET_ID, "Switch Standard")
        story, ids = await new_story(client, "Ada", "Bobby", "Cal")
        actor = ids["Ada"]
        await lls.long_lite_story(engine, story, actor, 40)
        assert lib.models.stub is not None
        lls.script(lib.models.stub)
        backfill_job.bind(lib.models)
        await _play(client, story, actor)

        held = await _ok(
            await client.put(
                f"/stories/{story}/engine-profile", json={"profile_id": full}, headers=WRITE
            )
        )
        (entry,) = held["impact"]["affected_stories"]
        assert held["applied"] is False
        assert entry["capabilities_gained"]
        assert entry["backfill"]["needed"] is True
        assert entry["backfill"]["estimate"]["model_calls"] > 0
        await _ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": full, "ignore_warnings": True},
                headers=WRITE,
            ),
            202,
        )
        assert await scalar(engine, "SELECT status FROM stories WHERE id = :s", s=story) == (
            "paused"
        )
        await run_backfill(engine, story)
        on_full = await _play(client, story, actor)
        assert on_full["profile"] == {"id": full, "version": 1}

        # A behavioral edit: next turn, no backfill.
        count = await _backfills(engine, story)
        warmer = {**full_settings}
        warmer["models"] = {
            **dict(full_settings.get("models") or {}),
            "generation": {
                **dict((full_settings.get("models") or {}).get("generation") or {}),
                "temperature": 0.33,
            },
        }
        edited = await _ok(
            await client.put(
                f"{PROFILES}/{full}",
                json={"name": "Switch Full", "settings": warmer, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        assert edited["applied"] is True
        assert edited["backfills"] == []
        assert (await _play(client, story, actor))["profile"] == {"id": full, "version": 2}
        assert await _backfills(engine, story) == count

        # Standard: a decrease keeps what was written, and writes no backfill.
        beliefs = "SELECT id, membership::text FROM beliefs WHERE story_id = :s ORDER BY id"
        nested = [tuple(r) for r in await rows(engine, beliefs, s=story)]
        down = await _ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": standard, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        assert down["applied"] is True
        assert await _backfills(engine, story) == count
        drop = (await _play(client, story, actor))["turn"]
        assert [tuple(r) for r in await rows(engine, beliefs, s=story)] == nested

        # Back to Full after more turns: only the turns since the drop are backfilled.
        await _play(client, story, actor)
        back = await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": full, "ignore_warnings": True},
            headers=WRITE,
        )
        if back.status_code == 202:
            latest = await _ok(await client.get(f"/stories/{story}/backfill", headers=READ))
            assert latest["range"]["from_turn"] >= drop - 1
            await run_backfill(engine, story)
        else:
            assert back.status_code == 200, back.text
        final = await _play(client, story, actor)
        assert final["profile"]["id"] == full


@pytest.mark.part("M7", "FR-PROF-6,7,8,9,12,18; FR-LITE-6; FR-CONC-14,15,16")
async def test_s_profile_switch__round_trip(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#709 (#232's S-PROFILE-SWITCH, the round trip): a 200-turn Lite era with pins and
    custom instructions and the prompt-only clock (anchored at 11:00); its last turns are
    played, so they record their pacing advances; Jenni's meeting is planted. To Full: the
    story backfills and resumes, Jenni's meeting is a canon scheduled event of hers, and the
    clock reads the fold of the recorded advances (the Lite era's pacing kept, nothing
    re-derived on top, #228's rule). Back to Lite: the downgrade keeps the structured data
    dormant and rebuilds the summary; the Lite prompt again carries the pins and the custom
    instructions; every reply reports the profile it ran under."""
    import json

    from epistrel.core.propositions import store
    from epistrel.core.seeds.profiles import LITE_PRESET_ID
    from epistrel.orchestrator.models.registry import EXTRACTION_BACKSTOP
    from tests import clock_support as ck
    from tests.concurrency_support import set_instructions, set_pins

    async with respond_client(stub_settings, engine) as (client, lib):
        full, _ = await _copy(client, FULL_PRESET_ID, "Round Full")
        lite, _ = await _copy(client, LITE_PRESET_ID, "Round Lite")
        story, ids = await new_story(client, "Ada", "Bobby", "Jenni")
        actor = ids["Ada"]
        await _ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": lite, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        await _ok(
            await client.patch(
                f"/stories/{story}/profile-overrides",
                json={"settings": {"clock": {"level": "prompt_only"}}, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        await ck.anchor_at(engine, story, 11 * 60)
        async with engine.begin() as conn:
            await store.add_entity(conn, story, "event", "the meeting")
        await lls.long_lite_story(engine, story, actor, 400)  # 200 turns
        await set_pins(engine, story, "The diner's neon sign hums.")
        await set_instructions(engine, story, "Keep replies short.")
        stub = lib.models.stub
        assert stub is not None
        lls.script(stub)
        stub.when(
            "Line 5.",
            json.dumps(
                {
                    "claims": [
                        {
                            "beat": 0,
                            "span": "Line 5",
                            "subject": "Jenni",
                            "predicate": "scheduled",
                            "object": "the meeting",
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
        for _ in range(3):
            lite_turn = await _play(client, story, actor)
        prompt = "\n".join(
            m.content for c in stub.calls if c.purpose == "generation" for m in c.messages
        )
        assert "The diner's neon sign hums." in prompt
        assert "Keep replies short." in prompt
        paced = await rows(
            engine,
            "SELECT c.delta FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
            "WHERE c.story_id = :s AND d.name = 'world_clock' AND c.membership = 'active'",
            s=story,
        )
        assert paced, "the played Lite turns recorded their pacing"
        before = sum(int(p.delta) for p in paced)

        await _ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": full, "ignore_warnings": True},
                headers=WRITE,
            ),
            202,
        )
        await run_backfill(engine, story)
        on_full = await _play(client, story, actor)
        assert on_full["profile"]["id"] == full
        meetings = await rows(
            engine,
            "SELECT s.status, f.subject_id FROM scheduled_events s JOIN facts f "
            "ON f.proposition_id = s.proposition_id WHERE s.story_id = :s",
            s=story,
        )
        assert {(m.status, str(m.subject_id)) for m in meetings} == {("pending", ids["Jenni"])}
        clock = await _ok(await client.get(f"/stories/{story}/clock", headers=READ))
        assert clock["offset"] >= before  # the Lite era's pacing, kept; Full's turn adds its own

        stub.reset()
        down = await _ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": lite, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        (entry,) = down["impact"]["affected_stories"]
        assert entry["data_kept_dormant"] is True
        back = await _play(client, story, actor)
        assert back["profile"]["id"] == lite
        prompt = "\n".join(
            m.content for c in stub.calls if c.purpose == "generation" for m in c.messages
        )
        assert "The diner's neon sign hums." in prompt
        assert "Keep replies short." in prompt
        del lite_turn
