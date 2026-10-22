"""E-07: structured-output robustness (docs/04 §8; FR-MODEL-6,7, NFR-QUAL-5; #157).

Each case is one structured turn (``suites.structured_turns``). The character render is a
structured call (constrained decoding or parse-validate-retry, ``structured_call``): an
unusable answer is sent back with a correction and retried, up to the deployment's retry budget,
and a render that never validates fails the turn (503 ``model_unavailable``, reason
``invalid_structure``: nothing is committed) rather than releasing it.

**Injected garbage** (``inject``, 10 % of the set): the suite wraps the engine's model adapter
(``setup``) so that, inside an injected case only, the render's answer (the turn's first
``generation`` call) is replaced by unparseable text — on its first attempt (``once``: the
retry must recover) or on every attempt (``always``: the turn must take the degradation
path). Every other call, and every call of every other case, goes to the model untouched.
After an injected case the layer's memo of endpoints that ignore ``response_format`` is
cleared, so the injected parse error never changes how later cases call the model.

- ``nfr_qual_5.valid_after_retry`` (rate, ≥ 99 %): over the cases not injected ``always``, the
  render's structured call ended in a valid answer, on its first attempt or a retry
  (``render_valid``: its ``invalid_output`` attempts were retried to an ``ok`` one). Only the
  model's structured-call validity counts (the owner's decision on #465, as #437 decided for
  the smoke scorer): what verification, repair or the output gate did with a valid render
  afterwards (a released safe fallback, a deterministic repair) is not an invalid structure
  here; E-06 (``structure_problems``) still judges the released turn. A turn that fails
  ``model_unavailable`` is a miss: ``invalid_structure`` means the render never validated, and
  any other model error fails the case (#134).
- ``e07.first_attempt_valid`` (report-only): over the cases not injected, the render's first
  attempt was valid — the model's own rate of invalid structured output is one minus this.
- ``e07.no_silent_acceptance`` (report-only; #157's test asserts 100 %): an ``always`` case
  failed the turn with ``invalid_structure`` instead of releasing anything.

Its stub answers are scripted per case (a valid render), so CI gates on its stub run.
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.evals.suites.structured_turns import play, structure_problems
from epistrel.orchestrator.models.adapter import Message, ModelAdapter, ModelParams, ModelResult
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.metrics import ModelCall
from epistrel.orchestrator.models.registry import GENERATION

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext

VALID_AFTER_RETRY = "nfr_qual_5.valid_after_retry"
FIRST_ATTEMPT = "e07.first_attempt_valid"
NO_SILENT = "e07.no_silent_acceptance"
GARBAGE = '<<injected garbage>> {"beats": [{"kind": "say", "text": '
INVALID_STRUCTURE = "invalid_structure"

Injection = Literal["none", "once", "always"]


def render_calls(calls: Sequence[ModelCall]) -> list[ModelCall]:
    """The attempts of the turn's render, its first ``generation`` call: from its attempt 1 up
    to the next ``generation`` call's attempt 1 (module docstring)."""
    found: list[ModelCall] = []
    for call in calls:
        if call.purpose != GENERATION.name:
            continue
        if call.attempt == 1 and found:
            break
        found.append(call)
    return found


def render_valid(calls: Sequence[ModelCall]) -> bool:
    """The render's structured call ended in a valid answer (``nfr_qual_5.valid_after_retry``):
    its last attempt is ``ok``."""
    attempts = render_calls(calls)
    return bool(attempts) and attempts[-1].outcome == "ok"


class _Injecting:
    """What one case injects: the mode and how many renders it has replaced so far."""

    def __init__(self, mode: Injection) -> None:
        self.mode = mode
        self.done = 0


_CURRENT: contextvars.ContextVar[_Injecting | None] = contextvars.ContextVar(
    "e07_injecting", default=None
)


