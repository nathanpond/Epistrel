"""U-28 (M6) and C-16 (M6) (docs/04): M6's modules delivered, their dependency rules, the seeded
``Structured`` profile's version 3, and the clock and States gated by their effective levels
(#203).

U-28 (M6) and C-16 (M6) have exactly this file (traceability allows one file per test ID and
milestone).

- ``test_u_28__m6``: each FR-PROF-4 rule an M6 module takes part in, now on the delivered path,
  violated by a fixture that is refused naming what it lacks and accepted with it present;
  docs/04's two (time-claim verification without the clock; the clock in a ``structured``
  profile without commitment) on copies of the seeded version 2; the prompt-only forms under
  ``lite`` (FR-LITE-5) validate and ``on`` does not.
- ``test_c_16__m6``: the delivered schema and the route publish the clock and States with #106's
  tags; the capability map and the guarantee table hold them; the seeded profile is
  ``Structured`` at version 3 (version 2 kept, budgets unchanged); a turn-less story pinned to
  version 2 moves to version 3 through the two-stage report and gains the modules (a new story
  on it gets the clock and States from genesis), and a story with history is
  ``upgrade_unavailable``.
- ``test_c_16__m6_seed_pins``: migration ``0104`` on a database with a story following
  ``Structured``: the story is pinned to version 2, the profile gains version 3 and the
  deployment's defaults every M6 setting at its off value; downgrading restores them.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import CAPABILITY, holding
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.guarantees import GUARANTEE, guarantees, guarantees_gained
from epistrel.core.profiles.validate import (
    RequiredModule,
    missing_dependencies,
    parse_settings,
    validate_profile,
)
from epistrel.core.seeds import profiles as seeds
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.testing.profiles import structured
from epistrel.tools import guarantees as guarantee_tool
from epistrel.tools.capability_map import MAP_PATH, render
from tests.auth_support import auth_settings, bearer
from tests.conftest import alembic_config
from tests.event_support import rows, scalar
from tests.profile_support import (
    PROFILES,
    REDERIVATION,
    STRUCTURED_DOCUMENT,
    delivered_rules,
    merged,
    nest,
    violating,
)
from tests.profile_support import holding as holding_doc
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, WRITE
from tests.structured_turn_support import structured_story

A = "p-capability-map-m6"
M6_MODULES = frozenset({"clock", "time_claims", "scheduled_stops", "states"})
GAINED = ["clock", "scheduled_stops", "states", "time_claims"]
"""What a story moving from version 2 to version 3 gains (#109's ``delta``: names)."""
M6_GUARANTEES = ("state_effect_validation", "clock_pacing", "time_estimation")


def headers(principal: str = A) -> dict[str, str]:
    return {**bearer(), "X-Epistrel-Principal": principal}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _m6_rules() -> list[schema.DependencyRule]:
    """The delivered FR-PROF-4 rules an M6 module takes part in."""
    return [
        rule
        for rule in delivered_rules()
        if rule.module in M6_MODULES or any(r.module in M6_MODULES for r in rule.requires)
    ]


def _v2(*documents: dict[str, Any]) -> dict[str, Any]:
    """A copy of the seeded version 2 (M5's ``Structured``) with ``documents`` over it."""
    return merged(seeds.structured_m5_settings(), *documents)


# -- U-28 (M6) -----------------------------------------------------------------------------------


@pytest.mark.part("M6", "FR-PROF-4")
async def test_u_28__m6(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Each delivered rule an M6 module takes part in (rules 11-14, 22-24) is exercised by a
    fixture that violates it, every other rule of its module met: validation names the missing
    dependency, and the REST dry run is 422 ``invalid_profile`` naming it; with the dependency
    present it validates (complement). docs/04's two, each a copy of the seeded version 2 with
    one violation: time-claim verification without the clock, and the clock in a
    ``structured`` profile without commitment. Under ``lite`` the prompt-only clock and States
    (FR-LITE-5) validate and ``on`` names the ``structured`` pipeline; a stop needs the clock."""
    rules = _m6_rules()
    named = {(rule.module, tuple(r.module for r in rule.requires)) for rule in rules}
    assert named == {
        ("clock", ("pipeline",)),
        ("time_claims", ("pipeline",)),
        ("scheduled_stops", ("pipeline",)),
        ("states", ("pipeline",)),
        ("time_claims", ("clock", "verification")),
        ("scheduled_stops", ("clock",)),
        ("clock", ("commitment",)),
    }
    async with respond_client(stub_settings, engine) as (client, _):
        for rule in rules:
            expected = [
                RequiredModule(module=r.module, levels=list(r.levels)) for r in rule.requires
            ]
            needs_pipeline = any(r.module == "pipeline" for r in rule.requires)
            others = [
                holding_doc(r)
                for other in rules
                if other.module == rule.module and other is not rule
                for r in other.requires
            ]
            fixture = merged(
                {} if needs_pipeline else STRUCTURED_DOCUMENT, *others, violating(rule)
            )
            found = missing_dependencies(parse_settings(fixture))
            assert [(f.module, f.requires) for f in found if f.module == rule.module] == [
                (rule.module, expected)
            ], rule.describe()
            assert not validate_profile(fixture).valid, rule.describe()
            dry = await client.post(
                f"{PROFILES}/{structured()}/validate", json={"settings": fixture}, headers=headers()
            )
            assert dry.status_code == 422, dry.text
            error = dry.json()["error"]
            assert error["code"] == "invalid_profile"
            mine = [
                d for d in error["details"]["missing_dependencies"] if d["module"] == rule.module
            ]
            assert [d["requires"] for d in mine] == [[r.model_dump() for r in expected]]
            present = merged(fixture, *(holding_doc(r) for r in rule.requires))
            left = missing_dependencies(parse_settings(present))
            assert not [f for f in left if f.module == rule.module], rule.describe()

        # docs/04's two, on copies of the seeded version 2.
        cases: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = [
            (
                {"clock": {"time_claims": True}, "commitment": {"enabled": True}},
                "time_claims",
                "clock",
                {"clock": {"level": "on"}},
            ),
            (
                {"clock": {"level": "on"}, "commitment": {"enabled": False}},
                "clock",
                "commitment",
                {"commitment": {"enabled": True}},
            ),
        ]
        for violation, module, missing, fix in cases:
            fixture = _v2(violation)
            found = missing_dependencies(parse_settings(fixture))
            assert [(f.module, [r.module for r in f.requires]) for f in found] == [
                (module, [missing])
            ], module
            dry = await client.post(
                f"{PROFILES}/{structured()}/validate", json={"settings": fixture}, headers=headers()
            )
            assert dry.status_code == 422, dry.text
            assert f"the profile cannot run: {module}" in dry.json()["error"]["message"]
            fixed = _v2(violation, fix)
            assert validate_profile(fixed).valid, module
            checked = await client.post(
                f"{PROFILES}/{structured()}/validate", json={"settings": fixed}, headers=headers()
            )
            assert checked.status_code == 200, checked.text
            assert checked.json()["valid"] is True

    # Under lite: the prompt-only forms run; `on` names the structured pipeline.
    lite = merged(
        seeds.lite_settings(),
        {"clock": {"level": "prompt_only"}, "states": {"level": "prompt_only"}},
    )
    assert validate_profile(lite).valid
    for module, switch in (("clock", "clock.level"), ("states", "states.level")):
        (refused,) = missing_dependencies(parse_settings(merged(lite, nest(switch, "on"))))
        assert (refused.module, [r.module for r in refused.requires]) == (module, ["pipeline"])
    stops = merged(seeds.structured_settings(), {"clock": {"level": "off"}})
    assert {f.module for f in missing_dependencies(parse_settings(stops))} == {
        "time_claims",
        "scheduled_stops",
    }


# -- C-16 (M6) -----------------------------------------------------------------------------------


@pytest.mark.part("M6", "FR-PROF-12,13")
async def test_c_16__m6(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The clock and States are delivered at the levels #106 declared and published (the
    delivered schema, ``GET /engine-profiles/schema`` with #106's tags), with nothing patched.
    The capability map's ``clock`` and ``states_external`` rows are delivered (the latter
    noting FR-EXT-8, which arrives in M7) and render; the guarantee table's M6 rows name no
    milestone, hold under ``Structured`` at version 3 and not at version 2 or under Lite, and
    render.

    The seeded profile is ``Structured`` (same id) at version 3: version 2 plus the clock
    ``on`` with both flags and States ``on``, budgets unchanged (16 calls, 60 s); version 2 is
    kept. A turn-less story pinned to version 2 sees nothing of version 3; ``version: latest``
    without ``ignore_warnings`` reports the modules and guarantees it gains and changes nothing;
    confirmed, it runs on version 3 and gains the modules (its clock and States answer). A
    story with history is ``upgrade_unavailable`` (the modules are data-producing) and keeps
    version 2."""
    m6 = {
        "clock": {"prompt_only", schema.ON},
        "time_claims": {schema.ON},
        "scheduled_stops": {schema.ON},
        "states": {"prompt_only", schema.ON},
    }
    assert {name: set(schema.DELIVERED[name]) for name in m6} == m6
    assert schema.missing_tags() == {}
    tags = schema.setting_tags()
    for path in ("clock.level", "states.level"):
        assert tags[path].tag is schema.Tag.data_producing, path
    for path in ("clock.time_claims", "clock.scheduled_stops"):
        assert tags[path].tag is schema.Tag.behavioral, path
    published = schema.delivered_schema()["properties"]
    assert published["clock"]["properties"]["level"]["enum"] == ["off", "prompt_only", "on"]
    assert published["states"]["properties"]["level"]["enum"] == ["off", "prompt_only", "on"]

    # The capability map and the guarantee table.
    assert render() == MAP_PATH.read_text(encoding="utf-8")
    assert CAPABILITY["clock"].delivered
    assert CAPABILITY["states_external"].delivered
    assert "FR-EXT-8" in CAPABILITY["states_external"].notes
    v2 = merged_settings(seeds.profile_defaults(), seeds.STRUCTURED_M5_SETTINGS)
    v3 = merged_settings(seeds.profile_defaults(), seeds.STRUCTURED_SETTINGS)
    assert {"clock", "states_external"} <= set(holding(v3))
    assert not {"clock", "states_external"} & set(holding(v2))
    for row in M6_GUARANTEES:
        assert GUARANTEE[row].delivered, row
        assert GUARANTEE[row].delivered_in is None, row
    assert set(M6_GUARANTEES) <= set(guarantees(v3))
    assert not set(M6_GUARANTEES) & set(guarantees(v2))
    # The deterministic ones are what a report's gains name (#109).
    assert {"state_effect_validation", "clock_pacing"} <= set(guarantees_gained(v2, v3))
    lite = guarantee_tool.SEEDED[0]
    (seeded,) = [p for p in guarantee_tool.SEEDED if p.profile_id == structured()]
    # #227: the row is published as Full, at version 4 (the same modules as version 3).
    assert (seeded.name, seeded.version) == (seeds.FULL_NAME, seeds.FULL_VERSION)
    assert set(M6_GUARANTEES) <= set(guarantees(seeded.effective()))
    assert not set(M6_GUARANTEES) & set(guarantees(lite.effective()))
    table = guarantee_tool.render()
    assert table == guarantee_tool.TABLE_PATH.read_text(encoding="utf-8")
    assert f"Profile `{structured()}`, version {seeds.FULL_VERSION} as seeded." in table
    assert "prompt-only forms (FR-LITE-5)" in table.split("### Lite", 1)[1].split("### ", 1)[0]

    # The seeded profile's versions.
    assert seeds.STRUCTURED_VERSION == 3
    assert seeds.STRUCTURED_M5_VERSION == 2
    v3_doc = seeds.structured_settings()
    assert v3_doc == {
        **seeds.structured_m5_settings(),
        "clock": {"level": "on", "time_claims": True, "scheduled_stops": True},
        "states": {"level": "on"},
    }
    assert v3_doc["budgets"] == {"model_calls": 16, "deadline_ms": 60_000}
    assert validate_profile(v3_doc).valid

    async with respond_client(stub_settings, engine) as (client, _):
        route = (await client.get(f"{PROFILES}/schema", headers=READ)).json()
        for name, levels in m6.items():
            assert set(route["delivered"][name]) == levels, name
        assert route["tags"]["clock.level"]["tag"] == "data_producing"
        third = (
            await client.get(f"{PROFILES}/{structured()}", params={"version": 3}, headers=READ)
        ).json()
        assert third["settings"] == v3_doc
        second = (
            await client.get(f"{PROFILES}/{structured()}", params={"version": 2}, headers=READ)
        ).json()
        assert second["settings"] == seeds.structured_m5_settings()

        # A turn-less story pinned to version 2 moves to version 3 through the report.
        story, _cast = await structured_story(client, "Wren", "Ada")
        pin = {"profile_id": structured(), "version": 2, "ignore_warnings": True}
        pinned = await client.put(f"/stories/{story}/engine-profile", json=pin, headers=WRITE)
        assert pinned.status_code == 200, pinned.text
        before = (await client.get(f"/stories/{story}/effective-config", headers=READ)).json()
        assert before["profile"]["version"] == 2
        assert before["effective"]["clock"]["level"] == "off"
        refused = await client.get(f"/stories/{story}/states", headers=READ)
        assert refused.status_code == 409, refused.text
        move = {"profile_id": structured(), "version": "latest"}
        held = await client.put(f"/stories/{story}/engine-profile", json=move, headers=WRITE)
        assert held.status_code == 200, held.text
        report = held.json()
        assert report["applied"] is False
        (entry,) = report["impact"]["affected_stories"]
        assert set(GAINED) <= set(entry["capabilities_gained"])
        assert {"state_effect_validation", "clock_pacing"} <= set(
            report["impact"]["guarantees_gained"]
        )
        assert entry["upgrade_unavailable"] is False
        still = (await client.get(f"/stories/{story}/effective-config", headers=READ)).json()
        assert still["profile"]["version"] == 2
        moved = await client.put(
            f"/stories/{story}/engine-profile",
            json={**move, "ignore_warnings": True},
            headers=WRITE,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["applied"] is True
        after = (await client.get(f"/stories/{story}/effective-config", headers=READ)).json()
        assert after["profile"] == {
            "id": structured(),
            "version": seeds.FULL_VERSION,  # the latest: 3 in M6 (#227 adds 4)
            "pinned": False,
            "deleted": False,
        }
        assert after["effective"]["clock"] == {
            "level": "on",
            "time_claims": True,
            "scheduled_stops": True,
        }
        assert after["effective"]["states"] == {"level": "on"}
        # The story gained the modules (complement of the 409 above): its clock and States run.
        clock = await client.get(f"/stories/{story}/clock", headers=READ)
        assert clock.status_code == 200, clock.text
        assert clock.json()["level"] == "on"
        states = await client.get(
            f"/stories/{story}/states", params={"viewpoint": "omniscient"}, headers=READ
        )
        assert states.status_code == 200, states.text

        # A story with history keeps version 2.
        played, cast = await structured_story(client, "Cal", "Bobby")
        pinned = await client.put(f"/stories/{played}/engine-profile", json=pin, headers=WRITE)
        assert pinned.status_code == 200, pinned.text
        turn = await respond(client, played, body(cast["Cal"], "Evening, Bobby."))
        assert turn.status_code == 200, turn.text
        kept = await client.put(
            f"/stories/{played}/engine-profile",
            json={**move, "ignore_warnings": True},
            headers=WRITE,
        )
        # #228: the upgrade re-derives the story's history first; until then it keeps its version.
        assert kept.status_code == 202, kept.text
        (entry,) = kept.json()["impact"]["affected_stories"]
        assert (entry["upgrade_unavailable"], entry[REDERIVATION]["needed"]) == (False, True)
        effective = (await client.get(f"/stories/{played}/effective-config", headers=READ)).json()
        assert effective["profile"]["version"] == 2
        cancelled = await client.delete(f"/stories/{played}/{REDERIVATION}", headers=WRITE)
        assert cancelled.status_code == 200, cancelled.text


async def _story_row(db: AsyncEngine) -> str:
    """A story row as revision 0103 has it, on the Lite preset (the migration reads only the
    row)."""
    story = new_ulid()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO stories (id, owner_principal, engine_profile_id) "
                "VALUES (:s, 'p-test', :p)"
            ),
            {"s": story, "p": LITE_PRESET_ID},
        )
    return story


@pytest.mark.part("M6", "FR-PROF-12,13")
async def test_c_16__m6_seed_pins(scratch_database: str) -> None:
    """Migration ``0104`` updates the seeded profile in place: on a database where a story
    follows ``Structured`` (version 2, unpinned) and another runs Lite, the story on it is
    pinned to version 2, the profile gains version 3 (the clock and States on) with its id and
    name unchanged; the Lite story is untouched; the deployment's defaults gain every M6
    setting at its off value. No configuration change is recorded. Downgrading restores the
    version, the pin and the defaults."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0103")
    db = make_engine(auth_settings(scratch_database))
    try:
        on_profile, lite = await _story_row(db), await _story_row(db)
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET engine_profile_id = :p WHERE id = :s"),
                {"p": structured(), "s": on_profile},
            )
        changes = await scalar(db, "SELECT count(*) FROM config_changes")
        command.upgrade(cfg, "0104")
        (head,) = await rows(
            db, "SELECT name, current_version FROM engine_profiles WHERE id = :p", p=structured()
        )
        assert (head.name, head.current_version) == (seeds.STRUCTURED_NAME, 3)
        version = await scalar(
            db,
            "SELECT settings FROM engine_profile_versions WHERE profile_id = :p AND version = 3",
            p=structured(),
        )
        assert version == seeds.structured_settings()
        pins = {
            r.id: r.engine_profile_version_pin
            for r in await rows(db, "SELECT id, engine_profile_version_pin FROM stories")
        }
        assert (pins[on_profile], pins[lite]) == (2, None)
        defaults = await scalar(db, "SELECT profile_defaults FROM deployment_state")
        assert defaults["clock"] == {"level": "off", "time_claims": False, "scheduled_stops": False}
        assert defaults["states"] == {"level": "off"}
        assert validate_profile(defaults).valid
        assert await scalar(db, "SELECT count(*) FROM config_changes") == changes

        command.downgrade(cfg, "0103")
        (head,) = await rows(
            db, "SELECT name, current_version FROM engine_profiles WHERE id = :p", p=structured()
        )
        assert (head.name, head.current_version) == (seeds.STRUCTURED_NAME, 2)
        assert (
            await scalar(
                db, "SELECT engine_profile_version_pin FROM stories WHERE id = :s", s=on_profile
            )
            is None
        )
        found = await scalar(db, "SELECT profile_defaults FROM deployment_state")
        assert "clock" not in found
        assert "states" not in found
    finally:
        await db.dispose()
