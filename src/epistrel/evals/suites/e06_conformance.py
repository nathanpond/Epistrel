"""E-06: generation conformance, M4's part (docs/04 §8; FR-TURN-1, NFR-QUAL-7; #157).

Each case is one structured turn (``suites.structured_turns``: ``Engine.respond`` on
``Structured``) with the case's ``prompt_params``. A case **conforms** when, together:

- **structure** (FR-TURN-1): the released turn is a valid say/think/do/latent render
  (``structure_problems``);
- **length**: the composed message is within the requested length — at most 125 % of its token
  budget (``short`` 150, ``medium`` 400, ``long`` 800, or the integer cap), counting a token
  as four characters, as the stub adapter and the model matrix's estimate do;
- **repeatability** (``repeat`` cases, temperature 0): the same line on a second scratch story
  gives the same **render call output** (``structured_turns.render_output``: the rendered
  beats, a repaired beat by its original text). The owner's decision on #464 (2026-10-20):
  NFR-QUAL-7's temperature-0 repeatability covers the render only, not the composition, the
  regeneration or the repair steps after it. **It counts only on a deterministic host**
  (#939, ADR-202, the owner's option (b)): ``CaseContext.deterministic``, a per-endpoint flag
  (``AdapterConfig.deterministic``; by default a hosted endpoint such as api.anthropic.com is
  not, LM Studio and the stub are). Elsewhere ``e06.repeatable`` is still measured and
  reported but leaves the verdict, and the report says so.

- ``nfr_qual_7.conformance`` (rate, ≥ 98 %): one combined per-case rate (the planner's choice).
- ``e06.structure_valid``, ``e06.length_honoured``, ``e06.repeatable`` (report-only): the
  parts, so a miss names its cause.

Its stub answers are scripted per case (a valid render), so CI gates on its stub run.

``detail.structure_code`` (#939) names a structure miss stably, for ``tools/e06_tally.py``:
``fallback:<cause>`` (with ``:<kinds>``, the sorted constraint kinds of the turn's
``render_rejected`` events, when there are any) for a render the safe fallback replaced, else
the first of ``structure_problems``; ``None`` when the structure is valid. A model error's
line has no ``structure_code``: its ``model_error`` and ``reason`` name it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from epistrel.core.settings_schema import LENGTH_TOKENS
from epistrel.core.tables import events
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, Summary, register
from epistrel.evals.suites.structured_turns import play, render_output, structure_problems
from epistrel.orchestrator.lite.schemas import SAFE_FALLBACK, RespondResult
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

CONFORMANCE = "nfr_qual_7.conformance"
STRUCTURE = "e06.structure_valid"
LENGTH = "e06.length_honoured"
REPEATABLE = "e06.repeatable"
LENGTH_TOLERANCE = 1.25
CHARS_PER_TOKEN = 4
NOT_DETERMINISTIC_NOTE = (
    "This host does not honour temperature 0 (its `deterministic` flag is off; #939, ADR-202): "
    "`e06.repeatable` is measured and reported, but `nfr_qual_7.conformance` leaves it out."
)


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


def conforms(*, structure: bool, length: bool, repeated: bool | None, deterministic: bool) -> bool:
    """NFR-QUAL-7's per-case rule: a valid structure within the length, and, on a
    deterministic host only (ADR-202), a repeat case that repeated."""
    return structure and length and (repeated is not False or not deterministic)


def structure_code(problems: list[str], fallback_causes: list[str], kinds: list[str]) -> str | None:
    """A structure miss's stable code (module docstring); ``None``: no miss."""
    if not problems:
        return None
    if fallback_causes:
        code = f"fallback:{fallback_causes[0]}"
        return f"{code}:{','.join(kinds)}" if kinds else code
    return problems[0]


_CONSTRAINT_CLASSES = (
    ("Do not assert what another character privately", "think_leak"),
    ("Do not claim to remember", "confabulation"),
    ("Do not assert what this beat could not support", "unsupported"),
    ("Do not settle that", "hidden"),
    ("Do not introduce facts about", "unpermitted"),
    ("Do not assert that", "contradicted_or_undecided"),
)
_REASON = re.compile(r"\(([a-z ]+)\)\.$")


