"""A story's effective configuration, its profile and its overrides (#113): the detailed tests
behind the U-28 (M3) and C-16 (M3) slices in ``tests/integration/test_engine_profiles.py`` (where
traceability wants every part of a test ID and milestone) and the I-18 (M3) slice in
``tests/integration/test_i_18.py``.

- No engine dial lives in a story's settings, nor in the deployment's outside its profile
  defaults; M2's names of the dials that moved are refused, naming their replacement.
- ``PUT /stories/{id}/engine-profile`` and ``PATCH /stories/{id}/profile-overrides`` run in two
  stages, are story-scoped configuration changes, and refuse what cannot run.
- A profile edit reaches each follower through its own overrides, and is refused when a
  follower could not run under it.
- ``profile_defaults`` (``/admin/settings``) is laid over, stays complete and valid, and is the
  bottom layer of every configuration.
- The configuration's switches and dials shape the turn: the rolling summary, the pinned
  memories and their cap, the call cap and its source.
- The resolution cache follows every layer.
- Migration 0030 carries M2's stored dials over.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import pytest
from alembic import command
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import ProfileAssignment, ProfileOverridesPatch, RequestContext
from epistrel.api.app import create_app
from epistrel.config import Settings
from epistrel.core import summary
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.errors import InvalidRequest
from epistrel.core.profiles import effective
from epistrel.core.profiles.schema import DependencyRule, ProfileSettings, Requirement
from epistrel.core.profiles.story import PATCH_SCHEMA
from epistrel.core.seeds.profiles import LITE_PRESET_ID, lite_settings
from epistrel.core.settings_schema import (
    PROFILE_DEFAULTS,
    SUPERSEDED_DEPLOYMENT_SETTINGS,
    SUPERSEDED_STORY_SETTINGS,
    DeploymentSettings,
    StorySettings,
)
from epistrel.core.stories import StorySpec
from epistrel.testing.profiles import TEST_CLAUSE, extra_rule
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.conftest import alembic_config
from tests.event_support import rows, scalar
from tests.profile_support import (
    LITE_CAPABILITIES,
    PROFILES,
    REDERIVATION,
    configuration_rows,
    merged,
    naming,
    nest,
    profile_body,
    undelivered_modules,
)
from tests.respond_support import body as respond_body
from tests.respond_support import generation_calls, respond_client
from tests.story_support import story_body
from tests.summary_support import run_due_jobs, summary_jobs

A = "p-effective-a"
READ = bearer()
SUMMARY_OFF = nest("memory.rolling_summary.enabled", False)
PINS_OFF = nest("pins.enabled", False)
WINDOW = "memory.rolling_summary.window_messages"
PINS_NEED_SUMMARY = DependencyRule(TEST_CLAUSE, "pins", (Requirement("rolling_summary"),))
"""A test-only rule (no two delivered modules depend on each other): pinned memories need the
rolling summary."""


def headers(key: str | None = None) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": A}
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
    """An app's settings and its database: a scratch one of the test's own."""

    settings: Settings
    db: AsyncEngine


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def scratch(pg_url: str) -> AsyncIterator[Space]:
    """A database of the test's own, migrated to head: the deployment's profile defaults change
    there, never in the shared database."""
    with migrated_database(pg_url, f"epistrel_effective_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            yield Space(settings, db)
        finally:
            await db.dispose()


async def _story(client: httpx.AsyncClient, **fields: Any) -> tuple[str, str]:
    created = await ok(
        await client.post("/stories", json=story_body("Wren", "Ada", **fields), headers=headers()),
        201,
    )
    return created["story_id"], created["characters"][0]["id"]


async def _profile(client: httpx.AsyncClient, settings: dict[str, Any], name: str = "P") -> str:
    created = await ok(
        await client.post(PROFILES, json=profile_body(name, settings=settings), headers=headers()),
        201,
    )
    return str(created["id"])


async def _assign(
    client: httpx.AsyncClient, story: str, profile: str, key: str | None = None, **fields: Any
) -> httpx.Response:
    return await client.put(
        f"/stories/{story}/engine-profile",
        json={"profile_id": profile, **fields},
        headers=headers(key),
    )


async def _override(
    client: httpx.AsyncClient, story: str, settings: Any, key: str | None = None, **fields: Any
) -> httpx.Response:
    return await client.patch(
        f"/stories/{story}/profile-overrides",
        json={"settings": settings, **fields},
        headers=headers(key),
    )


async def _effective(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    return await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))


async def _play(client: httpx.AsyncClient, story: str, actor: str, **fields: Any) -> Any:
    return await client.post(
        f"/stories/{story}/respond", json=respond_body(actor, **fields), headers=headers()
    )


async def _story_changes(db: AsyncEngine, story: str) -> list[tuple[Any, ...]]:
    found = await rows(
        db,
        'SELECT setting, old, new, "class", actor, config_revision FROM config_changes '
        "WHERE scope = 'story' AND scope_id = :s ORDER BY config_revision, setting",
        s=story,
    )
    return [tuple(r) for r in found]


async def _story_row(db: AsyncEngine, story: str) -> Any:
    (row,) = await rows(
        db,
        "SELECT engine_profile_id, engine_profile_version_pin, config_revision, "
        "timeline_revision FROM stories WHERE id = :s",
        s=story,
    )
    return row


# -- no engine dial in a story's settings (AC5) ------------------------------------------------


def _names(model: type[BaseModel]) -> set[str]:
    """Every field name of ``model``, at every level (its inner models walked)."""
    found: set[str] = set()
    for name, info in model.model_fields.items():
        found.add(name)
        for inner in (info.annotation, *getattr(info.annotation, "__args__", ())):
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                found |= _names(inner)
    return found


def _shared(model: type[BaseModel], *, beside: set[str] = frozenset()) -> set[str]:  # type: ignore[assignment]
    """The field names ``model`` shares with the engine profile's settings."""
    return (_names(model) - beside) & _names(ProfileSettings)