class GarbageInjector:
    """The engine's adapter, except a render answer inside an injected case (module docs)."""

    def __init__(self, inner: ModelAdapter) -> None:
        self.inner = inner
        self.name = inner.name

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self.inner, attribute)

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        injecting = _CURRENT.get()
        # A turn's first ``generation`` call is its character render (the player speaks, so no
        # narrator renders first); ``always`` replaces every one, so the render never validates.
        render = purpose == GENERATION.name
        if injecting is not None and render and (injecting.mode == "always" or not injecting.done):
            injecting.done += 1
            return ModelResult(
                text=GARBAGE,
                finish_reason="stop",
                model="injected",
                latency_ms=0,
                tokens_in=0,
                tokens_out=0,
                cost=0.0,
                request_id="eval-injected",
            )
        return await self.inner.complete(purpose, messages, params, response_schema)

    async def probe(self, purpose: str) -> Any:
        return await self.inner.probe(purpose)

    async def aclose(self) -> None:
        await self.inner.aclose()


class OutputInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    say: str = Field(min_length=1, max_length=500)
    inject: Injection = "none"


class OutputCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: OutputInputs
    expected: Literal["valid", "degraded"]
    stub_answer: str


@register
class E07Suite(Suite):
    name = "e07"
    title = "E-07: structured-output robustness (valid after retry, never silent)"
    set_path = "structured_output/cases.jsonl"
    min_cases = 200
    ci_gate = True
    case_model = OutputCase
    metrics = (
        Metric(
            VALID_AFTER_RETRY,
            Rule.rate,
            0.99,
            Direction.at_least,
            "the render's structured call ended in a valid answer, on its first attempt or a "
            "retry (what verification and repair did afterwards is not counted)",
        ),
        Metric(
            FIRST_ATTEMPT,
            Rule.informational,
            description="the render's first attempt was valid (no injection)",
        ),
        Metric(
            NO_SILENT,
            Rule.informational,
            description="a render that never validated failed the turn instead of releasing it",
        ),
    )
    reading_notes = (
        "E-07 injects unparseable render answers into 10 % of its cases (the first attempt, or "
        "every attempt) by wrapping the model adapter for those cases only; "
        "`e07.no_silent_acceptance` must be 100 %: such a turn fails with `invalid_structure` "
        "and commits nothing.",
        "`nfr_qual_5.valid_after_retry` counts only the render's structured-call validity "
        "(`invalid_output` attempts retried to a valid answer); a safe fallback or "
        "deterministic repair released after a valid render is not a miss (the owner's "
        "decision on #465).",
    )

    def __init__(self) -> None:
        self.wrapped: ModelAdapter | None = None

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        self.wrapped = engine.models.adapter
        engine.models.adapter = GarbageInjector(self.wrapped)

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        if self.wrapped is not None:
            engine.models.adapter = self.wrapped
        self.wrapped = None

    def injection(self, case: OutputCase) -> Injection:
        """What the case injects (its ``inject``; the scoring always reads the label)."""
        return case.inputs.inject

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, OutputCase)  # noqa: S101 - load() validated it
        mode = case.inputs.inject
        injected = self.injection(case)
        token = _CURRENT.set(_Injecting(injected) if injected != "none" else None)
        try:
            result = await play(ctx, case.inputs.say)
        except ModelUnavailable as exc:
            invalid = exc.details.get("reason") == INVALID_STRUCTURE or (
                INVALID_STRUCTURE in str(exc.details)
            )
            observations: dict[str, bool | None] = {}
            if mode == "always":
                observations[NO_SILENT] = invalid
            else:
                observations[VALID_AFTER_RETRY] = False
                if mode == "none":
                    observations[FIRST_ATTEMPT] = False
            return Outcome(observations, output=f"model_unavailable: {exc.details}")
        finally:
            _CURRENT.reset(token)
            if injected != "none":
                ctx.models.response_format.clear()
        problems = structure_problems(result)  # report-only: E-06's check (#465)
        first = next((c for c in result.model_calls if c.purpose == GENERATION.name), None)
        observations = {}
        if mode == "always":
            observations[NO_SILENT] = False
        else:
            observations[VALID_AFTER_RETRY] = render_valid(result.model_calls)
            if mode == "none":
                observations[FIRST_ATTEMPT] = first is not None and first.outcome == "ok"
        return Outcome(
            observations,
            output=result.message.text,
            detail={"problems": problems, "degradation": result.degradation},
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, OutputCase)  # noqa: S101 - load() validated it
        return {GENERATION.name: case.stub_answer}


__all__ = [
    "FIRST_ATTEMPT",
    "NO_SILENT",
    "VALID_AFTER_RETRY",
    "E07Suite",
    "GarbageInjector",
    "Injection",
    "OutputCase",
    "render_calls",
    "render_valid",
]
