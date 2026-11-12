"""#227: the Standard, Full and Max presets (U-28 M7), the conversion of the seeded
``Structured`` profile into Full, and the two dials and the routing sentinel Max needs
(``verification.shape_tightening``, ``commitment.salience_bias``, ``"@strongest"``)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.deployment import NULL_MEANS, NULLABLE
from epistrel.core.ids import new_ulid
from epistrel.core.profiles import schema, tiers
from epistrel.core.profiles.effective import merged_settings
from epistrel.core.profiles.fingerprint import ModelIdentity, RuntimeFacts, fingerprint
from epistrel.core.profiles.reservation import reservation
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.profiles.validate import validate_profile
from epistrel.core.seeds import profiles as seeds
from epistrel.core.settings_schema import DeploymentSettings, flatten
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.commit.salience import Beat, detect_salience
from epistrel.orchestrator.models.registry import SALIENCE_DETECTION, SceneContext
from epistrel.testing.profiles import full, max_, standard
from tests.auth_support import auth_settings
from tests.conftest import REPO_ROOT, alembic_config
from tests.escalation_support import (
    camp_render,
    camp_story,
    clear_gates,
    entailment,
    escalation_rows,
    play_camp,
    put_gate,
    regenerated,
)
from tests.event_support import rows, scalar
from tests.profile_support import PROFILES
from tests.respond_support import new_story, respond_client
from tests.story_support import READ, WRITE, override
from tests.verify_support import stub_models

pytestmark = pytest.mark.integration

PRESET_IDS = {
    "Lite": seeds.LITE_PRESET_ID,
    "Standard": seeds.STANDARD_PRESET_ID,
    "Full": seeds.FULL_PRESET_ID,
    "Max": seeds.MAX_PRESET_ID,
}
DOCUMENTS = {
    "Standard": seeds.standard_settings(),
    "Full": seeds.full_settings(),
    "Max": seeds.max_settings(),
}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def effective(document: dict[str, Any]) -> ProfileSettings:
    return merged_settings(seeds.profile_defaults(), document)


Check = Callable[[ProfileSettings], bool]

CLAUSES: dict[str, dict[str, Check]] = {
    "Standard": {
        "the structured pipeline": lambda s: s.pipeline == "structured",
        "per-character partitions": lambda s: s.character_knowledge.partitions,
        "`co_location` perception": lambda s: s.perception.level == "co_location",
        "declared-claim verification with gated extraction": lambda s: (
            s.verification.level == "extraction" and s.verification.extraction_mode == "gated"
        ),
        "contradiction checking (its contradiction_rules row)": lambda s: (
            s.verification.contradiction_checking
        ),
        "commitment on": lambda s: s.commitment.enabled,
        "the world clock on": lambda s: s.clock.level == "on",
        "consolidation on": lambda s: s.memory.consolidation.enabled,
        "one repair attempt": lambda s: s.repair.max_attempts == 1,
        "moderate budgets": lambda s: (s.budgets.model_calls, s.budgets.deadline_ms) == (8, 45_000),
    },
    "Full": {
        "the structured pipeline": lambda s: s.pipeline == "structured",
        "every module on": lambda s: all(
            (
                s.memory.episodic.enabled,
                s.memory.episodic.hybrid_search,
                s.memory.consolidation.enabled,
                s.commitment.enabled,
                s.clock.level == "on",
                s.clock.time_claims,
                s.clock.scheduled_stops,
                s.states.level == "on",
            )
        ),
        "entailment verification, extraction always, gate 1.0": lambda s: (
            (
                s.verification.level,
                s.verification.extraction_mode,
                s.verification.entailment_gate,
            )
            == ("entailment", "always", 1.0)
        ),
        "partitions with nested beliefs to depth 2": lambda s: (
            s.character_knowledge.partitions
            and s.character_knowledge.nested_beliefs.enabled
            and s.character_knowledge.nested_beliefs.depth == 2
        ),
        "full perception": lambda s: s.perception.level == "full",
        "per-character renders": lambda s: s.multi_character.renders == "per_character",
    },
    "Max": {
        "every uncertain verdict escalates": lambda s: (
            s.verification.level == "entailment"
            and s.verification.entailment_gate == 1.0
            and s.verification.shape_tightening == "off"
        ),
        "recall-biased salience": lambda s: s.commitment.salience_bias == "recall",
        "the strongest configured model for each verification purpose": lambda s: all(
            s.models.purposes[p].model == tiers.STRONGEST for p in seeds.MAX_STRONGEST_PURPOSES
        ),
        "more repair attempts": lambda s: s.repair.max_attempts == 3,
        "frequent consolidation": lambda s: s.memory.consolidation.cadence_turns == 10,
        "generous budgets": lambda s: (
            (s.budgets.model_calls, s.budgets.deadline_ms) == (24, 120_000)
        ),
    },
}
"""FR-PROF-3's clauses, each with the setting assertion that proves it."""


