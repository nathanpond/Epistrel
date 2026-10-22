"""``/admin/stories/{id}``: operator actions on one story.

- ``POST …/rebuild``: refold a story's projections from the log (FR-ADMIN-3). A story with more
  than ``rebuild_async_threshold`` events is rebuilt by a worker: the call answers 202
  ``{job_id, timeline_revision}`` (#70).
- ``POST …/consolidate`` ``{from_turn, to_turn, scope}``: consolidate those turns now, rerunning
  a window already consolidated (FR-MEM-3,10; #143). Always handed to a worker: 202
  ``{job_id, timeline_revision}``; 409 ``module_off`` with consolidation off, 422
  ``scope_mismatch`` for inputs no output may combine (FR-MEM-9).
- ``GET …/hot-set``: the story's hot set (FR-MEM-8; #147): its counters (recounted now, and as
  stored), budgets and pin ceilings, its pins, its open overflow episodes and its latest 20
  retirements. Identifiers and counts only.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.memory.consolidation import ConsolidateRequest
from epistrel.core.memory.pins import HotSetView
from epistrel.core.memory.retirement import HotCounts
from epistrel.core.projections import RebuildResult
from epistrel.orchestrator.jobs.handlers.consolidation import ConsolidationQueued
from epistrel.orchestrator.jobs.handlers.rebuild import RebuildQueued

router = APIRouter(prefix="/stories")


class RebuildResponse(MutationResponse):
    """Rows written per projection table, and the ``maintenance`` operation that wrote them."""

    counts: dict[str, int]
    operation_id: str

    @classmethod
    def of(cls, result: RebuildResult) -> RebuildResponse:
        return cls(
            counts=result.counts,
            operation_id=result.operation_id,
            timeline_revision=result.timeline_revision,
        )


class RebuildQueuedResponse(MutationResponse):
    """The ``rebuild_projections`` job a large story's rebuild was handed to (202), with the
    story's revision when it was queued."""

    job_id: str

    @classmethod
    def of(cls, queued: RebuildQueued) -> RebuildQueuedResponse:
        return cls(job_id=queued.job_id, timeline_revision=queued.timeline_revision)


