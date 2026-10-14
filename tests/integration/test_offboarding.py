"""Principal lookup and offboarding, in detail (#117). The milestone parts are I-21's slices,
``test_i_21__offboarding`` and ``test_i_21__offboarding_resumes``, in
``tests/integration/test_i_21.py``.

Each test runs on its own scratch database (``tests/offboarding_support.offboarding_db``): it
counts rows, and it runs the job worker, which in the shared database would claim other tests'
jobs too.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from epistrel.core import clock, offboarding
from epistrel.core.ids import new_ulid
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import offboarding as offboarding_job
from epistrel.orchestrator.jobs.handlers.retention import sweep_retention
from epistrel.orchestrator.jobs.types import JobContext
from epistrel.orchestrator.jobs.worker import Worker, WorkerConfig
from tests.integration.ownership_fixtures import headers, ok, persona_body
from tests.offboarding_support import (
    OFFBOARDING,
    READ,
    actions,
    counts,
    lookup,
    offboard,
    offboarding_db,
    report,
    rows,
    run_jobs,
    scalar,
    unique,
    world,
)
from tests.story_support import story_body

pytestmark = pytest.mark.integration


def _ids(page: dict[str, Any]) -> list[str]:
    return [item["id"] for item in page["items"]]


def _warnings(body: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
    return [(w["kind"], w["entity_id"], w["principals"]) for w in body["warnings"]]


async def test_lookup_groups_owned_and_related(pg_url: str) -> None:
    """The lookup lists what the principal owns by type (a character with its companion, which
    is not listed as a scenario) and what names it in a relation, with counts; an unknown
    principal answers empty pages, and ``system`` its presets."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        found = await lookup(s.client, w.a)
        owned, related = found["owned"], found["related"]
        assert found["principal"] == w.a
        assert sorted(_ids(owned["stories"])) == sorted([w.shared_story, w.own_story])
        assert owned["stories"]["count"] == 2
        assert _ids(owned["characters"]) == [w.character]
        assert owned["characters"]["items"][0]["companion"] == {"id": w.companion}
        assert owned["characters"]["items"][0]["name"] == "Mara Venn"
        assert owned["characters"]["items"][0]["status"] == "draft"
        assert owned["characters"]["items"][0]["listing_status"] == "private"
        assert _ids(owned["scenarios"]) == [w.scenario]
        assert owned["scenarios"]["count"] == 1, "the companion is not counted as a scenario"
        assert _ids(owned["personas"]) == [w.persona]
        assert sorted(_ids(owned["media"])) == sorted([w.kept_image, w.shared_image, w.loose_image])
        assert {item["status"] for item in owned["media"]["items"]} == {"live"}
        assert _ids(owned["engine_profiles"]) == [w.profile]
        assert owned["engine_profiles"]["items"][0]["status"] == "active"
        assert related["characters"] == {
            "count": 1,
            "items": [{"id": w.others_character, "relations": ["editor"]}],
            "next_cursor": None,
        }
        assert related["stories"]["count"] == 0
        player = await lookup(s.client, w.player)
        assert player["related"]["stories"]["items"] == [
            {"id": w.shared_story, "relations": ["player"]}
        ]
        assert all(page["count"] == 0 for page in player["owned"].values())
        nobody = await lookup(s.client, unique("p-nobody"))
        for section in ("owned", "related"):
            assert all(
                page == {"count": 0, "items": [], "next_cursor": None}
                for page in nobody[section].values()
            )
        system = await lookup(s.client, "system")
        assert system["owned"]["engine_profiles"]["count"] >= 1


