"""Changing a story's content rating (FR-RATE-6,7; #118).

``test_i_22__relax_tighten`` is I-22's M3 relax and tighten slice. Every I-22 (M3) part lives in
this file (docs/04 §15 counts one file per test ID and milestone): #119's creation check and
#120's reactivation re-checks add their slices here. The detailed tests follow it. Each runs on
its own scratch database (``tests/rating_change_support.rating_db``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from epistrel.core.ratings import change as rating_change
from epistrel.core.ratings import reactivation
from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID, MATURE_ID, TEEN_ID
from epistrel.engine import RATING_CHECK_HOOK, _end_rating_checks
from epistrel.orchestrator.jobs.handlers import rating_check as rating_check_job
from epistrel.orchestrator.jobs.handlers import retention
from epistrel.orchestrator.jobs.worker import (
    Worker,
    WorkerConfig,
    recovery_pass,
    register_recovery_hook,
    unregister_recovery_hook,
)
from epistrel.orchestrator.policies.schemas import PolicyFields
from tests import genesis_support as g
from tests import scenario_support as scenarios
from tests.catalog_support import dress_companion, read_companion
from tests.rating_change_support import (
    AUTHOR,
    OPERATOR,
    Scratch,
    change_rating,
    check,
    due_now,
    guard_rating,
    rating_db,
    rows,
    run_jobs,
    scalar,
    story,
    story_counts,
)
from tests.respond_support import FIRST, PLAYER, body, everything_sent, new_story, played, respond
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration

COMPANION_SECRET = "Linda kept the letters in the coal shed"
"""A phrase only Linda's companion's world holds (FR-CAT-5)."""


async def _version(s: Scratch, rating_id: str) -> str:
    return str(
        await scalar(
            s.db, "SELECT current_version_id FROM content_ratings WHERE id = :r", r=rating_id
        )
    )


async def _turn_rows(s: Scratch, story_id: str) -> list[Any]:
    return await rows(
        s.db,
        "SELECT operation_id, attempts, config_revisions, checked_guardrails FROM operations "
        "WHERE story_id = :s AND kind = 'turn' AND status = 'committed' "
        "AND membership = 'active' ORDER BY narrative_turn",
        s=story_id,
    )


async def _played_story(s: Scratch, rating_id: str, *replies: str) -> tuple[str, dict[str, str]]:
    """A story at ``rating_id`` with one played turn per reply."""
    story_id, ids = await new_story(s.client, PLAYER, FIRST, content_rating_id=rating_id)
    stub = s.lib.models.stub
    for reply in replies:
        stub.script("generation", [reply])
        await played(s.client, story_id, body(ids[PLAYER]))
    return story_id, ids


def _guardrail_calls(s: Scratch) -> int:
    return sum(1 for call in s.lib.models.stub.calls if call.purpose == "guardrail_check")


@pytest.mark.part("M3", "FR-RATE-6,7")
async def test_i_22__relax_tighten(pg_url: str) -> None:
    """Relaxing Teen to Explicit applies with no content check (no guardrail model call),
    records the acting principal, and a turn already generating commits under Explicit.
    Tightening Explicit to Teen on a history with an explicit turn pauses the story: a respond
    answers ``story_paused`` and records nothing, while reads are served; the change is refused
    with the violation's position and policy, the rating is unchanged, and the story plays
    again. On a clean history the same change applies and the turn records the new guardrail
    set.

    Complements asserted: the refused respond's counts (no event, no operation); the refused
    check's body names no text; the applied change moves the rating and its
    ``checked_guardrails``."""
    async with rating_db(pg_url) as s:
        dragons = await guard_rating(s)
        stub = s.lib.models.stub

        # Relaxing, with a turn in flight: Teen -> Explicit while the reply is generating.
        relaxed, ids = await new_story(s.client, PLAYER, FIRST, content_rating_id=TEEN_ID)
        stub.reset()
        stub.script("generation", ["A quiet evening falls."])
        latch = stub.hold("generation")
        turn = asyncio.ensure_future(respond(s.client, relaxed, body(ids[PLAYER])))
        await asyncio.wait_for(latch.entered.wait(), 5)
        before = _guardrail_calls(s)
        response = await change_rating(s.client, relaxed, EXPLICIT_ID)
        assert response.status_code == 200, response.text
        applied = response.json()
        assert applied["applied"] is True
        assert applied["changed"] is True
        assert applied["rating_version"] == await _version(s, EXPLICIT_ID)
        assert _guardrail_calls(s) == before
        latch.release()
        answered = await turn
        assert answered.status_code == 200, answered.text
        assert answered.json()["message"]["text"] == "A quiet evening falls."
        (row,) = await _turn_rows(s, relaxed)
        assert row.config_revisions["rating_version"] == await _version(s, EXPLICIT_ID)
        assert row.checked_guardrails["rating_id"] == EXPLICIT_ID
        assert "content_rating_id" in row.attempts[0]["config_changed"]
        (change,) = await rows(
            s.db,
            "SELECT setting, old, new, class, actor FROM config_changes "
            "WHERE scope = 'story' AND scope_id = :s AND setting = 'content_rating_id'",
            s=relaxed,
        )
        assert tuple(change) == ("content_rating_id", TEEN_ID, EXPLICIT_ID, "interpretive", AUTHOR)

        # Tightening an explicit history: Explicit -> Teen.
        explicit, ids = await _played_story(s, EXPLICIT_ID, "A dragon lands on the roof.")
        started = await change_rating(s.client, explicit, TEEN_ID)
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        assert started.json()["status"] == "checking"
        assert started.headers["Location"] == (
            f"/stories/{explicit}/content-rating/checks/{job_id}"
        )
        counted = await story_counts(s.db, explicit)
        refused = await respond(s.client, explicit, body(ids[PLAYER], "Again?"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "story_paused"
        assert refused.json()["error"]["details"]["job_id"] == job_id
        assert await story_counts(s.db, explicit) == counted
        paused = await story(s.client, explicit)
        assert paused["status"] == "paused"
        assert paused["pause"]["job_id"] == job_id
        history = await s.client.get(f"/stories/{explicit}/history", headers=READ)
        assert history.status_code == 200, history.text
        assert (await check(s.client, explicit, job_id))["status"] == "checking"

        assert await run_jobs(s.db) >= 1
        result = await check(s.client, explicit, job_id)
        assert result["status"] == "refused"
        assert result["violations"] == [
            {
                "position": {"turn": 1, "variant": 1, "beat": 2},
                "policy_id": dragons.id,
                "policy_name": dragons.name,
                "code": None,
            }
        ]
        assert "dragon lands" not in str(result).lower()
        after = await story(s.client, explicit)
        assert after["status"] == "active"
        assert after["pause"] is None
        assert after["content_rating"]["id"] == EXPLICIT_ID
        stub.script("generation", ["The wind turns."])
        await played(s.client, explicit, body(ids[PLAYER], "And now?"))

        # The same change on a clean history applies.
        clean, _ = await _played_story(s, EXPLICIT_ID, "A cat purrs by the fire.")
        started = await change_rating(s.client, clean, TEEN_ID)
        assert started.status_code == 202, started.text
        clean_job = started.json()["job_id"]
        await run_jobs(s.db)
        done = await check(s.client, clean, clean_job)
        assert done["status"] == "applied"
        assert done["violations"] == []
        assert done["rating_version"] == await _version(s, TEEN_ID)
        assert (await story(s.client, clean))["content_rating"]["id"] == TEEN_ID
        (row,) = await _turn_rows(s, clean)
        assert row.checked_guardrails["rating_id"] == TEEN_ID
        assert dragons.id in row.checked_guardrails["rating"]


async def test_tightening_an_empty_history_applies(pg_url: str) -> None:
    """A tightening over a story with no turns at all applies, and its record is kept: the
    progress, who asked, the config change (recorded with that principal) and the resume."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        empty, _ = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        started = await change_rating(s.client, empty, TEEN_ID)
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        await run_jobs(s.db)
        done = await check(s.client, empty, job_id)
        assert done["status"] == "applied"
        assert done["progress"] == {"done": 0, "total": 0}
        assert done["started_by"] == AUTHOR
        assert done["target"] == {"rating_id": TEEN_ID, "version": await _version(s, TEEN_ID)}
        assert (await story(s.client, empty))["content_rating"]["id"] == TEEN_ID
        types = [
            r[0]
            for r in await rows(
                s.db,
                "SELECT type FROM events WHERE story_id = :s AND category = 'control' ORDER BY seq",
                s=empty,
            )
        ]
        assert types[-3:] == ["story_paused", "config_changed", "story_resumed"]
        (actor,) = await rows(
            s.db,
            "SELECT actor FROM config_changes WHERE scope_id = :s AND setting = :n",
            s=empty,
            n="content_rating_id",
        )
        assert actor[0] == AUTHOR