def constraint_class(kind: str, text: str) -> str:
    """A rejected render's constraint as a stable code with no story content (#939): its kind,
    and for ``do_not_assert`` the repair ladder's wording class (``ladder._constraint``), an
    unpermitted claim with its denial reason (an enum value)."""
    if kind != "do_not_assert":
        return kind
    for prefix, name in _CONSTRAINT_CLASSES:
        if text.startswith(prefix):
            if name == "unpermitted" and (found := _REASON.search(text)):
                return f"{kind}:{name}({found.group(1).replace(' ', '_')})"
            return f"{kind}:{name}"
    return f"{kind}:state"


async def rejected_kinds(ctx: CaseContext, result: RespondResult) -> list[str]:
    """The sorted constraint classes of the turn's ``render_rejected`` events
    (``constraint_class``: no story text)."""
    operation = next((b.operation_id for b in result.beats), None)
    if operation is None:
        return []
    found: set[str] = set()
    async with ctx.engine.db.connect() as conn:
        rows = await conn.execute(
            select(events.c.payload).where(
                events.c.operation_id == operation, events.c.type == "render_rejected"
            )
        )
        for (payload,) in rows:
            for constraint in (payload or {}).get("constraints") or ():
                if isinstance(constraint, dict) and constraint.get("kind"):
                    found.add(
                        constraint_class(str(constraint["kind"]), str(constraint.get("text") or ""))
                    )
    return sorted(found)


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
            "valid say/think/do/latent structure, within the requested length, and, on a host "
            "that honours temperature 0, the same render again",
        ),
        Metric(STRUCTURE, Rule.informational, description="a valid structure was released"),
        Metric(LENGTH, Rule.informational, description="the message kept the requested length"),
        Metric(
            REPEATABLE,
            Rule.informational,
            description="temperature 0 repeated the render call's output (in the verdict only "
            "on a deterministic host)",
        ),
    )
    reading_notes = (
        "E-06 (M4) counts a case as conforming only when the structure is valid, the message is "
        "within 125 % of its length's token budget (four characters a token), and, for the "
        "temperature-0 `repeat` cases, a second story gives the same render call output (the "
        "rendered beats, a repaired beat by its original text; not the composed message: the "
        "owner's decision on #464). Repeatability counts only on a host that honours "
        "temperature 0 (its `deterministic` flag; #939, ADR-202): elsewhere `e06.repeatable` is "
        "measured and reported but leaves the verdict.",
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
        observations[CONFORMANCE] = conforms(
            structure=not problems,
            length=length_ok,
            repeated=repeated,
            deterministic=ctx.deterministic,
        )
        causes = [d.cause for d in result.degradation_details if d.step == SAFE_FALLBACK]
        kinds = await rejected_kinds(ctx, result) if causes else []
        return Outcome(
            observations,
            output=text,
            detail={
                "problems": problems,
                "chars": len(text),
                "degradation": result.degradation,
                "fallback_causes": causes,
                "rejected_kinds": kinds,
                "structure_code": structure_code(problems, causes, kinds),
                "deterministic": ctx.deterministic,
            },
        )

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        """#939: the report says when repeatability left the verdict."""
        if any(o.detail.get("deterministic") is False for o in outcomes):
            return Summary(notes=(NOT_DETERMINISTIC_NOTE,))
        return Summary()

    def error_metrics(self, case: CaseModel) -> frozenset[str]:
        """#530: structure, length and conformance; ``repeatable`` only on a repeat case."""
        assert isinstance(case, ConformanceCase)  # noqa: S101 - load() validated it
        rows = {STRUCTURE, LENGTH, CONFORMANCE}
        return frozenset(rows | {REPEATABLE} if case.inputs.repeat else rows)

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, ConformanceCase)  # noqa: S101 - load() validated it
        return {GENERATION.name: case.stub_answer}


__all__ = [
    "CONFORMANCE",
    "NOT_DETERMINISTIC_NOTE",
    "ConformanceCase",
    "E06Suite",
    "conforms",
    "constraint_class",
    "structure_code",
    "within_length",
]
