"""A story's predicate registry over REST (FR-PROP-3,4; #131).

- ``GET /stories/{id}/predicates`` → the core vocabulary, the story's author and auto predicates
  (each with its pending proposal), the story's implication rules and its configuration
  revision.
- ``POST /stories/{id}/predicates/{predicate_id}/approve`` with ``{changes?}`` applies the
  predicate's pending strengthening proposal (or the fields named), as an interpretive
  configuration change; the answer lists the story's propositions the new semantics would flag
  (they are never changed).
- ``POST /stories/{id}/predicates/{predicate_id}/reject`` closes the proposal unapplied.

Thin handlers over ``Engine``; the rules are in ``epistrel.core.propositions.registry``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.propositions.model import Implication, Predicate
from epistrel.core.propositions.registry import (
    ApproveRequest,
    Decision,
    PredicateList,
    ProposalView,
)

router = APIRouter(prefix="/stories", tags=["stories"])

PredicateId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The predicate's id (a ULID).",
        examples=["01JPRED0000000000000000018"],
    ),
]

DECISION_RULES = (
    " Only a predicate the engine auto-registered takes proposals; a proposal is inert until "
    "approved. 404 `not_found` when the predicate is not one of the story's or has no pending "
    "proposal. `Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional."
)


class ImplicationBody(BaseModel):
    """A declared rule: ``when(s, o) ⇒ then(s, o')``, the object carried over or mapped
    through one ``via`` join."""

    when: str
    then: str
    via: str | None = None

    @classmethod
    def of(cls, rule: Implication) -> ImplicationBody:
        return cls(when=rule.when, then=rule.then, via=rule.via)


class ProposalBody(BaseModel):
    """A pending proposal: what it would change."""

    id: str
    status: str
    changes: dict[str, Any]
    filed_by: str

    @classmethod
    def of(cls, view: ProposalView) -> ProposalBody:
        return cls(id=view.id, status=view.status, changes=view.proposed, filed_by=view.filed_by)


class PredicateBody(BaseModel):
    """A registered predicate and its semantics (FR-PROP-3): its layer (``core``, ``author``
    or ``auto``), argument types, cardinality over its uniqueness key (the subject plus
    ``key_qualifiers``), declared qualifiers (``null``: undeclared, an auto predicate's),
    temporal kind, exclusions, containment, implications, symmetry or inverse, owner and
    documentation."""

    id: str
    story_id: str | None
    name: str
    layer: str
    subject_type: str
    object_type: str
    cardinality: str
    max_cardinality: int | None
    key_qualifiers: list[str]
    qualifiers: dict[str, str] | None
    set_object: bool
    temporal_kind: str
    exclusions: list[str]
    containment_of: str | None
    implications: list[ImplicationBody]
    symmetric: bool
    inverse_of: str | None
    owner: str
    projection: str | None
    docs: str
    weak: bool
    pending_proposal: ProposalBody | None = None

    @classmethod
    def of(cls, predicate: Predicate, pending: ProposalView | None = None) -> PredicateBody:
        return cls(
            id=predicate.id,
            story_id=predicate.story_id,
            name=predicate.name,
            layer=predicate.layer.value,
            subject_type=predicate.subject_type,
            object_type=predicate.object_type,
            cardinality=predicate.cardinality.value,
            max_cardinality=predicate.max_cardinality,
            key_qualifiers=list(predicate.key_qualifiers),
            qualifiers=None
            if predicate.qualifiers is None
            else {k: v.value for k, v in predicate.qualifiers.items()},
            set_object=predicate.set_object,
            temporal_kind=predicate.temporal_kind.value,
            exclusions=list(predicate.exclusions),
            containment_of=predicate.containment_of,
            implications=[ImplicationBody.of(r) for r in predicate.implications],
            symmetric=predicate.symmetric,
            inverse_of=predicate.inverse_of,
            owner=predicate.owner.value,
            projection=predicate.projection,
            docs=predicate.docs,
            weak=predicate.weak,
            pending_proposal=None if pending is None else ProposalBody.of(pending),
        )


class PredicateListBody(BaseModel):
    """The story's registry: the core vocabulary first, then the story's predicates by name;
    the story's implication rules; its configuration revision."""

    predicates: list[PredicateBody]
    rules: list[ImplicationBody]
    config_revision: int

    @classmethod
    def of(cls, found: PredicateList) -> PredicateListBody:
        return cls(
            predicates=[PredicateBody.of(p, found.pending.get(p.id)) for p in found.predicates],
            rules=[ImplicationBody.of(r) for r in found.rules],
            config_revision=found.config_revision,
        )


class ViolationBody(BaseModel):
    """A key the approved semantics find with too many objects over overlapping time: the
    propositions are reported, never changed."""

    subject_id: str
    proposition_ids: list[str]
    rule: str


class PredicateDecisionBody(MutationResponse):
    """A proposal's decision: the predicate as it now is, the proposal and its status, the
    fields applied, the propositions the new semantics would flag, and the story's revisions
    (approval moves ``config_revision``; rejection moves nothing)."""

    predicate: PredicateBody
    proposal_id: str
    status: str
    applied: dict[str, Any]
    violations: list[ViolationBody]
    config_revision: int

    @classmethod
    def of(cls, decision: Decision) -> PredicateDecisionBody:
        return cls(
            predicate=PredicateBody.of(decision.predicate),
            proposal_id=decision.proposal_id,
            status=decision.status,
            applied=dict(decision.applied),
            violations=[
                ViolationBody(
                    subject_id=v.subject_id, proposition_ids=list(v.proposition_ids), rule=v.rule
                )
                for v in decision.violations
            ],
            config_revision=decision.config_revision,
            timeline_revision=decision.timeline_revision,
        )


@router.get(
    "/{story_id}/predicates",
    response_model=PredicateListBody,
    operation_id="list_predicates",
    summary="List the story's predicate registry",
)
async def get_predicates(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> PredicateListBody:
    return PredicateListBody.of(await engine.list_predicates(ctx, story_id))


@router.post(
    "/{story_id}/predicates/{predicate_id}/approve",
    response_model=PredicateDecisionBody,
    operation_id="approve_predicate",
    summary="Approve a predicate's strengthening proposal",
    description=(
        "Applies the predicate's pending proposal, or the fields `changes` names (each must be "
        "one the proposal holds; 422 otherwise), as an interpretive configuration change: the "
        "story's `config_revision` moves by one and a turn generating meanwhile is checked "
        "again at commit. Existing propositions the new semantics would flag are listed in "
        "`violations` and never changed." + DECISION_RULES
    ),
)
async def post_approve(
    story_id: StoryId,
    predicate_id: PredicateId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: ApproveRequest | None = None,
) -> PredicateDecisionBody:
    request = body if body is not None else ApproveRequest()
    decision = await engine.approve_predicate(ctx, story_id, predicate_id, request)
    return answered(PredicateDecisionBody.of(decision), ctx, response, replayed=decision.replayed)


@router.post(
    "/{story_id}/predicates/{predicate_id}/reject",
    response_model=PredicateDecisionBody,
    operation_id="reject_predicate",
    summary="Reject a predicate's strengthening proposal",
    description="Closes the predicate's pending proposal unapplied; nothing else changes."
    + DECISION_RULES,
)
async def post_reject(
    story_id: StoryId,
    predicate_id: PredicateId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> PredicateDecisionBody:
    decision = await engine.reject_predicate(ctx, story_id, predicate_id)
    return answered(PredicateDecisionBody.of(decision), ctx, response, replayed=decision.replayed)