@pytest.mark.part("M7", "FR-PROF-3")
async def test_u_28__m7(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The three M7 presets validate (as Lite does), and every FR-PROF-3 clause holds of each
    (``CLAUSES``). Max dominates Full on every quality dial (calls, deadline, repair attempts
    at least Full's; a consolidation cadence at most Full's), Standard's budgets are below
    Full's and still fund a two-primary turn under gated extraction (#174's ``P + B + C + G``).
    None raises ``below_safety_minimum`` at the deployment's guardrail count (#456). Through
    the API: ``?preset=true`` lists the four; a copy is editable and not a preset; an edit of
    Full makes version n+1 and leaves version n readable. Complement: a copy of Standard with
    nested beliefs but no partitions is refused 422 ``invalid_profile`` naming the missing
    dependency, so the validator is not vacuous."""
    for name, document in {"Lite": seeds.lite_settings(), **DOCUMENTS}.items():
        assert validate_profile(document).valid, name
    for name, clauses in CLAUSES.items():
        settings = effective(DOCUMENTS[name])
        for clause, check in clauses.items():
            assert check(settings), (name, clause)
    standard_s, full_s, max_s = (effective(DOCUMENTS[n]) for n in ("Standard", "Full", "Max"))
    assert (full_s.budgets.model_calls or 0) < (max_s.budgets.model_calls or 0)
    assert full_s.budgets.deadline_ms < max_s.budgets.deadline_ms
    assert full_s.repair.max_attempts <= max_s.repair.max_attempts
    assert max_s.memory.consolidation.cadence_turns <= full_s.memory.consolidation.cadence_turns
    assert (
        max_s.verification.entailment_min_confidence
        >= full_s.verification.entailment_min_confidence
    )
    assert (standard_s.budgets.model_calls or 0) < (full_s.budgets.model_calls or 0)
    assert standard_s.budgets.deadline_ms < full_s.budgets.deadline_ms
    two_primaries = (
        2 + reservation(standard_s, guardrail_input=1, guardrail_output=2, renders=2).total
    )
    assert two_primaries <= (standard_s.budgets.model_calls or 0)
    # Clause tables are not vacuous: a dial off breaks its clause.
    broken = effective({**DOCUMENTS["Standard"], "perception": {"level": "full"}})
    assert not CLAUSES["Standard"]["`co_location` perception"](broken)

    async with respond_client(stub_settings, engine) as (client, _):
        listed = (await client.get(PROFILES, params={"preset": "true"}, headers=READ)).json()
        assert {item["id"] for item in listed["items"]} == set(PRESET_IDS.values())
        assert {item["name"] for item in listed["items"]} == set(PRESET_IDS)
        others = (await client.get(PROFILES, params={"preset": "false"}, headers=READ)).json()
        assert not set(PRESET_IDS.values()) & {item["id"] for item in others["items"]}
        for preset in (standard(), full(), max_()):
            copied = await client.post(f"{PROFILES}/{preset}/copy", json={}, headers=WRITE)
            assert copied.status_code == 201, copied.text
            made = copied.json()
            assert made["preset"] is False
            assert made["name"].endswith(" (copy)")
            assert made["warnings"] == []  # #456: no below_safety_minimum
            source = (await client.get(f"{PROFILES}/{preset}", headers=READ)).json()
            assert made["settings"] == source["settings"]
            edited = await client.put(
                f"{PROFILES}/{made['id']}",
                json={
                    "name": made["name"],
                    "settings": {**made["settings"], "budgets": {"deadline_ms": 50_000}},
                    "ignore_warnings": True,
                },
                headers=WRITE,
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["result"]["version"] == 2

        broken_copy = await client.post(
            PROFILES,
            json={
                "name": "Broken Standard",
                "settings": {
                    **DOCUMENTS["Standard"],
                    "character_knowledge": {
                        "partitions": False,
                        "nested_beliefs": {"enabled": True},
                    },
                    "perception": {"level": "none"},
                },
            },
            headers=WRITE,
        )
        assert broken_copy.status_code == 422, broken_copy.text
        error = broken_copy.json()["error"]
        assert error["code"] == "invalid_profile"
        assert any(
            d["module"] == "nested_beliefs"
            and {"module": "partitions", "levels": []} in d["requires"]
            for d in error["details"]["missing_dependencies"]
        ), error

        head = (await client.get(f"{PROFILES}/{full()}", headers=READ)).json()
        n = head["version"]
        assert (head["name"], head["preset"], n) == (seeds.FULL_NAME, True, seeds.FULL_VERSION)
        try:
            moved = await client.put(
                f"{PROFILES}/{full()}",
                json={
                    "name": seeds.FULL_NAME,
                    "settings": {**head["settings"], "budgets": {"model_calls": 17}},
                    "ignore_warnings": True,
                },
                headers=WRITE,
            )
            assert moved.status_code == 200, moved.text
            assert moved.json()["result"]["version"] == n + 1
            assert moved.json()["result"]["preset"] is True
            earlier = (
                await client.get(f"{PROFILES}/{full()}", params={"version": n}, headers=READ)
            ).json()
            assert earlier["settings"] == seeds.full_settings()
        finally:
            await _restore_full(engine, n)


async def _restore_full(engine: AsyncEngine, version: int) -> None:
    """Put Full back on ``version`` (the shared database's other tests read it as seeded)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE engine_profiles SET current_version = :v WHERE id = :p"),
            {"v": version, "p": full()},
        )
        await conn.execute(
            text("DELETE FROM engine_profile_versions WHERE profile_id = :p AND version > :v"),
            {"v": version, "p": full()},
        )


