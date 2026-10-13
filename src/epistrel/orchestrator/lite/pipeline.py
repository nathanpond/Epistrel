"""The Lite turn: assemble → generate → record (FR-RESP-1..4,9, FR-LITE-1,4,5,7, FR-PROF-11; #57).

``respond`` runs one Lite turn of a story for ``Engine.respond``:

1. **Prechecks**, in #44's canonical order, before anything is written: the story exists and
   the request's characters are its cast (``cast.resolve_cast``, #62: 422 ``validation_error``
   with ``unknown_actor`` / ``unknown_render_ids`` / ``unknown_present_ids``), the story is not
   archived, the ``Idempotency-Key`` is claimed (#61: the request hash covers the body with the
   *resolved*, deduped render set, the actor, the *resolved* viewpoint and the expected
   revision, so a retry replays the stored turn and a key reused for another viewpoint or
   render set is 409 ``idempotency_conflict``), the expected revision holds
   (409), and the output viewpoint resolves (422 ``viewpoint_unresolved``). The requested viewpoint
   arrives on ``ctx.viewpoint``, the single carrier (``Engine.respond`` folds the body field and
   the keyword argument into it).
2. **The configuration and the budget.** The turn captures the story's effective
   configuration once, here at its start (#113, ``core.profiles.effective``: the deployment's
   defaults, then its profile's version, then its overrides), and runs under it to the end: a
   change of the profile or the overrides reaches the next turn, never this one (prospective).
   Its budget (#69, ``budget.plan_lite_budget``) is the request's ``budget.max_calls``, else the
   configuration's ``budgets.model_calls``, else ``9 + G``, and the configuration's
   ``budgets.deadline_ms`` (or the request's ``budget.deadline_s``), its clock started when the
   request arrived. It reserves the generation call and G, the model-assisted guardrail calls
   (one per input policy for the input, one per output policy for the first output check); a
   cap below ``1 + G`` is 422 ``budget_below_safety_minimum`` before any model call. Then **the
   input
   gate** (#56) over the actor's ``say``/``do`` joined with a newline. A violation is 422
   with only the audit row written. Then, last, **the turn gate** (FR-CONC-5, #63): the turn
   takes the story's turn slot (``core.turn_gate``); while another turn holds it, a ``reject``
   story answers 423 ``story_busy`` and a ``queue`` story queues the turn (``queue_turn``, from
   the Engine), which its drainer later plays through ``run_queued``.
3. **One ``turn`` operation** under #47's revision protocol (``run_with_revalidation``): the
   read phase reads the prompt inputs in the snapshot the read set hashes (so the read set *is*
   the prompt's inputs) and resolves the generation settings, profile → story overrides →
   request (FR-LITE-4); the generate step makes one ``generation`` call (FR-LITE-7) — an empty
   reply is asked once more if the budget's unreserved remainder can pay for it, then 503
   ``model_unavailable`` ``reason=empty`` — and passes the
   reply through **the output gate** (#68, ``policies.output_gate``): the story's output
   policies, deterministic first, then one ``guardrail_check`` per model-assisted policy; a
   violation generates again with the policy's message as a constraint, within the
   configuration's polish budget (``repair.polish.max_regenerations``; none with the polish
   pass off) and the turn budget, then releases the safe fallback line. It
   stages the input as beat 1, every rejected text as a ``guardrail_output_rejected`` audit
   event, and the released reply as beat 2. A touched read set regenerates the same operation;
   an interpretive configuration change the turn depends on (a rating's guardrails, a policy,
   ``minor_age_threshold``, the default output viewpoint) re-checks the reply under the new
   configuration before it commits (``policies.rebase``, #64). A regeneration the budget's
   unreserved remainder cannot pay for is 409 ``revision_conflict`` (``reason=budget``).
4. **The result**: the finished message (never "verified": under ``lite`` no claim is
   checked), the turn's beats for the resolved viewpoint, the new revision, the configuration
   it ran under (#109, FR-PROF-16: ``fingerprint``, ``capabilities`` and ``quality_status``,
   stamped from the configuration captured at turn start, and recorded in
   ``operations.config_revisions`` with the profile, the version, the overrides' revision and
   each purpose's model, read in the snapshot the prompt is read in; #113 adds ``profile {id,
   version}`` and the story's ``config_revision`` to the result), and the model calls
   the turn made (``model_calls``, #54, and ``call_counts`` by purpose) with its ``elapsed_ms``
   (fixed at the commit), its ``budget`` ``{max_calls, reserved, used}`` (#69), and the
   output gate's ``guardrail`` report (``degradation`` ``["safe_fallback"]`` when the reply is
   the fallback, with its cause in ``degradation_details``). The same report is stored on the
   operation's ``turn_report``; a model failure that ends the turn (503) carries
   ``model_calls`` and ``elapsed_ms`` in its details. Under an
   idempotent call it is built and stored ``done`` inside the turn's commit transaction, so a
   committed turn always has its result for a retry.

``regenerate`` (#66, FR-STORE-12) plays the head turn N again as variant ``v + 1``: it reuses
the recorded ``player_input`` (re-gated, #56), render set, output viewpoint and resolved
generation settings unless the request overrides them, reads the prompt inputs from before turn
N, takes the story's turn slot (a busy story is 423 ``story_busy`` whatever its concurrency
mode: a regenerate is a control action and is never queued), and commits one ``turn``
operation at N that makes everything the old variant held ``dormant`` and fails the story's
queued turns with ``timeline_changed``, all in one transaction. A failure leaves the old variant
active.

Scene context is validated by the request model and the cast check, then dropped (FR-RESP-4).
This module must not import ``orchestrator.policies.admin`` (import-linter): the Engine passes
the gate in as ``gate``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import idempotency, turn_gate
from epistrel.core.concurrency import LiteReadSet, check_expected_revision, run_with_revalidation
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, StoryStatus
from epistrel.core.errors import RevisionConflict, StoryArchived, TimelineChanged
from epistrel.core.events import Operation
from epistrel.core.events.types import GuardrailOutputRejected, RejectedViolation
from epistrel.core.history import turn_beats
from epistrel.core.profiles.effective import ProfileVersionRef, StoryProfile
from epistrel.core.profiles.fingerprint import Stamp
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import (
    GenerationSettings,
    ResolvedGeneration,
    resolve_generation,
)
from epistrel.core.tables import operations
from epistrel.core.turn_gate import Concurrency
from epistrel.core.turns import (
    REPLY_BEAT,
    RecordedTurn,
    TurnInputs,
    joined_input,
    read_regenerable,
    read_story_cast,
    read_turn_inputs,
    stage_turn,
)
from epistrel.core.viewpoint import resolve_viewpoint, try_resolve
from epistrel.observability import stages
from epistrel.observability.logging import get_logger
from epistrel.orchestrator.budget import GUARDRAIL_OUTPUT, TurnBudget, plan_lite_budget
from epistrel.orchestrator.lite.cast import ResolvedCast, resolve_cast
from epistrel.orchestrator.lite.prompt import assemble, model_params, prompt_hash
from epistrel.orchestrator.lite.schemas import (
    SAFE_FALLBACK,
    BudgetOverride,
    BudgetReport,
    DegradationStep,
    FinishedMessage,
    GuardrailReport,
    GuardrailViolationItem,
    PlayerInputBody,
    RegenerateRequest,
    RegenerateResult,
    RespondRequest,
    RespondResult,
)
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelResult
from epistrel.orchestrator.models.errors import EmptyCompletion, ModelError
from epistrel.orchestrator.models.facts import runtime_facts
from epistrel.orchestrator.models.metrics import CallLog, recording
from epistrel.orchestrator.policies.input_gate import input_policies, model_assisted
from epistrel.orchestrator.policies.output_gate import (
    Draft,
    FallbackLines,
    Gated,
    Rejected,
    gate_output,
    output_policies,
    output_reservation,
    raised_temperature,
    regeneration_cost,
)
from epistrel.orchestrator.policies.rebase import output_recheck
from epistrel.orchestrator.policies.store import guard_context

GENERATION = "generation"
INPUT_LOCATOR = "player_input"

content_log = get_logger("epistrel.lite.content")
"""DEBUG lines carrying the prompt and draft, which only content debug logging lets through."""

Gate = Callable[[TurnBudget, str, str, list[str]], Awaitable[Any]]
"""``gate(budget, text, actor_character_id, render_character_ids)``: the input gate (#56),
raising on a violation; it reserves the generation call and the first output check (#68)."""


QueueTurn = Callable[[str, dict[str, Any], str, Concurrency], Awaitable[RespondResult]]
"""``queue_turn(operation_id, request, viewpoint, concurrency)``: queue the turn (#63) and
wait for its result (or raise ``TurnAccepted``); the Engine provides it."""


@dataclass
class TurnTiming:
    """Where a turn's time went, for its stage histogram and report (#76): ``front`` is when it
    reached the front (``time.monotonic()``: a queued turn starting, else the request arriving),
    ``queued_ms`` its wait in the queue (``None``: never queued), and the seconds its read phase
    and prompt assembly, its gates (generation excluded) took."""

    front: float
    queued_ms: int | None = None
    assemble_s: float = 0.0
    policy_check_s: float = 0.0

    def first_call_offset_ms(self, calls: CallLog) -> int | None:
        if calls.first_started is None:
            return None
        return max(0, round((calls.first_started - self.front) * 1000))

    def observe(self, calls: CallLog) -> None:
        """Record the committed turn's stages (``commit`` is the ``Operation``'s own)."""
        if self.queued_ms is not None:
            stages.observe("queue_wait", self.queued_ms / 1000)
        offset = self.first_call_offset_ms(calls)
        if offset is not None:
            stages.observe("front_to_first_call", offset / 1000)
        stages.observe("assemble", self.assemble_s)
        generated = [c.latency_ms for c in calls.calls if c.purpose == GENERATION]
        stages.observe("generate", sum(generated) / 1000)
        stages.observe("policy_check", self.policy_check_s)


async def respond(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RespondRequest,
    *,
    principal: str,
    gate: Gate,
    queue_turn: QueueTurn,
) -> RespondResult:
    """Play one Lite turn; see the module docstring."""
    started = ctx.received_at if ctx.received_at is not None else time.monotonic()
    story = await read_story_cast(db, story_id)
    cast = resolve_cast(story, request)
    if story.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    resolution = partial(
        resolve_viewpoint,
        ctx.viewpoint,
        actor_character_id=cast.actor.id,
        story_default=story.settings.default_output_viewpoint,
        cast_ids=story.ids,
    )
    call = idempotency.current_call()
    if call is not None:
        await call.claim(
            {
                "story_id": story_id,
                "body": request.model_dump(mode="json", exclude={"output_viewpoint"})
                | {"render_character_ids": cast.render_ids},
                "actor_character_id": cast.actor.id,
                "viewpoint": try_resolve(resolution),
                "expected_revision": ctx.expected_revision,
            }
        )
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolution()
    # The configuration the turn runs under, captured now (#113).
    concurrency = await turn_gate.read_concurrency(db, story_id)
    # The budget is planned, and refused below its safety minimum, before the input gate (#69).
    budget = await plan_budget(
        db,
        story_id,
        cast,
        story_max_calls=concurrency.max_model_calls_per_turn,
        deployment_max_calls=None,
        turn_deadline=concurrency.turn_deadline,
        override=request.budget,
        started=started,
    )
    timing = TurnTiming(front=started)
    with recording() as calls, reporting_costs(calls, budget):
        gating = time.perf_counter()
        await gate(
            budget,
            joined_input(request.player_input.say_text, request.player_input.do_text),
            cast.actor.id,
            cast.render_ids,
        )
        timing.policy_check_s += time.perf_counter() - gating
        budget.release_to(1 + budget.reserved.get(GUARDRAIL_OUTPUT, 0))
        op = Operation(
            db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=principal,
            idempotency_key=ctx.idempotency_key,
            viewpoint=viewpoint,
        )
        # The turn gate comes last (#56's order): busy under ``reject``, queued under ``queue``.
        held = await turn_gate.acquire(db, story_id, op.operation_id, lease_s=concurrency.lease_s)
        if held is not None:
            if concurrency.mode == "reject":
                raise held.busy(story_id)
            queued = {
                "body": request.model_dump(mode="json"),
                "turn_budget": {"max_calls": budget.max_calls, "deadline_s": budget.deadline_s},
            }
            return await queue_turn(op.operation_id, queued, viewpoint, concurrency)
        op.holds_turn_slot = True
        try:
            async with turn_gate.renewing(db, story_id, op.operation_id, concurrency.lease_s):
                return await _play(
                    db,
                    models,
                    story_id,
                    request,
                    cast,
                    viewpoint,
                    op,
                    budget,
                    calls,
                    configuration=_captured(concurrency),
                    timing=timing,
                )
        finally:
            if op.status is None:  # took the slot but never began
                await turn_gate.release_now(db, story_id, op.operation_id)


def _captured(concurrency: Concurrency) -> StoryProfile:
    """The configuration ``read_concurrency`` captured at the turn's start."""
    assert concurrency.configuration is not None  # noqa: S101 - read_concurrency always sets it
    return concurrency.configuration


def polish_budget(settings: ProfileSettings) -> int:
    """How many times a reply that broke an output policy is generated again (FR-GUARD-6):
    the polish pass's ``max_regenerations``, or none with the pass off (#113)."""
    polish = settings.repair.polish
    return polish.max_regenerations if polish.enabled else 0


async def run_queued(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RespondRequest,
    *,
    operation_id: str,
    principal: str,
    concurrency: Concurrency,
    planned: dict[str, Any] | None = None,
) -> RespondResult:
    """Play a queued turn (#63): the drainer took the story's turn slot for it and runs it
    under its ``Idempotency-Key``. The request passed every precheck and the input gate when it
    was queued, and its output viewpoint was resolved then (``ctx.viewpoint``); the turn reads
    the story as the turns before it left it. ``planned``: the call cap and deadline the turn
    was planned with when it was queued (its clock starts now, when it is played)."""
    story = await read_story_cast(db, story_id)
    cast = resolve_cast(story, request)
    viewpoint = ctx.viewpoint
    if viewpoint is None:  # pragma: no cover - always resolved at enqueue
        raise RuntimeError(f"queued turn {operation_id} has no viewpoint")
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.TURN,
        principal=principal,
        idempotency_key=ctx.idempotency_key,
        viewpoint=viewpoint,
        operation_id=operation_id,
        queued=True,
    )
    op.holds_turn_slot = True
    planned = planned or {}
    front = time.monotonic()
    timing = TurnTiming(front=front, queued_ms=await queued_ms(db, operation_id))
    budget = await plan_budget(
        db,
        story_id,
        cast,
        story_max_calls=planned.get("max_calls", concurrency.max_model_calls_per_turn),
        deployment_max_calls=None,
        turn_deadline=planned.get("deadline_s") or concurrency.turn_deadline,
        override=None,
        started=front,
    )
    budget.release_to(1 + budget.reserved.get(GUARDRAIL_OUTPUT, 0))  # gated when queued
    with recording() as calls, reporting_costs(calls, budget):
        return await _play(
            db,
            models,
            story_id,
            request,
            cast,
            viewpoint,
            op,
            budget,
            calls,
            configuration=_captured(concurrency),
            timing=timing,
        )


async def regenerate(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RegenerateRequest,
    *,
    principal: str,
    gate: Gate,
) -> RegenerateResult:
    """Regenerate the head turn of ``story_id``; see the module docstring."""
    started = ctx.received_at if ctx.received_at is not None else time.monotonic()
    story = await read_story_cast(db, story_id)
    if story.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    recorded = await read_regenerable(db, story_id, request.turn)
    replay = _replayed_request(recorded, request)
    cast = resolve_cast(story, replay)
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolve_viewpoint(
        ctx.viewpoint or recorded.viewpoint,
        actor_character_id=cast.actor.id,
        story_default=story.settings.default_output_viewpoint,
        cast_ids=story.ids,
    )
    concurrency = await turn_gate.read_concurrency(db, story_id)
    budget = await plan_budget(
        db,
        story_id,
        cast,
        story_max_calls=concurrency.max_model_calls_per_turn,
        deployment_max_calls=None,
        turn_deadline=concurrency.turn_deadline,
        override=request.budget,
        started=started,
    )
    timing = TurnTiming(front=started)
    with recording() as calls, reporting_costs(calls, budget):
        gating = time.perf_counter()
        await gate(budget, joined_input(recorded.say, recorded.do), cast.actor.id, cast.render_ids)
        timing.policy_check_s += time.perf_counter() - gating
        budget.release_to(1 + budget.reserved.get(GUARDRAIL_OUTPUT, 0))
        op = Operation(
            db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=principal,
            turn=recorded.turn,
            idempotency_key=ctx.idempotency_key,
            viewpoint=viewpoint,
        )
        held = await turn_gate.acquire(db, story_id, op.operation_id, lease_s=concurrency.lease_s)
        if held is not None:
            raise held.busy(story_id)
        op.holds_turn_slot = True
        try:
            async with turn_gate.renewing(db, story_id, op.operation_id, concurrency.lease_s):
                played = await _play(
                    db,
                    models,
                    story_id,
                    replay,
                    cast,
                    viewpoint,
                    op,
                    budget,
                    calls,
                    configuration=_captured(concurrency),
                    timing=timing,
                    regenerating=recorded,
                )
        finally:
            if op.status is None:  # took the slot but never began
                await turn_gate.release_now(db, story_id, op.operation_id)
    assert isinstance(played, RegenerateResult)  # noqa: S101 - _play built it for a regenerate
    return played


def _replayed_request(recorded: RecordedTurn, request: RegenerateRequest) -> RespondRequest:
    """The respond request a regenerate replays: the recorded input, with the request's
    overrides (render set, prompt parameters) or else the recorded resolved values."""
    params = request.prompt_params
    if params is None and recorded.generation_settings is not None:
        params = GenerationSettings(
            temperature=recorded.generation_settings.get("temperature"),
            top_p=recorded.generation_settings.get("top_p"),
            length=recorded.generation_settings.get("length"),
        )
    renders = request.render_character_ids
    if renders is None and recorded.render_character_ids:
        renders = list(recorded.render_character_ids)
    return RespondRequest(
        actor_character_id=recorded.actor_character_id,
        player_input=PlayerInputBody(say=recorded.say, do=recorded.do),
        render_character_ids=renders,
        prompt_params=params,
    )


async def _play(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    request: RespondRequest,
    cast: ResolvedCast,
    viewpoint: str,
    op: Operation,
    budget: TurnBudget,
    calls: CallLog,
    *,
    configuration: StoryProfile,
    timing: TurnTiming | None = None,
    regenerating: RecordedTurn | None = None,
) -> RespondResult:
    """The turn itself: one ``turn`` operation under #47's revision protocol (steps 3 and 4 of
    the module docstring), under ``configuration`` (captured at the turn's start, #113).
    ``regenerating``: the head turn this one replaces (#66); ``timing``: where its time went so
    far (#76)."""
    timing = timing if timing is not None else TurnTiming(front=budget.started)
    say, do = request.player_input.say_text, request.player_input.do_text
    before_turn = regenerating.turn if regenerating is not None else None
    captured = configuration.configuration()

    stamped: list[Stamp] = []
    """The configuration the turn runs under (#109), stamped by its latest read phase."""

    async def begin(conn: AsyncConnection) -> tuple[TurnInputs, ResolvedGeneration]:
        reading = time.perf_counter()
        inputs = await read_turn_inputs(
            conn, story_id, before_turn=before_turn, configuration=configuration
        )
        timing.assemble_s += time.perf_counter() - reading
        resolved = resolve_generation(inputs.generation, request.prompt_params)
        # The configuration's fingerprint (FR-PROF-16), from the captured configuration and
        # the models and templates, in this snapshot.
        facts = await runtime_facts(conn)
        stamped[:] = [captured.stamp(facts)]
        # Recorded on the operation row (inserted right after this read).
        op.config_revisions = {
            **(op.config_revisions or {}),
            "generation_settings": resolved.model_dump(mode="json"),
            **stamped[0].record(captured.profile_id, captured.version, facts),
            "overrides_revision": configuration.overrides_revision,
        }
        return inputs, resolved

    gated: list[Gated] = []
    rounds: list[int] = []
    fixed: dict[str, Any] = {}
    produced: list[float] = [0.0]
    """Seconds spent producing drafts (assembly and generation) inside the output gate."""

    async def generate(op: Operation, read: tuple[TurnInputs, ResolvedGeneration]) -> None:
        inputs, resolved = read
        # The output policies as they are now; a change after this read reaches the turn
        # through #64's re-check at commit.
        context = await guard_context(db, story_id)
        policies = output_policies(context, cast.render_ids)
        rounds.append(1)
        if len(rounds) > 1:
            # A #47/#64 regeneration: its generation and re-checks come from the unreserved
            # remainder, or the turn is a revision conflict (#69).
            cost = regeneration_cost(policies)
            if budget.expired() or not budget.can_afford_unreserved(cost):
                raise RevisionConflict(
                    "the story changed under the turn and its model budget cannot pay to "
                    "generate again",
                    {
                        "current_revision": int(op.base_revision or 0),
                        "attempts": len(op.attempts),
                        "reason": "budget",
                    },
                )
            budget.hold(cost)

        async def produce(constraints: tuple[str, ...], attempt: int) -> Draft:
            producing = time.perf_counter()
            try:
                return await _produce(constraints, attempt)
            finally:
                produced[0] += time.perf_counter() - producing

        async def _produce(constraints: tuple[str, ...], attempt: int) -> Draft:
            assembling = time.perf_counter()
            payload = assemble(inputs, cast, say=say, do=do, constraints=constraints)
            settings = resolved.model_copy(
                update={"temperature": raised_temperature(resolved.temperature, attempt)}
            )
            _, _, messages = models.prepare(GENERATION, payload)
            timing.assemble_s += time.perf_counter() - assembling
            result = await _generate(models, payload, model_params(settings, cast), budget)
            # Content: the ContentFilter drops ``prompt`` and ``text`` unless operator-only
            # content debug logging is on (NFR-SEC-4; #74).
            content_log.debug(
                "generation draft",
                attempt=attempt,
                prompt=[message.content for message in messages],
                text=result.text,
            )
            return Draft(
                text=result.text,
                model=result.model,
                prompt_hash=prompt_hash(messages),
                finish_reason=result.finish_reason,
            )

        gating, before = time.perf_counter(), produced[0]
        outcome = await gate_output(
            models,
            context,
            policies,
            produce,
            budget=budget,
            retry_budget=polish_budget(configuration.settings),
            fallback=_fallback_lines(inputs, cast),
        )
        timing.policy_check_s += time.perf_counter() - gating - (produced[0] - before)
        gated[:] = [outcome]
        op.add_commit_step(write_report)
        stage_turn(
            op,
            actor_character_id=cast.actor.id,
            say=say,
            do=do,
            reply=outcome.draft.text,
            render_character_ids=cast.render_ids,
            model=outcome.draft.model,
            prompt_hash=outcome.draft.prompt_hash,
            finish_reason=outcome.draft.finish_reason,
            fallback_used=outcome.fallback_used,
            rejected=[_audit_record(r) for r in outcome.rejected],
        )
        if regenerating is not None:
            op.add_commit_step(fail_queue)

    failed_queue: list[str] = []

    async def write_report(conn: AsyncConnection) -> None:
        # ``elapsed_ms`` is fixed here, in the commit transaction, and the turn report is
        # stored on the operation (#69).
        fixed["elapsed_ms"] = budget.elapsed_ms()
        report = turn_report(
            calls,
            budget,
            gated[0] if gated else None,
            fixed["elapsed_ms"],
            queued_ms=timing.queued_ms,
            first_call_offset_ms=timing.first_call_offset_ms(calls),
        )
        await conn.execute(
            update(operations)
            .where(operations.c.operation_id == op.operation_id)
            .values(turn_report=report)
        )

    async def fail_queue(conn: AsyncConnection) -> None:
        # A regenerate is a control action: the story's queued turns fail with it (#66).
        failed_queue[:] = await turn_gate.fail_queued(
            conn,
            story_id,
            TimelineChanged(
                "a regenerate changed the timeline before the queued turn started",
                {
                    "current_revision": int(op.base_revision or 0) + 1,
                    "control_operation_id": op.operation_id,
                },
            ),
        )

    async def result(conn: AsyncConnection | AsyncEngine, committed: Operation) -> RespondResult:
        beats = await turn_beats(conn, story_id, committed.operation_id, viewpoint)
        reply = next(item for item in beats if item.beat == REPLY_BEAT)
        assert committed.narrative_turn is not None  # noqa: S101 - a committed turn's position
        assert committed.turn_variant is not None  # noqa: S101
        assert committed.timeline_revision is not None  # noqa: S101
        fields: dict[str, Any] = {
            "message": FinishedMessage(message_id=reply.message_id, text=reply.text),
            "beats": beats,
            "turn": committed.narrative_turn,
            "variant": committed.turn_variant,
            "timeline_revision": committed.timeline_revision,
            "output_viewpoint": viewpoint,
            "model_calls": list(calls.calls),
            "elapsed_ms": fixed.get("elapsed_ms", budget.elapsed_ms()),
            "call_counts": calls.by_purpose(),
            "budget": BudgetReport(**budget.report()),
            "queued_ms": timing.queued_ms,
            "first_call_offset_ms": timing.first_call_offset_ms(calls),
            **_guardrail_fields(gated[0] if gated else None),
            **_stamp_fields(stamped[0] if stamped else None),
            "profile": ProfileVersionRef(id=captured.profile_id, version=captured.version),
            "config_revision": configuration.config_revision,
        }
        if regenerating is not None:
            return RegenerateResult(**fields, previous_variant=regenerating.variant)
        return RespondResult(**fields)

    call = idempotency.current_call()
    if call is not None:
        # ``done`` and the result commit with the turn (#61).
        call.result_in_commit = result
    await run_with_revalidation(
        op,
        begin=begin,
        generate=generate,
        read_set=LiteReadSet(),
        config_recheck=output_recheck(db, models, budget),
    )
    timing.observe(calls)
    if failed_queue:
        turn_gate.wakeups.wake(story_id)
    if call is not None and call.stored_in_commit:
        stored: RespondResult = call.committed_value
        return stored
    return await result(db, op)


def _fallback_lines(inputs: TurnInputs, cast: ResolvedCast) -> FallbackLines:
    """The turn's fallback candidates: the first render character's own line (as read in the
    turn's snapshot) and the deployment's line in force at its start."""
    first = cast.render[0]
    current = next((c for c in inputs.cast if c.id == first.id), first)
    return FallbackLines(
        name=current.name,
        character_text=current.safe_fallback_text,
        deployment_text=inputs.default_safe_fallback,
    )


def _audit_record(rejected: Rejected) -> GuardrailOutputRejected:
    """A rejected text as its ``guardrail_output_rejected`` audit event."""
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
    )


