"""External effects follow the timeline that made them (FR-EXT-12,13,14; docs/02 §4.8; #211).

A write-capable provider's effect is an ``external_effect_outbox`` row (#210) written by a turn.
When that turn leaves the active timeline, or comes back to it, the row is classified here, in
the transaction of the membership flip:

- **Leaving** (``classify_discarded`` with ``to`` ``discarded`` or ``dormant``: a truncation, a
  regeneration retiring the active variant, a swap away, a turn written over a rewound tail):
  an ``intended`` row becomes ``cancelled`` and is never sent. The rows are locked first
  (``FOR UPDATE``), so a dispatcher claim racing the flip either took the lock first (the row
  is ``claimed`` when the flip reads it, and is treated as claimed) or skips the locked row
  (``SKIP LOCKED``) and, once the flip commits, finds it ``cancelled``. A ``claimed``,
  ``unknown`` or ``confirmed`` row cannot be undone: it is marked ``reconciliation_required``
  (``core.outbox.mark``), so the reconciliation status lists it until an explicit action
  resolves it (#218), and it is collected for the operation's response.
- **Coming back** (``to`` ``active``: an untruncate, a swap back): a ``cancelled`` row is not
  sent again (nothing re-dispatches a cancelled row); it is marked and collected as
  ``cancelled_on_reactivation``.
- **A staged turn** (#212, FR-EXT-17,19) that leaves the active timeline before its completion
  (``turn_stage`` ``intent_committed``, ``awaiting_outcome`` or ``outcome_recorded``) is
  ``abandoned``: its remaining stages never commit (a later stage's continuation is refused),
  and its rows are classified as above.

Nothing here writes an outbox row: no compensation is ever issued automatically. ``superseded``
never arrives through a flip (``core.membership.FLIP_TARGETS``): only #143's reflections are
superseded, and they write no effect. A fork's copies (``executable = false``) are records and
are never classified.

**The collector.** A follower returns nothing (``core.membership.Follower``), so what it
classified reaches the operation through ``collecting()``, a context variable each
``Operation.commit`` attempt opens around its transaction: the committed operation's
``effects`` holds the rows (``Collected``), which truncate, untruncate, swap and regenerate
return as ``reconciliation_required`` and ``cancelled_on_reactivation``.

``pending_in_source`` lists, for a fork's response, the source's ``intended``, ``claimed`` and
``unknown`` rows the copied operations wrote (``effects_pending_in_source``).

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, any_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import outbox as core_outbox
from epistrel.core.enums import (
    UNFINISHED_TURN_STAGES,
    Membership,
    StagedTurnStage,
    StateChangeStatus,
)
from epistrel.core.membership import register_follower
from epistrel.core.tables import operations
from epistrel.core.tables.outbox import (
    CANCELLED,
    CLAIMED,
    CONFIRMED,
    INTENDED,
    UNKNOWN,
    external_effect_outbox,
)
from epistrel.core.tables.states import state_changes
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

RECONCILIATION_REQUIRED = "reconciliation_required"
CANCELLED_ON_REACTIVATION = "cancelled_on_reactivation"
IRREVERSIBLE = frozenset({CLAIMED, UNKNOWN, CONFIRMED})
"""The statuses a timeline edit cannot undo: the effect may have reached the provider."""
PENDING = (INTENDED, CLAIMED, UNKNOWN)
"""The statuses a fork's response lists as pending in its source."""


@dataclass(frozen=True, slots=True)
class RequiredEffect:
    """An effect a timeline edit could not undo (or would not re-send): the outbox row (its id
    is the provider idempotency key), its provider, value and status. ``value`` is ``None`` on
    a row an erasure redacted."""

    outbox_id: str
    provider: str
    status: str
    value: str | None = None


@dataclass(frozen=True, slots=True)
class PendingEffect:
    """A source effect pending at a fork's boundary: the source's outbox id, its status and
    its effect document."""

    outbox_id: str
    status: str
    effect: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Collected:
    """What the followers of one commit classified."""

    reconciliation_required: list[RequiredEffect] = field(default_factory=list)
    cancelled_on_reactivation: list[RequiredEffect] = field(default_factory=list)
    cancelled: int = 0


_COLLECTED: ContextVar[Collected | None] = ContextVar("epistrel_timeline_effects", default=None)


@contextmanager
def collecting() -> Iterator[Collected]:
    """Collect what ``classify_discarded`` does inside the block (a fresh ``Collected``)."""
    found = Collected()
    token = _COLLECTED.set(found)
    try:
        yield found
    finally:
        _COLLECTED.reset(token)