async def test_naming_the_current_rating_changes_nothing(pg_url: str) -> None:
    async with rating_db(pg_url) as s:
        story_id, _ = await new_story(s.client, PLAYER, FIRST, content_rating_id=TEEN_ID)
        before = await story_counts(s.db, story_id)
        response = await change_rating(s.client, story_id, TEEN_ID)
        assert response.status_code == 200, response.text
        assert response.json()["changed"] is False
        assert await story_counts(s.db, story_id) == before


async def test_equal_guardrails_relax_and_a_retired_target_is_refused(pg_url: str) -> None:
    """Two ratings with the same (empty) guardrails relax either way, and a turn generating
    meanwhile still commits under the new rating (its identity is a dependency, not only its
    guardrails); a target that cannot be assigned is 422 ``rating_retired``; an unknown story
    is 404."""
    async with rating_db(pg_url) as s:
        story_id, ids = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        stub = s.lib.models.stub
        stub.script("generation", ["A quiet evening."])
        latch = stub.hold("generation")
        turn = asyncio.ensure_future(respond(s.client, story_id, body(ids[PLAYER])))
        await asyncio.wait_for(latch.entered.wait(), 5)
        response = await change_rating(s.client, story_id, EVERYONE_ID)
        assert response.status_code == 200, response.text
        latch.release()
        assert (await turn).status_code == 200
        (row,) = await _turn_rows(s, story_id)
        assert row.config_revisions["rating_version"] == await _version(s, EVERYONE_ID)
        assert row.checked_guardrails["rating_id"] == EVERYONE_ID
        missing = await change_rating(s.client, story_id, "00000000000000000000RATXXX")
        assert missing.status_code == 422, missing.text
        assert missing.json()["error"]["code"] == "rating_retired"
        unknown = await change_rating(s.client, "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3", TEEN_ID)
        assert unknown.status_code == 404


async def test_a_pause_refuses_truth_changes_and_serves_the_rest(pg_url: str) -> None:
    """While the check runs, every truth-changing call is 409 ``story_paused`` with the check's
    job and progress (ingest, pinned memories, custom instructions, truncate, archive, fork, a
    second rating change, the story's own policies), and records nothing; reads, a settings
    patch and feedback are served. A check that refuses resumes the story either way."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, ids = await _played_story(s, EXPLICIT_ID, "A dragon lands.")
        message_id = await scalar(
            s.db,
            "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=story_id,
        )
        started = await change_rating(s.client, story_id, TEEN_ID)
        job_id = started.json()["job_id"]
        before = await story_counts(s.db, story_id)
        base = f"/stories/{story_id}"
        refused = {
            "ingest": await s.client.post(
                f"{base}/ingest",
                json={"events": [{"client_event_id": "n-1", "kind": "narration", "text": "x"}]},
                headers=WRITE,
            ),
            "pins": await s.client.put(
                f"{base}/pinned-memories", json={"items": [{"text": "x"}]}, headers=WRITE
            ),
            "instructions": await s.client.put(
                f"{base}/custom-instructions", json={"text": "x"}, headers=WRITE
            ),
            "truncate": await s.client.post(f"{base}/truncate", json={"to_turn": 0}, headers=WRITE),
            "archive": await s.client.post(f"{base}/archive", headers=WRITE),
            "fork": await s.client.post(f"{base}/fork", json={}, headers=WRITE),
            "rating": await change_rating(s.client, story_id, EVERYONE_ID),
            "policies": await s.client.patch(
                f"{base}/settings",
                json={
                    "policies": [
                        {
                            "name": "own",
                            "rule": {"type": "wordlist", "words": ["x"]},
                            "message": "No.",
                        }
                    ]
                },
                headers=WRITE,
            ),
        }
        for name, response in refused.items():
            assert response.status_code == 409, (name, response.text)
            error = response.json()["error"]
            assert error["code"] == "story_paused", name
            assert error["details"]["job_id"] == job_id, name
            assert set(error["details"]["progress"]) == {"done", "total"}, name
        assert await story_counts(s.db, story_id) == before

        served = {
            "read": await s.client.get(base, headers=READ),
            "settings": await s.client.patch(
                f"{base}/settings", json={"turn_queue_max": 3}, headers=WRITE
            ),
            "feedback": await s.client.put(
                f"{base}/messages/{message_id}/feedback", json={"rating": 4}, headers=WRITE
            ),
        }
        for name, response in served.items():
            assert response.status_code in (200, 201), (name, response.text)
        await run_jobs(s.db)
        assert (await check(s.client, story_id, job_id))["status"] == "refused"
        assert (await story(s.client, story_id))["status"] == "active"
        stub = s.lib.models.stub
        stub.script("generation", ["Calm again."])
        await played(s.client, story_id, body(ids[PLAYER]))


async def test_the_check_waits_for_a_turn_in_flight(pg_url: str) -> None:
    """A turn generating when the tightening arrives still commits, and the check waits for it
    and checks it too: its reply breaks the target's policy, so the change is refused."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, ids = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        stub = s.lib.models.stub
        stub.script("generation", ["A dragon circles."])
        latch = stub.hold("generation")
        turn = asyncio.ensure_future(respond(s.client, story_id, body(ids[PLAYER])))
        await asyncio.wait_for(latch.entered.wait(), 5)
        started = await change_rating(s.client, story_id, TEEN_ID)
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        await run_jobs(s.db, runs=1)  # the turn holds the slot: the check waits
        assert (await check(s.client, story_id, job_id))["status"] == "checking"
        latch.release()
        assert (await turn).status_code == 200
        await due_now(s.db)
        await run_jobs(s.db)
        found = await check(s.client, story_id, job_id)
        assert found["status"] == "refused"
        assert [v["position"]["turn"] for v in found["violations"]] == [1]


