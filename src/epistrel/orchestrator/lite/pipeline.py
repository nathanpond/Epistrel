"""The Lite turn: assemble → generate → record (FR-RESP-1..4,9, FR-LITE-1,4,5,7, FR-PROF-11; #57).

``respond`` runs one Lite turn of a story for ``Engine.respond``:

1. **Prechecks**, in #44's canonical order, before anything is written: the story exists and
   the request's characters are its cast (422 ``validation_error`` with ``unknown_actor`` /
   ``unknown_render_ids`` / ``unknown_present_ids``), the story is not archived, an
   ``Idempotency-Key`` already used is not reused for another viewpoint (409
   ``idempotency_conflict``, #59's interim lookup until #61), the expected revision holds (409),
   and the output viewpoint resolves (422 ``viewpoint_unresolved``). The requested viewpoint
   arrives on ``ctx.viewpoint``, the single carrier (``Engine.respond`` folds the body field and
   the keyword argument into it).
2. **The input gate** (#56) over the actor's ``say``/``do`` joined with a newline, reserving the
   generation call in the turn's budget. A violation is 422 with only the audit row written.
3. **One ``turn`` operation** under #47's revision protocol (``run_with_revalidation``): the
   read phase reads the prompt inputs in the snapshot the read set hashes (so the read set *is*
   the prompt's inputs) and resolves the generation settings, deployment → story → request
   (FR-LITE-4); the generate step makes exactly one ``generation`` call (FR-LITE-7) — an empty
   reply is asked once more, then 503 ``model_unavailable`` ``reason=empty`` — and stages the
   input as beat 1 and the reply as beat 2. A touched read set regenerates the same operation.
4. **The result**: the finished message (never "verified": under ``lite`` no claim is
   checked), the turn's beats for the resolved viewpoint, the new revision, and the model calls
   the turn made (``model_calls``, #54) with its ``elapsed_ms``.

Scene context is validated by the request model and the cast check, then dropped (FR-RESP-4).
This module must not import ``orchestrator.policies.admin`` (import-linter): the Engine passes
the gate in as ``gate``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import LiteReadSet, check_expected_revision, run_with_revalidation
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, StoryStatus
from epistrel.core.errors import InvalidRequest, StoryArchived
from epistrel.core.events import Operation
from epistrel.core.history import turn_beats
from epistrel.core.settings_schema import ResolvedGeneration, resolve_generation
from epistrel.core.turns import (
    REPLY_BEAT,
    CastEntry,
    StoryCast,
    TurnInputs,
    joined_input,
    read_story_cast,
    read_turn_inputs,
    stage_turn,
)
from epistrel.core.viewpoint import check_key_viewpoint, resolve_viewpoint, try_resolve
from epistrel.orchestrator.budget import DEFAULT_TURN_DEADLINE_S, TurnBudget
from epistrel.orchestrator.lite.prompt import assemble, model_params, prompt_hash
from epistrel.orchestrator.lite.schemas import FinishedMessage, RespondRequest, RespondResult
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelResult
from epistrel.orchestrator.models.errors import EmptyCompletion
from epistrel.orchestrator.models.metrics import recording

GENERATION = "generation"
INPUT_LOCATOR = "player_input"

Gate = Callable[[TurnBudget, str, str], Awaitable[Any]]
"""``gate(budget, text, actor_character_id)``: the input gate (#56), raising on a violation."""


@dataclass(frozen=True, slots=True)
class ResolvedCast:
    """The actor and the render characters, in the order they are voiced."""

    actor: CastEntry
    render: tuple[CastEntry, ...]


def resolve_cast(story: StoryCast, request: RespondRequest) -> ResolvedCast:
    """The request's characters against the cast (FR-AUTH-3 basics; #62 extends the rules).

    The actor must be a cast member; ``render_character_ids`` default to every other cast
    member in cast order, explicit ones keep their order with duplicates dropped; scene
    context's ``characters_present`` must be cast members too. 422 otherwise."""
    members = story.by_id()
    problems: dict[str, Any] = {}
    actor = members.get(request.actor_character_id)
    if actor is None:
        problems["unknown_actor"] = request.actor_character_id
    if request.render_character_ids is None:
        ids = [c.id for c in story.cast if c.id != request.actor_character_id]
    else:
        ids = list(dict.fromkeys(request.render_character_ids))
        unknown = [i for i in ids if i not in members]
        if unknown:
            problems["unknown_render_ids"] = unknown
    if request.scene_context is not None:
        absent = [i for i in request.scene_context.characters_present if i not in members]
        if absent:
            problems["unknown_present_ids"] = absent
    if problems or actor is None:
        raise InvalidRequest("the request names characters that are not in the story", problems)
    return ResolvedCast(actor=actor, render=tuple(members[i] for i in ids))


async def respond(
    db: AsyncEngine,
    models: ModelAccessLayer,
    ctx: RequestContext,
    story_id: str,
    request: RespondRequest,
    *,
    principal: str,
    gate: Gate,
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
    await check_key_viewpoint(db, story_id, ctx.idempotency_key, try_resolve(resolution))
    await check_expected_revision(db, story_id, ctx.expected_revision)
    viewpoint = resolution()
    say, do = request.player_input.say_text, request.player_input.do_text
    budget = TurnBudget(deadline_s=DEFAULT_TURN_DEADLINE_S)
    with recording() as calls:
        await gate(budget, joined_input(say, do), cast.actor.id)
        op = Operation(
            db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=principal,
            idempotency_key=ctx.idempotency_key,
            viewpoint=viewpoint,
        )

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
            payload = assemble(inputs, actor=cast.actor, render=cast.render, say=say, do=do)
            params = model_params(resolved, actor=cast.actor, render=cast.render)
            _, _, messages = models.prepare(GENERATION, payload)
            result = await _generate(models, payload, params, budget)
            stage_turn(
                op,
                actor_character_id=cast.actor.id,
                say=say,
                do=do,
                reply=result.text,
                render_character_ids=[c.id for c in cast.render],
                model=result.model,
                prompt_hash=prompt_hash(messages),
                finish_reason=result.finish_reason,
            )

        await run_with_revalidation(op, begin=begin, generate=generate, read_set=LiteReadSet())
    beats = await turn_beats(db, story_id, op.operation_id, viewpoint)
    reply = next(item for item in beats if item.beat == REPLY_BEAT)
    assert op.narrative_turn is not None  # noqa: S101 - a committed turn has a position
    assert op.turn_variant is not None  # noqa: S101
    assert op.timeline_revision is not None  # noqa: S101
    return RespondResult(
        message=FinishedMessage(message_id=reply.message_id, text=reply.text),
        beats=beats,
        turn=op.narrative_turn,
        variant=op.turn_variant,
        timeline_revision=op.timeline_revision,
        output_viewpoint=viewpoint,
        model_calls=list(calls.calls),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )


async def _generate(
    models: ModelAccessLayer, payload: Any, params: Any, budget: TurnBudget
) -> ModelResult:
    """The turn's ``generation`` call; an empty reply is asked once more (then 503)."""
    try:
        return await models.call(GENERATION, payload, params, budget=budget)
    except EmptyCompletion:
        return await models.call(GENERATION, payload, params, budget=budget)


__all__ = ["Gate", "ResolvedCast", "resolve_cast", "respond"]