def _paths(document: Any, prefix: str = "") -> set[str]:
    found: set[str] = set()
    for key, value in (document or {}).items():
        path = f"{prefix}{key}"
        found.add(path)
        if isinstance(value, dict):
            found |= _paths(value, f"{path}.")
    return found


class _Leaky(BaseModel):
    """A fixture: a story-settings-like model that holds an engine dial."""

    turn_concurrency: str = "reject"
    temperature: float = 0.8


def test_story_settings_hold_no_engine_dial() -> None:
    """A story's settings describe the story (FR-SCN-2's story settings defaults) and hold no
    engine dial (FR-PROF-5): ``StorySettings`` and the engine profile's ``ProfileSettings``
    share no field name at any level, nor any dotted path, and neither does
    ``DeploymentSettings`` outside ``profile_defaults``. Each of M2's dials that moved names a
    setting of the profile, and none is a story or deployment setting any more.

    Complement asserted: a model holding a dial (``temperature``) is reported."""
    assert _shared(StorySettings) == set()
    assert {p for p, _ in leaves_of(StorySettings)} & {
        p for p, _ in leaves_of(ProfileSettings)
    } == (set())
    assert _shared(DeploymentSettings, beside={PROFILE_DEFAULTS}) == set()
    profile_paths = _paths(ProfileSettings().model_dump(mode="json"))
    for old, new in SUPERSEDED_STORY_SETTINGS.items():
        assert old not in StorySettings.model_fields, old
        assert new in profile_paths, new
    for old, new in SUPERSEDED_DEPLOYMENT_SETTINGS.items():
        assert old not in DeploymentSettings.model_fields, old
        assert new in profile_paths, new
    assert _shared(_Leaky) == {"temperature"}


def leaves_of(model: type[BaseModel], prefix: str = "") -> list[tuple[str, Any]]:
    """Every leaf of ``model`` by dotted path (``settings_schema.leaves``, re-walked here)."""
    found: list[tuple[str, Any]] = []
    for name, info in model.model_fields.items():
        inner = info.annotation
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            found += leaves_of(inner, f"{prefix}{name}.")
        else:
            found.append((f"{prefix}{name}", info))
    return found


