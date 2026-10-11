"""Writing the deployment-level ``audit_log`` (#56): records of acts that have no operation of
their own, and that must outlive the story they name.

Callers pass identifiers and small structured details only — never story text (NFR-SEC-4).
``record`` writes one row in the caller's transaction when given a connection, or in a
transaction of its own when given the engine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.tables.audit_log import AuditKind, audit_log


async def record(
    db: AsyncEngine | AsyncConnection,
    kind: AuditKind,
    *,
    principal: str,
    story_id: str | None = None,
    policy_ids: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
) -> str:
    """Append one ``audit_log`` row and return its id."""
    row_id = new_ulid()
    statement = insert(audit_log).values(
        id=row_id,
        story_id=story_id,
        principal=principal,
        kind=kind.value,
        policy_ids=list(policy_ids),
        details=dict(details or {}),
    )
    if isinstance(db, AsyncConnection):
        await db.execute(statement)
    else:
        async with db.begin() as conn:
            await conn.execute(statement)
    return row_id


async def detach_story(conn: AsyncConnection, story_id: str) -> int:
    """Null ``story_id`` on the story's audit rows (story deletion): the rows outlive it."""
    result = await conn.execute(
        update(audit_log).where(audit_log.c.story_id == story_id).values(story_id=None)
    )
    return result.rowcount


__all__ = ["AuditKind", "detach_story", "record"]
