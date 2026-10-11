"""Timers and recurring tasks: timed lifecycle work as rows the workers fire (NFR-REL-5; #71).

- ``schedule(conn, kind, target, due_at)`` upserts the one-shot timer ``(kind, target)`` (a
  later call moves its due time); ``cancel`` deletes it. The kind is a job kind: firing enqueues
  a job of that kind with ``{"target": target}``.
- ``fire_due(db)`` is what every worker loop runs each poll tick, and the recovery pass runs
  once at start-up. One transaction per row: claim the earliest due timer (``FOR UPDATE SKIP
  LOCKED``), delete it and enqueue its job; then, the same way, each due ``recurring_tasks`` row
  whose task is registered: enqueue its job (``coalesce_key`` ``recurring:<name>``, so a run
  still queued is not doubled), set ``last_run_at = now`` and ``next_run_at = now + interval``.
  Two workers racing for one row fire it once, and a task that missed several intervals fires
  once. A timer whose kind has no handler, or whose target the handler's payload refuses, is
  deleted and logged.
- ``reconcile(db)`` writes the registered recurring tasks into rows (a missing row is due now,
  a changed interval is updated); rows nothing is registered for are logged and left alone.

Due-ness is decided by ``core.clock.now()``, passed into the SQL, so tests move time without
sleeping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.ids import new_ulid
from epistrel.core.tables import recurring_tasks, timers
from epistrel.observability import instrument
from epistrel.orchestrator.jobs import handlers, queue
from epistrel.orchestrator.jobs.recurring import registered_tasks

logger = logging.getLogger(__name__)

FIRE_LIMIT = 100
"""The most rows of each table one ``fire_due`` call fires (the next tick takes the rest)."""


@dataclass(frozen=True, slots=True)
class Fired:
    """What one ``fire_due`` call did: the timers and recurring tasks it fired (by kind/name),
    and the timers it deleted unfired (unknown kind or refused target)."""

    timers: list[str] = field(default_factory=list)
    recurring: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# -- one-shot timers -------------------------------------------------------------------------


async def schedule(
    conn: AsyncConnection,
    kind: str,
    target: str,
    due_at: datetime,
    *,
    story_id: str | None = None,
) -> str:
    """Set timer ``(kind, target)`` to fire at ``due_at`` (in ``conn``'s transaction); returns
    its id. An existing timer for the pair is moved to ``due_at``."""
    statement = insert(timers).values(
        id=new_ulid(), kind=kind, target=target, story_id=story_id, due_at=due_at
    )
    statement = statement.on_conflict_do_update(
        index_elements=[timers.c.kind, timers.c.target],
        set_={"due_at": statement.excluded.due_at, "story_id": statement.excluded.story_id},
    )
    timer_id = await conn.scalar(statement.returning(timers.c.id))
    assert timer_id is not None  # noqa: S101 - an upsert returns its row
    return str(timer_id)


async def cancel(conn: AsyncConnection, kind: str, target: str) -> bool:
    """Delete timer ``(kind, target)``; ``False`` when there was none (already fired)."""
    gone = await conn.scalar(
        delete(timers)
        .where(timers.c.kind == kind, timers.c.target == target)
        .returning(timers.c.id)
    )
    return gone is not None


async def _fire_timer(db: AsyncEngine, now: datetime, fired: Fired) -> bool:
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(timers)
                .where(timers.c.due_at <= now)
                .order_by(timers.c.due_at, timers.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if row is None:
            return False
        await conn.execute(delete(timers).where(timers.c.id == row.id))
        handler = handlers.lookup(row.kind)
        payload = {"target": row.target}
        if handler is None:
            logger.warning("timer %s of unknown kind %s deleted unfired", row.id, row.kind)
            fired.dropped.append(row.kind)
            return True
        try:
            handler.payload.model_validate(payload)
        except ValueError:
            logger.warning("timer %s (%s): target refused, deleted unfired", row.id, row.kind)
            fired.dropped.append(row.kind)
            return True
        await queue.enqueue(
            conn,
            row.kind,
            payload,
            story_id=row.story_id,
            coalesce_key=f"timer:{row.kind}:{row.target}",
        )
        fired.timers.append(row.kind)
        return True


# -- recurring tasks -------------------------------------------------------------------------


async def _fire_recurring(db: AsyncEngine, now: datetime, names: list[str], fired: Fired) -> bool:
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(recurring_tasks.c.name)
                .where(recurring_tasks.c.next_run_at <= now, recurring_tasks.c.name.in_(names))
                .order_by(recurring_tasks.c.next_run_at, recurring_tasks.c.name)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if row is None:
            return False
        await conn.execute(
            update(recurring_tasks)
            .where(recurring_tasks.c.name == row.name)
            .values(last_run_at=now, next_run_at=now + recurring_tasks.c.interval)
        )
        await queue.enqueue(conn, row.name, {}, coalesce_key=f"recurring:{row.name}")
        fired.recurring.append(row.name)
        return True


async def fire_due(db: AsyncEngine, now: datetime | None = None) -> Fired:
    """Fire every due timer and registered recurring task (see the module docstring), as one
    unit of background work (``fire_timers``; a DEBUG line when nothing was due; #74)."""
    with instrument.background("fire_timers") as run:
        fired = await _fire_due(db, now)
        run.quiet = not (fired.timers or fired.recurring)
        return fired


async def _fire_due(db: AsyncEngine, now: datetime | None) -> Fired:
    moment = now or clock.now()
    fired = Fired()
    for _ in range(FIRE_LIMIT):
        if not await _fire_timer(db, moment, fired):
            break
    names = [name for name in registered_tasks() if handlers.lookup(name) is not None]
    if names:
        for _ in range(FIRE_LIMIT):
            if not await _fire_recurring(db, moment, names, fired):
                break
    if fired.timers or fired.recurring:
        logger.info(
            "fired %d timer(s) and recurring task(s) %s",
            len(fired.timers),
            ",".join(fired.recurring) or "-",
        )
    return fired


async def reconcile(db: AsyncEngine) -> list[str]:
    """Write every registered recurring task into ``recurring_tasks`` (missing rows due now,
    intervals updated); returns the names of rows no task is registered for (logged)."""
    tasks = registered_tasks()
    now = clock.now()
    async with db.begin() as conn:
        for task in tasks.values():
            statement = insert(recurring_tasks).values(
                name=task.name, interval=task.interval, next_run_at=now
            )
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[recurring_tasks.c.name],
                    set_={"interval": statement.excluded.interval},
                    where=recurring_tasks.c.interval != statement.excluded.interval,
                )
            )
        names = list((await conn.scalars(select(recurring_tasks.c.name))).all())
    unowned = sorted(name for name in names if name not in tasks)
    if unowned:
        logger.warning("recurring task row(s) with no registered task: %s", ",".join(unowned))
    return unowned


async def next_due(db: AsyncEngine) -> datetime | None:
    """The earliest due time of a timer or registered recurring task (``None``: neither)."""
    names = list(registered_tasks())
    async with db.connect() as conn:
        timer: datetime | None = await conn.scalar(select(func.min(timers.c.due_at)))
        task: datetime | None = (
            await conn.scalar(
                select(func.min(recurring_tasks.c.next_run_at)).where(
                    recurring_tasks.c.name.in_(names)
                )
            )
            if names
            else None
        )
    found = [moment for moment in (timer, task) if moment is not None]
    return min(found) if found else None


__all__ = ["FIRE_LIMIT", "Fired", "cancel", "fire_due", "next_due", "reconcile", "schedule"]
