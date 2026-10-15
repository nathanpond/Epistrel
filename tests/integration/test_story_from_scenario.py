"""Creating a story from a scenario (#119): S-AGE-ASSIGNMENT's M3 part and the detailed tests.

- ``test_s_age_assignment__eligibility`` (S-AGE-ASSIGNMENT's M3 part, FR-SCN-3): a role's
  eligibility age range refuses a catalog character outside it and accepts one inside.

The other M3 parts of story creation live in their test IDs' files (traceability counts a test
ID per milestone in one file): I-22 in ``test_rating_change.py``, I-17 in ``test_i_17.py``, I-27
in ``test_i_27.py``, I-21 in ``test_i_21.py``, U-26 in ``tests/test_u_26.py``, C-19 in
``test_personas.py``, C-20 in ``tests/conformance/test_c_20.py`` and S-COMPANION in
``test_companion.py``. The detailed tests below name no requirement ID, so they are not parts.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from tests import genesis_support as g
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings
from tests.catalog_support import character as catalog_character
from tests.profile_support import PROFILES, profile_body
from tests.respond_support import body as respond_body
from tests.respond_support import played, respond_client
from tests.story_support import create, rest, story_body

pytestmark = pytest.mark.integration

ROLES = (g.role("mother"), g.role("player", kind="player"))
DEFAULT_PROFILE = LITE_PRESET_ID
"""The deployment's default engine profile (the seeded Lite preset)."""


