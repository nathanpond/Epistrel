"""A test-only model-using operation (#53): a guarded reply, until #57's real turn lands.

``guarded_reply`` runs inside one ``turn`` ``Operation``:

1. the input check: every model-assisted policy that applies to ``text_input`` is checked with
   ``structured_call("guardrail_check", ...)`` behind the deterministic policies'
   ``evaluate`` (FR-MODEL-5); an ``Uncertain`` result takes the policy's ``on_uncertain``;
2. unless the input was refused, one ``generation`` call;
3. the same check on the reply for ``text_output``;
4. one ``test_event`` recording the outcome, then commit.

Any ``ModelError`` escapes the ``async with``, so the operation is marked ``failed``, nothing is
committed (no event, no revision change), and the REST route answers 503
``model_unavailable`` with ``details.purpose`` (NFR-REL-3). The event records only the
verdicts and the reply ``generation`` produced; a guardrail answer is never stored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.responses import MutationResponse
from epistrel.core.enums import EventCategory, OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.events.registry import EventPayload, register, registered_types
from epistrel.core.ids import new_ulid
from epistrel.core.policies.model import (
    RULE_ADAPTER,
    Effect,
    ModelAssistedRule,
    Policy,
    Scope,
    Target,
    kind_of,
)
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.metrics import ModelCall, recording
from epistrel.orchestrator.models.registry import (
    GenerationInputs,
    GuardrailCheckInputs,
    GuardrailVerdict,
    PlayerInput,
)
from epistrel.orchestrator.models.structured import (
    Uncertain,
    guardrail_deterministic_first,
    uncertain_verdict,
)

TEST_EVENT = "test_event"
TEST_PREFIX = "/_test"
PLAYER = "Player"

type DecidedBy = Literal["none", "deterministic", "model", "uncertain"]


class GuardedReplyEvent(EventPayload):
    """What the test operation committed: verdicts and the generated reply, nothing else."""

    said: str
    reply: str | None
    input_violation: bool
    output_violation: bool
    input_decided_by: DecidedBy
    output_decided_by: DecidedBy


if TEST_EVENT not in registered_types():
    register(TEST_EVENT, category=EventCategory.NARRATIVE_STATE)(GuardedReplyEvent)


def make_policy(rule: Mapping[str, object], *, name: str) -> Policy:
    """An in-memory global policy over text input and output."""
    parsed = RULE_ADAPTER.validate_python(rule)
    pid = new_ulid()
    now = datetime.now(UTC)
    return Policy(
        id=pid,
        scope=Scope.GLOBAL,
        scope_id=None,
        story_id=None,
        name=name,
        applies_to=(Target.TEXT_INPUT, Target.TEXT_OUTPUT),
        kind=kind_of(parsed),
        effect=Effect.DENY,
        rule=parsed,
        age_condition=None,
        message=f"{name} says no",
        version=1,
        created_by="test",
        updated_by="test",
        created_at=now,
        updated_at=now,
        retired_at=None,
    )


async def check(
    engine: Engine,
    text: str,
    policies: Sequence[Policy],
    target: Target,
    budget: TurnBudget,
) -> tuple[bool, DecidedBy]:
    """Whether ``text`` violates ``policies`` for ``target``, and what decided it."""
    applicable = [p for p in policies if target in p.applies_to]
    deterministic = [p for p in applicable if not isinstance(p.rule, ModelAssistedRule)]
    step = guardrail_deterministic_first(text, deterministic, target)
    fired: list[GuardrailVerdict] = []

    def first() -> GuardrailVerdict | None:
        conclusive = step()
        if conclusive is not None:
            fired.append(conclusive)
        return conclusive

    for policy in applicable:
        rule = policy.rule
        if not isinstance(rule, ModelAssistedRule):
            continue
        result = await engine.models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            GuardrailCheckInputs(text=text, policy_prompt=rule.prompt),
            deterministic_first=first,
            budget=budget,
        )
        if fired:
            return True, "deterministic"
        if isinstance(result, Uncertain):
            verdict = uncertain_verdict(rule.on_uncertain)
            if verdict.violation:
                return True, "uncertain"
        elif result.violation:
            return True, "model"
    if step() is not None:
        return True, "deterministic"
    return False, "model" if len(deterministic) < len(applicable) else "none"


async def guarded_reply(
    engine: Engine,
    story_id: str,
    said: str,
    *,
    principal: str,
    policies: Sequence[Policy] = (),
    budget: TurnBudget | None = None,
    params: ModelParams | None = None,
) -> Operation:
    """Run the guarded reply as one operation; see the module docstring."""
    budget = budget or TurnBudget()
    async with Operation(
        engine.db, story_id=story_id, kind=OperationKind.TURN, principal=principal
    ) as op:
        input_violation, input_by = await check(engine, said, policies, Target.TEXT_INPUT, budget)
        reply: str | None = None
        output_violation, output_by = False, "none"
        if not input_violation:
            inputs = GenerationInputs(player_input=PlayerInput(name=PLAYER, say=said))
            result = await engine.models.call("generation", inputs, params, budget=budget)
            output_violation, output_by = await check(
                engine, result.text, policies, Target.TEXT_OUTPUT, budget
            )
            reply = None if output_violation else result.text
        op.append(
            TEST_EVENT,
            GuardedReplyEvent(
                said=said,
                reply=reply,
                input_violation=input_violation,
                output_violation=output_violation,
                input_decided_by=input_by,
                output_decided_by=output_by,
            ),
            provenance=Provenance.PLAYER,
        )
        await op.commit()
    return op


class GuardedReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    said: str = Field(min_length=1, max_length=2_000)
    words: list[str] = Field(default_factory=list, max_length=100)
    model_prompt: str | None = Field(default=None, max_length=4_000)
    on_uncertain: Literal["violation", "pass"] = "violation"
    deadline_s: float | None = Field(default=None, gt=0, le=300)
    max_calls: int | None = Field(default=None, ge=0, le=50)
    timeout_s: float | None = Field(default=None, gt=0, le=600)

    def policies(self) -> list[Policy]:
        found: list[Policy] = []
        if self.words:
            found.append(make_policy({"type": "wordlist", "words": self.words}, name="words"))
        if self.model_prompt is not None:
            rule = {
                "type": "model_assisted",
                "prompt": self.model_prompt,
                "on_uncertain": self.on_uncertain,
            }
            found.append(make_policy(rule, name="model"))
        return found


class GuardedReplyResponse(MutationResponse):
    operation_id: str
    reply: str | None
    input_violation: bool
    output_violation: bool
    model_calls: list[ModelCall]
    """Every model call attempt the operation made (FR-MODEL-12), as #57's turns report them."""


