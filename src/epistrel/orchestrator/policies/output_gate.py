"""The output gate (FR-GUARD-2,3,6,9; #68): every Lite reply is checked against the story's
output policies before it is released; a violation regenerates within a bounded budget, then
the stored in-character safe fallback is used.

**Which policies** (``output_policies``): the story's active effective policies (#55's age
condition decided, ``store.guard_context``) whose ``applies_to`` holds ``text_output``; a
character-scoped policy only when its character is one of the turn's render characters.
Policies are data the story's records cannot touch: nothing a prompt holds changes the set
(FR-GUARD-3).

**One check** (``check_output``): every deterministic policy (#49's ``evaluate``, no model
call), then one ``guardrail_check`` call per model-assisted policy through #55's
``check_text``, sequentially. Every policy is checked, so a regeneration is told about each
one the draft broke. A check the turn budget cannot pay for (deadline passed, or no call left)
is not made: it resolves by the policy's ``on_uncertain`` (the seeded minors policy:
violation). #64's configuration re-check (``policies.rebase``) runs this same check.

**The loop** (``gate_output``): a draft that breaks a policy is kept as a ``Rejected`` record
(the pipeline stages it as a ``guardrail_output_rejected`` audit event, written with the
turn). Each violated policy adds one constraint line to the next prompt, once, and the lines
accumulate over the turn: ``Your previous reply violated: <message>. Reply again without it.``
The policy's message is all the model is told, never a subject or an age. The turn generates
again while all of these hold:

- the story's ``guardrail_retry_budget`` (0 to 5, default 2) is not spent: it spans the turn;
- the turn budget can afford one generation plus one re-check per model-assisted output policy
  and its deadline has not passed.

Otherwise, or when a regeneration's call runs out of budget or deadline, the **safe fallback**
is released (a ``ModelUnavailable`` of any other kind is a 503, as on the first call). The
fallback is the first render character's ``safe_fallback_text``, else the deployment's
``default_safe_fallback`` (``"{name} changes the subject."``), else the hard-coded last resort
``"{name} says nothing."``; ``{name}`` is the first render character's name. Each line is
checked deterministically only (no model call); a line that breaks a policy is itself a
``Rejected`` record and the next line is tried. The last resort is released whatever it
breaks, and its violations are reported.

``Gated.violations`` are the violations of the released text only (empty unless the last
resort broke a policy); ``fallback_used`` says the reply is a fallback line.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Scope, Target, Violation
from epistrel.core.settings_schema import DEFAULT_SAFE_FALLBACK, NAME_PLACEHOLDER
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.errors import BudgetExhausted, ModelError, ModelUnavailable
from epistrel.orchestrator.models.structured import uncertain_verdict
from epistrel.orchestrator.policies.check import check_text
from epistrel.orchestrator.policies.metrics import GUARDRAIL_REJECTIONS, GUARDRAIL_VIOLATIONS
from epistrel.orchestrator.policies.store import GuardContext

STAGE = "output"
FALLBACK_STAGE = "fallback"
CONSTRAINT = "Your previous reply violated: {message}. Reply again without it."
LAST_RESORT = "{name} says nothing."
FALLBACK_FINISH_REASON = "fallback"
TEMPERATURE_STEP = 0.1
TEMPERATURE_CAP = 2.0
type RejectReason = Literal["output_policy", "fallback_policy"]
OUTPUT_POLICY: RejectReason = "output_policy"
FALLBACK_POLICY: RejectReason = "fallback_policy"

type FallbackSource = Literal["character", "deployment", "last_resort"]


def output_policies(
    context: GuardContext, render_ids: Sequence[str] | None = None
) -> tuple[Policy, ...]:
    """The active policies that gate output; a character policy only when its character is
    rendered (``render_ids`` ``None``: every character policy, as a re-check of stored replies
    whose render set is not at hand)."""
    return tuple(
        p
        for p in context.active
        if Target.TEXT_OUTPUT in p.applies_to
        and (p.scope is not Scope.CHARACTER or render_ids is None or p.scope_id in render_ids)
    )


def _assisted(policies: Sequence[Policy]) -> tuple[Policy, ...]:
    return tuple(p for p in policies if isinstance(p.rule, ModelAssistedRule))


def output_reservation(context: GuardContext, render_ids: Sequence[str]) -> int:
    """The calls a turn reserves for its first output check: one per model-assisted output
    policy (none when every output policy is deterministic, or there is none)."""
    return len(_assisted(output_policies(context, render_ids)))


def regeneration_cost(policies: Sequence[Policy]) -> int:
    """What one more attempt costs: the generation and its re-check by every model-assisted
    output policy."""
    return 1 + len(_assisted(policies))


@dataclass(frozen=True, slots=True)
class OutputCheck:
    """The violations a text has (empty: it passed), and the guardrail model's confidence
    when a model verdict found one."""

    violations: tuple[Violation, ...]
    confidence: float | None = None

    @property
    def passed(self) -> bool:
        return not self.violations


async def check_output(
    models: ModelAccessLayer,
    text: str,
    policies: Sequence[Policy],
    context: GuardContext,
    *,
    budget: TurnBudget,
) -> OutputCheck:
    """``text`` against ``policies`` (already the output set): see the module docstring."""
    found: list[Violation] = list(evaluate(text, policies, Target.TEXT_OUTPUT))
    confidence: float | None = None
    for policy in _assisted(policies):
        rule = policy.rule
        assert isinstance(rule, ModelAssistedRule)  # noqa: S101 - filtered by _assisted
        if budget.expired() or not budget.can_afford(1):
            if uncertain_verdict(rule.on_uncertain).violation:
                found.append(
                    Violation(
                        policy_id=policy.id,
                        name=policy.name,
                        message=policy.message,
                        kind=policy.kind,
                    )
                )
            continue
        outcome = await check_text(
            models,
            text,
            [policy],
            Target.TEXT_OUTPUT,
            budget=budget,
            cast=context.cast,
            threshold=context.threshold,
        )
        found.extend(outcome.violations)
        if outcome.violations and outcome.confidence is not None:
            confidence = max(confidence or 0.0, outcome.confidence)
    return OutputCheck(tuple(found), confidence)


def deterministic_check(text: str, policies: Sequence[Policy]) -> OutputCheck:
    """``text`` against the deterministic output policies only (a fallback line)."""
    return OutputCheck(tuple(evaluate(text, policies, Target.TEXT_OUTPUT)))


@dataclass(frozen=True, slots=True)
class Draft:
    """A candidate reply and where it came from."""

    text: str
    model: str | None = None
    prompt_hash: str | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Rejected:
    """A text an output policy rejected: a generated draft (``attempt`` 0, 1, …) or a fallback
    line (``attempt`` ``None``)."""

    text: str
    check: OutputCheck
    reason: RejectReason
    attempt: int | None


@dataclass(frozen=True, slots=True)
class FallbackLines:
    """The fallback candidates of a turn: the first render character's name and own line, and
    the deployment's line in force when the turn started."""

    name: str
    character_text: str | None = None
    deployment_text: str = DEFAULT_SAFE_FALLBACK

    def candidates(self) -> list[tuple[FallbackSource, str]]:
        found: list[tuple[FallbackSource, str]] = []
        if self.character_text and self.character_text.strip():
            found.append(("character", self.character_text))
        if self.deployment_text and self.deployment_text.strip():
            found.append(("deployment", self.deployment_text))
        found.append(("last_resort", LAST_RESORT))
        return [(source, text.replace(NAME_PLACEHOLDER, self.name)) for source, text in found]


