"""#1014 (FR-REPAIR-5): the repair ladder's per-render budget, the drop rule, the per-beat cause
and its viewpoint filter, as pure units. The end-to-end parts are I-12 M12
(``tests/integration/test_i_12.py``)."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

from epistrel.api.routes.history import DegradationBody, TurnSummaryModel
from epistrel.core.history import HistoryItem, TurnSummary, visible_details
from epistrel.core.repair.rungs import FALLBACK, REGENERATION, RepairClass
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.permission import Denied, Reason
from epistrel.core.verify.rules import VerdictKind
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.schemas import DegradationStep
from epistrel.orchestrator.models.registry import GenerationInputs
from epistrel.orchestrator.repair import ladder
from epistrel.orchestrator.repair.ladder import (
    DUPLICATE_FALLBACK,
    REPAIR_DETERMINISTIC,
    REPAIR_DROPPED,
    SAFE_FALLBACK,
    SPEAKER_HAS_LINE,
    Context,
    Recheck,
    RepairLadder,
    RepairResult,
    drop_plan,
    violation_code,
    violation_codes,
)
from epistrel.orchestrator.structured.pipeline import (
    CHARACTER_RENDER,
    FOLLOW_UP_RENDER,
    render_key,
)
from epistrel.orchestrator.verify.extraction import CheckedClaim

ADA, BO = "A" * 26, "B" * 26


def _beat(number: int, kind: str = "say", who: str | None = ADA, **fields: Any) -> TurnBeat:
    return TurnBeat(
        kind=cast(Any, kind), beat=number, text=f"beat {number}", character_id=who, **fields
    )


def _result(
    beat: TurnBeat,
    *,
    rung: int = FALLBACK,
    fallback: bool = True,
    kept: bool = True,
    degradation: Sequence[tuple[str, str]] = ((SAFE_FALLBACK, "repair"),),
) -> RepairResult:
    return RepairResult(
        beat=beat if kept else None,
        rung=rung,
        attempts=0,
        rejected=[],
        repair_class=RepairClass.CASCADE,
        degradation=list(degradation),
        remaining_violations=[],
        original=beat,
        violations=[],
        context=Context(),
        fallback=fallback,
    )


# -- the per-render budget -----------------------------------------------------------------------


def _ladder(max_attempts: int, render_of: Any) -> RepairLadder:
    """A ladder whose context, regeneration and re-check are fakes: every regeneration is
    accepted. ``think`` beats go straight to rung 3."""
    found = object.__new__(RepairLadder)
    found.max_attempts = max_attempts
    found.render_of = render_of
    found.used = {}
    found.renders = []
    found.extraction_calls = 0
    found.check_calls = 0
    found.cascade_threshold = 5
    found.sketches_of = {}
    found.inputs_of = lambda _: cast(GenerationInputs, SimpleNamespace())

    async def context(beat: TurnBeat, violations: Sequence[CheckedClaim]) -> Context:
        return Context()

    async def regenerate(
        beat: TurnBeat, *_: Any, **__: Any
    ) -> tuple[TurnBeat, list[Any], str, int]:
        return beat, [], "hash", 0  # #996: no world-clock change dropped

    async def ok(_: str) -> bool:
        return True

    async def recheck(candidate: TurnBeat, *_: Any) -> Recheck:
        return Recheck(beat=candidate, claims=[])

    found.context = context  # type: ignore[method-assign]
    found._regenerate = regenerate  # type: ignore[method-assign]
    found.output_ok = ok
    found.recheck = recheck
    return found


async def test_max_attempts_counts_per_render() -> None:
    """Two flagged beats of two renders each regenerate under ``max_attempts: 1``; a second
    flagged beat of a render that used its attempt is ``repair_deterministic``
    (``max_attempts``), as before #1014 (complement). The ladder reports what it ran."""
    keys = {2: "narrator", 3: "character", 4: "character"}
    repairs = _ladder(1, lambda b: keys[b.beat])
    budget = TurnBudget(max_calls=50)
    first = await repairs.run(_beat(2, "think", narrator=True, who=None), [], budget)
    second = await repairs.run(_beat(3, "think"), [], budget)
    third = await repairs.run(_beat(4, "think"), [], budget)
    assert (first.rung, second.rung) == (REGENERATION, REGENERATION)
    assert third.rung == FALLBACK
    assert (REPAIR_DETERMINISTIC, "max_attempts") in third.degradation
    assert repairs.renders == ["narrator", "character"]
    assert repairs.regenerated == ["narrator", "character"]
    assert repairs.attempts_used == 2