async def test_queued_turns_fail_when_they_would_start(pg_url: str) -> None:
    """On a ``queue`` story, a turn waiting behind the one in flight when the tightening
    arrives fails ``story_paused`` when it would start; the one in flight commits."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, ids = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        settings = await s.client.patch(
            f"/stories/{story_id}/settings", json={"turn_concurrency": "queue"}, headers=WRITE
        )
        assert settings.status_code == 200, settings.text
        stub = s.lib.models.stub
        stub.script("generation", ["First.", "Second."])
        latch = stub.hold("generation")
        first = asyncio.ensure_future(respond(s.client, story_id, body(ids[PLAYER])))
        await asyncio.wait_for(latch.entered.wait(), 5)
        second = await respond(
            s.client, story_id, body(ids[PLAYER], "Me too"), {"Prefer": "respond-async"}
        )
        assert second.status_code == 202, second.text
        queued = second.json()["operation_id"]
        started = await change_rating(s.client, story_id, TEEN_ID)
        assert started.status_code == 202, started.text
        latch.release()
        assert (await first).status_code == 200
        await asyncio.wait_for(s.lib.drainer.idle(), 30)
        code = await scalar(
            s.db, "SELECT error_code FROM operations WHERE operation_id = :o", o=queued
        )
        assert code == "story_paused"


async def test_delete_during_the_check_ends_it(pg_url: str) -> None:
    """A paused story can be deleted; its check then finds nothing to do and ends."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, _ = await _played_story(s, EXPLICIT_ID, "A dragon lands.")
        started = await change_rating(s.client, story_id, TEEN_ID)
        assert started.status_code == 202
        deleted = await s.client.delete(f"/stories/{story_id}", headers=WRITE)
        assert deleted.status_code == 204, deleted.text
        assert await run_jobs(s.db) >= 1
        left = await scalar(
            s.db, "SELECT count(*) FROM rating_check_jobs WHERE story_id = :s", s=story_id
        )
        assert left == 0
        failed = await scalar(s.db, "SELECT count(*) FROM jobs WHERE status = 'failed'")
        assert failed == 0


async def _refused_positions(s: Scratch, story_id: str) -> list[dict[str, Any]]:
    """Tighten ``story_id`` from Explicit to Teen and run the check; its violations' positions
    (the check must refuse)."""
    started = await change_rating(s.client, story_id, TEEN_ID)
    assert started.status_code == 202, started.text
    await run_jobs(s.db)
    found = await check(s.client, story_id, started.json()["job_id"])
    assert found["status"] == "refused", found
    return [v["position"] for v in found["violations"]]


async def _event_position(s: Scratch, story_id: str, kind: str) -> dict[str, Any]:
    (row,) = await rows(
        s.db,
        "SELECT narrative_turn, turn_variant, beat FROM events WHERE story_id = :s AND type = :t",
        s=story_id,
        t=kind,
    )
    return {"turn": row.narrative_turn, "variant": row.turn_variant or 1, "beat": row.beat}