def _required(row: Any) -> RequiredEffect:
    return RequiredEffect(
        outbox_id=str(row.id),
        provider=str(row.provider_name),
        status=str(row.status),
        value=None if row.erased_at is not None else str(row.value_name),
    )


async def classify_discarded(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str], to: Membership
) -> None:
    """``core.membership``'s follower: classify the outbox rows of ``operation_ids`` after
    their flip to ``to`` (see the module docstring)."""
    if Membership(to) is not Membership.ACTIVE and operation_ids:
        # #212: a staged turn discarded before its completion is abandoned.
        await conn.execute(
            update(operations)
            .where(
                operations.c.operation_id.in_(list(operation_ids)),
                operations.c.turn_stage.in_(sorted(s.value for s in UNFINISHED_TURN_STAGES)),
            )
            .values(turn_stage=StagedTurnStage.ABANDONED.value)
        )
    rows = await core_outbox.locked_of(conn, story_id, operation_ids)
    if not rows:
        return
    found = _COLLECTED.get()
    if Membership(to) is Membership.ACTIVE:
        for row in rows:
            if str(row.status) != CANCELLED:
                continue
            await core_outbox.mark(conn, str(row.story_id), str(row.id), CANCELLED_ON_REACTIVATION)
            if found is not None:
                found.cancelled_on_reactivation.append(_required(row))
        return
    cancelled = await core_outbox.cancel_all(
        conn, [str(r.id) for r in rows if str(r.status) == INTENDED]
    )
    required = [r for r in rows if str(r.status) in IRREVERSIBLE]
    for row in required:
        await core_outbox.mark(conn, str(row.story_id), str(row.id), RECONCILIATION_REQUIRED)
    if found is not None:
        found.cancelled += cancelled
        found.reconciliation_required.extend(_required(r) for r in required)
    if cancelled or required:
        log.debug(
            "timeline effects: %d cancelled, %d listed reconciliation_required",
            cancelled,
            len(required),
        )


register_follower(classify_discarded)


async def pending_in_source(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> list[PendingEffect]:
    """The executable rows of ``story_id`` that ``operation_ids`` wrote and that are still
    ``intended``, ``claimed`` or ``unknown`` (read in ``conn``'s snapshot), oldest first; and
    (#217, FR-EXT-21) the ``confirmed`` ones whose late settlement ``operation_ids`` leave out
    (a contingent row of theirs ``converted`` by a settlement row no copied operation wrote):
    the fork holds their outcome only as a record, ``confirmed_unsettled``."""
    if not operation_ids:
        return []
    o = external_effect_outbox.c
    ids = list(operation_ids)
    intent = state_changes.alias("intent")
    settlement = state_changes.alias("settlement")
    unsettled = and_(
        o.status == CONFIRMED,
        exists().where(
            intent.c.story_id == o.story_id,
            intent.c.operation_id.in_(ids),
            intent.c.status == StateChangeStatus.CONVERTED.value,
            o.id == any_(intent.c.prerequisite_effect_ids),
            ~exists().where(
                settlement.c.story_id == intent.c.story_id,
                settlement.c.settles_change_id == intent.c.id,
                settlement.c.operation_id.in_(ids),
            ),
        ),
    )
    found = await conn.execute(
        select(o.id, o.status, o.effect, o.erased_at)
        .where(
            o.story_id == story_id,
            o.operation_id.in_(ids),
            o.executable.is_(True),
            or_(o.status.in_(PENDING), unsettled),
        )
        .order_by(o.created_at, o.id)
    )
    return [
        PendingEffect(
            outbox_id=str(r.id),
            status=str(r.status),
            effect={} if r.erased_at is not None else dict(r.effect or {}),
        )
        for r in found
    ]


async def with_effects(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> set[str]:
    """#598 (FR-RESP-14, FR-EXT-14): which of ``operation_ids`` have a dispatched or confirmed
    external effect (an executable row ``claimed``, ``unknown`` or ``confirmed``): a correction
    of such a turn cannot undo it, and its notices say so (``external_effects``)."""
    if not operation_ids:
        return set()
    o = external_effect_outbox.c
    found = await conn.scalars(
        select(o.operation_id)
        .where(
            o.story_id == story_id,
            o.operation_id.in_(list(operation_ids)),
            o.executable.is_(True),
            o.status.in_(sorted(IRREVERSIBLE)),
        )
        .distinct()
    )
    return {str(i) for i in found}


__all__ = [
    "CANCELLED_ON_REACTIVATION",
    "IRREVERSIBLE",
    "PENDING",
    "RECONCILIATION_REQUIRED",
    "Collected",
    "PendingEffect",
    "RequiredEffect",
    "classify_discarded",
    "collecting",
    "pending_in_source",
    "with_effects",
]
