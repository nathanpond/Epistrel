"""Engine-proposed States (FR-STATE-3; #192).

- ``POST /stories/{id}/state-proposals/analyze``: ask the engine what the story has been
  counting (202: a background analysis of the whole history).
- ``GET /stories/{id}/state-proposals``: the proposals.
- ``POST /stories/{id}/state-proposals/{pid}/approve``: declare the proposed State.
- ``POST /stories/{id}/state-proposals/{pid}/dismiss``: dismiss it for good.

Every handler makes one ``Engine`` call (``core.states.proposals`` holds the rules).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.states.proposals import (
    AnalysisQueued,
    ProposalApproved,
    ProposalDismissed,
    ProposalList,
    ProposalView,
    StateProposalApproveRequest,
)

router = APIRouter(prefix="/stories", tags=["stories"])

ProposalId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The State proposal's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2D5"],
    ),
]

_GATE = " 409 `module_off` while the story's configuration has States off."


class EvidenceBody(BaseModel):
    """Where the proposed value recurs: the beat's turn and event, the span in its text
    (null for a proposition the beat declared) and the text it quotes."""

    turn: int
    event_id: str
    span: list[int] | None
    text: str


class StateProposalBody(BaseModel):
    """One proposal: the State it suggests (name, type, scope, unit or entity), its evidence,
    its status (`proposed`, `approved`, `dismissed`), the analysis request that proposed it
    and, once approved, the definition it created."""

    id: str
    name: str
    suggested_type: str
    suggested_scope: str
    suggested_unit: str | None
    suggested_entity_id: str | None
    evidence: list[EvidenceBody]
    status: str
    created_by_operation_id: str
    created_at: datetime
    decided_at: datetime | None
    definition_id: str | None

    @classmethod
    def of(cls, view: ProposalView) -> StateProposalBody:
        return cls(
            id=view.id,
            name=view.name,
            suggested_type=view.suggested_type,
            suggested_scope=view.suggested_scope,
            suggested_unit=view.suggested_unit,
            suggested_entity_id=view.suggested_entity_id,
            evidence=[EvidenceBody.model_validate(e) for e in view.evidence],
            status=view.status,
            created_by_operation_id=view.created_by_operation_id,
            created_at=view.created_at,
            decided_at=view.decided_at,
            definition_id=view.definition_id,
        )


class StateProposalsBody(BaseModel):
    """The story's proposals, oldest first."""

    proposals: list[StateProposalBody]

    @classmethod
    def of(cls, found: ProposalList) -> StateProposalsBody:
        return cls(proposals=[StateProposalBody.of(p) for p in found.proposals])


class AnalysisQueuedBody(MutationResponse):
    """The analysis handed to a worker (202): its job and the request's operation (each
    proposal it writes names it)."""

    job_id: str
    operation_id: str

    @classmethod
    def of(cls, queued: AnalysisQueued) -> AnalysisQueuedBody:
        return cls(
            job_id=queued.job_id,
            operation_id=queued.operation_id,
            timeline_revision=queued.timeline_revision,
        )


class ProposalApprovedBody(MutationResponse):
    """An approved proposal, the definition it declared, the declaration's slot (`position`,
    `N⁺k`; `slot` the full position) and its operation."""

    proposal: StateProposalBody
    definition: dict[str, Any]
    position: str
    slot: dict[str, int]
    operation_id: str

    @classmethod
    def of(cls, approved: ProposalApproved) -> ProposalApprovedBody:
        return cls(
            proposal=StateProposalBody.of(approved.proposal),
            definition=approved.definition,
            position=approved.position,
            slot=approved.slot,
            operation_id=approved.operation_id,
            timeline_revision=approved.timeline_revision,
        )


class ProposalDismissedBody(MutationResponse):
    """A dismissed proposal (it is never proposed again)."""

    proposal: StateProposalBody

    @classmethod
    def of(cls, dismissed: ProposalDismissed) -> ProposalDismissedBody:
        return cls(
            proposal=StateProposalBody.of(dismissed.proposal),
            timeline_revision=dismissed.timeline_revision,
        )


