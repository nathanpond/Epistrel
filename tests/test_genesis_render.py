"""The Lite prompt's scenario blocks and a catalog character's author notes (FR-LITE-1,
FR-SCN-7,9,11, FR-CAT-2; #122): pure rendering from a snapshot and a cast."""

from __future__ import annotations

from typing import Any

from epistrel.core.catalog.scenario_models import ScenarioDraft, Seed
from epistrel.core.genesis import render
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests.character_support import PRIVATE_FIELDS

CAST = (
    render.StoryMember(id="c1", name="June", role_id="mother"),
    render.StoryMember(id="c2", name="Tom Harrow", role_id="brother"),
    render.StoryMember(
        id="c4",
        name="Sam",
        role_id="player",
        is_player=True,
        persona={"name": "Sam", "gender": "man", "age": 23, "description": "Wry."},
    ),
)


def _text(**changes: Any) -> str:
    return render.scenario_text(scenarios.complete(**changes), CAST)


def test_scenario_text_renders_the_world_seeds_and_starts() -> None:
    shown = _text()
    assert shown.startswith(lg.SCENARIO_HEADING + "\n")
    assert shown.endswith(render.PREVAIL)
    assert scenarios.WORLD_SECRET in shown
    assert "- The orchard deed (also known as the deed)" in shown
    assert "Everyone knows:\n- The farm has been in the family for four generations." in shown
    assert (
        f"June knows:\n- Last winter {scenarios.ROLE_SECRET}. (Known to June and the family; "
        "June conceals it and may lie to protect it.)" in shown
    )
    assert "Tom Harrow " + lg.BELIEF_HEADING in shown
    assert "(Known to those who know Tom Harrow well; Tom Harrow does not volunteer it.)" in shown
    assert "- June is Sam's mother." in shown
    assert "- Tom Harrow is also called Tommy." in shown
    assert "### Player: Sam" in shown
    assert (
        "Known to June and Tom Harrow: Sam's name and exact age; others perceive man, in their "
        "early twenties." in shown
    )
    assert shown.index("Where each starts:") < shown.index(
        "Present at the start: June and Sam at The kitchen."
    )


def test_an_unfilled_role_renders_as_other_people() -> None:
    """The aunt's role is not filled in ``CAST``: its seed is a world fact, and a relationship
    to it names its public label."""
    shown = _text()
    assert "Other people:\n- Aunt Ivy: She keeps the only key to the old barn." in shown
    assert "Aunt Ivy is June's sister." in shown


def test_deferred_only_items_and_public_fields_are_not_rendered() -> None:
    shown = _text()
    body = scenarios.complete()
    for absent in (
        body["public_card"]["public_description"],
        "Her children are home for the weekend.",
        "inherits",
        "the reading of the will",
        "The brother forged the second sale.",
        scenarios.GUIDANCE_SECRET,
        "oil painting",
    ):
        assert absent not in shown, absent


def test_audience_sentences() -> None:
    def sentence(audience: list[str], disclosure: str = "open") -> str | None:
        seed = Seed.model_validate({"text": "x", "audience": audience, "disclosure": disclosure})
        roles = render._roles(ScenarioDraft.model_validate(scenarios.complete()), CAST)
        return render.audience_sentence(seed, "June", roles)

    assert sentence(["self"]) == "Known only to June."
    assert sentence(["public"]) is None
    assert sentence(["public"], "secret") == "June conceals it and may lie to protect it."
    assert (
        sentence(["self", "brother"], "guarded")
        == "Known to June and Tom Harrow; June does not volunteer it."
    )
    assert sentence(["perceivable"]) == "Evident to anyone who meets June."


def test_a_personality_seed_yields() -> None:
    body = scenarios.complete()
    body["cast"]["roles"][0]["seeded_memories"].append(
        scenarios.seed("She is cruel to the hired hands.", "mother", audience=["public"])
    )
    shown = render.scenario_text(body, CAST)
    assert "She is cruel to the hired hands." in shown
    assert shown.index("She is cruel") < shown.index("June's own author notes decide")


def test_director_notes() -> None:
    shown = render.director_notes(scenarios.complete(), CAST)
    assert shown.startswith(f"{lg.DIRECTOR_HEADING}\n{render.DIRECTOR_STANDING}")
    order = [
        shown.index(h)
        for h in (
            "### Plot hooks",
            "### Objectives",
            "### Guidance",
            "### Planned reveals",
            "### Milestones",
            "### Endings",
        )
    ]
    assert order == sorted(order)
    assert (
        "- The brother forged the second sale. Who may learn it: Sam. When: Only after the will "
        "is read." in shown
    )


def test_a_story_without_a_scenario_renders_nothing() -> None:
    assert render.render(None, CAST) == render.RenderedScenario("", "")
    assert render.scenario_text({}, ()) == ""
    assert render.director_notes({}, ()) == ""


def test_definition_notes_are_the_private_definition_as_prose() -> None:
    content = {
        "public_card": {"name": "Mara", "public_description": "On the card."},
        "definition": PRIVATE_FIELDS,
    }
    shown = render.definition_notes("Mara", content)
    assert PRIVATE_FIELDS["personality"] in shown
    assert PRIVATE_FIELDS["background_flavor"] in shown
    assert "They stand their ground." in shown.replace(
        "When threatened or challenged, they", "They"
    )
    assert "checks the wick three times a night" in shown
    for absent in (
        "On the card.",
        "41",
        PRIVATE_FIELDS["deflection_style"],
        PRIVATE_FIELDS["safe_fallback_text"],
    ):
        assert absent not in shown, absent


def test_age_band() -> None:
    assert [render.age_band(a) for a in (9, 15, 22, 35, 48, 99, 120)] == [
        "a child",
        "a teenager",
        "in their early twenties",
        "in their mid thirties",
        "in their late forties",
        "in their late nineties",
        "very old",
    ]
