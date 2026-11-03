"""Timeline edits over REST (FR-STORE-10,11,12,18,28; #65, #66): truncate a story, undo it,
regenerate the latest reply, and swap continuations.

- ``POST /stories/{id}/truncate`` with ``{to_turn}`` discards everything positioned after
  ``to_turn``; nothing is deleted and valid-times are untouched.
- ``POST /stories/{id}/untruncate`` (body optional: ``{truncate_operation_id}``) reverts the
  most recent truncation, by a swap when newer turns were written over it.
- ``POST /stories/{id}/regenerate`` plays the latest turn again as a new variant; the old one
  goes dormant.
- ``POST /stories/{id}/correct`` with ``{turn, player_input}`` (#598, FR-RESP-14) replaces the
  latest turn with new input the same way, in one operation; a later turn is
  ``not_latest_turn``, a corrected one ``already_corrected``.
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
from typing import Annotated, Any

from fastapi import APIRouter, Body, Response
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.respond import RequiredEffectBody, RespondResponse
from epistrel.api.routes.story_rating import CHECK_PATH, RatingCheckStartedBody
from epistrel.core.enums import Membership
from epistrel.core.fork import Forked, ForkRequest
from epistrel.core.prune import PruneImpact, PruneOutcome, PruneRequest, PruneResult, PruneRuns
from epistrel.core.ratings.change import RatingCheckStarted
from epistrel.core.timeline import (
    Swapped,
    TruncateRequest,
    Truncation,
    UntruncateRequest,
    Untruncation,
)
from epistrel.core.variants import Chain, ChainRef, SwapRequest
from epistrel.orchestrator.commit.directed import DirectedPending
from epistrel.orchestrator.lite.schemas import CorrectRequest, RegenerateRequest, RegenerateResult
from epistrel.orchestrator.rewrite.api import RewriteSession

router = APIRouter(prefix="/stories", tags=["timeline"])

CONTROL_RULES = (
    "\n\nA control action: it bumps the story's timeline revision, and a turn generating while "
    "it commits is abandoned with 409 `timeline_changed`. `Idempotency-Key` and "
    "`X-Epistrel-Expected-Revision` are optional."
)
REACTIVATION_RULES = (
    "\n\nReactivation check (FR-RATE-7): when a turn this would make active was last checked "
    "against a guardrail set that lacks a policy now in effect (a stricter rating since it was "
    "set aside, a policy added since, or no recorded set at all), the story is paused and "
    "those turns are checked first: 202 `{job_id, operation_id, status: checking}` with "
    "`Location` (the check, `GET /stories/{id}/content-rating/checks/{job_id}`). If every turn "
    "passes, the check applies this edit and ends `reactivated` with its answer as `outcome`; "
    "otherwise it ends `refused` with each violation by position and policy, and nothing "
    "changes. Turns already checked against the current set are not checked again, and when "
    "there are none to check the edit answers synchronously as described. "
    "`X-Epistrel-Expected-Revision` is checked when the request is made."
)
CHECK_STARTED: dict[int | str, dict[str, Any]] = {
    202: {
        "model": RatingCheckStartedBody,
        "description": "The turns it would make active are checked first; the story is "
        "paused (read the check at the `Location`).",
    }
}


def _check_started(
    started: RatingCheckStarted, ctx: Any, response: Response, story_id: str
) -> JSONResponse:
    """The 202 of an edit held back for a reactivation check (#120)."""
    body = answered(RatingCheckStartedBody.of(started), ctx, response, replayed=started.replayed)
    headers: dict[str, Any] = dict(response.headers)
    headers["Location"] = CHECK_PATH.format(story_id, started.job_id)
    return JSONResponse(body.model_dump(mode="json"), status_code=202, headers=headers)


def _bodies(effects: Any) -> list[RequiredEffectBody]:
    return [RequiredEffectBody.of(r) for r in effects]


class TruncatedBody(MutationResponse):
    """A committed truncation: its operation, the story's revision and next turn index after
    it, and how many operations it discarded."""

    operation_id: str
    next_turn_index: int
    to_turn: int
    discarded_operations: int
    """How many operations the truncation discarded."""
    reconciliation_required: list[RequiredEffectBody]
    """#211 (FR-EXT-14): the claimed, ``unknown`` or confirmed external effects of the
    discarded turns. They cannot be undone: each stays in the reconciliation status until
    resolved, and nothing is compensated. Their ``intended`` effects are cancelled, never sent."""

    @classmethod
    def of(cls, done: Truncation) -> TruncatedBody:
        return cls(
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
            next_turn_index=done.next_turn_index,
            to_turn=done.to_turn,
            discarded_operations=done.discarded_operations,
            reconciliation_required=_bodies(done.reconciliation_required),
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
    reconciliation_required: list[RequiredEffectBody]
    """#211: a swap's claimed, ``unknown`` or confirmed external effects of the turns it made
    dormant (see ``truncate``)."""
    cancelled_on_reactivation: list[RequiredEffectBody]
    """#211: the external effects of the turns it made active again that were cancelled while
    they were out; never re-sent, listed for reconciliation."""

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
            reconciliation_required=_bodies(done.reconciliation_required),
            cancelled_on_reactivation=_bodies(done.cancelled_on_reactivation),
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
    reconciliation_required: list[RequiredEffectBody]
    """#211: the claimed, ``unknown`` or confirmed external effects of the turns it made
    dormant (see ``truncate``)."""
    cancelled_on_reactivation: list[RequiredEffectBody]
    """#211: the external effects of the turns it made active that were cancelled while they
    were out; never re-sent, listed for reconciliation."""

    @classmethod
    def of(cls, done: Swapped) -> SwappedBody:
        return cls(
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
            next_turn_index=done.next_turn_index,
            active_chain=ChainRefBody.of(done.active_chain),
            activated_operations=done.activated_operations,
            dormant_operations=done.dormant_operations,
            reconciliation_required=_bodies(done.reconciliation_required),
            cancelled_on_reactivation=_bodies(done.cancelled_on_reactivation),
        )


class RegeneratedBody(RespondResponse):
    """The new variant of the latest turn (the respond body) and the variant it replaced."""

    previous_variant: int

    @classmethod
    def of_regenerate(cls, result: RegenerateResult) -> RegeneratedBody:
        return cls(
            **RespondResponse.of(result).model_dump(), previous_variant=result.previous_variant
        )


class DirectedPendingBody(MutationResponse):
    """#241: a directed regeneration held for its ``directed_resolution`` session (202): the
    session (its preview lists the conflicting lines and records), the variant still active,
    and any warnings (``binding_unmatched``)."""

    disposition: str = "awaiting_rewrite"
    rewrite_session: RewriteSession
    previous_variant: dict[str, int]
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, found: DirectedPending) -> DirectedPendingBody:
        return cls(
            rewrite_session=found.rewrite_session,
            previous_variant=found.previous_variant,
            warnings=found.warnings,
            timeline_revision=found.timeline_revision,
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
    requires_check: bool
    """For a chain that is not active: whether making it active (swap, untruncate, fork)
    checks its turns against the current guardrails first (one of them lacks a policy now in
    effect)."""

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
            requires_check=chain.requires_check,
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


class PendingEffectBody(BaseModel):
    """#211 (FR-EXT-13): a source effect pending at the fork's boundary: the source's outbox id,
    its status (``intended``, ``claimed`` or ``unknown``) and the effect."""

    outbox_id: str
    status: str
    effect: dict[str, Any]


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
    effects_pending_in_source: list[PendingEffectBody]
    """#211 (FR-EXT-13): the source's external effects the copied turns wrote that were still
    pending at the fork; they may still happen in the source. The fork holds only
    non-executable records of them: nothing is dispatched from the fork."""

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
            effects_pending_in_source=[
                PendingEffectBody(outbox_id=p.outbox_id, status=p.status, effect=dict(p.effect))
                for p in done.effects_pending_in_source
            ],
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
    responses=CHECK_STARTED,
    operation_id="untruncate",
    summary="Undo the most recent truncation",
    description=(
        "Reactivate exactly what the most recent truncation discarded (undo is last in, first "
        "out). Naming `truncate_operation_id` is optional; a truncation that is not the most "
        "recent one, or none at all, is 409 `nothing_to_undo` (listing "
        '`options: ["swap", "fork"]` and the `dormant_chains` when there are any). When newer '
        "turns were written after the truncation, undoing it is a swap: the original "
        "continuation becomes active and the newer one dormant, nothing is lost, and the "
        "answer says `swapped: true` with the `active_chain`." + CONTROL_RULES + REACTIVATION_RULES
    ),
)
async def untruncate(
    story_id: StoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[UntruncateRequest | None, Body()] = None,
) -> UntruncatedBody | JSONResponse:
    done = await engine.untruncate(ctx, story_id, body)
    if isinstance(done, RatingCheckStarted):
        return _check_started(done, ctx, response, story_id)
    answer = UntruncatedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)


