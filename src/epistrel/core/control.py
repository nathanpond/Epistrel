"""Committed ``control`` operations for writes that run no ``Operation`` (the #61 pattern).

A deployment-scoped write — a configuration change (``core.config_changes``) or a catalog
mutation (``core.catalog``, #103) — is one transaction of plain row writes. It is recorded by a
``control`` operations row inserted **committed** in that same transaction, with the acting
principal, and the running idempotent call's ``Idempotency-Key`` is bound to it there
(``IdempotentCall.bind_configuration``): the key names the operation exactly when the write
committed. A refused write rolls back and leaves no operation, and a write that changes nothing
records none (its key keeps ``operation_id`` null, so a late retry's reference is ``null``).

Like every operation (FR-CONC-14; #214), the row records the configuration revisions the write
ran under (``config_revisions``): the story's and the deployment's (and the story rating's
version) for a story-scoped write, the deployment's for any other, read in the write's own
transaction, so a configuration change's operation names the revision it wrote.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import idempotency
from epistrel.core.enums import OperationKind, OperationStatus
from epistrel.core.ids import new_ulid
from epistrel.core.tables import deployment_state, operations
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID


async def record_control_operation(
    conn: AsyncConnection, *, actor: str, story_id: str | None = None
) -> str:
    """Insert a committed ``control`` operation by ``actor`` (story-less unless ``story_id``)
    in ``conn``'s transaction, bind the running call's key to it, and return its id."""
    operation_id = new_ulid()
    call = idempotency.current_call()
    now = func.transaction_timestamp()
    revisions = await _revisions(conn, story_id)
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
            config_revisions=revisions,
        )
    )
    if call is not None:
        await call.bind_configuration(conn, operation_id=operation_id)
    return operation_id


async def _revisions(conn: AsyncConnection, story_id: str | None) -> dict[str, Any] | None:
    """The configuration revisions a control write runs under (see the module docstring)."""
    if story_id is not None:
        from epistrel.core import config_dependencies  # here: it imports the ratings

        found = await config_dependencies.capture(conn, story_id, ())
        if found is not None:
            return found
    deployment = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return None if deployment is None else {"deployment": int(deployment)}


__all__ = ["record_control_operation"]
