"""#1019 (FR-MULTI-9, FR-REPAIR-2): the pure parts of "no beats from off-scene characters; a
narrowing that leaves only a name is not a line".

- ``core.repair.rungs.name_only``: a text of nothing but cast names, titles and punctuation;
- the repair ladder: a rung-1 narrowing to "Nathan Pond." is not accepted (the next rung runs,
  with a ``render_rejected`` constraint ``{"kind": "narrowing", "reason": "name_only"}``), while
  "Okay, Nathan." is; a regeneration candidate and the State deterministic rung's narrowing are
  held to the same check;
- ``core.epistemic.scene.present_for``: the turn actor's place, the whole cast only with no
  placements, no one with an unplaced actor in a placed story;
- ``core.projections.structured.scene``: the actor's ``present_at`` anchors its scene when it
  has no placement (``by_presence``), never for the stored rows.

The end-to-end parts are S-REACTIVE's M12 part
(``tests/scenario/test_s_reactive.py::test_s_reactive__off_scene``)."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

from epistrel.core.epistemic.scene import present_for
from epistrel.core.projections.structured import EntityRef, SceneView, scene
from epistrel.core.repair.rungs import (
    FALLBACK,
    NARROWING,
    REGENERATION,
    SUBSTITUTION,
    name_only,
    name_words,
    substitute,
)
from epistrel.core.states.effects import STATE_UNDECLARED, Finding
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.rules import Verdict, VerdictKind
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import GenerationInputs
from epistrel.orchestrator.repair.ladder import (
    NAME_ONLY,
    Context,
    Recheck,
    RepairLadder,
    state_deterministic,
)
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

CARLOS = "C" * 26
WORDS = name_words(["Nathan Pond", "Carlos Rivera", "Malik", "Big Jim"])
FLAGGED = "I am seventy years old."


# -- name_only -----------------------------------------------------------------------------------


def test_name_only() -> None:
    """Names (any word of any cast name or alias), titles and punctuation only; a possessive
    counts as the name; any other word, or a digit, is a line."""
    for text in ("Nathan Pond.", "Pond!", "Mr. Pond.", "Dr Rivera?", "Nathan's.", "...", "Jim"):
        assert name_only(text, WORDS), text
    for text in ("Okay, Nathan.", "Nathan, 3.", "Thanks, Mr. Pond.", "Carlos Rivera nods."):
        assert not name_only(text, WORDS), text
    assert name_only(f"Nathan{chr(0x2019)}s.", WORDS)  # a curly apostrophe's possessive too
    assert not name_only("Nathan Pond.", frozenset())  # no cast words: the check is off


# -- the ladder ----------------------------------------------------------------------------------


def _ladder(answers: Sequence[str] = ()) -> tuple[RepairLadder, list[str]]:
    """A ladder over a cast of ``WORDS`` whose every re-check passes, whose regeneration (when
    ``answers``) answers them in turn, and whose fallback is Carlos's last-resort line."""
    found = object.__new__(RepairLadder)
    found.max_attempts = len(answers)
    found.render_of = lambda _: "character"
    found.used = {}
    found.renders = []
    found.extraction_calls = 0
    found.check_calls = 0
    found.cascade_threshold = 5
    found.sketches_of = {}
    found.cast_names = {}
    found.name_words = WORDS
    found.inputs_of = (
        (lambda _: cast(GenerationInputs, SimpleNamespace())) if answers else (lambda _: None)
    )
    found.fallback = cast(
        Any,
        SimpleNamespace(
            candidates=lambda: [("last_resort", "Carlos Rivera changes the subject.")],
            name="Carlos Rivera",
        ),
    )
    queue = list(answers)
    asked: list[str] = []

    async def context(beat: TurnBeat, violations: Sequence[CheckedClaim]) -> Context:
        return Context()

    async def regenerate(
        beat: TurnBeat, *_: Any, **__: Any
    ) -> tuple[TurnBeat, list[Any], str, int] | None:
        asked.append(beat.text)
        if not queue:
            return None
        made = TurnBeat(kind=beat.kind, beat=beat.beat, text=queue.pop(0), character_id=CARLOS)
        return made, [], "h", 0

    async def ok(_: str) -> bool:
        return True

    async def recheck(candidate: TurnBeat, *_: Any) -> Recheck:
        return Recheck(beat=candidate, claims=[])

    found.context = context  # type: ignore[method-assign]
    found._regenerate = regenerate  # type: ignore[method-assign]
    found.output_ok = ok
    found.recheck = recheck
    return found, asked