def _delta(before: ProfileSettings, after: ProfileSettings) -> dict[str, schema.Tag]:
    old = flatten(ProfileSettings, before.model_dump(mode="json"))
    new = flatten(ProfileSettings, after.model_dump(mode="json"))
    tags = schema.setting_tags()
    return {path: tags[path].tag for path in new if new[path] != old.get(path)}


async def _story(db: AsyncEngine, profile: str, *, history: bool = False) -> str:
    story = new_ulid()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO stories (id, owner_principal, engine_profile_id) "
                "VALUES (:s, 'p-test', :p)"
            ),
            {"s": story, "p": profile},
        )
        if history:
            await conn.execute(
                text(
                    "INSERT INTO operations (operation_id, story_id, kind, narrative_turn, "
                    "turn_variant, principal, status, membership, committed_at) VALUES "
                    "(:o, :s, 'turn', 1, 0, 'p-test', 'committed', 'active', now())"
                ),
                {"o": new_ulid(), "s": story},
            )
    return story


async def test_presets__structured_to_full_delta(scratch_database: str) -> None:
    """Migration ``0110`` converts ``Structured`` (version 3) into Full in place. The delta of
    v3 to Full over the seeded defaults is classified with #106's tags: it raises no
    data-producing module and, over defaults that name #227's two dials at their defaults, has
    no behavioral leaf either (so the pickup half has nothing to observe), and so the migration
    takes the no-pin branch: a following story with history stays unpinned (it is never given a
    data-producing increase outside the two-stage flow, because there is none), a story ``0104``
    pinned to version 2 keeps its pin (its effective configuration still resolves version 2),
    no job is enqueued, and the version is recorded as one ``engine_profile:<id>`` row 3 → 4 by
    ``system:migration:0110`` with ``config_revision`` moved once. Standard and Max are seeded,
    and the defaults gain the two dials. Downgrade restores ``Structured`` v3."""
    defaults = seeds.profile_defaults()
    delta = _delta(
        merged_settings(defaults, seeds.structured_settings()),
        merged_settings(defaults, seeds.full_settings()),
    )
    assert delta == {}
    assert (
        schema.data_increases(
            merged_settings(defaults, seeds.structured_settings()),
            merged_settings(defaults, seeds.full_settings()),
        )
        == []
    )
    stored = _delta(
        ProfileSettings.model_validate({**seeds.structured_settings(), "pipeline": "structured"}),
        ProfileSettings.model_validate(seeds.full_settings()),
    )
    assert stored == {}  # the two dials are named at their defaults

    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "0109")
    db = make_engine(auth_settings(scratch_database))
    try:
        following = await _story(db, full(), history=True)
        fresh = await _story(db, full())
        pinned = await _story(db, full())
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET engine_profile_version_pin = 2 WHERE id = :s"),
                {"s": pinned},
            )
        revision = await scalar(db, "SELECT config_revision FROM deployment_state")
        jobs = await scalar(db, "SELECT count(*) FROM jobs")
        command.upgrade(cfg, "0110")
        (head,) = await rows(
            db,
            "SELECT name, preset, current_version FROM engine_profiles WHERE id = :p",
            p=full(),
        )
        assert (head.name, head.preset, head.current_version) == (
            seeds.FULL_NAME,
            True,
            seeds.FULL_VERSION,
        )
        version = await scalar(
            db,
            "SELECT settings FROM engine_profile_versions WHERE profile_id = :p AND version = 4",
            p=full(),
        )
        assert version == seeds.full_settings()
        pins = {
            r.id: r.engine_profile_version_pin
            for r in await rows(db, "SELECT id, engine_profile_version_pin FROM stories")
        }
        assert (pins[following], pins[fresh], pins[pinned]) == (None, None, 2)
        assert await scalar(db, "SELECT count(*) FROM jobs") == jobs
        assert await scalar(db, "SELECT config_revision FROM deployment_state") == revision + 1
        changes = await rows(
            db,
            'SELECT scope, setting, old, new, "class" AS cls FROM config_changes '
            "WHERE actor = 'system:migration:0110' ORDER BY change_id",
        )
        assert [(c.scope, c.setting, c.old, c.new) for c in changes] == [
            ("deployment", f"engine_profile:{full()}", 3, 4)
        ]
        presets = await rows(
            db,
            "SELECT id, name, preset, owner_principal, current_version FROM engine_profiles "
            "WHERE id IN (:a, :b) ORDER BY id",
            a=standard(),
            b=max_(),
        )
        assert [tuple(p) for p in presets] == [
            (standard(), seeds.STANDARD_NAME, True, "system", 1),
            (max_(), seeds.MAX_NAME, True, "system", 1),
        ]
        found = await scalar(db, "SELECT profile_defaults FROM deployment_state")
        assert found["verification"]["shape_tightening"] == "on"
        assert found["commitment"]["salience_bias"] == "balanced"
        assert validate_profile(found).valid

        command.downgrade(cfg, "0109")
        (head,) = await rows(
            db,
            "SELECT name, preset, current_version FROM engine_profiles WHERE id = :p",
            p=full(),
        )
        assert (head.name, head.preset, head.current_version) == (
            seeds.STRUCTURED_NAME,
            False,
            3,
        )
        assert (
            await scalar(
                db,
                "SELECT count(*) FROM engine_profiles WHERE id IN (:a, :b)",
                a=standard(),
                b=max_(),
            )
            == 0
        )
        found = await scalar(db, "SELECT profile_defaults FROM deployment_state")
        assert "shape_tightening" not in found["verification"]
        assert "salience_bias" not in found["commitment"]
    finally:
        await db.dispose()


