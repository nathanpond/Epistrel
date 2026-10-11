"""``GET /stories/{id}/history``: the corrected history through a turn, for one output viewpoint
(FR-RESP-12; #46, #59). A thin handler over ``Engine.history``."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.core.history import DEFAULT_LIMIT, History, HistoryItem, SummaryItem, TurnSummary
from epistrel.core.variants import ChainRef

router = APIRouter(tags=["stories"])

Channel = Literal["say", "do", "think", "latent", "narration", "message", "audit"]

HISTORY_DESCRIPTION = (
    "The story's corrected history through `as_of` (default: the latest turn), produced for one "
    "**output viewpoint**: `?viewpoint=` (`character:<id>` or `omniscient`), else "
    "`?actor_character_id=`, else the story's `default_output_viewpoint`; when none resolves "
    "the read is refused (422 `viewpoint_unresolved`). `omniscient` is used only when named."
    "\n\nA character viewpoint never receives `think` or `latent` beats. Under the `lite` "
    "profile perception is `none`, so every other beat is returned in full and "
    '`audience_filter` is `"none"`: Epistrel does not decide which callers may request which '
    "viewpoint. Omniscient items carry the true ages of the characters they name.\n\n"
    "`include_audit=1` adds the turns' audit records and requires `viewpoint=omniscient` "
    "(403 `omniscient_required`). Pages hold whole turns, newest first, up to `limit` items; "
    "pass `next_before_turn` as `before_turn` for the previous page."
    "\n\n`variant=turn:variant` reads along another chain of the story (`GET "
    "/stories/{id}/variants`), e.g. a dormant continuation or a regenerated reply's earlier "
    "variant: the active history up to where it parts, then that chain's turns; `as_of` "
    "defaults to its tip. A chain that names no variant, or continues in more than one way, is "
    "409 `unknown_chain`.\n\n`include_summary=1` adds `summary`: the Lite rolling-summary "
    "segments the prompt includes now (wholly at or before `as_of`), oldest first, each "
    "`{level, covers_from, covers_to, text, operation_id}`; they are never history items."
)


class HistoryItemModel(BaseModel):
    turn: int | None
    variant: int | None
    beat: int | None
    kind: str
    actor_character_id: str | None
    render_character_ids: list[str] | None
    channel: Channel
    text: str
    operation_id: str
    message_id: str
    origin: Literal["generated", "ingested"]


class OmniscientHistoryItemModel(HistoryItemModel):
    """An item of an omniscient read: also the true age of each character it names."""

    true_ages: dict[str, int]


ItemModel = OmniscientHistoryItemModel | HistoryItemModel


def item_model(item: HistoryItem) -> ItemModel:
    if item.true_ages is not None:
        return OmniscientHistoryItemModel.model_validate(item, from_attributes=True)
    return HistoryItemModel.model_validate(item, from_attributes=True)


class TurnMessageModel(BaseModel):
    message_id: str
    text: str


class TurnSummaryModel(BaseModel):
    """A turn on the page and its reply; ``message`` is null for a reply-less turn."""

    turn: int
    variant: int | None
    message: TurnMessageModel | None

    @classmethod
    def of(cls, summary: TurnSummary) -> TurnSummaryModel:
        return cls.model_validate(summary, from_attributes=True)


class SummarySegmentModel(BaseModel):
    """A rolling-summary segment the prompt includes (#72)."""

    level: int
    covers_from: int
    covers_to: int
    text: str
    operation_id: str

    @classmethod
    def of(cls, item: SummaryItem) -> SummarySegmentModel:
        return cls.model_validate(item, from_attributes=True)


class HistoryResponse(BaseModel):
    items: list[ItemModel]
    audit: list[ItemModel] | None = None
    summary: list[SummarySegmentModel] | None = None
    turns: list[TurnSummaryModel]
    output_viewpoint: str
    audience_filter: Literal["none", "co_location", "full"]
    as_of: int
    timeline_revision: int
    next_before_turn: int | None

    @classmethod
    def of(cls, found: History) -> HistoryResponse:
        extra: dict[str, Any] = {}
        if found.audit is not None:
            extra["audit"] = [item_model(i) for i in found.audit]
        if found.summary is not None:
            extra["summary"] = [SummarySegmentModel.of(s) for s in found.summary]
        return cls(
            items=[item_model(i) for i in found.items],
            **extra,
            turns=[TurnSummaryModel.of(t) for t in found.turns],
            output_viewpoint=found.output_viewpoint,
            audience_filter=found.audience_filter,
            as_of=found.as_of,
            timeline_revision=found.timeline_revision,
            next_before_turn=found.next_before_turn,
        )


@router.get(
    "/stories/{story_id}/history",
    response_model=HistoryResponse,
    response_model_exclude_unset=True,
    summary="Read the story's history for one output viewpoint",
    description=HISTORY_DESCRIPTION,
)
async def history(
    story_id: str,
    ctx: ContextDep,
    engine: EngineDep,
    as_of: int | None = None,
    limit: int = DEFAULT_LIMIT,
    before_turn: int | None = None,
    actor_character_id: str | None = None,
    include_audit: bool = False,
    variant: str | None = None,
    include_summary: bool = False,
) -> HistoryResponse:
    found = await engine.history(
        ctx,
        story_id,
        actor_character_id=actor_character_id,
        as_of=as_of,
        limit=limit,
        before_turn=before_turn,
        include_audit=include_audit,
        variant=None if variant is None else ChainRef.parse(variant),
        include_summary=include_summary,
    )
    return HistoryResponse.of(found)