async def test_lookup_pages_each_type_with_its_own_cursor(pg_url: str) -> None:
    """``limit`` applies to every type, newest first; each type continues with its own cursor,
    and one type's cursor is refused for another (422 ``cursor_invalid``)."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        first = await lookup(s.client, w.a, limit=1)
        assert first["owned"]["stories"]["count"] == 2
        assert _ids(first["owned"]["stories"]) == [w.own_story], "newest first"
        cursor = first["owned"]["stories"]["next_cursor"]
        assert cursor is not None
        assert first["owned"]["personas"]["next_cursor"] is None
        second = await lookup(s.client, w.a, limit=1, **{"cursor[stories]": cursor})
        assert _ids(second["owned"]["stories"]) == [w.shared_story]
        assert second["owned"]["stories"]["next_cursor"] is None
        assert _ids(second["owned"]["media"]) == _ids(first["owned"]["media"]), (
            "the other types start over"
        )
        refused = await s.client.get(
            f"/principals/{w.a}", params={"cursor[media]": cursor}, headers=READ
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "cursor_invalid"
        assert error["details"]["field"] == "query.cursor[media]"
        stranger = await s.client.get(
            f"/principals/{w.player}", params={"cursor[stories]": cursor}, headers=READ
        )
        assert stranger.json()["error"]["code"] == "cursor_invalid"
        media = first["owned"]["media"]["next_cursor"]
        rest = await lookup(s.client, w.a, limit=1, **{"cursor[media]": media})
        assert len(_ids(rest["owned"]["media"])) == 1
        assert _ids(rest["owned"]["media"]) != _ids(first["owned"]["media"])


async def test_dry_run_warns_and_changes_nothing(pg_url: str) -> None:
    """The dry run (and a request without ``ignore_warnings`` whose plan warns) answers the
    counts and the three warning kinds and writes nothing; ``dry_run`` wins over
    ``ignore_warnings``."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        body = {"principal": w.a, "actions": actions(), "transfer_to": w.curator}
        before = await counts(s.db)
        dry, response = await offboard(s.client, {**body, "dry_run": True}, status=200)
        assert dry["applied"] is False
        assert dry["counts"] == {
            "stories": {"delete": 2, "transfer": 0, "keep": 0},
            "characters": {"delete": 0, "transfer": 1, "keep": 0},
            "scenarios": {"delete": 0, "transfer": 1, "keep": 0},
            "personas": {"delete": 1, "transfer": 0, "keep": 0},
            "media": {"delete": 2, "transfer": 1, "keep": 0},
            "engine_profiles": {"delete": 1, "transfer": 0, "keep": 0},
            "relations": {"remove": 1, "keep": 0},
        }
        assert _warnings(dry) == [
            ("shared_story", w.shared_story, [w.player]),
            ("profile_in_use", w.profile, [w.other]),
            ("media_referenced", w.shared_image, [w.other]),
        ]
        assert "Idempotency-Key" in response.headers
        held, _ = await offboard(s.client, body, status=200)
        assert held["applied"] is False
        assert _warnings(held) == _warnings(dry)
        both, _ = await offboard(
            s.client, {**body, "dry_run": True, "ignore_warnings": True}, status=200
        )
        assert both["applied"] is False
        assert await counts(s.db) == before
        # Complement: the same request with ignore_warnings is enqueued.
        queued, _ = await offboard(s.client, {**body, "ignoreWarnings": True}, status=202)
        assert queued["applied"] is True
        assert queued["counts"] == dry["counts"]
        after = await counts(s.db)
        assert after["offboarding_jobs"] == before["offboarding_jobs"] + 1
        assert after["jobs"] == before["jobs"] + 1


