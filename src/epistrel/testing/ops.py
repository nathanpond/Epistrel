"""A test-only model-using operation (#53): a guarded reply, until #57's real turn lands.

``guarded_reply`` runs inside one ``turn`` ``Operation``:

1. the input check: every model-assisted policy that applies to ``text_input`` is checked with
   ``structured_call("guardrail_check", ...)`` behind the deterministic policies'
   ``evaluate`` (FR-MODEL-5); an ``Uncertain`` result takes the policy's ``on_uncertain``;
2. unless the input was refused, one ``generation`` call;
3. the same check on the reply for ``text_output``;
4. one ``test_event`` recording the outcome, then commit.

With ``story_policies`` (#55) the checks use the story's effective policies, its cast and the
live ``minor_age_threshold`` (``store.guard_context``), and the input check becomes a gate: it
runs before the operation opens, and a violation raises 422 ``guardrail_violation`` with
nothing written (as #56's input gate will). An output violation withholds the reply; #68 adds
regeneration and the safe fallback. ``POST /_test/stories/{id}/ingest-check`` gates a list of
ingest-shaped texts the same way, each its own text, until #58's ingest route exists.

The checks themselves are ``epistrel.orchestrator.policies.check.check_text``.

Any ``ModelError`` escapes the ``async with``, so the operation is marked ``failed``, nothing is
committed (no event, no revision change), and the REST route answers 503
``model_unavailable`` with ``details.purpose`` (NFR-REL-3). The event records only the
verdicts and the reply ``generation`` produced; a guardrail answer is never stored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Response, status
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
    Policy,
    Scope,
    Target,
    kind_of,
)
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.metrics import ModelCall, recording
from epistrel.orchestrator.models.registry import GenerationInputs, PlayerInput
from epistrel.orchestrator.policies.age import CastMember
from epistrel.orchestrator.policies.check import (
    DEFAULT_MINOR_AGE_THRESHOLD,
    DecidedBy,
    check_text,
    guardrail_violation,
)
from epistrel.orchestrator.policies.store import guard_context

TEST_EVENT = "test_event"
TEST_PREFIX = "/_test"
PLAYER = "Player"


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
    *,
    cast: Sequence[CastMember] = (),
    threshold: int = DEFAULT_MINOR_AGE_THRESHOLD,
) -> tuple[bool, DecidedBy]:
    """Whether ``text`` violates ``policies`` for ``target``, and what decided it."""
    outcome = await check_text(
        engine.models, text, policies, target, budget=budget, cast=cast, threshold=threshold
    )
    return outcome.violated, outcome.decided_by


async def gate(
    engine: Engine,
    texts: Sequence[str],
    policies: Sequence[Policy],
    budget: TurnBudget,
    *,
    cast: Sequence[CastMember] = (),
    threshold: int = DEFAULT_MINOR_AGE_THRESHOLD,
) -> None:
    """Raise 422 ``guardrail_violation`` for the first of ``texts`` that breaks an input
    policy; nothing is written either way."""
    for text in texts:
        outcome = await check_text(
            engine.models,
            text,
            policies,
            Target.TEXT_INPUT,
            budget=budget,
            cast=cast,
            threshold=threshold,
        )
        if outcome.violated:
            raise guardrail_violation(outcome.violations, policies)


async def guarded_reply(
    engine: Engine,
    story_id: str,
    said: str,
    *,
    principal: str,
    policies: Sequence[Policy] = (),
    budget: TurnBudget | None = None,
    params: ModelParams | None = None,
    cast: Sequence[CastMember] = (),
    threshold: int = DEFAULT_MINOR_AGE_THRESHOLD,
    gate_input: bool = False,
) -> Operation:
    """Run the guarded reply as one operation; see the module docstring. With ``gate_input``
    an input violation raises before the operation opens."""
    budget = budget or TurnBudget()
    gated: tuple[bool, DecidedBy] | None = None
    if gate_input:
        outcome = await check_text(
            engine.models,
            said,
            policies,
            Target.TEXT_INPUT,
            budget=budget,
            cast=cast,
            threshold=threshold,
        )
        if outcome.violated:
            raise guardrail_violation(outcome.violations, policies)
        gated = (False, outcome.decided_by)
    async with Operation(
        engine.db, story_id=story_id, kind=OperationKind.TURN, principal=principal
    ) as op:
        input_violation, input_by = gated or await check(
            engine, said, policies, Target.TEXT_INPUT, budget, cast=cast, threshold=threshold
        )
        reply: str | None = None
        output_violation, output_by = False, "none"
        if not input_violation:
            inputs = GenerationInputs(player_input=PlayerInput(name=PLAYER, say=said))
            result = await engine.models.call("generation", inputs, params, budget=budget)
            output_violation, output_by = await check(
                engine,
                result.text,
                policies,
                Target.TEXT_OUTPUT,
                budget,
                cast=cast,
                threshold=threshold,
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
    story_policies: bool = False
    """Check against the story's effective policies, cast and threshold, gating input (#55)."""

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


class IngestCheckBody(BaseModel):
    """Ingest-shaped texts, each gated on its own (#58's ingest route gates its events so)."""

    model_config = ConfigDict(extra="forbid")

    texts: list[str] = Field(min_length=1, max_length=20)


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
        policies = body.policies()
        ages: dict[str, Any] = {}
        if body.story_policies:
            context = await guard_context(engine.db, story_id)
            policies = [*context.policies, *policies]
            ages = {"cast": context.cast, "threshold": context.threshold, "gate_input": True}
        with recording() as calls:
            op = await guarded_reply(
                engine,
                story_id,
                body.said,
                principal=principal,
                policies=policies,
                budget=budget,
                params=params,
                **ages,
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

    @router.post("/stories/{story_id}/ingest-check", status_code=status.HTTP_204_NO_CONTENT)
    async def ingest_check(
        story_id: str, body: IngestCheckBody, ctx: ContextDep, engine: EngineDep
    ) -> Response:
        engine.require_principal(ctx)
        context = await guard_context(engine.db, story_id)
        await gate(
            engine,
            body.texts,
            context.policies,
            TurnBudget(),
            cast=context.cast,
            threshold=context.threshold,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = [
    "TEST_EVENT",
    "GuardedReplyBody",
    "GuardedReplyEvent",
    "check",
    "gate",
    "guarded_reply",
    "make_policy",
    "make_router",
]