def make_router() -> APIRouter:
    """``POST /_test/stories/{story_id}/guarded-reply``, registered only by tests."""
    router = APIRouter(prefix=TEST_PREFIX)

    @router.post("/stories/{story_id}/guarded-reply")
    async def guarded(
        story_id: str, body: GuardedReplyBody, ctx: ContextDep, engine: EngineDep
    ) -> GuardedReplyResponse:
        principal = engine.require_principal(ctx)
        budget = TurnBudget(max_calls=body.max_calls, deadline_s=body.deadline_s)
        params = ModelParams(timeout=body.timeout_s) if body.timeout_s else None
        with recording() as calls:
            op = await guarded_reply(
                engine,
                story_id,
                body.said,
                principal=principal,
                policies=body.policies(),
                budget=budget,
                params=params,
            )
        event = op.staged[0].payload if op.staged else None
        assert isinstance(event, GuardedReplyEvent)  # noqa: S101 - guarded_reply stages exactly one
        assert op.timeline_revision is not None  # noqa: S101 - set by commit()
        return GuardedReplyResponse(
            timeline_revision=op.timeline_revision,
            operation_id=op.operation_id,
            reply=event.reply,
            input_violation=event.input_violation,
            output_violation=event.output_violation,
            model_calls=list(calls.calls),
        )

    return router


__all__ = [
    "TEST_EVENT",
    "GuardedReplyBody",
    "GuardedReplyEvent",
    "check",
    "guarded_reply",
    "make_policy",
    "make_router",
]
