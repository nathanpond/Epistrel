"""The age repair's answer, without a database (#480; FR-CAT-12, FR-MODEL-6, NFR-QUAL-5): how
its names match the characters asked about, what the retry is told, and what the failure
report keeps. The live E-07 failures this guards were answers that named a character the
render had already aged (``Old Tom``) beside the one asked about, and a group split in two;
the exact-set check rejected the first on every attempt with a correction that named nobody.
Also the beat schema the render is sent: ``intent`` only on a ``say``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.registry import AgeClue, GenerationInputs, PlayerInput
from epistrel.orchestrator.models.structured import parse
from epistrel.orchestrator.structured.render import (
    AGE_UNREPAIRED,
    AgeRepair,
    age_schema,
    match_ages,
    name_words,
    render_schema,
    repair_ages,
)
from tests.test_structured_output import models

SECRET = "Merchant couple from Crossgate"


def answer(**ages: int) -> str:
    return json.dumps({"ages": [{"name": name, "age": age} for name, age in ages.items()]})


def answer_list(*pairs: tuple[str, int]) -> str:
    return json.dumps({"ages": [{"name": name, "age": age} for name, age in pairs]})


def valid(schema: type[AgeRepair], text: str) -> dict[str, int]:
    found = parse(text, schema)
    assert isinstance(found, AgeRepair), found
    return found.resolved()


def problem(schema: type[AgeRepair], text: str) -> Any:
    found = parse(text, schema)
    assert not isinstance(found, AgeRepair), found
    return found


def test_name_words_drop_case_punctuation_and_a_leading_article() -> None:
    assert name_words("The old man.") == ("old", "man")
    assert name_words("  OLD MAN!  ") == ("old", "man")
    assert name_words('"Mrs. Hale"') == ("mrs", "hale")
    assert name_words("An") == ("an",)  # an article alone is the name
    assert name_words("Theo") == ("theo",)


def test_an_answer_for_someone_not_asked_about_is_ignored() -> None:
    """Live E-07 ``-0004``: asked only about the couple (Old Tom had an age), the model answered
    both, each spelled exactly; the exact-set check refused it on all 3 attempts."""
    schema = age_schema({SECRET: (0, 150)})
    text = answer_list(("Old Tom", 65), (SECRET, 40))
    assert valid(schema, text) == {SECRET.casefold(): 40}


def test_case_punctuation_and_articles_are_tolerated() -> None:
    schema = age_schema({"the old man": (60, 69), "Priya": (18, 23), "Theo": (18, 18)})
    text = answer_list(("Old man.", 64), ("PRIYA", 20), ('"Theo"', 18))
    assert valid(schema, text) == {"the old man": 64, "priya": 20, "theo": 18}


def test_a_unique_containment_match_is_accepted() -> None:
    """The render's ``Traveling merchant couple (unnamed)`` answered as ``traveling merchant
    couple``, and ``Tom`` answered as ``Old Tom``."""
    schema = age_schema({"Traveling merchant couple (unnamed)": (0, 150), "Tom": (0, 150)})
    text = answer_list(("traveling merchant couple", 40), ("Old Tom", 61))
    assert valid(schema, text) == {"traveling merchant couple (unnamed)": 40, "tom": 61}


def test_an_exact_name_wins_over_a_containing_one() -> None:
    schema = age_schema({"Tom": (0, 150)})
    assert valid(schema, answer_list(("Old Tom", 70), ("Tom", 30))) == {"tom": 30}


@pytest.mark.parametrize(
    ("expected", "given"),
    [
        (["Tom"], [("Old Tom", 60), ("Tom Hale", 30)]),  # two answers could be Tom
        (["Old Tom", "Young Tom"], [("Tom", 60)]),  # one answer could be either
        (["Tom"], [("Tom", 60), ("tom", 30)]),  # Tom answered twice
        (["Tom"], [("Tom.", 60), ("the Tom", 30)]),  # twice, by the same words
    ],
)
def test_an_ambiguous_match_is_never_guessed(
    expected: list[str], given: list[tuple[str, int]]
) -> None:
    schema = age_schema(dict.fromkeys(expected, (0, 150)))
    failure = problem(schema, answer_list(*given))
    assert "more than one answer could be" in failure.problem
    assert failure.reason == "schema_error"


def test_a_split_group_is_missing_and_the_correction_names_everyone() -> None:
    """Live E-07 ``-0045``: the couple came back as two people. The correction names who was
    asked about, who is missing and the answers not asked about; the report names nobody."""
    asked = "Traveling merchant couple (unnamed)"
    schema = age_schema({asked: (0, 150)})
    failure = problem(
        schema, answer_list(("Traveling merchant (male)", 38), ("Traveling merchant (female)", 42))
    )
    assert f"copying its name: {asked!r}" in failure.problem
    assert f"no answer for {asked!r}" in failure.problem
    assert "not asked about: 'Traveling merchant (male)', 'Traveling merchant (female)'" in (
        failure.problem
    )
    report = failure.reported()
    assert "merchant" not in report.casefold()
    assert "1 asked about, 1 missing, 0 ambiguous, 2 not asked about" in report


def test_the_age_bounds_still_hold_and_the_correction_names_them() -> None:
    schema = age_schema({"the old man": (60, 69)})
    failure = problem(schema, answer(**{"Old man": 40}))
    assert "'the old man' is 60 to 69 years old" in failure.problem
    assert failure.reported() == "the object: ages.0.age: outside the plausible range"
    assert valid(schema, answer(**{"Old man": 69})) == {"the old man": 69}


def test_match_ages_reports_missing_ambiguous_and_extra() -> None:
    found = match_ages(["tom", "ada"], ["Old Tom", "Bob", "Tom Hale"])
    assert (found.matched, found.missing, found.ambiguous, found.extra) == (
        {},
        ["ada"],
        ["tom"],
        [1],
    )


def test_the_stub_answer_still_validates() -> None:
    schema = age_schema({"the old man": (60, 69), "Priya": (18, 23)})
    example = schema.stub_example("0" * 64, [])
    assert valid(schema, json.dumps(example)) == {"the old man": 60, "priya": 23}


def _inputs() -> GenerationInputs:
    return GenerationInputs(
        player_input=PlayerInput(name="Wren"),
        introductions=[AgeClue(name=SECRET, clue="arrived yesterday evening")],
    )


async def test_the_retry_names_the_characters_and_the_503_does_not() -> None:
    """Three answers that miss the couple: the second and third calls carry a correction
    naming it and the extra answer; the turn's 503 ``age_unrepaired`` reports counts only.
    The prompt asks for each listed name copied exactly. Attempts are unchanged (1 + 2)."""
    mal = models()
    wrong = answer_list(("Old Tom", 65), ("Merchant (male)", 40))
    mal.stub.script("generation", [wrong, wrong, wrong])
    with pytest.raises(ModelUnavailable) as caught:
        await repair_ages(
            mal, _inputs(), {SECRET: (0, 150)}, params=ModelParams(), budget=TurnBudget()
        )
    assert caught.value.details["reason"] == AGE_UNREPAIRED
    calls = mal.stub.calls
    assert len(calls) == 3
    assert "copied exactly as listed" in calls[0].messages[0].content
    correction = calls[1].messages[-1].content
    assert f"no answer for {SECRET!r}" in correction
    assert "not asked about: 'Old Tom', 'Merchant (male)'" in correction
    errors = caught.value.details["structural_errors"]
    assert len(errors) == 3
    for error in errors:
        assert "merchant" not in error.casefold()
        assert "tom" not in error.casefold()
        assert "1 missing" in error


async def test_a_tolerated_answer_repairs_on_the_first_attempt() -> None:
    mal = models()
    mal.stub.script("generation", [answer_list(("Old Tom", 65), ("merchant couple", 41))])
    ages = await repair_ages(
        mal, _inputs(), {SECRET: (0, 150)}, params=ModelParams(), budget=TurnBudget()
    )
    assert ages == {SECRET.casefold(): 41}
    assert len(mal.stub.calls) == 1


def test_the_render_schema_offers_intent_on_a_say_alone() -> None:
    """E-07's other failures (``beats.N.intent: only a say declares an intent``): the schema the
    render is sent (its ``response_format``) let any beat carry an intent, so constrained
    decoding could not stop one on a ``do``. Each kind group is now its own alternative; the
    validator still refuses an intent off a ``say``."""
    schema = render_schema(actor_id="a" * 26, player_id="p" * 26, cast_ids=[], max_beats=6)
    beat = schema.model_json_schema()["$defs"]["BeatDecl"]
    variants = {tuple(v["properties"]["kind"]["enum"]): v for v in beat["anyOf"]}
    assert set(variants) == {("say",), ("latent",), ("think", "do", "narration")}
    assert "intent" in variants[("say",)]["properties"]
    assert "intent" not in variants[("think", "do", "narration")]["properties"]
    assert "intent" not in variants[("latent",)]["properties"]
    assert "visibility" in variants[("latent",)]["properties"]
    assert "visibility" not in variants[("say",)]["properties"]
    assert all(v["additionalProperties"] is False for v in variants.values())
    assert all(v["required"] == ["kind", "text"] for v in variants.values())
    bad = '{"beats": [{"kind": "do", "text": "Maren nods.", "intent": "sincere"}]}'
    failure = parse(bad, schema)
    assert "beats.0.intent: only a say declares an intent" in failure.problem  # type: ignore[union-attr]


def test_the_render_schema_offers_ids_under_a_characters_scope_alone() -> None:
    """P-08's structured row (#153): three renders in a row named characters under a scope that
    names none (``beats.N.visibility: only a characters scope names some``), because the schema
    the render is sent let any scope carry ids. Each scope shape is now its own alternative: a
    ``characters`` scope requires one or more ids, ``objective`` and ``public`` offer none. The
    validator still refuses both mistakes."""
    cast = "c" * 26
    schema = render_schema(actor_id="a" * 26, player_id="p" * 26, cast_ids=[cast], max_beats=6)
    scope = schema.model_json_schema()["$defs"]["VisibilityDecl"]
    variants = {tuple(v["properties"]["scope"]["enum"]): v for v in scope["anyOf"]}
    assert set(variants) == {("objective", "public"), ("characters",)}
    assert "characters" not in variants[("objective", "public")]["properties"]
    named = variants[("characters",)]
    assert named["properties"]["characters"]["minItems"] == 1
    assert named["required"] == ["scope", "characters"]
    assert "default" not in named["properties"]["scope"]
    assert all(v["additionalProperties"] is False for v in variants.values())
    for visibility, problem in (
        ({"scope": "public", "characters": [cast]}, "only a characters scope names some"),
        ({"scope": "characters", "characters": []}, "a characters scope names some"),
    ):
        beats = [
            {"kind": "do", "text": "Maren nods."},
            {"kind": "latent", "text": "She knows.", "visibility": visibility},
        ]
        failure = parse(json.dumps({"beats": beats}), schema)
        assert f"beats.1.visibility: {problem}" in failure.problem  # type: ignore[union-attr]
    valid = [
        {"kind": "do", "text": "Maren nods."},
        {
            "kind": "latent",
            "text": "She knows.",
            "visibility": {"scope": "characters", "characters": [cast]},
        },
    ]
    assert len(parse(json.dumps({"beats": valid}), schema).beats) == 2  # type: ignore[union-attr]


def test_the_age_schema_takes_one_answer_per_name_asked_about() -> None:
    """P-08's structured row (#153): the age repair answered for nobody three times in a row
    (``1 asked about, 1 missing, 0 not asked about``), because the schema it was sent took any
    list of any names. It now takes exactly one answer per name asked about, each that name as
    the prompt listed it and an age inside its bounds; ``match_ages`` still validates it."""
    schema = age_schema({" Old Tom ": (60, 90), "the girl": (8, 12)})
    ages = schema.model_json_schema()["properties"]["ages"]
    assert (ages["minItems"], ages["maxItems"]) == (2, 2)
    answers = {a["properties"]["name"]["enum"][0]: a for a in ages["items"]["anyOf"]}
    assert set(answers) == {"Old Tom", "the girl"}
    tom = answers["Old Tom"]["properties"]["age"]
    assert (tom["minimum"], tom["maximum"]) == (60, 90)
    assert all(a["required"] == ["name", "age"] for a in answers.values())
    assert all(a["additionalProperties"] is False for a in answers.values())
    assert "$defs" not in schema.model_json_schema()
    failure = parse('{"ages": []}', schema)
    assert "no answer for 'Old Tom', 'the girl'" in failure.problem  # type: ignore[union-attr]
    good = parse(
        '{"ages": [{"name": "Old Tom", "age": 70}, {"name": "the girl", "age": 9}]}', schema
    )
    assert good.resolved() == {"old tom": 70, "the girl": 9}  # type: ignore[union-attr]
