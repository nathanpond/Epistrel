"""The catalog validation's pure parts (#111): the report shape and its order, the runner's
capability rule and gate, the personality heuristic, the policy texts a scenario sends, the
matched span of a word list, the character-document readers, the provider walk, and a catalog
character's publish rules (#115) run through the same runner."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog import validation
from epistrel.core.catalog.characters import CharacterDraft
from epistrel.core.catalog.checks import character_documents as documents
from epistrel.core.catalog.checks.base import (
    DEPLOYMENT_CAPABILITIES,
    Check,
    Findings,
    Subject,
    ValidationContext,
)
from epistrel.core.catalog.checks.input_policy import policy_texts, strings_in
from epistrel.core.catalog.checks.personality import (
    PERSONALITY_TERMS,
    asserts_personality,
    personality_sentences,
)
from epistrel.core.catalog.checks.providers import provider_names
from epistrel.core.catalog.models import PUBLISH_REQUIRED
from epistrel.core.catalog.report import (
    CODES,
    DRAFT,
    Entry,
    EntryLocation,
    Outcome,
    ValidationReport,
    summary,
)
from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.config_changes import DeploymentState
from epistrel.core.policies.evaluate import matched_span
from epistrel.core.profiles.schema import DELIVERED
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.tables.ownership import EntityType
from tests.character_support import complete
from tests.policy_support import policy
from tests.scenario_support import COMPLETE

ENTRY = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
NEWLINE = chr(10)


def _entry(
    code: str,
    outcome: Outcome = Outcome.rejected,
    *,
    role: str | None = None,
    field: str | None = None,
    seed_id: str | None = None,
    text: str | None = None,
) -> Entry:
    return Entry(
        code=code,
        location=EntryLocation(entry=ENTRY, version=DRAFT, role=role, field=field, seed_id=seed_id),
        text=text,
        outcome=outcome,
    )


def _context(capabilities: frozenset[str] = DEPLOYMENT_CAPABILITIES) -> ValidationContext:
    """A context for checks that read no database (the connection is never touched)."""
    settings = DeploymentState(
        config_revision=0, default_content_rating_id=EVERYONE_ID, minor_age_threshold=18
    )
    return ValidationContext(
        conn=cast(AsyncConnection, None),
        settings=settings.as_settings(),
        capabilities=capabilities,
    )


def _subject(document: dict[str, Any], entity_type: EntityType = EntityType.CHARACTER) -> Subject:
    model = CharacterDraft if entity_type is EntityType.CHARACTER else ScenarioDraft
    return Subject(entity_type, ENTRY, DRAFT, document, model)


# -- the report ---------------------------------------------------------------------------------


def test_report_shape_and_order() -> None:
    """A report sorts its entries by (entry, version, role, field, seed id, code), each part
    missing before present, drops a repeated entry, splits errors (``rejected``) from warnings,
    and lists each unavailable check once, sorted. ``ok`` is "no error"; JSON has exactly the
    entry's six keys and the location's five."""
    report = ValidationReport.of(
        [
            _entry("b_code", role="mother", field="cast.roles[0].character"),
            _entry("a_code", role="mother", field="cast.roles[0].character"),
            _entry("world_required", role=None, field="world.description"),
            _entry("x", Outcome.kept, role="aunt", field="cast.roles[1].character"),
            _entry("y", Outcome.ignored, role="aunt", field="cast.roles[1].setup_description"),
            _entry("x", Outcome.kept, role="aunt", field="cast.roles[1].character"),
            _entry("seeded", Outcome.kept, role="aunt", field="a", seed_id="S"),
            _entry("unseeded", Outcome.kept, role="aunt", field="a"),
        ],
        ["state_schema", "fact_consistency", "state_schema"],
    )
    assert [(e.code, e.location.role) for e in report.errors] == [
        ("world_required", None),
        ("a_code", "mother"),
        ("b_code", "mother"),
    ]
    assert [e.code for e in report.warnings] == ["unseeded", "seeded", "x", "y"]
    assert report.unavailable == ["fact_consistency", "state_schema"]
    assert not report.ok
    assert ValidationReport.of([_entry("w", Outcome.deferred)]).ok
    shown = report.as_json()
    assert set(shown) == {"errors", "warnings", "unavailable"}
    assert set(shown["errors"][0]) == {"code", "location", "text", "outcome", "source", "rule"}
    assert set(shown["errors"][0]["location"]) == {"entry", "version", "role", "field", "seed_id"}
    assert {o.value for o in Outcome} == {
        "ignored",
        "dropped",
        "kept",
        "deferred",
        "inactive",
        "rejected",
    }
    assert ValidationReport.model_validate(shown) == report


def test_the_codes_include_the_planned_ones() -> None:
    """The report's vocabulary holds every code #111's acceptance criteria name, the later
    milestones' included."""
    planned = {
        "cast_limit_exceeded",
        "character_missing",
        "character_not_published",
        "eligibility_failed",
        "seed_missing_audience",
        "media_not_allowed",
        "provider_undeclared",
        "rating_looser",
        "input_policy",
        "personality_in_role",
        "fact_conflict_dropped",
        "belief_conflict",
    }
    assert planned <= set(CODES)
    assert all(meaning for meaning in CODES.values())


