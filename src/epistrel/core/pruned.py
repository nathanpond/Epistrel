"""Refusing to bring back what a pruning removed (FR-STORE-18,28; #247): an untruncate or swap
that needs a ``pruned`` operation is 409 ``pruned``. Kept apart from ``core.prune`` so the
timeline reads it without importing the feedback marking (the ``feedback-is-flag-only``
contract)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership
from epistrel.core.errors import Pruned
from epistrel.core.tables import events, operations


async def assert_not_pruned(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> None:
    """409 ``pruned`` when any of ``operation_ids`` was pruned (an untruncate, a swap)."""
    if not operation_ids:
        return
    o = operations.c
    found = await conn.scalar(
        select(o.operation_id)
        .where(
            o.story_id == story_id,
            o.operation_id.in_(list(operation_ids)),
            o.membership == Membership.PRUNED.value,
        )
        .limit(1)
    )
    if found is not None:
        raise Pruned(
            "the records this needs were pruned", {"operation_id": str(found), "story_id": story_id}
        )


async def assert_truncation_not_pruned(
    conn: AsyncConnection, story_id: str, named: str | None = None
) -> None:
    """409 ``pruned`` when the most recent truncation (or ``named``) discarded operations a
    pruning has since removed (an untruncate cannot bring them back)."""
    e = events.c
    query = select(e.operation_id, e.payload).where(e.story_id == story_id, e.type == "truncate")
    if named is not None:
        query = query.where(e.operation_id == named)
    latest = (await conn.execute(query.order_by(e.seq.desc()).limit(1))).one_or_none()
    if latest is None:
        return
    ids = [str(i) for i in dict(latest.payload or {}).get("operation_ids", [])]
    await assert_not_pruned(conn, story_id, ids)


__all__ = ["assert_not_pruned", "assert_truncation_not_pruned"]