def _flagged(text: str) -> tuple[TurnBeat, list[CheckedClaim]]:
    """A ``say`` of Carlos's whose last sentence (``FLAGGED``) is contradicted."""
    beat = TurnBeat(kind="say", beat=3, text=text, character_id=CARLOS)
    start = text.index(FLAGGED)
    violation = CheckedClaim(
        claim=DetectedClaim(
            span=(3, start, start + len(FLAGGED)),
            layer="detector",
            category="quantity",
            channel="say",
            speaker=CARLOS,
        ),
        result=VerifyResult(
            claim=None, verdict=Verdict(VerdictKind.CONTRADICTED), kind=VerdictKind.CONTRADICTED
        ),
    )
    return beat, [violation]


async def test_a_name_is_not_a_line() -> None:
    """ "Nathan Pond. <flagged>" narrowed to "Nathan Pond." is not accepted at rung 1 (before
    #1019 it was): with no regeneration it ends at the fallback line, and the rejected
    narrowing is recorded with the ``name_only`` constraint."""
    ladder, _ = _ladder()
    beat, violations = _flagged(f"Nathan Pond. {FLAGGED}")
    result = await ladder.run(beat, violations, TurnBudget(max_calls=50))
    assert result.rung == FALLBACK
    assert result.beat is not None
    assert result.beat.text == "Carlos Rivera changes the subject."
    (narrowed,) = [r for r in result.rejected if r.rung == NARROWING and r.text == "Nathan Pond."]
    assert narrowed.event().constraints == (NAME_ONLY,)
    assert NAME_ONLY == {"kind": "narrowing", "reason": "name_only"}


async def test_a_word_beside_the_name_is_a_line() -> None:
    """The complement: "Okay, Nathan. <flagged>" is accepted at rung 1 as "Okay, Nathan."."""
    ladder, _ = _ladder()
    beat, violations = _flagged(f"Okay, Nathan. {FLAGGED}")
    result = await ladder.run(beat, violations, TurnBudget(max_calls=50))
    assert result.rung == NARROWING
    assert result.beat is not None
    assert result.beat.text == "Okay, Nathan."


async def test_a_regenerated_name_is_not_a_line() -> None:
    """A regeneration candidate that is only a name is rejected like a failing candidate; the
    next one with a word of its own is accepted."""
    ladder, asked = _ladder(["Mr. Pond.", "Fine, Mr. Pond."])
    beat, violations = _flagged(f"Nathan Pond. {FLAGGED}")
    result = await ladder.run(beat, violations, TurnBudget(max_calls=50))
    assert len(asked) == 2
    assert result.rung == REGENERATION
    assert result.beat is not None
    assert result.beat.text == "Fine, Mr. Pond."
    assert any(r.rung == REGENERATION and r.reasons == (NAME_ONLY,) for r in result.rejected)


def _swapped(text: str, answers: Sequence[str] = ()) -> tuple[RepairLadder, list[str], Any, Any]:
    """A ladder whose rung 2 swaps "Malik" for "Nathan Pond" (``rungs.substitute``, as
    ``RepairLadder._substitution`` does once it has the canon value), and a one-sentence
    ``say`` of Carlos's, contradicted as a whole (so rung 1 has no sentence to remove)."""
    ladder, asked = _ladder(answers)

    def swap(beat: TurnBeat, *_: Any) -> tuple[str, list[Any]] | None:
        made = substitute(beat.text, "Malik", "Nathan Pond")
        return None if made is None else (made, [])

    ladder._substitution = swap  # type: ignore[method-assign,assignment]
    beat = TurnBeat(kind="say", beat=3, text=text, character_id=CARLOS)
    violation = CheckedClaim(
        claim=DetectedClaim(
            span=(3, 0, len(text)),
            layer="detector",
            category="declared",
            channel="say",
            speaker=CARLOS,
        ),
        result=VerifyResult(
            claim=None, verdict=Verdict(VerdictKind.CONTRADICTED), kind=VerdictKind.CONTRADICTED
        ),
    )
    return ladder, asked, beat, [violation]