async def test_one_render_keeps_one_budget() -> None:
    """Complement: with every beat in one render (Lite has no ladder; a ``combined`` turn's
    character render), ``max_attempts: 1`` still means one regeneration."""
    repairs = _ladder(1, lambda _: "character")
    budget = TurnBudget(max_calls=50)
    found = [await repairs.run(_beat(n, "think"), [], budget) for n in (2, 3)]
    assert [r.rung for r in found] == [REGENERATION, FALLBACK]
    assert repairs.regenerated == ["character"]


def test_render_keys() -> None:
    """The narrator's beats are their own render; the follow-up's are only when the turn has
    several primaries (``follow_up`` given); the rest are the primary's (its reactions too)."""
    narration = _beat(2, "narration", who=None, narrator=True)
    assert render_key(narration) == ladder.NARRATOR_RENDER
    assert render_key(_beat(3)) == CHARACTER_RENDER
    assert render_key(_beat(4, "do", who=BO, narrated_by=ADA)) == CHARACTER_RENDER
    assert render_key(_beat(5), frozenset({5})) == FOLLOW_UP_RENDER
    assert render_key(_beat(5)) == CHARACTER_RENDER


# -- the drop rule -------------------------------------------------------------------------------


def test_a_fallback_next_to_a_real_line_is_dropped() -> None:
    """#1014's shape: a failing ``do`` and ``say`` next to a passing ``say`` of the same
    speaker: both fallback lines go (``speaker_has_line``)."""
    do, say, line = _beat(2, "do"), _beat(3), _beat(4)
    results = [_result(do), _result(say)]
    plan = drop_plan([do, say, line], results)
    assert [(r.original.beat, cause) for r, cause in plan] == [
        (2, SPEAKER_HAS_LINE),
        (3, SPEAKER_HAS_LINE),
    ]


def test_one_fallback_line_per_speaker() -> None:
    """Every beat of a speaker failed: its first fallback line stays, the rest go
    (``duplicate_fallback``); another speaker's fallback line is its own."""
    first, second, other = _beat(2, "do"), _beat(3), _beat(4, who=BO)
    plan = drop_plan([first, second, other], [_result(first), _result(second), _result(other)])
    assert [(r.original.beat, cause) for r, cause in plan] == [(3, DUPLICATE_FALLBACK)]
    alone = _beat(2)
    assert drop_plan([alone], [_result(alone)]) == []  # the fallback line is not switched off


def test_what_counts_as_a_line() -> None:
    """A ``think``, the player's input, a State finding's deterministic beat and another
    fallback line are not a real line; a narration is the narrator's, never a character's."""
    fallback = _beat(3)
    think = _beat(4, "think")
    player = _beat(1)
    settled = _beat(5, "do")
    narration = _beat(6, "narration", who=None, narrator=True)
    results = [_result(fallback), _result(settled, fallback=False)]
    assert drop_plan([player, fallback, think, settled, narration], results) == []
    narrated = _beat(7, "narration", who=None, narrator=True)
    plan = drop_plan([narration, narrated], [_result(narrated)])
    assert [(r.original.beat, cause) for r, cause in plan] == [(7, SPEAKER_HAS_LINE)]


def test_an_earlier_stage_counts_but_is_never_dropped() -> None:
    """A staged turn's outcome step: the intent's narration counts as the narrator's line,
    and the intent's kept fallback line makes the outcome step's a duplicate."""
    intent = _beat(2, "narration", who=None, narrator=True)
    outcome = _beat(5, "narration", who=None, narrator=True)
    plan = drop_plan([outcome], [_result(outcome)], earlier=[intent])
    assert [cause for _, cause in plan] == [SPEAKER_HAS_LINE]
    kept = _result(intent)
    plan = drop_plan([outcome], [_result(outcome)], earlier=[intent], earlier_results=[kept])
    assert [cause for _, cause in plan] == [DUPLICATE_FALLBACK]
    assert kept.drop_cause is None


def test_a_dropped_beat_reports_repair_dropped() -> None:
    """``drop`` replaces the beat's ``safe_fallback`` with ``repair_dropped``; a
    ``repair_deterministic`` that led to it stays; the rung reads ``dropped``."""
    result = _result(
        _beat(3), degradation=[(REPAIR_DETERMINISTIC, "max_attempts"), (SAFE_FALLBACK, "repair")]
    )
    assert result.rung_name == "safe_fallback"
    result.drop(SPEAKER_HAS_LINE)
    assert result.beat is None
    assert result.dropped
    assert result.degradation == [
        (REPAIR_DETERMINISTIC, "max_attempts"),
        (REPAIR_DROPPED, SPEAKER_HAS_LINE),
    ]
    assert [(d["step"], d["rung"]) for d in result.detail()] == [
        (REPAIR_DETERMINISTIC, "dropped"),
        (REPAIR_DROPPED, "dropped"),
    ]
    settled = _result(_beat(4, "do"), fallback=False)
    assert settled.rung_name == "state_settled"
    think = _result(_beat(5, "think"), fallback=False, kept=False)
    assert think.rung_name == "dropped"