async def test_offboarding_carries_out_every_action(pg_url: str) -> None:
    """The test plan's execution: stories deleted (the shared one for its player too), the
    character and its companion transferred with the image the character shows, the scenario
    transferred, the persona deleted, the image another owner's character shows retired but
    still served, the loose image retired, the profile retired, and the relation on the other
    owner's character removed. Every row the job writes names ``system`` and the job; the
    report lists each entity; a retry under the same key answers the same job."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        key = new_ulid()
        body = {
            "principal": w.a,
            "actions": actions(),
            "transfer_to": w.curator,
            "ignore_warnings": True,
        }
        queued, response = await offboard(s.client, body, status=202, key=key)
        job_id = queued["job_id"]
        assert response.headers["Location"] == f"{OFFBOARDING}/{job_id}"
        assert response.headers["Idempotency-Key"] == key
        waiting = await report(s.client, job_id)
        assert waiting["status"] == "queued"
        assert waiting["progress"] == {"done": 0, "total": 10}
        assert waiting["report"] is None
        assert await run_jobs(s.db) >= 1
        done = await report(s.client, job_id)
        assert done["status"] == "done", done
        assert done["progress"] == {"done": 10, "total": 10}
        assert done["current"] is None
        found = done["report"]
        assert sorted(found["deleted"]["stories"]) == sorted([w.shared_story, w.own_story])
        assert found["deleted"]["personas"] == [w.persona]
        assert sorted(found["deleted"]["media"]) == sorted([w.shared_image, w.loose_image])
        assert found["deleted"]["engine_profiles"] == [w.profile]
        assert found["transferred"]["characters"] == [{"id": w.character, "to": w.curator}]
        assert found["transferred"]["scenarios"] == [
            {"id": w.companion, "to": w.curator, "companion_of": w.character},
            {"id": w.scenario, "to": w.curator},
        ]
        assert found["transferred"]["media"] == [
            {"id": w.kept_image, "to": w.curator, "reason": "referenced"}
        ]
        assert found["removed"]["relations"] == [
            {"entity_type": "character", "id": w.others_character, "relations": ["editor"]}
        ]
        assert all(found["skipped"][kind] == [] for kind in found["skipped"])
        remaining = found["remaining"]
        assert all(ids == [] for ids in remaining["owned"].values()), remaining
        assert all(ids == [] for ids in remaining["related"].values()), remaining
        # The state the report describes.
        shared = await scalar(s.db, "SELECT count(*) FROM stories WHERE id = :s", s=w.shared_story)
        assert shared == 0
        player = await lookup(s.client, w.player)
        assert player["related"]["stories"]["count"] == 0, "gone for the player too"
        owners = {
            row.id: row.owner_principal
            for row in await rows(
                s.db,
                "SELECT id, owner_principal FROM catalog.character_catalog UNION ALL "
                "SELECT id, owner_principal FROM catalog.scenario_catalog UNION ALL "
                "SELECT id, owner_principal FROM media_assets",
            )
        }
        assert owners[w.character] == owners[w.companion] == owners[w.scenario] == w.curator
        assert owners[w.kept_image] == w.curator
        states = {
            row.id: row.state for row in await rows(s.db, "SELECT id, state FROM media_assets")
        }
        assert states[w.kept_image] == "live"
        assert states[w.shared_image] == states[w.loose_image] == "retired"
        served = await s.client.get(f"/media/{w.shared_image}/content", headers=READ)
        assert served.status_code == 200, "retired, not purged"
        assert await scalar(
            s.db, "SELECT deleted_at IS NOT NULL FROM engine_profiles WHERE id = :p", p=w.profile
        )
        assert await scalar(s.db, "SELECT count(*) FROM personas WHERE id = :p", p=w.persona) == 0
        # The job's rows: system, and the job's id.
        written = await rows(
            s.db,
            "SELECT entity_id, kind, acting_principal, job_id FROM ownership_events "
            "WHERE job_id = :j ORDER BY at, id",
            j=job_id,
        )
        assert {row.acting_principal for row in written} == {"system"}
        assert {(row.entity_id, row.kind) for row in written} >= {
            (w.shared_story, "deleted"),
            (w.own_story, "deleted"),
            (w.character, "transfer"),
            (w.companion, "transfer"),
            (w.scenario, "transfer"),
            (w.kept_image, "transfer"),
            (w.persona, "deleted"),
            (w.others_character, "relation_removed"),
        }
        # Not erasure: a's principal still names its turns in the other owner's story.
        assert (
            await scalar(
                s.db,
                "SELECT count(*) FROM events WHERE story_id = :s AND principal = :p",
                s=w.others_story,
                p=w.a,
            )
            > 0
        )
        # A retry under the key answers the same job and changes nothing.
        before = await counts(s.db)
        again, replay = await offboard(s.client, body, status=202, key=key)
        assert again["job_id"] == job_id
        assert replay.headers["Idempotent-Replay"] == "true"
        assert await counts(s.db) == before
        # Offboarding again finds nothing left to act on.
        later, _ = await offboard(s.client, body, status=202)
        assert later["counts"]["stories"] == {"delete": 0, "transfer": 0, "keep": 0}
        assert later["counts"]["media"] == {"delete": 2, "transfer": 0, "keep": 0}, (
            "the retired media are still the principal's until the sweep purges them"
        )


async def test_request_rules(pg_url: str) -> None:
    """``system`` cannot be offboarded; a transfer needs a target that is neither the principal
    nor ``system``; a second request while one is queued is 409; unknown actions and types are
    refused; a type not named is kept. Nothing is written by a refusal."""
    async with offboarding_db(pg_url) as s:
        a = unique("p-a")
        before = await counts(s.db)

        async def refused(body: dict[str, Any], status: int, code: str, field: str) -> None:
            response = await s.client.post(OFFBOARDING, json=body, headers=headers("p-operator"))
            assert response.status_code == status, response.text
            error = response.json()["error"]
            assert error["code"] == code, error
            assert error["details"]["field"] == field, error

        await refused({"principal": "system"}, 422, "principal_reserved", "body.principal")
        await refused(
            {"principal": a, "actions": {"stories": "transfer"}},
            422,
            "transfer_target_required",
            "body.transfer_to",
        )
        await refused(
            {"principal": a, "actions": {"media": "transfer"}, "transfer_to": a},
            422,
            "transfer_target_invalid",
            "body.transfer_to",
        )
        await refused(
            {"principal": a, "transfer_to": "system"},
            422,
            "transfer_target_invalid",
            "body.transfer_to",
        )
        await refused(
            {"principal": a, "actions": {"stories": "erase"}},
            422,
            "validation_error",
            "body.actions.stories",
        )
        await refused(
            {"principal": a, "actions": {"relations": "delete"}},
            422,
            "validation_error",
            "body.actions.relations",
        )
        await refused(
            {"principal": a, "actions": {"comments": "delete"}},
            422,
            "validation_error",
            "body.actions.comments",
        )
        await refused({"principal": " \t"}, 422, "validation_error", "body.principal")
        assert await counts(s.db) == before
        queued, _ = await offboard(s.client, {"principal": f" {a} "}, status=202)
        assert queued["counts"]["stories"] == {"delete": 0, "transfer": 0, "keep": 0}
        busy = await s.client.post(
            OFFBOARDING, json={"principal": a}, headers=headers("p-operator")
        )
        assert busy.status_code == 409, busy.text
        assert busy.json()["error"]["code"] == "offboarding_in_progress"
        assert busy.json()["error"]["details"]["job_id"] == queued["job_id"]
        stored = await report(s.client, queued["job_id"])
        assert stored["principal"] == a
        assert stored["request"]["actions"] == {
            "stories": "keep",
            "characters": "keep",
            "scenarios": "keep",
            "personas": "keep",
            "media": "keep",
            "engine_profiles": "keep",
            "relations": "keep",
        }
        await run_jobs(s.db)
        assert (await report(s.client, queued["job_id"]))["status"] == "done"
        again, _ = await offboard(s.client, {"principal": a}, status=202)
        assert again["job_id"] != queued["job_id"], "a finished one does not block a new one"
        missing = await s.client.get(f"{OFFBOARDING}/{new_ulid()}", headers=READ)
        assert missing.status_code == 404


async def test_keep_and_entities_created_later(pg_url: str) -> None:
    """``keep`` leaves an entity as it is and lists it as kept, with a character's companion;
    an entity created for the principal after the request is not in the job; one transferred
    away meanwhile is skipped as ``not_owned``, one deleted meanwhile as ``gone``."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        queued, _ = await offboard(
            s.client,
            {
                "principal": w.a,
                "actions": {"stories": "delete", "personas": "delete"},
                "ignore_warnings": True,
            },
            status=202,
        )
        later = await ok(
            await s.client.post("/stories", json=story_body("New", "Later"), headers=headers(w.a)),
            201,
        )
        await ok(
            await s.client.put(
                f"/stories/{w.own_story}/owner",
                json={"principal": w.other},
                headers=headers(w.a),
            )
        )
        deleted = await s.client.delete(f"/personas/{w.persona}", headers=headers(w.a))
        assert deleted.status_code == 204
        await run_jobs(s.db)
        found = (await report(s.client, queued["job_id"]))["report"]
        assert found["deleted"]["stories"] == [w.shared_story]
        assert found["skipped"]["stories"] == [{"id": w.own_story, "reason": "not_owned"}]
        assert found["skipped"]["personas"] == [{"id": w.persona, "reason": "gone"}]
        assert found["kept"]["characters"] == [w.character]
        assert found["kept"]["scenarios"] == [w.companion, w.scenario]
        assert later["story_id"] not in found["deleted"]["stories"]
        assert found["remaining"]["owned"]["stories"] == [later["story_id"]]
        assert await scalar(s.db, "SELECT count(*) FROM stories WHERE id = :s", s=later["story_id"])


