"""C-18's M3 part (docs/04: FR-PROF-16) and the detailed tests of #109's two-stage flow, impact
preview and effective-configuration read.

C-16's M3 slices of the same story (``test_c_16__two_stage``) and its M4 interim slice
(``test_c_16__upgrade_unavailable``) are in ``tests/integration/test_engine_profiles.py``, beside
#106's ``test_c_16__crud``: traceability counts a test ID per milestone in one file.

The tests that change the deployment's default or the Lite preset run on a scratch database of
their own.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import ImpactPreviewRequest, ProfileUpdate, RequestContext
from epistrel.config import Settings
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.profiles import impact
from epistrel.core.profiles.fingerprint import FINGERPRINT_PATTERN
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.orchestrator.models.registry import DELIVERED as DELIVERED_PURPOSES
from epistrel.testing.profiles import data_producing
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.conftest import alembic_config
from tests.event_support import rows, scalar
from tests.profile_support import (
    LITE_CAPABILITIES,
    POLISH,
    PROFILES,
    QUALITY_STATUSES,
    SETTINGS,
    STATUS_COMPONENT,
    UNCERTIFIED,
    configuration_rows,
    merged,
    nest,
    profile_body,
)
from tests.respond_support import body as respond_body
from tests.respond_support import respond_client
from tests.story_support import story_body

READ = bearer()
A = "p-two-stage-a"
B = "p-two-stage-b"


def headers(principal: str, key: str | None = None) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": principal}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json() if response.content else {}
    return found


def refused(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == code, error
    return error


@dataclass
class Space:
    settings: Settings
    db: AsyncEngine


@pytest.fixture
async def scratch(pg_url: str) -> AsyncIterator[Space]:
    """A database of the test's own, migrated to head (the default profile and the Lite preset
    change there, never in the shared database)."""
    with migrated_database(pg_url, f"epistrel_two_stage_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            yield Space(settings, db)
        finally:
            await db.dispose()


async def _story(client: httpx.AsyncClient) -> tuple[str, str]:
    created = await ok(
        await client.post("/stories", json=story_body("Wren", "Ada"), headers=headers(A)), 201
    )
    return created["story_id"], created["characters"][0]["id"]


async def _play(client: httpx.AsyncClient, story_id: str, actor: str) -> dict[str, Any]:
    played = await client.post(
        f"/stories/{story_id}/respond", json=respond_body(actor), headers=headers(A)
    )
    return await ok(played)


async def _effective(client: httpx.AsyncClient, story_id: str) -> dict[str, Any]:
    return await ok(await client.get(f"/stories/{story_id}/effective-config", headers=READ))


async def _preview(
    client: httpx.AsyncClient, story_id: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.post(f"/stories/{story_id}/impact-preview", json=body, headers=headers(A))


async def _default(client: httpx.AsyncClient, profile_id: str) -> None:
    await ok(
        await client.put(
            "/admin/settings", json={"default_engine_profile_id": profile_id}, headers=headers(A)
        )
    )


async def _turn_revisions(db: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    found = await rows(
        db,
        "SELECT config_revisions FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY committed_at",
        s=story_id,
    )
    return [dict(r[0]) for r in found]


# -- C-18 --------------------------------------------------------------------------------------


@pytest.mark.part("M3", "FR-PROF-16")
async def test_c_18__fingerprint(scratch: Space) -> None:
    """Every respond response, effective-configuration read and impact report states the
    configuration's fingerprint, the capabilities it enables and its quality status,
    ``uncertified``: the published status is an enum of the three statuses, of which M3 can
    produce only this one.

    A turn under the Lite preset answers a ``sha256:`` fingerprint, Lite's capabilities and
    ``uncertified``; its operation records the same fingerprint in ``config_revisions`` with the
    profile, its version, each delivered purpose's model (no reported version yet), the status
    and the capabilities. The story's effective configuration states the same fingerprint,
    capabilities and status, and a preview's report states it before and after a change.

    A change to a quality-relevant setting gives a new fingerprint: another generation model, a
    summary-window override in a preview, and an edit of the generation temperature, which the
    next turn then reports and records with the new version. A change to a quality-irrelevant
    setting does not (complement): the pinned-memories limit as an override, and the story's
    turn concurrency."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        routed = await client.put(
            "/admin/model-config/generation", json={"model": "house-model"}, headers=headers(A)
        )
        await ok(routed)
        story, actor = await _story(client)
        turn = await _play(client, story, actor)
        assert re.fullmatch(FINGERPRINT_PATTERN, turn["fingerprint"])
        assert (turn["capabilities"], turn["quality_status"]) == (LITE_CAPABILITIES, UNCERTIFIED)
        (recorded,) = await _turn_revisions(scratch.db, story)
        assert recorded["fingerprint"] == turn["fingerprint"]
        assert (recorded["profile"], recorded["profile_version"]) == (LITE_PRESET_ID, 1)
        assert (recorded["quality_status"], recorded["capabilities"]) == (
            UNCERTIFIED,
            LITE_CAPABILITIES,
        )
        configured = (await ok(await client.get("/admin/model-config", headers=READ)))["purposes"]
        assert recorded["models"] == {
            purpose: {"id": configured[purpose]["model"], "reported_version": None}
            for purpose in DELIVERED_PURPOSES
        }
        assert recorded["models"]["generation"]["id"] == "house-model"

        read = await _effective(client, story)
        assert (read["fingerprint"], read["capabilities"], read["quality_status"]) == (
            turn["fingerprint"],
            LITE_CAPABILITIES,
            UNCERTIFIED,
        )
        assert read["profile"] == {
            "id": LITE_PRESET_ID,
            "version": 1,
            "pinned": False,
            "deleted": False,
        }
        document = (await client.get("/openapi.json")).json()
        statuses = document["components"]["schemas"][STATUS_COMPONENT]["enum"]
        assert sorted(statuses) == sorted(QUALITY_STATUSES)

        # -- quality-irrelevant: the same fingerprint
        same = await ok(await _preview(client, story, {"overrides": nest("pins.max_pinned", 10)}))
        assert same["fingerprint_before"] == same["fingerprint_after"] == read["fingerprint"]
        assert (same["quality_status_after"], same["warnings"]) == (UNCERTIFIED, [])
        assert same["affected_stories"][0]["fingerprint_after"] == read["fingerprint"]
        concurrency = await client.patch(
            f"/stories/{story}/settings", json={"turn_concurrency": "queue"}, headers=headers(A)
        )
        await ok(concurrency)
        assert (await _effective(client, story))["fingerprint"] == read["fingerprint"]

        # -- quality-relevant: a new fingerprint, from a model, an override and an edit
        swapped = await client.put(
            "/admin/model-config/generation", json={"model": "other-model"}, headers=headers(A)
        )
        await ok(swapped)
        assert (await _effective(client, story))["fingerprint"] != read["fingerprint"]
        restored = await client.put(
            "/admin/model-config/generation", json={"model": "house-model"}, headers=headers(A)
        )
        await ok(restored)
        assert (await _effective(client, story))["fingerprint"] == read["fingerprint"]
        window = await ok(
            await _preview(
                client, story, {"overrides": nest("memory.rolling_summary.window_messages", 12)}
            )
        )
        assert window["fingerprint_before"] == read["fingerprint"] != window["fingerprint_after"]
        assert window["quality_status_after"] == UNCERTIFIED
        lite = (await ok(await client.get(f"{PROFILES}/{LITE_PRESET_ID}", headers=READ)))[
            "settings"
        ]
        warmer = merged(lite, nest("models.generation.temperature", 0.6))
        edited = await ok(
            await client.put(
                f"{PROFILES}/{LITE_PRESET_ID}",
                json={"name": "Lite", "settings": warmer},
                headers=headers(A),
            )
        )
        assert edited["applied"] is True
        (entry,) = edited["impact"]["affected_stories"]
        assert entry["fingerprint_before"] == read["fingerprint"] != entry["fingerprint_after"]
        assert entry["quality_status_after"] == UNCERTIFIED
        changed = await _effective(client, story)
        assert changed["fingerprint"] == entry["fingerprint_after"]
        assert changed["capabilities"] == LITE_CAPABILITIES
        next_turn = await _play(client, story, actor)
        assert next_turn["fingerprint"] == entry["fingerprint_after"]
        assert next_turn["quality_status"] == UNCERTIFIED
        latest = (await _turn_revisions(scratch.db, story))[-1]
        assert (latest["fingerprint"], latest["profile_version"]) == (entry["fingerprint_after"], 2)


