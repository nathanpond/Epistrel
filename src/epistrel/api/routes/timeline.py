"""Timeline edits over REST (FR-STORE-10,11,12,18,28; #65, #66): truncate a story, undo it,
regenerate the latest reply, and swap continuations.

- ``POST /stories/{id}/truncate`` with ``{to_turn}`` discards everything positioned after
  ``to_turn``; nothing is deleted and valid-times are untouched.
- ``POST /stories/{id}/untruncate`` (body optional: ``{truncate_operation_id}``) reverts the
  most recent truncation, by a swap when newer turns were written over it.
- ``POST /stories/{id}/regenerate`` plays the latest turn again as a new variant; the old one
  goes dormant.
- ``POST /stories/{id}/swap`` with ``{to_variant_chain: {turn, variant}}`` makes that chain
  active and the current continuation dormant.
- ``GET /stories/{id}/variants`` lists the story's chains.
- ``POST /stories/{id}/fork`` with ``{at_turn?, variant_chain?, name?}`` copies the corrected
  history through a turn (along a chain) into a new, independent story (#67).

The edits answer ``{operation_id, timeline_revision, next_turn_index, …}``. ``GET
/stories/{id}`` reports ``undo_available``, ``dormant_chains`` and ``variant_chains``;
``GET /stories/{id}/history?as_of=N&variant=turn:variant`` reads along a chain. Thin handlers
over ``Engine``; the rules are in ``epistrel.core.timeline`` and ``epistrel.core.variants``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.respond import RespondResponse
from epistrel.core.enums import Membership
from epistrel.core.fork import Forked, ForkRequest
from epistrel.core.timeline import (
    Swapped,
    TruncateRequest,
    Truncation,
    UntruncateRequest,
    Untruncation,
)
from epistrel.core.variants import Chain, ChainRef, SwapRequest
from epistrel.orchestrator.lite.schemas import RegenerateRequest, RegenerateResult

router = APIRouter(prefix="/stories", tags=["timeline"])

CONTROL_RULES = (
    "\n\nA control action: it bumps the story's timeline revision, and a turn generating while "
    "it commits is abandoned with 409 `timeline_changed`. `Idempotency-Key` and "
    "`X-Epistrel-Expected-Revision` are optional."
)


class TruncatedBody(MutationResponse):
    """A committed truncation: its operation, the story's revision and next turn index after
    it, and how many operations it discarded."""

    operation_id: str
    next_turn_index: int
    to_turn: int
    discarded_operations: int
    """How many operations the truncation discarded."""

    @classmethod
    def of(cls, done: Truncation) -> TruncatedBody:
        return cls(
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
            next_turn_index=done.next_turn_index,
            to_turn=done.to_turn,
            discarded_operations=done.discarded_operations,
        )


class ChainRefBody(BaseModel):
    """A chain: the ``{turn, variant}`` of its first (diverging) turn."""

    turn: int
    variant: int

    @classmethod
    def of(cls, ref: ChainRef) -> ChainRefBody:
        return cls(turn=ref.turn, variant=ref.variant)


class UntruncatedBody(MutationResponse):
    """A committed untruncation: the truncation it reverted and how many operations it
    reactivated, or, when newer turns made it a swap, the chain it activated."""

    operation_id: str
    next_turn_index: int
    truncate_operation_id: str
    restored_operations: int
    """How many operations the untruncation reactivated."""
    swapped: bool
    """``true`` when newer turns made the undo a swap: the original continuation is active and
    the newer one dormant (``dormant_operations``), nothing lost."""
    active_chain: ChainRefBody | None
    dormant_operations: int

    @classmethod
    def of(cls, done: Untruncation) -> UntruncatedBody:
        return cls(
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
            next_turn_index=done.next_turn_index,
            truncate_operation_id=done.truncate_operation_id,
            restored_operations=done.restored_operations,
            swapped=done.swapped,
            active_chain=None if done.active_chain is None else ChainRefBody.of(done.active_chain),
            dormant_operations=done.dormant_operations,
        )


class SwappedBody(MutationResponse):
    """A swap: the chain now active, how many operations it activated and made dormant, and
    the story's revision."""

    operation_id: str | None
    """``null`` when the chain was already active and nothing was written."""
    next_turn_index: int
    active_chain: ChainRefBody
    activated_operations: int
    dormant_operations: int

    @classmethod
    def of(cls, done: Swapped) -> SwappedBody:
        return cls(
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
            next_turn_index=done.next_turn_index,
            active_chain=ChainRefBody.of(done.active_chain),
            activated_operations=done.activated_operations,
            dormant_operations=done.dormant_operations,
        )