def test_summary_holds_codes_and_locations_only() -> None:
    """The INFO line's content: codes and locations, never the text, source or rule."""
    secret = "the deed burned before anyone woke"
    entry = _entry("personality_in_role", Outcome.ignored, role="mother", field="f", text=secret)
    line = summary(ValidationReport.of([entry], ["state_schema"]))
    assert line == {
        "errors": [],
        "warnings": [
            {
                "code": "personality_in_role",
                "entry": ENTRY,
                "version": DRAFT,
                "role": "mother",
                "field": "f",
            }
        ],
        "unavailable": ["state_schema"],
    }
    assert secret not in repr(line)


# -- the runner --------------------------------------------------------------------------------


class _Probe(Check):
    """A test check: records that it ran, and fails when ``error``."""

    name = "probe"
    kinds: ClassVar[frozenset[EntityType]] = frozenset({EntityType.CHARACTER})

    def __init__(self, *, error: bool = False) -> None:
        self.error = error
        self.ran = False

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        self.ran = True
        if not self.error:
            return Findings()
        return Findings(entries=[subject.entry(f"{self.name}_failed", Outcome.rejected)])


def _probe(
    name: str,
    *,
    capability: str | None = None,
    gate: bool = False,
    error: bool = False,
    kinds: frozenset[EntityType] = frozenset({EntityType.CHARACTER}),
) -> _Probe:
    """A ``_Probe`` subclass named ``name`` with these class attributes, instantiated."""
    attributes = {"name": name, "capability": capability, "gate": gate, "kinds": kinds}
    found: _Probe = type(f"_Probe_{name}", (_Probe,), attributes)(error=error)
    return found


async def test_runner_lists_what_the_deployment_cannot_run() -> None:
    """A check whose capability the context lacks is listed ``unavailable`` and never runs;
    given the capability, it runs. A gate's error stops the checks after it (they neither run
    nor appear), but a check the deployment cannot run is still listed. A check for another
    entry type does not run."""
    always = _probe("always")
    needs = _probe("needs", capability="states")
    report = await validation.run(_subject({}), _context(), checks=[always, needs])
    assert (always.ran, needs.ran, report.unavailable) == (True, False, ["needs"])

    granted = _probe("needs", capability="states")
    report = await validation.run(
        _subject({}), _context(DEPLOYMENT_CAPABILITIES | {"states"}), checks=[granted]
    )
    assert (granted.ran, report.unavailable) == (True, [])

    gate, after = _probe("gate", gate=True, error=True), _probe("after")
    later = _probe("later", capability="states")
    report = await validation.run(_subject({}), _context(), checks=[gate, after, later])
    assert (after.ran, [e.code for e in report.errors]) == (False, ["gate_failed"])
    assert report.unavailable == ["later"]

    scenario_only = _probe("scenario_only", kinds=frozenset({EntityType.SCENARIO}))
    await validation.run(_subject({}), _context(), checks=[scenario_only])
    assert scenario_only.ran is False


def test_the_checks_and_the_capabilities() -> None:
    """The ordered checks, each named once: the document check first (it gates), the three
    capability-gated ones last. The deployment's capabilities are each delivered level of the
    pipeline and every other delivered module (``DELIVERED``), not States; and ``providers``,
    delivered with the provider registry (#204)."""
    names = [check.name for check in validation.CHECKS]
    assert names == [
        "document",
        "required",
        "character_content",
        "cast_limit",
        "role_characters",
        "eligibility",
        "seeds",
        "media",
        "providers",
        "rating_mismatch",
        "companion",
        "input_policy",
        "personality",
        "seed_normalization",
        "fact_consistency",
        "state_schema",
    ]
    assert validation.CHECKS[0].gate
    assert {c.name: c.capability for c in validation.CHECKS if c.capability} == {
        "seed_normalization": "structured",
        "fact_consistency": "structured",
        "state_schema": "states",
    }
    delivered = set(DELIVERED["pipeline"]) | {name for name in DELIVERED if name != "pipeline"}
    assert delivered | {"providers"} == DEPLOYMENT_CAPABILITIES
    assert {"lite", "rolling_summary", "pins", "polish", "budgets", "models"} <= delivered
    assert "states" not in delivered