def _guardrail_fields(outcome: Gated | None) -> dict[str, Any]:
    """The result's ``guardrail`` report and ``degradation`` for the released reply."""
    if outcome is None:  # pragma: no cover - a committed turn always went through the gate
        return {}
    report = GuardrailReport(
        violations=[
            GuardrailViolationItem(
                policy_id=v.policy_id, name=v.name, message=v.message, kind=v.kind.value
            )
            for v in outcome.violations
        ],
        fallback_used=outcome.fallback_used,
    )
    return {
        "guardrail": report,
        "degradation": [SAFE_FALLBACK] if outcome.fallback_used else [],
        "degradation_details": _degradation_details(outcome),
    }


def _stamp_fields(stamped: Stamp | None) -> dict[str, Any]:
    """The result's ``fingerprint``, ``capabilities`` and ``quality_status`` (#109)."""
    if stamped is None:  # pragma: no cover - a committed turn always read its configuration
        return {}
    return {
        "fingerprint": stamped.fingerprint,
        "capabilities": list(stamped.capabilities),
        "quality_status": stamped.quality_status,
    }


def _degradation_details(outcome: Gated | None) -> list[DegradationStep]:
    if outcome is None or not outcome.fallback_used:
        return []
    return [DegradationStep(step=SAFE_FALLBACK, cause=outcome.fallback_cause or "retry_budget")]