@pytest.fixture
async def client(pg_url: str, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (found, _):
        yield found


async def _scenario(client: httpx.AsyncClient, *roles: dict[str, Any], **groups: Any) -> str:
    scenario_id, _ = await g.published_scenario(
        client, g.scenario_body(*(roles or ROLES), **groups)
    )
    return scenario_id


async def _one(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


@pytest.mark.part("M3", "FR-SCN-3")
async def test_s_age_assignment__eligibility(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A role whose eligibility age range is ``[25, 40]`` refuses a catalog character aged 19
    (``eligibility_failed`` at the role's age range, naming the character version) with nothing
    written, and accepts one aged 30. The player's persona is never held to the range.

    Complement asserted: the refused request, with only the character changed, succeeds."""
    young = await g.catalog_character_id(client, "Kit Young", age=19)
    grown = await g.catalog_character_id(client, "Kit Grown", age=30)
    scenario_id = await _scenario(
        client, g.role("mother", eligibility={"age_range": [25, 40]}), ROLES[1]
    )
    request = g.from_scenario(
        scenario_id, slots={"mother": g.catalog_slot(young)}, players={"player": g.player(age=12)}
    )
    counted = await g.counts(engine)
    status, code, details = await g.refused(client, request)
    assert (status, code) == (422, "eligibility_failed")
    (failed,) = details["report"]["errors"]
    assert failed["location"]["field"] == "cast.roles[0].eligibility.age_range"
    assert failed["source"] == f"{young}@1"
    assert await g.counts(engine) == counted
    made = await g.create_from(client, {**request, "slots": {"mother": g.catalog_slot(grown)}})
    assert made["applied"] is True


# -- the request -------------------------------------------------------------------------------


async def test_a_blank_story_is_still_created_without_a_snapshot(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """``POST /stories`` without ``scenario`` is #50's blank story, unchanged: no scenario, no
    snapshot, no roles, no catalog source."""
    made = await create(client, story_body("Wren", "Ada"))
    assert made["scenario"] is None
    assert made["report"] is None
    assert all("role_id" not in c for c in made["characters"])
    assert (
        await _one(
            engine, "SELECT scenario_snapshot FROM stories WHERE id = :s", s=made["story_id"]
        )
        is None
    )
    assert (
        await _one(
            engine,
            "SELECT count(*) FROM character_snapshots WHERE story_id = :s",
            s=made["story_id"],
        )
        == 0
    )
    read = await client.get(f"/stories/{made['story_id']}", headers=g.READ)
    assert read.json()["scenario"] is None
    assert all("source" not in c for c in read.json()["characters"])


async def test_request_shape_errors(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """``characters`` beside a scenario, an unknown role, an unknown scenario or character, a
    slot naming both kinds, an override outside the story settings defaults and a duplicate
    name are each 422 ``validation_error`` at the field, with nothing written."""
    mara = await g.catalog_character_id(client, "Mara Venn")
    twin = await g.catalog_character_id(client, "Mara Venn")
    scenario_id = await _scenario(
        client, g.role("mother"), g.role("aunt", required=False), ROLES[1]
    )
    base = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
    cases = [
        ({**base, "characters": []}, "body.characters"),
        ({**base, "slots": {**base["slots"], "cousin": g.catalog_slot(mara)}}, "body.slots.cousin"),
        ({**base, "scenario": {"id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"}}, "body.scenario.id"),
        (
            {**base, "slots": {"mother": g.catalog_slot("01J9Z8Q4N5V6W7X8Y9Z0A1B2C3")}},
            "body.slots.mother.catalog_id",
        ),
        (
            {**base, "slots": {"mother": {**g.catalog_slot(mara), **g.ad_hoc_slot("X", 30)}}},
            "body.slots.mother",
        ),
        ({**base, "overrides": {"world.description": "x"}}, "body.overrides.world.description"),
        (
            {**base, "slots": {"mother": g.catalog_slot(mara), "aunt": g.catalog_slot(twin)}},
            "body.slots.aunt",
        ),
        ({**base, "scenario": {"id": scenario_id, "version": 9}}, "body.scenario.version"),
    ]
    for request, field in cases:
        counted = await g.counts(engine)
        status, code, details = await g.refused(client, request)
        assert (status, code, details["field"]) == (422, "validation_error", field), request
        assert await g.counts(engine) == counted


async def test_unpublished_scenario_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A scenario with no published version starts no story (422 ``not_published``), and
    neither does a published one whose open role is filled with a catalog character that was
    created but never published; each refusal writes nothing.

    Complement asserted: the same request with a published character succeeds."""
    created = await client.post("/catalog/scenarios", json=g.scenario_body(), headers=g.WRITE)
    assert created.status_code == 201, created.text
    counted = await g.counts(engine)
    status, code, _ = await g.refused(client, g.from_scenario(created.json()["id"]))
    assert (status, code) == (422, "not_published")
    assert await g.counts(engine) == counted

    draft = await client.post(
        "/catalog/characters",
        json=catalog_character("Nell Draft", definition={"true_age": 41}),
        headers=scenarios.WRITE,
    )
    assert draft.status_code == 201, draft.text
    scenario_id = await _scenario(client)
    request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(draft.json()["id"])})
    counted = await g.counts(engine)
    status, code, details = await g.refused(client, request)
    assert (status, code) == (422, "not_published")
    assert g.report_codes(details) == ["not_published"]
    assert await g.counts(engine) == counted
    mara = await g.catalog_character_id(client, "Mara Venn")
    made = await g.create_from(client, {**request, "slots": {"mother": g.catalog_slot(mara)}})
    assert made["applied"] is True


async def test_several_errors_answer_the_first_by_precedence(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A request that leaves a required role empty, reassigns a locked one and overrides a
    field the scenario does not allow is one 422: ``slot_required`` (the first by precedence),
    with every error in ``details.report``."""
    tom = await g.catalog_character_id(client, "Tom Harrow")
    ivy = await g.catalog_character_id(client, "Ivy Harrow")
    scenario_id = await _scenario(
        client,
        g.role("mother"),
        g.role("brother", fill="locked", character={"catalog_id": tom, "version": 1}),
        ROLES[1],
    )
    request = g.from_scenario(
        scenario_id,
        slots={"brother": g.catalog_slot(ivy)},
        overrides={"story_settings_defaults.turn_concurrency": "queue"},
    )
    status, code, details = await g.refused(client, request)
    assert (status, code) == (422, "slot_required")
    assert sorted(g.report_codes(details)) == [
        "locked_slot_reassigned",
        "override_not_allowed",
        "slot_required",
    ]
    assert details["report"]["counts"]["errors"] == 3


async def test_strict_refuses_on_a_personality_warning(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A role text that asserts personality is a ``personality_in_role`` warning: the
    creation succeeds and reports it under ``full``; with ``strict`` it is refused
    (``creation_conflicts``) with the full report and nothing written. Under ``card`` the
    report holds counts only."""
    mara = await g.catalog_character_id(client, "Mara Venn")
    scenario_id = await _scenario(
        client,
        g.role("mother", setup_description="She is a habitual liar."),
        ROLES[1],
    )
    request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
    card = await g.create_from(client, request)
    assert card["report"]["counts"]["warnings"] == 1
    assert card["report"]["warnings"] is None
    full = await g.create_from(client, {**request, "projection": "full"})
    assert [w["code"] for w in full["report"]["warnings"]] == ["personality_in_role"]
    counted = await g.counts(engine)
    status, code, details = await g.refused(client, {**request, "strict": True})
    assert (status, code) == (422, "creation_conflicts")
    assert [w["code"] for w in details["report"]["warnings"]] == ["personality_in_role"]
    assert await g.counts(engine) == counted


async def test_ad_hoc_role_filters_warn_unverifiable(client: httpx.AsyncClient) -> None:
    """An ad-hoc character in a role with a label filter is held to the age range only: the
    label filter is the warning ``eligibility_unverifiable``."""
    scenario_id = await _scenario(
        client,
        g.role("mother", eligibility={"labels": ["warm"], "age_range": [25, 60]}),
        ROLES[1],
    )
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id, slots={"mother": g.ad_hoc_slot("June", 50)}, projection="full"
        ),
    )
    assert [w["code"] for w in made["report"]["warnings"]] == ["eligibility_unverifiable"]
    status, code, _ = await g.refused(
        client, g.from_scenario(scenario_id, slots={"mother": g.ad_hoc_slot("June", 61)})
    )
    assert (status, code) == (422, "eligibility_failed")


# -- what genesis writes -----------------------------------------------------------------------


async def test_genesis_writes_settings_policies_and_events(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The story's settings come from the scenario's defaults with the overrides (the default
    viewpoint's role becomes that role's character), its policies are the scenario's as
    ``story`` policies, its name is the scenario's, and the log holds ``story_created`` (naming
    the scenario version) and one ``character_defined`` per cast member (role and catalog
    source, never the definition). A catalog character's definition, safe fallback and
    deflection style are copied from its version; its public description is not."""
    mara = await g.catalog_character_id(
        client,
        "Mara Venn",
        definition={"deflection_style": "turns to the tide tables"},
        public_description="SHOWN-ON-THE-CARD-ONLY",
    )
    scenario_id = await _scenario(
        client,
        *ROLES,
        story_settings_defaults={
            "turn_concurrency": "reject",
            "default_output_viewpoint": "mother",
            "policies": [
                {
                    "name": "No real addresses",
                    "rule": {"type": "wordlist", "words": ["Elm Street"]},
                    "message": "Keep the address vague.",
                }
            ],
        },
        creation_rules={"overridable": ["story_settings_defaults"]},
    )
    made = await g.create_from(
        client,
        g.from_scenario(
            scenario_id,
            slots={"mother": g.catalog_slot(mara)},
            overrides={"story_settings_defaults.turn_concurrency": "queue"},
        ),
    )
    story_id = made["story_id"]
    mother = next(c["id"] for c in made["characters"] if c["role_id"] == "mother")
    read = (await client.get(f"/stories/{story_id}", headers=g.READ)).json()
    assert read["name"] == "Sunday Dinner"
    assert read["settings"]["turn_concurrency"] == "queue"
    assert read["settings"]["default_output_viewpoint"] == f"character:{mother}"
    assert read["scenario"] == made["scenario"]
    assert [c.get("source") for c in read["characters"]] == [
        {"catalog_id": mara, "version": 1},
        None,
    ]
    async with engine.connect() as conn:
        policy = (
            await conn.execute(
                text("SELECT scope, scope_id, name FROM policies WHERE story_id = :s"),
                {"s": story_id},
            )
        ).one()
        row = (
            await conn.execute(
                text(
                    "SELECT definition, deflection_style, safe_fallback_text FROM characters "
                    "WHERE id = :c"
                ),
                {"c": mother},
            )
        ).one()
        logged = (
            await conn.execute(
                text("SELECT type, payload FROM events WHERE story_id = :s ORDER BY seq"),
                {"s": story_id},
            )
        ).all()
    assert tuple(policy) == ("story", story_id, "No real addresses")
    assert "SHOWN-ON-THE-CARD-ONLY" not in row.definition
    assert "patient, private and slow to trust" in row.definition
    assert row.deflection_style == "turns to the tide tables"
    assert [r.type for r in logged] == [
        "story_created",
        "character_defined",
        "character_defined",
        "message_rendered",
    ]
    assert logged[0].payload["scenario"] == made["scenario"]
    assert logged[1].payload["source"] == {"catalog_id": mara, "version": 1}
    assert logged[1].payload["role_id"] == "mother"
    assert "definition" not in logged[1].payload
    assert logged[3].payload["text"] == g.OPENING


async def test_a_scenario_story_plays_a_turn(client: httpx.AsyncClient) -> None:
    """A story created from a scenario plays like any other: the opening narration is turn 1
    and the first respond is turn 2, voiced by the catalog character from its snapshot."""
    mara = await g.catalog_character_id(client, "Mara Venn")
    scenario_id = await _scenario(client)
    made = await g.create_from(
        client, g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
    )
    ids = {c["role_id"]: c["id"] for c in made["characters"]}
    turn = await played(client, made["story_id"], respond_body(ids["player"]))
    assert turn["turn"] == 2
    assert turn["timeline_revision"] == 3


async def test_the_recommended_profile_is_the_story_profile(client: httpx.AsyncClient) -> None:
    """A scenario that recommends an assignable engine profile creates its story under that
    profile when the request names none: the creation answer and the story read agree.

    Complement asserted: a request naming the deployment default overrides the
    recommendation."""
    profile = await client.post(PROFILES, json=profile_body(), headers=g.WRITE)
    assert profile.status_code == 201, profile.text
    profile_id = profile.json()["id"]
    assert profile_id != DEFAULT_PROFILE
    mara = await g.catalog_character_id(client, "Mara Venn")
    scenario_id = await _scenario(client, recommendation={"engine_profile_id": profile_id})
    request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
    made = await g.create_from(client, request)
    assert made["engine_profile"]["id"] == profile_id
    read = await client.get(f"/stories/{made['story_id']}", headers=g.READ)
    assert read.json()["engine_profile"]["id"] == profile_id
    chosen = await g.create_from(client, {**request, "engine_profile_id": DEFAULT_PROFILE})
    assert chosen["engine_profile"]["id"] == DEFAULT_PROFILE


async def test_an_unassignable_recommended_profile_falls_back(client: httpx.AsyncClient) -> None:
    """A scenario that recommends a profile that does not exist creates its story under the
    deployment default, with the warning ``profile_unavailable``; a request naming an unknown
    profile is 422 ``validation_error``."""
    mara = await g.catalog_character_id(client, "Mara Venn")
    scenario_id = await _scenario(client, recommendation={"engine_profile_id": "no-such-profile"})
    request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
    made = await g.create_from(client, {**request, "projection": "full"})
    assert [w["code"] for w in made["report"]["warnings"]] == ["profile_unavailable"]
    assert made["engine_profile"]["id"] == DEFAULT_PROFILE
    status, code, details = await g.refused(
        client, {**request, "engine_profile_id": "00000000000000000000PRF999"}
    )
    assert (status, code, details["field"]) == (422, "validation_error", "body.engine_profile_id")


async def test_a_catalog_cast_character_promotes_from_its_snapshot(
    client: httpx.AsyncClient,
) -> None:
    """Promoting a character a story was created with from the catalog copies its snapshot's
    typed definition (#114's hand-off), not the plain text the story renders: the draft's
    ``personality`` is the catalog version's, and nothing lands in ``background_flavor``."""
    mara = await g.catalog_character_id(client, "Mara Venn")
    scenario_id = await _scenario(client)
    made = await g.create_from(
        client, g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(mara)})
    )
    mother = next(c["id"] for c in made["characters"] if c["role_id"] == "mother")
    promoted = await client.post(
        f"/stories/{made['story_id']}/characters/{mother}/promote", json={}, headers=g.WRITE
    )
    assert promoted.status_code == 201, promoted.text
    draft = await client.get(
        f"/catalog/characters/{promoted.json()['catalog_id']}",
        params={"draft": "true", "projection": "full"},
        headers=g.READ,
    )
    assert draft.status_code == 200, draft.text
    definition = draft.json()["definition"]
    assert definition["personality"] == "Mara Venn is patient, private and slow to trust."
    assert definition["background_flavor"] is None
    assert definition["true_age"] == 41