# -- the detailed tests ----------------------------------------------------------------------------


async def test_a_held_edit_releases_its_key_for_the_confirmation(scratch: Space) -> None:
    """An edit held for confirmation keeps no ``Idempotency-Key`` row and records no operation,
    in REST and in the library: the confirmation under the same key is its first execution, and
    only that is stored (a retry replays it). The library answers ``TwoStageResult`` with the
    report and no result."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        profile = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        await _default(client, profile)
        await _story(client)
        quiet = {
            "name": "Brisk Lite",
            "settings": merged(SETTINGS, nest("memory.rolling_summary.enabled", False)),
        }
        key = f"held-{secrets.token_hex(6)}"
        held = await ok(
            await client.put(f"{PROFILES}/{profile}", json=quiet, headers=headers(B, key))
        )
        assert held["applied"] is False
        keyed = "SELECT count(*) FROM idempotency_keys WHERE key = :k"
        assert await scalar(scratch.db, keyed, k=key) == 0
        assert (
            await scalar(
                scratch.db, "SELECT count(*) FROM operations WHERE idempotency_key = :k", k=key
            )
            == 0
        )

        library = await lib.update_profile(
            RequestContext(principal=B, idempotency_key=key),
            profile,
            ProfileUpdate.model_validate(quiet),
        )
        assert (library.applied, library.result) == (False, None)
        assert library.impact.model_dump(mode="json") == held["impact"]
        assert await scalar(scratch.db, keyed, k=key) == 0

        confirmed = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={**quiet, "ignore_warnings": True},
                headers=headers(B, key),
            )
        )
        assert confirmed["applied"] is True
        assert await scalar(scratch.db, keyed, k=key) == 1
        other = await client.put(f"{PROFILES}/{profile}", json=quiet, headers=headers(B, key))
        refused(other, 409, "idempotency_conflict")
        await _default(client, LITE_PRESET_ID)


async def test_preview_refusals_write_nothing(scratch: Space) -> None:
    """A preview names a profile, overrides or both: neither is 422 at ``body``. A profile that
    does not exist or is deleted is 422 ``profile_deleted`` (``unknown``, ``deleted``) at
    ``body.profile_id``; an override that names an undelivered module is 422 ``invalid_profile``,
    and one of the wrong shape (a group sent as ``null`` included: overrides are settings, not a
    merge patch) 422 ``validation_error`` at its field; an unknown story is 404.
    A story's own profile is no reassignment, deleted or not: its preview is answered. Nothing
    is ever written, and no key is echoed."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        await _default(client, profile)
        story, _ = await _story(client)
        await _default(client, LITE_PRESET_ID)
        await ok(await client.delete(f"{PROFILES}/{profile}", headers=headers(A)))
        other = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        await ok(await client.delete(f"{PROFILES}/{other}", headers=headers(A)))
        before = await configuration_rows(scratch.db)
        error = refused(await _preview(client, story, {}), 422, "validation_error")
        assert error["details"]["field"] == "body"
        unknown = refused(
            await _preview(client, story, {"profile_id": "0" * 26}), 422, "profile_deleted"
        )
        assert (unknown["details"]["reason"], unknown["details"]["field"]) == (
            "unknown",
            "body.profile_id",
        )
        gone = refused(await _preview(client, story, {"profile_id": other}), 422, "profile_deleted")
        assert gone["details"]["reason"] == "deleted"
        refused(
            await _preview(client, story, {"overrides": {"commitment": {"enabled": True}}}),
            422,
            "invalid_profile",
        )
        shape = refused(
            await _preview(client, story, {"overrides": nest("pins.max_pinned", "ten")}),
            422,
            "validation_error",
        )
        assert shape["details"]["field"] == "body.overrides.pins.max_pinned"
        nulled = refused(
            await _preview(client, story, {"overrides": {"repair": None}}), 422, "validation_error"
        )
        assert nulled["details"]["field"] == "body.overrides.repair"
        refused(await _preview(client, "0" * 26, {"profile_id": LITE_PRESET_ID}), 404, "not_found")
        own = await _preview(
            client, story, {"profile_id": profile, "overrides": nest(POLISH, False)}
        )
        report = await ok(own)
        assert "Idempotency-Key" not in own.headers
        assert report["affected_stories"][0]["capabilities_lost"] == ["polish"]
        assert (await _effective(client, story))["profile"]["deleted"] is True
        assert await configuration_rows(scratch.db) == before