# -- the per-beat cause --------------------------------------------------------------------------


def _claim(**result: Any) -> CheckedClaim:
    fields: dict[str, Any] = {
        "repair_tag": None,
        "denied": None,
        "hidden_conflict": False,
        "kind": VerdictKind.CONTRADICTED,
    }
    fields.update(result)
    return CheckedClaim(
        claim=cast(Any, SimpleNamespace()), result=cast(Any, SimpleNamespace(**fields))
    )


def test_violation_codes() -> None:
    """The codes: a denial's reason (bare ``unpermitted`` with none), the repair tag, a hidden
    conflict, the verdict; ``unchecked`` with no result. Distinct and sorted."""
    secret = Denied(Reason.OTHER_CHARACTER_SECRET)
    assert violation_code(_claim(repair_tag="unpermitted", denied=secret)) == (
        "unpermitted:other_character_secret"
    )
    assert violation_code(_claim(repair_tag="unpermitted")) == "unpermitted"
    assert violation_code(_claim(repair_tag="think_leak")) == "think_leak"
    assert violation_code(_claim(repair_tag="hallucination")) == "hallucinated"
    assert violation_code(_claim(repair_tag="state_constraint")) == "state_constraint"
    assert violation_code(_claim(hidden_conflict=True)) == "hidden_conflict"
    assert violation_code(_claim(kind=VerdictKind.UNCERTAIN)) == "uncertain"
    assert violation_code(_claim()) == "contradicted"
    assert violation_code(CheckedClaim(claim=cast(Any, SimpleNamespace()), result=None)) == (
        "unchecked"
    )
    assert violation_codes([_claim(), _claim(kind=VerdictKind.UNCERTAIN), _claim()]) == [
        "contradicted",
        "uncertain",
    ]


def _item(beat: int, kind: str = "beat_say", who: str | None = ADA) -> HistoryItem:
    return HistoryItem(
        turn=1,
        variant=0,
        beat=beat,
        kind=kind,
        actor_character_id=who,
        render_character_ids=None,
        channel="say",
        text="",
        operation_id="op",
        message_id="m",
    )


def test_visible_details() -> None:
    """Per-beat entries follow the beats the viewpoint sees (``omniscient``: every one); a
    dropped line shows when a beat of its speaker does; entries with no beat always show; the
    internal ``kind`` never leaves."""
    plain = {"step": "salience_skipped", "cause": "budget"}
    seen = {"step": SAFE_FALLBACK, "cause": "repair", "beat": 2, "character_id": ADA, "kind": "say"}
    unseen = {**seen, "beat": 3, "character_id": BO}
    dropped = {
        "step": REPAIR_DROPPED,
        "cause": SPEAKER_HAS_LINE,
        "beat": 4,
        "character_id": ADA,
        "rung": "dropped",
        "kind": "do",
    }
    thought = {**dropped, "beat": 5, "kind": "think", "step": SAFE_FALLBACK}
    details: list[dict[str, Any]] = [plain, seen, unseen, dropped, thought]
    shown = visible_details(details, [_item(2)], omniscient=False)
    assert [d.get("beat") for d in shown] == [None, 2, 4]
    assert all("kind" not in d for d in shown)
    assert [d.get("beat") for d in visible_details(details, [], omniscient=True)] == [
        None,
        2,
        3,
        4,
        5,
    ]


def test_the_api_body() -> None:
    """A repair's entry carries its beat, speaker (``null`` for the narrator), rung and codes;
    any other entry only ``step`` and ``cause``; a stored entry from before #1014 reads."""
    narrator = DegradationStep(
        step=SAFE_FALLBACK,
        cause="repair",
        beat=2,
        character_id=None,
        rung="safe_fallback",
        violations=("contradicted",),
        kind="narration",
    )
    assert DegradationBody.of(narrator).model_dump(mode="json") == {
        "step": SAFE_FALLBACK,
        "cause": "repair",
        "beat": 2,
        "character_id": None,
        "rung": "safe_fallback",
        "violations": ["contradicted"],
    }
    plain = DegradationStep(step="salience_skipped", cause="budget")
    assert DegradationBody.of(plain).model_dump(mode="json") == {
        "step": "salience_skipped",
        "cause": "budget",
    }
    assert plain.payload() == {"step": "salience_skipped", "cause": "budget"}
    summary = TurnSummary(1, 0, None, degradation_details=[{"step": "x", "cause": "y"}])
    assert TurnSummaryModel.of(summary).model_dump(mode="json")["degradation_details"] == [
        {"step": "x", "cause": "y"}
    ]
    assert "degradation_details" not in TurnSummaryModel.of(TurnSummary(1, 0, None)).model_dump(
        mode="json"
    )