async def test_presets__shape_tightening(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """``verification.shape_tightening``: with a live tightened gate (0.25) for the camp claim's
    shape (its confidence 0.5), ``on`` (the default) skips the entailment call
    (``skipped_by_gate``), and ``off`` ignores the tightening, so the claim escalates at the
    profile's 1.0 gate (one entailment call)."""
    await clear_gates(engine)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        edu = await camp_story(client, engine)
        stub.script("generation", [camp_render()])
        stub.script("entailment", [entailment("contradicted")])
        stub.script("regeneration", [regenerated()])
        await play_camp(client, edu)
        (first,) = await escalation_rows(engine, edu.story_id)
        shape = first.predicate_shape
        try:
            live = await put_gate(client, shape, 0.25, "live", ignore_warnings=True)
            assert live.status_code == 200, live.text
            for setting, calls in (("on", 0), ("off", 1)):
                edu = await camp_story(client, engine)
                await override(
                    client, edu.story_id, {"verification": {"shape_tightening": setting}}
                )
                stub.reset()
                stub.script("generation", [camp_render()])
                stub.script("entailment", [entailment("contradicted")])
                stub.script("regeneration", [regenerated()])
                await play_camp(client, edu)
                assert [c.purpose for c in stub.calls].count("entailment") == calls, setting
                (row,) = [
                    r for r in await escalation_rows(engine, edu.story_id) if r.pass_ == "render"
                ]
                assert row.skipped_by_gate is (calls == 0), setting
        finally:
            await clear_gates(engine, [shape])


SCENE = SceneContext(present_entities=["Bobby"], last_beats=[])


def _low_confidence_answer() -> str:
    return json.dumps(
        {
            "salient": [
                {
                    "beat": 1,
                    "span": "Someone knocks",
                    "proposition_sketch": {"subject": "someone", "relation": "knocks"},
                }
            ],
            "texture": [{"beat": 2, "span": "A lamp flickers"}],
            "confidence": 0.2,
        }
    )


async def test_presets__salience_bias() -> None:
    """``commitment.salience_bias``: a ``salience_detection`` answer whose confidence (0.2) is
    below ``uncertain_below`` (0.5) is no answer under ``balanced`` (every beat texture,
    ``salience_failed``), and under ``recall`` every span counts salient: the salient span with
    the model's sketch, the texture span promoted with its own words. Complement: a confident
    answer (0.9) is read as given under ``recall`` (its texture stays texture)."""
    beats = [
        Beat(1, "narration", "Someone knocks at the door."),
        Beat(2, "narration", "A lamp flickers in the hall."),
    ]
    mal = stub_models(uncertain_below=0.5)
    mal.stub.script(SALIENCE_DETECTION.name, [_low_confidence_answer()])
    balanced = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=4))
    assert (balanced.salient, balanced.degradation) == ((), ("salience_failed",))

    mal = stub_models(uncertain_below=0.5)
    mal.stub.script(SALIENCE_DETECTION.name, [_low_confidence_answer()])
    recall = await detect_salience(
        beats, SCENE, models=mal, budget=TurnBudget(max_calls=4), bias="recall"
    )
    assert recall.promoted is True
    assert [(s.span.beat, s.proposition_sketch.relation) for s in recall.salient] == [
        (1, "knocks"),
        (2, "A lamp flickers"),
    ]
    assert recall.texture == ()

    confident = json.loads(_low_confidence_answer()) | {"confidence": 0.9}
    mal = stub_models(uncertain_below=0.5)
    mal.stub.script(SALIENCE_DETECTION.name, [json.dumps(confident)])
    sure = await detect_salience(
        beats, SCENE, models=mal, budget=TurnBudget(max_calls=4), bias="recall"
    )
    assert (sure.promoted, [s.span.beat for s in sure.salient], [t.beat for t in sure.texture]) == (
        False,
        [1],
        [2],
    )