async def test_a_substituted_name_is_not_a_line() -> None:
    """#1041 (#1019): rung 2 is held to the same check. "Malik." with the name swapped for
    canon's is "Nathan Pond.", only a name: it is not accepted at rung 2 and is recorded with
    the ``name_only`` reason; the next rung runs and its regeneration is accepted."""
    ladder, asked, beat, violations = _swapped("Malik.", ["It was Nathan Pond."])
    result = await ladder.run(beat, violations, TurnBudget(max_calls=50))
    assert result.rung == REGENERATION
    assert result.beat is not None
    assert result.beat.text == "It was Nathan Pond."
    assert asked == ["Malik."]
    (swapped,) = [r for r in result.rejected if r.rung == SUBSTITUTION]
    assert (swapped.text, swapped.reasons) == ("Nathan Pond.", (NAME_ONLY,))
    assert not [r for r in result.rejected if r.rung == NARROWING]

    # With no regeneration either, it ends at the fallback line, never at the name.
    ladder, _, beat, violations = _swapped("Malik.")
    result = await ladder.run(beat, violations, TurnBudget(max_calls=50))
    assert result.rung == FALLBACK
    assert result.beat is not None
    assert result.beat.text == "Carlos Rivera changes the subject."


async def test_a_substitution_with_a_word_is_a_line() -> None:
    """The complement: "It was Malik." swapped to "It was Nathan Pond." is accepted at rung 2,
    and no regeneration is asked for."""
    ladder, asked, beat, violations = _swapped("It was Malik.", ["Never mind."])
    result = await ladder.run(beat, violations, TurnBudget(max_calls=50))
    assert result.rung == SUBSTITUTION
    assert result.beat is not None
    assert result.beat.text == "It was Nathan Pond."
    assert asked == []
    assert not [r for r in result.rejected if r.reasons == (NAME_ONLY,)]


def test_a_state_narrowing_to_a_name_leaves_nothing() -> None:
    """The State deterministic rung: an undeclared change's sentence removed leaving only a
    name leaves nothing (the fallback line then); with a word left it stands."""
    for text, kept in (
        (f"Nathan Pond. {FLAGGED}", None),
        (f"Okay, Nathan. {FLAGGED}", "Okay, Nathan."),
    ):
        start = text.index(FLAGGED)
        finding = Finding(
            tag=STATE_UNDECLARED, beat=3, state="age", span=(start, start + len(FLAGGED))
        )
        beat = TurnBeat(kind="say", beat=3, text=text, character_id=CARLOS)
        found = state_deterministic(beat, [finding], words=WORDS)
        assert (None if found is None else found.text) == kept


# -- the present set -----------------------------------------------------------------------------


def _view(location: str | None, *present: str) -> SceneView:
    return SceneView(
        turn=1,
        variant=None,
        location=None if location is None else EntityRef(location, "place", location),
        present=tuple(EntityRef(e, "character", e) for e in present),
        viewpoint="omniscient",
    )


ENTITY = {"e-nathan": "nathan", "e-malik": "malik", "e-ethan": "ethan", "e-carlos": "carlos"}
CAST = ("nathan", "malik", "ethan", "carlos", "jared")


def test_present_for() -> None:
    """The characters at the actor's place; the whole cast with no placements; no one for an
    unplaced actor in a placed story; an entity that is no cast member never counts."""
    at_camp = _view("camp-a", "e-nathan", "e-malik", "e-ethan", "e-fire")
    assert present_for(at_camp, "e-nathan", characters=ENTITY, cast=CAST, placed=True) == {
        "nathan",
        "malik",
        "ethan",
    }
    assert present_for(None, None, characters=ENTITY, cast=CAST, placed=False) == set(CAST)
    unplaced = _view(None)
    assert present_for(unplaced, "e-nathan", characters=ENTITY, cast=CAST, placed=True) == set()
    assert present_for(at_camp, None, characters=ENTITY, cast=CAST, placed=True) == set()


def _row(entity: str, location: str, **f: Any) -> dict[str, Any]:
    return {
        "entity_id": entity,
        "location_id": location,
        "since_turn": None,
        "since_slot": None,
        "valid_from": None,
        "valid_to": None,
        "source_fact_id": f"f-{entity}",
        "closed_by_fact_id": None,
        "closed_turn": None,
        "closed_slot": None,
        **f,
    }


def _presence(subject: str, place: str) -> dict[str, Any]:
    found = _row(subject, place)
    del found["entity_id"], found["location_id"]
    return {**found, "subject_id": subject, "object_id": place}


def test_scene_by_presence() -> None:
    """An actor with no placement takes the place of its ``present_at`` row only with
    ``by_presence`` (the turn actor's scene); the stored scene keeps answering no scene."""
    places = [_row("e-malik", "camp-a")]
    presences = [_presence("e-nathan", "camp-a")]
    assert scene(places, presences, "e-nathan", (1, 0), 0).location_id is None
    found = scene(places, presences, "e-nathan", (1, 0), 0, by_presence=True)
    assert (found.location_id, found.present) == ("camp-a", ("e-malik", "e-nathan"))
