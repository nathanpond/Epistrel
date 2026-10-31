"""U-28 (M5) and C-16 (M5) (docs/04): M5's modules delivered, their dependency rules, the seeded
``Structured`` profile's version 2, and every M5 module gated by its effective level (#181).

C-16 (M5) and U-28 (M5) have exactly this file (traceability allows one file per test ID and
milestone).

- ``test_u_28__m5_dependencies``: each FR-PROF-4 rule an M5 module takes part in, now on the
  delivered path, violated by a fixture that is refused naming what it lacks and accepted with
  it present; docs/04's three names (nested beliefs without partitions, partitions with
  perception ``none``, perception ``co_location`` without partitions) on copies of the seeded
  version 1; the partition dials refused only when set (the defaults validate under shared
  knowledge); per-character renders need the ``structured`` pipeline.
- ``test_c_16__m5_capabilities``: the delivered schema, the route and the tags publish the M5
  modules and their three new dials; the capability map and the guarantee table hold them; the
  seeded profile is ``Structured`` at version 2 with every M5 module on, its version 1 kept; a
  story pinned to version 1 moves to version 2 through the two-stage report, which lists the
  modules and guarantees it gains, and a story with history cannot (``upgrade_unavailable``);
  the M5 story settings are tagged and a scenario may set them; a scenario recommending an M5
  module the profile leaves off warns.
- ``test_c_16__m5_seed_pins``: migration ``0080`` on a database with a story following
  ``Structured (M4)``: the story is pinned to version 1, the profile renamed with version 2.
- ``test_c_16__m5_gating``: with every M5 module off (the seeded version 1), a structured turn
  writes no belief, exposure or divergence, routes no perception and filters no audience, and a
  request naming two characters is 422 ``one_render_character``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.catalog.scenario_models import StorySettingsDefaults
from epistrel.core.db import make_engine
from epistrel.core.epistemic import perception
from epistrel.core.genesis.settings_map import (
    MULTI_CHARACTER,
    PROMPT_ONLY,
    SETTING_CAPABILITIES,
    unmapped_settings,
)
from epistrel.core.ids import new_ulid
from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import (
    CAPABILITY,
    MULTI_CHARACTER_SETTINGS,
    holding,
    module_on,
)
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.guarantees import GUARANTEE, guarantees, guarantees_gained
from epistrel.core.profiles.impact import Recommendation, unmet
from epistrel.core.profiles.validate import (
    RequiredModule,
    missing_dependencies,
    parse_settings,
    validate_profile,
)
from epistrel.core.seeds import profiles as seeds
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.settings_schema import (
    CharacterSettings,
    ConfigClass,
    StorySettings,
    setting_class,
    untagged,
)
from epistrel.testing.profiles import structured, structured_m4
from epistrel.tools import guarantees as guarantee_tool
from epistrel.tools.capability_map import MAP_PATH, render
from tests import perception_support as ps
from tests.auth_support import auth_settings, bearer
from tests.conftest import alembic_config
from tests.event_support import rows, scalar
from tests.profile_support import (
    M5_MODULES,
    PROFILES,
    STRUCTURED_DOCUMENT,
    delivered_rules,
    merged,
    nest,
    violating,
)
from tests.profile_support import holding as holding_doc
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, WRITE
from tests.structured_turn_support import beat, structured_story
from tests.structured_turn_support import render as beat_render

A = "p-capability-map-m5"
GAINED = ["lazy_resolution", "multi_character", "nested_beliefs", "partitions", "perception"]
"""The modules a story moving from version 1 to version 2 gains (#109's ``delta``: names)."""
M5_GUARANTEES = (
    "structural_knowledge_separation",
    "audience_filtering",
    "multi_character_isolation",
)
NEW_DIALS = {
    "character_knowledge.max_adjudications_per_turn": (2, 0, 10),
    "character_knowledge.max_lazy_resolutions_per_turn": (20, 0, 200),
    "multi_character.interjection_max_words": (6, 1, 20),
}
"""The three dials #181 adds: default, minimum, maximum."""
M5_STORY_SETTINGS = {
    "interaction_mode": ConfigClass.prospective,
    "sequence_order": ConfigClass.prospective,
    "simultaneous_tie_break": ConfigClass.prospective,
    "player_authority": ConfigClass.interpretive,
    "player_view": ConfigClass.prospective,
    "reactive_interjections": ConfigClass.prospective,
}
"""The story settings M5 adds, with their classes (#64's table; ``player_authority`` is
interpretive as #180 built it: a structured turn's handling of its input depends on it)."""


def headers(principal: str = A) -> dict[str, str]:
    return {**bearer(), "X-Epistrel-Principal": principal}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _m5_rules() -> list[schema.DependencyRule]:
    """The delivered FR-PROF-4 rules an M5 module takes part in."""
    return [
        rule
        for rule in delivered_rules()
        if rule.module in M5_MODULES or any(r.module in M5_MODULES for r in rule.requires)
    ]


def _v1(*documents: dict[str, Any]) -> dict[str, Any]:
    """A copy of the seeded version 1 (``Structured (M4)``) with ``documents`` over it."""
    return merged(seeds.structured_m4_settings(), *documents)


# -- U-28 (M5) -----------------------------------------------------------------------------------


@pytest.mark.part("M5", "FR-PROF-4")
async def test_u_28__m5_dependencies(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Each delivered rule an M5 module takes part in (rules 3-7, 15, 17-21) is exercised by a
    fixture that violates it, every other rule of its module met: validation names the missing
    dependency, and the REST dry run is 422 ``invalid_profile`` naming it; with the dependency
    present it validates (complement). docs/04's three: nested beliefs without partitions,
    partitions with perception ``none``, perception ``co_location`` without partitions, each a
    copy of the seeded version 1 with one violation. The partition dials are refused only when
    set: the deployment's defaults, every M5 dial at its default, validate under shared
    knowledge. Per-character renders need ``pipeline = structured``."""
    rules = _m5_rules()
    named = {(rule.module, tuple(r.module for r in rule.requires)) for rule in rules}
    assert {
        ("partitions", ("pipeline",)),
        ("nested_beliefs", ("pipeline",)),
        ("lazy_resolution", ("pipeline",)),
        ("testimony", ("pipeline",)),
        ("perception", ("pipeline",)),
        ("multi_character", ("pipeline",)),
        ("partitions", ("perception",)),
        ("perception", ("partitions",)),
        ("nested_beliefs", ("partitions",)),
        ("lazy_resolution", ("partitions",)),
        ("testimony", ("partitions",)),
    } == named
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
            if not needs_pipeline:  # under `structured`, nothing else is missing
                assert len(found) == 1, rule.describe()
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
            if rule.module in ("nested_beliefs", "lazy_resolution", "testimony"):
                present = merged(present, nest("perception.level", "co_location"))
            assert validate_profile(present).valid, rule.describe()

        # docs/04's three, on copies of the seeded version 1.
        cases: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = [
            (
                {"character_knowledge": {"nested_beliefs": {"enabled": True, "depth": 2}}},
                "nested_beliefs",
                "partitions",
                {"character_knowledge": {"partitions": True}, "perception": {"level": "full"}},
            ),
            (
                {"character_knowledge": {"partitions": True}, "perception": {"level": "none"}},
                "partitions",
                "perception",
                {"perception": {"level": "co_location"}},
            ),
            (
                {"perception": {"level": "co_location"}},
                "perception",
                "partitions",
                {"character_knowledge": {"partitions": True}},
            ),
        ]
        for violation, module, missing, fix in cases:
            fixture = _v1(violation)
            found = missing_dependencies(parse_settings(fixture))
            assert [(f.module, [r.module for r in f.requires]) for f in found] == [
                (module, [missing])
            ], module
            dry = await client.post(
                f"{PROFILES}/{structured_m4()}/validate",
                json={"settings": fixture},
                headers=headers(),
            )
            assert dry.status_code == 422, dry.text
            assert f"the profile cannot run: {module}" in dry.json()["error"]["message"]
            fixed = _v1(violation, fix)
            assert validate_profile(fixed).valid, module
            checked = await client.post(
                f"{PROFILES}/{structured_m4()}/validate",
                json={"settings": fixed},
                headers=headers(),
            )
            assert checked.status_code == 200, checked.text
            assert checked.json()["valid"] is True

    # The partition dials are refused only when set: their defaults validate under shared.
    defaults = seeds.profile_defaults()
    assert validate_profile(defaults).valid
    assert validate_profile(merged(defaults, STRUCTURED_DOCUMENT)).valid
    assert (
        validate_profile(
            merged(defaults, STRUCTURED_DOCUMENT, nest("character_knowledge.lazy_resolution", True))
        ).valid
        is False
    )
    # Per-character renders need the structured pipeline (rule 15).
    lite = merged(seeds.lite_settings(), nest("multi_character.renders", "per_character"))
    (renders,) = missing_dependencies(parse_settings(lite))
    assert (renders.module, [r.module for r in renders.requires]) == (
        "multi_character",
        ["pipeline"],
    )


# -- C-16 (M5) -----------------------------------------------------------------------------------


@pytest.mark.part("M5", "FR-PROF-12,13")
async def test_c_16__m5_capabilities(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """M5's modules are delivered at the levels #106 declared, and published: the delivered
    schema, ``GET /engine-profiles/schema`` (with every leaf's three tags) and the OpenAPI
    document carry them, and #106's tags are kept. The three new dials are tagged, bounded and
    defaulted. The capability map's M5 areas (partitions and its dials, perception, per-character
    renders with their story settings) are delivered and render; the guarantee table's M5 rows
    name no milestone and hold under ``Structured``, which renders at version 2.

    The seeded profile is ``Structured`` (same id), version 2 with every M5 module on and its
    budgets raised; version 1 is ``Structured (M4)``'s settings. A story pinned to version 1 sees
    nothing of version 2; ``version: latest`` without ``ignore_warnings`` reports the modules and
    guarantees it gains and changes nothing; confirmed, it runs on version 2, unpinned. A story
    with history is ``upgrade_unavailable`` and keeps version 1.

    The M5 story settings are registered with their classes (participation in
    ``CharacterSettings``), mapped to ``multi_character`` (``player_view`` and
    ``player_authority`` are applied everywhere; ``player_authority`` is prompt-only under Lite),
    and a scenario's ``story_settings_defaults`` accepts them. A recommended ``partitions``,
    ``nested_beliefs`` or ``perception`` the profile leaves off is unmet (a warning)."""
    # The modules and their tags.
    m5 = {
        "partitions": {schema.ON},
        "nested_beliefs": {schema.ON},
        "lazy_resolution": {schema.ON},
        "testimony": {"model", "defaulted"},
        "perception": {"co_location", "full"},
        "multi_character": {"per_character"},
    }
    assert {name: set(schema.DELIVERED[name]) for name in m5} == m5
    assert schema.missing_tags() == {}
    tags = schema.setting_tags()
    data, behavioral = schema.Tag.data_producing, schema.Tag.behavioral
    for path in (
        "character_knowledge.partitions",
        "character_knowledge.nested_beliefs.enabled",
        "perception.level",
    ):
        assert tags[path].tag is data, path
    for path in ("character_knowledge.lazy_resolution", "character_knowledge.testimony"):
        assert tags[path].tag is behavioral, path
    assert (tags["multi_character.renders"].tag, tags["multi_character.renders"].change_class) == (
        behavioral,
        ConfigClass.prospective,
    )
    for path in (
        "character_knowledge.partitions",
        "character_knowledge.lazy_resolution",
        "character_knowledge.testimony",
        "perception.level",
    ):
        assert tags[path].change_class is ConfigClass.interpretive, path
    published = schema.delivered_schema()["properties"]
    for path, (default, low, high) in NEW_DIALS.items():
        group, leaf = path.split(".")
        node = published[group]["properties"][leaf]
        assert (node["default"], node["minimum"], node["maximum"]) == (default, low, high), path
        assert tags[path].quality is schema.Quality.relevant, path
    assert published["perception"]["properties"]["level"]["enum"] == ["none", "co_location", "full"]
    assert published["multi_character"]["properties"]["renders"]["enum"] == [
        "combined",
        "per_character",
    ]

    # The capability map and the guarantee table.
    assert render() == MAP_PATH.read_text(encoding="utf-8")
    for slug in (
        "partitions",
        "partitions_nested",
        "partitions_lazy",
        "partitions_testimony",
        "perception_co_location",
        "perception_full",
        "per_character_renders",
    ):
        assert CAPABILITY[slug].delivered, slug
    assert CAPABILITY["per_character_renders"].story_settings == MULTI_CHARACTER_SETTINGS
    assert "player_authority" in MULTI_CHARACTER_SETTINGS
    v1 = merged_settings(seeds.profile_defaults(), seeds.STRUCTURED_M4_SETTINGS)
    v2 = merged_settings(seeds.profile_defaults(), seeds.STRUCTURED_SETTINGS)
    assert set(CAPABILITY) - set(holding(v1)) >= {
        "partitions",
        "perception_co_location",
        "per_character_renders",
    }
    assert {"partitions", "partitions_nested", "partitions_lazy", "partitions_testimony"} <= set(
        holding(v2)
    )
    assert {"perception_co_location", "perception_full", "per_character_renders"} <= set(
        holding(v2)
    )
    for row in ("structural_knowledge_separation", "audience_filtering", "belief_adjudication"):
        assert GUARANTEE[row].delivered, row
        assert GUARANTEE[row].delivered_in is None, row
    assert GUARANTEE["paraphrase_detection"].metric == "NFR-QUAL-1"
    assert set(M5_GUARANTEES) <= set(guarantees(v2))
    assert not set(M5_GUARANTEES) & set(guarantees(v1))
    assert set(M5_GUARANTEES) <= set(guarantees_gained(v1, v2))
    assert GUARANTEE["audience_filtering"].provided(v2, {"player_view": "story"}) is False
    assert GUARANTEE["audience_filtering"].provided(v2, {"player_view": "character"}) is True
    (published_structured,) = [p for p in guarantee_tool.SEEDED if p.profile_id == structured()]
    assert (published_structured.name, published_structured.version) == (
        seeds.STRUCTURED_NAME,
        2,
    )
    table = guarantee_tool.render()
    assert table == (guarantee_tool.TABLE_PATH).read_text(encoding="utf-8")
    assert f"### {seeds.STRUCTURED_NAME}\n\nProfile `{structured()}`, version 2 as seeded." in table
    assert "`player_authority`" in table.split("### Lite", 1)[1].split("### ", 1)[0]

    # The story settings.
    assert untagged(StorySettings) == []
    assert untagged(CharacterSettings) == []
    for name, expected in M5_STORY_SETTINGS.items():
        assert setting_class(StorySettings, name) is expected, name
    assert setting_class(CharacterSettings, "participation.role") is ConfigClass.prospective
    assert unmapped_settings() == []
    for name in ("interaction_mode", "sequence_order", "simultaneous_tie_break"):
        assert SETTING_CAPABILITIES[name] == MULTI_CHARACTER, name
    assert SETTING_CAPABILITIES["reactive_interjections"] == MULTI_CHARACTER
    assert SETTING_CAPABILITIES["player_view"] is None
    assert SETTING_CAPABILITIES["player_authority"] is None
    assert "player_authority" in PROMPT_ONLY
    defaults = StorySettingsDefaults.model_validate(
        {
            "interaction_mode": "simultaneous",
            "sequence_order": "director",
            "simultaneous_tie_break": "request_order",
            "reactive_interjections": True,
            "player_authority": "co_author",
            "player_view": "story",
        }
    )
    assert (defaults.sequence_order, defaults.reactive_interjections) == ("director", True)
    with pytest.raises(ValueError, match="simultaneous_tie_break"):
        StorySettingsDefaults.model_validate({"simultaneous_tie_break": "loudest"})
    recommended = Recommendation(("partitions", "nested_beliefs", "perception"), "scn@1")
    assert [u.capability for u in unmet(recommended, v1)] == [
        "partitions",
        "nested_beliefs",
        "perception",
    ]
    co_location = merged_settings(
        seeds.profile_defaults(),
        seeds.STRUCTURED_M4_SETTINGS,
        {"character_knowledge": {"partitions": True}, "perception": {"level": "co_location"}},
    )
    assert [u.capability for u in unmet(recommended, co_location)] == ["nested_beliefs"]
    assert unmet(recommended, v2) == []

    async with respond_client(stub_settings, engine) as (client, _):
        published_route = (await client.get(f"{PROFILES}/schema", headers=READ)).json()
        for name, levels in m5.items():
            assert set(published_route["delivered"][name]) == levels, name
        for path in NEW_DIALS:
            assert published_route["tags"][path] == {
                "tag": "behavioral",
                "class": "prospective",
                "quality": "relevant",
            }, path
        openapi = (await client.get("/openapi.json")).json()["components"]["schemas"]
        assert "max_adjudications_per_turn" in str(openapi["ProfileCreate"])
        assert "guarantees_gained" in openapi["ImpactReport"]["properties"]

        # The seeded profile.
        head = (await client.get(f"{PROFILES}/{structured()}", headers=READ)).json()
        assert (head["name"], head["version"]) == (seeds.STRUCTURED_NAME, 2)
        assert head["settings"] == seeds.structured_settings()
        first = (
            await client.get(f"{PROFILES}/{structured()}", params={"version": 1}, headers=READ)
        ).json()
        assert first["settings"] == seeds.structured_m4_settings()
        assert module_on(
            parse_settings(merged(seeds.profile_defaults(), head["settings"])), "partitions"
        )

        # A turn-less story pinned to version 1 moves to version 2 through the report.
        story, _ = await structured_story(client, "Wren", "Ada")
        before = (await client.get(f"/stories/{story}/effective-config", headers=READ)).json()
        assert before["profile"] == {
            "id": structured(),
            "version": 1,
            "pinned": True,
            "deleted": False,
        }
        assert before["effective"]["character_knowledge"]["partitions"] is False
        move = {"profile_id": structured(), "version": "latest"}
        held = await client.put(f"/stories/{story}/engine-profile", json=move, headers=WRITE)
        assert held.status_code == 200, held.text
        report = held.json()
        assert report["applied"] is False
        impact = report["impact"]
        (entry,) = impact["affected_stories"]
        assert set(GAINED) <= set(entry["capabilities_gained"])
        assert "testimony" in entry["capabilities_gained"]
        assert set(M5_GUARANTEES) <= set(impact["guarantees_gained"])
        assert "capabilities_gained" in impact["warnings"]
        assert entry["upgrade_unavailable"] is False
        still = (await client.get(f"/stories/{story}/effective-config", headers=READ)).json()
        assert still["profile"]["version"] == 1
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
            "version": 2,
            "pinned": False,
            "deleted": False,
        }
        assert after["effective"]["perception"]["level"] == "full"
        pins = await rows(
            engine,
            "SELECT old, new FROM config_changes WHERE scope_id = :s "
            "AND setting = 'engine_profile_version_pin' ORDER BY change_id",
            s=story,
        )
        assert [(r.old, r.new) for r in pins] == [(None, 1), (1, None)]
        again = await client.put(
            f"/stories/{story}/engine-profile",
            json={**move, "ignore_warnings": True},
            headers=WRITE,
        )
        assert again.json()["applied"] is True  # nothing to change: nothing is written
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM config_changes WHERE scope_id = :s "
                "AND setting = 'engine_profile_version_pin'",
                s=story,
            )
            == 2
        )
        unknown = await client.put(
            f"/stories/{story}/engine-profile",
            json={"profile_id": structured(), "version": 99, "ignore_warnings": True},
            headers=WRITE,
        )
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["error"]["details"]["field"] == "body.version"

        # A story with history keeps version 1.
        played, ids = await structured_story(client, "Cal", "Bobby")
        turn = await respond(client, played, body(ids["Cal"], "Evening, Bobby."))
        assert turn.status_code == 200, turn.text
        kept = await client.put(
            f"/stories/{played}/engine-profile",
            json={**move, "ignore_warnings": True},
            headers=WRITE,
        )
        assert kept.status_code == 200, kept.text
        (entry,) = kept.json()["impact"]["affected_stories"]
        assert entry["upgrade_unavailable"] is True
        effective = (await client.get(f"/stories/{played}/effective-config", headers=READ)).json()
        assert effective["profile"]["version"] == 1


async def _story_row(db: AsyncEngine) -> str:
    """A story row as revision 0079 has it, on the Lite preset: today's genesis writes tables a
    later revision adds (#193), and the migration reads only the row."""
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


@pytest.mark.part("M5", "FR-PROF-12,13")
async def test_c_16__m5_seed_pins(scratch_database: str) -> None:
    """Migration ``0080`` updates the seeded profile in place: on a database where a story
    follows ``Structured (M4)`` (version 1, unpinned) and another runs Lite, the story on it is
    pinned to version 1, the profile is renamed ``Structured`` with version 2 (every M5 module
    on) and the id is unchanged; the Lite story is untouched; the deployment's defaults gain
    every M5 setting at its off value. No configuration change is recorded (no story's
    configuration changes). Downgrading restores the name, the version and the pin."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0079")
    db = make_engine(auth_settings(scratch_database))
    try:
        on_profile, lite = await _story_row(db), await _story_row(db)
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET engine_profile_id = :p WHERE id = :s"),
                {"p": structured_m4(), "s": on_profile},
            )
        changes = await scalar(db, "SELECT count(*) FROM config_changes")
        command.upgrade(cfg, "head")
        head = (
            await rows(
                db,
                "SELECT name, current_version FROM engine_profiles WHERE id = :p",
                p=structured(),
            )
        )[0]
        assert (head.name, head.current_version) == (seeds.STRUCTURED_NAME, 2)
        version = await scalar(
            db,
            "SELECT settings FROM engine_profile_versions WHERE profile_id = :p AND version = 2",
            p=structured(),
        )
        assert version == seeds.structured_settings()
        pins = {
            r.id: r.engine_profile_version_pin
            for r in await rows(db, "SELECT id, engine_profile_version_pin FROM stories")
        }
        assert (pins[on_profile], pins[lite]) == (1, None)
        defaults = await scalar(db, "SELECT profile_defaults FROM deployment_state")
        assert defaults["perception"] == {"level": "none"}
        assert defaults["multi_character"] == {"renders": "combined", "interjection_max_words": 6}
        assert defaults["character_knowledge"]["max_lazy_resolutions_per_turn"] == 20
        assert await scalar(db, "SELECT count(*) FROM config_changes") == changes

        command.downgrade(cfg, "0079")
        head = (
            await rows(
                db,
                "SELECT name, current_version FROM engine_profiles WHERE id = :p",
                p=structured(),
            )
        )[0]
        assert (head.name, head.current_version) == (seeds.STRUCTURED_M4_NAME, 1)
        assert (
            await scalar(
                db, "SELECT engine_profile_version_pin FROM stories WHERE id = :s", s=on_profile
            )
            is None
        )
        assert "perception" not in await scalar(db, "SELECT profile_defaults FROM deployment_state")
    finally:
        await db.dispose()


@pytest.mark.part("M5", "FR-PROF-12,13")
async def test_c_16__m5_gating(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every M5 module reads its level from the story's effective configuration and is inert
    when off. On the seeded version 1 (``character_knowledge`` shared, perception ``none``,
    ``multi_character`` combined) a structured turn, with the characters placed together and a
    render that speaks, writes no ``beliefs``, ``exposures`` or ``divergences`` row and calls no
    perception routing (``route_perception``, ``route_turn``); the character viewpoint's answer
    states no audience filter. A request naming two characters is 422 ``validation_error``
    (``one_render_character``) exactly as in M4. Complement: a story switched to partitions with
    perception ``full`` before its first turn routes that turn's beats."""
    calls: dict[str, int] = {"route_perception": 0, "route_turn": 0}

    def spy(name: str) -> Callable[..., Any]:
        real = getattr(perception, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            calls[name] += 1
            return real(*args, **kwargs)

        return counted

    for name in calls:
        monkeypatch.setattr(perception, name, spy(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Bobby", "Jenni")
        study = await ps.place_entity(engine, story, "study")
        for name in ("Cal", "Bobby", "Jenni"):
            await ps.put(engine, story, ids[name], study)
        stub.when(
            "## You are Bobby\n",
            beat_render(beat("say", "Jenni, the lamp is still on.", intent="sincere")),
            purpose="generation",
        )
        answer = await respond(
            client,
            story,
            body(
                ids["Cal"],
                "What now?",
                render_character_ids=[ids["Bobby"]],
                output_viewpoint=f"character:{ids['Cal']}",
            ),
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["audience_filter"] == "none"
        for table in ("beliefs", "exposures", "divergences"):
            assert (
                await scalar(
                    engine,
                    f"SELECT count(*) FROM {table} WHERE story_id = :s",  # noqa: S608
                    s=story,
                )
                == 0
            ), table
        assert calls == {"route_perception": 0, "route_turn": 0}

        two = await respond(
            client,
            story,
            body(ids["Cal"], "Both of you?", render_character_ids=[ids["Bobby"], ids["Jenni"]]),
        )
        assert two.status_code == 422, two.text
        error = two.json()["error"]
        assert (error["code"], error["details"]["reason"]) == (
            "validation_error",
            "one_render_character",
        )

        # Complement: a story switched to partitions with full perception before its first
        # turn routes the turn's beats.
        other, others = await structured_story(client, "Cal", "Bobby", "Jenni")
        await ps.perceive(engine, other, "full")
        room = await ps.place_entity(engine, other, "study")
        for name in ("Cal", "Bobby", "Jenni"):
            await ps.put(engine, other, others[name], room)
        routed = await respond(
            client,
            other,
            body(
                others["Cal"],
                "And now?",
                render_character_ids=[others["Bobby"]],
                output_viewpoint=f"character:{others['Cal']}",
            ),
        )
        assert routed.status_code == 200, routed.text
        assert calls["route_perception"] + calls["route_turn"] > 0
        assert routed.json()["audience_filter"] != "none"


def test_inactive_multi_settings_are_promoted() -> None:
    """A story created before M5 delivered per-character renders keeps its scenario's
    multi-character settings under ``settings.inactive`` (#122); a turn that runs them reads
    them from there (#181) where the story has no value of its own: the order, the tie-break,
    interjections and each role's participation. A value of the story's own wins, a fixed or
    explicitly set role is never replaced, and a story without them reads the defaults."""
    from epistrel.core.turns import CastEntry
    from epistrel.orchestrator.structured import multi
    from epistrel.orchestrator.structured import roles as roles_rt

    inactive = {
        "inactive": {
            "interaction_mode": {"value": "simultaneous", "capability": MULTI_CHARACTER},
            "sequence_order": {"value": "director", "capability": MULTI_CHARACTER},
            "simultaneous_tie_break": {"value": "director", "capability": MULTI_CHARACTER},
            "reactive_interjections": {"value": True, "capability": MULTI_CHARACTER},
            "participation.mother": {"value": "primary", "capability": MULTI_CHARACTER},
            "participation.father": {"value": "silent", "capability": MULTI_CHARACTER},
        }
    }
    story = StorySettings.from_row(inactive)
    assert multi.mode_of(story) == "simultaneous"
    assert multi.order_of(story) == "director"
    assert multi.tie_break_of(story) == ("director", None)
    assert roles_rt.interjections_of(story) is True
    own = StorySettings.from_row(
        {**inactive, "sequence_order": "request", "reactive_interjections": False}
    )
    assert multi.order_of(own) == "request"
    assert roles_rt.interjections_of(own) is False
    plain = StorySettings.from_row({})
    assert (multi.order_of(plain), multi.tie_break_of(plain)) == (
        "request",
        ("request_order", None),
    )
    assert roles_rt.interjections_of(plain) is False

    cast = [
        CastEntry("p", "Sam", "", is_player=True, role_id="player"),
        CastEntry("m", "Mother", "", is_player=False, role_id="mother"),
        CastEntry("f", "Father", "", is_player=False, role_id="father", participation_fixed=True),
        CastEntry("b", "Brother", "", is_player=False, role_id="brother"),
    ]
    found = roles_rt.stored_of(cast, story)
    assert {cid: (p.role, p.fixed) for cid, p in found.items()} == {
        "m": ("primary", False),
        "f": ("reactive", True),
        "b": ("reactive", False),
    }
    assert {cid: p.role for cid, p in roles_rt.stored_of(cast).items()} == {
        "m": "reactive",
        "f": "reactive",
        "b": "reactive",
    }