async def test_every_history_source_is_checked(pg_url: str) -> None:
    """A tightening checks every source FR-RATE-7 names, not only generated replies: on a
    story whose replies are clean, a violating player input, ingested narration, pinned
    memory, custom instructions or genesis seed (a scenario's seeded memory) each refuses the
    change, at its own position (#118 verification round 1)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s, applies_to=["text_input", "text_output"])
        stub = s.lib.models.stub
        base = "/stories/{}"

        # (a) a player input
        said, ids = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        stub.script("generation", ["A cat purrs by the fire."])
        await played(s.client, said, body(ids[PLAYER], "I saw a dragon."))
        assert await _refused_positions(s, said) == [await _event_position(s, said, "player_input")]

        # (b) an ingested narration event
        ingested, _ = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        sent = await s.client.post(
            f"{base.format(ingested)}/ingest",
            json={
                "events": [
                    {"client_event_id": "n-1", "kind": "narration", "text": "A dragon sleeps."}
                ]
            },
            headers=WRITE,
        )
        assert sent.status_code in (200, 201), sent.text
        position = await _event_position(s, ingested, "message_rendered")
        assert position["turn"] is not None
        assert await _refused_positions(s, ingested) == [position]

        # (c) a pinned memory
        pinned, _ = await _played_story(s, EXPLICIT_ID, "A cat purrs by the fire.")
        put = await s.client.put(
            f"{base.format(pinned)}/pinned-memories",
            json={"items": [{"text": "The cat is calm."}, {"text": "The dragon is real."}]},
            headers=WRITE,
        )
        assert put.status_code == 200, put.text
        pin_id = put.json()["items"][1]["id"]
        assert await _refused_positions(s, pinned) == [{"kind": "pin", "id": pin_id}]

        # (d) the custom instructions
        instructed, _ = await _played_story(s, EXPLICIT_ID, "A cat purrs by the fire.")
        put = await s.client.put(
            f"{base.format(instructed)}/custom-instructions",
            json={"text": "Bring a dragon into every scene."},
            headers=WRITE,
        )
        assert put.status_code == 200, put.text
        assert await _refused_positions(s, instructed) == [
            {"kind": "instruction", "id": "custom_instructions"}
        ]

        # (e) a genesis seed: the scenario's seeded memory, created at the scenario's rating
        tom = await g.catalog_character_id(s.client, "Tom Harrow", age=44, rating=EXPLICIT_ID)
        scenario = g.scenario_body(
            g.role("mother"), g.role("player", kind="player"), rating=EXPLICIT_ID
        )
        scenario["starting_state"]["seeded_memories"] = [
            {
                "text": "A dragon sleeps under the barn.",
                "holder": "world",
                "audience": ["public"],
                "disclosure": "open",
            }
        ]
        scenario_id, _ = await g.published_scenario(s.client, scenario)
        made = await g.create_from(
            s.client, g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(tom)})
        )
        assert made["content_rating"]["id"] == EXPLICIT_ID
        assert await _refused_positions(s, made["story_id"]) == [
            {"kind": "seed", "id": "starting_state.seeded_memories[0].text"}
        ]


async def test_a_story_deleted_mid_check_keeps_no_verdicts(pg_url: str) -> None:
    """A story deleted while its check waits on a model call takes the check and its cached
    verdicts with it: the run's next checkpoint finds the check gone, stores nothing and ends
    (``save_progress``), and the job does not fail (#118 verification round 1)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s, rule={"type": "model_assisted", "prompt": "Is it gory?"})
        story_id, _ = await _played_story(s, EXPLICIT_ID, "Snow falls.")
        stub = s.lib.models.stub
        stub.reset()
        stub.when("Snow falls", '{"violation": false, "confidence": 0.9}', "guardrail_check")
        latch = stub.hold("guardrail_check")
        started = await change_rating(s.client, story_id, TEEN_ID)
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        running = asyncio.ensure_future(run_jobs(s.db, runs=1))
        await asyncio.wait_for(latch.entered.wait(), 10)
        deleted = await s.client.delete(f"/stories/{story_id}", headers=WRITE)
        assert deleted.status_code == 204, deleted.text
        latch.release()
        assert await asyncio.wait_for(running, 30) == 1
        await run_jobs(s.db)
        for table in ("guardrail_check_results", "rating_check_jobs"):
            left = await scalar(
                s.db,
                f"SELECT count(*) FROM {table} WHERE story_id = :s",  # noqa: S608
                s=story_id,
            )
            assert left == 0, table
        assert await scalar(s.db, "SELECT status FROM jobs WHERE id = :j", j=job_id) == "done"


def _capped_worker(s: Scratch) -> Worker:
    return Worker(
        s.db,
        WorkerConfig(
            poll_interval=0.2,
            lease_s=30.0,
            listen=False,
            concurrency=1,
            timers=False,
            max_runtime_s=1.0,
        ),
    )


async def _paused_status(s: Scratch, story_id: str, job_id: str) -> tuple[str, str]:
    (row,) = await rows(
        s.db,
        "SELECT s.status, c.status AS check_status FROM stories s "
        "JOIN rating_check_jobs c ON c.id = :j WHERE s.id = :s",
        s=story_id,
        j=job_id,
    )
    return row.status, row.check_status


async def test_a_last_attempt_the_runtime_cap_ends_resumes_the_story(pg_url: str) -> None:
    """The worker's runtime cap ends the check's last attempt without the handler's failure
    path running (the handler is cancelled), so the job fails with the check still
    ``checking`` and the story still paused. The next read of the check ends it ``failed``
    with the job's error class and resumes the story on its old rating
    (``change.reconcile``; #118 verification round 1)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s, rule={"type": "model_assisted", "prompt": "Is it gory?"})
        story_id, ids = await _played_story(s, EXPLICIT_ID, "Snow falls.")
        stub = s.lib.models.stub
        stub.reset()
        latch = stub.hold("guardrail_check")
        started = await change_rating(s.client, story_id, TEEN_ID)
        job_id = started.json()["job_id"]
        async with s.db.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET failures = :f WHERE id = :j"),
                {"f": rating_check_job.MAX_ATTEMPTS - 1, "j": job_id},
            )
        assert await _capped_worker(s).run_one("rating-test:capped")
        latch.release()
        assert await scalar(s.db, "SELECT status FROM jobs WHERE id = :j", j=job_id) == "failed"
        assert await _paused_status(s, story_id, job_id) == ("paused", "checking")
        found = await check(s.client, story_id, job_id)
        assert found["status"] == "failed"
        assert found["error"]["class"] == "TimeoutError"
        assert found["finished_at"] is not None
        after = await story(s.client, story_id)
        assert after["status"] == "active"
        assert after["pause"] is None
        assert after["content_rating"]["id"] == EXPLICIT_ID
        stub.script("generation", ["Calm again."])
        await played(s.client, story_id, body(ids[PLAYER]))


async def test_a_check_whose_job_is_gone_is_ended(pg_url: str) -> None:
    """A paused story whose check's job row is gone is resumed by the next rating change on it
    (which then starts its own check, not 409 ``story_paused``), by the retention sweep and by
    the worker's recovery pass (#118 verification round 1)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        stories_ = [(await _played_story(s, EXPLICIT_ID, "A cat purrs."))[0] for _ in range(3)]
        jobs_ = []
        for story_id in stories_:
            started = await change_rating(s.client, story_id, TEEN_ID)
            assert started.status_code == 202, started.text
            jobs_.append(started.json()["job_id"])
        async with s.db.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE id = ANY(:j)"), {"j": jobs_})

        # The next rating change on the story ends the old check and starts its own.
        again = await change_rating(s.client, stories_[0], TEEN_ID)
        assert again.status_code == 202, again.text
        assert again.json()["job_id"] != jobs_[0]
        old = await check(s.client, stories_[0], jobs_[0])
        assert (old["status"], old["error"]["class"]) == ("failed", "JobMissing")

        # The retention sweep ends the rest.
        swept = await retention.sweep_retention(s.db)
        assert swept["rating_checks_ended"] == 2
        for story_id, job_id in zip(stories_[1:], jobs_[1:], strict=True):
            assert await _paused_status(s, story_id, job_id) == ("active", "failed")

        # And so does the worker's recovery pass, through the Engine's hook.
        fourth, _ = await _played_story(s, EXPLICIT_ID, "A cat purrs.")
        started = await change_rating(s.client, fourth, TEEN_ID)
        async with s.db.begin() as conn:
            await conn.execute(
                text("DELETE FROM jobs WHERE id = :j"), {"j": started.json()["job_id"]}
            )
        register_recovery_hook(RATING_CHECK_HOOK, _end_rating_checks)
        try:
            report = await recovery_pass(s.db)
        finally:
            unregister_recovery_hook(RATING_CHECK_HOOK)
        assert RATING_CHECK_HOOK in report.hooks
        assert await _paused_status(s, fourth, started.json()["job_id"]) == ("active", "failed")


async def test_model_assisted_policies_are_cached_and_never_quoted(pg_url: str) -> None:
    """A model-assisted target policy is checked through ``guardrail_check``, outside any turn
    budget, and its verdicts are stored: the same check asked again (after the refusal)
    makes no new model call. The refusal writes an audit row naming the job, not the text."""
    async with rating_db(pg_url) as s:
        gore = await guard_rating(
            s,
            rule={"type": "model_assisted", "prompt": "Is it gory?"},
            name="no-gore",
            message="No gore.",
        )
        story_id, _ = await _played_story(s, EXPLICIT_ID, "Blood on the snow.")
        stub = s.lib.models.stub
        stub.reset()
        stub.when("Blood on the snow", '{"violation": true, "confidence": 0.9}', "guardrail_check")
        started = await change_rating(s.client, story_id, TEEN_ID)
        await run_jobs(s.db)
        first = await check(s.client, story_id, started.json()["job_id"])
        assert [v["policy_id"] for v in first["violations"]] == [gore.id]
        calls = _guardrail_calls(s)
        assert calls >= 1
        stored = await scalar(
            s.db,
            "SELECT count(*) FROM guardrail_check_results WHERE story_id = :s AND verdict = :v",
            s=story_id,
            v="violation",
        )
        assert stored >= 1
        again = await change_rating(s.client, story_id, TEEN_ID)
        await run_jobs(s.db)
        second = await check(s.client, story_id, again.json()["job_id"])
        assert second["status"] == "refused"
        assert _guardrail_calls(s) == calls
        (audit,) = await rows(
            s.db,
            "SELECT principal, policy_ids, details FROM audit_log "
            "WHERE kind = 'rating_change_refused' AND details->>'job_id' = :j",
            j=started.json()["job_id"],
        )
        assert audit.principal == AUTHOR
        assert audit.policy_ids == [gore.id]
        assert "Blood" not in str(audit.details)


async def test_a_model_that_never_answers_fails_the_check_and_resumes(pg_url: str) -> None:
    """When the model cannot answer on the check's last attempt, the check ends ``failed``
    with the error's class only, and the story resumes on its old rating."""
    async with rating_db(pg_url) as s:
        await guard_rating(s, rule={"type": "model_assisted", "prompt": "Is it gory?"})
        story_id, _ = await _played_story(s, EXPLICIT_ID, "Snow falls.")
        stub = s.lib.models.stub
        stub.reset()
        stub.fault("guardrail_check", "http_401", n=50)
        started = await change_rating(s.client, story_id, TEEN_ID)
        job_id = started.json()["job_id"]
        async with s.db.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET failures = :f WHERE id = :j"),
                {"f": rating_check_job.MAX_ATTEMPTS - 1, "j": job_id},
            )
        await run_jobs(s.db)
        found = await check(s.client, story_id, job_id)
        assert found["status"] == "failed"
        assert set(found["error"]) == {"class", "message"}
        after = await story(s.client, story_id)
        assert after["status"] == "active"
        assert after["content_rating"]["id"] == EXPLICIT_ID


