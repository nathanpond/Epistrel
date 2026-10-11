"""The configuration re-check of a turn in flight (FR-CONC-16, FR-GUARD-9; #64).

When an interpretive setting a Lite turn depends on changed between its start and its commit
(a rating's guardrails, a policy, ``minor_age_threshold``, the story's default output
viewpoint), ``run_with_revalidation`` re-captures the configuration and calls the re-check
built here before committing again. It checks the turn's staged reply — every
``message_rendered`` text, output only — against the story's effective output policies as they
are now (``store.guard_context``: live policies, the cast's true ages, the live threshold):

- every deterministic policy runs (#49's ``evaluate``, no model call);
- a model-assisted policy runs (one ``guardrail_check`` call through ``check_text``) only while
  the turn budget can afford it; one it cannot afford resolves by its ``on_uncertain`` (the
  seeded minors policy: violation; the generic default: pass).

A violation fails the re-check, and the turn regenerates within ``regenerate_retry_budget``,
then 409 ``revision_conflict`` — never #68's safe fallback.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.concurrency import ConfigRecheckFn
from epistrel.core.events import Operation
from epistrel.core.policies.evaluate import evaluate
from epistrel.core.policies.model import ModelAssistedRule, Policy, Target
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.structured import uncertain_verdict
from epistrel.orchestrator.policies.check import check_text
from epistrel.orchestrator.policies.store import GuardContext, guard_context

REPLY_EVENT = "message_rendered"


def staged_replies(op: Operation) -> list[str]:
    """The texts of the replies ``op`` would commit."""
    return [
        text
        for staged in op.staged
        if staged.type == REPLY_EVENT
        and isinstance(text := getattr(staged.payload, "text", None), str)
    ]


async def output_passes(
    models: ModelAccessLayer,
    texts: Sequence[str],
    context: GuardContext,
    *,
    budget: TurnBudget,
) -> bool:
    """Whether every text passes the context's active output policies (see the module
    docstring for affordability)."""
    applicable: list[Policy] = [p for p in context.active if Target.TEXT_OUTPUT in p.applies_to]
    deterministic = [p for p in applicable if not isinstance(p.rule, ModelAssistedRule)]
    assisted = [p for p in applicable if isinstance(p.rule, ModelAssistedRule)]
    for text in texts:
        if evaluate(text, deterministic, Target.TEXT_OUTPUT):
            return False
        for policy in assisted:
            rule = policy.rule
            assert isinstance(rule, ModelAssistedRule)  # noqa: S101 - filtered above
            if budget.expired() or not budget.can_afford(1):
                if uncertain_verdict(rule.on_uncertain).violation:
                    return False
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
            if outcome.violated:
                return False
    return True


def output_recheck(
    db: AsyncEngine, models: ModelAccessLayer, budget: TurnBudget
) -> ConfigRecheckFn:
    """The ``config_recheck`` of a Lite turn: its staged replies against the story's output
    policies as they are now."""

    async def recheck(op: Operation, changed: tuple[str, ...]) -> bool:
        assert op.story_id is not None  # noqa: S101 - a turn has a story
        context = await guard_context(db, op.story_id)
        return await output_passes(models, staged_replies(op), context, budget=budget)

    return recheck


__all__ = ["output_passes", "output_recheck", "staged_replies"]
