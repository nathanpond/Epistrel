"""One text against a set of policies (FR-GUARD-9,11): deterministic first, then one
``guardrail_check`` call per applicable model-assisted policy.

#56's input gate (``input_gate``) and #68's output gate build on ``check_text``; #53's
test-only guarded reply (``epistrel.testing.ops``) calls it too.

- Policies that do not apply to ``target`` are skipped, and so are age-conditioned policies
  whose condition is false for the cast (``age.applies``): that is decided before any model
  call, so a story without a minor pays nothing for the minors policy.
- #49's ``evaluate`` runs as the deterministic step of every model call (FR-MODEL-5): a
  deterministic violation is conclusive, and no model is asked.
- An age-conditioned policy's check receives the cast as positional subjects with ages
  (``age.check_inputs``); any other policy receives none.
- ``Uncertain`` takes the policy's ``on_uncertain`` (the seeded minors policy: ``violation``).
- The first violation ends the check.

``guardrail_violation`` turns violations into the 422 a caller raises. For an age-conditioned
policy it says that a minor is involved or present and nothing else: the policy's own message,
its id and its name — never a character's name, label or age.

``refuse_withheld`` is what a gate calls before it serves a violation: while a pending
catalog-scope erasure reaches a violated policy's text (#447, ADR-161), the violation is not
served (its name and message may name the erased subject) and the caller gets 503
``erasure_pending`` instead, as a read of the policy by id does (#429). A retry after the
reapply gets the real outcome.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.erasure import catalog_block
from epistrel.core.errors import GuardrailViolation
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Target, Violation
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.registry import GuardrailVerdict
from epistrel.orchestrator.models.structured import (
    Uncertain,
    guardrail_deterministic_first,
    uncertain_verdict,
)
from epistrel.orchestrator.policies.age import CastMember, applies, check_inputs

DEFAULT_MINOR_AGE_THRESHOLD = 18
MINOR_INVOLVED = "a minor is involved or present"

type DecidedBy = Literal["none", "deterministic", "model", "uncertain"]


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The violations found (empty when the text passed), what decided the outcome, and the
    guardrail model's confidence when a model verdict decided it (#68 records it)."""

    violations: tuple[Violation, ...]
    decided_by: DecidedBy
    confidence: float | None = None

    @property
    def violated(self) -> bool:
        return bool(self.violations)


def _model_violation(policy: Policy) -> Violation:
    return Violation(
        policy_id=policy.id, name=policy.name, message=policy.message, kind=policy.kind
    )


async def check_text(
    models: ModelAccessLayer,
    text: str,
    policies: Sequence[Policy],
    target: Target,
    *,
    budget: TurnBudget,
    cast: Sequence[CastMember] = (),
    threshold: int = DEFAULT_MINOR_AGE_THRESHOLD,
) -> CheckOutcome:
    """Check ``text`` against the ``policies`` that apply to ``target`` and to ``cast``."""
    applicable = [p for p in policies if target in p.applies_to and applies(p, cast, threshold)]
    deterministic = [p for p in applicable if not isinstance(p.rule, ModelAssistedRule)]
    step = guardrail_deterministic_first(text, deterministic, target)
    fired: list[GuardrailVerdict] = []

    def first() -> GuardrailVerdict | None:
        conclusive = step()
        if conclusive is not None:
            fired.append(conclusive)
        return conclusive

    def by_rules() -> CheckOutcome:
        return CheckOutcome(tuple(evaluate(text, deterministic, target)), "deterministic")

    for policy in applicable:
        rule = policy.rule
        if not isinstance(rule, ModelAssistedRule):
            continue
        result = await models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            check_inputs(text, policy, cast, threshold),
            deterministic_first=first,
            budget=budget,
        )
        if fired:
            return by_rules()
        if isinstance(result, Uncertain):
            if uncertain_verdict(rule.on_uncertain).violation:
                return CheckOutcome((_model_violation(policy),), "uncertain")
        elif result.violation:
            return CheckOutcome((_model_violation(policy),), "model", result.confidence)
    if step() is not None:
        return by_rules()
    return CheckOutcome((), "model" if len(deterministic) < len(applicable) else "none")


def guardrail_violation(
    violations: Sequence[Violation],
    policies: Sequence[Policy],
    *,
    what: str = "Input",
    locators: Mapping[str, Sequence[str]] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> GuardrailViolation:
    """The 422 for ``violations``: each policy's id, name and message. When any is
    age-conditioned, the message adds that a minor is involved or present — never who.

    ``locators`` (#56) names, per policy id, where in the request each violation was found:
    one as ``locator``, several as ``locators``. ``extra`` adds top-level details."""
    conditioned = {p.id for p in policies if p.age_condition is not None}
    count = len(violations)
    noun = "policy" if count == 1 else "policies"
    message = f"{what} violates {count} {noun}"
    if any(v.policy_id in conditioned for v in violations):
        message = f"{message}: {MINOR_INVOLVED}"
    found: list[dict[str, Any]] = []
    for v in violations:
        entry: dict[str, Any] = {"policy_id": v.policy_id, "name": v.name, "message": v.message}
        where = list((locators or {}).get(v.policy_id, ()))
        if len(where) == 1:
            entry["locator"] = where[0]
        elif where:
            entry["locators"] = where
        found.append(entry)
    return GuardrailViolation(f"{message}.", {"violations": found, **(extra or {})})


async def refuse_withheld(db: AsyncEngine, policy_ids: Iterable[str]) -> None:
    """503 ``erasure_pending`` (``details.story_id`` null) when a pending catalog-scope erasure
    reaches any of ``policy_ids``' text (#447, ADR-161). Called only once a violation is found,
    so a passing text costs nothing; otherwise one query when nothing is pending."""
    wanted = sorted(set(policy_ids))
    if not wanted:
        return
    async with db.connect() as conn:
        block = await catalog_block.read(conn)
    if block is not None:
        block.check_records([(catalog_block.POLICIES, policy_id) for policy_id in wanted])


__all__ = [
    "DEFAULT_MINOR_AGE_THRESHOLD",
    "MINOR_INVOLVED",
    "CheckOutcome",
    "DecidedBy",
    "check_text",
    "guardrail_violation",
    "refuse_withheld",
]