async def test_reports_list_at_most_the_cap(
    scratch: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile edit's report lists its following stories by id up to ``AFFECTED_MAX`` and
    counts them all; a story pinned to an earlier version does not follow the profile and is
    neither listed nor moved by later edits, and a fork of it keeps its pin."""
    monkeypatch.setattr(impact, "AFFECTED_MAX", 2)
    off = merged(SETTINGS, nest(POLISH, False))
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile = (
            await ok(
                await client.post(PROFILES, json=profile_body(settings=off), headers=headers(A)),
                201,
            )
        )["id"]
        await _default(client, profile)
        lived, actor = await _story(client)
        await _play(client, lived, actor)
        others = sorted([(await _story(client))[0] for _ in range(3)])
        polished = merged(SETTINGS, nest(POLISH, True))
        on = {"name": "Brisk Lite", "settings": polished}
        with data_producing("polish"):
            held = await ok(await client.put(f"{PROFILES}/{profile}", json=on, headers=headers(A)))
            listed = [e["story_id"] for e in held["impact"]["affected_stories"]]
            assert listed == sorted([lived, *others])[:2]
            assert held["impact"]["affected_count"] == 4
            assert "upgrade_unavailable" in held["impact"]["warnings"]
            await ok(
                await client.put(
                    f"{PROFILES}/{profile}",
                    json={**on, "ignore_warnings": True},
                    headers=headers(A),
                )
            )
        assert (await _effective(client, lived))["profile"]["version"] == 1
        warmer = merged(polished, nest("models.generation.temperature", 0.5))
        later = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={"name": "Brisk Lite", "settings": warmer},
                headers=headers(A),
            )
        )
        assert later["applied"] is True
        assert lived not in [e["story_id"] for e in later["impact"]["affected_stories"]]
        assert later["impact"]["affected_count"] == 3
        assert (await _effective(client, lived))["profile"] == {
            "id": profile,
            "version": 1,
            "pinned": True,
            "deleted": False,
        }
        assert (await _effective(client, others[0]))["profile"]["version"] == 3
        forked = await ok(
            await client.post(f"/stories/{lived}/fork", json={"at_turn": 1}, headers=headers(A)),
            201,
        )
        assert (await _effective(client, forked["story_id"]))["profile"]["pinned"] is True
        await _default(client, LITE_PRESET_ID)


async def test_a_new_profile_version_is_a_new_fingerprint(scratch: Space) -> None:
    """The profile version is part of the fingerprint, so an edit that changes only
    a quality-irrelevant setting still gives the stories that follow it a new fingerprint (a new
    version, applied at once: no capability moves), while the same change as a story override
    keeps it. Both stay ``uncertified``."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        story, _ = await _story(client)
        before = (await _effective(client, story))["fingerprint"]
        override = await ok(
            await _preview(client, story, {"overrides": nest("pins.max_pinned", 7)})
        )
        assert override["fingerprint_after"] == before
        lite = (await ok(await client.get(f"{PROFILES}/{LITE_PRESET_ID}", headers=READ)))[
            "settings"
        ]
        edited = await ok(
            await client.put(
                f"{PROFILES}/{LITE_PRESET_ID}",
                json={"name": "Lite", "settings": merged(lite, nest("pins.max_pinned", 7))},
                headers=headers(A),
            )
        )
        assert (edited["applied"], edited["impact"]["warnings"]) == (True, [])
        (entry,) = edited["impact"]["affected_stories"]
        assert entry["fingerprint_before"] == before != entry["fingerprint_after"]
        assert entry["quality_status_after"] == UNCERTIFIED


async def test_library_matches_rest(scratch: Space) -> None:
    """The library's preview and effective-configuration read answer what REST does."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        story, _ = await _story(client)
        body = {"profile_id": LITE_PRESET_ID, "overrides": nest("pins.max_pinned", 5)}
        rest = await ok(await _preview(client, story, body))
        library = await lib.impact_preview(
            RequestContext(principal=A), story, ImpactPreviewRequest.model_validate(body)
        )
        assert library.model_dump(mode="json") == rest
        read = await lib.get_effective_config(RequestContext(), story)
        assert read.model_dump(mode="json") == await _effective(client, story)


def test_the_pin_migration_round_trips(scratch_database: str) -> None:
    """0026 adds ``stories.engine_profile_version_pin``: every existing story follows its
    profile (``NULL``), a pin must name a version of the story's profile (a foreign key), and
    the downgrade drops the column and its key."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0025")
    sync = make_sync_engine(scratch_database)
    story = "0000000000000000000000STR8"
    try:
        with sync.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO stories (id, owner_principal, engine_profile_id) "
                    "VALUES (:s, 'p-1', :p)"
                ),
                {"s": story, "p": LITE_PRESET_ID},
            )
        command.upgrade(cfg, "0026")
        with sync.begin() as conn:
            pin = conn.execute(
                text("SELECT engine_profile_version_pin FROM stories WHERE id = :s"), {"s": story}
            ).scalar_one()
            assert pin is None
            conn.execute(
                text("UPDATE stories SET engine_profile_version_pin = 1 WHERE id = :s"),
                {"s": story},
            )
        with pytest.raises(IntegrityError), sync.begin() as conn:
            conn.execute(
                text("UPDATE stories SET engine_profile_version_pin = 9 WHERE id = :s"),
                {"s": story},
            )
        command.downgrade(cfg, "0025")
        with sync.connect() as conn:
            columns = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name = 'stories' "
                    "AND column_name = 'engine_profile_version_pin'"
                )
            ).scalar_one()
            assert columns == 0
        command.upgrade(cfg, "head")
    finally:
        sync.dispose()
