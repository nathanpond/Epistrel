"""``GET /stories/{id}/history``: the corrected history through a turn, for one output viewpoint
(FR-RESP-12; #46, #59). A thin handler over ``Engine.history``."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.core.history import DEFAULT_LIMIT, History, HistoryItem, SummaryItem, TurnSummary
from epistrel.core.variants import ChainRef

router = APIRouter(tags=["stories"])

Channel = Literal["say", "do", "think", "latent", "narration", "message", "audit"]
AudienceFilterValue = Literal[
    "none", "co_location", "full", "nothing_perceived", "bypassed_by_player_view"
]
"""What a result states about its audience filtering (FR-RESP-13; #177)."""

HISTORY_DESCRIPTION = (
    "The story's corrected history through `as_of` (default: the latest turn), produced for one "
    "**output viewpoint**: `?viewpoint=` (`character:<id>` or `omniscient`), else "
    "`?actor_character_id=`, else the story's `default_output_viewpoint`; when none resolves "
    "the read is refused (422 `viewpoint_unresolved`). `omniscient` is used only when named."
    "\n\nA character viewpoint never receives `think` or `latent` beats. Under the `lite` "
    "profile perception is `none`, so every other beat is returned in full and "
    '`audience_filter` is `"none"`: Epistrel does not decide which callers may request which '
    "viewpoint. Omniscient items carry the true ages of the characters they name."
    "\n\nWhere the story's perception is `co_location` or `full` (FR-RESP-13), a character "
    "read is filtered beat by beat by the same perception routing as the character's memory, "
    "from authoritative positions: a beat the character perceived is returned in full, one it "
    "perceived only the envelope of is returned as that envelope (`fidelity: envelope`, "
    '"Jenni whispers something to Marcus"), and any other is omitted; each turn\'s message '
    "is "
    "recomposed over what is returned, and `audience_filter` names the level. A character "
    "read's structured beats carry no verdicts, `detected` claims or `effects`. `player_view` "
    "(`character` | `story`; default the story's setting) `story` returns every beat in full "
    "instead, each with the `fidelity` the character would have perceived it at "
    '(`audience_filter: "bypassed_by_player_view"`); it is echoed as `player_view`.\n\n'
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


class BeatPropositionModel(BaseModel):
    """A proposition a structured beat asserts, with its verdict (#150)."""

    proposition: dict[str, Any] | None
    verdict: str | None


class UtteranceLabelsModel(BaseModel):
    """An utterance's three independent labels (FR-TURN-6; #150) and, once the verifier
    classified it under per-character partitions (#166), its classification."""

    objective_relation: Literal["true", "false", "unresolved"]
    belief_relation: Literal["consistent", "inconsistent", "unknown"]
    intent: Literal["sincere", "deceptive", "joking", "unspecified"]
    classification: (
        Literal["sincere", "sincere_error", "lie", "joke", "hallucination_repaired"] | None
    ) = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="#166: the verifier's classification of the utterance (FR-VERIFY-5), its "
        "worst claim's. A classified utterance's labels are returned on `omniscient` reads "
        "only: its belief relation is the speaker's private state.",
    )
    intent_corrected: bool = Field(
        default=False,
        exclude_if=lambda v: not v,
        description="#166: a declared `deceptive` the speaker's beliefs support, relabelled "
        "`sincere` (`intent` is then `sincere`).",
    )


class BeatVisibilityModel(BaseModel):
    """A ``latent`` beat's scope (FR-TURN-4; #150)."""

    scope: Literal["objective", "public", "characters"]
    characters: list[str] = Field(default_factory=list)


class DetectedClaimModel(BaseModel):
    """A claim detection found in a beat beyond what it declared (#148, #150)."""

    start: int
    end: int
    category: str
    verdict: str | None = None


class HistoryItemModel(BaseModel):
    """One history item: a beat, a narration, a reply message or an audit record, with its
    position (turn, variant, beat), channel, text and the operation that recorded it."""

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
    redacted: bool = Field(
        default=False,
        exclude_if=lambda redacted: not redacted,
        description="Present (`true`) when an erasure rewrote the item in place: each span "
        "that named the erased subject reads `[erased]` (#124).",
    )
    quarantined: bool = Field(
        default=False,
        exclude_if=lambda quarantined: not quarantined,
        description="Omniscient reads only: present (`true`) when the item's event is a "
        "source of a quarantined memory (#145). A quarantine keeps the item in history; it "
        "only keeps the memory out of retrieval and every generation context.",
    )
    propositions: list[BeatPropositionModel] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A structured turn's beat (#150): each proposition it asserts (the "
        "normalized proposition) with its verdict (`supported`, `contradicted`, `novel`, "
        "`uncertain`, `denied`; `null` with verification off). Absent from a Lite item.",
    )
    effects: list[dict[str, Any]] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A structured turn's beat (#150): its state effects (empty in M4).",
    )
    labels: UtteranceLabelsModel | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A `say` of a structured turn (#150): the utterance's three independent "
        "labels (FR-TURN-6).",
    )
    visibility: BeatVisibilityModel | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A `latent` beat (#150; omniscient reads only): its declared scope.",
    )
    detected: list[DetectedClaimModel] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A structured turn's beat (#150): claims detection found in it that it "
        "did not declare, as spans, categories and verdicts.",
    )
    narrated_by: str | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A reactive character's reaction (#176, FR-MULTI-9): the id of the "
        "character whose render narrated it (the item's actor is the reactor).",
    )
    fidelity: Literal["content", "envelope", "none"] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A structured turn's beat (#177, FR-RESP-13): the fidelity the viewing "
        "character perceived it at: `content`, or `envelope` (the text is then the envelope, "
        "with no propositions or labels); under `player_view = story` also `none` (the beat "
        "in full, unperceived). Always `content` for `omniscient` and perception `none`.",
    )


class OmniscientHistoryItemModel(HistoryItemModel):
    """An item of an omniscient read: also the true age of each character it names."""

    true_ages: dict[str, int]


ItemModel = OmniscientHistoryItemModel | HistoryItemModel


def item_model(item: HistoryItem) -> ItemModel:
    if item.true_ages is not None:
        return OmniscientHistoryItemModel.model_validate(item, from_attributes=True)
    return HistoryItemModel.model_validate(item, from_attributes=True)


class TurnMessageModel(BaseModel):
    """A turn's reply: its transcript row id and text."""

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
    """A page of the story's corrected history for one output viewpoint, with the turns on the
    page, the revision it was read at and the cursor to the previous page."""

    items: list[ItemModel]
    audit: list[ItemModel] | None = None
    summary: list[SummarySegmentModel] | None = None
    turns: list[TurnSummaryModel]
    output_viewpoint: str
    audience_filter: AudienceFilterValue
    player_view: Literal["character", "story"]
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
            player_view=found.player_view,
            as_of=found.as_of,
            timeline_revision=found.timeline_revision,
            next_before_turn=found.next_before_turn,
        )


@router.get(
    "/stories/{story_id}/history",
    response_model=HistoryResponse,
    response_model_exclude_unset=True,
    operation_id="history",
    summary="Read the story's history for one output viewpoint",
    description=HISTORY_DESCRIPTION,
)
async def history(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    as_of: int | None = None,
    limit: int = DEFAULT_LIMIT,
    before_turn: int | None = None,
    actor_character_id: str | None = None,
    include_audit: bool = False,
    variant: str | None = None,
    include_summary: bool = False,
    player_view: Literal["character", "story"] | None = None,
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
        player_view=player_view,
    )
    return HistoryResponse.of(found)