async def test_capability_checks_never_pass_silently() -> None:
    """Even given its capability, a capability-gated check not implemented yet lists itself
    (the State-schema check, M6), so none can pass a scenario silently; the seed checks #149
    implements run and find nothing to report in an empty scenario."""
    gated = [check for check in validation.CHECKS if check.capability is not None]
    context = _context(DEPLOYMENT_CAPABILITIES | {"structured", "states"})
    implemented = {"seed_normalization", "fact_consistency"}
    for check in gated:
        found = await check.run(_subject({}, EntityType.SCENARIO), context)
        listed = [] if check.name in implemented else [check.name]
        assert (found.entries, found.unavailable) == ([], listed)


async def test_a_character_runs_its_rules() -> None:
    """A catalog character is validated through the same runner (#115). A draft holding every
    required field but its rating reports exactly ``content_rating_missing`` (judging a rating
    that is set, and images, takes the database: the integration tests do that). An empty draft
    reports every missing field at once, ``true_age_missing`` at the true age, and, with the
    deployment's ``require_primary_image`` on, ``primary_image_required``. A stored alias that is
    the name breaks a save rule (the document check: ``alias_is_name``, and the gate stops the
    rest), and a document the draft model no longer accepts has an error per field, at its
    path."""
    unrated = complete("Mara")
    del unrated["metadata"]["content_rating_id"]
    report = await validation.run(_subject(unrated), _context())
    assert [(e.code, e.location.field, e.outcome) for e in report.errors] == [
        ("content_rating_missing", "metadata.content_rating_id", Outcome.rejected)
    ]
    assert (report.warnings, report.unavailable) == ([], [])

    empty = await validation.run(_subject({}), _context())
    expected = [
        *(("field_required", path) for path in PUBLISH_REQUIRED),
        ("true_age_missing", "definition.true_age"),
        ("content_rating_missing", "metadata.content_rating_id"),
    ]
    assert sorted((e.code, e.location.field) for e in empty.errors) == sorted(expected)
    required = _context()
    required = ValidationContext(
        conn=required.conn,
        settings=required.settings.model_copy(update={"require_primary_image": True}),
        capabilities=required.capabilities,
    )
    pictured = await validation.run(_subject(unrated), required)
    assert sorted((e.code, e.location.field) for e in pictured.errors) == [
        ("content_rating_missing", "metadata.content_rating_id"),
        ("primary_image_required", "public_card.images.primary"),
    ]

    aliased = {**unrated, "metadata": {"aliases": ["MARA"]}}
    found = await validation.run(_subject(aliased), _context())
    assert [(e.code, e.location.field) for e in found.errors] == [
        ("alias_is_name", "metadata.aliases[0]")
    ]
    broken = await validation.run(
        _subject({"definition": {"personality": 7}, "mood": "x"}), _context()
    )
    assert sorted((e.code, e.location.field) for e in broken.errors) == [
        ("extra_forbidden", "mood"),
        ("string_type", "definition.personality"),
    ]
    assert (broken.warnings, broken.unavailable) == ([], [])


# -- the personality heuristic -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("She is a habitual liar.", ["She is a habitual liar."]),
        ("Born in May. He is warm and generous! Likes rain.", ["He is warm and generous!"]),
        ("They were honestly kind to strangers?", ["They were honestly kind to strangers?"]),
        ("A short-tempered man" + NEWLINE + "who waits.", ["A short-tempered man"]),
        ("She is quick to anger with liars.", ["She is quick to anger with liars."]),
        ("He was QUICK TO FORGIVE.", ["He was QUICK TO FORGIVE."]),
        ("She is not a liar.", []),
        ("He is never cruel to the dogs.", []),
        ("It was the kind of day that ends early.", []),
        ("All kinds of birds nest here.", []),
        ("He teaches kindergarten.", []),
        ("She keeps the only key to the old barn.", []),
        ("The house is being sold.", []),
        ("She is cruel. She is cruel.", ["She is cruel."]),
    ],
)
def test_personality_sentences(text: str, found: list[str]) -> None:
    """Each sentence that asserts a disposition, once, as written; negated terms, ``kind of``
    and words that merely start like a term are not; a phrase matches across spaces or a
    hyphen, any case, and a term with an ending (``liars``, ``honestly``)."""
    assert personality_sentences(text) == found
    assert asserts_personality(text) is bool(found)


