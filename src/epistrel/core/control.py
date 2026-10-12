"""Committed ``control`` operations for writes that run no ``Operation`` (the #61 pattern).

A deployment-scoped write — a configuration change (``core.config_changes``) or a catalog
mutation (``core.catalog``, #103) — is one transaction of plain row writes. It is recorded by a
``control`` operations row inserted **committed** in that same transaction, with the acting
principal, and the running idempotent call's ``Idempotency-Key`` is bound to it there
(``IdempotentCall.bind_configuration``): the key names the operation exactly when the write
committed. A refused write rolls back and leaves no operation, and a write that changes nothing
records none (its key keeps ``operation_id`` null, so a late retry's reference is ``null``).
"""

from __future__ import annotations

from sqlalchemy import func, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import idempotency
from epistrel.core.enums import OperationKind, OperationStatus
from epistrel.core.ids import new_ulid
from epistrel.core.tables import operations


async def record_control_operation(
    conn: AsyncConnection, *, actor: str, story_id: str | None = None
) -> str:
    """Insert a committed ``control`` operation by ``actor`` (story-less unless ``story_id``)
    in ``conn``'s transaction, bind the running call's key to it, and return its id."""
    operation_id = new_ulid()
    call = idempotency.current_call()
    now = func.transaction_timestamp()
    await conn.execute(
        insert(operations).values(
            operation_id=operation_id,
            story_id=story_id,
            kind=OperationKind.CONTROL.value,
            principal=actor,
            status=OperationStatus.COMMITTED.value,
            membership_tx=now,
            committed_at=now,
            idempotency_key=call.key if call is not None and call.claimed else None,
        )
    )
    if call is not None:
        await call.bind_configuration(conn, operation_id=operation_id)
    return operation_id


__all__ = ["record_control_operation"]
