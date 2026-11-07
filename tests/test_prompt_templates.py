"""Prompt-template list layout (#760): one list item per line.

The Jinja environment trims the newline after a block tag (``trim_blocks``), so a list item
that ends in an inline ``{% if %}…{% endif %}`` used to eat its own newline and run every item
of the list onto one line. The templates end such a line with ``{% endif +%}``. A lint test
renders every delivered template, variants included, with two items in every list; the render
tests check each fixed template with one item per branch."""

from __future__ import annotations

import re
import types
import typing
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel

from epistrel.orchestrator.models import prompts
from epistrel.orchestrator.models.adapter import Message
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    AgeClue,
    DirectorCandidate,
    GenerationInputs,
    GuardrailCheckInputs,
    RewriteEntity,
    RewriteParseInputs,
    RewriteState,
    Subject,
)

RUN_ON = re.compile(r"\S- \w+")
"""A second ``- <word>`` item glued to the end of the one before it."""
LISTED = {
    "guardrail_check",
    "directive_parse",
    "rewrite_parse",
    "generation:age_repair",
    "generation:director_order",
}
"""The templates whose list items ended in an inline ``{% endif %}`` (#760)."""


def _value(annotation: Any, index: int) -> Any:
    """A value for ``annotation`` in which every list has two items (the second numbered 2)."""
    origin = typing.get_origin(annotation)
    if origin is Annotated:
        return _value(typing.get_args(annotation)[0], index)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _instance(annotation, index)
    if origin is Literal:
        return typing.get_args(annotation)[0]
    if origin in (list, tuple, set, frozenset):
        return [_value(typing.get_args(annotation)[0], n) for n in (1, 2)]
    if origin is types.UnionType or origin is typing.Union:
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _value(options[0], index)
    if annotation is bool:
        return True
    if annotation is int:
        return index
    if annotation is float:
        return 0.5
    return f"item{index}"


def _instance[T: BaseModel](schema: type[T], index: int = 1) -> T:
    return schema.model_validate(
        {
            name: info.examples[0] if info.examples else _value(info.annotation, index)
            for name, info in schema.model_fields.items()
        }
    )


TEMPLATES = [
    pytest.param(name, variant, id=prompts.variant_key(name, variant))
    for name, purpose in DELIVERED.items()
    for variant in (None, *purpose.variants)
]


@pytest.mark.parametrize(("name", "variant"), TEMPLATES)
def test_list_items_one_per_line(name: str, variant: str | None) -> None:
    """Every template, rendered with two items in every list, puts each ``- `` item on its own
    line. Complement asserted: the templates #760 fixed do render list items here, so the
    check is not empty for them."""
    purpose = DELIVERED[name]
    messages = prompts.render(purpose, _instance(purpose.input_schema), variant)
    items = [line for m in messages for line in m.content.splitlines() if line.startswith("- ")]
    if prompts.variant_key(name, variant) in LISTED:
        assert items, "no list item rendered"
    assert [line for line in items if RUN_ON.search(line[2:])] == []


def _section(content: str, heading: str) -> list[str]:
    """The lines under ``## <heading>``, up to the next heading."""
    lines = content.splitlines()
    start = lines.index(f"## {heading}") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


def _items(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("- ")]


def _guardrail(subjects: list[Subject]) -> list[Message]:
    inputs = GuardrailCheckInputs(text="t", policy_prompt="No minors.", subjects=subjects)
    return prompts.render(DELIVERED["guardrail_check"], inputs)


def test_guardrail_check_lists_one_subject_per_line() -> None:
    """The minors policy's only statement of who is a minor: one subject per line, the absent
    one marked."""
    system, _ = _guardrail(
        [
            Subject(label="character_1", age=30),
            Subject(label="character_2", age=12, present=False),
            Subject(label="character_3", age=40),
        ]
    )
    assert _section(system.content, "Subjects (label: true age)") == [
        "- character_1: age 30",
        "- character_2: age 12, not present",
        "- character_3: age 40",
    ]


def test_guardrail_check_without_subjects_has_no_subjects_section() -> None:
    """Complement: no subjects → no heading and no stray blank lines."""
    system, user = _guardrail([])
    assert "## Subjects" not in system.content
    assert system.content.endswith("## Policy\nNo minors.")
    assert "\n\n\n" not in system.content
    assert user.content == "t"


REWRITE = RewriteParseInputs(
    instruction="Bobby went to UCLA.",
    entities=[
        RewriteEntity(name="Bobby", kind="character", aliases=["Bob", "Rob"]),
        RewriteEntity(name="UCLA", kind="place"),
        RewriteEntity(name="Ada", kind="character", aliases=["Addie"]),
    ],
    states=[
        RewriteState(name="gold", type="int", scope="character", unit="coins"),
        RewriteState(name="weather", type="text", scope="story"),
        RewriteState(name="health", type="int", scope="character", owner="dice"),
    ],
)


@pytest.mark.parametrize("name", ["directive_parse", "rewrite_parse"])
def test_parse_templates_list_one_entity_and_state_per_line(name: str) -> None:
    _, user = prompts.render(DELIVERED[name], REWRITE)
    assert _items(_section(user.content, "Entities")) == [
        "- Bobby (character), also Bob, Rob",
        "- UCLA (place)",
        "- Ada (character), also Addie",
    ]
    states = _items(_section(user.content, "States"))
    owned = ", owned by a provider" if name == "rewrite_parse" else ""
    assert states == [
        "- gold: int, per character, in coins",
        "- weather: text, per story",
        f"- health: int, per character{owned}",
    ]


def _generation(**fields: object) -> GenerationInputs:
    return prompts.example_instance(GenerationInputs).model_copy(
        update={"working_memory": [], "recent_messages": [], **fields}
    )


def test_age_repair_lists_one_introduction_per_line() -> None:
    inputs = _generation(
        introductions=[
            AgeClue(name="the guard", clue="a grey beard"),
            AgeClue(name="the child"),
            AgeClue(name="the cook", clue="flour on her hands"),
        ]
    )
    _, user = prompts.render(DELIVERED["generation"], inputs, "age_repair")
    assert user.content.splitlines() == [
        "- the guard: a grey beard",
        "- the child",
        "- the cook: flour on her hands",
    ]


def test_director_order_lists_one_candidate_per_line() -> None:
    inputs = _generation(
        director_candidates=[
            DirectorCandidate(id="a1", name="Ada", card="The innkeeper."),
            DirectorCandidate(id="b2", name="Bo"),
            DirectorCandidate(id="c3", name="Cy", card="A bard."),
        ]
    )
    system = prompts.render(DELIVERED["generation"], inputs, "director_order")[0]
    assert _items(_section(system.content, "Who acts first")) == [
        "- Ada (id a1): The innkeeper.",
        "- Bo (id b2)",
        "- Cy (id c3): A bard.",
    ]