async def test_moved_dials_are_refused_naming_their_replacement(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """M2's engine dials are refused wherever they were written, each naming the profile
    setting that replaced it (``details.replacement``) with ``reason: superseded``: a story
    setting (``PATCH /stories/{id}/settings``, and at creation), and a deployment setting
    (``PUT /admin/settings``, under ``profile_defaults``). Nothing is written.

    Complement asserted: the settings that stayed are written as before."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _ = await _story(client)
        before = await configuration_rows(engine)
        for name, replacement in SUPERSEDED_STORY_SETTINGS.items():
            patched = await client.patch(
                f"/stories/{story}/settings", json={name: None}, headers=headers()
            )
            details = refused(patched, 422, "validation_error")["details"]
            assert (details["field"], details["reason"], details["replacement"]) == (
                f"body.{name}",
                "superseded",
                replacement,
            )
        created = await client.post(
            "/stories",
            json=story_body("Wren", "Ada", settings={"turn_deadline": 30}),
            headers=headers(),
        )
        assert refused(created, 422, "validation_error")["details"]["field"] == (
            "body.settings.turn_deadline"
        )
        with pytest.raises(InvalidRequest, match=r"budgets\.deadline_ms"):
            StorySpec.model_validate(
                {"characters": [{"name": "W", "definition": "w"}], "settings": {"turn_deadline": 3}}
            )
        for name, replacement in SUPERSEDED_DEPLOYMENT_SETTINGS.items():
            put = await client.put("/admin/settings", json={name: 1}, headers=headers())
            details = refused(put, 422, "validation_error")["details"]
            assert details["replacement"] == f"{PROFILE_DEFAULTS}.{replacement}"
        assert await configuration_rows(engine) == before
        kept = await client.patch(
            f"/stories/{story}/settings", json={"regenerate_retry_budget": 1}, headers=headers()
        )
        assert (await ok(kept))["settings"]["regenerate_retry_budget"] == 1
        with pytest.raises(InvalidRequest):
            await lib.update_story_settings(
                RequestContext(principal=A), story, {"lite_window_messages": 4}
            )


# -- assignment ----------------------------------------------------------------------------------


async def test_assignment_runs_in_two_stages(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``PUT /stories/{id}/engine-profile`` (FR-PROF-1,7; FR-API-6). Assigning a profile that
    turns the rolling summary off loses a capability: the answer is 200 with ``applied: false``,
    the one-story report (the capability lost, the fingerprints at the top) and no ``result``,
    and nothing is written (the story, its revisions, the configuration log, no key kept). Sent
    again under the same key with ``ignoreWarnings`` it applies: the story runs under the new
    profile at its latest version (its pin cleared), its ``config_revision`` moves by one and its
    timeline revision does not, one prospective ``engine_profile_id`` row and a
    ``config_changed`` event record it, a retry replays it, ``GET /stories/{id}`` names the
    profile and version, and the next turn reports it. The story's own profile again changes
    nothing. A deleted or unknown profile is 422 ``profile_deleted``; ``null`` is 422; an
    archived story is 409 ``story_archived``; the library answers as REST does."""
    async with respond_client(stub_settings, engine) as (client, lib):
        quiet = await _profile(client, SUMMARY_OFF, "Quiet")
        story, actor = await _story(client)
        await engine_pin(engine, story, LITE_PRESET_ID, 1)
        start = await _story_row(engine, story)
        before = await configuration_rows(engine)
        key = f"assign-{secrets.token_hex(6)}"
        held = await ok(await _assign(client, story, quiet, key))
        assert (held["applied"], held["result"]) == (False, None)
        report = held["impact"]
        assert (report["warnings"], report["affected_count"]) == (["capabilities_lost"], 1)
        (entry,) = report["affected_stories"]
        assert (entry["story_id"], entry["capabilities_lost"]) == (story, ["rolling_summary"])
        assert entry[REDERIVATION] == {"needed": False, "estimate": None}
        assert report["capabilities_after"] == [
            c for c in LITE_CAPABILITIES if c != "rolling_summary"
        ]
        assert report["fingerprint_before"] != report["fingerprint_after"]
        assert await configuration_rows(engine) == before

        confirmed = await _assign(client, story, quiet, key, ignoreWarnings=True)
        done = await ok(confirmed)
        assert (done["applied"], done["impact"]) == (True, report)
        assert done["result"]["profile"] == {
            "id": quiet,
            "version": 1,
            "pinned": False,
            "deleted": False,
        }
        assert done["result"]["fingerprint"] == report["fingerprint_after"]
        row = await _story_row(engine, story)
        assert (row.engine_profile_id, row.engine_profile_version_pin) == (quiet, None)
        assert row.config_revision == done["config_revision"] == start.config_revision + 1
        assert row.timeline_revision == done["timeline_revision"] == start.timeline_revision
        changes = await _story_changes(engine, story)
        assert changes[-1][:5] == ("engine_profile_id", LITE_PRESET_ID, quiet, "prospective", A)
        assert changes[-1][5] == done["config_revision"]
        events = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'config_changed'",
            s=story,
        )
        assert [e.payload for e in events] == [
            {"setting": "engine_profile_id", "old": LITE_PRESET_ID, "new": quiet}
        ]
        again = await _assign(client, story, quiet, key, ignoreWarnings=True)
        assert (again.headers.get("Idempotent-Replay"), again.json()) == ("true", done)
        shown = await ok(await client.get(f"/stories/{story}", headers=READ))
        assert shown["engine_profile"] == {"id": quiet, "version": 1}
        played = await ok(await _play(client, story, actor))
        assert played["profile"] == {"id": quiet, "version": 1}

        # -- the same profile changes nothing; what cannot be assigned is refused
        same = await ok(await _assign(client, story, quiet))
        assert (same["applied"], same["impact"]["warnings"]) == (True, [])
        assert same["config_revision"] == done["config_revision"]
        assert len(await _story_changes(engine, story)) == len(changes)
        gone = await _profile(client, {}, "Gone")
        await ok(await client.delete(f"{PROFILES}/{gone}", headers=headers()))
        for profile, reason in ((gone, "deleted"), ("0" * 26, "unknown")):
            details = refused(
                await _assign(client, story, profile, ignore_warnings=True), 422, "profile_deleted"
            )["details"]
            assert (details["field"], details["reason"]) == ("body.profile_id", reason)
        refused(await _assign(client, story, None), 422, "validation_error")  # type: ignore[arg-type]

        # -- the library, and an archived story
        ctx = RequestContext(principal=A)
        back = await lib.assign_engine_profile(
            ctx, story, ProfileAssignment(profile_id=LITE_PRESET_ID, ignore_warnings=True)
        )
        assert back.applied
        assert back.result is not None
        assert back.result.profile.id == LITE_PRESET_ID
        await ok(await client.post(f"/stories/{story}/archive", headers=headers()))
        refused(await _assign(client, story, quiet, ignore_warnings=True), 409, "story_archived")
        unchanged = await ok(await _assign(client, story, LITE_PRESET_ID))
        assert unchanged["applied"] is True


