"""U-28, C-16 and C-17 (docs/04), their M3 parts for engine profiles (#106, #109), and the
detailed tests behind them.

Every U-28 (M3) and C-16 (M3) part lives in this file, because traceability counts a test ID per
milestone in one file: #113's U-28 and C-16 parts join it. So does C-16's M4 interim slice
(#109's ``test_c_16__upgrade_unavailable``), and any later C-16 (M4) part. The C-17 (M3) parts
are here too. The I-21 part (profiles' owners) is ``test_i_21__profiles`` in
``tests/integration/test_i_21.py``; C-18 (#109) is in ``tests/integration/test_two_stage.py``.

- ``test_u_28__schema``: the schema's three tags on every setting, the published schema holding
  only what is delivered, the Lite preset validating, undelivered names refused, and the
  dependency graph declared for every clause of the requirement.
- ``test_c_16__crud``: create, list, read, edit (a new version), copy, validate and delete over
  REST; presets edited and copied like any profile; the default cannot be deleted; a story is
  created under the default; deleting a profile a story uses warns, hides it and keeps the
  story running; the retention sweep purges it once no story uses it.
- ``test_c_16__two_stage`` (#109): a profile edit that its following stories would feel is
  held for confirmation, then applied with ``ignore_warnings``; one they would not feel applies
  at once; settings that cannot run are refused even when confirmed; the impact preview; and a
  creation held until confirmed, through the same flow.
- ``test_c_16__upgrade_unavailable`` (#109; M4's slot, interim until M7): a data-producing
  edit cannot reach a story with history, which keeps its version, while one without turns
  upgrades.
- ``test_u_28__effective`` (#113): the effective configuration, deployment defaults → profile
  version → story overrides, validated as a whole; an invalid override refused and not stored;
  the overrides kept across versions; a behavioral edit followed at the next turn with no job;
  a turn in flight finishing under the configuration it captured.
- ``test_c_16__follow_latest`` (#113): stories assigned a profile through the API follow its
  latest version; a behavioral edit is a configuration change that applies at each follower's
  next turn boundary without a warning or a job; every respond response names the profile and
  version.
- ``test_c_17__profiles``: a pairwise covering array over the delivered modules' settings; each
  configuration is accepted and a story runs a turn under it, or it is refused by validation.
- ``test_c_17__dependencies``: ``missing_dependencies`` end to end: the undelivered path for
  real, and the delivered path through a test-only rule (``epistrel.testing.profiles``).

The C-16 and C-17 parts change the deployment's default and the Lite preset, so they run on a
scratch database of their own. Module and level names come from the schema's registry
(``tests/profile_support.py``): a part may not name a later milestone's capability.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextvars import Context
from dataclasses import dataclass
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from alembic import command
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel import (
    InvalidProfile,
    ProfileCopy,
    ProfileCreate,
    ProfileUpdate,
    ProfileValidate,
    RequestContext,
)
from epistrel.config import Settings
from epistrel.core import fork as forking
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.core.errors import InvalidRequest
from epistrel.core.profiles.effective import Configuration, effective_configuration
from epistrel.core.profiles.estimates import CALL_LATENCY_MS
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.impact import (
    CAMEL_CASE,
    Recommendation,
    TwoStageResult,
    creation_report,
    two_stage,
)
from epistrel.core.profiles.schema import (
    BEHAVIORAL,
    DATA_PRODUCING,
    DEPENDENCIES,
    FR_PROF_4,
    LEAF_PATHS,
    PROSPECTIVE,
    RELEVANT,
    DependencyRule,
    ProfileSettings,
    Purposes,
    Quality,
    Requirement,
    Tag,
    delivered_schema,
    delivered_tags,
    exposed,
    missing_tags,
    setting_tags,
)
from epistrel.core.profiles.validate import (
    ProfileValidation,
    RequiredModule,
    UndeliveredSetting,
    missing_dependencies,
    normalised,
    parse_settings,
    require_valid,
    validate_profile,
)
from epistrel.core.seeds.profiles import LITE_PRESET_ID, lite_settings
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.stories import delete_story
from epistrel.orchestrator.jobs.handlers import retention
from epistrel.orchestrator.models.registry import DELIVERED as DELIVERED_PURPOSES
from epistrel.orchestrator.models.registry import RESERVED as RESERVED_PURPOSES
from epistrel.testing import pauses
from epistrel.testing.profiles import TEST_CLAUSE, data_producing, extra_rule
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.conftest import alembic_config
from tests.event_support import rows, scalar
from tests.policy_support import AUTHOR, story_item
from tests.profile_support import (
    EDITED,
    LITE_CAPABILITIES,
    POLISH,
    PROFILES,
    REDERIVATION,
    SETTINGS,
    UNAVAILABLE_WARNINGS,
    UNCERTIFIED,
    configuration_rows,
    covered_pairs,
    covering_array,
    documented_rules,
    merged,
    naming,
    nest,
    profile_body,
    requirement_text,
    satisfying,
    undelivered_levels,
    undelivered_modules,
    violating,
)
from tests.respond_support import body as respond_body
from tests.respond_support import generation_calls, respond_client
from tests.scenario_support import scenario as catalog_scenario
from tests.story_support import story_body
from tests.summary_support import summary_jobs

READ = bearer()
A = "p-profiles-a"
B = "p-profiles-b"
PURPOSES = Purposes.of(list(DELIVERED_PURPOSES), list(RESERVED_PURPOSES))


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
    """An app's settings and its database: the shared one, or a scratch one."""

    settings: Settings
    db: AsyncEngine


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def scratch(pg_url: str) -> AsyncIterator[Space]:
    """A database of the test's own, migrated to head (the default profile and the Lite preset
    change there, never in the shared database)."""
    with migrated_database(pg_url, f"epistrel_profiles_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        try:
            yield Space(settings, db)
        finally:
            await db.dispose()


async def _story(client: httpx.AsyncClient, principal: str = A) -> tuple[str, str]:
    """A new two-character story: its id and its player character's id."""
    created = await ok(
        await client.post("/stories", json=story_body("Wren", "Ada"), headers=headers(principal)),
        201,
    )
    return created["story_id"], created["characters"][0]["id"]


async def _profile_of(db: AsyncEngine, story_id: str) -> str:
    found = await scalar(db, "SELECT engine_profile_id FROM stories WHERE id = :s", s=story_id)
    return str(found)


async def _default(client: httpx.AsyncClient, profile_id: str) -> dict[str, Any]:
    response = await client.put(
        "/admin/settings",
        json={"default_engine_profile_id": profile_id},
        headers=headers(A),
    )
    return await ok(response)


async def _effective(client: httpx.AsyncClient, story_id: str) -> dict[str, Any]:
    return await ok(await client.get(f"/stories/{story_id}/effective-config", headers=READ))


async def _play(client: httpx.AsyncClient, story_id: str, actor: str) -> dict[str, Any]:
    played = await client.post(
        f"/stories/{story_id}/respond", json=respond_body(actor), headers=headers(A)
    )
    return await ok(played)


def _schema_leaves(node: dict[str, Any], prefix: str = "") -> set[str]:
    """Every setting a published JSON Schema holds, by dotted path."""
    found: set[str] = set()
    for name, child in node.get("properties", {}).items():
        path = f"{prefix}{name}"
        if "properties" in child:
            found |= _schema_leaves(child, f"{path}.")
        else:
            found.add(path)
    return found


class _Half(BaseModel):
    whole: Annotated[int, BEHAVIORAL, PROSPECTIVE, RELEVANT] = 1
    half: Annotated[int, BEHAVIORAL, PROSPECTIVE] = 2


class _Untagged(BaseModel):
    """A fixture schema: one leaf with no tag at all, one with two of a kind, and one inside an
    inner model without its quality tag."""

    bare: int = 0
    doubled: Annotated[bool, BEHAVIORAL, DATA_PRODUCING, PROSPECTIVE, RELEVANT] = False
    inner: _Half = Field(default_factory=_Half)


# -- U-28 --------------------------------------------------------------------------------------


@pytest.mark.part("M3", "FR-PROF-2,3,4")
async def test_u_28__schema(engine: AsyncEngine) -> None:
    """Every setting of the profile schema carries its three tags: behavioral or
    data-producing, interpretive or prospective, quality-relevant or not (complement: a fixture
    schema's settings that lack a tag, or carry two of a kind, are each reported). Every setting
    delivered in M3 is prospective, and pinned memories are the quality-irrelevant ones.

    The published schema holds only what is delivered: its settings are exactly the delivered
    ones, the pipeline offers only ``lite``, and no undelivered module's setting is in it.

    The Lite preset validates, as shipped and as stored: owned by ``system``, a preset, the
    deployment's default, at version 1 with the shipped settings: the ``lite`` pipeline, a
    30-message window, a 6,000-token threshold, and budgets of no call cap of its own (the
    NFR-PERF-6 formula) and 45 s.

    A document that switches on any undelivered module, or picks the undelivered level of a
    delivered one, is refused naming it (422 ``invalid_profile``); the rolling summary switched
    off under ``lite`` is allowed.

    The dependency graph is declared for every clause of the requirement, each quoted verbatim
    from docs/03; the documented rule list equals the declared rules, one by one. Each rule
    fires on settings that switch its module on without what it requires, and does not once
    those are on (complement), whether its modules are delivered or not."""
    # -- three tags on every setting
    assert missing_tags() == {}
    assert missing_tags(_Untagged) == {
        "bare": ("Tag", "ConfigClass", "Quality"),
        "doubled": ("Tag",),
        "inner.half": ("Quality",),
    }
    assert set(setting_tags()) == set(LEAF_PATHS)
    tags = delivered_tags()
    assert {t.change_class for t in tags.values()} == {ConfigClass.prospective}
    irrelevant = sorted(path for path, t in tags.items() if t.quality is Quality.irrelevant)
    assert irrelevant == ["pins.enabled", "pins.max_pinned"]
    assert tags["pipeline"].tag is Tag.data_producing
    assert tags["budgets.model_calls"].tag is Tag.behavioral

    # -- the published schema: delivered modules and levels only
    published = delivered_schema()
    assert _schema_leaves(published) == {path for path in LEAF_PATHS if exposed(path)}
    assert published["properties"]["pipeline"]["enum"] == ["lite"]
    for module in undelivered_modules():
        assert module.switch not in _schema_leaves(published), module.name

    # -- the Lite preset validates, as shipped and as stored
    shipped = lite_settings()
    clean = ProfileValidation(valid=True, missing_dependencies=[], undelivered=[])
    assert validate_profile(shipped, purposes=PURPOSES) == clean
    assert normalised(parse_settings(shipped)) == shipped
    assert shipped["pipeline"] == "lite"
    assert shipped["memory"]["rolling_summary"] == {
        "enabled": True,
        "window_messages": 30,
        "compress_threshold_tokens": 6_000,
    }
    assert shipped["budgets"] == {"model_calls": None, "deadline_ms": 45_000}
    assert set(shipped["models"]["purposes"]) == set(DELIVERED_PURPOSES)
    (stored,) = await rows(
        engine,
        "SELECT p.name, p.owner_principal, p.preset, v.settings, v.created_by, "
        "d.default_engine_profile_id FROM engine_profiles p "
        "JOIN engine_profile_versions v ON v.profile_id = p.id AND v.version = 1 "
        "CROSS JOIN deployment_state d WHERE p.id = :p",
        p=LITE_PRESET_ID,
    )
    assert tuple(stored) == ("Lite", "system", True, shipped, "system", LITE_PRESET_ID)

    # -- an undelivered module, or level, is refused naming it
    for module in undelivered_modules():
        found = validate_profile(naming(module), purposes=PURPOSES)
        assert [(u.module, u.setting) for u in found.undelivered] == [(module.name, module.switch)]
        with pytest.raises(InvalidProfile) as caught:
            require_valid(naming(module), purposes=PURPOSES)
        assert caught.value.http_status == 422
        assert module.name in {u["module"] for u in caught.value.details["undelivered"]}
    for module, level in undelivered_levels():
        found = validate_profile(naming(module, level), purposes=PURPOSES)
        assert found.undelivered == [
            UndeliveredSetting(module=module.name, level=level, setting=module.switch or "")
        ]
    summary_off = nest("memory.rolling_summary.enabled", False)
    assert validate_profile(merged(shipped, summary_off), purposes=PURPOSES) == clean

    # -- the dependency graph: every clause, the documented list, each rule firing
    spec = requirement_text()
    assert [clause.text for clause in FR_PROF_4 if clause.text not in spec] == []
    assert {rule.clause for rule in DEPENDENCIES} == {clause.key for clause in FR_PROF_4}
    assert documented_rules() == [rule.describe() for rule in DEPENDENCIES]
    assert len(DEPENDENCIES) == 26
    for rule in DEPENDENCIES:
        expected = [RequiredModule(module=r.module, levels=list(r.levels)) for r in rule.requires]
        fired = missing_dependencies(parse_settings(violating(rule)))
        assert any(f.module == rule.module and f.requires == expected for f in fired), (
            rule.describe()
        )
        met = missing_dependencies(parse_settings(satisfying(rule)))
        assert not any(f.module == rule.module and f.requires == expected for f in met), (
            rule.describe()
        )


# -- C-16 --------------------------------------------------------------------------------------


@pytest.mark.part("M3", "FR-PROF-1; FR-API-6")
async def test_c_16__crud(scratch: Space) -> None:
    """Over REST: a profile is created at version 1 (owned by its creator), read, listed
    (newest first, the Lite preset included, narrowed by owner only when asked), edited into
    version 2 (a name-only edit makes no version), read at version 1, listed by version, copied
    (by another principal, who owns the copy; at a named version too) and validated (its latest
    version, and a dry run that names an undelivered level: 422 ``invalid_profile``). Presets are
    ordinary profiles: Lite is edited into its version 2, still a preset owned by ``system``,
    and copied into a plain profile. Deleting the deployment's default is 409 (``is_default``)
    and changes nothing.

    A story created without a profile runs under the deployment's default: under the user
    profile once that is the default, and under a copy of Lite once that is (complement). Lite,
    no longer the default, is deleted like any profile. Deleting the user profile while that
    story uses it answers 200 with a warning naming the story; the profile leaves the listing
    (``include_deleted`` shows it), still reads and copies, cannot be edited or made the default
    (422 ``profile_deleted``), and the story still runs under it: a turn is played. The
    retention sweep purges Lite, which no story uses, and keeps the user profile while the story
    uses it; once the story is deleted, the sweep purges it with its versions, recording its
    deletion by ``system``.

    Complement asserted: a valid edit to a user profile succeeds, so the validator does not
    refuse everything."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        revision = int(await scalar(scratch.db, "SELECT config_revision FROM deployment_state"))

        # -- create, read, list
        created = await ok(
            await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201
        )
        profile = created["id"]
        assert (created["owner_principal"], created["preset"], created["deleted_at"]) == (
            A,
            False,
            None,
        )
        assert (created["version"], created["current_version"]) == (1, 1)
        assert (created["settings"], created["config_revision"]) == (SETTINGS, revision)
        read = await ok(await client.get(f"{PROFILES}/{profile}", headers=READ))
        assert read == {
            k: v for k, v in created.items() if k not in ("config_revision", "idempotency_key")
        }
        listed = await ok(await client.get(PROFILES, headers=READ))
        assert [item["id"] for item in listed["items"]] == [profile, LITE_PRESET_ID]
        assert listed["items"][1]["preset"] is True
        mine = await ok(await client.get(PROFILES, params={"owner": A}, headers=READ))
        assert [item["id"] for item in mine["items"]] == [profile]
        shipped = await ok(await client.get(PROFILES, params={"owner": "system"}, headers=READ))
        assert [item["id"] for item in shipped["items"]] == [LITE_PRESET_ID]

        # -- edit into version 2; a name-only edit makes none (no story follows the profile, so
        # each edit applies at once: #109's two stages)
        edited = (
            await ok(
                await client.put(
                    f"{PROFILES}/{profile}",
                    json={"name": "Brisk Lite", "settings": EDITED},
                    headers=headers(B),
                )
            )
        )["result"]
        assert (edited["version"], edited["current_version"], edited["settings"]) == (2, 2, EDITED)
        renamed = (
            await ok(
                await client.put(
                    f"{PROFILES}/{profile}",
                    json={"name": "Renamed", "settings": EDITED},
                    headers=headers(A),
                )
            )
        )["result"]
        assert (renamed["name"], renamed["version"]) == ("Renamed", 2)
        first = await ok(
            await client.get(f"{PROFILES}/{profile}", params={"version": 1}, headers=READ)
        )
        assert (first["version"], first["current_version"], first["settings"]) == (1, 2, SETTINGS)
        versions = await ok(await client.get(f"{PROFILES}/{profile}/versions", headers=READ))
        assert [(v["version"], v["created_by"]) for v in versions["items"]] == [(1, A), (2, B)]

        # -- copy, at the latest version and at version 1
        copied = await ok(
            await client.post(
                f"{PROFILES}/{profile}/copy", json={"name": "Mine"}, headers=headers(B)
            ),
            201,
        )
        assert (copied["owner_principal"], copied["version"], copied["preset"]) == (B, 1, False)
        assert (copied["name"], copied["settings"]) == ("Mine", EDITED)
        older = await ok(
            await client.post(
                f"{PROFILES}/{profile}/copy",
                params={"version": 1},
                json={"name": "Old"},
                headers=headers(B),
            ),
            201,
        )
        assert older["settings"] == SETTINGS

        # -- validate: the latest version, and a dry run naming an undelivered level
        valid = await ok(
            await client.post(f"{PROFILES}/{profile}/validate", json={}, headers=headers(A))
        )
        assert valid == {"valid": True, "missing_dependencies": [], "undelivered": []}
        dry = await client.post(
            f"{PROFILES}/{profile}/validate",
            json={"settings": {"pipeline": "structured"}},
            headers=headers(A),
        )
        error = refused(dry, 422, "invalid_profile")
        assert error["details"]["undelivered"] == [
            {"module": "pipeline", "level": "structured", "setting": "pipeline"}
        ]

        # -- presets are ordinary profiles: edited and copied like any other
        lite = merged(lite_settings(), nest("memory.rolling_summary.window_messages", 40))
        lite_v2 = (
            await ok(
                await client.put(
                    f"{PROFILES}/{LITE_PRESET_ID}",
                    json={"name": "Lite", "settings": lite},
                    headers=headers(A),
                )
            )
        )["result"]
        assert (lite_v2["version"], lite_v2["preset"], lite_v2["owner_principal"]) == (
            2,
            True,
            "system",
        )
        lite_copy = await ok(
            await client.post(
                f"{PROFILES}/{LITE_PRESET_ID}/copy", json={"name": "My Lite"}, headers=headers(A)
            ),
            201,
        )
        assert (lite_copy["preset"], lite_copy["owner_principal"], lite_copy["settings"]) == (
            False,
            A,
            lite,
        )
        default_refused = refused(
            await client.delete(f"{PROFILES}/{LITE_PRESET_ID}", headers=headers(A)),
            409,
            "validation_error",
        )
        assert default_refused["details"] == {"id": LITE_PRESET_ID, "reason": "is_default"}
        still = await ok(await client.get(f"{PROFILES}/{LITE_PRESET_ID}", headers=READ))
        assert still["deleted_at"] is None

        # -- a story created without a profile runs under the deployment's default
        assert (await _default(client, profile))["default_engine_profile_id"] == profile
        story, actor = await _story(client)
        assert await _profile_of(scratch.db, story) == profile
        refused(
            await client.delete(f"{PROFILES}/{profile}", headers=headers(A)),
            409,
            "validation_error",
        )
        await _default(client, lite_copy["id"])
        other, _ = await _story(client)
        assert await _profile_of(scratch.db, other) == lite_copy["id"]
        # Lite is no longer the default: a preset is then deleted like any other profile.
        lite_gone = await ok(
            await client.delete(f"{PROFILES}/{LITE_PRESET_ID}", headers=headers(A))
        )
        assert (lite_gone["warnings"], lite_gone["stories_using"]) == ([], 0)

        # -- delete while the story uses it: warned, hidden, unassignable, still serving
        deleted = await ok(await client.delete(f"{PROFILES}/{profile}", headers=headers(B)))
        assert deleted["id"] == profile
        assert deleted["deleted_at"] is not None
        assert deleted["warnings"] == [
            {"kind": "profile_in_use", "story_id": story, "scenario_id": None}
        ]
        assert (deleted["stories_using"], deleted["scenarios_recommending"]) == (1, 0)
        listed = await ok(await client.get(PROFILES, headers=READ))
        assert profile not in [item["id"] for item in listed["items"]]
        everything = await ok(
            await client.get(PROFILES, params={"include_deleted": "true"}, headers=READ)
        )
        assert profile in [item["id"] for item in everything["items"]]
        gone = await ok(await client.get(f"{PROFILES}/{profile}", headers=READ))
        assert (gone["deleted_at"], gone["version"]) == (deleted["deleted_at"], 2)
        edit = await client.put(
            f"{PROFILES}/{profile}", json={"name": "Again", "settings": {}}, headers=headers(A)
        )
        assert refused(edit, 422, "profile_deleted")["details"]["reason"] == "deleted"
        rescued = await ok(
            await client.post(
                f"{PROFILES}/{profile}/copy", json={"name": "Rescued"}, headers=headers(A)
            ),
            201,
        )
        assert (rescued["deleted_at"], rescued["settings"]) == (None, EDITED)
        assign = await client.put(
            "/admin/settings", json={"default_engine_profile_id": profile}, headers=headers(A)
        )
        assert refused(assign, 422, "profile_deleted")["details"]["field"] == (
            "body.default_engine_profile_id"
        )
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(actor), headers=headers(A)
        )
        assert (await ok(played))["message"]["text"]
        assert await _profile_of(scratch.db, story) == profile

        # -- the sweep purges a deleted profile once no story uses it
        kept = await retention.sweep_retention(scratch.db)
        assert kept["engine_profiles"] == 1  # Lite: deleted, and no story uses it
        refused(await client.get(f"{PROFILES}/{LITE_PRESET_ID}", headers=READ), 404, "not_found")
        await ok(await client.get(f"{PROFILES}/{profile}", headers=READ))
        removed = await client.delete(f"/stories/{story}", headers=headers(A))
        assert removed.status_code == 204, removed.text
        purged = await retention.sweep_retention(scratch.db)
        assert purged["engine_profiles"] == 1
        refused(await client.get(f"{PROFILES}/{profile}", headers=READ), 404, "not_found")
        left = await scalar(
            scratch.db,
            "SELECT count(*) FROM engine_profile_versions WHERE profile_id = :p",
            p=profile,
        )
        assert left == 0
        events = await rows(
            scratch.db,
            "SELECT kind, acting_principal FROM ownership_events WHERE entity_id = :p",
            p=profile,
        )
        assert [tuple(e) for e in events] == [("deleted", "system")]


@pytest.mark.part("M3", "FR-PROF-7; FR-SCN-2")
async def test_c_16__two_stage(scratch: Space) -> None:
    """A profile edit runs in two stages (FR-PROF-7). Two stories follow a profile, one of them
    with a played turn. Turning the rolling summary off is answered 200 with ``applied: false``
    and no ``result``: the impact report lists both stories by id, each losing the rolling
    summary, with its fingerprint before (the one its effective configuration states) and a new
    one after, the capabilities it would run with (no rolling summary) and its quality status,
    and warns of the lost capability; the report's own fingerprints and capabilities are null,
    as it may list many stories. Nothing changed: the profiles, versions, stories, operations,
    keys and configuration rows compare equal, so the ``Idempotency-Key`` was not kept. Sent
    again under that key with ``ignoreWarnings: true``, the edit applies: version 2 in
    ``result``, the same report, both stories running on version 2 without the rolling summary
    under the fingerprint the report gave; a retry replays the answer. An edit the stories
    would not feel as a lost or gained capability (a temperature) applies at once, with a new
    fingerprint. Settings that cannot run are refused 422 ``invalid_profile`` even with
    ``ignore_warnings``, and nothing changes; a preview of them is refused the same way. So is a
    confirmed edit sent without the service credential (401). ``ignore_warnings`` and
    ``ignoreWarnings`` with different values are 422.

    The impact preview of assigning Lite to a story states the one-story report with the
    fingerprints and the capabilities at the top too, and writes nothing (no key is echoed). A
    story creation whose profile lacks a capability its scenario recommends goes through the
    same flow (#119 sends ``POST /stories`` through it): held, with the recommendation unmet,
    the new story's capabilities stated and nothing created, until ``ignore_warnings``."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        await _default(client, profile)
        played, actor = await _story(client)
        await _play(client, played, actor)
        idle, _ = await _story(client)
        await _default(client, LITE_PRESET_ID)
        followers = sorted([played, idle])
        before = {story: (await _effective(client, story))["fingerprint"] for story in followers}

        # -- a lost capability: held, nothing written, the key not kept
        quiet = merged(SETTINGS, nest("memory.rolling_summary.enabled", False))
        edit = {"name": "Brisk Lite", "settings": quiet}
        key = f"edit-{secrets.token_hex(6)}"
        unchanged = await configuration_rows(scratch.db)
        held = await ok(
            await client.put(f"{PROFILES}/{profile}", json=edit, headers=headers(B, key))
        )
        assert (held["applied"], held["result"]) == (False, None)
        report = held["impact"]
        assert [entry["story_id"] for entry in report["affected_stories"]] == followers
        assert (report["affected_count"], report["warnings"]) == (2, ["capabilities_lost"])
        assert (report["recommendations_unmet"], report["guarantees_lost"]) == ([], [])
        assert (
            report["fingerprint_before"],
            report["fingerprint_after"],
            report["capabilities_after"],
        ) == (None, None, None)
        without_summary = [c for c in LITE_CAPABILITIES if c != "rolling_summary"]
        for entry in report["affected_stories"]:
            assert (entry["capabilities_gained"], entry["capabilities_lost"]) == (
                [],
                ["rolling_summary"],
            )
            assert entry[REDERIVATION] == {"needed": False, "estimate": None}
            assert entry["upgrade_unavailable"] is False
            assert entry["fingerprint_before"] == before[entry["story_id"]]
            assert entry["fingerprint_after"] != entry["fingerprint_before"]
            assert entry["capabilities_after"] == without_summary
            assert entry["quality_status_after"] == UNCERTIFIED
        assert await configuration_rows(scratch.db) == unchanged

        # -- confirmed under the same key (camelCase): applied, and replayed on a retry
        confirm = {**edit, CAMEL_CASE: True}
        applied = await client.put(f"{PROFILES}/{profile}", json=confirm, headers=headers(B, key))
        done = await ok(applied)
        assert (done["applied"], done["result"]["version"], done["impact"]) == (True, 2, report)
        after = {e["story_id"]: e["fingerprint_after"] for e in report["affected_stories"]}
        for story in followers:
            now = await _effective(client, story)
            assert (now["profile"]["version"], now["profile"]["pinned"]) == (2, False)
            assert now["fingerprint"] == after[story]
            assert now["capabilities"] == without_summary
        again = await client.put(f"{PROFILES}/{profile}", json=confirm, headers=headers(B, key))
        assert (again.headers.get("Idempotent-Replay"), again.json()) == ("true", done)
        (recorded,) = await rows(
            scratch.db, "SELECT kind, principal FROM operations WHERE idempotency_key = :k", k=key
        )
        assert tuple(recorded) == ("control", B)

        # -- a change no story feels as a capability: applied at once, a new fingerprint
        warmer = merged(quiet, nest("models.generation.temperature", 0.5))
        warm = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={"name": "Brisk Lite", "settings": warmer},
                headers=headers(A),
            )
        )
        assert (warm["applied"], warm["result"]["version"], warm["impact"]["warnings"]) == (
            True,
            3,
            [],
        )
        for entry in warm["impact"]["affected_stories"]:
            assert (entry["capabilities_gained"], entry["capabilities_lost"]) == ([], [])
            assert entry["fingerprint_before"] == after[entry["story_id"]]
            assert entry["fingerprint_after"] != entry["fingerprint_before"]

        # -- what cannot run is refused, confirmed or not; so are both spellings at odds
        still = await configuration_rows(scratch.db)
        invalid = {"name": "Brisk Lite", "settings": {"pipeline": "structured"}}
        for body in (invalid, {**invalid, "ignore_warnings": True}):
            refused(
                await client.put(f"{PROFILES}/{profile}", json=body, headers=headers(A)),
                422,
                "invalid_profile",
            )
        refused(
            await client.post(
                f"/stories/{idle}/impact-preview",
                json={"overrides": {"pipeline": "structured"}},
                headers=headers(A),
            ),
            422,
            "invalid_profile",
        )
        uncredentialed = await client.put(
            f"{PROFILES}/{profile}",
            json={**edit, "ignore_warnings": True},
            headers={"X-Epistrel-Principal": A},
        )
        refused(uncredentialed, 401, "unauthenticated")
        clash = {**edit, "ignore_warnings": True, CAMEL_CASE: False}
        error = refused(
            await client.put(f"{PROFILES}/{profile}", json=clash, headers=headers(A)),
            422,
            "validation_error",
        )
        assert error["details"]["field"] == "body"
        assert await configuration_rows(scratch.db) == still

        # -- the preview: one story, the fingerprints at the top, nothing written
        shown = await client.post(
            f"/stories/{idle}/impact-preview",
            json={"profile_id": LITE_PRESET_ID},
            headers=headers(A),
        )
        preview = await ok(shown)
        assert "Idempotency-Key" not in shown.headers
        (entry,) = preview["affected_stories"]
        assert (entry["story_id"], entry["capabilities_gained"]) == (idle, ["rolling_summary"])
        assert (preview["affected_count"], preview["warnings"]) == (1, ["capabilities_gained"])
        current = (await _effective(client, idle))["fingerprint"]
        assert preview["fingerprint_before"] == entry["fingerprint_before"] == current
        assert preview["fingerprint_after"] == entry["fingerprint_after"] != current
        assert preview["capabilities_after"] == entry["capabilities_after"] == LITE_CAPABILITIES
        assert preview["quality_status_after"] == UNCERTIFIED
        assert await configuration_rows(scratch.db) == still

    # -- a creation the scenario's recommendation is not met by: held, then confirmed
    lacking = undelivered_modules()[0].name
    lite = Configuration(LITE_PRESET_ID, 1, parse_settings(lite_settings()))
    facts = RuntimeFacts(models={}, templates={})
    recommended = Recommendation(capabilities=(lacking, "lite"), recommended_by="SCN@1")
    created: list[str] = []

    async def report_fn() -> Any:
        return creation_report(lite.stamp(facts), lite.settings, recommended)

    async def create(_: Any) -> str:
        created.append("story")
        return "story"

    first: TwoStageResult[str] = await two_stage(report_fn, create, ignore_warnings=False)
    assert (first.applied, first.result, created) == (False, None, [])
    assert first.impact.warnings == ["recommendations_unmet"]
    assert [(u.capability, u.recommended_by) for u in first.impact.recommendations_unmet] == [
        (lacking, "SCN@1")
    ]
    assert first.impact.fingerprint_after == lite.stamp(facts).fingerprint
    assert first.impact.capabilities_after == LITE_CAPABILITIES
    second: TwoStageResult[str] = await two_stage(report_fn, create, ignore_warnings=True)
    assert (second.applied, second.result, created) == (True, "story", ["story"])
    assert second.impact == first.impact


