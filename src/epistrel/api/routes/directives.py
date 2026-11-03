"""A story's directives (FR-DIRECT-1,2,6; docs/02 §9; #226).

``POST /stories/{id}/directives``: an author's direction, ephemeral or durable; one ``Engine``
call (``orchestrator.directives`` holds the rules).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.orchestrator.directives import DirectiveApplied, DirectiveRequest, DirectiveResult
from epistrel.orchestrator.rewrite.api import RewriteSession

router = APIRouter(prefix="/stories", tags=["stories"])


class DirectiveBody(MutationResponse):
    """A directive's answer: its id, classification and disposition (``steer_queued``,
    ``applied`` or ``routed_to_rewrite``), the slot it applied at and what it applied, or the
    rewrite session it routed to."""

    directive_id: str
    classification: str
    disposition: str
    position: str | None = Field(default=None, exclude_if=lambda v: v is None)
    applied: DirectiveApplied | None = Field(default=None, exclude_if=lambda v: v is None)
    rewrite_session: RewriteSession | None = Field(default=None, exclude_if=lambda v: v is None)
    steer_text: str | None = Field(default=None, exclude_if=lambda v: v is None)

    @classmethod
    def of(cls, found: DirectiveResult) -> DirectiveBody:
        return cls(
            directive_id=found.directive_id,
            classification=found.classification,
            disposition=found.disposition,
            position=found.position,
            applied=found.applied,
            rewrite_session=found.rewrite_session,
            steer_text=found.steer_text,
            timeline_revision=found.timeline_revision,
        )


@router.post(
    "/{story_id}/directives",
    response_model=DirectiveBody,
    responses={202: {"model": DirectiveBody, "description": "Routed to a rewrite session."}},
    operation_id="apply_directive",
    summary="Direct the story as its author",
    description=(
        "An author's direction (FR-DIRECT-1,2): **ephemeral** guidance for the next reply "
        '("make this scene tense": `steer_queued`, carried by the next turn\'s renders and '
        "consumed by its commit; nothing else is written) or a **durable** change to the world "
        '("it starts raining", "the next morning, 08:00", "Bobby believes he graduated '
        "from Florida\"), applied at the next inter-turn slot with the author's authority: "
        "facts, State sets, a clock advance and beliefs (`applied`, its `position` `N⁺k`; the "
        "revision moves by one). A change to the past is routed into a rewrite session of kind "
        "`directive` under `resolution_mode` and `preview` (202, `routed_to_rewrite`). `kind` "
        "overrides the classification. Authority comes from this route, never from text "
        "claiming it (FR-DIRECT-6). 422 `guardrail_violation` (nothing written), "
        "`directive_unparseable`, `directive_no_changes`, `state_constraint_violation`, "
        "`clock_backward`; 409 `story_paused`, `module_off` (a Lite story), `revision_conflict`; "
        "423 `story_busy`. An omniscient call: 403 `omniscient_required` for a character."
    ),
)
async def apply_directive(
    story_id: StoryId,
    body: DirectiveRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> DirectiveBody | JSONResponse:
    found = await engine.apply_directive(ctx, story_id, body, viewpoint=viewpoint)
    shown = answered(DirectiveBody.of(found), ctx, response, replayed=found.replayed)
    if found.disposition != "routed_to_rewrite":
        return shown
    content: Any = shown.model_dump(mode="json")
    return JSONResponse(content, status_code=202, headers=dict(response.headers))


__all__ = ["router"]
