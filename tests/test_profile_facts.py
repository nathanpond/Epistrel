"""#109's pure pieces, without a database: the canonical JSON and the fingerprint, capabilities
and guarantees, data-producing increases, the re-derivation estimate and its constant, the
Orchestrator's model ids and template hashes, the two-spelling request base, and the two-stage
helper."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from typing import Any, cast

import pytest
from pydantic import ValidationError

from epistrel.core.profiles import schema
from epistrel.core.profiles.capabilities import CAPABILITY
from epistrel.core.profiles.effective import Configuration
from epistrel.core.profiles.estimates import (
    CALL_LATENCY_MS,
    CHARS_PER_TOKEN,
    CHUNK_MESSAGES,
    backfill_cost,
)
from epistrel.core.profiles.fingerprint import (
    FINGERPRINT_PATTERN,
    TEMPLATES_KEY,
    UNCERTIFIED,
    ModelIdentity,
    QualityStatus,
    QualityStatusValue,
    RuntimeFacts,
    canonical_json,
    capabilities,
    fingerprint,
    quality_relevant_settings,
    routed_models,
)
from epistrel.core.profiles.guarantees import GUARANTEES, guarantees
from epistrel.core.profiles.impact import (
    CAMEL_CASE,
    WARNING_KINDS,
    ImpactPreviewRequest,
    ImpactReport,
    TwoStageRequest,
    affected,
    creation_report,
    delta,
    single_report,
    two_stage,
    unmet,
    with_history,
)
from epistrel.core.profiles.schema import ProfileSettings, delivered_values, exposed
from epistrel.core.profiles.store import ProfileUpdate
from epistrel.core.profiles.validate import parse_settings
from epistrel.core.seeds.profiles import LITE_PRESET_ID, lite_settings
from epistrel.orchestrator.models.config import ConfigRow
from epistrel.orchestrator.models.facts import TEMPLATE_HASHES, identities
from epistrel.orchestrator.models.registry import DEFAULT_MODEL_KEY, STORY_PURPOSES
from epistrel.testing.profiles import data_producing
from tests.conftest import REPO_ROOT
from tests.profile_support import LITE_CAPABILITIES, merged, nest

FACTS = RuntimeFacts(
    models={"generation": ModelIdentity(id="m-1"), "guardrail_check": ModelIdentity(id="m-2")},
    templates={"lite_generation": "a" * 64},
)


def _settings(*documents: dict[str, Any]) -> ProfileSettings:
    return parse_settings(merged(lite_settings(), *documents))


def _print(settings: ProfileSettings, facts: RuntimeFacts = FACTS, version: int = 1) -> str:
    return fingerprint(profile_id=LITE_PRESET_ID, version=version, settings=settings, facts=facts)


def test_canonical_json_is_one_text_per_value() -> None:
    """Keys are sorted at every depth, separators carry no spaces, text stays unescaped,
    floats print their shortest form and decimals print as strings."""
    assert canonical_json({"b": 1, "a": {"d": [1.5, 0.1], "c": "é"}}) == (
        '{"a":{"c":"é","d":[1.5,0.1]},"b":1}'
    )
    assert canonical_json({"x": Decimal("1.10")}) == '{"x":"1.10"}'
    with pytest.raises(TypeError):
        canonical_json({"x": datetime.now(UTC)})


def test_the_fingerprint_follows_what_is_quality_relevant() -> None:
    """``sha256:`` and 64 hex digits, the same whatever order the settings were written in. A
    quality-relevant setting, a model, a template, the profile or its version changes it; a
    quality-irrelevant setting (pinned memories) does not."""
    base = _print(_settings())
    assert re.fullmatch(FINGERPRINT_PATTERN, base)
    reordered = parse_settings(dict(reversed(list(lite_settings().items()))))
    assert _print(reordered) == base
    assert _print(_settings(nest("pins.max_pinned", 3))) == base
    assert _print(_settings(nest("pins.enabled", False))) == base
    assert _print(_settings(nest("memory.rolling_summary.window_messages", 9))) != base
    assert _print(_settings(nest("budgets.deadline_ms", 9_000))) != base
    assert _print(_settings(nest("models.generation.temperature", 0.2))) != base
    swapped = RuntimeFacts(
        models={**FACTS.models, "generation": ModelIdentity(id="m-9")}, templates=FACTS.templates
    )
    assert _print(_settings(), swapped) != base
    reported = RuntimeFacts(
        models={**FACTS.models, "generation": ModelIdentity(id="m-1", reported_version="2")},
        templates=FACTS.templates,
    )
    assert _print(_settings(), reported) != base
    edited = RuntimeFacts(models=FACTS.models, templates={"lite_generation": "b" * 64})
    assert _print(_settings(), edited) != base
    assert _print(_settings(), version=2) != base
    other = fingerprint(profile_id="0" * 26, version=1, settings=_settings(), facts=FACTS)
    assert other != base


def test_a_routed_model_is_the_fingerprints_model() -> None:
    """A configuration that routes a purpose to a model names that model for it in the
    fingerprint and in what an operation records, as its calls use it (#325; FR-PROF-16): a
    profile's model wins over the deployment's. Two configurations that route ``generation``
    to different models have different fingerprints, and an unrouted purpose keeps the
    deployment's model. A route that names no model, or names a purpose the deployment does not
    list, leaves the models as they are."""
    routed = _settings(nest("models.purposes.generation.model", "m-9"))
    stamped = Configuration(LITE_PRESET_ID, 1, routed).stamp(FACTS)
    assert stamped.models == {
        "generation": ModelIdentity(id="m-9"),
        "guardrail_check": ModelIdentity(id="m-2"),
    }
    assert stamped.record(LITE_PRESET_ID, 1)["models"] == {
        "generation": {"id": "m-9", "reported_version": None},
        "guardrail_check": {"id": "m-2", "reported_version": None},
    }
    assert routed_models(FACTS.models, routed) == stamped.models
    elsewhere = _settings(nest("models.purposes.generation.model", "m-8"))
    assert _print(routed) != _print(elsewhere)
    assert stamped.fingerprint == _print(routed)
    params_only = _settings(nest("models.purposes.generation.params.temperature", 0.3))
    assert routed_models(FACTS.models, params_only) == dict(FACTS.models)
    unlisted = _settings(nest("models.purposes.lite_summary.model", "m-7"))
    assert routed_models(FACTS.models, unlisted) == dict(FACTS.models)


def test_quality_relevant_settings_are_the_tagged_ones_and_the_templates() -> None:
    relevant = quality_relevant_settings(_settings(), FACTS.templates)
    tags = schema.setting_tags()
    assert set(relevant) - {TEMPLATES_KEY} == {
        path for path, tag in tags.items() if tag.quality is schema.Quality.relevant
    }
    assert "pins.max_pinned" not in relevant
    assert relevant[TEMPLATES_KEY] == dict(FACTS.templates)


def test_capabilities_and_guarantees() -> None:
    """Lite enables its pipeline (by level) and the delivered modules it turns on, and provides
    none of the four guarantees the boundary names for it. Each row of the guarantee table holds
    exactly where the modules (and the setting) it needs are on, when they are delivered, and
    nowhere while one is not; a row held under a capability-map entry needs at least what that
    entry needs, and a row outside stories is neither provided nor withheld."""
    assert list(capabilities(_settings())) == LITE_CAPABILITIES
    assert "rolling_summary" not in capabilities(
        _settings(nest("memory.rolling_summary.enabled", False))
    )
    labeled = {row.id for row in GUARANTEES if row.label is not None}
    assert len(labeled) == 4
    assert not labeled & set(guarantees(_settings()))
    structured = parse_settings({"pipeline": "structured"})
    assert "structured" in capabilities(structured)
    for row in GUARANTEES:
        if row.capability is not None:
            entry = CAPABILITY[row.capability]
            assert set(entry.requires) <= set(row.requires), row.id
        document: dict[str, Any] = {"pipeline": "structured"}
        for need in row.requires:
            module = schema.MODULE[need.module]
            assert module.switch is not None
            value = (
                1
                if module.positive
                else next(v for v, level in module.levels if need.level in (None, level))
            )
            document = merged(document, nest(module.switch, value))
        if row.setting is not None:
            document = merged(document, nest(row.setting.path, row.setting.values[0]))
        held = parse_settings(document)
        if not row.in_story:
            assert row.provided(held) is None
            assert row.id not in guarantees(held)
            continue
        assert (row.id in guarantees(held)) is row.delivered, row.id
        if row.setting is not None and row.delivered:
            other = merged(document, nest(row.setting.path, "rules"))
            assert row.id not in guarantees(parse_settings(other)), row.id
    assert QualityStatus(status=QualityStatusValue.uncertified) == UNCERTIFIED
    assert {s.value for s in QualityStatusValue} == {"certified", "uncertified", "stale"}


def test_data_producing_increases() -> None:
    """The modules whose switch is tagged data-producing, switched on or raised a level, and a
    data-producing dial raised while its module stays on; never a decrease or a behavioral
    change. A test may mark a delivered module data-producing for a block, and leaving it
    restores the tags."""
    tagged = {
        m.name
        for m in schema.MODULES
        if m.switch is not None and schema.setting_tags()[m.switch].tag is schema.DATA_PRODUCING
    }
    assert tagged == schema.DATA_PRODUCING_MODULES
    assert "polish" not in schema.DATA_PRODUCING_MODULES
    lite = parse_settings({})
    assert schema.data_increases(lite, parse_settings({"pipeline": "structured"})) == ["pipeline"]
    declared = parse_settings({"verification": {"level": "declared"}})
    extraction = parse_settings({"verification": {"level": "extraction"}})
    always = parse_settings({"verification": {"level": "extraction", "extraction_mode": "always"}})
    assert schema.data_increases(declared, extraction) == ["verification"]
    assert schema.data_increases(extraction, always) == ["verification"]
    assert schema.data_increases(always, extraction) == []
    assert schema.data_increases(extraction, declared) == []
    assert schema.data_increases(lite, parse_settings(nest("pins.max_pinned", 3))) == []
    shallow = parse_settings({"character_knowledge": {"nested_beliefs": {"enabled": True}}})
    deeper = parse_settings(
        {"character_knowledge": {"nested_beliefs": {"enabled": True, "depth": 3}}}
    )
    assert schema.data_increases(shallow, deeper) == ["nested_beliefs"]
    assert schema.data_increases(deeper, shallow) == []
    off = parse_settings(nest("repair.polish.enabled", False))
    assert schema.data_increases(off, lite) == []
    with data_producing("polish"):
        assert schema.data_increases(off, lite) == ["polish"]
        assert schema.data_increases(lite, off) == []
    assert "polish" not in schema.DATA_PRODUCING_MODULES
    with pytest.raises(ValueError, match="not modules"), data_producing("nonesuch"):
        pass


def test_a_delta_names_levels_raised_and_lowered() -> None:
    """Capabilities gained and lost are module names (the pipeline by level), and a module that
    stays on at a higher or lower level is gained or lost too; the guarantees lost follow."""
    one = Configuration(LITE_PRESET_ID, 1, parse_settings({"verification": {"level": "declared"}}))
    two = Configuration(
        LITE_PRESET_ID, 2, parse_settings({"verification": {"level": "entailment"}})
    )
    up = delta(one, two, FACTS)
    assert (up.gained, up.lost, up.increases) == (("verification",), (), ("verification",))
    down = delta(two, one, FACTS)
    assert (down.gained, down.lost, down.increases) == ((), ("verification",), ())
    lite = Configuration(LITE_PRESET_ID, 1, parse_settings({}))
    structured = Configuration(LITE_PRESET_ID, 2, parse_settings({"pipeline": "structured"}))
    moved = delta(lite, structured, FACTS)
    assert ("structured" in moved.gained, "lite" in moved.lost) == (True, True)
    gone = delta(one, lite, FACTS)
    assert gone.guarantees_lost == ("verification", "claim_detectors")
    assert down.guarantees_lost == ()


def test_an_entry_keeps_the_configuration_it_cannot_leave() -> None:
    """An increase for a story with history is unavailable: the re-derivation is needed with
    its estimate and the story keeps its fingerprint and capabilities; without history, or
    without an increase, nothing is needed and the entry states the new ones. A one-story report
    lifts the entry's fingerprints, capabilities and status to its top and names its warnings in
    ``WARNING_KINDS`` order."""
    off = Configuration(LITE_PRESET_ID, 1, _settings(nest("repair.polish.enabled", False)))
    on = Configuration(LITE_PRESET_ID, 2, _settings())
    with data_producing("polish"):
        change = delta(off, on, FACTS)
    stuck = affected("S1", change, history=True, transcript=(61, 4_003))
    assert stuck.upgrade_unavailable
    assert stuck.backfill.needed
    assert stuck.backfill.estimate is not None
    assert stuck.backfill.estimate.model_dump() == {
        "model_calls": 3,
        "tokens": 1_000,
        "duration_s": 3 * CALL_LATENCY_MS / 1000,
    }
    assert stuck.fingerprint_after == stuck.fingerprint_before
    unpolished = [c for c in LITE_CAPABILITIES if c != "polish"]
    assert stuck.capabilities_after == list(capabilities(off.settings)) == unpolished
    assert stuck.quality_status_after == UNCERTIFIED
    free = affected("S2", change, history=False)
    assert (free.upgrade_unavailable, free.backfill.needed, free.backfill.estimate) == (
        False,
        False,
        None,
    )
    assert free.fingerprint_after != free.fingerprint_before
    assert free.capabilities_after == list(capabilities(on.settings)) == LITE_CAPABILITIES
    report = single_report(stuck, change)
    assert report.warnings == ["capabilities_gained", "backfill_needed", "upgrade_unavailable"]
    assert set(report.warnings) <= set(WARNING_KINDS)
    assert (report.fingerprint_before, report.fingerprint_after) == (
        stuck.fingerprint_before,
        stuck.fingerprint_after,
    )
    assert (report.capabilities_after, report.quality_status_after) == (
        stuck.capabilities_after,
        UNCERTIFIED,
    )


def test_the_estimate_and_its_measured_latency() -> None:
    """One call per started chunk of messages for each module, the characters over four in
    tokens for each, and the calls' latency one after another; an empty transcript costs
    nothing. The per-call latency is the model matrix's largest median per-call latency, so a
    re-measured matrix fails here until the constant follows it."""
    assert backfill_cost(0, 0, 2) == (0, 0, 0.0)
    assert backfill_cost(10, 400, 0) == (0, 0, 0.0)
    calls, tokens, seconds = backfill_cost(CHUNK_MESSAGES + 1, 4_000, 2)
    assert (calls, tokens) == (4, 2 * 4_000 // CHARS_PER_TOKEN)
    assert seconds == pytest.approx(4 * CALL_LATENCY_MS / 1000)
    matrix = json.loads((REPO_ROOT / "docs" / "model-matrix.json").read_text(encoding="utf-8"))
    per_call = [row["p50_ms"] / sum(row["calls_per_turn"].values()) for row in matrix["rows"]]
    assert round(max(per_call)) == CALL_LATENCY_MS


def test_model_ids_and_template_hashes() -> None:
    """Each delivered purpose's model is its own row's, else the ``default`` row's; the
    template hashes are the shipped files' SHA-256, one per delivered purpose's template and
    template variant (#150: ``generation``'s structured, narrator and age-repair renders)."""
    now = datetime.now(UTC)

    def row(purpose: str, model: str | None) -> ConfigRow:
        return ConfigRow(purpose, None, model, None, {}, now)

    found = identities(
        {DEFAULT_MODEL_KEY: row(DEFAULT_MODEL_KEY, "base"), "generation": row("generation", "big")}
    )
    assert set(found) == set(STORY_PURPOSES)
    assert found["generation"].id == "big"
    assert {found[name].id for name in STORY_PURPOSES if name != "generation"} == {"base"}
    assert all(model.reported_version is None for model in found.values())
    assert identities({})["generation"].id is None
    folder = resources.files("epistrel.orchestrator.models").joinpath("templates")
    assert dict(TEMPLATE_HASHES) == {
        template.removesuffix(".j2"): hashlib.sha256(
            folder.joinpath(template).read_bytes()
        ).hexdigest()
        for purpose in STORY_PURPOSES.values()
        for template in (purpose.template, *purpose.variants.values())
    }
    assert {"structured_generation", "narrator_generation", "age_repair"} <= set(TEMPLATE_HASHES)


def test_both_spellings_of_ignore_warnings() -> None:
    """``ignore_warnings`` or ``ignoreWarnings``; both agreeing is accepted, both at odds is
    refused at the body; the published schema documents both."""
    body = {"name": "Lite", "settings": {}}
    assert ProfileUpdate.model_validate(body).ignore_warnings is False
    assert ProfileUpdate.model_validate({**body, CAMEL_CASE: True}).ignore_warnings is True
    both = {**body, "ignore_warnings": True, CAMEL_CASE: True}
    assert ProfileUpdate.model_validate(both).ignore_warnings is True
    with pytest.raises(ValidationError) as caught:
        ProfileUpdate.model_validate({**body, "ignore_warnings": True, CAMEL_CASE: False})
    assert caught.value.errors()[0]["loc"] == ()
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate_json(json.dumps({**body, CAMEL_CASE: "yes"}), strict=True)
    documented = ProfileUpdate.model_json_schema(mode="validation")["properties"]
    assert {"ignore_warnings", CAMEL_CASE} <= set(documented)
    assert documented[CAMEL_CASE]["type"] == "boolean"
    assert issubclass(ProfileUpdate, TwoStageRequest)
    with pytest.raises(ValidationError):
        ImpactPreviewRequest.model_validate({})


async def test_two_stage_applies_only_when_it_may() -> None:
    """A report with warnings holds the change unless confirmed (the change is not even
    called); a report without warnings applies at once."""
    off = Configuration(LITE_PRESET_ID, 1, _settings(nest("pins.enabled", False)))
    on = Configuration(LITE_PRESET_ID, 2, _settings())
    calls: list[int] = []

    async def apply(_: Any) -> int:
        calls.append(1)
        return len(calls)

    warning = single_report(
        affected("S", delta(off, on, FACTS), history=False), delta(off, on, FACTS)
    )
    silent = single_report(affected("S", delta(on, on, FACTS), history=False), delta(on, on, FACTS))

    async def warns() -> Any:
        return warning

    async def quiet() -> Any:
        return silent

    lite = Configuration(LITE_PRESET_ID, 1, _settings())
    assert unmet(None, lite.settings) == []
    created = creation_report(lite.stamp(FACTS), lite.settings, None)
    assert (created.warnings, created.affected_count) == ([], 0)
    assert (created.capabilities_after, created.quality_status_after) == (
        LITE_CAPABILITIES,
        UNCERTIFIED,
    )
    assert await with_history(cast(Any, None), []) == set()
    held = await two_stage(warns, apply, ignore_warnings=False)
    assert (held.applied, held.result, calls, held.kept()) == (False, None, [], False)
    forced = await two_stage(warns, apply, ignore_warnings=True)
    assert (forced.applied, forced.result, forced.kept()) == (True, 1, True)
    plain = await two_stage(quiet, apply, ignore_warnings=False)
    assert (plain.applied, plain.result, plain.impact.warnings) == (True, 2, [])


async def test_a_lost_guarantee_is_reported_and_held() -> None:
    """A change that turns off the module holding a guarantee loses that guarantee: a one-story
    report names it in ``guarantees_lost`` and warns of it after the capability lost, so the
    change is held until it is confirmed. Raising the module's level, or turning it on, loses
    none, and the report neither names nor warns of one. Turning off a module that holds none
    (the polish pass) loses the capability alone: the entry lists it, and the report names no
    guarantee and warns only of the capability lost."""
    declared = Configuration(
        LITE_PRESET_ID, 1, parse_settings({"verification": {"level": "declared"}})
    )
    lite = Configuration(LITE_PRESET_ID, 2, parse_settings({}))
    gone = delta(declared, lite, FACTS)
    report = single_report(affected("S", gone, history=False), gone)
    assert report.guarantees_lost == ["verification", "claim_detectors"]
    assert report.warnings == ["capabilities_lost", "guarantees_lost"]
    calls: list[str] = []

    async def lose() -> ImpactReport:
        return report

    async def apply(_: ImpactReport) -> str:
        calls.append("applied")
        return "applied"

    held = await two_stage(lose, apply, ignore_warnings=False)
    assert (held.applied, held.result, calls) == (False, None, [])
    assert held.impact.guarantees_lost == ["verification", "claim_detectors"]
    forced = await two_stage(lose, apply, ignore_warnings=True)
    assert (forced.applied, forced.result, calls) == (True, "applied", ["applied"])
    entailment = Configuration(
        LITE_PRESET_ID, 2, parse_settings({"verification": {"level": "entailment"}})
    )
    for kept in (delta(declared, entailment, FACTS), delta(lite, declared, FACTS)):
        same = single_report(affected("S", kept, history=False), kept)
        assert (same.guarantees_lost, same.warnings) == ([], ["capabilities_gained"])
    unpolished = Configuration(
        LITE_PRESET_ID, 3, parse_settings(nest("repair.polish.enabled", False))
    )
    plain = delta(lite, unpolished, FACTS)
    quiet = single_report(affected("S", plain, history=False), plain)
    assert quiet.affected_stories[0].capabilities_lost == ["polish"]
    assert (quiet.guarantees_lost, quiet.warnings) == ([], ["capabilities_lost"])


def test_the_effective_values_are_the_delivered_ones() -> None:
    """An effective-configuration read shows every delivered setting and nothing else."""
    values = delivered_values(_settings())

    def paths(node: dict[str, Any], prefix: str = "") -> set[str]:
        found: set[str] = set()
        for name, value in node.items():
            path = f"{prefix}{name}"
            if isinstance(value, dict) and path not in schema.LEAF_PATHS:
                found |= paths(value, f"{path}.")
            else:
                found.add(path)
        return found

    assert paths(values) == {path for path in schema.LEAF_PATHS if exposed(path)}
    assert values["pipeline"] == "lite"