def test_personality_terms_are_distinct_words() -> None:
    assert len(set(PERSONALITY_TERMS)) == len(PERSONALITY_TERMS)
    assert all(term == term.lower().strip() for term in PERSONALITY_TERMS)


# -- what the input-policy check sends -------------------------------------------------------------


def test_policy_texts_cover_the_private_prose() -> None:
    """The texts a scenario sends to the input policies: the world, each role's setup
    description, relationships and name overrides, every seed (with its role and id), the
    opening narration, the director's material and every string inside the objects stored as
    given; never the public card or the visual style."""
    draft = ScenarioDraft.model_validate(COMPLETE)
    texts = {t.field: t for t in policy_texts(draft)}
    expected = {
        "world.description",
        "world.locations[0].name",
        "world.locations[0].description",
        "world.entities[0].name",
        "world.entities[0].aliases[0]",
        "cast.roles[0].setup_description",
        "cast.roles[0].relationships[0].name",
        "cast.roles[0].name_overrides.aliases[0]",
        "cast.roles[0].seeded_memories[0].text",
        "cast.roles[1].name_overrides.canonical",
        "starting_state.seeded_memories[0].text",
        "starting_state.opening.narration",
        "drivers.plot_hooks[0]",
        "drivers.director_guidance",
        "drivers.planned_reveals[0].truth.text",
        "drivers.planned_reveals[0].conditions",
        "drivers.endings[1]",
        "starting_state.rules[0].when",
    }
    assert expected <= set(texts)
    assert texts["cast.roles[0].seeded_memories[0].text"].role == "mother"
    assert texts["drivers.planned_reveals[0].truth.text"].role is None
    for field in texts:
        assert not field.startswith(("public_card", "metadata", "visual_style")), field
    assert list(strings_in({"a": [{"b": "x"}, 3, "y"]}, "s")) == [
        ("s.a[0].b", "x"),
        ("s.a[2]", "y"),
    ]


def test_matched_span_quotes_the_words_as_written() -> None:
    """A word or name list's span is its first match as the text spells it (case and the
    space between a phrase's words kept); a pattern has none."""
    words = policy({"type": "wordlist", "words": ["deep cellar", "barn"]})
    assert matched_span("They met in the Deep  Cellar at dusk.", words) == "Deep  Cellar"
    names = policy({"type": "entity_list", "names": ["Old Meg"]}, applies_to=None)
    assert matched_span("We saw old meg again.", names) == "old meg"
    assert matched_span("Nothing here.", words) is None
    pattern = policy({"type": "regex", "patterns": ["cell[a-z]+"]})
    assert matched_span("a cellar", pattern) is None


# -- what the cast checks read from a character's version ------------------------------------------


def test_character_documents_read_both_shapes() -> None:
    """#103's interim document and #115's both read; a value of another type reads
    as absent."""
    interim = {"name": "Mara", "definition": {"personality": "Wry."}}
    planned = {
        "public_card": {"name": "Mara", "images": {"primary": "P", "gallery": ["G", 4]}},
        "metadata": {"labels": [" Warm ", 3], "content_rating_id": EVERYONE_ID},
        "definition": {"true_age": 41},
    }
    assert (documents.name_of(interim), documents.name_of(planned)) == ("Mara", "Mara")
    assert documents.labels_of(planned) == {"warm"}
    assert documents.labels_of(interim) == frozenset()
    assert (documents.rating_id_of(planned), documents.rating_id_of(interim)) == (EVERYONE_ID, None)
    assert (documents.true_age_of(planned), documents.true_age_of(interim)) == (41, None)
    assert documents.true_age_of({"definition": {"true_age": True}}) is None
    assert documents.images_of(planned) == [
        ("public_card.images.primary", "P"),
        ("public_card.images.gallery[0]", "G"),
    ]
    assert documents.images_of(None) == []


def test_provider_names_are_found_at_any_depth() -> None:
    found = list(
        provider_names(
            [{"provider": "weather"}, {"steps": [{"provider": "news"}, {"provider": 4}]}], "s"
        )
    )
    assert found == [("s[0].provider", "weather"), ("s[1].steps[0].provider", "news")]
