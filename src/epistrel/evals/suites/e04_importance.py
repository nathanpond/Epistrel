"""E-04: importance calibration (docs/04 §8; FR-MEM-7; #157).

Each case is one memory of the labeled set (``evals/sets/importance/cases.jsonl``: 100
``pivotal``, 100 ``trivial``), scored by the ``importance_scoring`` purpose exactly as #135's
job scores it (``jobs.handlers.importance.score``: its inputs, params and deterministic step).
The guarantee table lists importance as ``unmeasured`` (#132): the eval is a calibration with no
floor, so every metric is report-only.

- ``e04.spearman``: Spearman's rank correlation, per seed, between the scores and the binary
  label (pivotal 1, trivial 0; ties take their average rank). docs/04: pivotal out-ranks trivia;
  #157's test asserts ≥ 0.6 on the stub run.
- ``e04.pivotal_above_median``: a pivotal memory scored above 0.5, a trivial one at or below
  (a per-case reading of the same calibration).
- ``e04.scored``: the call gave a valid score (an ``Uncertain`` answer is a miss).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals.gate import Metric, Rule
from epistrel.evals.metrics import spearman
from epistrel.evals.suites import Aggregate, CaseModel, Outcome, Suite, Value, register
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs.handlers import importance
from epistrel.orchestrator.models.registry import IMPORTANCE_SCORING
from epistrel.orchestrator.models.structured import Uncertain

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

SPEARMAN = "e04.spearman"
ABOVE_MEDIAN = "e04.pivotal_above_median"
SCORED = "e04.scored"
MIDPOINT = 0.5


class ImportanceInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["episodic", "semantic", "reflection"] = "episodic"
    age_turns: int = Field(ge=0, le=10_000)
    """How many turns ago the memory was written (U-10's decay check reads it)."""


class ImportanceCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: ImportanceInputs
    expected: Literal["pivotal", "trivial"]
    stub_answer: str


@register
class E04Suite(Suite):
    name = "e04"
    title = "E-04: importance calibration (pivotal vs trivial)"
    set_path = "importance/cases.jsonl"
    min_cases = 200
    ci_gate = False
    case_model = ImportanceCase
    metrics = (
        Metric(
            SPEARMAN,
            Rule.informational,
            description="rank correlation of the score with the pivotal/trivial label",
        ),
        Metric(
            ABOVE_MEDIAN,
            Rule.informational,
            description="a pivotal memory scored above 0.5, a trivial one at or below",
        ),
        Metric(SCORED, Rule.informational, description="the call returned a valid score"),
    )
    reading_notes = (
        "E-04 has no floor (#132 lists importance as `unmeasured`): `e04.spearman` is the "
        "per-seed rank correlation between the score and the pivotal (1) / trivial (0) label, "
        "with tied ranks averaged; its n is the cases scored.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ImportanceCase)  # noqa: S101 - load() validated it as case_model
        answer = await importance.score(
            ctx.models,
            case.inputs.text,
            case.inputs.kind,
            budget=TurnBudget(max_calls=importance.MAX_CALLS_PER_RUN),
        )
        if isinstance(answer, Uncertain):
            return Outcome(
                {SCORED: False, ABOVE_MEDIAN: False},
                output=f"uncertain: {answer.reason}",
                detail={"label": case.expected},
            )
        pivotal = case.expected == "pivotal"
        above = answer.importance > MIDPOINT
        return Outcome(
            {SCORED: True, ABOVE_MEDIAN: above == pivotal},
            output=str(answer.importance),
            detail={"label": case.expected, "score": answer.importance},
        )

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        scored = [o for o in outcomes if "score" in o.detail]
        labels = [1.0 if o.detail["label"] == "pivotal" else 0.0 for o in scored]
        scores = [float(o.detail["score"]) for o in scored]
        return {SPEARMAN: Value(spearman(scores, labels), len(scored))}

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, ImportanceCase)  # noqa: S101 - load() validated it as case_model
        return {IMPORTANCE_SCORING.name: case.stub_answer}


__all__ = ["ABOVE_MEDIAN", "SCORED", "SPEARMAN", "E04Suite", "ImportanceCase"]
