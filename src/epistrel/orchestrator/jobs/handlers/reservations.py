"""State reservation expiry (FR-STATE-14, FR-EXT-22, NFR-REL-5; #216): the
``reservation_expiry`` timer kind.

A turn that holds a reservation on a State with ``reservation_expiry_minutes`` schedules one
timer for it in its own commit (``orchestrator.structured.effects.apply_declared``; the target
is the ``held`` change's id, due that many minutes after the intent commit). When it fires,
``expire`` releases the hold (``core.states.contingency.release``: ``held → released``) if it
is still held and its operation still active; a converted, released or discarded row is left
alone, so the release happens exactly once however often the timer fires (two workers racing,
a restart's recovery pass). The released hold is listed for reconciliation as
``reservation_expired`` while its external effect has no settled answer
(``core.outbox.listings``), and it never converts automatically: a confirmation that arrives
later is #218's ``late_success_after_expiry``.

The release writes no operation and bumps no revision: it changes no value (a held row is not
in the value), only what later turns may spend. Log lines carry ids only (invariant 3).
"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.states import contingency
from epistrel.core.tables.states import state_changes
from epistrel.orchestrator.jobs.handlers.outbox import LeasePayload
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

logger = logging.getLogger(__name__)

RESERVATION_EXPIRY = "reservation_expiry"
"""The timer (and job) kind: its target is the ``held`` change's id."""
RELEASED = "released"
SKIPPED = "skipped"


async def expire(db: AsyncEngine, change_id: str) -> str:
    """Release the hold ``change_id`` when it is still held and active (see the module
    docstring): ``released``, or ``skipped`` when there is nothing to release."""
    async with db.begin() as conn:
        c = state_changes.c
        story_id = await conn.scalar(select(c.story_id).where(c.id == change_id).with_for_update())
        if story_id is None:
            return SKIPPED
        moved = await contingency.release(conn, str(story_id), change_id)
    logger.info("reservation %s: %s", change_id, RELEASED if moved else SKIPPED)
    return RELEASED if moved else SKIPPED


async def _expire(ctx: JobContext) -> JobOutput:
    payload = cast(LeasePayload, ctx.payload)
    return JobOutput(result={"outcome": await expire(ctx.db, payload.target)})


RESERVATION_EXPIRY_HANDLER = JobHandler(
    kind=RESERVATION_EXPIRY, payload=LeasePayload, run=_expire, records_operation=False
)


__all__ = ["RELEASED", "RESERVATION_EXPIRY", "RESERVATION_EXPIRY_HANDLER", "SKIPPED", "expire"]