def turn_report(
    calls: CallLog,
    budget: TurnBudget,
    outcome: Gated | None,
    elapsed_ms: int,
    *,
    queued_ms: int | None = None,
    first_call_offset_ms: int | None = None,
) -> dict[str, Any]:
    """What ``operations.turn_report`` stores for a committed turn (#69): its model calls,
    their counts by purpose, the degradation steps with their causes, the budget, the elapsed
    time, and (#76) its wait in the queue and the delay to its first model call. Ids, purposes
    and counts only: never story text (invariant 3)."""
    return {
        "model_calls": [call.model_dump(mode="json") for call in calls.calls],
        "call_counts": calls.by_purpose(),
        "degradation": [d.step for d in _degradation_details(outcome)],
        "degradation_details": [
            {"step": d.step, "cause": d.cause} for d in _degradation_details(outcome)
        ],
        "budget": budget.report(),
        "elapsed_ms": elapsed_ms,
        "queued_ms": queued_ms,
        "first_call_offset_ms": first_call_offset_ms,
    }


async def queued_ms(db: AsyncEngine, operation_id: str) -> int:
    """How long the queued turn ``operation_id`` waited: from its enqueue (its ``operations``
    row's ``started_at``) to now, on the database's clock (#76)."""
    async with db.connect() as conn:
        waited = await conn.scalar(
            select(func.extract("epoch", func.clock_timestamp() - operations.c.started_at)).where(
                operations.c.operation_id == operation_id
            )
        )
    return max(0, round(float(waited or 0) * 1000))


