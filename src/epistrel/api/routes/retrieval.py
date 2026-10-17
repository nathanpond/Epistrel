"""What a structured render would retrieve, for debugging (FR-RET-2..7; #141).

- ``GET /stories/{id}/retrieve?q=&viewpoint=&as_of=&actor_character_id=`` → the result of
  ``core.retrieval.pipeline.retrieve`` for the text ``q`` asked by ``actor_character_id``
  (default the player) just after turn ``as_of`` (default the head): the resolution, the located
  episodes and their participants' assessments, the ranked memories with their scores, the
  profiles and canon, the assembled context with what the budget dropped, and the per-stage
  timings.

``omniscient`` only (403 ``omniscient_required`` otherwise): the ranking runs over records a
character viewpoint may not see. A read: no operation, and no memory's access time moves. A
thin handler over ``Engine.retrieve``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.routes.scene import EntityProfileBody
from epistrel.core.resolution.types import Uncertain
from epistrel.core.retrieval.pipeline import RetrievalResult, RetrievedMemory

router = APIRouter(prefix="/stories", tags=["stories"])


class RetrievedMemoryBody(BaseModel):
    """One returned memory: its id, kind, text and position, its actor, occasion and
    participants, its retrieval tier (``retired``: rehydrated for this query, its tier
    unchanged), its importance, the arms that found it and, for a ranked memory, its score and
    the terms it came from (fused similarity, recency, decayed importance)."""

    id: str
    kind: str
    text: str
    narrative_turn: int | None
    beat: int | None
    character_id: str | None
    event_type: str | None
    participants: list[str]
    retrieval_tier: str
    importance: float | None
    arms: list[str]
    quarantined: bool = False
    """Quarantined (#145): returned only with ``include_quarantined=true``."""
    score: float | None
    similarity: float | None
    recency: float | None
    decayed_importance: float | None

    @classmethod
    def of(cls, m: RetrievedMemory) -> RetrievedMemoryBody:
        return cls(
            id=m.id,
            kind=m.kind.value,
            text=m.text,
            narrative_turn=m.narrative_turn,
            beat=m.beat,
            character_id=m.character_id,
            event_type=m.event_type,
            participants=list(m.participants),
            retrieval_tier=m.retrieval_tier.value,
            importance=m.importance,
            arms=list(m.arms),
            quarantined=m.quarantined,
            score=m.score,
            similarity=m.similarity,
            recency=m.recency,
            decayed_importance=m.decayed_importance,
        )


class MentionBody(BaseModel):
    """A mention the resolve stage found: its character span, kind, the entity it resolved to
    (or ``null``) and, when unresolved, the reason code."""

    span: list[int]
    kind: str
    entity_id: str | None
    reason: str | None


class ResolutionBody(BaseModel):
    """The resolve stage: the mentions, the resolved entities, the event type, the time window
    as story-minutes (``[start, end)``, ``null`` when the time did not resolve or the text has
    none) with the turns it maps to, and why the time did not resolve (a reason code)."""

    mentions: list[MentionBody]
    entities: list[str]
    event_type: str | None
    window: list[int | None] | None
    turns: list[int | None] | None
    time_reason: str | None


class CanonBody(BaseModel):
    """A canon fact about a resolved entity: its id, subject, predicate and its object (an
    entity, a set or a literal), as the viewpoint may read it."""

    fact_id: str
    subject_id: str
    predicate_id: str
    object_id: str | None
    object_set: list[str] | None
    literal: Any | None
    polarity: str


class ContextItemBody(BaseModel):
    """A placed context item: its tier (1 ledger and pins, 2 occasions, 3 profiles and canon,
    4 ranked memories), kind, id, text and tokens (``len // 4``)."""

    tier: int
    kind: str
    item_id: str
    text: str
    tokens: int


class DroppedBody(BaseModel):
    """A context item the budget left out: ``budget``, ``pin_cap`` or ``duplicate``."""

    tier: int
    kind: str
    item_id: str
    tokens: int
    reason: str


class ContextBody(BaseModel):
    """The assembled context within the token budget."""

    budget: int
    used: int
    items: list[ContextItemBody]
    dropped: list[DroppedBody]


class RetrievalBody(BaseModel):
    """What a structured render's context would retrieve for the query (FR-RET-2..7)."""

    position: list[int]
    resolution: ResolutionBody
    episodes: list[RetrievedMemoryBody]
    participants: list[str]
    assessments: list[RetrievedMemoryBody]
    memories: list[RetrievedMemoryBody]
    profiles: list[EntityProfileBody]
    canon: list[CanonBody]
    context: ContextBody
    rehydrated: list[str]
    degradation: list[str]
    skipped: list[str]
    model_calls: int
    embedding_model_version: str | None
    timings: dict[str, float]

    @classmethod
    def of(cls, r: RetrievalResult) -> RetrievalBody:
        res = r.resolution
        reason = res.time.reason.code.value if isinstance(res.time, Uncertain) else None
        return cls(
            position=list(r.position),
            resolution=ResolutionBody(
                mentions=[
                    MentionBody(
                        span=list(m.span),
                        kind=m.kind.value,
                        entity_id=m.entity_id,
                        reason=None if m.reason is None else m.reason.code.value,
                    )
                    for m in res.mentions
                ],
                entities=list(res.entities),
                event_type=res.event_type,
                window=None if res.window is None else list(res.window),
                turns=None if r.turn_window is None else list(r.turn_window),
                time_reason=reason,
            ),
            episodes=[RetrievedMemoryBody.of(m) for m in r.episodes],
            participants=list(r.participants),
            assessments=[RetrievedMemoryBody.of(m) for m in r.assessments],
            memories=[RetrievedMemoryBody.of(m) for m in r.memories],
            profiles=[EntityProfileBody.of(p) for p in r.profiles],
            canon=[
                CanonBody(
                    fact_id=f.id,
                    subject_id=f.subject_id,
                    predicate_id=f.predicate_id,
                    object_id=f.proposition.object_id,
                    object_set=None
                    if f.proposition.object_set is None
                    else list(f.proposition.object_set),
                    literal=None
                    if f.proposition.literal is None
                    else f.proposition.literal.model_dump(mode="json"),
                    polarity=f.proposition.polarity.value,
                )
                for f in r.canon
            ],
            context=ContextBody(
                budget=r.context.budget,
                used=r.context.used,
                items=[
                    ContextItemBody(
                        tier=int(i.tier),
                        kind=i.kind,
                        item_id=i.item_id,
                        text=i.text,
                        tokens=i.tokens,
                    )
                    for i in r.context.items
                ],
                dropped=[
                    DroppedBody(
                        tier=int(d.tier),
                        kind=d.kind,
                        item_id=d.item_id,
                        tokens=d.tokens,
                        reason=d.reason,
                    )
                    for d in r.context.dropped
                ],
            ),
            rehydrated=list(r.rehydrated),
            degradation=list(r.degradation),
            skipped=list(r.skipped),
            model_calls=r.model_calls,
            embedding_model_version=r.embedding_model_version,
            timings=dict(r.timings),
        )


@router.get(
    "/{story_id}/retrieve",
    response_model=RetrievalBody,
    operation_id="retrieve",
    summary="Show what a structured render would retrieve for a query",
    description=(
        "Runs the retrieval pipeline for the text `q` asked by `actor_character_id` (default the "
        "player) just after turn `as_of` (default the head): resolution of time, entities and "
        "event type; the occasion's episodes and the participants' assessments; hybrid vector "
        "and lexical search ranked by similarity, recency and decayed importance; standing "
        "profiles and canon; and the context assembled within the token budget, with per-stage "
        "timings (ms, model calls excluded). `viewpoint` must resolve to `omniscient` (403 "
        "`omniscient_required` otherwise). A read: no operation, and no memory's access time "
        "moves. A story whose `episodic` module is off answers an empty result with "
        '`skipped: ["episodic"]`. A quarantined memory (#145) is never retrieved; '
        "`include_quarantined=true` shows it anyway, marked `quarantined`, for debugging."
    ),
)
async def get_retrieve(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    q: str,
    viewpoint: str | None = None,
    as_of: int | None = None,
    actor_character_id: str | None = None,
    include_quarantined: bool = False,
) -> RetrievalBody:
    return RetrievalBody.of(
        await engine.retrieve(
            ctx,
            story_id,
            q=q,
            viewpoint=viewpoint,
            as_of=as_of,
            actor_character_id=actor_character_id,
            include_quarantined=include_quarantined,
        )
    )
