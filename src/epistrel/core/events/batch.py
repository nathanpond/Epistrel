"""``OperationBatch``: several operations of one story committed all or none (#58).

Each operation keeps its own row, position and events, exactly as if it had committed alone;
the batch only shares the commit: one transaction, one story lock (``lock_timeout`` 5 s), the
operations written in order, so the second sees the first's turn index and ``seq`` values.
``before`` steps run under the lock ahead of the first operation; ``after`` steps run once
every operation is written. Any exception rolls everything back and settles every operation
(``abandoned`` after a revalidation, else ``failed``), so no event of the batch survives.

``ingest`` (#58) uses it: a multi-event body is N ``turn`` operations, all or nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.concurrency import Decide
from epistrel.core.errors import StoryNotFound
from epistrel.core.events.operation import LOCK_TIMEOUT, CommitStep, Operation
from epistrel.core.tables import stories


class OperationBatch:
    """``begin()`` every operation, then ``commit()`` them in one transaction."""

    def __init__(self, db: AsyncEngine, story_id: str, operations: Sequence[Operation]) -> None:
        if not operations:
            raise ValueError("a batch needs at least one operation")
        if any(op.story_id != story_id for op in operations):
            raise ValueError("every operation of a batch belongs to the batch's story")
        self.db = db
        self.story_id = story_id
        self.operations = tuple(operations)

    @property
    def last(self) -> Operation:
        return self.operations[-1]

    async def begin(self) -> None:
        """Insert each ``in_flight`` row; if one fails, the begun ones are marked ``failed``."""
        begun: list[Operation] = []
        try:
            for op in self.operations:
                await op.begin()
                begun.append(op)
        except BaseException as exc:
            for op in begun:
                await op.settle(exc)
            raise

    async def commit(
        self,
        *,
        decide: Decide | None = None,
        before: Sequence[CommitStep] = (),
        after: Sequence[CommitStep] = (),
    ) -> None:
        """Write every operation in one transaction under the story lock, or none of them."""
        try:
            async with self.db.connect() as conn, conn.begin():
                await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                locked = await conn.scalar(
                    select(stories.c.id).where(stories.c.id == self.story_id).with_for_update()
                )
                if locked is None:
                    raise StoryNotFound(f"story {self.story_id} does not exist")
                for step in before:
                    await step(conn)
                for op in self.operations:
                    await op.write_in(conn, decide)
                for step in after:
                    await step(conn)
        except BaseException as exc:
            for op in self.operations:
                if op.in_flight:
                    await op.settle(exc)
            raise
        for op in self.operations:
            await op.settle(None)

    async def abandon(self, cause: BaseException) -> None:
        """Give up before ``commit()`` (every begun operation settles with ``cause``)."""
        for op in self.operations:
            if op.in_flight:
                await op.settle(cause)


__all__ = ["OperationBatch"]
