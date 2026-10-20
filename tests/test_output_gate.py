"""The output gate's loop and fallback chain (#68; FR-GUARD-6), pure: deterministic policies
only, so no model is asked; a ``produce`` stand-in plays the generation."""

from __future__ import annotations

from typing import Any, cast

import pytest

from epistrel.core.errors import ErasurePending
from epistrel.core.policies.model import Scope, Target
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.policies.output_gate import (
    CONSTRAINT,
    FALLBACK_FINISH_REASON,
    Draft,
    FallbackLines,
    gate_output,
    output_policies,
    output_reservation,
    raised_temperature,
)
from epistrel.orchestrator.policies.store import GuardContext
from tests.policy_support import policy, words

NO_MODELS = cast(ModelAccessLayer, None)
"""Deterministic policies never reach the Model Access Layer."""

BANNED = policy(words("dragon"), policy_id="no-dragons", applies_to=(Target.TEXT_OUTPUT,))
ALSO = policy(words("goblin"), policy_id="no-goblins", applies_to=(Target.TEXT_OUTPUT,))


def _context(*policies: Any) -> GuardContext:
    return GuardContext(policies=tuple(policies), cast=(), threshold=18)


class Script:
    """A ``produce`` that answers the scripted texts in order and records what it was told."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.seen: list[tuple[tuple[str, ...], int]] = []

    async def __call__(self, constraints: tuple[str, ...], attempt: int) -> Draft:
        self.seen.append((constraints, attempt))
        return Draft(text=self.texts.pop(0), model="m", prompt_hash="h", finish_reason="stop")


async def _gate(
    script: Script, *policies: Any, retry: int = 2, withheld: Any = None, **lines: Any
) -> Any:
    context = _context(*policies)
    return await gate_output(
        NO_MODELS,
        context,
        output_policies(context),
        script,
        budget=TurnBudget(),
        retry_budget=retry,
        fallback=FallbackLines(name="Ada", **lines),
        withheld=withheld,
    )


class Withholding:
    """A ``withheld`` hook refusing the listed policy ids (as ``check.refuse_withheld`` does
    while a pending catalog-scope erasure reaches them), recording what it was asked."""

    def __init__(self, *policy_ids: str) -> None:
        self.policy_ids = set(policy_ids)
        self.asked: list[list[str]] = []

    async def __call__(self, policy_ids: Any) -> None:
        self.asked.append(list(policy_ids))
        if self.policy_ids & set(policy_ids):
            raise ErasurePending("withheld", {"story_id": None})


async def test_regenerates_with_accumulating_constraints() -> None:
    """Two violating drafts, then a clean one: three generations, the policy messages fed
    back once each and accumulated; no fallback. Complement: a clean first draft is released
    as is with no constraint and no rejected record."""
    script = Script("A dragon.", "A goblin.", "A cat.")
    gated = await _gate(script, BANNED, ALSO)
    assert gated.draft.text == "A cat."
    assert (gated.fallback_used, gated.violations, gated.attempts) == (False, (), 3)
    dragon = CONSTRAINT.format(message="no-dragons says no")
    goblin = CONSTRAINT.format(message="no-goblins says no")
    assert script.seen == [((), 0), ((dragon,), 1), ((dragon, goblin), 2)]
    assert [(r.text, r.attempt, r.reason) for r in gated.rejected] == [
        ("A dragon.", 0, "output_policy"),
        ("A goblin.", 1, "output_policy"),
    ]
    assert [v.policy_id for v in gated.rejected[0].check.violations] == ["no-dragons"]

    clean = Script("A cat.")
    released = await _gate(clean, BANNED)
    assert (released.draft.text, released.rejected, clean.seen) == ("A cat.", (), [((), 0)])


async def test_budget_spent_uses_the_fallback_chain() -> None:
    """Always violating: 1 + budget generations, then the character's line with ``{name}``
    substituted. A character line that breaks a policy is itself rejected and the deployment
    line is used; one that breaks too falls to the last resort, released with its
    violations. ``guardrail_retry_budget`` 0 goes straight to the fallback."""
    script = Script("A dragon.", "A dragon.", "A dragon.")
    gated = await _gate(script, BANNED, character_text="{name} shrugs.")
    assert len(script.seen) == 3
    assert (gated.draft.text, gated.fallback_used, gated.fallback_source) == (
        "Ada shrugs.",
        True,
        "character",
    )
    assert (gated.draft.model, gated.draft.prompt_hash) == (None, None)
    assert gated.draft.finish_reason == FALLBACK_FINISH_REASON
    assert gated.violations == ()

    script = Script("A dragon.")
    gated = await _gate(script, BANNED, retry=0, character_text="{name} feeds the dragon.")
    assert len(script.seen) == 1
    assert (gated.draft.text, gated.fallback_source) == ("Ada changes the subject.", "deployment")
    assert [(r.text, r.reason, r.attempt) for r in gated.rejected] == [
        ("A dragon.", "output_policy", 0),
        ("Ada feeds the dragon.", "fallback_policy", None),
    ]

    gated = await _gate(
        Script("A dragon."),
        BANNED,
        retry=0,
        deployment_text="{name} mentions a dragon.",
    )
    assert (gated.draft.text, gated.fallback_source) == ("Ada says nothing.", "last_resort")
    assert gated.violations == ()

    nothing = policy(words("nothing"), policy_id="no-nothing", applies_to=(Target.TEXT_OUTPUT,))
    gated = await _gate(Script("A dragon."), BANNED, nothing, retry=0, deployment_text="")
    assert (gated.draft.text, gated.fallback_source) == ("Ada says nothing.", "last_resort")
    assert [v.policy_id for v in gated.violations] == ["no-nothing"]


async def test_out_of_time_mid_regeneration_falls_back() -> None:
    """A regeneration whose call runs out of deadline gets the fallback (a draft exists);
    any other model failure mid-regeneration propagates (503)."""

    class Failing(Script):
        def __init__(self, error: Exception) -> None:
            super().__init__("A dragon.")
            self.error = error

        async def __call__(self, constraints: tuple[str, ...], attempt: int) -> Draft:
            if attempt:
                raise self.error
            return await super().__call__(constraints, attempt)

    gated = await _gate(Failing(ModelUnavailable("generation", "deadline")), BANNED)
    assert gated.fallback_used
    with pytest.raises(ModelUnavailable):
        await _gate(Failing(ModelUnavailable("generation", "http_500")), BANNED)


async def test_a_withheld_policy_refuses_before_its_message_is_used() -> None:
    """#447 (ADR-161): a draft breaking a withheld policy refuses the turn (503
    ``erasure_pending``) before the policy's message becomes a constraint, so nothing is
    generated again; a fallback line breaking one refuses too. Complement: a withheld policy
    nothing breaks changes nothing, and the hook is asked only about violations."""
    script = Script("A dragon.", "A cat.")
    with pytest.raises(ErasurePending):
        await _gate(script, BANNED, ALSO, withheld=Withholding("no-dragons"))
    assert script.seen == [((), 0)]

    hook = Withholding("no-dragons")
    with pytest.raises(ErasurePending):
        await _gate(
            Script("A goblin."),
            BANNED,
            ALSO,
            retry=0,
            character_text="{name} feeds the dragon.",
            withheld=hook,
        )
    assert hook.asked == [["no-goblins"], ["no-dragons"]]

    hook = Withholding("no-dragons")
    script = Script("A goblin.", "A cat.")
    gated = await _gate(script, BANNED, ALSO, withheld=hook)
    assert gated.draft.text == "A cat."
    assert script.seen == [((), 0), ((CONSTRAINT.format(message="no-goblins says no"),), 1)]
    assert hook.asked == [["no-goblins"]]


def test_which_policies_and_reservation() -> None:
    """Output policies only; a character policy only for a render character. Deterministic
    policies reserve nothing; each model-assisted output policy reserves one check."""
    input_only = policy(words("x"), policy_id="in", applies_to=(Target.TEXT_INPUT,))
    character = policy(
        words("y"), policy_id="ch", scope=Scope.CHARACTER, applies_to=(Target.TEXT_OUTPUT,)
    )
    context = _context(BANNED, input_only, character)
    assert [p.id for p in output_policies(context, ["s"])] == ["no-dragons", "ch"]
    assert [p.id for p in output_policies(context, ["other"])] == ["no-dragons"]
    assert output_reservation(context, ["s"]) == 0
    assisted = policy(
        {"type": "model_assisted", "prompt": "Gory?"},
        policy_id="gore",
        applies_to=(Target.TEXT_INPUT, Target.TEXT_OUTPUT),
    )
    assert output_reservation(_context(BANNED, assisted), ["s"]) == 1


def test_raised_temperature() -> None:
    """+0.1 per regeneration, capped at 2; the first attempt and an unset temperature are as
    given."""
    assert raised_temperature(0.8, 0) == 0.8
    assert raised_temperature(0.8, 2) == pytest.approx(1.0)
    assert raised_temperature(1.95, 1) == 2.0
    assert raised_temperature(None, 3) is None