@dataclass(frozen=True, slots=True)
class Gated:
    """What the gate released: the draft, the violations of the released text, whether it is
    a fallback line (and which), and every rejected text on the way, in order."""

    draft: Draft
    violations: tuple[Violation, ...] = ()
    fallback_used: bool = False
    fallback_source: FallbackSource | None = None
    rejected: tuple[Rejected, ...] = field(default_factory=tuple)
    attempts: int = 1


Produce = Callable[[tuple[str, ...], int], Awaitable[Draft]]
"""``produce(constraints, attempt)``: one generation with the constraint lines so far
(``attempt`` 0 is the first generation)."""


def _reject(rejected: list[Rejected], record: Rejected, stage: str) -> None:
    rejected.append(record)
    for violation in record.check.violations:
        GUARDRAIL_VIOLATIONS.labels(kind=violation.kind.value, stage=STAGE).inc()
        GUARDRAIL_REJECTIONS.labels(policy_id=violation.policy_id, stage=stage).inc()


def _out_of_time(exc: ModelError, budget: TurnBudget) -> bool:
    """A regeneration that ran out of budget or deadline (the fallback applies), as opposed to
    a model failure (a 503, #53)."""
    if isinstance(exc, BudgetExhausted) or budget.expired():
        return True
    return isinstance(exc, ModelUnavailable) and exc.reason == "deadline"