class PruneBody(MutationResponse):
    """A pruning's two stages (#247): ``applied``, the ``impact`` report, and the ``result``
    once applied (``null`` with the report alone)."""

    applied: bool
    impact: PruneImpact
    result: PruneResult | None

    @classmethod
    def of(cls, found: PruneOutcome) -> PruneBody:
        return cls(
            applied=found.applied,
            impact=found.impact,
            result=found.result,
            timeline_revision=found.timeline_revision,
        )


@router.post(
    "/{story_id}/prune",
    response_model=PruneBody,
    operation_id="prune_story",
    summary="Prune a story's discarded tails and dormant continuations",
    description=(
        "Manual pruning (FR-STORE-19,32; #247), in two stages. Without `ignore_warnings` "
        "(`ignoreWarnings`) the answer is the `impact` report and nothing changes: the "
        "discarded tails and dormant continuations that would be deleted (`targets` names "
        "some by their ids; omitted, every eligible one), the untruncates, swaps and "
        "promotions that would stop being possible, what is retained, the targets `excluded` "
        "(`amendment_dependency`: an amendment names one of their records, `in_flight`) and a "
        "`promote_before_pruning` suggestion per dormant continuation. With it one control "
        "operation deletes the targets' records (their `operations` rows stay as markers with "
        "membership `pruned`; audit and amendment events, redacted events, outbox rows and "
        "outcomes, the erasure ledger and feedback rows, marked `pruned`, are kept), writes a "
        "`pruning` marker and moves the revision by one: a turn in flight rebases. Afterwards "
        "an untruncate or swap that needs a pruned record is 409 `pruned`. 422 "
        "`prune_target_unknown`, `prune_target_ineligible`; 409 `story_archived`, "
        "`story_paused`."
    ),
)
async def prune_story(
    story_id: StoryId, body: PruneRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> PruneBody:
    found = await engine.prune_story(ctx, story_id, body)
    return answered(PruneBody.of(found), ctx, response, replayed=found.replayed)


@router.get(
    "/{story_id}/prune-runs",
    response_model=PruneRuns,
    operation_id="list_prune_runs",
    summary="List a story's prunings",
    description=(
        "Every pruning of the story, manual (`POST /stories/{id}/prune`) or by its "
        "`prune_policy` (#248's daily run), newest first: its id, `trigger` (`manual` | "
        "`policy`), the policy it ran under, the principal, when, the revision after it and its "
        "impact report (identifiers and counts only)."
    ),
)
async def list_prune_runs(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> PruneRuns:
    return await engine.list_prune_runs(ctx, story_id)


@router.post(
    "/{story_id}/swap",
    response_model=SwappedBody,
    responses=CHECK_STARTED,
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
        "queue fail with `timeline_changed`." + CONTROL_RULES + REACTIVATION_RULES
    ),
)
async def swap(
    story_id: StoryId,
    body: SwapRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> SwappedBody | JSONResponse:
    done = await engine.swap(ctx, story_id, body)
    if isinstance(done, RatingCheckStarted):
        return _check_started(done, ctx, response, story_id)
    answer = SwappedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/regenerate",
    response_model=RegeneratedBody,
    responses={
        http_status.HTTP_202_ACCEPTED: {
            "model": DirectedPendingBody,
            "description": "A directed regeneration whose binding conflicts with recorded "
            "history, held for its `directed_resolution` session (#241).",
        }
    },
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
        "`timeline_changed`. `Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional. "
        "`directive` (#241, FR-COMMIT-12: structured stories; a Lite story is 409 `module_off`) "
        'directs the outcome ("the knocker is George"): it is checked by the input policies, '
        "and a binding of an open question is reconciled with recorded history before anything "
        "commits. Without a conflict the regeneration commits steered to the author's decision. "
        "With one, the author's choice proceeds as a `directed_resolution` amendment under "
        "`resolution_mode` and `preview`: 202 with the session, nothing committed until it is "
        "applied (the regenerated reply and the amendment then commit together)."
    ),
)
async def regenerate(
    story_id: StoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[RegenerateRequest | None, Body()] = None,
) -> RegeneratedBody | JSONResponse:
    result = await engine.regenerate(ctx, story_id, body or RegenerateRequest())
    if isinstance(result, DirectedPending):
        held = answered(DirectedPendingBody.of(result), ctx, response, replayed=result.replayed)
        return JSONResponse(
            held.model_dump(mode="json"),
            status_code=http_status.HTTP_202_ACCEPTED,
            headers=dict(response.headers),
        )
    return answered(RegeneratedBody.of_regenerate(result), ctx, response, replayed=result.replayed)


