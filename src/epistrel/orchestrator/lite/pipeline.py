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
2. **The input gate** (#56) over the actor's ``say``/``do`` joined with a newline, reserving the
   generation call in the turn's budget (``turn_deadline``, a story setting). A violation is 422
   with only the audit row written. Then, last, **the turn gate** (FR-CONC-5, #63): the turn
   takes the story's turn slot (``core.turn_gate``); while another turn holds it, a ``reject``
   story answers 423 ``story_busy`` and a ``queue`` story queues the turn (``queue_turn``, from
   the Engine), which its drainer later plays through ``run_queued``.
3. **One ``turn`` operation** under #47's revision protocol (``run_with_revalidation``): the
   read phase reads the prompt inputs in the snapshot the read set hashes (so the read set *is*
   the prompt's inputs) and resolves the generation settings, deployment → story → request
   (FR-LITE-4); the generate step makes exactly one ``generation`` call (FR-LITE-7) — an empty
   reply is asked once more, then 503 ``model_unavailable`` ``reason=empty`` — and stages the
   input as beat 1 and the reply as beat 2. A touched read set regenerates the same operation;
   an interpretive configuration change the turn depends on (a rating's guardrails, a policy,
   ``minor_age_threshold``, the default output viewpoint) re-checks the reply under the new
   configuration before it commits (``policies.rebase``, #64).
4. **The result**: the finished message (never "verified": under ``lite`` no claim is
   checked), the turn's beats for the resolved viewpoint, the new revision, and the model calls
   the turn made (``model_calls``, #54) with its ``elapsed_ms`` (fixed at the commit). Under an
   idempotent call it is built and stored ``done`` inside the turn's commit transaction, so a
   committed turn always has its result for a retry.

Scene context is validated by the request model and the cast check, then dropped (FR-RESP-4).
This module must not import ``orchestrator.policies.admin`` (import-linter): the Engine passes
the gate in as ``gate``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import idempotency, turn_gate
from epistrel.core.concurrency import LiteReadSet, check_expected_revision, run_with_revalidation
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, StoryStatus
from epistrel.core.errors import StoryArchived
from epistrel.core.events import Operation
from epistrel.core.history import turn_beats
from epistrel.core.settings_schema import ResolvedGeneration, resolve_generation
from epistrel.core.turn_gate import Concurrency
from epistrel.core.turns import (
    REPLY_BEAT,
    TurnInputs,
    joined_input,
    read_story_cast,
    read_turn_inputs,
    stage_turn,
)
from epistrel.core.viewpoint import resolve_viewpoint, try_resolve
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.cast import ResolvedCast, resolve_cast
from epistrel.orchestrator.lite.prompt import assemble, model_params, prompt_hash
from epistrel.orchestrator.lite.schemas import FinishedMessage, RespondRequest, RespondResult
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelResult
from epistrel.orchestrator.models.errors import EmptyCompletion
from epistrel.orchestrator.models.metrics import CallLog, recording
from epistrel.orchestrator.policies.rebase import output_recheck

GENERATION = "generation"
INPUT_LOCATOR = "player_input"

Gate = Callable[[TurnBudget, str, str], Awaitable[Any]]
"""``gate(budget, text, actor_character_id)``: the input gate (#56), raising on a violation."""


QueueTurn = Callable[[str, dict[str, Any], str, Concurrency], Awaitable[RespondResult]]
"""``queue_turn(operation_id, request, viewpoint, concurrency)``: queue the turn (#63) and
wait for its result (or raise ``TurnAccepted``); the Engine provides it."""


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
    started = time.monotonic()
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
    concurrency = await turn_gate.read_concurrency(db, story_id)
    budget = TurnBudget(deadline_s=concurrency.turn_deadline)
    with recording() as calls:
        await gate(
            budget,
            joined_input(request.player_input.say_text, request.player_input.do_text),
            cast.actor.id,
        )
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
            queued = {"body": request.model_dump(mode="json")}
            return await queue_turn(op.operation_id, queued, viewpoint, concurrency)
        op.holds_turn_slot = True
        try:
            async with turn_gate.renewing(db, story_id, op.operation_id, concurrency.lease_s):
                return await _play(
                    db, models, story_id, request, cast, viewpoint, op, budget, calls, started
                )
        finally:
            if op.status is None:  # took the slot but never began
                await turn_gate.release_now(db, story_id, op.operation_id)


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
) -> RespondResult:
    """Play a queued turn (#63): the drainer took the story's turn slot for it and runs it
    under its ``Idempotency-Key``. The request passed every precheck and the input gate when it
    was queued, and its output viewpoint was resolved then (``ctx.viewpoint``); the turn reads
    the story as the turns before it left it."""
    started = time.monotonic()
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
    budget = TurnBudget(deadline_s=concurrency.turn_deadline)
    with recording() as calls:
        return await _play(
            db, models, story_id, request, cast, viewpoint, op, budget, calls, started
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
    started: float,
) -> RespondResult:
    """The turn itself: one ``turn`` operation under #47's revision protocol (steps 3 and 4 of
    the module docstring)."""
    say, do = request.player_input.say_text, request.player_input.do_text

    async def begin(conn: AsyncConnection) -> tuple[TurnInputs, ResolvedGeneration]:
        inputs = await read_turn_inputs(conn, story_id)
        resolved = resolve_generation(
            inputs.deployment_generation, inputs.story_generation, request.prompt_params
        )
        # Recorded on the operation row (inserted right after this read).
        op.config_revisions = {
            **(op.config_revisions or {}),
            "generation_settings": resolved.model_dump(mode="json"),
        }
        return inputs, resolved

    async def generate(op: Operation, read: tuple[TurnInputs, ResolvedGeneration]) -> None:
        inputs, resolved = read
        payload = assemble(inputs, cast, say=say, do=do)
        params = model_params(resolved, cast)
        _, _, messages = models.prepare(GENERATION, payload)
        result = await _generate(models, payload, params, budget)
        stage_turn(
            op,
            actor_character_id=cast.actor.id,
            say=say,
            do=do,
            reply=result.text,
            render_character_ids=cast.render_ids,
            model=result.model,
            prompt_hash=prompt_hash(messages),
            finish_reason=result.finish_reason,
        )

    async def result(conn: AsyncConnection | AsyncEngine, committed: Operation) -> RespondResult:
        beats = await turn_beats(conn, story_id, committed.operation_id, viewpoint)
        reply = next(item for item in beats if item.beat == REPLY_BEAT)
        assert committed.narrative_turn is not None  # noqa: S101 - a committed turn's position
        assert committed.turn_variant is not None  # noqa: S101
        assert committed.timeline_revision is not None  # noqa: S101
        return RespondResult(
            message=FinishedMessage(message_id=reply.message_id, text=reply.text),
            beats=beats,
            turn=committed.narrative_turn,
            variant=committed.turn_variant,
            timeline_revision=committed.timeline_revision,
            output_viewpoint=viewpoint,
            model_calls=list(calls.calls),
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )

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
    if call is not None and call.stored_in_commit:
        stored: RespondResult = call.committed_value
        return stored
    return await result(db, op)


async def _generate(
    models: ModelAccessLayer, payload: Any, params: Any, budget: TurnBudget
) -> ModelResult:
    """The turn's ``generation`` call; an empty reply is asked once more (then 503)."""
    try:
        return await models.call(GENERATION, payload, params, budget=budget)
    except EmptyCompletion:
        return await models.call(GENERATION, payload, params, budget=budget)


__all__ = ["Gate", "QueueTurn", "respond", "run_queued"]