class RegeneratedBody(RespondResponse):
    """The new variant of the latest turn (the respond body) and the variant it replaced."""

    previous_variant: int

    @classmethod
    def of_regenerate(cls, result: RegenerateResult) -> RegeneratedBody:
        return cls(
            **RespondResponse.of(result).model_dump(), previous_variant=result.previous_variant
        )


class VariantChainBody(BaseModel):
    """One chain of the story: its first turn and variant, its membership (``active``,
    ``dormant``, or ``discarded`` for a rewound tail no turn was written over yet), its tip,
    how many turns it holds, when its membership last changed, and the chain it diverges from
    (``null`` for a chain starting at turn 1)."""

    turn: int
    variant: int
    membership: Membership
    tip_turn: int
    turn_count: int
    membership_tx: datetime | None
    parent: ChainRefBody | None

    @classmethod
    def of(cls, chain: Chain) -> VariantChainBody:
        return cls(
            turn=chain.turn,
            variant=chain.variant,
            membership=chain.membership,
            tip_turn=chain.tip_turn,
            turn_count=chain.turn_count,
            membership_tx=chain.membership_tx,
            parent=None if chain.parent is None else ChainRefBody.of(chain.parent),
        )


class VariantsBody(BaseModel):
    """Every chain of the story's variant tree."""

    items: list[VariantChainBody]

    @classmethod
    def of(cls, found: list[Chain]) -> VariantsBody:
        return cls(items=[VariantChainBody.of(c) for c in found])


class ForkedFromBody(BaseModel):
    """The source story, the source revision the fork equals, the last turn copied, and the
    chain followed (``null``: the active one)."""

    story_id: str
    revision: int
    at_turn: int
    variant_chain: ChainRefBody | None


class ForkCharacterBody(BaseModel):
    """A character of the fork and the source character it copies."""

    id: str
    name: str
    source_id: str


class ForkedBody(MutationResponse):
    """The new story (``timeline_revision`` is the fork's: 1), its genesis operation, where it
    came from, its cast, and how many rows were copied per table."""

    story_id: str
    operation_id: str
    next_turn_index: int
    name: str | None
    forked_from: ForkedFromBody
    characters: list[ForkCharacterBody]
    counts: dict[str, int]

    @classmethod
    def of(cls, done: Forked) -> ForkedBody:
        origin = done.forked_from
        return cls(
            story_id=done.story_id,
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
            next_turn_index=done.next_turn_index,
            name=done.name,
            forked_from=ForkedFromBody(
                story_id=origin.story_id,
                revision=origin.revision,
                at_turn=origin.at_turn,
                variant_chain=(
                    None if origin.variant_chain is None else ChainRefBody.of(origin.variant_chain)
                ),
            ),
            characters=[
                ForkCharacterBody(id=c.id, name=c.name, source_id=c.source_id)
                for c in done.characters
            ],
            counts=dict(done.counts),
        )