async def test_a_check_of_another_story_is_not_found(pg_url: str) -> None:
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        one, _ = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        other, _ = await new_story(s.client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        started = await change_rating(s.client, one, TEEN_ID)
        job_id = started.json()["job_id"]
        response = await s.client.get(
            f"/stories/{other}/content-rating/checks/{job_id}", headers=READ
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        replay = await change_rating(
            s.client, one, TEEN_ID, **{"Idempotency-Key": started.headers["Idempotency-Key"]}
        )
        assert replay.status_code == 202
        assert replay.json()["job_id"] == job_id


# -- #119: creating a story from a scenario ---------------------------------------------------


async def _companion_version(s: Scratch, character_id: str) -> tuple[str, int, int]:
    """The character's companion: its id, its latest version and the character version that
    version pins."""
    found = await s.client.get(f"/catalog/characters/{character_id}/companion", headers=READ)
    assert found.status_code == 200, found.text
    companion = found.json()
    pinned = await scalar(
        s.db,
        "SELECT pinned_character_version FROM catalog.scenario_versions "
        "WHERE catalog_id = :c AND version = :v",
        c=companion["id"],
        v=companion["version"],
    )
    return companion["id"], int(companion["version"]), int(pinned)


async def _snapshot_pairs(s: Scratch, story_id: str) -> list[tuple[str, int]]:
    found = await rows(
        s.db,
        "SELECT source_catalog_id, version FROM character_snapshots WHERE story_id = :s",
        s=story_id,
    )
    return [(str(r.source_catalog_id), int(r.version)) for r in found]


async def _story_text(s: Scratch, story_id: str) -> str:
    """Every row the story holds, as text."""
    parts: list[str] = []
    for table in g.COUNTED:
        column = "id" if table == "stories" else "story_id"
        found = await rows(
            s.db,
            f'SELECT t::text AS row FROM "{table}" t WHERE t."{column}" = :s',  # noqa: S608
            s=story_id,
        )
        parts.extend(str(r.row) for r in found)
    return "\n".join(parts)


@pytest.mark.part("M3", "FR-SCN-5,6; FR-RATE-3,5,7; FR-CAT-10")
async def test_i_22__creation(pg_url: str) -> None:
    """A story created from a Teen scenario is Teen. An Explicit catalog character cast in an
    Everyone scenario is a ``rating_looser`` warning and the creation succeeds; the character
    plays under Everyone: a stubbed render that breaks Everyone's guardrail is caught, and the
    turn records Everyone's guardrail set. Creating at Teen from a Mature scenario whose seed
    breaks a Teen policy is refused (``rating_check_failed``, by location and policy) and
    creates nothing. The same request under the same key replays the result. Every story
    created from a character's companion versions snapshots exactly the pinned character
    version, as a one-to-one story (the character and the player), even with no world or
    opening; another scenario casting the character brings none of the companion's world.

    Complements asserted: the row counts move for a creation that succeeds; the Teen request
    over the clean Mature scenario applies; a story at the scenario's own rating needs no
    check."""
    async with rating_db(pg_url) as s:
        c = s.client
        stub = s.lib.models.stub
        roles = (g.role("mother"), g.role("player", kind="player"))

        # A Teen scenario makes a Teen story.
        tom = await g.catalog_character_id(c, "Tom Harrow", age=44, rating=TEEN_ID)
        teen, _ = await g.published_scenario(c, g.scenario_body(*roles, rating=TEEN_ID))
        made = await g.create_from(c, g.from_scenario(teen, slots={"mother": g.catalog_slot(tom)}))
        assert made["content_rating"]["id"] == TEEN_ID
        assert (await story(c, made["story_id"]))["content_rating"]["id"] == TEEN_ID

        # An Explicit character in an Everyone scenario: a warning, and Everyone governs.
        dragons = await guard_rating(s, EVERYONE_ID)
        rex = await g.catalog_character_id(c, "Rex Dane", age=35, rating=EXPLICIT_ID)
        everyone, version = await g.published_scenario(c, g.scenario_body(*roles))
        before = await g.counts(s.db)
        cast = await g.create_from(
            c,
            g.from_scenario(everyone, slots={"mother": g.catalog_slot(rex)}, projection="full"),
        )
        after = await g.counts(s.db)
        assert after["stories"] == before["stories"] + 1
        assert after["character_snapshots"] == before["character_snapshots"] + 1
        assert cast["content_rating"]["id"] == EVERYONE_ID
        looser = [w for w in cast["report"]["warnings"] if w["code"] == "rating_looser"]
        assert [(w["location"]["role"], w["source"]) for w in looser] == [("mother", f"{rex}@1")]
        assert cast["report"]["counts"]["errors"] == 0
        story_id = cast["story_id"]
        ids = {ch["role_id"]: ch["id"] for ch in cast["characters"]}
        stub.reset()
        stub.script("generation", ["A dragon lands on the roof."] * 3)
        played_turn = await played(
            c, story_id, body(ids["player"], render_character_ids=[ids["mother"]])
        )
        assert played_turn["guardrail"]["fallback_used"] is True
        assert "dragon" not in played_turn["message"]["text"].lower()
        stamp = (await _turn_rows(s, story_id))[-1].checked_guardrails
        assert stamp["rating_id"] == EVERYONE_ID
        assert stamp["rating_version_id"] == await _version(s, EVERYONE_ID)
        assert dragons.id in stamp["rating"]

        # Teen requested over a Mature scenario whose seed breaks a Teen policy: refused.
        await guard_rating(s, TEEN_ID)
        mature_body = g.scenario_body(*roles, rating=MATURE_ID)
        mature_body["starting_state"]["seeded_memories"] = [
            {
                "text": "A dragon sleeps under the barn.",
                "holder": "world",
                "audience": ["public"],
                "disclosure": "open",
            }
        ]
        mature, _ = await g.published_scenario(c, mature_body)
        request = g.from_scenario(
            mature, slots={"mother": g.catalog_slot(tom)}, content_rating_id=TEEN_ID
        )
        counted = await g.counts(s.db)
        status, code, details = await g.refused(c, request)
        assert (status, code) == (422, "rating_check_failed")
        failed = [e for e in details["report"]["errors"] if e["code"] == "rating_check_failed"]
        assert [e["location"]["field"] for e in failed] == [
            "starting_state.seeded_memories[0].text"
        ]
        assert all(e["source"] for e in failed)
        assert await g.counts(s.db) == counted
        # Complements: the scenario's own rating needs no check; a clean Mature one applies.
        own = await g.create_from(c, {**request, "content_rating_id": MATURE_ID})
        assert own["content_rating"]["id"] == MATURE_ID
        clean, _ = await g.published_scenario(c, g.scenario_body(*roles, rating=MATURE_ID))
        teen_story = await g.create_from(
            c,
            g.from_scenario(
                clean, slots={"mother": g.catalog_slot(tom)}, content_rating_id=TEEN_ID
            ),
        )
        assert teen_story["content_rating"]["id"] == TEEN_ID

        # The same request under the same key replays the stored result, writing nothing.
        keyed = g.from_scenario(everyone, version=version, slots={"mother": g.catalog_slot(tom)})
        first = await c.post("/stories", json=keyed, headers=g.headers("key-119-replay"))
        assert first.status_code == 201, first.text
        counted = await g.counts(s.db)
        again = await c.post("/stories", json=keyed, headers=g.headers("key-119-replay"))
        assert again.status_code == 201, again.text
        assert again.headers.get("Idempotent-Replay") == "true"
        assert again.json()["story_id"] == first.json()["story_id"]
        assert await g.counts(s.db) == counted

        # Companion versions: each story snapshots exactly the pinned character version.
        linda = await g.catalog_character_id(c, "Linda Shaw", age=52, versions=1)
        companion, v1, pinned1 = await _companion_version(s, linda)
        republished = await c.post(
            f"/catalog/characters/{linda}/publish",
            json={"draft_revision": 2},
            headers=g.headers(principal="p-scenarios"),
        )
        assert republished.status_code == 201, republished.text
        _, v2, pinned2 = await _companion_version(s, linda)
        assert (pinned1, pinned2) == (1, 2)
        for number, pinned in ((v1, pinned1), (v2, pinned2)):
            alone = await g.create_from(c, g.from_scenario(companion, version=number))
            assert await _snapshot_pairs(s, alone["story_id"]) == [(linda, pinned)]
            assert sorted(ch["role_id"] for ch in alone["characters"]) == ["npc", "player"]
            assert alone["timeline_revision"] == 1

        # Another scenario casting the character brings none of its companion's world.
        await dress_companion(
            c,
            linda,
            g.scenario_body(world={"description": f"Leeds, where {COMPANION_SECRET}."}),
        )
        other = await g.create_from(
            c, g.from_scenario(everyone, slots={"mother": g.catalog_slot(linda, 2)})
        )
        assert COMPANION_SECRET not in await _story_text(s, other["story_id"])
        others = {ch["role_id"]: ch["id"] for ch in other["characters"]}
        stub.reset()
        stub.script("generation", ["Linda smiles."])
        await played(c, other["story_id"], body(others["player"]))
        assert COMPANION_SECRET not in everything_sent(stub)


# -- reactivation re-checks (#120) -----------------------------------------------------------


class _EvaluationSpy:
    """Wraps ``change.evaluate_history``: each call's policy ids and record positions."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[set[str], list[dict[str, Any]]]] = []
        original = rating_change.evaluate_history

        async def spy(target: Any, records: Any, **kwargs: Any) -> Any:
            self.calls.append(({p.id for p in target}, [dict(r.position) for r in records]))
            return await original(target, records, **kwargs)

        monkeypatch.setattr(rating_change, "evaluate_history", spy)

    def turns(self, index: int = -1) -> list[tuple[int, int]]:
        """The ``(turn, variant)`` pairs the call at ``index`` evaluated."""
        return sorted({(p["turn"], p["variant"]) for p in self.calls[index][1] if "turn" in p})


async def _edit(s: Scratch, story_id: str, path: str, payload: Any = None) -> Any:
    return await s.client.post(f"/stories/{story_id}/{path}", json=payload, headers=WRITE)


async def _truncated(s: Scratch, story_id: str, to_turn: int) -> None:
    response = await _edit(s, story_id, "truncate", {"to_turn": to_turn})
    assert response.status_code == 200, response.text


async def _set_aside(s: Scratch, rating_id: str, *replies: str, keep: int = 1) -> tuple[str, str]:
    """A story at ``rating_id`` with one turn per reply, truncated to ``keep``; its id and the
    player's character id."""
    story_id, ids = await _played_story(s, rating_id, *replies)
    await _truncated(s, story_id, keep)
    return story_id, ids[PLAYER]


async def _tightened(s: Scratch, story_id: str, rating_id: str = TEEN_ID) -> None:
    """Tighten the story's rating and run its check, which must apply (a clean active
    history)."""
    started = await change_rating(s.client, story_id, rating_id)
    assert started.status_code == 202, started.text
    await run_jobs(s.db)
    assert (await check(s.client, story_id, started.json()["job_id"]))["status"] == "applied"


async def _held(response: Any, story_id: str) -> str:
    """The job id of an edit held back for its reactivation check (202, ``Location``)."""
    assert response.status_code == 202, response.text
    found = response.json()
    assert found["status"] == "checking"
    assert response.headers["Location"] == (
        f"/stories/{story_id}/content-rating/checks/{found['job_id']}"
    )
    return str(found["job_id"])


async def _ended(s: Scratch, story_id: str, job_id: str) -> dict[str, Any]:
    paused = await story(s.client, story_id)
    assert paused["status"] == "paused"
    assert paused["pause"]["cause"] == "reactivation_check"
    await run_jobs(s.db)
    found = await check(s.client, story_id, job_id)
    assert found["kind"] == "reactivation"
    assert (await story(s.client, story_id))["status"] == "active"
    return found


async def _memberships(s: Scratch, story_id: str) -> list[tuple[int, int, str]]:
    found = await rows(
        s.db,
        "SELECT narrative_turn, turn_variant, membership FROM operations "
        "WHERE story_id = :s AND kind = 'turn' AND status = 'committed' "
        "ORDER BY narrative_turn, turn_variant",
        s=story_id,
    )
    return [(int(r[0]), int(r[1]), str(r[2])) for r in found]


async def _stamp(s: Scratch, story_id: str, turn: int, variant: int = 1) -> Any:
    return await scalar(
        s.db,
        "SELECT checked_guardrails FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND narrative_turn = :t AND turn_variant = :v",
        s=story_id,
        t=turn,
        v=variant,
    )


async def _forget_checks(s: Scratch, story_id: str) -> None:
    """Clear the recorded guardrail set of the story's set-aside turns, as a turn that entered
    the story inactive from outside the Engine (M7) has none: the rule must cover it by
    construction, since a ``NULL`` stamp lacks every policy."""
    async with s.db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE operations SET checked_guardrails = NULL WHERE story_id = :s "
                "AND kind = 'turn' AND membership <> 'active'"
            ),
            {"s": story_id},
        )