async def test_presets__strongest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``"@strongest"``: a story on Max routes its verification purposes to the deployment's
    ``model_tiers.strongest``, and its effective configuration and fingerprint carry the
    resolved id, never the sentinel; with the setting ``null`` the routes are dropped (each
    purpose its own configured model). The setting accepts only a model ``/admin/model-config``
    lists (422 otherwise), and ``/admin/model-config`` refuses the sentinel as a model.
    Complement: changing the setting changes Max's fingerprint."""
    settings = ProfileSettings.model_validate(seeds.max_settings())
    assert tiers.uses_strongest(settings)
    named = tiers.resolved(settings, "big-model")
    assert {named.models.purposes[p].model for p in seeds.MAX_STRONGEST_PURPOSES} == {"big-model"}
    dropped = tiers.resolved(settings, None)
    assert {dropped.models.purposes[p].model for p in seeds.MAX_STRONGEST_PURPOSES} == {None}
    facts = RuntimeFacts(models={"entailment": ModelIdentity(id="small")}, templates={})
    prints = {
        fingerprint(profile_id=max_(), version=1, settings=tiers.resolved(settings, m), facts=facts)
        for m in ("big-model", "other-model", None)
    }
    assert len(prints) == 3

    async with respond_client(stub_settings, engine) as (client, _):
        refused = await client.put(
            "/admin/model-config/entailment", json={"model": tiers.STRONGEST}, headers=WRITE
        )
        assert refused.status_code == 422, refused.text
        unknown = await client.put(
            "/admin/settings", json={"model_tiers": {"strongest": "no-such-model"}}, headers=WRITE
        )
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["error"]["details"]["field"] == "body.model_tiers.strongest"
        listed = await client.put(
            "/admin/model-config/salience_detection", json={"model": "big-model"}, headers=WRITE
        )
        assert listed.status_code == 200, listed.text
        try:
            set_ = await client.put(
                "/admin/settings", json={"model_tiers": {"strongest": "big-model"}}, headers=WRITE
            )
            assert set_.status_code == 200, set_.text
            story_id, _cast = await new_story(client, "Wren", "Ada")
            moved = await client.put(
                f"/stories/{story_id}/engine-profile",
                json={"profile_id": max_(), "ignore_warnings": True},
                headers=WRITE,
            )
            assert moved.status_code == 200, moved.text
            config = (
                await client.get(f"/stories/{story_id}/effective-config", headers=READ)
            ).json()
            routed = config["effective"]["models"]["purposes"]
            assert {routed[p]["model"] for p in seeds.MAX_STRONGEST_PURPOSES} == {"big-model"}
            assert tiers.STRONGEST not in json.dumps(config)
            before = config["fingerprint"]
            cleared = await client.put(
                "/admin/settings", json={"model_tiers": {"strongest": None}}, headers=WRITE
            )
            assert cleared.status_code == 200, cleared.text
            config = (
                await client.get(f"/stories/{story_id}/effective-config", headers=READ)
            ).json()
            routed = config["effective"]["models"]["purposes"]
            assert {routed[p]["model"] for p in seeds.MAX_STRONGEST_PURPOSES} == {None}
            assert config["fingerprint"] != before
            whole = await client.put("/admin/settings", json={"model_tiers": None}, headers=WRITE)
            assert whole.status_code == 422, whole.text
            assert whole.json()["error"]["code"] == "validation_error"
            assert whole.json()["error"]["details"]["field"] == "body.model_tiers"
            again = await client.put(
                "/admin/settings", json={"model_tiers": {"strongest": None}}, headers=WRITE
            )
            assert again.status_code == 200, again.text
            assert again.json()["model_tiers"]["strongest"] is None
        finally:
            await client.put(
                "/admin/settings", json={"model_tiers": {"strongest": None}}, headers=WRITE
            )
            await client.delete("/admin/model-config/salience_detection", headers=WRITE)


