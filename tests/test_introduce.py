"""Minting ids for new places and objects at normalization (#479; ``core.verify.introduce``).

``normalize`` with the turn's ``Introductions`` gives an unresolved argument a minted id only
when the predicate's argument type on that side is ``place`` or ``item``, the story's index
found nothing by that name (``no_mention`` or ``unknown_name``), and the text is no indefinite.
A name the story already holds resolves to its entity, and a name this turn already minted
reuses that id. Literal, ``any`` and ``person`` slots, unregistered predicates, pronouns and
indefinites keep today's ``uncertain`` result. The extraction model's snake_case arguments
(``the_inn``) are read with spaces."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from epistrel.core.canon.canonize import NewEntity
from epistrel.core.propositions.model import Proposition
from epistrel.core.resolution.entities import NARRATOR, EntityEntry, EntityIndex, ResolutionContext
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.introduce import Introductions, entity_name
from epistrel.core.verify.normalize import (
    ClaimSketch,
    Normalized,
    RegisterPredicate,
    Unnormalizable,
    normalize,
)
from epistrel.core.verify.rules import VerdictReason
from epistrel.orchestrator.models.registry import ExtractedClaim
from epistrel.orchestrator.verify.extraction import extraction_sketch, surfaces
from tests import verify_support as vs

BASIS = TimeBasis(minute_zero=vs.year(2020), epoch=datetime(2000, 1, 1, tzinfo=UTC))
CONTEXT = ResolutionContext(
    EntityIndex(
        [
            EntityEntry(vs.BOBBY, "person", "Bobby", "masculine"),
            EntityEntry(vs.OHIO, "place", "Ohio"),
            EntityEntry(vs.KITCHEN, "place", "the kitchen", descriptors=("kitchen",)),
        ]
    )
)


def norm(subject: str, relation: str, obj: str | None, minted: Introductions | None) -> Normalized:
    return normalize(
        ClaimSketch(subject_text=subject, relation_text=relation, object_text=obj, declared=True),
        NARRATOR,
        3,
        registry=vs.REGISTRY,
        context=CONTEXT,
        basis=BASIS,
        introduce=minted,
    )


def test_a_place_slot_mints_and_a_repeat_reuses_it() -> None:
    minted = Introductions()
    first = norm("Bobby", "located_at", "the old mill", minted)
    assert isinstance(first, Proposition)
    assert first.object_id is not None
    ((mill_id, mill),) = minted.minted.items()
    assert (first.object_id, mill.kind, mill.canonical_name) == (mill_id, "place", "the old mill")
    # The same name, and a description of it, resolve to the minted id: one entity per turn.
    for again in ("the old mill", "The Old Mill", "the mill"):
        found = norm("Bobby", "located_at", again, minted)
        assert isinstance(found, Proposition), again
        assert found.object_id == mill_id, again
    assert len(minted.minted) == 1
    assert minted.introduces(first) == (NewEntity(mill_id, "the old mill"),)


def test_an_item_slot_mints_on_either_side() -> None:
    minted = Introductions()
    held = norm("Bobby", "holds", "the brass key", minted)
    assert isinstance(held, Proposition)
    (key,) = minted.minted.values()
    assert (held.object_id, key.kind) == (key.id, "item")
    # ``held_by``'s subject is the item: the same key, by name.
    back = norm("the brass key", "held_by", "Bobby", minted)
    assert isinstance(back, Proposition)
    assert key.id in (back.subject_id, back.object_id)
    assert len(minted.minted) == 1


def test_a_name_the_story_holds_is_never_minted() -> None:
    minted = Introductions()
    for place, entity in (("Ohio", vs.OHIO), ("the kitchen", vs.KITCHEN), ("kitchen", vs.KITCHEN)):
        found = norm("Bobby", "located_at", place, minted)
        assert isinstance(found, Proposition), place
        assert found.object_id == entity, place
    assert minted.minted == {}


@pytest.mark.parametrize(
    ("subject", "relation", "obj", "reason"),
    [
        # a literal slot: a duration is never an entity
        ("Bobby", "age", "approximately eight years", VerdictReason.ARGUMENT_TYPE),
        # ``any`` and ``person`` slots
        ("the regulars", "located_at", "Ohio", VerdictReason.UNRESOLVED_ENTITY),
        ("Bobby", "knows", "the ferryman", VerdictReason.UNRESOLVED_ENTITY),
        ("the ferryman", "sibling_of", "Bobby", VerdictReason.UNRESOLVED_ENTITY),
        # indefinites leave the argument open; a pronoun with no antecedent is no name
        ("Bobby", "holds", "something", VerdictReason.UNRESOLVED_ENTITY),
        ("Bobby", "holds", "a wooden mug", VerdictReason.UNRESOLVED_ENTITY),
        ("Bobby", "located_at", "it", VerdictReason.UNRESOLVED_ENTITY),
    ],
)
def test_other_slots_and_phrases_never_mint(
    subject: str, relation: str, obj: str, reason: VerdictReason
) -> None:
    minted = Introductions()
    found = norm(subject, relation, obj, minted)
    assert isinstance(found, Unnormalizable), found
    assert found.reason is reason
    assert minted.minted == {}


def test_an_unregistered_predicate_never_mints() -> None:
    """A free-form relation has no argument types: its untracked argument stays unresolved
    (the residue of #479 measured live)."""
    minted = Introductions()
    found = norm("the inn", "has_been_in_business_for", "a hundred years", minted)
    assert found == norm("the inn", "has_been_in_business_for", "a hundred years", None)
    assert isinstance(found, Unnormalizable)
    assert minted.minted == {}
    # With every argument resolved, the relation registers as before.
    assert isinstance(norm("Bobby", "wipes_down", "the kitchen", minted), RegisterPredicate)


def test_without_introductions_nothing_changes() -> None:
    found = norm("Bobby", "located_at", "the old mill", None)
    assert isinstance(found, Unnormalizable)
    assert found.reason is VerdictReason.UNRESOLVED_ENTITY


def test_snake_case_arguments_read_with_spaces() -> None:
    sketch = extraction_sketch(
        ExtractedClaim(
            beat=0,
            span="the inn",
            subject="Maren's_husband",
            predicate="located_at",
            object="the_old_mill",
            time="last_winter",
            confidence=1.0,
        ),
        (0, 7),
    )
    assert (sketch.subject_text, sketch.object_text, sketch.time_text) == (
        "Maren's husband",
        "the old mill",
        "last winter",
    )
    assert sketch.relation_text == "located_at"
    assert entity_name("  'the_inn'. ") == "the inn"


# -- #479's second part (ADR-163): what an unresolved weak-relation claim is marked ------------

WREN = "01JA00000000000000000000W1"
MAREN = "01JA00000000000000000000M1"
CAST = ResolutionContext(
    EntityIndex(
        [
            EntityEntry(MAREN, "person", "Maren", "feminine", character_id=MAREN),
            EntityEntry(WREN, "person", "Wren", "feminine", character_id=WREN),
            EntityEntry(vs.OHIO, "place", "Ohio"),
        ]
    ),
    player_id=WREN,
)


def said(subject: str, relation: str, obj: str | None, speaker: str = MAREN) -> Normalized:
    return normalize(
        ClaimSketch(subject_text=subject, relation_text=relation, object_text=obj, declared=True),
        speaker,
        3,
        registry=vs.REGISTRY,
        context=CAST,
        basis=BASIS,
        introduce=Introductions(),
    )


@pytest.mark.parametrize(
    ("subject", "relation", "obj"),
    [
        ("Maren", "looks_up_from", "wiping_down_the_bar"),  # an object no entity holds
        ("the inn", "has_been_in_business_for", "a hundred years"),  # a subject too
        ("the regulars", "needed", None),  # object-less
        ("the inn", "is_near", "Ohio"),  # the other argument is a place, not a character
        ("Maren", "talks_about", "the Crossroads Inn"),  # an unknown proper name
        ("Maren", "looks_up_from", "it"),  # a pronoun with no antecedent
    ],
)
def test_a_weak_relation_naming_something_untracked_is_marked(
    subject: str, relation: str, obj: str | None
) -> None:
    found = said(subject, relation, obj)
    assert isinstance(found, Unnormalizable), found
    assert (found.weak_relation, found.touches_character, found.untracked_weak) == (
        True,
        False,
        True,
    )


@pytest.mark.parametrize(
    ("subject", "relation", "obj"),
    [
        ("Wren", "looks_up_from", "the bar"),  # the player's character
        ("the bar", "reminds", "Wren"),
        ("the stew", "pleases", "Maren"),  # Maren, from the narrator: another character
    ],
)
def test_a_weak_relation_touching_another_character_is_not_marked(
    subject: str, relation: str, obj: str | None
) -> None:
    found = said(
        subject, relation, obj, speaker=MAREN if "Maren" not in (subject, obj) else NARRATOR
    )
    assert isinstance(found, Unnormalizable), found
    assert found.weak_relation
    assert found.touches_character
    assert not found.untracked_weak


def test_a_composed_renders_own_character_is_not_the_narrators_other() -> None:
    """#484 (the owner's option (a)): on the composition path the narrator's "other character"
    excludes the turn's render characters, so a weak texture claim about Maren surfaces as on
    her own render's beats; Wren, not a render character, is still another."""
    composed = dataclasses.replace(CAST, composing=frozenset({MAREN}))
    sketch = ClaimSketch(
        subject_text="Maren", relation_text="wipes_down", object_text="the counter", declared=True
    )
    found = normalize(
        sketch,
        NARRATOR,
        3,
        registry=vs.REGISTRY,
        context=composed,
        basis=BASIS,
        introduce=Introductions(),
    )
    assert isinstance(found, Unnormalizable)
    assert (found.weak_relation, found.touches_character) == (True, False)
    other = normalize(
        replace_subject(sketch, "Wren"),
        NARRATOR,
        3,
        registry=vs.REGISTRY,
        context=composed,
        basis=BASIS,
        introduce=Introductions(),
    )
    assert isinstance(other, Unnormalizable)
    assert other.touches_character


def replace_subject(sketch: ClaimSketch, subject: str) -> ClaimSketch:
    return dataclasses.replace(sketch, subject_text=subject)


@pytest.mark.parametrize(
    ("subject", "relation", "obj"),
    [
        ("Maren", "knows", "the ferryman"),  # a core relation: option 2 governs it
        ("the ferryman", "located_at", "Ohio"),
    ],
)
def test_other_unresolved_claims_are_not_untracked_weak(
    subject: str, relation: str, obj: str | None
) -> None:
    found = said(subject, relation, obj)
    assert isinstance(found, Unnormalizable), found
    assert not found.untracked_weak


def test_declared_and_extraction_claims_surface_detector_findings_do_not() -> None:
    """A declared or extraction claim surfaces, merged with each other or not; a detector
    finding, a claim merged with one, and protected content keep the safe disposition
    (``extraction.surfaces``)."""
    found = said("Maren", "looks_up_from", "wiping_down_the_bar")
    assert isinstance(found, Unnormalizable)
    own = DetectedClaim(
        span=(2, 0, 10),
        layer="extraction",
        category="extracted",
        channel="do",
        speaker=MAREN,
        unnormalized=found,
    )
    assert surfaces(own)
    assert not surfaces(replace(own, layer="detector", category="possession"))
    assert surfaces(replace(own, layer="declared", category="declared"))
    assert surfaces(replace(own, merged=("extraction",), layer="declared"))
    assert not surfaces(replace(own, merged=("detector",)))
    assert not surfaces(replace(own, category="protected_content"))
    assert not surfaces(replace(own, unnormalized=said("Maren", "knows", "the ferryman")))  # type: ignore[arg-type]