async def gate_output(
    models: ModelAccessLayer,
    context: GuardContext,
    policies: Sequence[Policy],
    produce: Produce,
    *,
    budget: TurnBudget,
    retry_budget: int,
    fallback: FallbackLines,
) -> Gated:
    """Generate, check, and regenerate or fall back; see the module docstring."""
    rejected: list[Rejected] = []
    constraints: list[str] = []
    constrained: set[str] = set()
    attempt = 0
    draft = await produce((), attempt)
    while True:
        check = await check_output(models, draft.text, policies, context, budget=budget)
        if check.passed:
            return Gated(draft=draft, rejected=tuple(rejected), attempts=attempt + 1)
        _reject(rejected, Rejected(draft.text, check, OUTPUT_POLICY, attempt), STAGE)
        for violation in check.violations:
            if violation.policy_id not in constrained:
                constrained.add(violation.policy_id)
                constraints.append(CONSTRAINT.format(message=violation.message.rstrip(". ")))
        affordable = not budget.expired() and budget.can_afford(regeneration_cost(policies))
        if attempt >= retry_budget or not affordable:
            break
        attempt += 1
        try:
            draft = await produce(tuple(constraints), attempt)
        except ModelError as exc:
            if not _out_of_time(exc, budget):
                raise
            break
    return _fall_back(policies, fallback, rejected, attempts=attempt + 1)


def _fall_back(
    policies: Sequence[Policy],
    fallback: FallbackLines,
    rejected: list[Rejected],
    *,
    attempts: int,
) -> Gated:
    """The first fallback line that passes the deterministic output policies; the last resort
    whatever it breaks."""
    candidates = fallback.candidates()
    for index, (source, line) in enumerate(candidates):
        check = deterministic_check(line, policies)
        if check.passed or index == len(candidates) - 1:
            return Gated(
                draft=Draft(text=line, finish_reason=FALLBACK_FINISH_REASON),
                violations=check.violations,
                fallback_used=True,
                fallback_source=source,
                rejected=tuple(rejected),
                attempts=attempts,
            )
        _reject(rejected, Rejected(line, check, FALLBACK_POLICY, None), FALLBACK_STAGE)
    raise AssertionError("unreachable: the last resort is always released")  # pragma: no cover


def raised_temperature(temperature: float | None, attempt: int) -> float | None:
    """A regeneration's temperature: ``TEMPERATURE_STEP`` higher per attempt, capped at
    ``TEMPERATURE_CAP`` (``None``, the provider's default, stays ``None``)."""
    if temperature is None or attempt == 0:
        return temperature
    return min(TEMPERATURE_CAP, round(temperature + TEMPERATURE_STEP * attempt, 6))


__all__ = [
    "CONSTRAINT",
    "FALLBACK_FINISH_REASON",
    "FALLBACK_POLICY",
    "LAST_RESORT",
    "OUTPUT_POLICY",
    "Draft",
    "FallbackLines",
    "Gated",
    "OutputCheck",
    "Produce",
    "Rejected",
    "check_output",
    "deterministic_check",
    "gate_output",
    "output_policies",
    "output_reservation",
    "raised_temperature",
    "regeneration_cost",
]
