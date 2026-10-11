"""Drain every ``queue`` story's turn queue in order (FR-CONC-5; #63).

A ``QueueDrainer`` keeps one detached asyncio task per story with queued turns, so stories
never wait for each other. The task takes the head of the story's ``turn_queue`` as soon as
the story's turn slot is free (``turn_gate.acquire``), runs it through the handler for its
kind (``respond`` or ``ingest``, given by the Engine: the pipeline is run again from the stored
request and context, after the gates the request already passed at enqueue), and moves on once
it committed or failed. The slot's lease is renewed while a turn runs. A turn that fails is
recorded on its operation (``turn_gate.fail_item``) for the requests waiting on it.

It is woken in-process when a turn of the story finishes (``turn_gate.wakeups``) and otherwise
polls every ``turn_gate.POLL_S``, so a turn queued by another process is still started. Before
#70's worker loop exists it runs inside the serving process (``all`` mode): the Engine kicks it
when it queues a turn, and ``recover()`` (the app's start-up) drains the queues a stopped
process left behind. A drainer stopped mid-turn puts the turn back in the queue.

``await_turn`` is the waiting side: a request whose turn was queued waits (long-polls) for its
outcome up to its ``turn_deadline``, then is answered 202 (``TurnAccepted``); ``answer`` turns
the outcome into the request's result — the result stored under its ``Idempotency-Key`` (#61),
the turn's error, or a reference when the result was lost to a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock, turn_gate
from epistrel.core.enums import OperationStatus
from epistrel.core.errors import StoryNotFound
from epistrel.core.idempotency import KeyStatus, ReferenceReplay, Scope, StoredResult
from epistrel.core.tables import idempotency_keys
from epistrel.core.turn_gate import Concurrency, Outcome, QueueItem, TurnAccepted

logger = logging.getLogger(__name__)

Handler = Callable[[QueueItem, Concurrency], Awaitable[None]]
"""Runs one queued turn to its commit (or raises); the Engine provides one per kind."""

WAIT_POLL_S = 0.25
"""How often a waiting request re-reads its turn when nothing in this process woke it."""
RESULT_GRACE_S = 5.0
"""How long a waiter gives a committed turn's drainer to store the result under its key."""


class QueueDrainer:
    """One drain task per story with queued turns; see the module docstring."""

    def __init__(self, db: Callable[[], AsyncEngine], handlers: Mapping[str, Handler]) -> None:
        self._db = db
        self._handlers = dict(handlers)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: set[str] = set()

    def kick(self, story_id: str) -> None:
        """Make sure the story's queue is being drained (a no-op while its task runs)."""
        self._pending.add(story_id)
        task = self._tasks.get(story_id)
        loop = asyncio.get_running_loop()
        if task is not None and not task.done() and task.get_loop() is loop:
            turn_gate.wakeups.wake(story_id)
            return
        self._tasks[story_id] = loop.create_task(self._drain(story_id))

    async def recover(self) -> None:
        """Drain every queue left behind (the start-up recovery pass)."""
        for story_id in await turn_gate.queued_stories(self._db()):
            self.kick(story_id)

    async def stop(self) -> None:
        """Stop every drain task; a turn stopped mid-run goes back to its queue."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await task

    async def idle(self) -> None:
        """Wait until no drain task runs (tests)."""
        while running := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    async def _drain(self, story_id: str) -> None:
        db = self._db()
        try:
            while True:
                self._pending.discard(story_id)
                item = await turn_gate.head(db, story_id)
                if item is None:
                    if story_id in self._pending:
                        continue
                    return
                try:
                    concurrency = await turn_gate.read_concurrency(db, story_id)
                    held = await turn_gate.acquire(
                        db,
                        story_id,
                        item.operation_id,
                        lease_s=concurrency.lease_s,
                        newcomer=False,
                    )
                except StoryNotFound:
                    return
                if held is not None:
                    await turn_gate.wakeups.wait(story_id, turn_gate.POLL_S)
                    continue
                await self._run(db, item, concurrency)
        finally:
            if self._tasks.get(story_id) is asyncio.current_task():
                del self._tasks[story_id]

    async def _run(self, db: AsyncEngine, item: QueueItem, concurrency: Concurrency) -> None:
        story_id, operation_id = item.story_id, item.operation_id
        try:
            async with turn_gate.renewing(db, story_id, operation_id, concurrency.lease_s):
                await self._handlers[item.kind](item, concurrency)
        except asyncio.CancelledError:
            # Stopped (shutdown): a begun operation already went back to ``queued``; the slot
            # is given back so the next process starts the turn at once.
            await asyncio.shield(turn_gate.release_now(db, story_id, operation_id))
            raise
        except Exception as exc:
            logger.info("queued turn %s failed: %s", operation_id, type(exc).__name__)
            await turn_gate.fail_item(db, story_id, operation_id, exc)
        else:
            await turn_gate.close_item(db, story_id, operation_id)


# -- the waiting side ----------------------------------------------------------------------------


async def await_turn(
    db: AsyncEngine, story_id: str, operation_id: str, *, wait_s: float, key: str | None
) -> Outcome:
    """Wait for the queued turn to finish, at most ``wait_s`` seconds; then 202
    (``TurnAccepted`` with the turns still ahead of it). ``wait_s`` 0 answers 202 at once."""
    deadline = time.monotonic() + wait_s
    while True:
        outcome = await turn_gate.outcome(db, story_id, operation_id)
        if outcome.done:
            return outcome
        left = deadline - time.monotonic()
        if left <= 0:
            raise TurnAccepted(operation_id=operation_id, position=outcome.position, key=key)
        await turn_gate.wakeups.wait(story_id, min(WAIT_POLL_S, left))


async def answer(
    db: AsyncEngine,
    story_id: str,
    operation_id: str,
    outcome: Outcome,
    *,
    key: str,
    revision_field: str,
) -> StoredResult:
    """The finished turn's answer for a request under ``key``: its stored result; the turn's
    error (raised); or, when the result is gone (a crash between commit and store), the
    reference to the committed operation (``ReferenceReplay``, raised)."""
    if outcome.error_code is not None:
        raise turn_gate.stored_error(outcome)
    deadline = time.monotonic() + RESULT_GRACE_S
    while True:
        async with db.connect() as conn:
            row = (
                await conn.execute(
                    select(idempotency_keys).where(
                        idempotency_keys.c.scope == Scope.STORY.value,
                        idempotency_keys.c.scope_id == story_id,
                        idempotency_keys.c.key == key,
                    )
                )
            ).one_or_none()
        if row is not None and row.status == KeyStatus.DONE.value:
            fresh = row.expires_at is not None and row.expires_at > clock.now()
            if row.result is not None and fresh:
                return StoredResult.from_json(row.result)
            raise ReferenceReplay(
                key=key,
                operation_id=row.operation_id,
                revision=row.revision,
                revision_field=revision_field,
            )
        committed = outcome.status == OperationStatus.COMMITTED.value
        if row is None or time.monotonic() >= deadline:
            if committed:
                raise ReferenceReplay(
                    key=key,
                    operation_id=operation_id,
                    revision=None,
                    revision_field=revision_field,
                )
            raise turn_gate.stored_error(outcome)
        await asyncio.sleep(0.05)


__all__ = [
    "RESULT_GRACE_S",
    "WAIT_POLL_S",
    "Handler",
    "QueueDrainer",
    "answer",
    "await_turn",
]