async def test_type_rules_that_skip(pg_url: str) -> None:
    """A persona whose name the new owner already uses is skipped (``name_taken``), and the
    deployment's default profile is warned of and never deleted (``default_profile``)."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        name = (await ok(await s.client.get(f"/personas/{w.persona}", headers=READ)))["name"]
        await ok(
            await s.client.post(
                "/personas", json=persona_body(name=name), headers=headers(w.curator)
            ),
            201,
        )
        await ok(
            await s.client.put(
                "/admin/settings",
                json={"default_engine_profile_id": w.profile},
                headers=headers("p-operator"),
            )
        )
        body = {
            "principal": w.a,
            "actions": {"personas": "transfer", "engine_profiles": "delete"},
            "transfer_to": w.curator,
        }
        dry, _ = await offboard(s.client, {**body, "dry_run": True}, status=200)
        assert ("default_profile", w.profile, []) in _warnings(dry)
        queued, _ = await offboard(s.client, {**body, "ignore_warnings": True}, status=202)
        await run_jobs(s.db)
        found = (await report(s.client, queued["job_id"]))["report"]
        assert found["skipped"]["personas"] == [{"id": w.persona, "reason": "name_taken"}]
        assert found["skipped"]["engine_profiles"] == [
            {"id": w.profile, "reason": "default_profile"}
        ]
        assert await scalar(
            s.db, "SELECT deleted_at IS NULL FROM engine_profiles WHERE id = :p", p=w.profile
        )
        assert found["remaining"]["owned"]["personas"] == [w.persona]


async def test_a_busy_story_is_retried_then_skipped(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A story that answers 423 is deferred to the end of the pass and retried; one that frees
    up is deleted, one still busy after three retries is skipped as ``busy``."""
    monkeypatch.setattr(offboarding_job, "BUSY_DELAY_S", 0.0)
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        lease = clock.now() + timedelta(hours=1)
        async with s.db.begin() as conn:
            for story_id in (w.shared_story, w.own_story):
                await conn.exec_driver_sql(
                    "INSERT INTO jobs (id, kind, story_id, due_at, status, lease_until, "
                    "created_at, updated_at) VALUES (%(id)s, 'rebuild_projections', %(s)s, now(), "
                    "'running', %(lease)s, now(), now())",
                    {"id": new_ulid(), "s": story_id, "lease": lease},
                )
        queued, _ = await offboard(
            s.client,
            {"principal": w.a, "actions": {"stories": "delete"}, "ignore_warnings": True},
            status=202,
        )
        worker_runs = await run_jobs(s.db, runs=1)
        assert worker_runs == 1
        midway = await report(s.client, queued["job_id"])
        assert midway["status"] == "running"
        assert midway["progress"] == {"done": 8, "total": 10}, "both stories wait, all else kept"
        async with s.db.begin() as conn:
            await conn.exec_driver_sql(
                "DELETE FROM jobs WHERE story_id = %(s)s", {"s": w.own_story}
            )
        await run_jobs(s.db)
        done = await report(s.client, queued["job_id"])
        assert done["status"] == "done"
        assert done["report"]["deleted"]["stories"] == [w.own_story]
        assert done["report"]["skipped"]["stories"] == [{"id": w.shared_story, "reason": "busy"}]
        assert done["report"]["remaining"]["owned"]["stories"] == [w.shared_story]