def _optional(annotation: Any) -> bool:
    return get_origin(annotation) in (Union, UnionType) and type(None) in get_args(annotation)


def test_presets__nullable_settings_described() -> None:
    """#780: ``PUT /admin/settings``'s committed description names every setting in
    ``NULLABLE`` with what its ``null`` means, and how a model tier is cleared
    (``model_tiers.<tier>``; ``model_tiers: null`` itself refused). Guard: ``NULLABLE`` is
    exactly the ``DeploymentSettings`` fields typed ``| None``, so the two cannot diverge."""
    document = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    described = document["paths"]["/admin/settings"]["put"]["description"]
    assert sorted(name for name in NULLABLE if f"`{name}`" not in described) == []
    for name, meaning in NULL_MEANS.items():
        assert f"`{name}` ({meaning})" in described, name
    assert set(NULL_MEANS) == NULLABLE
    assert "`model_tiers.<tier>`" in described
    assert "`model_tiers: null` itself is 422" in described
    typed_none = {
        name for name, info in DeploymentSettings.model_fields.items() if _optional(info.annotation)
    }
    assert typed_none == NULLABLE


def test_presets__no_structured_name_asserted() -> None:
    """#227 renamed the seeded row ``Full``: no test asserts the literal ``"Structured"`` any
    more (the name is read through ``seeds.STRUCTURED_NAME`` where a test means the pre-M7 row).
    Complement: the scan finds a planted assertion."""
    pattern = re.compile(r"assert .*\"Structured\"")

    def found(text_: str) -> list[str]:
        return [line for line in text_.splitlines() if pattern.search(line)]

    hits = [
        f"{path}: {line}"
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if path != Path(__file__)
        for line in found(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
    assert found('    assert head["name"] == "Structured"\n')
