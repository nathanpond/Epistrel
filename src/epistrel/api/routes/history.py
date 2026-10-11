"""``GET /stories/{id}/history``: the story's active transcript (interim; #59 extends it)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep

router = APIRouter(tags=["stories"])


class HistoryItemModel(BaseModel):
    turn: int | None
    variant: int | None
    beat: int | None
    kind: str
    actor_character_id: str | None
    render_character_ids: list[str] | None
    channel: str
    text: str
    operation_id: str
    message_id: str


class HistoryResponse(BaseModel):
    items: list[HistoryItemModel]
    timeline_revision: int


@router.get("/stories/{story_id}/history", response_model=HistoryResponse)
async def history(story_id: str, ctx: ContextDep, engine: EngineDep) -> HistoryResponse:
    found = await engine.history(ctx, story_id)
    return HistoryResponse(
        items=[HistoryItemModel.model_validate(item, from_attributes=True) for item in found.items],
        timeline_revision=found.timeline_revision,
    )