async def _count(s: Scratch, table: str) -> int:
    return int(await scalar(s.db, f'SELECT count(*) FROM "{table}"'))  # noqa: S608


async def _no_guard(*_: Any, **__: Any) -> None:
    """A test-only stand-in for ``reactivation.guard`` (no production switch exists)."""
    return None


@pytest.mark.part("M3", "FR-RATE-5")
async def test_i_22__companion_rating(pg_url: str) -> None:
    """A companion rated differently from its character warns and publishes (docs/04 I-22
    [M3], #341): an Explicit character's companion set to Everyone publishes (201) with one
    ``rating_looser`` warning at its character's role, naming the pinned character version.

    Complement asserted: the companion at its character's own rating publishes with no
    ``rating_looser`` warning."""
    async with rating_db(pg_url) as s:
        c = s.client
        # Everyone guards a policy Explicit lacks, so an Explicit character is looser.
        await guard_rating(s, EVERYONE_ID)
        character = await g.catalog_character_id(c, "Vera Lune", age=38, rating=EXPLICIT_ID)
        companion = await read_companion(c, character)
        assert companion["metadata"]["content_rating_id"] == EXPLICIT_ID
        # A companion publishes on its own once it has an opening (a world and its narration).
        roles = (g.role("mother"), g.role("player", kind="player"))
        await dress_companion(
            c, character, g.scenario_body(*roles, rating=EXPLICIT_ID), publish=False
        )

        async def publish_at(rating_id: str) -> dict[str, Any]:
            draft = await read_companion(c, character)
            saved = await scenarios.save(
                c, draft["id"], draft["draft_revision"], metadata={"content_rating_id": rating_id}
            )
            assert saved.status_code == 200, saved.text
            done = await scenarios.publish(c, draft["id"], saved.json()["draft_revision"])
            assert done.status_code == 201, done.text
            found: dict[str, Any] = done.json()
            return found

        stricter = await publish_at(EVERYONE_ID)
        assert [
            (w["code"], w["location"]["field"], w["source"]) for w in stricter["report"]["warnings"]
        ] == [("rating_looser", "cast.roles[0].character", f"{character}@1")]

        same = await publish_at(EXPLICIT_ID)
        assert [w for w in same["report"]["warnings"] if w["code"] == "rating_looser"] == []