async def plan_budget(
    db: AsyncEngine,
    story_id: str,
    cast: ResolvedCast,
    *,
    story_max_calls: int | None,
    deployment_max_calls: int | None,
    turn_deadline: float,
    override: BudgetOverride | None,
    started: float,
) -> TurnBudget:
    """The turn's budget (#69, ``budget.plan_lite_budget``): G is one call per model-assisted
    input policy for the actor's input (``say`` + ``do`` are one text) plus one per
    model-assisted output policy for the render characters' first output check. Raises 422
    ``budget_below_safety_minimum`` before any model call."""
    context = await guard_context(db, story_id)
    return plan_lite_budget(
        story_max_calls=story_max_calls,
        deployment_max_calls=deployment_max_calls,
        turn_deadline=turn_deadline,
        guardrail_input=len(model_assisted(input_policies(context, actor=cast.actor.id))),
        guardrail_output=output_reservation(context, cast.render_ids),
        override_max_calls=override.max_calls if override is not None else None,
        override_deadline_s=override.deadline_s if override is not None else None,
        started=started,
    )


@contextmanager
def reporting_costs(calls: CallLog, budget: TurnBudget) -> Iterator[None]:
    """A model failure that ends the turn carries what the turn spent: its ``model_calls``
    (failed attempts included, with their outcome) and ``elapsed_ms`` (#69)."""
    try:
        yield
    except ModelError as exc:
        exc.details.setdefault(
            "model_calls", [call.model_dump(mode="json") for call in calls.calls]
        )
        exc.details.setdefault("elapsed_ms", budget.elapsed_ms())
        raise


async def _generate(
    models: ModelAccessLayer, payload: Any, params: Any, budget: TurnBudget
) -> ModelResult:
    """The turn's ``generation`` call; an empty reply is asked once more if the budget's
    unreserved remainder can pay for it, else (or when it is empty again) 503 ``reason=empty``."""
    try:
        return await models.call(GENERATION, payload, params, budget=budget)
    except EmptyCompletion:
        if budget.expired() or not budget.can_afford_unreserved():
            raise
        budget.hold(1)
        return await models.call(GENERATION, payload, params, budget=budget)


__all__ = [
    "Gate",
    "QueueTurn",
    "TurnTiming",
    "plan_budget",
    "polish_budget",
    "queued_ms",
    "regenerate",
    "reporting_costs",
    "respond",
    "run_queued",
    "turn_report",
]
