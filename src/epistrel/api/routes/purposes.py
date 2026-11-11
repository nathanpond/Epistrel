"""Consumer purpose calls (FR-MODEL-15, FR-MODEL-16; #825, #826).

``POST /purposes/{name}/calls`` calls a consumer purpose (registered under ``/admin/purposes``,
FR-MODEL-14) with **no story** and answers its structured output synchronously, after the same
safety path as everything else (``orchestrator.purposes.call``): the input gate on every string
input, the minors policy with the true ages ``depicted`` declares, structured output against the
purpose's schema, and the output gate on every string of the result under the call's content
rating, failing closed. ``Idempotency-Key`` is honoured: a repeat answers the first result with
no model call (FR-RESP-6). A consumer route: it takes the service credential, like every route.

``POST /stories/{id}/purposes/{name}/calls`` (#826) calls one inside a story: the story supplies
the content rating, its policies and its cast's true ages, and ``viewpoint_character_id`` gives
the template one character's viewpoint (``orchestrator.purposes.story``). It reads the story and
never changes it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse, MutationResponse
from epistrel.orchestrator.purposes.call import CallModel, CallUsage, PurposeCall, PurposeResult
from epistrel.orchestrator.purposes.story import StoryPurposeCall

router = APIRouter(prefix="/purposes", tags=["purposes"])
story_router = APIRouter(prefix="/stories", tags=["purposes"])

PurposeNamePath = Annotated[
    str,
    Path(max_length=64, description="The consumer purpose's name.", examples=["vote_reason"]),
]


class PurposeCallBody(DeploymentMutationResponse):
    """A consumer purpose call's answer: the purpose and the version that answered, its
    ``output`` (valid against that version's output schema), the model it went to, and what
    it used: the purpose's own model calls and tokens, and apart, the guardrail checks.
    ``config_revision`` is the deployment configuration it was routed by."""

    purpose: str
    version: int
    output: dict[str, Any]
    model: CallModel
    usage: CallUsage

    @classmethod
    def of(cls, result: PurposeResult) -> PurposeCallBody:
        return cls(
            config_revision=result.config_revision,
            purpose=result.purpose,
            version=result.version,
            output=result.output,
            model=result.model,
            usage=result.usage,
        )


@router.post(
    "/{name}/calls",
    response_model=PurposeCallBody,
    operation_id="call_purpose",
    summary="Call a consumer purpose with no story",
    description=(
        "Render the purpose's templates with `inputs` and call its model (routed by the "
        "purpose's own model configuration, else `generation`'s), and answer the output, "
        "valid against the purpose's output schema (FR-MODEL-15). Every string in `inputs` "
        "passes the input gate under `content_rating` (default: the deployment's default "
        "rating) first: a violation is 422 `guardrail_violation` and the purpose's model is "
        "not called. `depicted` declares the people the call depicts with their true ages, so "
        "the minors policy applies when one is a minor. Every string of the output passes the "
        "output gate under the same rating: a violation is retried once, then refused with 422 "
        "`output_policy_violation`, with no part of the output. 404 for an unknown purpose or "
        "version (`version_not_found`), 410 `purpose_archived`, 422 `template_render_error` "
        "when the template does not render for `inputs`, 502 `model_output_invalid` when no "
        "answer matches the schema, 503 `model_unavailable` (`details.reason: deadline` once "
        "`deadline_ms` passes)."
    ),
)
async def call_purpose(
    name: PurposeNamePath,
    body: PurposeCall,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> PurposeCallBody:
    result = await engine.call_purpose(
        ctx,
        name,
        body.inputs,
        version=body.version,
        content_rating=body.content_rating,
        depicted=body.depicted,
        deadline_ms=body.deadline_ms,
    )
    return answered(PurposeCallBody.of(result), ctx, response, replayed=result.replayed)


class StoryPurposeCallBody(MutationResponse):
    """A story-bound consumer purpose call's answer: #825's ``{purpose, version, output, model,
    usage}``, the story's ``timeline_revision`` (as the call read it, or the revision its
    ``store_as_memory`` write left), the deployment ``config_revision`` it was routed by and the
    stored memory's id (#827)."""

    config_revision: int
    purpose: str
    version: int
    output: dict[str, Any]
    model: CallModel
    usage: CallUsage
    memory_id: str | None = Field(
        description="The memory `store_as_memory` wrote (FR-MODEL-17); null when the call "
        "stored nothing."
    )

    @classmethod
    def of(cls, result: PurposeResult) -> StoryPurposeCallBody:
        return cls(
            timeline_revision=result.timeline_revision or 0,
            config_revision=result.config_revision,
            purpose=result.purpose,
            version=result.version,
            output=result.output,
            model=result.model,
            usage=result.usage,
            memory_id=result.memory_id,
        )


@story_router.post(
    "/{story_id}/purposes/{name}/calls",
    response_model=StoryPurposeCallBody,
    operation_id="call_story_purpose",
    summary="Call a consumer purpose inside a story",
    description=(
        "Call the purpose as `POST /purposes/{name}/calls` does, inside the story "
        "(FR-MODEL-16): the story's content rating (its prompt guidance first in the system "
        "message), its effective policies and its cast's true ages gate the inputs and the "
        "output, the cast present at the latest scene counting for the minors policy. With "
        "`viewpoint_character_id`, the template's `viewpoint` is that character's viewpoint "
        "(`viewpoint.name`, and `viewpoint.text`: what it knows, believes and has seen, as "
        "its own render would show it; never what it does not know), and `story` holds `id`, "
        "`title`, `location`, `present` and `scene`; without one, `story` holds `id` and "
        "`title`. Without `store_as_memory` the call reads the story and never changes it: "
        "no lock, no revision, no event, so it runs while a turn is in flight. With "
        "`store_as_memory: {text_field}` (FR-MODEL-17; it needs `viewpoint_character_id`, 422 "
        "`viewpoint_required` otherwise) the named output field's text is also stored as the "
        "viewpoint character's memory, its alone: after the unlocked model call, an "
        "inter-turn slot write at the story's head that takes the story's turn slot (423 "
        "`story_busy` while a turn is in flight), records a `purpose_memory_stored` event, "
        "bumps the revision and answers `memory_id`; 409 `module_off` without per-character "
        "partitions or episodic memory, 422 `text_field_invalid` (`details.reason`) for a "
        "field that is no top-level string, 422 `memory_text_empty` or "
        "`memory_text_too_long`, and 409 `erased_subject_referenced` for a text naming an "
        "erased subject, each storing nothing and returning no output. Routed by the "
        "deployment's model "
        "configuration, never the story's. 404 for an unknown story, purpose, version or "
        "character; 409 `story_archived`; 422 `validation_error` with `details.reason: "
        "player_has_no_context` for the player's character; 409 `module_off` for a "
        "viewpoint on a Lite story; 409 `erased_subject_referenced` for an input naming an "
        "erased subject; then the refusals of `POST /purposes/{name}/calls`."
    ),
)
async def call_story_purpose(
    story_id: StoryId,
    name: PurposeNamePath,
    body: StoryPurposeCall,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StoryPurposeCallBody:
    result = await engine.call_story_purpose(
        ctx,
        story_id,
        name,
        body.inputs,
        version=body.version,
        viewpoint_character_id=body.viewpoint_character_id,
        deadline_ms=body.deadline_ms,
        store_as_memory=body.store_as_memory,
    )
    return answered(StoryPurposeCallBody.of(result), ctx, response, replayed=result.replayed)


__all__ = ["PurposeCallBody", "StoryPurposeCallBody", "router", "story_router"]