@pytest.mark.part("M3", "FR-RATE-7")
async def test_i_22__reactivation(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a tightening Explicit -> Teen that applied over a clean active history,
    untruncating a set-aside explicit turn is refused with its violation, swapping back to a
    chain holding one is refused, and promoting that dormant chain into a fork is refused (no
    fork is created); each one paused the story and answered 202 with the check. A turn that
    passed in a refused attempt is stamped and not checked again in the next. After a new
    global policy is added, untruncating a turn played before it checks it against that
    policy first, and the job applies the undo. A set-aside turn with no recorded guardrail
    set is checked too.

    Complements asserted: with the guard replaced by a no-op, the same untruncate, swap and
    fork succeed; a turn already checked against the current set is untruncated synchronously
    with no check, no job and no guardrail call; refusals change no membership and create no
    story."""
    async with rating_db(pg_url) as s:
        dragons = await guard_rating(s)
        spy = _EvaluationSpy(monkeypatch)
        clean, explicit, wind = "A cat purrs by the fire.", "A dragon lands on the roof.", "Wind."

        # The same set-aside explicit turn, without the guard first: the edits would succeed.
        unguarded, _ = await _set_aside(s, EXPLICIT_ID, clean, explicit)
        await _tightened(s, unguarded)
        guard = reactivation.guard
        monkeypatch.setattr(reactivation, "guard", _no_guard)
        undone = await _edit(s, unguarded, "untruncate")
        assert undone.status_code == 200, undone.text
        assert (await _memberships(s, unguarded))[-1] == (2, 1, "active")
        bystander, player = await _set_aside(s, EXPLICIT_ID, clean, explicit)
        s.lib.models.stub.script("generation", [wind])
        await played(s.client, bystander, body(player, "Again?"))
        await _tightened(s, bystander)
        chain = {"to_variant_chain": {"turn": 2, "variant": 1}}
        stories_before = await _count(s, "stories")
        forked = await _edit(s, bystander, "fork", {"variant_chain": {"turn": 2, "variant": 1}})
        assert forked.status_code == 201, forked.text
        assert await _count(s, "stories") == stories_before + 1
        swapped = await _edit(s, bystander, "swap", chain)
        assert swapped.status_code == 200, swapped.text
        monkeypatch.setattr(reactivation, "guard", guard)

        # Untruncate a set-aside explicit turn after the tightening: refused.
        story_a, _ = await _set_aside(s, EXPLICIT_ID, clean, explicit)
        await _tightened(s, story_a)
        before = await _memberships(s, story_a)
        job = await _held(await _edit(s, story_a, "untruncate"), story_a)
        refused = await _ended(s, story_a, job)
        assert refused["status"] == "refused"
        assert refused["violations"] == [
            {
                "position": {"turn": 2, "variant": 1, "beat": 2},
                "policy_id": dragons.id,
                "policy_name": dragons.name,
                "code": None,
            }
        ]
        assert "dragon lands" not in str(refused).lower()
        assert await _memberships(s, story_a) == before
        assert spy.turns() == [(2, 1)]

        # Swap back to a chain holding it, and promote that chain into a fork: refused.
        story_b, player = await _set_aside(s, EXPLICIT_ID, clean, explicit)
        s.lib.models.stub.script("generation", [wind])
        await played(s.client, story_b, body(player, "Again?"))
        await _tightened(s, story_b)
        before = await _memberships(s, story_b)
        assert (2, 1, "dormant") in before
        listed = await s.client.get(f"/stories/{story_b}/variants", headers=READ)
        flags = {(c["turn"], c["variant"]): c["requires_check"] for c in listed.json()["items"]}
        assert flags[(2, 1)] is True
        assert flags[(2, 2)] is False
        job = await _held(await _edit(s, story_b, "swap", chain), story_b)
        assert (await _ended(s, story_b, job))["status"] == "refused"
        assert await _memberships(s, story_b) == before
        stories_before = await _count(s, "stories")
        fork_body = {"variant_chain": {"turn": 2, "variant": 1}}
        job = await _held(await _edit(s, story_b, "fork", fork_body), story_b)
        promoted = await _ended(s, story_b, job)
        assert promoted["status"] == "refused"
        assert promoted["outcome"] is None
        assert [v["position"]["turn"] for v in promoted["violations"]] == [2]
        assert await _count(s, "stories") == stories_before
        assert await _memberships(s, story_b) == before

        # A turn that passed in a refused attempt is not checked again.
        story_c, _ = await _set_aside(s, EXPLICIT_ID, clean, "A cat sleeps.", explicit)
        await _tightened(s, story_c)
        job = await _held(await _edit(s, story_c, "untruncate"), story_c)
        assert (await _ended(s, story_c, job))["status"] == "refused"
        assert spy.turns() == [(2, 1), (3, 1)]
        assert dragons.id in (await _stamp(s, story_c, 2))["rating"]
        assert dragons.id not in (await _stamp(s, story_c, 3))["rating"]
        calls = len(spy.calls)
        job = await _held(await _edit(s, story_c, "untruncate"), story_c)
        assert (await _ended(s, story_c, job))["status"] == "refused"
        assert len(spy.calls) == calls + 1
        assert spy.turns() == [(3, 1)]

        # Already checked against the current set: synchronous, no check at all.
        story_f, _ = await _set_aside(s, TEEN_ID, clean, "A cat naps.")
        calls, jobs, guardrail = len(spy.calls), await _count(s, "jobs"), _guardrail_calls(s)
        undone = await _edit(s, story_f, "untruncate")
        assert undone.status_code == 200, undone.text
        assert undone.json()["restored_operations"] >= 1
        assert (await _memberships(s, story_f))[-1] == (2, 1, "active")
        assert (len(spy.calls), await _count(s, "jobs")) == (calls, jobs)
        assert _guardrail_calls(s) == guardrail

        # A new global policy: the set-aside turn is checked against it first, then undone.
        story_d, _ = await _set_aside(s, TEEN_ID, clean, "A cat naps.")
        goblins = await s.lib.policies_create(
            OPERATOR,
            PolicyFields.model_validate(
                {
                    "scope": "global",
                    "name": "no-goblins",
                    "rule": {"type": "wordlist", "words": ["goblin"]},
                    "message": "No goblins.",
                }
            ),
        )
        job = await _held(await _edit(s, story_d, "untruncate"), story_d)
        undone_d = await _ended(s, story_d, job)
        assert undone_d["status"] == "reactivated", undone_d
        assert goblins.policy.id in spy.calls[-1][0]
        assert dragons.id in spy.calls[-1][0]
        assert undone_d["outcome"]["operation"] == "untruncate"
        assert undone_d["outcome"]["result"]["restored_operations"] >= 1
        assert (await _memberships(s, story_d))[-1] == (2, 1, "active")
        assert goblins.policy.id in (await _stamp(s, story_d, 2))["global"]

        # A set-aside turn with no recorded guardrail set is checked too.
        story_e, _ = await _set_aside(s, TEEN_ID, clean, "A cat naps.")
        await _forget_checks(s, story_e)
        assert await _stamp(s, story_e, 2) is None
        job = await _held(await _edit(s, story_e, "untruncate"), story_e)
        assert (await _ended(s, story_e, job))["status"] == "reactivated"
        assert spy.turns() == [(2, 1)]
        assert (await _memberships(s, story_e))[-1] == (2, 1, "active")


async def test_a_reactivation_check_that_passes_applies_a_swap_and_a_fork(pg_url: str) -> None:
    """A dormant chain whose turn lacks the current policy but passes it: the swap is held for
    its check and then applied by the job (the chain is active, the check's ``outcome`` is the
    swap's answer), and a fork promoting a passing dormant chain is created by the job (its
    answer is the outcome; the fork holds the chain as its active history; the source keeps
    its memberships)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        stub = s.lib.models.stub
        swap_story, player = await _set_aside(s, EXPLICIT_ID, "A cat purrs.", "A cat sleeps.")
        stub.script("generation", ["The wind turns."])
        await played(s.client, swap_story, body(player, "Again?"))
        await _tightened(s, swap_story)
        chain = {"to_variant_chain": {"turn": 2, "variant": 1}}
        job = await _held(await _edit(s, swap_story, "swap", chain), swap_story)
        swapped = await _ended(s, swap_story, job)
        assert swapped["status"] == "reactivated", swapped
        assert swapped["outcome"]["operation"] == "swap"
        assert swapped["outcome"]["result"]["active_chain"] == {"turn": 2, "variant": 1}
        assert (2, 1, "active") in await _memberships(s, swap_story)
        assert (2, 2, "dormant") in await _memberships(s, swap_story)
        # Swapping back: the chain made dormant was checked against the current set at commit.
        back = await _edit(s, swap_story, "swap", {"to_variant_chain": {"turn": 2, "variant": 2}})
        assert back.status_code == 200, back.text

        fork_story, player = await _set_aside(s, EXPLICIT_ID, "A cat purrs.", "A cat sleeps.")
        stub.script("generation", ["The wind turns."])
        await played(s.client, fork_story, body(player, "Again?"))
        await _tightened(s, fork_story)
        source = await _memberships(s, fork_story)
        stories_before = await _count(s, "stories")
        fork_body = {"variant_chain": {"turn": 2, "variant": 1}, "name": "Checked fork"}
        job = await _held(await _edit(s, fork_story, "fork", fork_body), fork_story)
        forked = await _ended(s, fork_story, job)
        assert forked["status"] == "reactivated", forked
        assert forked["outcome"]["operation"] == "fork"
        result = forked["outcome"]["result"]
        assert result["name"] == "Checked fork"
        assert result["forked_from"]["variant_chain"] == {"turn": 2, "variant": 1}
        assert await _count(s, "stories") == stories_before + 1
        assert await _memberships(s, result["story_id"]) == [(1, 1, "active"), (2, 1, "active")]
        assert (await story(s.client, result["story_id"]))["status"] == "active"
        assert await _memberships(s, fork_story) == source


async def test_a_held_edit_pauses_the_story_and_replays_its_check(pg_url: str) -> None:
    """While the check runs the story refuses truth-changing calls with ``story_paused``
    (``cause: reactivation_check``); the same request under the same ``Idempotency-Key``
    replays the 202 and its job; an ``X-Epistrel-Expected-Revision`` that does not match is
    409 before any check starts; a fork along the active chain is never checked."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, player = await _set_aside(s, EXPLICIT_ID, "A cat purrs.", "A dragon lands.")
        await _tightened(s, story_id)
        current = (await story(s.client, story_id))["timeline_revision"]
        stale = await s.client.post(
            f"/stories/{story_id}/untruncate",
            headers={**WRITE, "X-Epistrel-Expected-Revision": str(current - 1)},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "revision_conflict"
        jobs = await _count(s, "jobs")
        assert (await story(s.client, story_id))["status"] == "active"

        keyed = {**WRITE, "Idempotency-Key": "key-120-untruncate"}
        first = await s.client.post(f"/stories/{story_id}/untruncate", headers=keyed)
        job = await _held(first, story_id)
        assert await _count(s, "jobs") == jobs + 1
        again = await s.client.post(f"/stories/{story_id}/untruncate", headers=keyed)
        assert again.status_code == 202, again.text
        assert again.headers.get("Idempotent-Replay") == "true"
        assert again.json()["job_id"] == job
        assert await _count(s, "jobs") == jobs + 1

        refused = await respond(s.client, story_id, body(player, "Hello?"))
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "story_paused"
        assert error["details"]["cause"] == "reactivation_check"
        assert error["details"]["job_id"] == job
        second = await _edit(s, story_id, "untruncate")
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "story_paused"
        assert (await _ended(s, story_id, job))["status"] == "refused"

        active_fork = await _edit(s, story_id, "fork", {"variant_chain": {"turn": 1, "variant": 1}})
        assert active_fork.status_code == 201, active_fork.text


async def test_an_edit_that_became_impossible_ends_the_check_failed(pg_url: str) -> None:
    """When the held-back edit can no longer be made when the job plans it again (here the
    set-aside turns were made active behind its back), the check ends ``failed`` with the
    error's class and the story resumes."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, _ = await _set_aside(s, EXPLICIT_ID, "A cat purrs.", "A cat sleeps.")
        await _tightened(s, story_id)
        job = await _held(await _edit(s, story_id, "untruncate"), story_id)
        async with s.db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE operations SET membership = 'active' WHERE story_id = :s "
                    "AND membership = 'discarded'"
                ),
                {"s": story_id},
            )
        failed = await _ended(s, story_id, job)
        assert failed["status"] == "failed"
        assert failed["error"]["class"] == "NothingToUndo"
        assert failed["outcome"] is None


