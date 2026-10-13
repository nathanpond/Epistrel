"""The input gate (FR-GUARD-9,10): text a consumer sends into a story is checked against every
policy that applies to input before anything is recorded.

``input_gate(models, context, texts, budget=…)`` returns ``None`` when every text passes, or
the ``InputViolations`` found. ``enforce_input`` is what the Engine's truth-changing paths call:
it runs the gate, and on a violation writes the ``audit_log`` row and raises 422
``guardrail_violation``. Both run **before** an ``Operation`` opens, so a rejection has no
operation, event or revision change to roll back.

- **Which policies:** the story's active effective policies (#55's age condition decided)
  whose ``applies_to`` holds ``text_input`` — every global policy by default; rating, story and
  character policies only when set to input (FR-GUARD-10). A character-scoped policy applies
  only when that character is the ``actor``.
- **Each text on its own:** ``say`` + ``do`` are one text (#57); each ingested event, each
  pinned item and each character definition is its own. ``locator`` is a dotted path into the
  request (``player_input``, ``events[3]``, ``characters[0].definition``).
- **Order:** every deterministic policy (#49's ``evaluate``, no model call) over every text
  first, then one ``guardrail_check`` call per model-assisted policy per text, sequentially,
  through #55's ``check_text`` (so an ``Uncertain`` takes the policy's ``on_uncertain``). Nothing
  short-circuits: every violated policy is named, each once, deterministic ones first and then
  by policy id, with the locators it hit.
- **Budget:** the model-assisted checks cost ``G = policies * texts`` calls (plus whatever the
  caller reserves, e.g. #57's generation call). If the budget cannot cover that before the
  first call, the request is refused 422 ``budget_below_safety_minimum`` with
  ``minimum_required`` — no call is made and nothing is written (NFR-PERF-6).
- **The audit row** holds the policy ids, story, principal, entry point and total text length;
  never the text (NFR-SEC-4). A ``ModelUnavailable`` during a check propagates (503) and writes
  no audit row: nothing was decided.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import audit
from epistrel.core.errors import BudgetBelowSafetyMinimum, GuardrailViolation
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import (
    ModelAssistedRule,
    Policy,
    PolicyKind,
    Scope,
    Target,
    Violation,
)
from epistrel.orchestrator.budget import DEFAULT_TURN_DEADLINE_S, TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.policies.check import check_text, guardrail_violation
from epistrel.orchestrator.policies.metrics import GUARDRAIL_VIOLATIONS
from epistrel.orchestrator.policies.store import GuardContext

STAGE = "input"


class EntryPoint(StrEnum):
    """Where gated text entered (the audit row's ``details.entry_point``)."""

    RESPOND = "respond"
    REGENERATE = "regenerate"
    INGEST = "ingest"
    PINNED_MEMORIES = "pinned_memories"
    CUSTOM_INSTRUCTIONS = "custom_instructions"
    STORY_CREATE = "story_create"


@dataclass(frozen=True, slots=True)
class GateText:
    """One text to gate and where it sits in the request."""

    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class InputViolation:
    """A policy the input breaks, and every text it broke."""

    violation: Violation
    locators: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputViolations:
    """The gate's refusal: every violated policy, plus what the gate cost."""

    violations: tuple[InputViolation, ...]
    model_calls: int
    elapsed_ms: int

    @property
    def policy_ids(self) -> tuple[str, ...]:
        return tuple(v.violation.policy_id for v in self.violations)

    def error(self, policies: Sequence[Policy]) -> GuardrailViolation:
        """422 ``guardrail_violation`` naming each policy (``[{policy_id, name, message,
        locator|locators}]``), with ``model_calls`` and ``elapsed_ms``."""
        return guardrail_violation(
            [v.violation for v in self.violations],
            policies,
            locators={v.violation.policy_id: v.locators for v in self.violations},
            extra={"model_calls": self.model_calls, "elapsed_ms": self.elapsed_ms},
        )


@dataclass(frozen=True, slots=True)
class InputPass:
    """The gate let the input through; what it cost (a turn reports it)."""

    model_calls: int
    elapsed_ms: int


def input_policies(context: GuardContext, *, actor: str | None = None) -> tuple[Policy, ...]:
    """The active policies that gate input; a character policy only when its character acts."""
    return tuple(
        p
        for p in context.active
        if Target.TEXT_INPUT in p.applies_to
        and (p.scope is not Scope.CHARACTER or (actor is not None and p.scope_id == actor))
    )


def model_assisted(policies: Sequence[Policy]) -> tuple[Policy, ...]:
    return tuple(p for p in policies if isinstance(p.rule, ModelAssistedRule))


def calls_needed(policies: Sequence[Policy], texts: Sequence[GateText]) -> int:
    """G for these texts: one ``guardrail_check`` per model-assisted policy per text."""
    return len(model_assisted(policies)) * len(texts)


def input_budget(
    policies: Sequence[Policy],
    texts: Sequence[GateText],
    *,
    deadline_s: float = DEFAULT_TURN_DEADLINE_S,
) -> TurnBudget:
    """The budget of a gate outside a turn (ingest, pins, instructions, story creation):
    exactly G calls, under the story's turn deadline (#69; the default for a story not
    created yet). It makes no generation call, so it is never below its safety minimum."""
    return TurnBudget(max_calls=calls_needed(policies, texts), deadline_s=deadline_s)


async def input_gate(
    models: ModelAccessLayer,
    context: GuardContext,
    texts: Sequence[GateText],
    *,
    budget: TurnBudget,
    actor: str | None = None,
    reserved: int = 0,
) -> InputViolations | None:
    """Check ``texts`` against the input policies of ``context``; ``None`` when all pass."""
    started = time.monotonic()
    used_before = budget.used
    gating = input_policies(context, actor=actor)
    assisted = model_assisted(gating)
    required = calls_needed(gating, texts) + reserved
    if not budget.can_afford(required):
        raise BudgetBelowSafetyMinimum(
            f"the model budget cannot cover the {required} calls this request must reserve",
            {"minimum_required": required, "max_calls": budget.max_calls},
        )
    hits: dict[str, tuple[Violation, list[str]]] = {}

    def hit(violation: Violation, locator: str) -> None:
        entry = hits.setdefault(violation.policy_id, (violation, []))
        if locator not in entry[1]:
            entry[1].append(locator)

    for item in texts:
        for violation in evaluate(item.text, gating, Target.TEXT_INPUT):
            hit(violation, item.locator)
    for policy in assisted:
        for item in texts:
            outcome = await check_text(
                models,
                item.text,
                [policy],
                Target.TEXT_INPUT,
                budget=budget,
                cast=context.cast,
                threshold=context.threshold,
            )
            for violation in outcome.violations:
                hit(violation, item.locator)
    if not hits:
        return None
    ordered = sorted(
        hits.values(),
        key=lambda entry: (entry[0].kind is not PolicyKind.DETERMINISTIC, entry[0].policy_id),
    )
    return InputViolations(
        violations=tuple(InputViolation(v, tuple(where)) for v, where in ordered),
        model_calls=budget.used - used_before,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )


async def enforce_input(
    db: AsyncEngine,
    models: ModelAccessLayer,
    context: GuardContext,
    texts: Sequence[GateText],
    *,
    entry_point: EntryPoint,
    principal: str,
    story_id: str | None,
    budget: TurnBudget | None = None,
    actor: str | None = None,
    reserved: int = 0,
    deadline_s: float = DEFAULT_TURN_DEADLINE_S,
) -> InputPass:
    """Run the gate; on a violation write the ``audit_log`` row (no text) and raise 422
    ``guardrail_violation``. Without ``budget`` the gate gets ``input_budget`` under
    ``deadline_s``."""
    started = time.monotonic()
    if budget is None:
        budget = input_budget(input_policies(context, actor=actor), texts, deadline_s=deadline_s)
    used_before = budget.used
    found = await input_gate(models, context, texts, budget=budget, actor=actor, reserved=reserved)
    if found is None:
        return InputPass(
            model_calls=budget.used - used_before,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
    for entry in found.violations:
        GUARDRAIL_VIOLATIONS.labels(kind=entry.violation.kind.value, stage=STAGE).inc()
    await audit.record(
        db,
        audit.AuditKind.GUARDRAIL_INPUT_REJECTED,
        principal=principal,
        story_id=story_id,
        policy_ids=found.policy_ids,
        details={
            "entry_point": entry_point.value,
            "text_length": sum(len(item.text) for item in texts),
        },
    )
    raise found.error(context.policies)


__all__ = [
    "EntryPoint",
    "GateText",
    "InputPass",
    "InputViolation",
    "InputViolations",
    "calls_needed",
    "enforce_input",
    "input_budget",
    "input_gate",
    "input_policies",
    "model_assisted",
]
