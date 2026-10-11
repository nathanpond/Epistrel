"""``GET /operations/{id}``: one operation through the history API (FR-CONC-6; #61).

The reference a retry gets after the idempotency result window points here. A thin handler
over ``Engine.get_operation``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.core.operation_view import OperationView

router = APIRouter(prefix="/operations", tags=["operations"])

OPERATION_DESCRIPTION = (
    "An operation's kind, status (`in_flight`, `committed`, `abandoned` or `failed`), position "
    "(`narrative_turn`, `turn_variant`), the story's `timeline_revision` once it committed, the "
    "requesting principal, its timestamps, revalidation `attempts`, `config_revisions`, "
    "`origin` (`generated` or `ingested`) and, for an ingested batch, `batch_operation_ids`."
    "\n\nWithin the idempotency result window (default 24 hours) `result` holds the stored "
    "response (`status`, `body`) of the operation's `Idempotency-Key`. A result produced for an "
    "output viewpoint (a turn) is returned only when `?viewpoint=` names that viewpoint; another "
    "named viewpoint is 409 `idempotency_conflict`. After the window `result` is absent: the "
    "operation is the reference a retry receives. Unknown ids are 404."
)


class StoredResultBody(BaseModel):
    status: int
    body: Any


class OperationBody(BaseModel):
    id: str
    story_id: str | None
    kind: str
    status: str
    narrative_turn: int | None
    turn_variant: int | None
    timeline_revision: int | None
    principal: str
    started_at: datetime
    committed_at: datetime | None
    viewpoint: str | None
    origin: str
    batch_operation_ids: list[str] | None
    attempts: list[dict[str, Any]]
    config_revisions: dict[str, Any] | None
    idempotency_key: str | None
    result: StoredResultBody | None = None

    @classmethod
    def of(cls, view: OperationView) -> OperationBody:
        return cls(
            id=view.id,
            story_id=view.story_id,
            kind=view.kind,
            status=view.status,
            narrative_turn=view.narrative_turn,
            turn_variant=view.turn_variant,
            timeline_revision=view.timeline_revision,
            principal=view.principal,
            started_at=view.started_at,
            committed_at=view.committed_at,
            viewpoint=view.viewpoint,
            origin=view.origin,
            batch_operation_ids=view.batch_operation_ids,
            attempts=view.attempts,
            config_revisions=view.config_revisions,
            idempotency_key=view.idempotency_key,
            result=(
                StoredResultBody(status=view.result.status, body=view.result.body)
                if view.result is not None
                else None
            ),
        )


@router.get(
    "/{operation_id}",
    response_model=OperationBody,
    response_model_exclude_none=False,
    summary="Read one operation (the reference of an idempotent retry)",
    description=OPERATION_DESCRIPTION,
)
async def get_operation(operation_id: str, ctx: ContextDep, engine: EngineDep) -> OperationBody:
    return OperationBody.of(await engine.get_operation(ctx, operation_id))