async def test_finished_reports_expire_with_the_retention_setting(pg_url: str) -> None:
    """The retention sweep purges a finished offboarding's row after
    ``offboarding_report_retention_days``; an unfinished or recent one stays."""
    async with offboarding_db(pg_url) as s:
        done, _ = await offboard(s.client, {"principal": unique("p-a")}, status=202)
        await run_jobs(s.db)
        waiting, _ = await offboard(s.client, {"principal": unique("p-b")}, status=202)
        swept = await sweep_retention(s.db, now=clock.now() + timedelta(days=89))
        assert swept["offboarding_jobs"] == 0
        swept = await sweep_retention(s.db, now=clock.now() + timedelta(days=91))
        assert swept["offboarding_jobs"] == 1
        gone = await s.client.get(f"{OFFBOARDING}/{done['job_id']}", headers=READ)
        assert gone.status_code == 404
        assert (await report(s.client, waiting["job_id"]))["status"] == "queued"


async def test_a_moving_assets_poster_moves_too(pg_url: str) -> None:
    """Media move with the transferred entry that shows them, whatever the media action, and so
    does a moving video's poster; the principal's other media follow the media action."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        # The image the character shows stands in for a video whose poster is the loose image.
        async with s.db.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE media_assets SET poster_media_id = %(p)s WHERE id = %(m)s",
                {"p": w.loose_image, "m": w.kept_image},
            )
        body = {
            "principal": w.a,
            "actions": {"characters": "transfer", "media": "keep"},
            "transfer_to": w.curator,
        }
        dry, _ = await offboard(s.client, {**body, "dry_run": True}, status=200)
        assert dry["counts"]["media"] == {"delete": 0, "transfer": 2, "keep": 1}
        assert dry["warnings"] == []
        queued, _ = await offboard(s.client, body, status=202)
        await run_jobs(s.db)
        found = (await report(s.client, queued["job_id"]))["report"]
        moved = {record["id"]: record for record in found["transferred"]["media"]}
        assert set(moved) == {w.kept_image, w.loose_image}
        assert {record["reason"] for record in moved.values()} == {"referenced"}
        assert found["kept"]["media"] == [w.shared_image]
        owner = await scalar(
            s.db, "SELECT owner_principal FROM media_assets WHERE id = :m", m=w.loose_image
        )
        assert owner == w.curator


async def test_the_job_yields_between_batches(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The job puts itself back in the queue after each batch, and each run continues from the
    progress row."""
    monkeypatch.setattr(offboarding_job, "BATCH", 3)
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        queued, _ = await offboard(s.client, {"principal": w.a}, status=202)
        assert await run_jobs(s.db, runs=1) == 1
        first = await report(s.client, queued["job_id"])
        assert first["status"] == "running"
        assert first["progress"] == {"done": 3, "total": 10}
        status = await scalar(s.db, "SELECT status FROM jobs WHERE id = :j", j=queued["job_id"])
        assert status == "queued", "yielded, not failed"
        failures = await scalar(s.db, "SELECT failures FROM jobs WHERE id = :j", j=queued["job_id"])
        assert failures == 0
        assert await run_jobs(s.db) == 3
        done = await report(s.client, queued["job_id"])
        assert done["status"] == "done"
        assert done["progress"] == {"done": 10, "total": 10}