@pytest.mark.part("M4", "FR-PROF-7", interim_until="M7")
async def test_c_16__upgrade_unavailable(scratch: Space) -> None:
    """Until M7 nothing re-derives a story's data from its transcript, so a data-producing
    increase cannot reach a story with history. A delivered module is marked data-producing for
    the test (none is in M3; M4's own slice uses consolidation). Two stories follow a profile
    with the polish pass off; one has played a turn. Turning the polish pass on, without
    ``ignore_warnings``, changes nothing and reports, by story id: the story with history
    ``upgrade_unavailable``, its re-derivation needed with an estimate over its transcript (one
    call per 30 messages, its characters over four in tokens, the measured per-call latency),
    and keeping its fingerprint and its capabilities (no polish pass); the story without turns
    gaining the polish pass with nothing to re-derive, its capabilities after holding it. With
    ``ignore_warnings`` the edit applies to the second story, which runs on version 2 with the
    polish pass, while the first keeps version 1 (pinned, as its effective configuration states)
    and its next turn runs on it: each runs with the capabilities its entry stated. A preview of
    the same increase reports the same for each story alone.

    Complement asserted: without the marking, the same kind of edit moves a story with history
    to the new version, as an ordinary gained capability."""
    off = merged(SETTINGS, nest(POLISH, False))
    on = merged(SETTINGS, nest(POLISH, True))
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile = (
            await ok(
                await client.post(PROFILES, json=profile_body(settings=off), headers=headers(A)),
                201,
            )
        )["id"]
        await _default(client, profile)
        played, actor = await _story(client)
        await _play(client, played, actor)
        fresh, _ = await _story(client)
        before = {s: (await _effective(client, s))["fingerprint"] for s in (played, fresh)}
        sizes = await rows(
            scratch.db,
            "SELECT count(*), sum(length(text)) FROM transcript_messages "
            "WHERE story_id = :s AND membership = 'active'",
            s=played,
        )
        (messages, characters) = tuple(sizes[0])
        edit = {"name": "Brisk Lite", "settings": on}
        with data_producing("polish"):
            unchanged = await configuration_rows(scratch.db)
            held = await ok(
                await client.put(f"{PROFILES}/{profile}", json=edit, headers=headers(A))
            )
            assert held["applied"] is False
            assert await configuration_rows(scratch.db) == unchanged
            report = held["impact"]
            assert report["warnings"] == UNAVAILABLE_WARNINGS
            entries = {e["story_id"]: e for e in report["affected_stories"]}
            assert list(entries) == sorted([played, fresh])
            kept, moved = entries[played], entries[fresh]
            assert (kept["upgrade_unavailable"], kept["capabilities_gained"]) == (True, ["polish"])
            assert kept[REDERIVATION] == {
                "needed": True,
                "estimate": {
                    "model_calls": -(-messages // 30),
                    "tokens": characters // 4,
                    "duration_s": -(-messages // 30) * CALL_LATENCY_MS / 1000,
                },
            }
            assert kept["fingerprint_before"] == kept["fingerprint_after"] == before[played]
            unpolished = [c for c in LITE_CAPABILITIES if c != "polish"]
            assert kept["capabilities_after"] == unpolished
            assert (moved["upgrade_unavailable"], moved["capabilities_gained"]) == (
                False,
                ["polish"],
            )
            assert moved[REDERIVATION] == {"needed": False, "estimate": None}
            assert moved["fingerprint_before"] == before[fresh] != moved["fingerprint_after"]
            assert moved["capabilities_after"] == LITE_CAPABILITIES

            preview = await ok(
                await client.post(
                    f"/stories/{played}/impact-preview",
                    json={"overrides": nest(POLISH, True)},
                    headers=headers(A),
                )
            )
            assert preview["warnings"] == UNAVAILABLE_WARNINGS
            assert preview["affected_stories"][0]["upgrade_unavailable"] is True
            assert preview["capabilities_after"] == unpolished
            alone = await ok(
                await client.post(
                    f"/stories/{fresh}/impact-preview",
                    json={"overrides": nest(POLISH, True)},
                    headers=headers(A),
                )
            )
            assert alone["warnings"] == ["capabilities_gained"]
            assert alone["capabilities_after"] == LITE_CAPABILITIES

            confirmed = await ok(
                await client.put(
                    f"{PROFILES}/{profile}",
                    json={**edit, "ignore_warnings": True},
                    headers=headers(A),
                )
            )
            assert (confirmed["applied"], confirmed["result"]["version"]) == (True, 2)
        stays = await _effective(client, played)
        goes = await _effective(client, fresh)
        assert (stays["profile"]["version"], stays["profile"]["pinned"]) == (1, True)
        assert "polish" not in stays["capabilities"]
        assert stays["capabilities"] == kept["capabilities_after"]
        assert stays["fingerprint"] == before[played]
        assert (goes["profile"]["version"], goes["profile"]["pinned"]) == (2, False)
        assert "polish" in goes["capabilities"]
        assert goes["capabilities"] == moved["capabilities_after"]
        assert goes["fingerprint"] == moved["fingerprint_after"]
        pins = await rows(
            scratch.db,
            "SELECT id, engine_profile_version_pin FROM stories WHERE id IN (:a, :b)",
            a=played,
            b=fresh,
        )
        assert dict((r[0], r[1]) for r in pins) == {played: 1, fresh: None}
        turn = await _play(client, played, actor)
        assert turn["fingerprint"] == before[played]
        assert "polish" not in turn["capabilities"]

        # -- complement: unmarked, the same kind of edit moves a story with history along
        other = (
            await ok(
                await client.post(PROFILES, json=profile_body(settings=off), headers=headers(A)),
                201,
            )
        )["id"]
        await _default(client, other)
        lived, hero = await _story(client)
        await _play(client, lived, hero)
        ordinary = await ok(await client.put(f"{PROFILES}/{other}", json=edit, headers=headers(A)))
        assert ordinary["impact"]["warnings"] == ["capabilities_gained"]
        assert ordinary["impact"]["affected_stories"][0]["upgrade_unavailable"] is False
        moved_on = await ok(
            await client.put(
                f"{PROFILES}/{other}", json={**edit, "ignore_warnings": True}, headers=headers(A)
            )
        )
        assert moved_on["applied"] is True
        assert (await _effective(client, lived))["profile"] == {
            "id": other,
            "version": 2,
            "pinned": False,
            "deleted": False,
        }
        await _default(client, LITE_PRESET_ID)


# -- C-17 --------------------------------------------------------------------------------------

FACTORS: dict[str, list[Any]] = {
    "memory.rolling_summary.enabled": [True, False],
    "pins.enabled": [True, False],
    "repair.polish.enabled": [True, False],
    "budgets.model_calls": [None, 12],
    "models.generation.response_length": ["short", 600],
}
"""The delivered modules' settings C-17 combines, each with two levels."""


# -- #113: the effective configuration and following the latest version ------------------------


async def _assign(client: httpx.AsyncClient, story: str, profile: str) -> dict[str, Any]:
    """Assign ``profile`` to ``story`` (confirmed); the answer."""
    answer = await client.put(
        f"/stories/{story}/engine-profile",
        json={"profile_id": profile, "ignore_warnings": True},
        headers=headers(A),
    )
    found = await ok(answer)
    assert found["applied"] is True, found
    return found


async def _overrides(
    client: httpx.AsyncClient, story: str, settings: dict[str, Any], **fields: Any
) -> httpx.Response:
    return await client.patch(
        f"/stories/{story}/profile-overrides",
        json={"settings": settings, **fields},
        headers=headers(A),
    )


async def _edit(client: httpx.AsyncClient, profile: str, settings: dict[str, Any]) -> Any:
    """A confirmed edit of ``profile``'s settings; the answer."""
    return await ok(
        await client.put(
            f"{PROFILES}/{profile}",
            json={"name": "Brisk Lite", "settings": settings, "ignore_warnings": True},
            headers=headers(A),
        )
    )


def _sent(stub: Any) -> tuple[Any, ...]:
    """The temperature, top_p and token cap of the stub's one generation call."""
    (call,) = generation_calls(stub)
    return (call.params.temperature, call.params.top_p, call.params.max_tokens)


async def _turn_configuration(db: AsyncEngine, story: str) -> dict[str, Any]:
    """The configuration the story's latest turn recorded (``operations.config_revisions``)."""
    (row,) = await rows(
        db,
        "SELECT config_revisions FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY committed_at DESC LIMIT 1",
        s=story,
    )
    found: dict[str, Any] = row.config_revisions
    return found


JOBS = "SELECT count(*) FROM jobs"
ASSISTED_INPUT = {"type": "model_assisted", "prompt": "Is this text rude?", "on_uncertain": "pass"}
"""A model-assisted input policy: the turn's input check is one model call a test can hold."""


@pytest.mark.part("M3", "FR-PROF-5,6")
async def test_u_28__effective(scratch: Space) -> None:
    """The effective configuration is the deployment's defaults, then the profile's version,
    then the story's overrides, validated as a whole (FR-PROF-5). The defaults set ``top_p``
    and the response length, a profile leaves them out and sets the temperature and the window,
    and the story overrides the window: the read and the library name each setting's layer, and
    the turn's generation request carries the temperature, ``top_p`` and length so resolved.

    An override that makes the configuration invalid is refused 422 ``invalid_profile`` and
    nothing is stored: one naming an undelivered module, and (under a test-only rule that makes
    pinned memories need the rolling summary) one turning the summary off. Complement: a valid
    override is stored, at the next revision.

    The overrides persist across the profile's versions. A behavioral edit (FR-PROF-6, the
    temperature) is followed at once: the read states version 2, the edit enqueues no job, and
    the story's next turn sends the new temperature. A turn already under way when the profile
    changes (its input being checked, before its read phase) finishes under the configuration
    it captured at its start (prospective): its generation request and its recorded version
    are the old ones, and the turn after it runs under the new version."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        stub = lib.models.stub
        defaults = {"models": {"generation": {"top_p": 0.7, "response_length": "short"}}}
        await ok(
            await client.put(
                "/admin/settings", json={"profile_defaults": defaults}, headers=headers(A)
            )
        )
        sparse = merged(
            nest("models.generation.temperature", 0.4),
            nest("memory.rolling_summary.window_messages", 10),
        )
        profile = (
            await ok(
                await client.post(PROFILES, json=profile_body(settings=sparse), headers=headers(A)),
                201,
            )
        )["id"]
        story, actor = await _story(client)
        await _assign(client, story, profile)
        window = nest("memory.rolling_summary.window_messages", 6)
        stored = await ok(await _overrides(client, story, window))
        assert stored["result"]["overrides"] == {"settings": window, "revision": 1}

        # -- precedence: defaults → the profile's version → the story's overrides
        read = await _effective(client, story)
        assert read["deployment_defaults"]["models"]["generation"]["top_p"] == 0.7
        assert read["profile"] == {"id": profile, "version": 1, "pinned": False, "deleted": False}
        assert read["overrides"] == {"settings": window, "revision": 1}
        generation = read["effective"]["models"]["generation"]
        assert (generation["temperature"], generation["top_p"], generation["response_length"]) == (
            0.4,
            0.7,
            "short",
        )
        assert read["effective"]["memory"]["rolling_summary"]["window_messages"] == 6
        async with scratch.db.connect() as conn:
            found = await effective_configuration(conn, story)
        layers = {
            path: found.source_of(path)
            for path in (
                "models.generation.top_p",
                "models.generation.temperature",
                "memory.rolling_summary.window_messages",
            )
        }
        assert layers == {
            "models.generation.top_p": "deployment",
            "models.generation.temperature": "profile",
            "memory.rolling_summary.window_messages": "story",
        }
        stub.reset()
        await _play(client, story, actor)
        assert _sent(stub) == (0.4, 0.7, 150)

        # -- what cannot run is refused and nothing is stored; what can is stored
        unchanged = await configuration_rows(scratch.db)
        refused(
            await _overrides(client, story, naming(undelivered_modules()[0])),
            422,
            "invalid_profile",
        )
        rule = DependencyRule(TEST_CLAUSE, "pins", (Requirement("rolling_summary"),))
        with extra_rule(rule):
            quiet = nest("memory.rolling_summary.enabled", False)
            error = refused(
                await _overrides(client, story, quiet, ignore_warnings=True),
                422,
                "invalid_profile",
            )
            assert [m["module"] for m in error["details"]["missing_dependencies"]] == ["pins"]
        assert await configuration_rows(scratch.db) == unchanged
        assert (await _effective(client, story))["overrides"]["revision"] == 1
        pins = nest("pins.max_pinned", 7)
        kept = await ok(await _overrides(client, story, pins))
        assert kept["result"]["overrides"] == {"settings": merged(window, pins), "revision": 2}

        # -- the overrides persist across a new version, which is followed at once
        jobs = await scalar(scratch.db, JOBS)
        edited = await _edit(
            client, profile, merged(sparse, nest("models.generation.temperature", 0.5))
        )
        assert edited["result"]["version"] == 2
        assert await scalar(scratch.db, JOBS) == jobs  # no job: nothing to re-derive
        read = await _effective(client, story)
        assert read["profile"]["version"] == 2
        assert read["overrides"] == {"settings": merged(window, pins), "revision": 2}
        assert read["effective"]["models"]["generation"]["temperature"] == 0.5
        assert read["effective"]["memory"]["rolling_summary"]["window_messages"] == 6
        stub.reset()
        await _play(client, story, actor)
        assert _sent(stub) == (0.5, 0.7, 150)
        assert (await _turn_configuration(scratch.db, story))["profile_version"] == 2

        # -- a turn in flight keeps the configuration it captured at its start: the edit lands
        # while the turn's input is checked (a model-assisted input policy), before its read
        # phase and its generation
        await lib.policies_set_story(
            AUTHOR,
            story,
            [story_item(ASSISTED_INPUT, name="effective-in", applies_to=["text_input"])],
        )
        stub.reset()
        latch = stub.hold("guardrail_check")
        turn = asyncio.ensure_future(_play(client, story, actor))
        await asyncio.wait_for(latch.entered.wait(), 10)
        warmer = await _edit(
            client, profile, merged(sparse, nest("models.generation.temperature", 0.9))
        )
        assert warmer["result"]["version"] == 3
        latch.release()
        held = await turn
        assert _sent(stub) == (0.5, 0.7, 150)
        assert held["profile"] == {"id": profile, "version": 2}
        recorded = await _turn_configuration(scratch.db, story)
        assert (recorded["profile_version"], recorded["overrides_revision"]) == (2, 2)
        assert recorded["generation_settings"]["temperature"] == 0.5
        stub.reset()
        after = await _play(client, story, actor)
        assert _sent(stub) == (0.9, 0.7, 150)
        assert after["profile"] == {"id": profile, "version": 3}


@pytest.mark.part("M3", "FR-PROF-1,6; FR-API-6")
async def test_c_16__follow_latest(scratch: Space) -> None:
    """A story follows its profile's latest version (FR-PROF-6). Two stories are assigned a
    profile through the API (FR-PROF-1, FR-API-6) and a third stays on Lite; each plays, and
    every respond response reports the profile and the version the turn ran under, and the
    story's configuration revision at its start (which the edit below, a deployment-scope
    change, leaves as it is: the deployment's moves). A
    behavioral edit of the profile (the temperature and the summary window) applies at once:
    its impact report lists both followers with no capability gained or lost and no backfill,
    and warns of nothing. It is a configuration change: one deployment revision, a row for the
    version and one per changed setting, each prospective. It enqueues no job. At each
    follower's next turn boundary the edit takes effect: the generation request carries the new
    temperature, the response and the turn's recorded configuration name version 2, and the
    smaller window makes the summary job due (it was not under the old one). The Lite story is
    untouched: its turn still runs under Lite version 1 and Lite's temperature."""
    async with respond_client(scratch.settings, scratch.db) as (client, lib):
        stub = lib.models.stub
        tuned = merged(
            nest("models.generation.temperature", 0.6),
            nest("memory.rolling_summary.window_messages", 12),
        )
        profile = (
            await ok(
                await client.post(PROFILES, json=profile_body(settings=tuned), headers=headers(A)),
                201,
            )
        )["id"]
        followers = sorted([(await _story(client)) for _ in range(2)])
        bystander, bystander_actor = await _story(client)
        for story, _ in followers:
            assigned = await _assign(client, story, profile)
            assert assigned["result"]["profile"]["id"] == profile
            assert assigned["impact"]["warnings"] == []
            chunked = await client.patch(
                f"/stories/{story}/settings", json={"lite_summary_chunk": 2}, headers=headers(A)
            )
            await ok(chunked)
        story_revision = "SELECT config_revision FROM stories WHERE id = :s"
        revisions: dict[str, int] = {}
        for story, actor in followers:
            for _ in range(3):
                stub.reset()
                played = await _play(client, story, actor)
                assert played["profile"] == {"id": profile, "version": 1}
                assert _sent(stub)[0] == 0.6
            assert await summary_jobs(scratch.db, story) == []  # 6 rows: within the window
            revisions[story] = await scalar(scratch.db, story_revision, s=story)
            assert played["config_revision"] == revisions[story]
        stub.reset()
        lite = await _play(client, bystander, bystander_actor)
        assert lite["profile"] == {"id": LITE_PRESET_ID, "version": 1}
        assert _sent(stub)[0] == 0.8

        # -- the behavioral edit: applied at once, no warning, a configuration change
        revision = int(await scalar(scratch.db, "SELECT config_revision FROM deployment_state"))
        jobs = await scalar(scratch.db, JOBS)
        quicker = merged(
            nest("models.generation.temperature", 0.3),
            nest("memory.rolling_summary.window_messages", 4),
        )
        edited = await ok(
            await client.put(
                f"{PROFILES}/{profile}",
                json={"name": "Brisk Lite", "settings": quicker},
                headers=headers(A),
            )
        )
        assert (edited["applied"], edited["result"]["version"]) == (True, 2)
        report = edited["impact"]
        assert report["warnings"] == []
        assert [e["story_id"] for e in report["affected_stories"]] == [s for s, _ in followers]
        for entry in report["affected_stories"]:
            assert (entry["capabilities_gained"], entry["capabilities_lost"]) == ([], [])
            assert entry[REDERIVATION] == {"needed": False, "estimate": None}
            assert entry["upgrade_unavailable"] is False
        changes = await rows(
            scratch.db,
            'SELECT setting, old, new, "class" FROM config_changes WHERE scope = '
            "'deployment' AND config_revision = :r ORDER BY setting",
            r=revision + 1,
        )
        assert [tuple(c) for c in changes] == [
            (f"engine_profile:{profile}", 1, 2, "prospective"),
            (
                f"engine_profile:{profile}.memory.rolling_summary.window_messages",
                12,
                4,
                "prospective",
            ),
            (f"engine_profile:{profile}.models.generation.temperature", 0.6, 0.3, "prospective"),
        ]
        assert await scalar(scratch.db, JOBS) == jobs  # no backfill, nothing queued
        listed = await ok(
            await client.get(
                "/admin/config-changes",
                params={"setting": f"engine_profile:{profile}"},
                headers=READ,
            )
        )
        assert {i["config_revision"] for i in listed["items"]} == {revision + 1}

        # -- the next turn boundary: the new temperature, version 2, the smaller window
        for story, actor in followers:
            assert (await _effective(client, story))["profile"]["version"] == 2
            stub.reset()
            played = await _play(client, story, actor)
            assert played["profile"] == {"id": profile, "version": 2}
            assert played["config_revision"] == revisions[story]
            assert await scalar(scratch.db, story_revision, s=story) == revisions[story]
            assert _sent(stub)[0] == 0.3
            recorded = await _turn_configuration(scratch.db, story)
            assert (recorded["profile"], recorded["profile_version"]) == (profile, 2)
            assert len(await summary_jobs(scratch.db, story)) == 1  # 8 rows: over 4
        stub.reset()
        lite = await _play(client, bystander, bystander_actor)
        assert lite["profile"] == {"id": LITE_PRESET_ID, "version": 1}
        assert _sent(stub)[0] == 0.8


@pytest.mark.part("M3", "FR-PROF-2,4")
async def test_c_17__profiles(scratch: Space) -> None:
    """A pairwise covering array over the delivered modules' settings (the rolling summary,
    pinned memories and the polish pass on or off, a call budget or the formula, a named length
    or a token cap): every pair of levels appears in at least one configuration. Each
    configuration is validated, then created; it becomes the default, a story is created under
    it and plays a turn, which succeeds. A configuration that picks an undelivered level is
    refused by validation and never runs: no valid configuration fails at run time, and every
    configuration either runs cleanly or was refused."""
    rows_ = covering_array(FACTORS)
    every_pair = {
        (a, va, b, vb)
        for a in FACTORS
        for b in FACTORS
        if a < b
        for va in FACTORS[a]
        for vb in FACTORS[b]
    }
    seen = {(a, va, b, vb) if a < b else (b, vb, a, va) for a, va, b, vb in covered_pairs(rows_)}
    assert seen >= every_pair
    assert len(rows_) < 2 ** len(FACTORS)
    configurations = [merged(*(nest(path, value) for path, value in row.items())) for row in rows_]
    rejected = merged(configurations[0], {"pipeline": "structured"})

    async with respond_client(scratch.settings, scratch.db) as (client, _):
        ran = 0
        for settings in [*configurations, rejected]:
            checked = await client.post(
                f"{PROFILES}/{LITE_PRESET_ID}/validate",
                json={"settings": settings},
                headers=headers(A),
            )
            if checked.status_code == 422:
                assert checked.json()["error"]["code"] == "invalid_profile"
                refusal = await client.post(
                    PROFILES, json=profile_body(settings=settings), headers=headers(A)
                )
                refused(refusal, 422, "invalid_profile")
                continue
            assert checked.json()["valid"] is True
            created = await ok(
                await client.post(
                    PROFILES, json=profile_body(settings=settings), headers=headers(A)
                ),
                201,
            )
            await _default(client, created["id"])
            story, actor = await _story(client)
            assert await _profile_of(scratch.db, story) == created["id"]
            played = await client.post(
                f"/stories/{story}/respond", json=respond_body(actor), headers=headers(A)
            )
            assert (await ok(played))["message"]["text"], settings
            ran += 1
        await _default(client, LITE_PRESET_ID)
    assert ran == len(configurations)


@pytest.mark.part("M3", "FR-PROF-4")
async def test_c_17__dependencies(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``missing_dependencies`` end to end. The declared graph holds a rule for each clause of
    the requirement (counted against the documented list). An undelivered module switched on is
    refused for real, naming it, on create, edit and validate. No two delivered modules depend
    on each other, so a test-only rule makes pinned memories require the rolling summary: a
    profile with pins on and the summary off is refused 422 ``invalid_profile`` naming the
    missing module, on create, edit and validate, and nothing is written; the same profile with
    the summary on is accepted (complement). Without the rule, the first profile is accepted
    too: the refusal was the rule's."""
    assert len(DEPENDENCIES) == len(documented_rules()) == 26
    rule = DependencyRule(TEST_CLAUSE, "pins", (Requirement("rolling_summary"),))
    lacking = merged(nest("pins.enabled", True), nest("memory.rolling_summary.enabled", False))
    having = merged(lacking, nest("memory.rolling_summary.enabled", True))
    expected = [
        {"module": "pins", "level": "on", "requires": [{"module": "rolling_summary", "levels": []}]}
    ]
    async with respond_client(stub_settings, engine) as (client, _):
        base = await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        path = f"{PROFILES}/{base['id']}"
        for module in undelivered_modules():
            for method, url, body in (
                ("POST", PROFILES, profile_body(settings=naming(module))),
                ("PUT", path, {"name": "Brisk Lite", "settings": naming(module)}),
                ("POST", f"{path}/validate", {"settings": naming(module)}),
            ):
                found = refused(
                    await client.request(method, url, json=body, headers=headers(A)),
                    422,
                    "invalid_profile",
                )
                assert module.name in {u["module"] for u in found["details"]["undelivered"]}
        before = await scalar(engine, "SELECT count(*) FROM engine_profile_versions")
        with extra_rule(rule):
            for method, url, body in (
                ("POST", PROFILES, profile_body(settings=lacking)),
                ("PUT", path, {"name": "Brisk Lite", "settings": lacking}),
                ("POST", f"{path}/validate", {"settings": lacking}),
            ):
                found = refused(
                    await client.request(method, url, json=body, headers=headers(A)),
                    422,
                    "invalid_profile",
                )
                assert found["details"] == {"missing_dependencies": expected, "undelivered": []}
            assert await scalar(engine, "SELECT count(*) FROM engine_profile_versions") == before
            accepted = await client.post(
                PROFILES, json=profile_body(settings=having), headers=headers(A)
            )
            assert (await ok(accepted, 201))["settings"] == having
        without = await client.post(
            PROFILES, json=profile_body(settings=lacking), headers=headers(A)
        )
        assert (await ok(without, 201))["settings"] == lacking


# -- the detailed tests ------------------------------------------------------------------------


async def test_settings_are_checked_at_the_boundary(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A settings document is validated strictly: a value only coercion would accept, an
    unknown key, a bound crossed, an unknown model purpose, or an endpoint in a route is 422
    ``validation_error`` at its field; a purpose reserved for a later milestone is 422
    ``invalid_profile``; a NUL character is 422; nothing is written for any of them. The
    library refuses the same way. Complement: a delivered purpose with its own model and
    parameters is accepted and stored as sent."""
    cases: list[tuple[dict[str, Any], str, str]] = [
        (
            nest("memory.rolling_summary.window_messages", True),
            "validation_error",
            "body.settings.memory.rolling_summary.window_messages",
        ),
        (nest("pins.max_pinned", "20"), "validation_error", "body.settings.pins.max_pinned"),
        (
            nest("memory.rolling_summary.window_messages", 501),
            "validation_error",
            "body.settings.memory.rolling_summary.window_messages",
        ),
        ({"pins": {"maximum": 3}}, "validation_error", "body.settings.pins.maximum"),
        ({"surprise": {}}, "validation_error", "body.settings.surprise"),
        (nest("budgets.model_calls", 0), "validation_error", "body.settings.budgets.model_calls"),
        (
            nest("budgets.deadline_ms", 600_001),
            "validation_error",
            "body.settings.budgets.deadline_ms",
        ),
        (
            {"models": {"purposes": {"nonesuch": {"model": "m"}}}},
            "validation_error",
            "body.settings.models.purposes.nonesuch",
        ),
        (
            {"models": {"purposes": {"generation": {"base_url": "http://x"}}}},
            "validation_error",
            "body.settings.models.purposes.generation.base_url",
        ),
        ({"models": {"purposes": {"entailment": {"model": "m"}}}}, "invalid_profile", ""),
        (
            nest("models.purposes.generation.model", "a\x00b"),
            "validation_error",
            "body.settings.models.purposes.generation.model",
        ),
    ]
    async with respond_client(stub_settings, engine) as (client, lib):
        before = await scalar(engine, "SELECT count(*) FROM engine_profiles")
        for settings, code, field in cases:
            error = refused(
                await client.post(
                    PROFILES, json=profile_body(settings=settings), headers=headers(A)
                ),
                422,
                code,
            )
            if field:
                assert error["details"]["field"] == field, (settings, error)
            else:
                assert error["details"]["undelivered"] == [
                    {"module": "models", "level": None, "setting": "models.purposes.entailment"}
                ]
            with pytest.raises((InvalidRequest, InvalidProfile)):
                await lib.create_profile(
                    RequestContext(principal=A),
                    _library_body(profile_body(settings=settings)),
                )
        assert await scalar(engine, "SELECT count(*) FROM engine_profiles") == before

        routed = {
            "models": {
                "purposes": {"generation": {"model": "house-model", "params": {"temperature": 0.4}}}
            }
        }
        stored = await ok(
            await client.post(PROFILES, json=profile_body(settings=routed), headers=headers(A)), 201
        )
        assert stored["settings"] == routed


def _library_body(body: dict[str, Any]) -> ProfileCreate:
    return ProfileCreate.model_validate(body)


async def test_validate_without_a_body_checks_the_latest_version(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``POST /engine-profiles/{id}/validate`` with no body, ``{}`` or ``{"settings": null}``
    validates the profile's latest version, as the library does without a request. While a
    test-only rule makes pinned memories require the rolling summary, a profile stored with
    pins on and the summary off is refused 422 ``invalid_profile`` naming the missing module,
    on each of them and in the library, while a dry run of settings that meet the rule is
    accepted. Once an edit stores those settings as version 2, the call with no body is
    accepted: it validates the latest version, not the first. Complement: without the rule,
    the first version validates with no body too. An unknown profile is 404."""
    rule = DependencyRule(TEST_CLAUSE, "pins", (Requirement("rolling_summary"),))
    lacking = merged(nest("pins.enabled", True), nest("memory.rolling_summary.enabled", False))
    having = merged(lacking, nest("memory.rolling_summary.enabled", True))
    expected = [
        {"module": "pins", "level": "on", "requires": [{"module": "rolling_summary", "levels": []}]}
    ]
    valid = {"valid": True, "missing_dependencies": [], "undelivered": []}
    ctx = RequestContext(principal=A)
    async with respond_client(stub_settings, engine) as (client, lib):
        created = await ok(
            await client.post(PROFILES, json=profile_body(settings=lacking), headers=headers(A)),
            201,
        )
        profile = created["id"]
        path = f"{PROFILES}/{profile}/validate"
        assert await ok(await client.post(path, headers=headers(A))) == valid
        with extra_rule(rule):
            for body in (None, {}, {"settings": None}):
                found = refused(
                    await client.post(path, json=body, headers=headers(A)), 422, "invalid_profile"
                )
                assert found["details"] == {"missing_dependencies": expected, "undelivered": []}
            with pytest.raises(InvalidProfile):
                await lib.validate_profile(ctx, profile)
            dry = await client.post(path, json={"settings": having}, headers=headers(A))
            assert await ok(dry) == valid
            edited = await client.put(
                f"{PROFILES}/{profile}",
                json={"name": "Brisk Lite", "settings": having},
                headers=headers(A),
            )
            assert (await ok(edited))["result"]["version"] == 2
            assert await ok(await client.post(path, headers=headers(A))) == valid
            assert (await lib.validate_profile(ctx, profile)).valid
        unknown = await client.post(f"{PROFILES}/{'0' * 26}/validate", headers=headers(A))
        refused(unknown, 404, "not_found")


async def test_edits_record_their_requester_and_replay(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Every profile write records one committed ``control`` operation by its requester. Only a
    new version is a configuration change (#113): the edit that makes one moves the
    deployment's configuration revision by exactly one, and the create, the copy and the delete
    move none; an edit that changes nothing records nothing and makes no version. A retry under
    the same ``Idempotency-Key`` replays the answer (``Idempotent-Replay: true``) and records
    nothing more; the key with another body is 409 ``idempotency_conflict``. Validation writes
    nothing and takes no key."""
    async with respond_client(stub_settings, engine) as (client, _):
        revision = await scalar(engine, "SELECT config_revision FROM deployment_state")
        key = f"prof-{secrets.token_hex(6)}"
        created = await client.post(PROFILES, json=profile_body(), headers=headers(A, key))
        profile = (await ok(created, 201))["id"]
        again = await client.post(PROFILES, json=profile_body(), headers=headers(A, key))
        assert (again.status_code, again.headers.get("Idempotent-Replay")) == (201, "true")
        assert again.json()["id"] == profile
        conflict = await client.post(PROFILES, json=profile_body("Other"), headers=headers(A, key))
        refused(conflict, 409, "idempotency_conflict")
        for method, suffix, body, status, principal in (
            ("PUT", "", {"name": "Brisk Lite", "settings": EDITED}, 200, B),
            ("POST", "/copy", {"name": "Copy"}, 201, A),
            ("DELETE", "", None, 200, B),
        ):
            write_key = f"prof-{secrets.token_hex(6)}"
            response = await client.request(
                method,
                f"{PROFILES}/{profile}{suffix}",
                json=body,
                headers=headers(principal, write_key),
            )
            await ok(response, status)
            (operation,) = await rows(
                engine,
                "SELECT kind, principal, story_id FROM operations WHERE idempotency_key = :k",
                k=write_key,
            )
            assert tuple(operation) == ("control", principal, None)
        noop_key = f"prof-{secrets.token_hex(6)}"
        fresh = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        noop = await client.put(
            f"{PROFILES}/{fresh}",
            json={"name": "Brisk Lite", "settings": SETTINGS},
            headers=headers(A, noop_key),
        )
        assert (await ok(noop))["result"]["version"] == 1
        recorded = await scalar(
            engine, "SELECT count(*) FROM operations WHERE idempotency_key = :k", k=noop_key
        )
        assert recorded == 0
        checked = await client.post(f"{PROFILES}/{fresh}/validate", json={}, headers=headers(A))
        assert "Idempotency-Key" not in checked.headers
        # One new version (the PUT of ``EDITED``) and nothing else moved it.
        assert await scalar(engine, "SELECT config_revision FROM deployment_state") == revision + 1


async def test_lists_page_and_filter(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The list pages newest first through ``next_cursor`` without repeating or skipping a
    profile; ``owner`` and ``relation`` narrow it only when asked; deleted profiles appear only
    with ``include_deleted``; a malformed cursor is 422 at ``query.cursor``."""
    owner = f"p-pages-{secrets.token_hex(4)}"
    async with respond_client(stub_settings, engine) as (client, _):
        made = [
            (
                await ok(
                    await client.post(PROFILES, json=profile_body(owner=owner), headers=headers(A)),
                    201,
                )
            )["id"]
            for _ in range(5)
        ]
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"owner": owner, "limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            page = await ok(await client.get(PROFILES, params=params, headers=READ))
            seen += [item["id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == list(reversed(made))
        related = f"editor_{secrets.token_hex(3)}"
        await ok(
            await client.post(
                f"{PROFILES}/{made[0]}/relations",
                json={"principal": B, "relation": related},
                headers=headers(A),
            ),
            201,
        )
        filtered = await ok(
            await client.get(PROFILES, params={"relation": f"{related}:{B}"}, headers=READ)
        )
        assert [item["id"] for item in filtered["items"]] == [made[0]]
        await ok(await client.delete(f"{PROFILES}/{made[1]}", headers=headers(A)))
        live = await ok(await client.get(PROFILES, params={"owner": owner}, headers=READ))
        assert made[1] not in [item["id"] for item in live["items"]]
        every = await ok(
            await client.get(
                PROFILES, params={"owner": owner, "include_deleted": "true"}, headers=READ
            )
        )
        assert [item["id"] for item in every["items"]] == list(reversed(made))
        bad = await client.get(PROFILES, params={"cursor": "not-a-cursor"}, headers=READ)
        assert refused(bad, 422, "validation_error")["details"]["field"] == "query.cursor"


async def test_the_default_is_a_configuration_change(scratch: Space) -> None:
    """Making a profile the default is a prospective configuration change: one
    ``config_changes`` row with the old and new ids and the actor, and the revision moves by
    one. An unknown profile is 422 ``profile_deleted`` (``unknown``) and changes nothing; so is
    ``null``. A fork keeps its source's profile whatever the default is now."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        created = await ok(
            await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201
        )
        profile = created["id"]
        story, _ = await _story(client)
        before = await ok(await client.get("/admin/settings", headers=READ))
        assert before["default_engine_profile_id"] == LITE_PRESET_ID
        after = await _default(client, profile)
        assert after["config_revision"] == before["config_revision"] + 1
        (change,) = await rows(
            scratch.db,
            "SELECT setting, old, new, class, actor FROM config_changes "
            "WHERE setting = 'default_engine_profile_id'",
        )
        assert tuple(change) == (
            "default_engine_profile_id",
            LITE_PRESET_ID,
            profile,
            "prospective",
            A,
        )
        unknown = await client.put(
            "/admin/settings",
            json={"default_engine_profile_id": "0" * 26},
            headers=headers(A),
        )
        assert refused(unknown, 422, "profile_deleted")["details"]["reason"] == "unknown"
        nulled = await client.put(
            "/admin/settings", json={"default_engine_profile_id": None}, headers=headers(A)
        )
        refused(nulled, 422, "validation_error")
        current = await ok(await client.get("/admin/settings", headers=READ))
        assert current["config_revision"] == after["config_revision"]
        forked = await ok(
            await client.post(f"/stories/{story}/fork", json={"at_turn": 0}, headers=headers(A)),
            201,
        )
        assert await _profile_of(scratch.db, forked["story_id"]) == LITE_PRESET_ID


async def test_the_sweep_purges_only_unused_deleted_profiles(scratch: Space) -> None:
    """The retention sweep purges a deleted profile only once no story uses it: with two stories
    on it, deleting one keeps it; deleting both purges it. A live profile no story uses is
    never purged, and neither is a deleted one whose stories remain."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        used = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        idle = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        await _default(client, used)
        first, _ = await _story(client)
        second, _ = await _story(client)
        await _default(client, LITE_PRESET_ID)
        await ok(await client.delete(f"{PROFILES}/{used}", headers=headers(A)))
        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 0
        assert (await client.delete(f"/stories/{first}", headers=headers(A))).status_code == 204
        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 0
        assert (await client.delete(f"/stories/{second}", headers=headers(A))).status_code == 204
        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 1
        refused(await client.get(f"{PROFILES}/{used}", headers=READ), 404, "not_found")
        await ok(await client.get(f"{PROFILES}/{idle}", headers=READ))


async def _deleted_in_use(client: httpx.AsyncClient) -> tuple[str, str, str, dict[str, Any]]:
    """A profile deleted while a story uses it: the profile's id, the story's, the story's
    player character's, and the delete's answer (the default is Lite again)."""
    profile = (await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201))[
        "id"
    ]
    await _default(client, profile)
    story, actor = await _story(client)
    await _default(client, LITE_PRESET_ID)
    deleted = await ok(await client.delete(f"{PROFILES}/{profile}", headers=headers(A)))
    assert deleted["warnings"] == [
        {"kind": "profile_in_use", "story_id": story, "scenario_id": None}
    ]
    return profile, story, actor, deleted


async def test_a_fork_keeps_its_sources_deleted_profile(scratch: Space) -> None:
    """A fork copies its story, profile included, as it copies a retired rating, so it is not a
    new assignment: a fork of a story whose profile is deleted is 201 and runs under the
    deleted profile, and a turn plays on it. Deleting the profile again changes nothing and
    names both stories. The retention sweep keeps the profile while the source or the fork uses
    it, and purges it once neither does. Complement: a story created meanwhile runs under the
    default, never the deleted profile."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile, story, actor, deleted = await _deleted_in_use(client)
        forked = await ok(
            await client.post(f"/stories/{story}/fork", json={"at_turn": 0}, headers=headers(A)),
            201,
        )
        fork_id = forked["story_id"]
        assert await _profile_of(scratch.db, fork_id) == profile
        player = next(c["id"] for c in forked["characters"] if c["source_id"] == actor)
        played = await client.post(
            f"/stories/{fork_id}/respond", json=respond_body(player), headers=headers(A)
        )
        assert (await ok(played))["message"]["text"]
        assert await _profile_of(scratch.db, fork_id) == profile

        again = await ok(await client.delete(f"{PROFILES}/{profile}", headers=headers(A)))
        assert (again["deleted_at"], again["stories_using"]) == (deleted["deleted_at"], 2)
        assert again["warnings"] == [
            {"kind": "profile_in_use", "story_id": using, "scenario_id": None}
            for using in sorted([story, fork_id])
        ]
        fresh, _ = await _story(client)
        assert await _profile_of(scratch.db, fresh) == LITE_PRESET_ID

        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 0
        assert (await client.delete(f"/stories/{story}", headers=headers(A))).status_code == 204
        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 0
        await ok(await client.get(f"{PROFILES}/{profile}", headers=READ))
        assert (await client.delete(f"/stories/{fork_id}", headers=headers(A))).status_code == 204
        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 1
        refused(await client.get(f"{PROFILES}/{profile}", headers=READ), 404, "not_found")


async def test_a_fork_committing_mid_sweep_keeps_the_profile(
    scratch: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The purge checks a profile's stories again once it holds the profile's row. A fork reads
    its source in one snapshot without locking it, so a fork that began before its source was
    deleted still commits a story on the source's deleted profile. Here the source is deleted
    while the fork holds its snapshot (S-FORK-SNAPSHOT's seam), the sweep finds the profile
    unused and stops before its lock (``profiles.purge.before_lock``), and the fork commits:
    the sweep then keeps the profile, and no foreign-key error ends it. Complement: once the
    fork is deleted, the sweep purges the profile.

    The delete and the sweep run in contexts of their own, as separate requests and the job
    worker do: run in the fork's, the delete would take the fork's ``Idempotency-Key`` call."""
    async with respond_client(scratch.settings, scratch.db) as (client, _):
        profile, story, _, _ = await _deleted_in_use(client)
        original = forking._select_operations
        sweeps: list[asyncio.Task[dict[str, int]]] = []
        with pauses.held(pauses.PROFILE_PURGE_BEFORE_LOCK) as hold:

            async def source_gone_then_sweep(conn: AsyncConnection, *args: Any) -> Any:
                if not sweeps:
                    await asyncio.create_task(
                        delete_story(scratch.db, story, principal=A), context=Context()
                    )
                    sweeps.append(
                        asyncio.create_task(
                            retention.sweep_retention(scratch.db), context=Context()
                        )
                    )
                    await hold.arrived(1)
                return await original(conn, *args)

            monkeypatch.setattr(forking, "_select_operations", source_gone_then_sweep)
            forked = await ok(
                await client.post(
                    f"/stories/{story}/fork", json={"at_turn": 0}, headers=headers(A)
                ),
                201,
            )
            hold.release()
            swept = await sweeps[0]
        assert swept["engine_profiles"] == 0
        fork_id = forked["story_id"]
        assert await _profile_of(scratch.db, fork_id) == profile
        assert await scalar(scratch.db, "SELECT count(*) FROM stories WHERE id = :s", s=story) == 0
        await ok(await client.get(f"{PROFILES}/{profile}", headers=READ))
        assert (await client.delete(f"/stories/{fork_id}", headers=headers(A))).status_code == 204
        assert (await retention.sweep_retention(scratch.db))["engine_profiles"] == 1
        refused(await client.get(f"{PROFILES}/{profile}", headers=READ), 404, "not_found")


async def test_deleting_warns_of_recommending_scenarios(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A delete also names every scenario whose draft or latest published version recommends
    the profile (``profile_recommended``): one recommending it in its draft, and one whose
    draft now recommends another profile while its published version still recommends this
    one. A scenario recommending another profile is not named."""
    async with respond_client(stub_settings, engine) as (client, _):
        profile = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]
        other = (
            await ok(await client.post(PROFILES, json=profile_body(), headers=headers(A)), 201)
        )["id"]

        async def scenario(recommended: str) -> str:
            body = catalog_scenario(
                "Gull Rock", recommendation={"engine_profile_id": recommended}
            )  # publishable as it is (#111's validation)
            created = await client.post("/catalog/scenarios", json=body, headers=headers(A))
            return str((await ok(created, 201))["id"])

        drafted = await scenario(profile)
        published = await scenario(profile)
        await ok(
            await client.post(
                f"/catalog/scenarios/{published}/publish",
                json={"draft_revision": 1},
                headers=headers(A),
            ),
            201,
        )
        await ok(
            await client.patch(
                f"/catalog/scenarios/{published}",
                json={"draft_revision": 2, "recommendation": {"engine_profile_id": other}},
                headers=headers(A),
            )
        )
        await scenario(other)
        deleted = await ok(await client.delete(f"{PROFILES}/{profile}", headers=headers(A)))
        assert deleted["warnings"] == [
            {"kind": "profile_recommended", "story_id": None, "scenario_id": scenario_id}
            for scenario_id in sorted([drafted, published])
        ]
        assert (deleted["stories_using"], deleted["scenarios_recommending"]) == (0, 2)


async def test_the_schema_route_publishes_what_is_delivered(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``GET /engine-profiles/schema`` answers the delivered schema, every delivered setting's
    three tags, the delivered modules with their levels, and the delivered model purposes; the
    library answers the same. The OpenAPI document's profile bodies carry the same schema."""
    async with respond_client(stub_settings, engine) as (client, lib):
        found = await ok(await client.get(f"{PROFILES}/schema", headers=READ))
        assert found["schema"] == delivered_schema()
        assert found["delivered"] == {
            "pipeline": ["lite"],
            "rolling_summary": ["on"],
            "pins": ["on"],
            "polish": ["on"],
            "budgets": ["on"],
            "models": ["on"],
        }
        assert found["purposes"] == sorted(DELIVERED_PURPOSES)
        assert found["tags"]["pins.max_pinned"] == {
            "tag": "behavioral",
            "class": "prospective",
            "quality": "irrelevant",
        }
        assert set(found["tags"]) == {path for path in LEAF_PATHS if exposed(path)}
        library = await lib.profile_schema(RequestContext())
        assert library.settings_schema == found["schema"]
        document = (await client.get("/openapi.json")).json()
        components = document["components"]["schemas"]
        delivered = _schema_leaves(delivered_schema())
        for name in ("ProfileCreate", "ProfileUpdate", "EngineProfile", "ProfileBody"):
            published = components[name]["properties"]["settings"]
            assert _schema_leaves(published) == delivered, name
            assert published["properties"]["pipeline"]["enum"] == ["lite"], name
        dry_run = components["ProfileValidate"]["properties"]["settings"]["anyOf"]
        assert _schema_leaves(dry_run[0]) == delivered


def test_lite_settings_validate_as_a_model() -> None:
    """The shipped Lite settings parse into the full model with nothing left over, and the
    model's defaults are Lite's for every delivered setting but the routing (Lite routes each
    delivered purpose to the deployment's model configuration explicitly)."""
    lite = parse_settings(lite_settings())
    defaults = ProfileSettings()
    assert lite.memory == defaults.memory
    assert lite.pins == defaults.pins
    assert lite.repair == defaults.repair
    assert lite.budgets == defaults.budgets
    assert lite.models.generation == defaults.models.generation
    assert set(lite.models.purposes) == set(DELIVERED_PURPOSES)
    assert all(route.model is None for route in lite.models.purposes.values())


def test_engine_profiles_migration_backfills_stories(scratch_database: str) -> None:
    """0025 seeds Lite, makes it the default and puts every existing story under it; the column
    keeps no server default. Downgrading with a profile's relations in place drops both tables,
    both columns and those relations; upgrading again restores Lite as the default."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0024")
    sync = make_sync_engine(scratch_database)
    story = "0000000000000000000000STR9"
    try:
        with sync.begin() as conn:
            conn.execute(
                text("INSERT INTO stories (id, owner_principal) VALUES (:s, 'p-1')"), {"s": story}
            )
        command.upgrade(cfg, "0025")
        with sync.begin() as conn:
            assert (
                conn.execute(
                    text("SELECT engine_profile_id FROM stories WHERE id = :s"), {"s": story}
                ).scalar_one()
                == LITE_PRESET_ID
            )
            assert (
                conn.execute(
                    text("SELECT default_engine_profile_id FROM deployment_state")
                ).scalar_one()
                == LITE_PRESET_ID
            )
            default = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'stories' AND column_name = 'engine_profile_id'"
                )
            ).scalar_one()
            assert default is None
            conn.execute(
                text(
                    "INSERT INTO entity_relations (entity_type, entity_id, principal, relation, "
                    "created_by) VALUES ('engine_profile', :p, 'p-d', 'editor', 'p-1')"
                ),
                {"p": LITE_PRESET_ID},
            )
        command.downgrade(cfg, "0024")
        with sync.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('engine_profiles')")).scalar() is None
            relations = conn.execute(
                text("SELECT count(*) FROM entity_relations WHERE entity_type = 'engine_profile'")
            ).scalar_one()
            assert relations == 0
        command.upgrade(cfg, "0025")
        with sync.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT default_engine_profile_id FROM deployment_state")
                ).scalar_one()
                == LITE_PRESET_ID
            )
    finally:
        sync.dispose()


async def test_library_matches_rest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The library's profile calls take the same requests and give the same answers as REST,
    and refuse the same way."""
    ctx = RequestContext(principal=A)
    async with respond_client(stub_settings, engine) as (client, lib):
        created = await lib.create_profile(ctx, _library_body(profile_body()))
        profile = created.profile.id
        rest = await ok(await client.get(f"{PROFILES}/{profile}", headers=READ))
        assert rest == created.profile.model_dump(mode="json")
        edited = await lib.update_profile(
            ctx, profile, ProfileUpdate.model_validate({"name": "Brisk Lite", "settings": EDITED})
        )
        assert edited.applied
        assert edited.result is not None
        assert edited.result.profile.version == 2
        copied = await lib.copy_profile(ctx, profile, ProfileCopy(name="Mine"), version=1)
        assert copied.profile.settings == SETTINGS
        checked = await lib.validate_profile(ctx, profile, ProfileValidate())
        assert checked.valid
        with pytest.raises(InvalidProfile):
            await lib.validate_profile(
                ctx, profile, ProfileValidate(settings={"pipeline": "structured"})
            )
        versions = await lib.list_profile_versions(ctx, profile)
        assert [v.version for v in versions] == [1, 2]
        deleted = await lib.delete_profile(ctx, profile)
        assert (deleted.changed, deleted.warnings) == (True, ())
        again = await lib.delete_profile(RequestContext(principal=B), profile)
        assert (again.changed, again.deleted_at) == (False, deleted.deleted_at)
        page = await lib.list_profiles(ctx, owner=A, include_deleted=True)
        assert profile in [item.id for item in page.items]