async def engine_pin(db: AsyncEngine, story: str, profile: str, version: int) -> None:
    """Pin ``story`` to ``version`` of its profile (what an unavailable upgrade leaves)."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stories SET engine_profile_version_pin = :v "
                "WHERE id = :s AND engine_profile_id = :p"
            ),
            {"v": version, "s": story, "p": profile},
        )


async def test_assignment_keeps_and_rechecks_the_overrides(scratch: Space) -> None:
    """The story's overrides survive a reassignment and are checked against the new profile
    (FR-PROF-4,5). The story runs without pinned memories and overrides the rolling summary off;
    while a test-only rule makes pinned memories need the summary, assigning Lite (pinned
    memories on) would leave it unable to run: 422 ``invalid_profile`` naming the overrides in
    ``details.overrides``, and nothing is written. Complement: another profile without pinned
    memories is assigned, the overrides intact."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        pinless = await _profile(client, PINS_OFF, "Pinless")
        story, _ = await _story(client)
        await ok(await _assign(client, story, pinless, ignore_warnings=True))
        await ok(await _override(client, story, SUMMARY_OFF, ignore_warnings=True))
        assert (await _effective(client, story))["profile"]["id"] == pinless
        with extra_rule(PINS_NEED_SUMMARY):
            before = await configuration_rows(scratch.db)
            error = refused(
                await _assign(client, story, LITE_PRESET_ID, ignore_warnings=True),
                422,
                "invalid_profile",
            )
            assert error["details"]["overrides"] == SUMMARY_OFF
            assert [m["module"] for m in error["details"]["missing_dependencies"]] == ["pins"]
            assert await configuration_rows(scratch.db) == before
            also_pinless = await _profile(client, PINS_OFF, "Pinless too")
            moved = await ok(await _assign(client, story, also_pinless, ignore_warnings=True))
            assert moved["result"]["overrides"] == {"settings": SUMMARY_OFF, "revision": 1}


# -- overrides -----------------------------------------------------------------------------------