async def test_a_fenced_run_writes_nothing(pg_url: str) -> None:
    """A run whose claim was taken over (its lease lapsed and the job was reclaimed) records
    nothing: no entity is acted on and the row is unchanged; the run that holds the claim
    finishes the job."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        queued, _ = await offboard(
            s.client,
            {"principal": w.a, "actions": {"personas": "delete"}, "ignore_warnings": True},
            status=202,
        )
        lost = await queue.claim(s.db, "lost:0", lease_s=30)
        assert lost is not None
        async with s.db.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE jobs SET status = 'queued', attempt = attempt + 1, lease_until = NULL "
                "WHERE id = %(j)s",
                {"j": lost.id},
            )
        before = await counts(s.db)
        output = await offboarding_job.OFFBOARDING_HANDLER.run(
            JobContext(db=s.db, job=lost, payload=offboarding_job.OffboardingPayload())
        )
        assert await queue.commit_job(
            s.db, lost, offboarding_job.OFFBOARDING_HANDLER, output, {}
        ) == (queue.CommitOutcome.REFUSED)
        assert await counts(s.db) == before
        assert (await report(s.client, queued["job_id"]))["status"] == "queued"
        assert await scalar(s.db, "SELECT count(*) FROM personas WHERE id = :p", p=w.persona) == 1
        await run_jobs(s.db)
        assert (await report(s.client, queued["job_id"]))["status"] == "done"
        assert await scalar(s.db, "SELECT count(*) FROM personas WHERE id = :p", p=w.persona) == 0


async def test_the_last_failed_attempt_reports_failure(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the job's last attempt fails, the offboarding is ``failed`` with the error's class
    and its partial report; earlier attempts leave it running for the retry."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        real = offboarding.apply
        seen: list[str] = []

        async def breaks_on_the_persona(*args: Any, **kwargs: Any) -> Any:
            if args[2]["type"] == "personas":
                raise RuntimeError("persona text never reaches the report")
            seen.append(str(args[2]["id"]))
            return await real(*args, **kwargs)

        monkeypatch.setattr(offboarding, "apply", breaks_on_the_persona)
        queued, _ = await offboard(
            s.client,
            {"principal": w.a, "actions": {"stories": "delete"}, "ignore_warnings": True},
            status=202,
        )
        await run_jobs(s.db)
        running = await report(s.client, queued["job_id"])
        assert running["status"] == "running"
        assert running["error"] is None
        async with s.db.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE jobs SET failures = %(n)s, due_at = now() WHERE id = %(j)s",
                {"n": offboarding_job.MAX_ATTEMPTS - 1, "j": queued["job_id"]},
            )
        await run_jobs(s.db)
        failed = await report(s.client, queued["job_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["class"] == "RuntimeError"
        assert "persona" not in failed["error"]["message"]
        assert sorted(failed["report"]["deleted"]["stories"]) == sorted(
            [w.shared_story, w.own_story]
        )
        assert failed["report"]["remaining"]["owned"]["personas"] == [w.persona]
        assert failed["finished_at"] is not None
        again, _ = await offboard(s.client, {"principal": w.a}, status=202)
        assert again["job_id"] != queued["job_id"], "a failed one does not block a new one"


async def test_a_job_the_framework_ends_does_not_leave_the_offboarding_active(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's runtime cap ends the job's last attempt without the handler's own failure
    path running (the handler is cancelled). The offboarding still reads ``failed`` with the
    job's error class and its partial report, and the principal can be offboarded again
    (``offboarding.reconcile``; #117 verification round 1)."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        real = offboarding.apply

        async def hangs_on_the_persona(*args: Any, **kwargs: Any) -> Any:
            if args[2]["type"] == "personas":
                await asyncio.sleep(60)
            return await real(*args, **kwargs)

        monkeypatch.setattr(offboarding, "apply", hangs_on_the_persona)
        queued, _ = await offboard(
            s.client,
            {"principal": w.a, "actions": {"stories": "delete"}, "ignore_warnings": True},
            status=202,
        )
        async with s.db.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE jobs SET failures = %(n)s WHERE id = %(j)s",
                {"n": offboarding_job.MAX_ATTEMPTS - 1, "j": queued["job_id"]},
            )
        capped = Worker(
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
        await run_jobs(s.db, fresh=capped)
        assert await scalar(s.db, "SELECT status FROM jobs WHERE id = :j", j=queued["job_id"]) == (
            "failed"
        )
        failed = await report(s.client, queued["job_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["class"] == "TimeoutError"
        assert failed["current"] is None
        assert failed["finished_at"] is not None
        assert sorted(failed["report"]["deleted"]["stories"]) == sorted(
            [w.shared_story, w.own_story]
        )
        assert failed["report"]["remaining"]["owned"]["personas"] == [w.persona]
        monkeypatch.setattr(offboarding, "apply", real)
        again, _ = await offboard(s.client, {"principal": w.a}, status=202)
        assert again["job_id"] != queued["job_id"]


async def test_an_offboarding_whose_job_is_gone_is_closed_at_the_next_request(
    pg_url: str,
) -> None:
    """A request for a principal whose active offboarding's job has ended or is gone closes the
    old one as ``failed`` in its own transaction and is accepted (not 409
    ``offboarding_in_progress``)."""
    async with offboarding_db(pg_url) as s:
        w = await world(s.client, s.db)
        queued, _ = await offboard(
            s.client,
            {"principal": w.a, "actions": {"stories": "delete"}, "ignore_warnings": True},
            status=202,
        )
        async with s.db.begin() as conn:
            await conn.exec_driver_sql(
                "UPDATE offboarding_jobs SET status = 'running' WHERE id = %(j)s",
                {"j": queued["job_id"]},
            )
            await conn.exec_driver_sql("DELETE FROM jobs WHERE id = %(j)s", {"j": queued["job_id"]})
        again, _ = await offboard(s.client, {"principal": w.a}, status=202)
        assert again["job_id"] != queued["job_id"]
        closed = await report(s.client, queued["job_id"])
        assert closed["status"] == "failed"
        assert closed["error"]["class"] == "JobMissing"
        assert closed["report"]["deleted"]["stories"] == []


async def test_a_principal_may_hold_a_slash(pg_url: str) -> None:
    """A principal is the rest of the lookup's path, so it may contain ``/``; a blank one is
    422 at the path."""
    async with offboarding_db(pg_url) as s:
        team = "team/ana"
        created = await ok(
            await s.client.post("/stories", json=story_body("Una", "Bo"), headers=headers(team)),
            201,
        )
        found = await lookup(s.client, team)
        assert _ids(found["owned"]["stories"]) == [created["story_id"]]
        blank = await s.client.get("/principals/%20", headers=READ)
        assert blank.status_code == 422, blank.text
        assert blank.json()["error"]["details"]["field"] == "path.principal_id"
