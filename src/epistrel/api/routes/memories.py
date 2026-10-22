"""``/stories/{id}/memories/{memory_id}/pin``: author pins (FR-MEM-8, FR-LITE-3; #147).

- ``POST …/pin``: pin the memory. It is outside the hot-set budgets and never retired (a
  retired memory is rehydrated to the hot set), and every structured render's context carries
  it (unless it is quarantined). 422 ``pin_limit_exceeded`` with ``details {scope, limit,
  actual}`` past the deployment's ``max_pinned_per_character`` or ``max_pinned_per_story`` (the
  story's text pins count toward the latter);
- ``DELETE …/pin``: remove the pin; the memory counts toward the budgets again.

Each write is a position-less ``control`` operation that bumps the story's timeline revision and
answers ``{memory, operation_id, timeline_revision}`` (``operation_id`` ``null`` when nothing
changed); ``Idempotency-Key`` and ``X-Epistrel-Expected-Revision`` are honoured. Thin handlers
over ``Engine``; the rules are in ``epistrel.core.memory.pins``.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.routes.admin.mutation import MemoryId, MemoryWritten

router = APIRouter(prefix="/stories", tags=["stories"])

PIN_RULES = (
    "\n\nA position-less `control` operation that bumps the story's timeline revision, so a "
    "turn generating meanwhile revalidates before it commits. 404 for an unknown memory. "
    "`Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional."
)


@router.post(
    "/{story_id}/memories/{memory_id}/pin",
    response_model=MemoryWritten,
    operation_id="pin_memory",
    summary="Pin a memory",
    description=(
        "Pin the memory (FR-MEM-8): it is outside the story's hot-set budgets and never "
        "retired (a retired memory returns to the hot set), and every structured render's "
        "context carries it unless it is quarantined. A character may hold at most "
        "`max_pinned_per_character` pinned memories and a story `max_pinned_per_story` "
        "(its text pins included), deployment settings: past either, 422 "
        "`pin_limit_exceeded` with `details.scope` (`character` or `story`), `limit` and "
        "`actual`. A pinned memory answers unchanged." + PIN_RULES
    ),
)
async def pin_memory(
    story_id: StoryId,
    memory_id: MemoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> MemoryWritten:
    written = await engine.pin_memory(ctx, story_id, memory_id)
    return answered(MemoryWritten.of(written), ctx, response, replayed=written.replayed)


@router.delete(
    "/{story_id}/memories/{memory_id}/pin",
    response_model=MemoryWritten,
    operation_id="unpin_memory",
    summary="Remove a memory's pin",
    description=(
        "Remove the pin: the memory counts toward the story's hot-set budgets again, so an "
        "unpin above budget starts an overflow episode (FR-MEM-8). A memory that is not "
        "pinned answers unchanged." + PIN_RULES
    ),
)
async def unpin_memory(
    story_id: StoryId,
    memory_id: MemoryId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> MemoryWritten:
    written = await engine.unpin_memory(ctx, story_id, memory_id)
    return answered(MemoryWritten.of(written), ctx, response, replayed=written.replayed)
