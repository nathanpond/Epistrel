"""#1013 (FR-MULTI-8,11): the copied-line check and its repair, as pure units: the
normalization, the 5-word floor, the sentence splitting it shares with narrowing, what is
compared, the synthetic claim, and the ladder's one regeneration or drop. The end-to-end parts
are I-12 M12 (``tests/integration/test_i_12.py::test_i_12__m12_copied_lines*``)."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

from epistrel.core.repair.rungs import FALLBACK, REGENERATION, sentences
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.rules import Verdict, VerdictKind
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import GenerationInputs
from epistrel.orchestrator.repair.ladder import (
    COPIED_LINE,
    REPAIR_DETERMINISTIC,
    REPAIR_DROPPED,
    SAFE_FALLBACK,
    Context,
    Recheck,
    RepairLadder,
    copy_dropped,
    drop_plan,
)
from epistrel.orchestrator.structured import copies
from epistrel.orchestrator.structured.multi import MultiReport
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

MALIK, JARED, SAM = "M" * 26, "J" * 26, "S" * 26
LINE = "Hey, we lost, that's the game. Shake it off, we need everybody sharp tonight."
ONE = "Hey, we lost, that's the game."
OWN = "I'm not ready to let this one go, honestly."


def _beat(number: int, text: str, who: str | None = MALIK, kind: str = "say", **f: Any) -> TurnBeat:
    return TurnBeat(kind=cast(Any, kind), beat=number, text=text, character_id=who, **f)


# -- normalization -------------------------------------------------------------------------------


def test_normalize() -> None:
    """Case, curly quotes and apostrophes, dashes, other punctuation and white space; an
    apostrophe inside a word stays."""
    assert copies.normalize("Hey,  WE lost!") == "hey we lost"
    assert copies.normalize("\u201cThat\u2019s the game,\u201d he said.") == (
        "that's the game he said"
    )
    assert copies.normalize("'Tis well-known \u2014 really\u2026") == "tis well known really"
    assert copies.normalize("\uff28ey") == "hey"  # NFKC: a full-width letter
    assert copies.normalize("Don't\tstop") == "don't stop"


def test_words() -> None:
    """A word is a ``[\\w']+`` run of the normalized text."""
    assert copies.words(copies.normalize("That's well-known.")) == 3
    assert copies.words(copies.normalize("I hear you, man.")) == 4


def test_sentences_are_narrowings() -> None:
    """The check's sentences are the narrowing rung's (``core.repair.rungs.sentences``)."""
    text = 'He said "Go." Then left! Really?'
    assert [(s, e) for s, e, _ in copies.sentence_keys(text)] == sentences(text)


# -- what is compared ----------------------------------------------------------------------------


def test_a_copy_of_another_speaker() -> None:
    """A word-for-word repeat (punctuation, case and quotes aside) of another speaker's
    sentence of 5+ words is a copy; its source is the first beat holding it."""
    malik = _beat(2, LINE)
    jared = _beat(3, "\u201cHEY we lost \u2014 that\u2019s the game!\u201d " + OWN, who=JARED)
    found = copies.find(jared, [malik])
    assert [(c.beat, c.source_beat, c.source_speaker) for c in found] == [(3, 2, MALIK)]
    (copy,) = found
    assert jared.text[copy.span[0] : copy.span[1]].startswith("\u201cHEY")


def test_short_sentences_are_never_copies() -> None:
    """Under 5 words is never a copy (complement: 5 words is)."""
    for short in ("Yeah.", "Let's go.", "I hear you.", "I hear you, man."):
        assert copies.find(_beat(3, short, JARED), [_beat(2, short)]) == []
    five = "We need to talk tonight."
    assert len(copies.find(_beat(3, five, JARED), [_beat(2, five)])) == 1


def test_what_is_never_compared() -> None:
    """Out of scope: a speaker repeating itself, the player's input (beat 1, and a sentence
    echoing it), the player's co_author narration, a narrator beat, a think, an attempt line,
    an exempt beat (a fallback line or render), and a later beat."""
    assert copies.find(_beat(3, LINE), [_beat(2, LINE)]) == []  # its own line
    assert copies.find(_beat(3, LINE, JARED), [_beat(1, LINE, "P" * 26)]) == []
    assert copies.find(_beat(3, LINE, JARED), [_beat(2, LINE)], player=[LINE]) == []
    co_author = _beat(2, LINE, "P" * 26, kind="do", narrated_by="player")
    assert copies.find(_beat(3, LINE, JARED), [co_author]) == []
    narration = _beat(2, LINE, None, kind="narration", narrator=True)
    assert copies.find(_beat(3, LINE, JARED), [narration]) == []
    assert copies.find(_beat(3, LINE, JARED), [_beat(2, LINE, kind="think")]) == []
    assert copies.find(_beat(3, LINE, JARED, kind="think"), [_beat(2, LINE)]) == []
    assert copies.find(_beat(3, LINE, JARED), [_beat(2, LINE, attempt=True)]) == []
    assert copies.find(_beat(3, LINE, JARED), [_beat(2, LINE)], exempt={2}) == []
    assert copies.find(_beat(3, LINE, JARED), [_beat(4, LINE)]) == []
    # Complement: a do and a narrated reaction are compared, both ways.
    reaction = _beat(3, ONE, SAM, kind="do", narrated_by=MALIK)
    assert len(copies.find(reaction, [_beat(2, ONE, kind="do")])) == 1
    assert len(copies.find(_beat(4, ONE), [reaction])) == 1


def test_the_claim() -> None:
    """A copy is an ``uncertain`` claim of its beat that always needs repair, tagged
    ``copied_line``, its source in the verdict's payload (ids only)."""
    jared = _beat(3, LINE, JARED)
    first, second = copies.claims_of(copies.find(jared, [_beat(2, LINE)]), jared, "op")
    assert second.claim_id == "op:copied:3:1"  # each copied sentence is a claim
    claim = first
    assert claim.claim_id == "op:copied:3:0"
    assert claim.claim.span == (3, 0, len("Hey, we lost, that's the game."))
    result = claim.result
    assert result is not None
    assert result.needs_repair
    assert result.repair_tag == COPIED_LINE
    assert copies.is_copy(claim)
    payload = result.as_payload(claim.claim_id)
    assert payload["cause"] == COPIED_LINE
    assert payload["copied_from"] == {"beat": 2, "character_id": MALIK}
    assert LINE not in str(payload)


# -- the repair ----------------------------------------------------------------------------------


def _copy(beat: TurnBeat, source: TurnBeat) -> list[CheckedClaim]:
    return copies.claims_of(copies.find(beat, [source]), beat, "op")


def _ladder(
    max_attempts: int, answers: Sequence[str | None], *, copied_again: bool = False
) -> tuple[RepairLadder, list[str]]:
    """A ladder whose regeneration answers ``answers`` in turn (``None``: unusable) and whose
    re-check flags a candidate repeating ``LINE``'s first sentence (``copied_again``) as a
    copy. Returns the ladder and the texts it was asked to rewrite."""
    found = object.__new__(RepairLadder)
    found.max_attempts = max_attempts
    found.render_of = lambda _: "character"
    found.used = {}
    found.renders = []
    found.extraction_calls = 0
    found.check_calls = 0
    found.cascade_threshold = 5
    found.sketches_of = {}
    found.cast_names = {MALIK: "Malik"}
    found.inputs_of = lambda _: cast(GenerationInputs, SimpleNamespace())
    asked: list[str] = []
    queue = list(answers)

    async def context(beat: TurnBeat, violations: Sequence[CheckedClaim]) -> Context:
        made = [await found._constraint(v, cast(Any, None), {}) for v in violations]
        return Context(constraints=tuple(dict.fromkeys(made)))

    async def regenerate(
        beat: TurnBeat, *_: Any, **__: Any
    ) -> tuple[TurnBeat, list[Any], str, int] | None:
        asked.append(beat.text)
        text = queue.pop(0) if queue else None
        return None if text is None else (TurnBeat(**{**_fields(beat), "text": text}), [], "h", 0)

    async def ok(_: str) -> bool:
        return True

    async def recheck(candidate: TurnBeat, *_: Any) -> Recheck:
        source = _beat(2, LINE)
        return Recheck(beat=candidate, claims=_copy(candidate, source) if copied_again else [])

    found.context = context  # type: ignore[method-assign]
    found._regenerate = regenerate  # type: ignore[method-assign]
    found.output_ok = ok
    found.recheck = recheck
    return found, asked


def _fields(beat: TurnBeat) -> dict[str, Any]:
    return {f: getattr(beat, f) for f in TurnBeat.__dataclass_fields__}


async def test_a_copy_is_regenerated_once() -> None:
    """A copied beat skips narrowing (it has a sentence to keep) and is regenerated once, with
    the ``no_copy`` constraint naming the source speaker and no text of the line."""
    repairs, asked = _ladder(1, [OWN])
    jared = _beat(3, LINE, JARED)
    result = await repairs.run(jared, _copy(jared, _beat(2, LINE)), TurnBudget(max_calls=50))
    assert asked == [LINE]
    assert result.rung == REGENERATION
    assert result.beat is not None
    assert result.beat.text == OWN
    (constraint,) = result.context.constraints
    assert (constraint.kind, constraint.text) == (
        "no_copy",
        "Do not repeat what Malik just said; respond in your own words.",
    )
    assert result.copied
    assert result.detail() == []  # a fixed copy reports through its repair entries only


async def test_a_failed_copy_is_dropped() -> None:
    """A regeneration that copies again, or gives nothing usable, drops the beat
    (``repair_dropped``, ``copied_line``), never the fallback line; exactly one regeneration
    even under ``max_attempts: 3``."""
    for answers, again in (([LINE] * 3, True), ([None] * 3, False)):
        repairs, asked = _ladder(3, answers, copied_again=again)
        jared = _beat(3, LINE, JARED)
        result = await repairs.run(jared, _copy(jared, _beat(2, LINE)), TurnBudget(max_calls=50))
        assert len(asked) == 1
        assert result.beat is None
        assert not result.fallback
        assert result.drop_cause == COPIED_LINE
        assert result.degradation == [(REPAIR_DROPPED, COPIED_LINE)]
        (entry,) = result.detail()
        assert (entry["step"], entry["cause"], entry["rung"], entry["violations"]) == (
            REPAIR_DROPPED,
            COPIED_LINE,
            "dropped",
            [COPIED_LINE],
        )
        # The drop rule's post-pass neither keeps nor drops it.
        assert drop_plan([], [result]) == []


async def test_no_attempt_left_drops_the_copy() -> None:
    """With no attempt left (``max_attempts: 0``, or the render's used) or no budget, the copy
    is dropped without a regeneration and with no ``repair_deterministic``."""
    jared = _beat(3, LINE, JARED)
    for attempts, budget in ((0, TurnBudget(max_calls=50)), (1, TurnBudget(max_calls=0))):
        repairs, asked = _ladder(attempts, [OWN])
        result = await repairs.run(jared, _copy(jared, _beat(2, LINE)), budget)
        assert asked == []
        assert result.beat is None
        assert result.degradation == [(REPAIR_DROPPED, COPIED_LINE)]
        assert REPAIR_DETERMINISTIC not in {d[0] for d in result.degradation}


async def test_a_candidate_that_copies_is_rejected() -> None:
    """A non-copy beat whose regeneration copies another speaker is rejected like any failing
    candidate (the next rung: the fallback line here), with ``copied_line`` added to what
    flagged it; it is no copy drop."""
    repairs, _ = _ladder(1, [LINE], copied_again=True)
    repairs.fallback = cast(
        Any,
        SimpleNamespace(
            candidates=lambda: [("last_resort", "Jared changes the subject.")], name="Jared"
        ),
    )
    old = _beat(3, "I am seventy years old.", JARED)
    violation = CheckedClaim(
        claim=DetectedClaim(
            span=(3, 0, len(old.text)),
            layer="detector",
            category="quantity",
            channel="say",
            speaker=JARED,
        ),
        result=VerifyResult(
            claim=None,
            verdict=Verdict(VerdictKind.CONTRADICTED),
            kind=VerdictKind.CONTRADICTED,
        ),
    )

    async def context(beat: TurnBeat, violations: Sequence[CheckedClaim]) -> Context:
        return Context()

    repairs.context = context  # type: ignore[method-assign]
    result = await repairs.run(old, [violation], TurnBudget(max_calls=50))
    assert result.rung == FALLBACK
    assert result.drop_cause is None
    assert COPIED_LINE in result.codes
    assert (SAFE_FALLBACK, "repair") in result.degradation


def test_copy_dropped_without_the_ladder() -> None:
    """The repair module off: a copied beat is dropped and reports why."""
    jared = _beat(3, LINE, JARED)
    result = copy_dropped(jared, _copy(jared, _beat(2, LINE)))
    assert result.beat is None
    assert result.drop_cause == COPIED_LINE
    assert [(d["step"], d["violations"]) for d in result.detail()] == [
        (REPAIR_DROPPED, [COPIED_LINE])
    ]
    assert result.rejected[0].text == LINE


def test_the_multi_report_counts_copies() -> None:
    """``turn_report.multi.copied`` is always present (0 when none), in both modes."""
    assert MultiReport(mode="sequential", order=[]).report()["copied"] == 0
    found = MultiReport(mode="simultaneous", order=[], copied=2).report()
    assert found["copied"] == 2