@router.post(
    "/{story_id}/state-proposals/analyze",
    response_model=AnalysisQueuedBody,
    status_code=http_status.HTTP_202_ACCEPTED,
    operation_id="analyze_state_proposals",
    summary="Ask the engine to propose States",
    description=(
        "An on-demand analysis of the story's whole history (FR-STATE-3), run by a worker with "
        "no model call: 202 `{job_id, operation_id}` (the request is a `maintenance` operation; "
        "no revision moves). It reads the active beats' quantities (a declared quantity "
        "literal, or an amount in a detected claim's span) and `holds`/`owns` propositions "
        "about objects, and proposes each unit or object that recurs in three or more turns "
        "and that no State tracks by name, alias, unit or entity. The engine never creates a "
        "State: list the proposals and approve or dismiss each. Running it again writes no "
        "duplicate, and a dismissed proposal is never proposed again." + _GATE
    ),
)
async def analyze_state_proposals(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> AnalysisQueuedBody:
    queued = await engine.analyze_state_proposals(ctx, story_id)
    return answered(AnalysisQueuedBody.of(queued), ctx, response, replayed=queued.replayed)


@router.get(
    "/{story_id}/state-proposals",
    response_model=StateProposalsBody,
    operation_id="list_state_proposals",
    summary="List a story's State proposals",
    description=(
        "The engine's proposed States (FR-STATE-3), oldest first: each suggested name, type, "
        "scope, unit or entity, the evidence (the beats it recurs in, quoting them), the "
        "status and the analysis that proposed it." + _GATE
    ),
)
async def list_state_proposals(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> StateProposalsBody:
    return StateProposalsBody.of(await engine.list_state_proposals(ctx, story_id))


@router.post(
    "/{story_id}/state-proposals/{proposal_id}/approve",
    response_model=ProposalApprovedBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="approve_state_proposal",
    summary="Approve a proposed State",
    description=(
        "Declare the proposed State (FR-STATE-3) as suggested, with `definition`'s members on "
        "top (any member of a `POST /stories/{id}/states` definition but its owner), declared "
        "by `proposal`, and mark the proposal `approved`. The declaration is an inter-turn "
        "slot operation at `N⁺k`, as `POST /stories/{id}/states` is, with its refusals. 404 "
        "`not_found` (`reason: no_pending_proposal`) unless the proposal is pending." + _GATE
    ),
)
async def approve_state_proposal(
    story_id: StoryId,
    proposal_id: ProposalId,
    body: StateProposalApproveRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ProposalApprovedBody:
    approved = await engine.approve_state_proposal(ctx, story_id, proposal_id, body)
    return answered(ProposalApprovedBody.of(approved), ctx, response, replayed=approved.replayed)


@router.post(
    "/{story_id}/state-proposals/{proposal_id}/dismiss",
    response_model=ProposalDismissedBody,
    operation_id="dismiss_state_proposal",
    summary="Dismiss a proposed State",
    description=(
        "Dismiss the proposed State (FR-STATE-3): nothing is tracked, and the analysis never "
        "proposes it again. 404 `not_found` (`reason: no_pending_proposal`) unless the "
        "proposal is pending." + _GATE
    ),
)
async def dismiss_state_proposal(
    story_id: StoryId,
    proposal_id: ProposalId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ProposalDismissedBody:
    dismissed = await engine.dismiss_state_proposal(ctx, story_id, proposal_id)
    return answered(ProposalDismissedBody.of(dismissed), ctx, response, replayed=dismissed.replayed)


__all__ = [
    "AnalysisQueuedBody",
    "ProposalApprovedBody",
    "ProposalDismissedBody",
    "StateProposalBody",
    "StateProposalsBody",
    "router",
]
