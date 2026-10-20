"""E-06: generation conformance, M4's part (docs/04 §8; FR-TURN-1, NFR-QUAL-7; #157).

Each case is one structured turn (``suites.structured_turns``: ``Engine.respond`` on
``Structured (M4)``) with the case's ``prompt_params``. A case **conforms** when, together:

- **structure** (FR-TURN-1): the released turn is a valid say/think/do/latent render
  (``structure_problems``);
- **length**: the composed message is within the requested length — at most 125 % of its token
  budget (``short`` 150, ``medium`` 400, ``long`` 800, or the integer cap), counting a token
  as four characters, as the stub adapter and the model matrix's estimate do;
- **repeatability** (``repeat`` cases, temperature 0): the same line on a second scratch story
  gives the same **render call output** (``structured_turns.render_output``: the rendered
  beats, a repaired beat by its original text). The owner's decision on #464 (2026-10-20):
  NFR-QUAL-7's temperature-0 repeatability covers the render only, not the composition, the
  regeneration or the repair steps after it.

- ``nfr_qual_7.conformance`` (rate, ≥ 98 %): one combined per-case rate (the planner's choice).
- ``e06.structure_valid``, ``e06.length_honoured``, ``e06.repeatable`` (report-only): the
  parts, so a miss names its cause.

Its stub answers are scripted per case (a valid render), so CI gates on its stub run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.settings_schema import LENGTH_TOKENS
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.structured_turns import play, render_output, structure_problems
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

CONFORMANCE = "nfr_qual_7.conformance"
STRUCTURE = "e06.structure_valid"
LENGTH = "e06.length_honoured"
REPEATABLE = "e06.repeatable"
LENGTH_TOLERANCE = 1.25
CHARS_PER_TOKEN = 4


class PromptParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    length: Literal["short", "medium", "long"] | int
    temperature: float = Field(ge=0.0, le=2.0)


class ConformanceInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    say: str = Field(min_length=1, max_length=500)
    prompt_params: PromptParams
    repeat: bool = False


class ConformanceCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: ConformanceInputs
    expected: Literal["valid"]
    stub_answer: str


def token_budget(length: str | int) -> int:
    return length if isinstance(length, int) else LENGTH_TOKENS[length]


def within_length(text: str, length: str | int) -> bool:
    """``text`` is at most ``LENGTH_TOLERANCE`` times the length's token budget."""
    return len(text) / CHARS_PER_TOKEN <= LENGTH_TOLERANCE * token_budget(length)


@register
class E06Suite(Suite):
    name = "e06"
    title = "E-06 (M4): structured generation conformance"
    set_path = "generation_conformance/cases.jsonl"
    min_cases = 200
    guarantees = ("generation",)
    ci_gate = True
    case_model = ConformanceCase
    metrics = (
        Metric(
            CONFORMANCE,
            Rule.rate,
            0.98,
            Direction.at_least,
            "valid say/think/do/latent structure, within the requested length, and the same "
            "render again at temperature 0",
        ),
        Metric(STRUCTURE, Rule.informational, description="a valid structure was released"),
        Metric(LENGTH, Rule.informational, description="the message kept the requested length"),
        Metric(
            REPEATABLE,
            Rule.informational,
            description="temperature 0 repeated the render call's output",
        ),
    )
    reading_notes = (
        "E-06 (M4) counts a case as conforming only when the structure is valid, the message is "
        "within 125 % of its length's token budget (four characters a token), and, for the "
        "temperature-0 `repeat` cases, a second story gives the same render call output (the "
        "rendered beats, a repaired beat by its original text; not the composed message: the "
        "owner's decision on #464).",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ConformanceCase)  # noqa: S101 - load() validated it
        params = case.inputs.prompt_params.model_dump()
        result = await play(ctx, case.inputs.say, params)
        problems = structure_problems(result)
        text = result.message.text
        length_ok = within_length(text, case.inputs.prompt_params.length)
        observations: dict[str, bool | None] = {
            STRUCTURE: not problems,
            LENGTH: length_ok,
        }
        repeated: bool | None = None
        if case.inputs.repeat:
            again = await play(ctx, case.inputs.say, params)
            repeated = await render_output(ctx, again) == await render_output(ctx, result)
            observations[REPEATABLE] = repeated
        observations[CONFORMANCE] = not problems and length_ok and repeated is not False
        return Outcome(
            observations,
            output=text,
            detail={"problems": problems, "chars": len(text), "degradation": result.degradation},
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, ConformanceCase)  # noqa: S101 - load() validated it
        return {GENERATION.name: case.stub_answer}


__all__ = ["CONFORMANCE", "ConformanceCase", "E06Suite", "within_length"]
