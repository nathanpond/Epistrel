"""The configuration re-check of a turn in flight (FR-CONC-16, FR-GUARD-9; #64, #68).

When an interpretive setting a Lite turn depends on changed between its start and its commit
(a rating's guardrails, a policy, ``minor_age_threshold``, the story's default output
viewpoint), ``run_with_revalidation`` re-captures the configuration and calls the re-check
built here before committing again. It checks the turn's staged reply — every
``message_rendered`` text, output only — against the story's output policies as they are now
(``store.guard_context``: live policies, the cast's true ages, the live threshold), through
#68's output gate check (``output_gate.check_output``): every deterministic policy, then one
``guardrail_check`` per model-assisted policy while the turn budget can afford it (one it
cannot afford resolves by its ``on_uncertain``). Character-scoped policies apply to the
reply's render characters.

A violation fails the re-check, and the turn regenerates within ``regenerate_retry_budget``,
then 409 ``revision_conflict`` — never #68's safe fallback for the re-check itself. The
regenerated reply goes through the output gate inside the turn's generate step like any
other generation (#68), so it is not checked twice.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.concurrency import ConfigRecheckFn
from epistrel.core.events import Operation
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.policies.output_gate import check_output, output_policies
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


def staged_render_ids(op: Operation) -> list[str] | None:
    """The render characters of the replies ``op`` would commit (``None``: none recorded)."""
    found: list[str] = []
    for staged in op.staged:
        if staged.type == REPLY_EVENT:
            found.extend(getattr(staged.payload, "render_character_ids", ()) or ())
    return found or None


async def output_passes(
    models: ModelAccessLayer,
    texts: Sequence[str],
    context: GuardContext,
    *,
    budget: TurnBudget,
    render_ids: Sequence[str] | None = None,
) -> bool:
    """Whether every text passes the context's output policies (the output gate's check)."""
    policies = output_policies(context, render_ids)
    for text in texts:
        if not (await check_output(models, text, policies, context, budget=budget)).passed:
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
        return await output_passes(
            models, staged_replies(op), context, budget=budget, render_ids=staged_render_ids(op)
        )

    return recheck


__all__ = ["output_passes", "output_recheck", "staged_render_ids", "staged_replies"]
