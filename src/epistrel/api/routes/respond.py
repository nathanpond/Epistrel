"""``POST /stories/{id}/respond``: play one Lite turn (FR-RESP-1; #57). A thin handler over
``Engine.respond``: the actor's input in, one finished reply out."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.history import ItemModel, item_model
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from epistrel.orchestrator.models.metrics import ModelCall

router = APIRouter(tags=["stories"])

RESPOND_DESCRIPTION = (
    "Play one turn of a story. The request names the acting character (any character of the "
    "story; who acts as whom is your decision), what they say and/or do (`player_input`, the "
    "turn's first beat and only ever the actor's own words), who answers "
    "(`render_character_ids`: default every other character, voiced in the order given and "
    "composed into one message), the output "
    "viewpoint (`character:<id>` or `omniscient`; default the actor, then the story's default; "
    "echoed as `output_viewpoint`, and `beats` are produced for it), prompt parameters "
    "(`length`, `temperature`, `top_p`) and optional scene context."
    "\n\nThe consumer does no retrieval or memory work: the prompt is assembled from the story's "
    "own records. Scene context is checked against the cast and then ignored: it is never "
    "stored and never placed in the model's context.\n\nThe response is the **finished** reply: "
    "under the `lite` profile it is generated in one model call and none of its claims is "
    "checked against the story's facts. Several characters are voiced in one generation, and "
    "their knowledge is kept apart only by instructions in that shared prompt."
    "\n\nOptional headers: `Idempotency-Key` (a retry returns the identical response) and "
    "`X-Epistrel-Expected-Revision` (a stale revision is refused with 409 and nothing written)."
)


class MessageBody(BaseModel):
    """The finished reply: the id of its transcript row and its text."""

    message_id: str
    text: str


class RespondResponse(MutationResponse):
    """One finished turn: the reply, the turn's beats for the resolved viewpoint, its position,
    the story's new revision, and the model calls it made."""

    message: MessageBody
    beats: list[ItemModel]
    turn: int
    variant: int
    output_viewpoint: str
    audience_filter: Literal["none", "co_location", "full"]
    model_calls: list[ModelCall]
    elapsed_ms: int = Field(ge=0)
    degradation: list[str]

    @classmethod
    def of(cls, result: RespondResult) -> RespondResponse:
        return cls(
            message=MessageBody(message_id=result.message.message_id, text=result.message.text),
            beats=[item_model(b) for b in result.beats],
            turn=result.turn,
            variant=result.variant,
            timeline_revision=result.timeline_revision,
            output_viewpoint=result.output_viewpoint,
            audience_filter=result.audience_filter,
            model_calls=result.model_calls,
            elapsed_ms=result.elapsed_ms,
            degradation=result.degradation,
        )


@router.post(
    "/stories/{story_id}/respond",
    response_model=RespondResponse,
    summary="Play one turn and return the finished reply",
    description=RESPOND_DESCRIPTION,
)
async def respond(
    story_id: str, body: RespondRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> RespondResponse:
    result = await engine.respond(ctx, story_id, body)
    return answered(RespondResponse.of(result), ctx, response, replayed=result.replayed)
