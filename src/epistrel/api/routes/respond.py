"""``POST /stories/{id}/respond``: play one Lite turn (FR-RESP-1; #57). A thin handler over
``Engine.respond``: the actor's input in, one finished reply out."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.history import HistoryItemModel
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.models.metrics import ModelCall

router = APIRouter(tags=["stories"])

RESPOND_DESCRIPTION = (
    "Play one turn of a story. The request names the acting character, what they say and/or do "
    "(`player_input`, the turn's first beat), who answers (`render_character_ids`), the output "
    "viewpoint, prompt parameters (`length`, `temperature`, `top_p`) and optional scene context."
    "\n\nThe consumer does no retrieval or memory work: the prompt is assembled from the story's "
    "own records. Scene context is checked against the cast and then ignored: it is never "
    "stored and never placed in the model's context.\n\nThe response is the **finished** reply: "
    "under the `lite` profile it is generated in one model call and none of its claims is "
    "checked against the story's facts."
)


class MessageBody(BaseModel):
    """The finished reply: the id of its transcript row and its text."""

    message_id: str
    text: str


class RespondResponse(MutationResponse):
    """One finished turn: the reply, the turn's beats for the resolved viewpoint, its position,
    the story's new revision, and the model calls it made."""

    message: MessageBody
    beats: list[HistoryItemModel]
    turn: int
    variant: int
    output_viewpoint: str
    audience_filter: Literal["none", "co_location", "full"]
    model_calls: list[ModelCall]
    elapsed_ms: int = Field(ge=0)
    degradation: list[str]


@router.post(
    "/stories/{story_id}/respond",
    response_model=RespondResponse,
    summary="Play one turn and return the finished reply",
    description=RESPOND_DESCRIPTION,
)
async def respond(
    story_id: str, body: RespondRequest, ctx: ContextDep, engine: EngineDep
) -> RespondResponse:
    result = await engine.respond(ctx, story_id, body)
    return RespondResponse(
        message=MessageBody(message_id=result.message.message_id, text=result.message.text),
        beats=[HistoryItemModel.model_validate(b, from_attributes=True) for b in result.beats],
        turn=result.turn,
        variant=result.variant,
        timeline_revision=result.timeline_revision,
        output_viewpoint=result.output_viewpoint,
        audience_filter=result.audience_filter,
        model_calls=result.model_calls,
        elapsed_ms=result.elapsed_ms,
        degradation=result.degradation,
    )