def _impact_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == "impact report"
    ]


async def test_creation_logs_its_impact_report(
    settings: Settings, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Creation from a scenario runs through the shared two stages (``impact.two_stage``, #351)
    like the other two-stage mutations: a held creation logs one INFO ``impact report`` line,
    ``applied: false`` with its warning codes, and the confirmed one logs one line, ``applied:
    true``. The lines carry counts and codes only: no character name is in them (invariant 3)."""
    caplog.set_level(logging.INFO, logger="epistrel.core.profiles.impact")
    async with rest(settings, engine) as client:
        locked, default = await scenarios.cast_characters(client)
        warm = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        scenario_id, _ = await g.published_scenario(
            client, scenarios.complete_with(locked, default)
        )
        request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(warm)})

        caplog.clear()
        held = await g.create_from(client, request, status=200)
        assert held["applied"] is False
        (line,) = _impact_lines(caplog)
        assert line["applied"] is False
        assert line["warnings"]
        assert "June Harrow" not in repr(line)

        caplog.clear()
        made = await g.create_from(client, {**request, "ignore_warnings": True})
        assert made["applied"] is True
        assert [(x["applied"], x["warnings"]) for x in _impact_lines(caplog)] == [
            (True, line["warnings"])
        ]


async def test_creation_takes_either_spelling_of_the_confirm_flag(
    settings: Settings, engine: AsyncEngine
) -> None:
    """``POST /stories`` from a scenario is a two-stage request like the other three (FR-PROF-7,
    #336): ``ignoreWarnings`` is the camelCase spelling of ``ignore_warnings``, and sending both
    with different values is 422 ``validation_error``.

    Complements asserted: the held request and the disagreeing one write no row."""
    async with rest(settings, engine) as client:
        locked, default = await scenarios.cast_characters(client)
        warm = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        scenario_id, _ = await g.published_scenario(
            client, scenarios.complete_with(locked, default)
        )
        request = g.from_scenario(scenario_id, slots={"mother": g.catalog_slot(warm)})

        counted = await g.counts(engine)
        held = await g.create_from(client, request, status=200)
        assert held["applied"] is False
        assert await g.counts(engine) == counted

        status, code, _ = await g.refused(
            client, {**request, "ignore_warnings": False, "ignoreWarnings": True}
        )
        assert (status, code) == (422, "validation_error")
        assert await g.counts(engine) == counted

        made = await g.create_from(client, {**request, "ignoreWarnings": True})
        assert made["applied"] is True
        assert made["story_id"] is not None
        assert (await g.counts(engine))["stories"] == counted["stories"] + 1
