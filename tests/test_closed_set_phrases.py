"""#580: the exclusion table that reads a character's background flavor as closed sets
(``core.verify.seeds.closed_sets_in``, ``detectors/closed_sets.json``): a phrasing that rules a
relation out for the character yields an empty closed set over it; one that does not (a
determiner or possessive before it, a preposition or relative clause after "the only child")
yields none."""

from __future__ import annotations

import pytest

from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.verify.seeds import closed_sets_in
from tests import verify_support as vs

HOLDER = vs.BOBBY


def families(text: str) -> list[str]:
    found = closed_sets_in(text, HOLDER, vs.REGISTRY)
    assert all((r.subject_id, r.members) == (HOLDER, ()) for r in found)
    by_id = {p.id: name for name, p in CORE_BY_NAME.items()}
    return [by_id[r.predicate_id] for r in found]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Linda grew up an only child.", ["sibling_of"]),
        ("Being an only child, she learned to be alone.", ["sibling_of"]),
        ("Tom grew up an only child in Dayton.", ["sibling_of"]),
        ("He has no siblings.", ["sibling_of"]),
        ("No brothers or sisters, she says.", ["sibling_of"]),
        ("He never married.", ["married_to"]),
        ("A childless widower.", ["parent_of"]),
        ("An only child who never married.", ["sibling_of", "married_to"]),
    ],
)
def test_an_exclusion_rules_the_relation_out(text: str, expected: list[str]) -> None:
    assert families(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Her only brother lives abroad.",
        "She was the only child at the party.",
        "He was the only child of a farmer.",
        "The only child who came was Ruth.",
        "She keeps a photo of her only child.",
        "Linda has a sister in the city.",
        "He was married twice.",
    ],
)
def test_a_background_that_rules_nothing_out_yields_none(text: str) -> None:
    assert families(text) == []