@router.post(
    "/{story_id}/truncate",
    response_model=TruncatedBody,
    operation_id="truncate",
    summary="Truncate the story to a turn",
    description=(
        "Delete a message and everything after it, without destroying anything: every "
        "operation positioned after `to_turn` becomes `discarded` (stamped with this "
        "transaction's time); no event is deleted and no valid-time changes, and "
        "`untruncate` brings them back. `to_turn` runs from 0 (keep only the story's setup) to "
        "the head - 1; anything else is 422. Turns waiting in the story's queue fail with "
        "`timeline_changed`." + CONTROL_RULES
    ),
)
async def truncate(
    story_id: StoryId,
    body: TruncateRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> TruncatedBody:
    done = await engine.truncate(ctx, story_id, body)
    answer = TruncatedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/untruncate",
    response_model=UntruncatedBody,
    operation_id="untruncate",
    summary="Undo the most recent truncation",
    description=(
        "Reactivate exactly what the most recent truncation discarded (undo is last in, first "
        "out). Naming `truncate_operation_id` is optional; a truncation that is not the most "
        "recent one, or none at all, is 409 `nothing_to_undo` (listing "
        '`options: ["swap", "fork"]` and the `dormant_chains` when there are any). When newer '
        "turns were written after the truncation, undoing it is a swap: the original "
        "continuation becomes active and the newer one dormant, nothing is lost, and the "
        "answer says `swapped: true` with the `active_chain`." + CONTROL_RULES
    ),
)
async def untruncate(
    story_id: StoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[UntruncateRequest | None, Body()] = None,
) -> UntruncatedBody:
    done = await engine.untruncate(ctx, story_id, body)
    answer = UntruncatedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/swap",
    response_model=SwappedBody,
    operation_id="swap",
    summary="Make another continuation the active one",
    description=(
        "Make the chain `to_variant_chain` (`{turn, variant}` of its first turn, as "
        "`GET /stories/{id}/variants` lists it) active: everything active after the turn where "
        "it parts from the active chain becomes `dormant`, and the chain's turns (and the work "
        "positioned on them) become `active`. Nothing is deleted; swapping back restores the "
        "exact prior state. Swapping to the active chain writes nothing (200, `operation_id` "
        "null). A chain that names no variant, or continues in more than one way, is 409 "
        "`unknown_chain` (with the `candidates` to choose from). Turns waiting in the story's "
        "queue fail with `timeline_changed`." + CONTROL_RULES
    ),
)
async def swap(
    story_id: StoryId,
    body: SwapRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> SwappedBody:
    done = await engine.swap(ctx, story_id, body)
    answer = SwappedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/regenerate",
    response_model=RegeneratedBody,
    operation_id="regenerate",
    summary="Regenerate the latest reply",
    description=(
        "Play the latest turn again: its recorded input (never re-sent by you, and checked by "
        "the input policies again) is answered anew as the next variant of the same turn, and "
        "the old variant becomes `dormant` (swap back to it with `POST /stories/{id}/swap`). "
        "`turn`, when given, must be the latest turn (422). `render_character_ids`, "
        "`output_viewpoint` and `prompt_params` override the values the turn was played with; "
        "omitted, the recorded ones are reused. A latest turn without a reply (an ingested "
        "player turn, a narration turn) is 422 `nothing_to_regenerate`. The answer is the "
        "respond body plus `previous_variant`. While another turn of the story runs the "
        "regenerate is 423 `story_busy` (it is never queued); queued turns fail with "
        "`timeline_changed`. `Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional."
    ),
)
async def regenerate(
    story_id: StoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[RegenerateRequest | None, Body()] = None,
) -> RegeneratedBody:
    result = await engine.regenerate(ctx, story_id, body or RegenerateRequest())
    return answered(RegeneratedBody.of_regenerate(result), ctx, response, replayed=result.replayed)


@router.get(
    "/{story_id}/variants",
    response_model=VariantsBody,
    operation_id="variants",
    summary="List the story's variant chains",
    description=(
        "Every chain of the story's variant tree: each turn variant continues the active "
        "variant of the turn before it, and a chain is a run of variants continuing one "
        "another with the same membership, named by the `{turn, variant}` of its first turn. "
        "The `active` chains make up the story; `dormant` ones are continuations a swap can "
        "bring back (a regenerated reply, a rewound tail newer turns were written over); "
        "`discarded` ones are rewound tails `untruncate` can reactivate."
    ),
)
async def variants(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> VariantsBody:
    return VariantsBody.of(await engine.variants(ctx, story_id))


@router.post(
    "/{story_id}/fork",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ForkedBody,
    operation_id="fork",
    summary="Fork the story at a turn",
    description=(
        "Copy the story's corrected history through `at_turn` (default the head) into a new "
        "story: every active record positioned at or before it, the cast, settings, rating, "
        "pinned memories, custom instructions and story policies, under new ids. The copy reads "
        "one snapshot of the source, so it equals the source at exactly one timeline revision "
        "(`forked_from.revision`), and the two stories are independent from then on. "
        '`variant_chain` (`{turn, variant}` or `"turn:variant"`) copies another chain, e.g. a '
        "dormant continuation, as the fork's active history; `at_turn` must then lie on it "
        "(422 otherwise), and a chain that names no variant or continues in more than one way "
        "is 409 `unknown_chain`. `at_turn` above the head is 422. An archived story can be "
        "forked; the fork is active. Answers 201 with `Location`. The source records a `fork` "
        "event and keeps its revision. `Idempotency-Key` (per deployment, like story creation) "
        "and `X-Epistrel-Expected-Revision` (the source's) are optional."
    ),
)
async def fork(
    story_id: StoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[ForkRequest | None, Body()] = None,
) -> ForkedBody:
    done = await engine.fork(ctx, story_id, body or ForkRequest())
    response.headers["Location"] = f"/stories/{done.story_id}"
    answer = ForkedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)
