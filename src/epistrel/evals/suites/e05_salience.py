"""E-05: salience detection (docs/04 §8; FR-COMMIT-3, NFR-QUAL-4; #157).

Each case is one render of one to three observable beats with its scene, read by #146's
``detect_salience`` at ``commitment.salience = rules_and_model`` — the ``salience_detection``
purpose through the Model Access Layer and the production span location (the model quotes each
span and the engine locates the quote in its beat, #461; a quote not found there is dropped and
counted as ``unlocated`` in the case's detail). A predicted salient span **hits** a labeled
one when both are on the same beat and share at least half of the shorter span
(``metrics.hits``).

- ``nfr_qual_4.recall`` (rate, ≥ 95 %): over the labeled salient spans (one per salient case),
  the share some predicted salient span hits.
- ``nfr_qual_4.precision`` (rate, ≥ 70 %): over the predicted salient spans, the share that hit
  a labeled one (micro-averaged per seed: ``Suite.aggregate``).
- ``e05.f_beta3`` (report-only): F-beta with beta 3, false negatives weighted x3 (they cause
  under-commitment: the story forgets what it owes).
- ``e05.texture_clean`` (report-only): a texture-only render got no salient span.
- ``e05.answered`` (report-only): the call returned a usable answer (no degradation).

Its stub answers are scripted per case (the labeled spans, quoted), so CI gates on its stub
run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.metrics import f_beta, hits
from epistrel.evals.suites import (
    Aggregate,
    CaseModel,
    Counts,
    Outcome,
    Suite,
    Value,
    register,
)
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.commit.salience import Beat, detect_salience
from epistrel.orchestrator.models.registry import SALIENCE_DETECTION, SceneContext

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

RECALL = "nfr_qual_4.recall"
PRECISION = "nfr_qual_4.precision"
F_BETA = "e05.f_beta3"
TEXTURE_CLEAN = "e05.texture_clean"
ANSWERED = "e05.answered"
BETA = 3.0
CALL_DEADLINE_S = 300.0


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SalienceBeatIn(_Model):
    kind: Literal["say", "do", "narration"]
    speaker: str | None = None
    text: str = Field(min_length=1, max_length=4000)


class SalienceInputsIn(_Model):
    beats: list[SalienceBeatIn] = Field(min_length=1, max_length=10)
    present: list[str] = Field(default_factory=list)
    last_beats: list[str] = Field(default_factory=list)


class LabeledSpan(_Model):
    beat: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    subject: str
    relation: str
    object: str | None = None


class SalienceCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: SalienceInputsIn
    expected: list[LabeledSpan]


def beats_of(case: SalienceCase) -> list[Beat]:
    return [
        Beat(index=i, kind=b.kind, text=b.text, speaker_name=b.speaker)
        for i, b in enumerate(case.inputs.beats)
    ]


def score(
    expected: Sequence[LabeledSpan], predicted: Sequence[tuple[int, int, int]]
) -> tuple[int, int, int, int]:
    """``(labeled hit, labeled missed, predicted hit, predicted false)``."""

    def hit(label: LabeledSpan, p: tuple[int, int, int]) -> bool:
        return p[0] == label.beat and hits((label.start, label.end), (p[1], p[2]))

    found = sum(1 for e in expected if any(hit(e, p) for p in predicted))
    right = sum(1 for p in predicted if any(hit(e, p) for e in expected))
    return found, len(expected) - found, right, len(predicted) - right


@register
class E05Suite(Suite):
    name = "e05"
    title = "E-05: salience detection (salient vs texture)"
    set_path = "salience/cases.jsonl"
    min_cases = 300
    guarantees = ("salience",)
    ci_gate = True
    case_model = SalienceCase
    metrics = (
        Metric(RECALL, Rule.rate, 0.95, Direction.at_least, "a labeled salient span was found"),
        Metric(
            PRECISION,
            Rule.rate,
            0.70,
            Direction.at_least,
            "a predicted salient span is a labeled one",
        ),
        Metric(F_BETA, Rule.informational, description="F-beta, false negatives weighted x3"),
        Metric(
            TEXTURE_CLEAN,
            Rule.informational,
            description="a texture-only render got no salient span",
        ),
        Metric(ANSWERED, Rule.informational, description="the call returned a usable answer"),
    )
    reading_notes = (
        "E-05 counts a predicted salient span as a hit when it is on the labeled span's beat "
        "and shares at least half of the shorter span; recall and precision are micro-averaged "
        "over spans per seed, and `e05.f_beta3` weights a missed salient span three times a "
        "false one.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, SalienceCase)  # noqa: S101 - load() validated it as case_model
        verdict = await detect_salience(
            beats_of(case),
            SceneContext(present_entities=case.inputs.present, last_beats=case.inputs.last_beats),
            mode="rules_and_model",
            models=ctx.models,
            budget=TurnBudget(deadline_s=CALL_DEADLINE_S),
        )
        predicted = [(s.span.beat, s.span.start, s.span.end) for s in verdict.salient]
        found, missed, right, wrong = score(case.expected, predicted)
        observations: dict[str, bool | None] = {ANSWERED: not verdict.degradation}
        if case.expected:
            observations[RECALL] = found == len(case.expected)
        else:
            observations[TEXTURE_CLEAN] = not predicted
        if predicted:
            observations[PRECISION] = wrong == 0
        return Outcome(
            observations,
            output=json.dumps(predicted),
            detail={
                "found": found,
                "missed": missed,
                "right": right,
                "wrong": wrong,
                "unlocated": len(verdict.unlocated),
                "degradation": list(verdict.degradation),
            },
        )

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        total = {k: sum(int(o.detail.get(k, 0)) for o in outcomes) for k in
                 ("found", "missed", "right", "wrong")}  # fmt: skip
        labeled = total["found"] + total["missed"]
        predicted = total["right"] + total["wrong"]
        recall = total["found"] / labeled if labeled else 0.0
        precision = total["right"] / predicted if predicted else 0.0
        return {
            RECALL: Counts(total["found"], total["missed"]),
            PRECISION: Counts(total["right"], total["wrong"]),
            F_BETA: Value(f_beta(precision, recall, BETA) if labeled else None, len(outcomes)),
        }

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, SalienceCase)  # noqa: S101 - load() validated it as case_model
        salient = [
            {
                "beat": e.beat,
                "span": case.inputs.beats[e.beat].text[e.start : e.end],
                "proposition_sketch": {
                    "subject": e.subject,
                    "relation": e.relation,
                    "object": e.object,
                    "time": None,
                },
            }
            for e in case.expected
        ]
        labeled = {e.beat for e in case.expected}
        texture = [
            {"beat": i, "span": b.text} for i, b in enumerate(case.inputs.beats) if i not in labeled
        ]
        return {SALIENCE_DETECTION.name: json.dumps({"salient": salient, "texture": texture})}


__all__ = ["PRECISION", "RECALL", "E05Suite", "SalienceCase", "score"]
