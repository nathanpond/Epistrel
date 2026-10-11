"""FR-CONC-2: no database lock is held while the model runs; the commit locks only briefly."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from tests.concurrency_support import start_turn
from tests.event_support import new_story, play_turn

STORY_LOCKS = (
    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
    "WHERE c.relname = 'stories' AND l.mode = 'RowShareLock' AND l.pid <> pg_backend_pid()"
)
IN_TRANSACTION = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
)
STATE_OF = "SELECT state FROM pg_stat_activity WHERE pid = :pid"


async def _probe(engine: AsyncEngine, pid: int | None = None) -> dict[str, Any]:
    """Look at the server from a separate connection."""
    async with engine.connect() as probe:
        found = {
            "story_locks": await probe.scalar(text(STORY_LOCKS)),
            "in_transaction": await probe.scalar(text(IN_TRANSACTION)),
        }
        if pid is not None:
            found["state"] = await probe.scalar(text(STATE_OF), {"pid": pid})
        return found


async def test_no_lock_across_model_call(engine: AsyncEngine) -> None:
    """While the stub model runs, no backend holds a lock on the story row or sits in an open
    transaction, and the turn's own connection is `idle`. Complement: inside the commit the
    probe sees the story row lock, so the probe can see one."""
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    during: list[dict[str, Any]] = []

    async with engine.connect() as conn:
        pid = int(await conn.scalar(text("SELECT pg_backend_pid()")) or 0)
        await conn.rollback()

        async def on_call(op: Operation) -> None:
            during.append(await _probe(engine, pid))

        held = await start_turn(conn, story, "two", on_call=on_call)
        during.append(await _probe(engine, pid))
        await held.release()
        await held.task

    assert during == [{"story_locks": 0, "in_transaction": 0, "state": "idle"}] * 2

    committing: list[dict[str, Any]] = []

    async def step(conn: AsyncConnection) -> None:
        committing.append(await _probe(engine))

    async with Operation(engine, story_id=story, kind=OperationKind.CONTROL, principal="p") as op:
        op.add_commit_step(step)
        await op.commit()
    assert committing[0]["story_locks"] >= 1
