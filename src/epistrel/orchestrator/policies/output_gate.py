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

- the story's polish budget (its configuration's ``repair.polish.max_regenerations``, 0 to 5,
  default 2; none with the polish pass off; #113) is not spent: it spans the turn;
- the turn budget can afford one generation plus one re-check per model-assisted output policy
  and its deadline has not passed.

Otherwise, or when a regeneration's call or a check runs out of budget or deadline, the **safe
fallback** is released (a ``ModelUnavailable`` of any other kind is a 503, as on the first
generation call). A regeneration is affordable only from the budget's unreserved remainder and
holds its calls before it starts (#69); ``Gated.fallback_cause`` says which bound ended the loop
(``retry_budget``, ``call_budget`` or ``deadline``). The
fallback is the first render character's ``safe_fallback_text``, else the deployment's
``default_safe_fallback`` (``"{name} changes the subject."``), else the hard-coded last resort
``"{name} says nothing."``; ``{name}`` is the first render character's name. Each line is
checked deterministically only (no model call); a line that breaks a policy is itself a
``Rejected`` record and the next line is tried. The last resort is released whatever it
breaks, and its violations are reported.

``Gated.violations`` are the violations of the released text only (empty unless the last
resort broke a policy); ``fallback_used`` says the reply is a fallback line.

**Implication** (FR-GUARD-5; #145). A draft may carry the records its context held
(``Draft.context``: the memories and facts retrieval ranked into it; ``Draft.pins``: the pins it
showed). With an ``implicate`` hook, every rejected draft's records are handed to it before the
next attempt: the hook (``orchestrator.policies.implication``) records them as the draft's
``implicated`` set (stored on its ``guardrail_output_rejected`` event as
``implicated_record_ids``), counts them, and quarantines a memory implicated in
``repair.implication_threshold`` rejected renders of the story, so the next ``produce`` (which
retrieves again) no longer sees it and the loop converges. ``Gated.quarantined`` lists what was
quarantined during the turn (the response's ``degradation`` then names
``quarantined_context``) and ``Gated.pins_quarantined`` the author pins among them. A Lite
draft carries no records and no hook is passed: #68's behaviour is unchanged.

**Withheld policies** (#447, ADR-161). With a ``withheld`` hook (both pipelines pass
``check.refuse_withheld``), every violation is put to it before the draft is rejected or a
fallback released: while a pending catalog-scope erasure reaches a violated policy's text, the
hook raises 503 ``erasure_pending`` and the turn rolls back, so the policy's message is never
sent as a constraint, stored on a ``guardrail_output_rejected`` event or reported.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from epistrel.core.events.types import GuardrailOutputRejected, RejectedViolation
from epistrel.core.events.types import RecordRef as EventRecordRef
from epistrel.core.mutation import RecordRef
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
type FallbackCause = Literal["retry_budget", "call_budget", "deadline"]
RETRY_BUDGET: FallbackCause = "retry_budget"
CALL_BUDGET: FallbackCause = "call_budget"
DEADLINE: FallbackCause = "deadline"


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
class PinText:
    """A pin a draft's context showed: its memory id and text (#145)."""

    memory_id: str
    text: str


@dataclass(frozen=True, slots=True)
class Draft:
    """A candidate reply and where it came from: ``context``, the records retrieval ranked into
    its prompt, and ``pins``, the pins it showed (#145; both empty for a Lite reply)."""

    text: str
    model: str | None = None
    prompt_hash: str | None = None
    finish_reason: str | None = None
    context: tuple[RecordRef, ...] = ()
    pins: tuple[PinText, ...] = ()


@dataclass(frozen=True, slots=True)
class Rejected:
    """A text an output policy rejected: a generated draft (``attempt`` 0, 1, …) or a fallback
    line (``attempt`` ``None``)."""

    text: str
    check: OutputCheck
    reason: RejectReason
    attempt: int | None
    implicated: tuple[RecordRef, ...] = ()
    """The records the rejected draft's context held that it implicated (FR-GUARD-5)."""


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
    fallback_cause: FallbackCause | None = None
    """Why the fallback was released (#69): the polish budget was spent, the call
    budget could not afford another attempt, or the deadline passed."""
    quarantined: tuple[str, ...] = ()
    """The memories the turn's rejections quarantined (#145)."""
    pins_quarantined: tuple[str, ...] = ()
    """The author pins among them (each a ``pin_quarantined`` warning)."""


Produce = Callable[[tuple[str, ...], int], Awaitable[Draft]]
"""``produce(constraints, attempt)``: one generation with the constraint lines so far
(``attempt`` 0 is the first generation)."""


@dataclass(frozen=True, slots=True)
class Implicated:
    """What an ``implicate`` hook did with one rejected draft: the records it implicated, the
    memories it quarantined, and the author pins among them."""

    refs: tuple[RecordRef, ...] = ()
    quarantined: tuple[str, ...] = ()
    pins: tuple[str, ...] = ()


Implicate = Callable[[Draft, OutputCheck, int], Awaitable[Implicated]]
"""``implicate(draft, check, attempt)``: FR-GUARD-5's feedback for a rejected draft."""

ContextOf = Callable[[Draft], Awaitable[GuardContext]]
"""``context_of(draft)``: the guard context ``draft`` is checked under (#571)."""

Withheld = Callable[[Sequence[str]], Awaitable[None]]
"""``withheld(policy_ids)``: raises (503 ``erasure_pending``) when a pending catalog-scope
erasure reaches any of the violated policies (#447; ``check.refuse_withheld``)."""


def _reject(rejected: list[Rejected], record: Rejected, stage: str) -> None:
    rejected.append(record)
    for violation in record.check.violations:
        GUARDRAIL_VIOLATIONS.labels(kind=violation.kind.value, stage=STAGE).inc()
        GUARDRAIL_REJECTIONS.labels(policy_id=violation.policy_id, stage=stage).inc()


def _out_of_time(exc: ModelError, budget: TurnBudget) -> FallbackCause | None:
    """Why a regeneration or re-check ran out (the fallback applies): its budget or its
    deadline; ``None`` for a model failure (a 503, #53)."""
    if isinstance(exc, BudgetExhausted):
        return CALL_BUDGET
    if budget.expired() or (isinstance(exc, ModelUnavailable) and exc.reason == "deadline"):
        return DEADLINE
    return None


async def gate_output(
    models: ModelAccessLayer,
    context: GuardContext,
    policies: Sequence[Policy],
    produce: Produce,
    *,
    budget: TurnBudget,
    retry_budget: int,
    fallback: FallbackLines,
    implicate: Implicate | None = None,
    withheld: Withheld | None = None,
    context_of: ContextOf | None = None,
) -> Gated:
    """Generate, check, and regenerate or fall back; see the module docstring. ``withheld`` is
    asked about every violation before anything is done with it (#447). ``context_of`` (#571),
    when given, is each draft's own guard context (in place of ``context``): who is present
    for it can depend on what it says (a narration located in another room)."""
    rejected: list[Rejected] = []
    quarantined: list[str] = []
    pins: list[str] = []
    constraints: list[str] = []
    constrained: set[str] = set()
    attempt = 0
    draft = await produce((), attempt)
    cause: FallbackCause
    while True:
        # The first check spends the slots reserved for it; a regeneration's, those it held.
        budget.release_to(len(_assisted(policies)))
        try:
            checking = context if context_of is None else await context_of(draft)
            check = await check_output(models, draft.text, policies, checking, budget=budget)
        except ModelError as exc:
            # The deadline cut the check short: the draft is unchecked, so it is never released.
            out = _out_of_time(exc, budget)
            if out is None:
                raise
            cause = out
            break
        finally:
            budget.release_to(0)
        if check.passed:
            return Gated(
                draft=draft,
                rejected=tuple(rejected),
                attempts=attempt + 1,
                quarantined=tuple(quarantined),
                pins_quarantined=tuple(pins),
            )
        if withheld is not None:
            await withheld([v.policy_id for v in check.violations])
        implicated = Implicated()
        if implicate is not None and (draft.context or draft.pins):
            implicated = await implicate(draft, check, attempt)
            quarantined.extend(i for i in implicated.quarantined if i not in quarantined)
            pins.extend(i for i in implicated.pins if i not in pins)
        _reject(
            rejected,
            Rejected(draft.text, check, OUTPUT_POLICY, attempt, implicated=implicated.refs),
            STAGE,
        )
        for violation in check.violations:
            if violation.policy_id not in constrained:
                constrained.add(violation.policy_id)
                constraints.append(CONSTRAINT.format(message=violation.message.rstrip(". ")))
        cost = regeneration_cost(policies)
        if attempt >= retry_budget:
            cause = RETRY_BUDGET
            break
        if budget.expired():
            cause = DEADLINE
            break
        if not budget.can_afford_unreserved(cost):
            cause = CALL_BUDGET
            break
        budget.hold(cost)
        attempt += 1
        try:
            draft = await produce(tuple(constraints), attempt)
        except ModelError as exc:
            out = _out_of_time(exc, budget)
            if out is None:
                raise
            cause = out
            break
    fell = _fall_back(policies, fallback, rejected, attempts=attempt + 1, cause=cause)
    if withheld is not None:
        await withheld(
            [
                v.policy_id
                for r in fell.rejected
                if r.reason == FALLBACK_POLICY
                for v in r.check.violations
            ]
            + [v.policy_id for v in fell.violations]
        )
    return replace(fell, quarantined=tuple(quarantined), pins_quarantined=tuple(pins))


def _fall_back(
    policies: Sequence[Policy],
    fallback: FallbackLines,
    rejected: list[Rejected],
    *,
    attempts: int,
    cause: FallbackCause,
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
                fallback_cause=cause,
            )
        _reject(rejected, Rejected(line, check, FALLBACK_POLICY, None), FALLBACK_STAGE)
    raise AssertionError("unreachable: the last resort is always released")  # pragma: no cover


def rejected_event(rejected: Rejected) -> GuardrailOutputRejected:
    """A rejected text as its ``guardrail_output_rejected`` audit event (payload version 2: the
    records the draft's context held that it implicated, #145)."""
    violations = rejected.check.violations
    return GuardrailOutputRejected(
        text=rejected.text,
        reason=rejected.reason,
        attempt=rejected.attempt,
        policy_ids=tuple(v.policy_id for v in violations),
        violations=tuple(
            RejectedViolation(
                policy_id=v.policy_id, name=v.name, message=v.message, kind=v.kind.value
            )
            for v in violations
        ),
        confidence=rejected.check.confidence,
        implicated_record_ids=tuple(
            EventRecordRef(table=r.table, id=r.id) for r in rejected.implicated
        ),
    )


def raised_temperature(temperature: float | None, attempt: int) -> float | None:
    """A regeneration's temperature: ``TEMPERATURE_STEP`` higher per attempt, capped at
    ``TEMPERATURE_CAP`` (``None``, the provider's default, stays ``None``)."""
    if temperature is None or attempt == 0:
        return temperature
    return min(TEMPERATURE_CAP, round(temperature + TEMPERATURE_STEP * attempt, 6))


__all__ = [
    "CALL_BUDGET",
    "CONSTRAINT",
    "DEADLINE",
    "FALLBACK_FINISH_REASON",
    "FALLBACK_POLICY",
    "LAST_RESORT",
    "OUTPUT_POLICY",
    "RETRY_BUDGET",
    "Draft",
    "FallbackCause",
    "FallbackLines",
    "Gated",
    "Implicate",
    "Implicated",
    "OutputCheck",
    "PinText",
    "Produce",
    "Rejected",
    "Withheld",
    "check_output",
    "deterministic_check",
    "gate_output",
    "output_policies",
    "output_reservation",
    "raised_temperature",
    "regeneration_cost",
    "rejected_event",
]
