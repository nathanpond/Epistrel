"""#523: a persona trait is a literal. Genesis registers the auto ``trait`` predicate with a
string object (migration 0111 types the ones already registered), so a declared or extracted
``trait`` claim normalizes its object as a literal, never as an unresolved entity. Complement:
the same words on an ``any`` predicate still resolve as an entity first (an unknown name stays
``unresolved_entity``), and a known entity is still the object of an ``any`` predicate."""

from __future__ import annotations

from epistrel.core.propositions.model import Layer, LiteralKind, Predicate, Proposition
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.verify.normalize import ClaimSketch, Normalized, Unnormalizable, normalize
from epistrel.core.verify.rules import VerdictReason
from tests import verify_support as vs
from tests.test_introduce import BASIS, CONTEXT

TRAIT_STR = "01JP00000000000000000000T1"
LOOSE = "01JP00000000000000000000T2"
REGISTRY = vs.registry_of(
    extra=(
        Predicate(
            id=TRAIT_STR,
            story_id=vs.STORY,
            name="trait",
            layer=Layer.AUTO,
            object_type=LiteralKind.STR.value,
            qualifiers=None,
        ),
        Predicate(id=LOOSE, story_id=vs.STORY, name="regards", layer=Layer.AUTO, qualifiers=None),
    )
)


def norm(relation: str, obj: str) -> Normalized:
    return normalize(
        ClaimSketch(subject_text="Bobby", relation_text=relation, object_text=obj, declared=True),
        NARRATOR,
        3,
        registry=REGISTRY,
        context=CONTEXT,
        basis=BASIS,
    )


def test_a_trait_object_is_a_literal() -> None:
    found = norm("trait", "conflict-avoidant")
    assert isinstance(found, Proposition), found
    assert found.object_id is None
    assert found.literal is not None
    assert (found.literal.kind, found.literal.value) == (LiteralKind.STR, "conflict-avoidant")


def test_an_any_object_still_resolves_as_an_entity() -> None:
    unknown = norm("regards", "conflict-avoidant")
    assert isinstance(unknown, Unnormalizable)
    assert unknown.reason is VerdictReason.UNRESOLVED_ENTITY
    known = norm("regards", "Ohio")
    assert isinstance(known, Proposition)
    assert (known.object_id, known.literal) == (vs.OHIO, None)
