"""Operations a crash left ``in_flight`` (NFR-REL-1, NFR-REL-5; #75).

A process that dies mid-operation leaves its ``operations`` row ``in_flight``: nothing of it was
committed (the commit is one transaction), but the row says it still runs. A turn is reclaimed
by the next request for the story's slot (``turn_gate``); a control action, a story's creation
or a rebuild has no slot, and until #75 stayed ``in_flight`` for ever.

``fail_orphans`` runs in the worker's start-up recovery pass. It first reclaims every turn slot
whose lease ran out (``turn_gate``'s own reclaim: a queued turn goes back to its queue, any
other turn fails ``turn_interrupted``). Then it marks ``failed`` every operation that is still
``in_flight``, began more than ``grace_s`` ago, and that nothing alive still claims:

- no live turn slot names it, and no ``turn_queue`` row holds it (the drainer runs those);
- no ``in_flight`` idempotency key with a live lease covers it: its own key, any key of its
  story, or, for an operation recorded without a key, any deployment-scoped key;
- no ``running`` job with a live lease works on its story (or on none).

A live call renews its key's lease, a live turn its slot's and a live job its own, so a
running operation always has a claim. The rule is deliberately conservative: an operation it
leaves alone is swept by a later pass. Its key is left to the retry (or ``idempotency_purge``).
Running it again changes nothing.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, exists, func, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import turn_gate
from epistrel.core.enums import OperationStatus
from epistrel.core.idempotency import KeyStatus, Scope
from epistrel.core.tables import idempotency_keys, jobs, operations, stories, turn_queue

GRACE_S = 60.0
"""How long an unclaimed operation must have been running before it counts as orphaned (the
idempotency lease, ``idempotency.LEASE_S``). Fault tests shorten it."""


async def reclaim_expired_slots(db: AsyncEngine) -> list[str]:
    """Reclaim every turn slot whose lease ran out; the reclaimed turns' operation ids."""
    async with db.connect() as conn:
        held = (
            await conn.execute(
                select(stories.c.id, stories.c.in_flight_turn).where(
                    stories.c.in_flight_turn.is_not(None),
                    stories.c.in_flight_lease_until <= func.clock_timestamp(),
                )
            )
        ).all()
    reclaimed: list[str] = []
    for story_id, holder in held:
        async with db.begin() as conn:
            locked = (
                await conn.execute(
                    select(stories.c.in_flight_turn)
                    .where(
                        stories.c.id == story_id,
                        stories.c.in_flight_turn == holder,
                        stories.c.in_flight_lease_until <= func.clock_timestamp(),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).one_or_none()
            if locked is not None:
                await turn_gate.reclaim(conn, story_id, holder)
                reclaimed.append(holder)
    return reclaimed


def _unclaimed(grace_s: float) -> ColumnElement[bool]:
    now = func.clock_timestamp()
    op = operations.c
    slot = exists().where(
        stories.c.in_flight_turn == op.operation_id, stories.c.in_flight_lease_until > now
    )
    queued = exists().where(turn_queue.c.operation_id == op.operation_id)
    key = exists().where(
        idempotency_keys.c.status == KeyStatus.IN_FLIGHT.value,
        idempotency_keys.c.lease_until > now,
        or_(
            idempotency_keys.c.key == op.idempotency_key,
            and_(
                idempotency_keys.c.scope == Scope.STORY.value,
                idempotency_keys.c.scope_id == op.story_id,
            ),
            and_(
                idempotency_keys.c.scope == Scope.DEPLOYMENT.value,
                op.idempotency_key.is_(None),
            ),
        ),
    )
    job = exists().where(
        jobs.c.status == "running",
        jobs.c.lease_until > now,
        or_(jobs.c.story_id == op.story_id, jobs.c.story_id.is_(None)),
    )
    return and_(
        op.status == OperationStatus.IN_FLIGHT.value,
        op.started_at < now - literal_column(f"make_interval(secs => {float(grace_s)!r})"),
        ~slot,
        ~queued,
        ~key,
        ~job,
    )


async def fail_orphans(db: AsyncEngine, *, grace_s: float | None = None) -> list[str]:
    """Reclaim expired turn slots, then mark every orphaned operation ``failed`` (see the
    module docstring); the operation ids it changed."""
    changed = await reclaim_expired_slots(db)
    grace = GRACE_S if grace_s is None else grace_s
    async with db.begin() as conn:
        swept = list(
            await conn.scalars(
                update(operations)
                .where(_unclaimed(grace))
                .values(status=OperationStatus.FAILED.value)
                .returning(operations.c.operation_id)
            )
        )
    return changed + swept


__all__ = ["GRACE_S", "fail_orphans", "reclaim_expired_slots"]