async def test_a_refused_fork_of_an_archived_story_leaves_it_archived(pg_url: str) -> None:
    """An archived source is not paused by a fork's reactivation check, so a refused check
    leaves it as it was: still ``archived``, with no ``story_resumed`` recorded and no fork
    created (#120 verification round 1)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)
        story_id, player = await _set_aside(s, EXPLICIT_ID, "A cat purrs.", "A dragon lands.")
        s.lib.models.stub.script("generation", ["The wind turns."])
        await played(s.client, story_id, body(player, "Again?"))
        await _tightened(s, story_id)
        archived = await s.client.post(f"/stories/{story_id}/archive", headers=WRITE)
        assert archived.status_code == 200, archived.text
        before = await _memberships(s, story_id)
        stories_before = await _count(s, "stories")
        events = "SELECT type FROM events WHERE story_id = :s ORDER BY seq"
        events_before = [r[0] for r in await rows(s.db, events, s=story_id)]
        fork_body = {"variant_chain": {"turn": 2, "variant": 1}}
        job = await _held(await _edit(s, story_id, "fork", fork_body), story_id)
        assert (await story(s.client, story_id))["status"] == "archived"
        await run_jobs(s.db)
        found = await check(s.client, story_id, job)
        assert found["status"] == "refused", found
        assert (await story(s.client, story_id))["status"] == "archived"
        assert await scalar(s.db, "SELECT paused_by FROM stories WHERE id = :s", s=story_id) is None
        added = [r[0] for r in await rows(s.db, events, s=story_id)][len(events_before) :]
        assert "story_resumed" not in added
        assert "story_paused" not in added
        assert await _count(s, "stories") == stories_before
        assert await _memberships(s, story_id) == before


async def test_requires_check_covers_the_whole_path_a_swap_activates(pg_url: str) -> None:
    """``requires_check`` reads every turn a swap to the chain would make active: its parent
    segments that are not active too, and the one way it continues. Turns 1-3 played, turn 3
    played again, then truncated to 1: turn 2 is discarded, its two continuations 3:1
    (dormant) and 3:2 (discarded). An unchecked turn 2 flags 3:1 (whose own turn is checked),
    and the swap to it is held for a check; an unchecked 3:2 flags 2:1 (whose own turn is
    checked) but not 3:1 (#120 verification round 1)."""
    async with rating_db(pg_url) as s:
        await guard_rating(s)

        async def branched() -> str:
            story_id, player = await _set_aside(
                s, TEEN_ID, "A cat purrs.", "A cat naps.", "A cat sleeps.", keep=2
            )
            s.lib.models.stub.script("generation", ["The wind turns."])
            await played(s.client, story_id, body(player, "Again?"))
            await _truncated(s, story_id, 1)
            assert await _memberships(s, story_id) == [
                (1, 1, "active"),
                (2, 1, "discarded"),
                (3, 1, "dormant"),
                (3, 2, "discarded"),
            ]
            return story_id

        async def unchecked(story_id: str, turn: int, variant: int) -> None:
            async with s.db.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE operations SET checked_guardrails = NULL WHERE story_id = :s "
                        "AND kind = 'turn' AND narrative_turn = :t AND turn_variant = :v"
                    ),
                    {"s": story_id, "t": turn, "v": variant},
                )

        async def flags(story_id: str) -> dict[tuple[int, int], bool]:
            listed = await s.client.get(f"/stories/{story_id}/variants", headers=READ)
            assert listed.status_code == 200, listed.text
            return {(c["turn"], c["variant"]): c["requires_check"] for c in listed.json()["items"]}

        parent = await branched()
        assert set((await flags(parent)).values()) == {False}
        await unchecked(parent, 2, 1)
        assert (await flags(parent))[(3, 1)] is True
        chain = {"to_variant_chain": {"turn": 3, "variant": 1}}
        job = await _held(await _edit(s, parent, "swap", chain), parent)
        assert (await _ended(s, parent, job))["status"] == "reactivated"

        tip = await branched()
        await unchecked(tip, 3, 2)
        found = await flags(tip)
        assert found[(2, 1)] is True
        assert found[(3, 1)] is False
