"""The policy evaluation a catalog validation asks for (FR-GUARD-10, FR-SCN-5; #111): the
Orchestrator's ``PolicyEvaluator`` port, which ``Engine`` wires into Core.

Catalog content enters no story until genesis, so a publish (or a dry-run validation) reports
input-policy hits as warnings and never refuses: this evaluation raises nothing, writes no audit
row and records nothing. It is not #56's gate (``input_gate``), which refuses, audits and needs a
budget covering every model-assisted check before it starts.

- **The policies** are the creation context's (#56's ``creation_context``): the live global
  policies and the closure of the scenario's rating, each applying to ``text_input``
  (``input_policies``; a rating policy only when it is set to input). The context has no cast,
  so age-conditioned policies (the seeded minors policy) are inactive: they are skipped and
  listed ``age_conditioned_policies``.
- **Deterministic** policies (word lists, name lists, patterns) run over every text with
  ``evaluate``: no model call. A word or name list's hit carries the matched span.
- **Model-assisted** policies run one ``guardrail_check`` call per policy and text, in policy
  id order, while the ``max_model_calls`` budget (the deployment's ``validation_model_calls``,
  default 0) can afford one more. What the budget does not reach, and everything after a model
  that cannot answer (``ModelError``: unavailable, a deadline), is listed
  ``input_policy_model_assisted``, never passed. An ``Uncertain`` verdict follows the policy's
  ``on_uncertain``, except one the budget cut short, which is listed too.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.policies.evaluate import evaluate, matched_span
from epistrel.core.policies.model import ModelAssistedRule, Policy, Target
from epistrel.core.ports import PolicyEvaluation, PolicyHit, PortText
from epistrel.orchestrator.budget import DEFAULT_TURN_DEADLINE_S, TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import GuardrailVerdict
from epistrel.orchestrator.models.structured import (
    Uncertain,
    guardrail_deterministic_first,
    uncertain_verdict,
)
from epistrel.orchestrator.policies.age import check_inputs
from epistrel.orchestrator.policies.input_gate import input_policies, model_assisted
from epistrel.orchestrator.policies.store import creation_context

AGE_CONDITIONED = "age_conditioned_policies"
MODEL_ASSISTED = "input_policy_model_assisted"
PURPOSE = "guardrail_check"
BUDGET_EXHAUSTED = "budget_exhausted"


def _hit(item: PortText, policy: Policy, span: str | None) -> PolicyHit:
    return PolicyHit(
        locator=item.locator,
        policy_id=policy.id,
        name=policy.name,
        message=policy.message,
        span=span,
    )


class CatalogPolicyCheck:
    """``PolicyEvaluator`` for the catalog validation (see the module docstring)."""

    def __init__(self, models: ModelAccessLayer) -> None:
        self.models = models

    async def evaluate(
        self,
        db: AsyncEngine,
        texts: Sequence[PortText],
        *,
        rating_id: str | None = None,
        max_model_calls: int = 0,
    ) -> PolicyEvaluation:
        context = await creation_context(db, rating_id, ())
        gating = input_policies(context)
        unavailable: list[str] = []
        if any(Target.TEXT_INPUT in policy.applies_to for policy in context.inactive):
            unavailable.append(AGE_CONDITIONED)
        hits: list[PolicyHit] = []
        for item in texts:
            for violation in evaluate(item.text, gating, Target.TEXT_INPUT):
                policy = next(p for p in gating if p.id == violation.policy_id)
                hits.append(_hit(item, policy, matched_span(item.text, policy)))
        budget = TurnBudget(max_calls=max_model_calls, deadline_s=DEFAULT_TURN_DEADLINE_S)
        assisted = sorted(model_assisted(gating), key=lambda policy: policy.id)
        if (
            assisted
            and texts
            and not await self._assisted(assisted, texts, budget, context.threshold, hits)
        ):
            unavailable.append(MODEL_ASSISTED)
        return PolicyEvaluation(
            hits=tuple(hits), unavailable=tuple(unavailable), model_calls=budget.used
        )

    async def _assisted(
        self,
        assisted: Sequence[Policy],
        texts: Sequence[PortText],
        budget: TurnBudget,
        threshold: int,
        hits: list[PolicyHit],
    ) -> bool:
        """Check every model-assisted policy against every text while the budget affords it;
        ``False`` when some (policy, text) was left unchecked."""
        for policy in assisted:
            rule = policy.rule
            if not isinstance(rule, ModelAssistedRule):  # pragma: no cover - model_assisted()
                continue
            for item in texts:
                if not budget.can_afford(1):
                    return False
                try:
                    result = await self.models.structured_call(
                        PURPOSE,
                        GuardrailVerdict,
                        check_inputs(item.text, policy, (), threshold),
                        deterministic_first=guardrail_deterministic_first(
                            item.text, (), Target.TEXT_INPUT
                        ),
                        budget=budget,
                    )
                except ModelError:
                    return False
                if isinstance(result, Uncertain):
                    if result.reason == BUDGET_EXHAUSTED:
                        return False
                    if uncertain_verdict(rule.on_uncertain).violation:
                        hits.append(_hit(item, policy, None))
                elif result.violation:
                    hits.append(_hit(item, policy, None))
        return True


__all__ = ["AGE_CONDITIONED", "MODEL_ASSISTED", "CatalogPolicyCheck"]