@router.post(
    "/{story_id}/correct",
    response_model=RegeneratedBody,
    operation_id="correct",
    summary="Correct the latest turn with new input",
    description=(
        "Replace the story's latest turn with new player input (FR-RESP-14), typically after an "
        "`implausible_knowledge` notice: one operation rewinds the story to before `turn` and "
        "plays `player_input` as the next variant of that turn, so a failure leaves the "
        "original turn active. The replaced turn becomes `dormant` (swap back to it with "
        "`POST /stories/{id}/swap`). `turn` must be the latest turn: once a later turn has "
        "committed it is 409 `not_latest_turn` (only history rewrite reaches it). A turn a "
        "correction wrote cannot be corrected again (409 `already_corrected`) and its notices "
        "say `correctable: false`. Both refusals write nothing. A correction does not undo an "
        "external effect the replaced turn dispatched or confirmed: it is listed in "
        "`reconciliation_required` (FR-EXT-14). The replacement is checked by the input "
        "policies; the actor is the turn's, and `render_character_ids`, `output_viewpoint` and "
        "`prompt_params` default to the recorded ones. The answer is the respond body plus "
        "`previous_variant`. While another turn of the story runs it is 423 `story_busy` (never "
        "queued); queued turns fail with `timeline_changed`. `Idempotency-Key` and "
        "`X-Epistrel-Expected-Revision` are optional."
    ),
)
async def correct(
    story_id: StoryId,
    body: CorrectRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> RegeneratedBody:
    result = await engine.correct(ctx, story_id, body)
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
        "`discarded` ones are rewound tails `untruncate` can reactivate. A chain that is not "
        "active says `requires_check: true` when making it active would first check its turns "
        "against the story's current guardrails (FR-RATE-7)."
    ),
)
async def variants(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> VariantsBody:
    return VariantsBody.of(await engine.variants(ctx, story_id))


@router.post(
    "/{story_id}/fork",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ForkedBody,
    responses={
        202: {
            "model": RatingCheckStartedBody,
            "description": "The dormant continuation's turns are checked first; the source is "
            "paused, and the check creates the fork if they pass (read it at the `Location`).",
        }
    },
    operation_id="fork",
    summary="Fork the story at a turn",
    description=(
        "Copy the story's corrected history through `at_turn` (default the head) into a new "
        "story: every active record positioned at or before it, the cast, settings, rating, "
        "engine profile (a deleted one too: the fork runs on its last version), pinned "
        "memories, custom instructions and story policies, under new ids. The copy reads "
        "one snapshot of the source, so it equals the source at exactly one timeline revision "
        "(`forked_from.revision`), and the two stories are independent from then on. "
        '`variant_chain` (`{turn, variant}` or `"turn:variant"`) copies another chain, e.g. a '
        "dormant continuation, as the fork's active history; `at_turn` must then lie on it "
        "(422 otherwise), and a chain that names no variant or continues in more than one way "
        "is 409 `unknown_chain`. `at_turn` above the head is 422. An archived story can be "
        "forked; the fork is active. Answers 201 with `Location`. The source records a `fork` "
        "event and keeps its revision. `Idempotency-Key` (per deployment, like story creation) "
        "and `X-Epistrel-Expected-Revision` (the source's) are optional.\n\nReactivation check "
        "(FR-RATE-7): when `variant_chain` names a continuation that is not active and one of "
        "the turns it would copy was last checked against a guardrail set that lacks a policy "
        "now in effect, the source is paused and those turns are checked first: 202 `{job_id, "
        "operation_id, status: checking}` with `Location` (the check). If they all pass, the "
        "check creates the fork and ends `reactivated` with this answer as its `outcome`; "
        "otherwise it ends `refused` with each violation and no fork is created."
    ),
)
async def fork(
    story_id: StoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[ForkRequest | None, Body()] = None,
) -> ForkedBody | JSONResponse:
    done = await engine.fork(ctx, story_id, body or ForkRequest())
    if isinstance(done, RatingCheckStarted):
        return _check_started(done, ctx, response, story_id)
    response.headers["Location"] = f"/stories/{done.story_id}"
    answer = ForkedBody.of(done)
    return answered(answer, ctx, response, replayed=done.replayed)
