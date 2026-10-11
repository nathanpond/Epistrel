"""Timeline edits over REST (FR-STORE-10,11,18; #65): truncate a story and undo it.

- ``POST /stories/{id}/truncate`` with ``{to_turn}`` discards everything positioned after
  ``to_turn``; nothing is deleted and valid-times are untouched.
- ``POST /stories/{id}/untruncate`` (body optional: ``{truncate_operation_id}``) reverts the
  most recent truncation.

Both answer ``{operation_id, timeline_revision, next_turn_index, …}``. ``GET /stories/{id}``
reports ``undo_available`` and ``dormant_chains``; ``GET /stories/{id}/history?as_of=N`` reads
the corrected history through N. Thin handlers over ``Engine``; the rules are in
``epistrel.core.timeline``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Response

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.timeline import TruncateRequest, UntruncateRequest

router = APIRouter(prefix="/stories", tags=["timeline"])

CONTROL_RULES = (
    "\n\nA control action: it bumps the story's timeline revision, and a turn generating while "
    "it commits is abandoned with 409 `timeline_changed`. `Idempotency-Key` and "
    "`X-Epistrel-Expected-Revision` are optional."
)


class TruncatedBody(MutationResponse):
    operation_id: str
    next_turn_index: int
    to_turn: int
    discarded_operations: int
    """How many operations the truncation discarded."""


class UntruncatedBody(MutationResponse):
    operation_id: str
    next_turn_index: int
    truncate_operation_id: str
    restored_operations: int
    """How many operations the untruncation reactivated."""


@router.post(
    "/{story_id}/truncate",
    response_model=TruncatedBody,
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
    story_id: str,
    body: TruncateRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> TruncatedBody:
    done = await engine.truncate(ctx, story_id, body)
    answer = TruncatedBody(
        operation_id=done.operation_id,
        timeline_revision=done.timeline_revision,
        next_turn_index=done.next_turn_index,
        to_turn=done.to_turn,
        discarded_operations=done.discarded_operations,
    )
    return answered(answer, ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/untruncate",
    response_model=UntruncatedBody,
    summary="Undo the most recent truncation",
    description=(
        "Reactivate exactly what the most recent truncation discarded (undo is last in, first "
        "out). Naming `truncate_operation_id` is optional; a truncation that is not the most "
        "recent one, or none at all, is 409 `nothing_to_undo`. When newer turns were written "
        "after the truncation, undoing it is a swap: 409 `swap_required` with "
        '`options: ["swap", "fork"]`.' + CONTROL_RULES
    ),
)
async def untruncate(
    story_id: str,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[UntruncateRequest | None, Body()] = None,
) -> UntruncatedBody:
    done = await engine.untruncate(ctx, story_id, body)
    answer = UntruncatedBody(
        operation_id=done.operation_id,
        timeline_revision=done.timeline_revision,
        next_turn_index=done.next_turn_index,
        truncate_operation_id=done.truncate_operation_id,
        restored_operations=done.restored_operations,
    )
    return answered(answer, ctx, response, replayed=done.replayed)
