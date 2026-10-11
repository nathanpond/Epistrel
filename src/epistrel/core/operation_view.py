"""One operation as the history API returns it: ``GET /operations/{id}`` (FR-CONC-6; #61).

The view answers "what happened to my request": the operation's kind, status, position (turn
and variant), the story's revision once it committed, who asked, when, the viewpoint its result
was produced for, its revalidation attempts (#47), its origin and, for an ingested batch, every
operation the batch committed together (#58). A committed Lite turn also reports its wait in its
story's queue and its delay to the first model call (``queued_ms``, ``first_call_offset_ms``,
from its turn report; #76). Within the result window the stored result of
the operation's ``Idempotency-Key`` is included — for a viewpoint-bound result (a turn) only
when the read names that same viewpoint; another named viewpoint is 409
``idempotency_conflict``, and a read naming none gets the view without the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import EpistrelError, IdempotencyConflict
from epistrel.core.idempotency import StoredResult, stored_result
from epistrel.core.tables import operations


class OperationNotFound(EpistrelError):
    """No operation has the id a call names."""

    code = "not_found"
    http_status = 404


@dataclass(frozen=True, slots=True)
class OperationView:
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
    result: StoredResult | None = None
    queued_ms: int | None = None
    """A committed turn's wait in its story's queue (``None``: not queued, or not a turn; #76)."""
    first_call_offset_ms: int | None = None
    """A committed turn's delay from reaching the front to its first model call (#76)."""


INGESTED = "ingested"


async def read_operation(
    db: AsyncEngine, operation_id: str, *, viewpoint: str | None = None
) -> OperationView:
    """The view of ``operation_id``; see the module docstring."""
    async with db.connect() as conn:
        row = (
            await conn.execute(select(operations).where(operations.c.operation_id == operation_id))
        ).one_or_none()
        if row is None:
            raise OperationNotFound(
                f"operation {operation_id} does not exist", {"operation_id": operation_id}
            )
        batch: list[str] | None = None
        if row.origin == INGESTED and row.committed_at is not None:
            # A batch commits in one transaction: its operations share ``committed_at``.
            batch = list(
                await conn.scalars(
                    select(operations.c.operation_id)
                    .where(
                        operations.c.story_id == row.story_id,
                        operations.c.origin == INGESTED,
                        operations.c.committed_at == row.committed_at,
                    )
                    .order_by(operations.c.narrative_turn, operations.c.operation_id)
                )
            )
        result = await stored_result(conn, operation_id)
    if result is not None and row.viewpoint is not None and viewpoint != row.viewpoint:
        if viewpoint is not None:
            raise IdempotencyConflict(
                "the operation's result was produced for another output viewpoint",
                {"reason": "viewpoint"},
            )
        result = None
    return OperationView(
        id=row.operation_id,
        story_id=row.story_id,
        kind=str(row.kind),
        status=str(row.status),
        narrative_turn=row.narrative_turn,
        turn_variant=row.turn_variant,
        timeline_revision=row.timeline_revision,
        principal=row.principal,
        started_at=row.started_at,
        committed_at=row.committed_at,
        viewpoint=row.viewpoint,
        origin=row.origin,
        batch_operation_ids=batch,
        attempts=list(row.attempts or []),
        config_revisions=row.config_revisions,
        idempotency_key=row.idempotency_key,
        result=result,
        queued_ms=_report_ms(row.turn_report, "queued_ms"),
        first_call_offset_ms=_report_ms(row.turn_report, "first_call_offset_ms"),
    )


def _report_ms(report: Any, name: str) -> int | None:
    """An integer field of the turn report (``operations.turn_report``), if recorded."""
    value = report.get(name) if isinstance(report, dict) else None
    return value if isinstance(value, int) else None


__all__ = ["OperationNotFound", "OperationView", "read_operation"]