@router.post(
    "/{story_id}/rebuild",
    response_model=RebuildResponse,
    responses={202: {"model": RebuildQueuedResponse, "description": "Rebuilt by a worker."}},
    operation_id="rebuild_projections",
    summary="Rebuild a story's projections from its event log",
    description=(
        "Refold every projection of the story from the event log (FR-ADMIN-3) and answer the "
        "rows written per table and the `maintenance` operation that wrote them. A story with "
        "more than `rebuild_async_threshold` events is handed to a worker instead: 202 "
        "`{job_id, timeline_revision}` (a rebuild already queued for the story is that job)."
    ),
)
async def rebuild_projections(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> RebuildResponse | JSONResponse:
    result = await engine.rebuild_projections(ctx, story_id)
    if isinstance(result, RebuildQueued):
        queued = answered(RebuildQueuedResponse.of(result), ctx, response, replayed=result.replayed)
        return JSONResponse(
            queued.model_dump(mode="json"), status_code=202, headers=dict(response.headers)
        )
    return answered(RebuildResponse.of(result), ctx, response, replayed=result.replayed)


class ConsolidationQueuedResponse(MutationResponse):
    """The ``consolidation`` job a requested consolidation was handed to (202), with the story's
    revision when it was queued."""

    job_id: str

    @classmethod
    def of(cls, queued: ConsolidationQueued) -> ConsolidationQueuedResponse:
        return cls(job_id=queued.job_id, timeline_revision=queued.timeline_revision)


@router.post(
    "/{story_id}/consolidate",
    response_model=ConsolidationQueuedResponse,
    status_code=202,
    operation_id="consolidate_story",
    summary="Consolidate a window of a story's memories",
    description=(
        "Compress turns `from_turn`..`to_turn` of one viewpoint `scope` (the shared scope when "
        "omitted; `{characters: [...]}` for memories private to those characters) into "
        "semantic facts and reflections (FR-MEM-3,6,9,10). An explicit request: a window "
        "already consolidated is rerun with new provenance, and its earlier outputs are "
        "superseded. Handed to a worker: 202 `{job_id, timeline_revision}` (the same request "
        "while it waits is that job). 409 `module_off` when the story's configuration has "
        "consolidation off; 422 `scope_mismatch` when the window's inputs span scopes no "
        "output may combine."
    ),
)
async def consolidate_story(
    story_id: StoryId,
    body: ConsolidateRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ConsolidationQueuedResponse:
    queued = await engine.consolidate(ctx, story_id, body)
    return answered(ConsolidationQueuedResponse.of(queued), ctx, response, replayed=queued.replayed)


class HotCountsBody(BaseModel):
    """Each character's unpinned hot memories (characters with none left out) and the story's
    unpinned hot vectors."""

    characters: dict[str, int]
    story_vectors: int

    @classmethod
    def of(cls, counts: HotCounts) -> HotCountsBody:
        document = counts.document()
        return cls(characters=document["characters"], story_vectors=document["story_vectors"])


class HotSetBudgets(BaseModel):
    """The story's ``memory.hot_set.*`` dials and the deployment's pin ceilings."""

    memories_per_character: int
    vectors_per_story: int
    importance_floor: float
    retirement_max_lag: int
    retain_retired_vectors: bool
    retain_dormant_vectors: bool
    max_pinned_per_character: int
    max_pinned_per_story: int


class HotSetPins(BaseModel):
    """The story's pinned memories (all, and per holding character) and its text pins: the
    story ceiling counts both."""

    memory_pins: int
    text_pins: int
    by_character: dict[str, int]


class EpisodeBody(BaseModel):
    """One open overflow episode: its set, when it opened, its fixed deadline and its excess."""

    id: str
    scope: str
    scope_id: str
    opened_turn: int
    deadline_turn: int
    excess_at_open: int
    excess: int


class RetirementBody(BaseModel):
    """One retired memory (identifiers only)."""

    memory_id: str
    reason: str | None
    retired_turn: int | None
    retired_at: datetime | None
    holder_character_id: str | None


class HotSetBody(BaseModel):
    """``GET /admin/stories/{id}/hot-set``."""

    story_id: str
    episodic: bool
    """Whether the story's configuration keeps episodic memory (retirement runs only then)."""
    counters: HotCountsBody
    stored_counters: HotCountsBody | None
    """The counters as stored (``null``: not computed yet; the next commit recounts)."""
    budgets: HotSetBudgets
    pins: HotSetPins
    head_turn: int
    open_episodes: list[EpisodeBody]
    recent_retirements: list[RetirementBody]
    timeline_revision: int

    @classmethod
    def of(cls, view: HotSetView) -> HotSetBody:
        b = view.budgets
        return cls(
            story_id=view.story_id,
            episodic=view.episodic,
            counters=HotCountsBody.of(view.counters),
            stored_counters=None if view.stored is None else HotCountsBody.of(view.stored),
            budgets=HotSetBudgets(
                memories_per_character=b.memories_per_character,
                vectors_per_story=b.vectors_per_story,
                importance_floor=b.importance_floor,
                retirement_max_lag=b.max_lag,
                retain_retired_vectors=b.retain_retired,
                retain_dormant_vectors=b.retain_dormant,
                max_pinned_per_character=view.caps.max_pinned_per_character,
                max_pinned_per_story=view.caps.max_pinned_per_story,
            ),
            pins=HotSetPins(
                memory_pins=view.pins.memory_pins,
                text_pins=view.pins.text_pins,
                by_character=dict(sorted(view.pins.by_character.items())),
            ),
            head_turn=view.head_turn,
            open_episodes=[
                EpisodeBody(
                    id=e.id,
                    scope=e.scope,
                    scope_id=e.scope_id,
                    opened_turn=e.opened_turn,
                    deadline_turn=e.deadline_turn,
                    excess_at_open=e.excess_at_open,
                    excess=e.excess,
                )
                for e in view.open_episodes
            ],
            recent_retirements=[
                RetirementBody(
                    memory_id=r.memory_id,
                    reason=r.reason,
                    retired_turn=r.retired_turn,
                    retired_at=r.retired_at,
                    holder_character_id=r.holder_character_id,
                )
                for r in view.recent_retirements
            ],
            timeline_revision=view.timeline_revision,
        )


@router.get(
    "/{story_id}/hot-set",
    response_model=HotSetBody,
    operation_id="hot_set",
    summary="Read a story's hot set",
    description=(
        "The story's retrieval-eligible memories at a glance (FR-MEM-8, NFR-SCALE-2): each "
        "character's unpinned hot memories and the story's unpinned hot vectors (recounted "
        "now, and as stored), the `memory.hot_set.*` budgets and the deployment's pin "
        "ceilings, the story's pins, its open overflow episodes (each with its fixed "
        "deadline turn and its excess) and its latest 20 retirements. Identifiers and counts "
        "only. A read: nothing is written."
    ),
)
async def hot_set(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> HotSetBody:
    return HotSetBody.of(await engine.hot_set(ctx, story_id))