async def test_overrides_are_a_merge_patch(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``PATCH /stories/{id}/profile-overrides`` (FR-PROF-5,7). ``settings`` is a JSON merge
    patch over the story's overrides: a key sets a dial, an object merges, ``null`` removes the
    override. Each write that changes something moves the overrides' revision and the story's
    ``config_revision`` by one, and records one row per dial set, changed or removed
    (``profile_overrides.<path>``, prospective) with a ``config_changed`` event each; a patch
    that changes nothing writes nothing. ``expected_revision`` other than the current one is
    409 ``revision_conflict`` (``reason: overrides_revision``). A patch whose result loses a
    capability (the rolling summary off) is held until confirmed. A malformed value is 422
    ``validation_error`` at its path; an undelivered module is 422 ``invalid_profile``; nothing
    is written for either. The published schema lets every key be ``null``. An archived story
    is 409 ``story_archived``."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _ = await _story(client)
        start = (await _story_row(engine, story)).config_revision
        first = await ok(
            await _override(
                client,
                story,
                merged(nest(WINDOW, 9), nest("models.generation.temperature", 0.4)),
                expected_revision=0,
            )
        )
        assert first["applied"] is True
        assert first["result"]["overrides"]["revision"] == 1
        assert first["config_revision"] == start + 1
        merged_in = await ok(await _override(client, story, nest("models.generation.top_p", 0.6)))
        assert merged_in["result"]["overrides"]["settings"] == merged(
            nest(WINDOW, 9), {"models": {"generation": {"temperature": 0.4, "top_p": 0.6}}}
        )
        removed = await ok(
            await _override(client, story, {"models": {"generation": {"temperature": None}}})
        )
        assert removed["result"]["overrides"] == {
            "settings": merged(nest(WINDOW, 9), nest("models.generation.top_p", 0.6)),
            "revision": 3,
        }
        assert removed["result"]["effective"]["models"]["generation"]["temperature"] == 0.8
        changes = [c[:4] for c in await _story_changes(engine, story)]
        assert changes == [
            ("profile_overrides.memory.rolling_summary.window_messages", None, 9, "prospective"),
            ("profile_overrides.models.generation.temperature", None, 0.4, "prospective"),
            ("profile_overrides.models.generation.top_p", None, 0.6, "prospective"),
            ("profile_overrides.models.generation.temperature", 0.4, None, "prospective"),
        ]
        events = await scalar(
            engine,
            "SELECT count(*) FROM events WHERE story_id = :s AND type = 'config_changed'",
            s=story,
        )
        assert events == len(changes)
        noop = await ok(await _override(client, story, nest(WINDOW, 9)))
        assert (noop["applied"], noop["config_revision"]) == (True, removed["config_revision"])
        assert len(await _story_changes(engine, story)) == len(changes)

        stale = refused(
            await _override(client, story, nest(WINDOW, 8), expected_revision=1),
            409,
            "revision_conflict",
        )
        assert (stale["details"]["current_revision"], stale["details"]["reason"]) == (
            3,
            "overrides_revision",
        )
        before = await configuration_rows(engine)
        key = f"quiet-{secrets.token_hex(6)}"
        held = await ok(await _override(client, story, SUMMARY_OFF, key))
        assert (held["applied"], held["impact"]["warnings"]) == (False, ["capabilities_lost"])
        assert await configuration_rows(engine) == before
        confirmed = await ok(await _override(client, story, SUMMARY_OFF, key, ignore_warnings=True))
        assert confirmed["result"]["capabilities"] == [
            c for c in LITE_CAPABILITIES if c != "rolling_summary"
        ]

        settled = await configuration_rows(engine)
        bad = refused(await _override(client, story, nest(WINDOW, "lots")), 422, "validation_error")
        assert bad["details"]["field"] == f"body.settings.{WINDOW}"
        refused(
            await _override(client, story, naming(undelivered_modules()[0])),
            422,
            "invalid_profile",
        )
        refused(await _override(client, story, "everything"), 422, "validation_error")
        assert await configuration_rows(engine) == settled
        window = PATCH_SCHEMA["properties"]["memory"]["anyOf"][0]["properties"]["rolling_summary"]
        assert {"type": "null"} in window["anyOf"]

        library = await lib.set_profile_overrides(
            RequestContext(principal=A),
            story,
            ProfileOverridesPatch(settings=nest(WINDOW, 7), ignore_warnings=True),
        )
        assert library.result is not None
        assert (
            library.result.overrides.settings["memory"]["rolling_summary"]["window_messages"] == 7
        )
        await ok(await client.post(f"/stories/{story}/archive", headers=headers()))
        refused(await _override(client, story, nest(WINDOW, 6)), 409, "story_archived")
        same = await ok(await _override(client, story, nest(WINDOW, 7)))
        assert same["applied"] is True  # nothing to change: allowed while archived


async def test_a_fork_keeps_the_overrides(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A fork copies its story (FR-STORE-15), its overrides of its profile included (FR-PROF-5):
    the fork runs under the same profile with the same overrides at the same revision, and its
    own later overrides leave its source's alone."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, actor = await _story(client)
        await ok(await _override(client, story, nest(WINDOW, 5)))
        await ok(await _play(client, story, actor))
        fork = await ok(
            await client.post(f"/stories/{story}/fork", json={"at_turn": 1}, headers=headers()), 201
        )
        forked = fork["story_id"]
        assert (await _effective(client, forked))["overrides"] == {
            "settings": nest(WINDOW, 5),
            "revision": 1,
        }
        await ok(await _override(client, forked, nest(WINDOW, 4)))
        assert (await _effective(client, story))["overrides"]["settings"] == nest(WINDOW, 5)


# -- a profile edit and its followers' overrides ----------------------------------------------


async def test_an_edit_reaches_each_follower_through_its_overrides(scratch: Space) -> None:
    """A profile edit's impact report gives each following story the change it sees through
    its own overrides (FR-PROF-5,7): turning the rolling summary off loses it for a follower
    without overrides, not for one whose overrides keep it on. Confirmed, both run on the new
    version, each with its own summary setting. An edit under which a follower's overrides
    could not run (a test-only rule makes pinned memories need the rolling summary, which the
    follower's overrides turn off, and the edit turns pinned memories on) is 422
    ``invalid_profile`` naming that story in ``details.stories``, confirmed or not, and nothing
    is written (FR-PROF-4)."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile = await _profile(client, {}, "Shared")
        plain, _ = await _story(client)
        kept, _ = await _story(client)
        for story in (plain, kept):
            await ok(await _assign(client, story, profile, ignore_warnings=True))
        await ok(await _override(client, kept, nest("memory.rolling_summary.enabled", True)))
        held = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={"name": "Shared", "settings": SUMMARY_OFF},
                headers=headers(),
            )
        )
        assert (held["applied"], held["impact"]["warnings"]) == (False, ["capabilities_lost"])
        entries = {e["story_id"]: e for e in held["impact"]["affected_stories"]}
        assert entries[plain]["capabilities_lost"] == ["rolling_summary"]
        assert entries[kept]["capabilities_lost"] == []
        assert "rolling_summary" in entries[kept]["capabilities_after"]
        done = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={"name": "Shared", "settings": SUMMARY_OFF, "ignore_warnings": True},
                headers=headers(),
            )
        )
        assert done["result"]["version"] == 2
        assert (await _effective(client, plain))["effective"]["memory"]["rolling_summary"][
            "enabled"
        ] is False
        assert (await _effective(client, kept))["effective"]["memory"]["rolling_summary"][
            "enabled"
        ] is True

        # Version 3: neither the summary nor pinned memories; the second follower overrides the
        # summary off too. Then, under the rule, version 4 (both on, the defaults') would leave
        # it with pinned memories and no summary.
        await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={
                    "name": "Shared",
                    "settings": merged(SUMMARY_OFF, PINS_OFF),
                    "ignore_warnings": True,
                },
                headers=headers(),
            )
        )
        await ok(await _override(client, kept, SUMMARY_OFF, ignore_warnings=True))
        with extra_rule(PINS_NEED_SUMMARY):
            before = await configuration_rows(scratch.db)
            for confirm in (False, True):
                error = refused(
                    await client.put(
                        f"{PROFILES}/{profile}",
                        json={"name": "Shared", "settings": {}, "ignore_warnings": confirm},
                        headers=headers(),
                    ),
                    422,
                    "invalid_profile",
                )
                assert error["details"]["stories"] == [kept]
                assert error["details"]["count"] == 1
                assert [m["module"] for m in error["details"]["missing_dependencies"]] == ["pins"]
            assert await configuration_rows(scratch.db) == before


# -- the deployment's profile defaults ---------------------------------------------------------


async def test_profile_defaults_are_the_bottom_layer(scratch: Space) -> None:
    """``profile_defaults`` (``PUT /admin/settings``, FR-PROF-5) is laid over the stored
    defaults: an object merges into an object, and any other value replaces, ``null`` included
    (``budgets.model_calls``: the formula). Each changed setting is one prospective row,
    ``profile_defaults.<path>``, under one revision. A profile that leaves a setting out follows
    the defaults' change at once, as its stories' configuration shows, while Lite, which names
    every setting, does not move. Defaults whose shape is wrong are 422 ``validation_error`` at
    their path; defaults naming an undelivered module, or under which a profile in use could not
    run (a test-only rule), are 422 ``invalid_profile`` (``details.profiles``); nothing is
    written. The read of every story states the defaults."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        sparse = await _profile(client, {}, "Sparse")
        story, _ = await _story(client)
        lite_story, _ = await _story(client)
        await ok(await _assign(client, story, sparse))
        start = int(await scalar(scratch.db, "SELECT config_revision FROM deployment_state"))
        put = await ok(
            await client.put(
                "/admin/settings",
                json={PROFILE_DEFAULTS: merged(nest(WINDOW, 14), nest("budgets.model_calls", 12))},
                headers=headers(),
            )
        )
        assert put["config_revision"] == start + 1
        assert put[PROFILE_DEFAULTS]["memory"]["rolling_summary"]["window_messages"] == 14
        assert put[PROFILE_DEFAULTS]["memory"]["rolling_summary"]["enabled"] is True  # kept
        changes = await rows(
            scratch.db,
            'SELECT setting, old, new, "class" FROM config_changes WHERE scope = '
            "'deployment' AND config_revision = :r ORDER BY setting",
            r=start + 1,
        )
        assert [tuple(c) for c in changes] == [
            ("profile_defaults.budgets.model_calls", None, 12, "prospective"),
            (f"profile_defaults.{WINDOW}", 30, 14, "prospective"),
        ]
        sparse_read = await _effective(client, story)
        assert sparse_read["deployment_defaults"]["budgets"]["model_calls"] == 12
        assert sparse_read["effective"]["memory"]["rolling_summary"]["window_messages"] == 14
        assert sparse_read["effective"]["budgets"]["model_calls"] == 12
        settings = await ok(await client.get(f"/stories/{story}/settings", headers=READ))
        assert settings["effective_budget"]["source"] == "deployment"
        lite_read = await _effective(client, lite_story)
        assert lite_read["effective"]["memory"]["rolling_summary"]["window_messages"] == 30
        formula = await ok(
            await client.put(
                "/admin/settings",
                json={PROFILE_DEFAULTS: nest("budgets.model_calls", None)},
                headers=headers(),
            )
        )
        assert formula[PROFILE_DEFAULTS]["budgets"]["model_calls"] is None

        settled = await configuration_rows(scratch.db)
        for bad, field in (
            (nest(WINDOW, 0), f"body.profile_defaults.{WINDOW}"),
            ({"memory": 3}, "body.profile_defaults.memory"),
            ("all", "body.profile_defaults"),
        ):
            error = refused(
                await client.put(
                    "/admin/settings", json={PROFILE_DEFAULTS: bad}, headers=headers()
                ),
                422,
                "validation_error",
            )
            assert error["details"]["field"] == field
        refused(
            await client.put(
                "/admin/settings",
                json={PROFILE_DEFAULTS: naming(undelivered_modules()[0])},
                headers=headers(),
            ),
            422,
            "invalid_profile",
        )
        assert await configuration_rows(scratch.db) == settled
        pinned = await _profile(client, nest("pins.enabled", True), "Pinned")
        settled = await configuration_rows(scratch.db)
        with extra_rule(PINS_NEED_SUMMARY):
            refused(  # the defaults themselves cannot run: pinned memories without the summary
                await client.put(
                    "/admin/settings", json={PROFILE_DEFAULTS: SUMMARY_OFF}, headers=headers()
                ),
                422,
                "invalid_profile",
            )
            error = refused(
                await client.put(
                    "/admin/settings",
                    json={PROFILE_DEFAULTS: merged(SUMMARY_OFF, PINS_OFF)},
                    headers=headers(),
                ),
                422,
                "invalid_profile",
            )
            # The profile that turns pinned memories on, leaving the summary to the defaults,
            # could not run; the sparse one (both from the defaults) and Lite (both named) could.
            assert error["details"]["profiles"] == [f"{pinned}@1"]
            assert error["details"]["count"] == 1
        assert await configuration_rows(scratch.db) == settled


# -- the switches and dials of a turn ---------------------------------------------------------


async def test_the_configuration_shapes_the_turn(scratch: Space) -> None:
    """The configuration a turn captures shapes it (FR-LITE-1,2,3; FR-PROF-5). With the rolling
    summary off a story's prompt carries no summary, keeps only the latest
    ``window_messages`` rows, and no summary job is ever queued; with it on (the complement) the
    same story gets its job and its summary. With pinned memories off the prompt carries none of
    them. A story may hold no more pinned memories than its ``pins.max_pinned``, nor than the
    deployment's ``max_pinned_memories`` (``details.cap`` names the smaller). A request's budget
    above the story's call cap (its ``budgets.model_calls``) is 422 ``validation_error``."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        stub = lib.models.stub
        story, actor = await _story(client, settings={"lite_summary_chunk": 2})
        await ok(
            await _override(
                client, story, merged(SUMMARY_OFF, nest(WINDOW, 2)), ignore_warnings=True
            )
        )
        await ok(
            await client.put(
                f"/stories/{story}/pinned-memories",
                json={"items": [{"text": "PIN: the bell rings at noon."}]},
                headers=headers(),
            )
        )
        for n in range(4):
            await ok(await _play(client, story, actor, say=f"line {n}"))
        assert await summary_jobs(scratch.db, story) == []
        async with scratch.db.connect() as conn:
            state = await summary.summary_state(conn, story)
        assert (state.settings.enabled, state.needs_work) == (False, False)
        stub.reset()
        await ok(await _play(client, story, actor, say="line 4"))
        (call,) = generation_calls(stub)
        system = call.messages[0].content
        assert "Earlier in the story" not in system
        assert [m.role for m in call.messages[1:-1]] == ["user", "assistant"]  # two rows
        assert "PIN: the bell rings at noon." in system

        summary_on = {"memory": {"rolling_summary": {"enabled": None}}}
        await ok(await _override(client, story, merged(PINS_OFF, summary_on), ignore_warnings=True))
        await ok(await _play(client, story, actor, say="line 5"))
        assert len(await summary_jobs(scratch.db, story)) == 1  # the summary is on again
        await run_due_jobs(scratch.db, lib.models, story)
        stub.reset()
        await ok(await _play(client, story, actor, say="line 6"))
        (call,) = generation_calls(stub)
        system = call.messages[0].content
        assert "Earlier in the story" in system
        assert "PIN: the bell rings at noon." not in system

        # Off again: the summary the story has is left out, the window is its last rows, and no
        # job is queued (one already queued finds nothing to do).
        await ok(await _override(client, story, SUMMARY_OFF, ignore_warnings=True))
        await run_due_jobs(scratch.db, lib.models, story)
        jobs = len(await summary_jobs(scratch.db, story))
        stub.reset()
        await ok(await _play(client, story, actor, say="line 7"))
        (call,) = generation_calls(stub)
        system = call.messages[0].content
        assert "Earlier in the story" not in system
        assert [m.role for m in call.messages[1:-1]] == ["user", "assistant"]  # two rows
        assert len(await summary_jobs(scratch.db, story)) == jobs

        await ok(await _override(client, story, nest("pins.max_pinned", 2)))
        three = {"items": [{"text": f"pin {n}"} for n in range(3)]}
        error = refused(
            await client.put(f"/stories/{story}/pinned-memories", json=three, headers=headers()),
            422,
            "validation_error",
        )
        assert (error["details"]["cap"], error["details"]["limit"]) == ("pins.max_pinned", 2)
        await ok(
            await client.put("/admin/settings", json={"max_pinned_memories": 1}, headers=headers())
        )
        error = refused(
            await client.put(f"/stories/{story}/pinned-memories", json=three, headers=headers()),
            422,
            "validation_error",
        )
        assert (error["details"]["cap"], error["details"]["limit"]) == ("max_pinned_memories", 1)
        await ok(
            await client.put("/admin/settings", json={"max_pinned_memories": 50}, headers=headers())
        )

        await ok(await _override(client, story, nest("budgets.model_calls", 4)))
        settings = await ok(await client.get(f"/stories/{story}/settings", headers=READ))
        assert (
            settings["effective_budget"]["max_model_calls_per_turn"],
            settings["effective_budget"]["source"],
        ) == (4, "story")
        over = refused(
            await _play(client, story, actor, budget={"max_calls": 5}), 422, "validation_error"
        )
        assert over["details"]["field"] == "body.budget.max_calls"
        quick = await _profile(client, nest("budgets.model_calls", 6), "Capped")
        await ok(await _override(client, story, {"budgets": None}))
        await ok(await _assign(client, story, quick))
        settings = await ok(await client.get(f"/stories/{story}/settings", headers=READ))
        assert (
            settings["effective_budget"]["max_model_calls_per_turn"],
            settings["effective_budget"]["source"],
        ) == (6, "profile")


# -- the cache ---------------------------------------------------------------------------------


async def test_the_cache_follows_every_layer(scratch: Space) -> None:
    """With the resolution cache on (``Settings.effective_config_cache``, FR-PROF-5) a story's
    configuration is resolved once and then served from the cache, and a change of any layer
    is seen at the next read: the story's overrides, its profile's new version, the
    deployment's defaults, a reassignment. Turned off, nothing is kept."""
    settings = scratch.settings.model_copy(update={"effective_config_cache": True})
    app = create_app(settings)
    app.state.epistrel.bind(scratch.db)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert effective.CACHE.enabled
            profile = await _profile(client, {}, "Cached")
            story, _ = await _story(client)
            await ok(await _assign(client, story, profile))

            async def window() -> int:
                read = await _effective(client, story)
                found: int = read["effective"]["memory"]["rolling_summary"]["window_messages"]
                return found

            assert await window() == 30
            hits = effective.CACHE.hits
            assert await window() == 30
            assert effective.CACHE.hits == hits + 1
            await ok(await _override(client, story, nest(WINDOW, 11)))
            assert await window() == 11
            await ok(await _override(client, story, {"memory": None}))
            await ok(
                await client.put(
                    f"{PROFILES}/{profile}",
                    json={"name": "Cached", "settings": nest(WINDOW, 12)},
                    headers=headers(),
                )
            )
            assert await window() == 12
            await ok(
                await client.put(
                    f"{PROFILES}/{profile}",
                    json={"name": "Cached", "settings": {}},
                    headers=headers(),
                )
            )
            await ok(
                await client.put(
                    "/admin/settings",
                    json={PROFILE_DEFAULTS: nest(WINDOW, 13)},
                    headers=headers(),
                )
            )
            assert await window() == 13
            await ok(await _assign(client, story, LITE_PRESET_ID))
            assert await window() == 30
    finally:
        effective.configure_cache(False)
    assert not effective.CACHE.enabled
    before = effective.CACHE.hits
    async with scratch.db.connect() as conn:
        await effective.effective_configuration(conn, story)
        await effective.effective_configuration(conn, story)
    assert effective.CACHE.hits == before


# -- migration 0030 ----------------------------------------------------------------------------


def test_migration_moves_the_m2_dials(scratch_database: str) -> None:
    """0030 carries M2's stored engine dials over (FR-PROF-5): a story's
    ``generation_settings``, window, summary cap, call cap, deadline (seconds to milliseconds)
    and polish budget become its overrides at revision 1, and leave its settings, while the
    settings that stayed are kept; a story that holds only M2's defaults gets no overrides. The
    deployment's changed Lite defaults and call cap become its ``profile_defaults`` over the
    Lite seed, and leave its settings document; one at M2's default does not. A fresh database
    gets the Lite seed. Downgrading puts the values back under their M2 names."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0029")
    sync = make_sync_engine(scratch_database)
    tuned, plain = "0000000000000000000000STR1", "0000000000000000000000STR2"
    m2_story = {
        "turn_concurrency": "queue",
        "regenerate_retry_budget": 3,
        "turn_deadline": 12.5,
        "max_model_calls_per_turn": 7,
        "generation_settings": {"temperature": 0.4, "top_p": None, "length": "short"},
        "lite_window_messages": 8,
        "lite_summary_chunk": 4,
        "lite_summary_max_tokens": 900,
        "guardrail_retry_budget": 1,
    }
    m2_defaults = {
        "turn_deadline": 45.0,
        "guardrail_retry_budget": 2,
        "generation_settings": None,
        "lite_window_messages": None,
        "max_model_calls_per_turn": None,
        "lite_summary_max_tokens": None,
    }
    try:
        with sync.begin() as conn:
            for story, document in ((tuned, m2_story), (plain, m2_defaults)):
                conn.execute(
                    text(
                        "INSERT INTO stories (id, owner_principal, settings, engine_profile_id) "
                        "VALUES (:s, 'p-1', CAST(:d AS jsonb), :p)"
                    ),
                    {"s": story, "d": json_text(document), "p": LITE_PRESET_ID},
                )
            conn.execute(
                text("UPDATE deployment_state SET settings = CAST(:d AS jsonb)"),
                {
                    "d": json_text(
                        {
                            "lite_generation_defaults": {
                                "temperature": 0.5,
                                "top_p": 0.95,
                                "length": "medium",
                            },
                            "lite_window_messages": 30,
                            "lite_summary_chunk": 6,
                            "lite_summary_max_tokens": 2_000,
                            "max_model_calls_per_turn": 20,
                            "turn_queue_max": 4,
                        }
                    )
                },
            )
        command.upgrade(cfg, "0030")
        with sync.connect() as conn:
            overrides: dict[str, Any] = {
                str(r.story_id): r.settings
                for r in conn.execute(
                    text("SELECT story_id, settings FROM story_profile_overrides")
                ).all()
            }
            assert overrides == {
                tuned: {
                    "memory": {
                        "rolling_summary": {
                            "window_messages": 8,
                            "compress_threshold_tokens": 900,
                        }
                    },
                    "budgets": {"model_calls": 7, "deadline_ms": 12_500},
                    "repair": {"polish": {"max_regenerations": 1}},
                    "models": {"generation": {"temperature": 0.4, "response_length": "short"}},
                }
            }
            assert (
                conn.execute(text("SELECT revision FROM story_profile_overrides")).scalar_one() == 1
            )
            kept = conn.execute(
                text("SELECT settings FROM stories WHERE id = :s"), {"s": tuned}
            ).scalar_one()
            assert kept == {
                "turn_concurrency": "queue",
                "regenerate_retry_budget": 3,
                "lite_summary_chunk": 4,
            }
            assert (
                conn.execute(
                    text("SELECT settings FROM stories WHERE id = :s"), {"s": plain}
                ).scalar_one()
                == {}
            )
            defaults, document = conn.execute(
                text("SELECT profile_defaults, settings FROM deployment_state")
            ).one()
            expected = lite_settings()
            expected["models"]["generation"]["temperature"] = 0.5
            expected["budgets"]["model_calls"] = 20
            assert defaults == expected
            assert document == {"lite_summary_chunk": 6, "turn_queue_max": 4}
        command.downgrade(cfg, "0029")
        with sync.connect() as conn:
            assert (
                conn.execute(text("SELECT to_regclass('story_profile_overrides')")).scalar() is None
            )
            back = conn.execute(
                text("SELECT settings FROM stories WHERE id = :s"), {"s": tuned}
            ).scalar_one()
            assert (back["turn_deadline"], back["max_model_calls_per_turn"]) == (12.5, 7)
            assert back["generation_settings"] == {
                "temperature": 0.4,
                "top_p": None,
                "length": "short",
            }
            restored = conn.execute(text("SELECT settings FROM deployment_state")).scalar_one()
            assert restored["max_model_calls_per_turn"] == 20
            assert restored["lite_generation_defaults"]["temperature"] == 0.5
        command.upgrade(cfg, "head")
        with sync.connect() as conn:
            assert conn.execute(text("SELECT profile_defaults FROM deployment_state")).scalar_one()
    finally:
        sync.dispose()


def json_text(value: Any) -> str:
    return json.dumps(value)
