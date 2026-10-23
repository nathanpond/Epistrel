"""``POST /admin/stories/{id}/beliefs``: an author-directed belief (FR-EPI-7, FR-BELIEF-4; #160).

An operator endpoint under ``/admin`` (like #145's ``…/facts``): it needs the service credential
and a principal, and any principal may call it. The call is a position-less ``control``
operation that bumps the story's timeline revision and answers ``{timeline_revision, belief,
transition, closed, operation_id}`` (201); ``Idempotency-Key`` and
``X-Epistrel-Expected-Revision`` are honoured. A thin handler over ``Engine.write_belief``; the
rules are in ``epistrel.core.epistemic.author`` and ``…transitions``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.epistemic.author import BeliefRequest, BeliefWrite
from epistrel.core.epistemic.partitions import Belief
from epistrel.core.epistemic.transitions import Transition
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition

router = APIRouter(prefix="/stories")


class PositionBody(BaseModel):
    """A narrative position: turn, variant, inter-turn slot, beat and step."""

    turn: int
    variant: int
    slot_k: int
    beat: int
    step: int

    @classmethod
    def of(cls, position: Position) -> PositionBody:
        return cls(**position.document())


class AuthoredBeliefBody(BaseModel):
    """The belief after the call: its proposition, dual confidence, accessibility and hold
    state, the transition that produced it (``provenance``), the positions between which the
    holder holds it, valid-time (story-minutes) and its lineage."""

    id: str
    holder_character_id: str
    proposition_id: str
    proposition: Proposition
    polarity: str
    confidence_level: str
    confidence_scalar: float | None
    accessibility: float
    state: str
    provenance: str
    held_from: PositionBody
    held_to: PositionBody | None
    valid_from: int | None
    valid_to: int | None
    lineage_id: str

    @classmethod
    def of(cls, belief: Belief) -> AuthoredBeliefBody:
        return cls(
            id=belief.id,
            holder_character_id=belief.holder_character_id,
            proposition_id=belief.proposition_id,
            proposition=belief.proposition,
            polarity=belief.polarity,
            confidence_level=belief.confidence_level.value,
            confidence_scalar=belief.confidence_scalar,
            accessibility=belief.accessibility,
            state=belief.state.value,
            provenance=belief.provenance,
            held_from=PositionBody.of(belief.held_from),
            held_to=None if belief.held_to is None else PositionBody.of(belief.held_to),
            valid_from=belief.valid_from,
            valid_to=belief.valid_to,
            lineage_id=belief.lineage_id,
        )


class TransitionBody(BaseModel):
    """The ``author`` transition: its kind, antecedent (the call's author act), story-time
    (``valid_at``, story-minutes), the confidence it left and its position."""

    id: str
    kind: str
    antecedent: dict[str, Any]
    valid_at: int | None
    confidence_level: str
    confidence_scalar: float | None
    position: PositionBody

    @classmethod
    def of(cls, transition: Transition) -> TransitionBody:
        return cls(
            id=transition.id,
            kind=transition.kind.value,
            antecedent=transition.antecedent.document(),
            valid_at=transition.valid_at,
            confidence_level=transition.confidence_level.value,
            confidence_scalar=transition.confidence_scalar,
            position=PositionBody.of(transition.position),
        )


class BeliefWritten(MutationResponse):
    """The belief, its transition, the held beliefs it closed (``closed``), the story's
    revision and the ``control`` operation."""

    belief: AuthoredBeliefBody
    transition: TransitionBody
    closed: list[str]
    operation_id: str

    @classmethod
    def of(cls, written: BeliefWrite) -> BeliefWritten:
        return cls(
            belief=AuthoredBeliefBody.of(written.belief),
            transition=TransitionBody.of(written.transition),
            closed=list(written.closed),
            operation_id=written.operation_id,
            timeline_revision=written.timeline_revision,
        )


@router.post(
    "/{story_id}/beliefs",
    response_model=BeliefWritten,
    status_code=201,
    operation_id="write_belief",
    summary="Direct a character's belief",
    description=(
        "Set what modeled character `holder` believes: #131's normalized `proposition` (JSON, "
        "never prose), an optional `polarity` replacing its own, and the confidence: "
        "`confidence_level`, `confidence_scalar` (0 to 1, at most three decimals), both (they "
        "must agree: the scalar projects onto the level by the bands `<0.2` unknown, `<0.5` "
        "suspected, `<0.9` believed, else known; 422 `confidence_inconsistent` otherwise) or "
        "neither (`known`). `disclosure` (`open`, `guarded`, `secret`) is recorded on the "
        "author act. The belief is an `author` transition citing the call's own author act "
        "(FR-EPI-7); a belief the holder already holds changes in place, and a held belief it "
        "contradicts is closed (`closed`, kept in its history). 409 `module_off` unless the "
        "story keeps partitions; 422 `holder_has_no_partition` for the player's character or "
        "a non-character.\n\nA position-less `control` operation that bumps the story's "
        "timeline revision, so a turn generating meanwhile revalidates before it commits. Any "
        "principal may call it. `Idempotency-Key` and `X-Epistrel-Expected-Revision` are "
        "optional."
    ),
)
async def write_belief(
    story_id: StoryId,
    body: BeliefRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> JSONResponse:
    written = await engine.write_belief(ctx, story_id, body)
    answer = answered(BeliefWritten.of(written), ctx, response, replayed=written.replayed)
    return JSONResponse(
        answer.model_dump(mode="json"), status_code=201, headers=dict(response.headers)
    )


__all__ = ["BeliefWritten", "router"]
