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

from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID, MATURE_ID, TEEN_ID
from epistrel.orchestrator.jobs.handlers import rating_check as rating_check_job
from tests import genesis_support as g
from tests.catalog_support import dress_companion
from tests.rating_change_support import (
    AUTHOR,
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


@pytest.mark.part("M3", "FR-SCN-6; FR-RATE-3,7; FR-CAT-10")
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
