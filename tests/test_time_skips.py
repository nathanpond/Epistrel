"""#199 (FR-CLOCK-7,8): the pure pieces of narrated skips. The ``time`` detector's ``elapsed``
hits, #190's diff reporting undeclared skips, skip authority (``core.world_clock.skips``), the
turn's decisions (``orchestrator.structured.time_estimation.decide``), the ``time_estimation``
purpose's registration and its prompt. The scenario is S-MOVIE (``tests/scenario/
test_s_movie.py``); the degradation is P-09's M6 part."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from epistrel.core.enums import StateChangeSource
from epistrel.core.resolution.entities import EntityIndex, ResolutionContext
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.states import completeness
from epistrel.core.states.effects import UNESTIMATED_SKIP, UNPROMPTED_SKIP
from epistrel.core.turns.beats import DeclaredAdvance, TurnBeat
from epistrel.core.verify.detect import DetectBeat, DetectionEnv, detect_render, elapsed_spans
from epistrel.core.world_clock import skips
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import registry
from epistrel.orchestrator.models.prompts import render
from epistrel.orchestrator.models.registry import ContentCategory as C
from epistrel.orchestrator.structured import time_estimation as te
from tests import verify_support as vs

ESTABLISHED_WATCHING = skips.established(["I put on a two-hour movie for us."])


def _said(text: str) -> list[str]:
    return [text[a:b] for a, b in elapsed_spans(text)]


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("They watch the rest of the movie in silence.", ["the rest of the movie"]),
        ("An hour later, the doorbell rings.", ["An hour later"]),
        ("The drive takes twenty minutes.", ["takes twenty minutes"]),
        ("We talk until the diner closes.", ["until the diner closes"]),
        ("We lose track of time and talk until 6.", ["until 6"]),
        ("Later that night, the rain stops.", ["Later that night"]),
        ("They fall asleep and wake at dawn.", ["wake at dawn"]),
        ("By the time the kettle boils, the street is dark.", ["By the time"]),
        ("Two hours pass.", ["Two hours pass"]),
        ("She waits for an hour.", ["for an hour"]),
        ("Hours later, she is back.", ["Hours later"]),
        ("The next morning, the car is gone.", ["The next morning"]),
        # Complements: no elapsed time narrated.
        ("You drift off on the couch.", []),
        ("She hands him the key.", []),
        ("It is a quarter past seven.", []),
    ],
)
def test_elapsed_spans(text: str, hits: list[str]) -> None:
    assert _said(text) == hits


def test_detection_counts_elapsed_hits_never_a_claim_nor_a_flashback() -> None:
    env = DetectionEnv(
        registry=vs.REGISTRY,
        basis=TimeBasis(),
        context_for=lambda _speaker: ResolutionContext(index=EntityIndex([])),
    )
    text = "An hour later, the doorbell rings."
    found = detect_render(
        [
            DetectBeat(1, "narration", text),
            DetectBeat(2, "narration", text, flashback=True),
            DetectBeat(3, "narration", "The doorbell rings."),
        ],
        (3, 0),
        env,
    )
    now, then, plain = found.beats
    assert now.elapsed == ((0, len("An hour later")),)
    assert now.hits.get("elapsed") == 1
    assert (then.elapsed, then.hits.get("elapsed")) == ((), None)
    assert plain.elapsed == ()
    assert "elapsed" not in {str(c.category) for c in found.claims}  # a hit, never a claim


def _beat(n: int, kind: str, text: str, **kw: object) -> completeness.Beat:
    return completeness.Beat(n, kind, text, **kw)  # type: ignore[arg-type]


def test_undeclared_skips() -> None:
    found = completeness.undeclared_skips(
        [
            _beat(2, "narration", "Jenni smiles. They watch the rest of the movie in silence."),
            _beat(3, "say", "We could talk until the diner closes."),  # talk, not the scene
            _beat(4, "do", "She waits for an hour.", advances=1),  # declared
            _beat(5, "narration", "An hour later, the bell rings.", flashback=True),
            _beat(6, "narration", "They don't wait an hour."),  # negated
            _beat(7, "do", "Do they wait for an hour?"),  # a question
            _beat(8, "do", "She waits for an hour.", attempt=True),
        ]
    )
    text = "Jenni smiles. They watch the rest of the movie in silence."
    assert found == (
        completeness.UndeclaredSkip(
            2, (14, len(text)), (14 + len("They watch "), len(text) - len(" in silence."))
        ),
    )
    diff = completeness.diff([], [], {}, beats=[_beat(2, "narration", text)])
    assert [s.beat for s in diff.skips] == [2]


def test_stated_minutes_and_activities() -> None:
    assert skips.stated_minutes("We wait two hours.") == 120
    assert skips.stated_minutes("half an hour, then") == 30
    assert skips.stated_minutes("for 20 minutes") == 20
    assert skips.stated_minutes("Tell me more.") is None
    assert skips.activities("They watch the rest of the movie.") == {"watch"}
    assert skips.activities("You drift off on the couch") == {"sleep"}
    assert skips.activities("Tell me more.") == frozenset()


def test_classify_skip() -> None:
    skip = skips.Skip("They watch the rest of the movie.", minutes=90)
    assert skips.classify_skip(skip, "We wait two hours.") == skips.PlayerInitiated(120)
    assert skips.classify_skip(skip, "I fall asleep on the couch") == skips.PlayerInitiated()
    assert skips.classify_skip(skip, "Skip to the next day.") == skips.PlayerInitiated()
    assert skips.classify_skip(skip, "Hm?", ["Skip ahead to the evening."]) == (
        skips.PlayerInitiated(None, skips.AUTHOR)
    )
    found = skips.classify_skip(skip, "I glance at Jenni.")
    assert found == skips.ModelInitiated(frozenset({"watch"}))
    assert found.initiated_by == skips.MODEL


def test_authorize() -> None:
    model = skips.ModelInitiated(frozenset({"watch"}))
    accept = skips.Accepted(skips.MODEL)
    assert skips.authorize(model, minutes=90, cap=1440, established=ESTABLISHED_WATCHING) == accept
    assert skips.authorize(model, minutes=90, cap=90, established=ESTABLISHED_WATCHING) == accept
    assert skips.authorize(
        model, minutes=91, cap=90, established=ESTABLISHED_WATCHING
    ) == skips.Refused(skips.OVER_CAP)
    assert skips.authorize(model, minutes=5, cap=1440, established=frozenset()) == skips.Refused(
        skips.NO_ACTIVITY
    )
    assert skips.authorize(
        model, minutes=5, cap=0, established=ESTABLISHED_WATCHING
    ) == skips.Refused(skips.DISABLED)
    # The player's skip is uncapped, even at 0 and with nothing established.
    player = skips.PlayerInitiated()
    assert skips.authorize(player, minutes=10_000, cap=0, established=frozenset()) == (
        skips.Accepted(skips.PLAYER)
    )


def _candidate(text: str, minutes: int | None = None) -> te.Candidate:
    return te.Candidate(2, 0 if minutes is not None else None, None, text, minutes=minutes)


def test_decide() -> None:
    movie = _candidate("They watch the rest of the movie in silence.")
    kw = {"directives": (), "cap": 1440, "established": ESTABLISHED_WATCHING}
    # An undeclared, established, model-initiated skip waits for its estimate ...
    waiting = te.decide(movie, player_input="I glance at Jenni.", **kw)  # type: ignore[arg-type]
    assert waiting.outcome == "estimate"
    made = te.Estimate(90, "about ninety minutes remain", 0.8, "m", "h" * 64)
    found = te.decide(movie, player_input="I glance at Jenni.", estimated=made, **kw)  # type: ignore[arg-type]
    assert found.outcome == "accept"
    assert found.advance == DeclaredAdvance(
        "about ninety minutes remain",
        minutes=90,
        source=StateChangeSource.ESTIMATE.value,
        meta={
            "reason": "about ninety minutes remain",
            "confidence": 0.8,
            "model": "m",
            "prompt_hash": "h" * 64,
            "initiated_by": "model",
        },
    )
    # ... and is refused over the cap.
    capped = te.decide(
        movie,
        player_input="I glance at Jenni.",
        directives=(),
        cap=60,
        established=ESTABLISHED_WATCHING,
        estimated=made,
    )
    assert (capped.outcome, capped.reason) == ("refuse", skips.OVER_CAP)
    # A skip no duration could make acceptable is refused before any estimate.
    doorbell = _candidate("An hour later, the doorbell rings.")
    assert te.decide(doorbell, player_input="Hm?", **kw).outcome == "refuse"  # type: ignore[arg-type]
    zero = te.decide(
        movie, player_input="Hm?", directives=(), cap=0, established=ESTABLISHED_WATCHING
    )
    assert (zero.outcome, zero.reason) == ("refuse", skips.DISABLED)
    # The player's stated amount needs no estimate (``source = player``).
    stated = te.decide(movie, player_input="We wait two hours.", **kw)  # type: ignore[arg-type]
    assert stated.outcome == "accept"
    assert stated.advance is not None
    assert (stated.advance.minutes, stated.advance.source) == (120, "player")
    # A declared skip is authorized with its own minutes.
    declared = _candidate("They fall asleep and wake at dawn.", 600)
    assert te.decide(declared, player_input="Hm?", **kw).outcome == "refuse"  # type: ignore[arg-type]
    asleep = te.decide(declared, player_input="I fall asleep on the couch", **kw)  # type: ignore[arg-type]
    assert asleep.outcome == "accept"


def _turn_beat(n: int, kind: str, text: str, *advances: DeclaredAdvance) -> TurnBeat:
    return TurnBeat(kind=kind, beat=n, text=text, character_id=None, advances=advances)  # type: ignore[arg-type]


def test_candidates_applied_and_stripped() -> None:
    from tests.test_u_23 import clock as clock_at  # U-23's clock: 15:05 at offset 0

    clock = clock_at(0)
    beats = [
        _turn_beat(2, "narration", "They watch the rest of the movie in silence."),
        _turn_beat(
            3, "narration", "They drive for an hour.", DeclaredAdvance("the drive", minutes=60)
        ),
        _turn_beat(4, "narration", "Time stops.", DeclaredAdvance("stop", to_offset=30)),
        _turn_beat(5, "narration", "Asleep.", DeclaredAdvance("until ten", to="22:00")),
    ]
    found = te.candidates(beats, clock)
    assert [(c.beat, c.declared, c.minutes) for c in found] == [
        (2, False, None),
        (3, True, 60),
        (5, True, 22 * 60 - (15 * 60 + 5)),
    ]
    decided = [
        te.Decision(found[1], "accept", skips.ModelInitiated(frozenset({"drive"}))),
        te.Decision(
            found[0],
            "accept",
            skips.PlayerInitiated(),
            DeclaredAdvance("est", minutes=90, source="estimate", meta={"initiated_by": "player"}),
        ),
    ]
    applied = {b.beat: te.applied(b, decided) for b in beats}
    assert applied[2].advances[-1].minutes == 90
    assert applied[3].advances[0].meta == {"initiated_by": "model"}
    assert te.stripped(beats[2]) == beats[2]  # an engine target is kept
    assert te.stripped(beats[1]).advances == ()
    refused = te.Decision(found[1], "refuse", skips.ModelInitiated(), reason=skips.NO_ACTIVITY)
    assert te.finding_of(refused).tag == UNPROMPTED_SKIP
    assert te.finding_of(te.unestimated(found[0], skips.ModelInitiated())).tag == (UNESTIMATED_SKIP)
    assert te.undeclared(beats) == 1


def test_time_estimation_purpose() -> None:
    purpose = registry.get_purpose("time_estimation")
    assert purpose.default_model_key == "extraction_backstop"
    assert (purpose.hot_path, purpose.story_scoped) == (True, True)
    assert "time_estimation" not in registry.RESERVED
    # #208: provider data never reaches it.
    assert C.EXTERNAL_DATA in purpose.never_receives
    assert registry.schema_categories(purpose.input_schema) == {C.RENDER_TEXT, C.SCENE}
    inputs = te.inputs_of(
        "They watch the rest of the movie in silence.",
        "I put on a two-hour movie for us at 19:10.",
        ["The clock reads 19:40."],
    )
    messages = render(purpose, inputs)
    text = "\n".join(m.content for m in messages)
    for wanted in ("rest of the movie", "two-hour movie", "The clock reads 19:40."):
        assert wanted in text
    assert registry.TimeEstimate().model_dump() == {"minutes": 0, "reason": "", "confidence": 0.0}


class _Answering:
    """A Model Access Layer stand-in answering ``time_estimation`` with ``answer`` (the
    structured layer's own ``uncertain_below`` check left out, so ``estimate``'s floor is the
    one exercised)."""

    def __init__(self, answer: registry.TimeEstimate) -> None:
        self.answer = answer
        self.adapter = SimpleNamespace(name="stub")

    def prepare(
        self, purpose: str, inputs: registry.TimeEstimationInputs
    ) -> tuple[None, None, list[Any]]:
        return None, None, render(registry.get_purpose(purpose), inputs)

    async def structured_call(self, *_: object, **__: object) -> registry.TimeEstimate:
        return self.answer

    async def endpoint(self, purpose: str) -> tuple[str, str, int]:
        return "stub", "", 0


@pytest.mark.parametrize(
    ("minutes", "confidence", "taken"), [(45, 0.7, True), (45, 0.29, False), (0, 0.9, False)]
)
async def test_estimate_floor(minutes: int, confidence: float, taken: bool) -> None:
    models = _Answering(
        registry.TimeEstimate(minutes=minutes, reason="a stew", confidence=confidence)
    )
    found = await te.estimate(
        models,  # type: ignore[arg-type]
        narrated_text="She cooks on until the stew is ready.",
        budget=TurnBudget(),
    )
    if not taken:
        assert found is None
        return
    assert found is not None
    assert (found.minutes, found.reason, found.confidence, found.model) == (
        45,
        "a stew",
        0.7,
        "stub",
    )
    assert found.meta("model")["initiated_by"] == "model"
