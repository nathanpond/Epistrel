"""State effects contingent on external ones (FR-STATE-14, FR-STATE-12's cross-system
transfers, FR-EXT-21's settlement position; docs/02 §7.1; #216).

**Classification** (``contingency_of``, ``classify``). In a turn that also requests external
effects (an effect on a State an external provider owns: #209's consumer requests and #210's
outbox rows), every story-owned State effect is one of three kinds:

- ``unconditional``: committed at intent (``status = committed``);
- a ``reservation``: written at intent as a hold (``status = held``): not in the value
  (``value_of`` folds ``committed`` rows only), but unavailable for other use. A held decrease of
  a number counts against what the turn's dry-run may spend (``held_amounts``), and a held item
  (a State that tracks an entity, ``entity_id``) fails #189's ``availability`` precondition for
  everyone but its beneficiary (``held_items``, ``core.states.preconditions.Canon.reserved``);
- ``conditional`` on named external effects: written at intent as ``pending``, with no value
  impact, and narration may not assert it (#209's outcome rule, ``Pending.grant``).

The render may declare an effect's ``contingency`` and its ``prerequisites`` (the States of the
turn's external effects it waits on). Undeclared, a transfer with one external and one
story-owned side is classified by the story-owned side's direction (#188's): incoming is
``conditional`` on the external side (a sword bought with an external payment is granted only on
confirmation) and outgoing a ``reservation`` (a sword sold for an external payout is held). Any
other effect is ``unconditional``, and so is every effect of a turn that requests no external
effect. A declared ``reservation`` holds a decrease only: on an increase or a ``set`` it is
``conditional``. Prerequisites are the cited external effects, else the transfer's own external
side, else every external effect of the turn.

**Settlement** (``settle(conn, op, outcomes, position)``), called by #212's outcome transaction
(at the outcome step's position) and #217's late settlement (at ``N⁺k``), for every active
``held`` or ``pending`` row whose prerequisites ``outcomes`` all decide:

- every prerequisite ``confirmed``: the intent row is marked ``converted`` (bookkeeping, no value
  impact) and a **new** ``committed`` row is written at ``position`` under ``op`` (its
  membership is the settling operation's), ``source = settlement``, with the intent row's
  prerequisites and ``settles_change_id`` naming it. Settlement never backdates: a view through
  any position before ``position`` never includes the change, and discarding the settling
  operation drops it. A new row the State's bounds now refuse is ``conflict`` (#217 lists it
  ``settlement_conflict``) and nothing changes; so is one the caller's ``check`` refuses (#214:
  #212's outcome transaction revalidates the grant's authorization under the current
  configuration, ``orchestrator.structured.rebase.revalidate_settlement``, and lists the
  conflict ``settlement_conflict`` itself);
- any prerequisite ``failed``: ``held → released``, ``pending → dropped``; no new row;
- otherwise (``unknown``, a timeout, or a prerequisite not decided yet): the row is left as it
  is.

A ``released``, ``dropped`` or ``converted`` row is never settled again: a confirmation after an
expired hold changes nothing here (#218's ``late_success_after_expiry``).

**Expiry** (``release``): a hold whose State sets ``reservation_expiry_minutes`` is released by
the Orchestrator's ``reservation_expiry`` timer (``orchestrator.jobs.handlers.reservations``,
scheduled by the commit path; Core never schedules it), once: the update only moves a row that
is still ``held`` and active. An expired hold never converts automatically.

Codes and ids only; this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import (
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
    StateContingency,
)
from epistrel.core.membership import Position, active_only
from epistrel.core.states.model import (
    ADDITIVE,
    StateChange,
    StateDefinition,
    Violation,
    from_json,
)
from epistrel.core.tables.states import state_changes, state_definitions

if TYPE_CHECKING:
    from epistrel.core.events.operation import Operation

CONFIRMED: Final = "confirmed"
FAILED: Final = "failed"
UNKNOWN: Final = "unknown"
Outcome = Literal["confirmed", "failed", "unknown"]

CONVERTED: Final = StateChangeStatus.CONVERTED.value
RELEASED: Final = StateChangeStatus.RELEASED.value
DROPPED: Final = StateChangeStatus.DROPPED.value
CONFLICT: Final = "conflict"
"""A confirmed settlement the State's bounds now refuse (#217 lists ``settlement_conflict``)."""
NOBODY: Final = ""
"""A hold's beneficiary when it is no character of the story (an external counterparty)."""

AT_INTENT: Mapping[StateContingency, StateChangeStatus] = {
    StateContingency.UNCONDITIONAL: StateChangeStatus.COMMITTED,
    StateContingency.RESERVATION: StateChangeStatus.HELD,
    StateContingency.CONDITIONAL: StateChangeStatus.PENDING,
}
"""A change's status when its turn commits, by its contingency."""
WAITING: frozenset[str] = frozenset({StateChangeStatus.HELD.value, StateChangeStatus.PENDING.value})
"""The statuses a settlement moves."""


# -- classification ------------------------------------------------------------------------------


def contingency_of(
    *,
    declared: str | None,
    delta: Decimal | None,
    transfer: bool,
    counterparty_external: bool,
    turn_external: bool,
) -> StateContingency:
    """The contingency of one story-owned side of an effect (see the module docstring):
    ``declared`` the render's (``None``: the default), ``delta`` the side's signed amount
    (``None``: a ``set``), ``counterparty_external`` whether the transfer's other side is an
    external value, ``turn_external`` whether the turn requests any external effect."""
    if not turn_external:
        return StateContingency.UNCONDITIONAL
    if declared is not None:
        chosen = StateContingency(declared)
        if chosen is StateContingency.RESERVATION and (delta is None or delta >= 0):
            return StateContingency.CONDITIONAL
        return chosen
    if transfer and counterparty_external and delta is not None and delta != 0:
        return StateContingency.RESERVATION if delta < 0 else StateContingency.CONDITIONAL
    return StateContingency.UNCONDITIONAL


@dataclass(frozen=True, slots=True)
class Classified:
    """One side's contingency, its status at intent and the external effects it waits on."""

    contingency: StateContingency
    prerequisites: tuple[str, ...] = ()

    @property
    def status(self) -> StateChangeStatus:
        return AT_INTENT[self.contingency]


def classify(
    *,
    declared: str | None,
    delta: Decimal | None,
    transfer: bool,
    counterparty_external: bool,
    turn_external: Sequence[str],
    own: Sequence[str] = (),
    cited: Sequence[str] = (),
) -> Classified:
    """``contingency_of`` with the prerequisites (see the module docstring): ``turn_external``
    the ids of the turn's external effects, ``own`` the effect's own external side's, ``cited``
    those of the external effects its declared ``prerequisites`` name."""
    found = contingency_of(
        declared=declared,
        delta=delta,
        transfer=transfer,
        counterparty_external=counterparty_external,
        turn_external=bool(turn_external),
    )
    if found is StateContingency.UNCONDITIONAL:
        return Classified(found)
    prerequisites = tuple(cited) or tuple(own) or tuple(turn_external)
    return Classified(found, tuple(dict.fromkeys(prerequisites)))


def decide(prerequisites: Iterable[str], outcomes: Mapping[str, str]) -> Outcome | None:
    """How a row's prerequisites settle it: ``failed`` when any failed, ``confirmed`` when every
    one is confirmed, else ``None`` (an ``unknown`` or undecided one keeps it waiting)."""
    found = [outcomes.get(p) for p in prerequisites]
    if not found:
        return None
    if FAILED in found:
        return FAILED
    if all(o == CONFIRMED for o in found):
        return CONFIRMED
    return None


# -- the holds -----------------------------------------------------------------------------------


def _waiting(story_id: str, status: str) -> list[ColumnElement[bool]]:
    c = state_changes.c
    return [c.story_id == story_id, c.status == status, active_only(state_changes)]


async def held_amounts(
    conn: AsyncConnection, story_id: str
) -> dict[tuple[str, str | None], Decimal]:
    """Each value's active ``held`` decreases, summed (negative), by ``(State name, scope key)``:
    what the dry-run may not spend again."""
    c, d = state_changes.c, state_definitions.c
    rows = await conn.execute(
        select(d.name, c.scope_key, c.delta)
        .select_from(state_changes.join(state_definitions, (d.id == c.state_id)))
        .where(
            *_waiting(story_id, StateChangeStatus.HELD.value),
            d.story_id == story_id,
            c.kind.in_([k.value for k in ADDITIVE]),
        )
    )
    found: dict[tuple[str, str | None], Decimal] = {}
    for r in rows:
        if r.delta is None or Decimal(r.delta) >= 0:
            continue
        key = (str(r.name), None if r.scope_key is None else str(r.scope_key))
        found[key] = found.get(key, Decimal(0)) + Decimal(r.delta)
    return found


async def held_items(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    """Each held item (the entity of a State whose active ``held`` row decreases it) and its
    beneficiary: the character a story-owned other side of the transfer goes to, else
    ``NOBODY`` (an external counterparty). #189's ``Canon.reserved``."""
    c, d = state_changes.c, state_definitions.c
    rows = list(
        await conn.execute(
            select(c.id, c.operation_id, c.beat, c.step, d.entity_id)
            .select_from(state_changes.join(state_definitions, (d.id == c.state_id)))
            .where(
                *_waiting(story_id, StateChangeStatus.HELD.value),
                d.story_id == story_id,
                d.entity_id.is_not(None),
                c.delta < 0,
            )
            .order_by(c.id)
        )
    )
    found: dict[str, str] = {}
    for r in rows:
        sibling = await conn.scalar(
            select(c.scope_key).where(
                c.story_id == story_id,
                c.operation_id == r.operation_id,
                c.beat == r.beat,
                c.step == r.step,
                c.kind == StateChangeKind.TRANSFER_IN.value,
                c.id != r.id,
            )
        )
        found[str(r.entity_id)] = NOBODY if sibling is None else str(sibling)
    return found


async def release(conn: AsyncConnection, story_id: str, change_id: str) -> bool:
    """Release the hold ``change_id`` (``held → released``) when it is still held and active;
    answers whether it moved (a second call, or a converted row, changes nothing)."""
    c = state_changes.c
    moved = await conn.scalar(
        update(state_changes)
        .where(c.id == change_id, *_waiting(story_id, StateChangeStatus.HELD.value))
        .values(status=StateChangeStatus.RELEASED.value)
        .returning(c.state_id)
    )
    return moved is not None


# -- settlement ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settled:
    """What a settlement did to one intent row: ``converted`` (``settlement_id`` the new
    ``committed`` row), ``released``, ``dropped`` or ``conflict`` (nothing changed)."""

    change_id: str
    state: str
    scope_key: str | None
    result: str
    settlement_id: str | None = None
    prerequisites: tuple[str, ...] = ()
    """The intent row's prerequisite effect ids (a ``conflict``'s are listed, #214)."""


SettlementCheck = Callable[[AsyncConnection, Any, StateDefinition], Awaitable[bool]]
"""``check(conn, row, definition)``: whether intent row ``row``, its prerequisites confirmed, may
be settled under ``definition`` as it is now (#214); ``False`` is a ``conflict``."""


async def settle(
    conn: AsyncConnection,
    op: Operation,
    outcomes: Mapping[str, str],
    position: Position,
    *,
    check: SettlementCheck | None = None,
) -> list[Settled]:
    """Settle every active ``held`` or ``pending`` row of ``op``'s story whose prerequisites
    ``outcomes`` (``{effect_id: confirmed | failed | unknown}``) decide, in ``op``'s
    transaction (see the module docstring); answers what it did, in position order. ``check``
    (#214) revalidates a converting row under the current configuration."""
    from epistrel.core import states  # here: this module is part of ``core.states``

    story_id = op.story_id
    if story_id is None or not outcomes:
        return []
    c = state_changes.c
    rows = list(
        await conn.execute(
            select(state_changes)
            .where(
                c.story_id == story_id,
                c.status.in_(sorted(WAITING)),
                active_only(state_changes),
                c.prerequisite_effect_ids.overlap(sorted(outcomes)),
            )
            .order_by(c.narrative_turn, c.slot_k, c.beat, c.step, c.id)
            .with_for_update()
        )
    )
    if not rows:
        return []
    by_id = {d.id: d for d in await states.definitions(conn, story_id)}
    done: list[Settled] = []
    touched: set[tuple[str, str | None]] = set()
    for row in rows:
        decision = decide(row.prerequisite_effect_ids, outcomes)
        definition = by_id.get(str(row.state_id))
        if decision is None or definition is None:
            continue
        scope_key = None if row.scope_key is None else str(row.scope_key)
        status = str(getattr(row.status, "value", row.status))
        prerequisites = tuple(str(p) for p in row.prerequisite_effect_ids)
        at = Position(
            int(row.narrative_turn), int(row.turn_variant), int(row.slot_k), int(row.beat)
        )
        if position.key() < at.key():
            raise ValueError("a settlement is never positioned before its intent (no backdating)")
        if decision == FAILED:
            result = RELEASED if status == StateChangeStatus.HELD.value else DROPPED
            await _mark(conn, str(row.id), status, result)
            touched.add((str(row.state_id), scope_key))
            done.append(
                Settled(str(row.id), definition.name, scope_key, result, None, prerequisites)
            )
            continue
        if check is not None and not await check(conn, row, definition):
            done.append(
                Settled(str(row.id), definition.name, scope_key, CONFLICT, None, prerequisites)
            )
            continue
        kind = StateChangeKind(row.kind)
        change = StateChange(
            state=definition.name,
            reason=str(row.reason),
            source=StateChangeSource.SETTLEMENT,
            kind=kind,
            value=None if row.value is None else from_json(definition.type, row.value),
            delta=None if row.delta is None else Decimal(row.delta),
            scope_key=scope_key,
            position=position,
            contingency=StateContingency(row.contingency),
            status=StateChangeStatus.COMMITTED,
            prerequisite_effect_ids=prerequisites,
            settles_change_id=str(row.id),
        )
        savepoint = await conn.begin_nested()
        applied = await states.apply_change(conn, op, change)
        if isinstance(applied, Violation):
            await savepoint.rollback()
            done.append(
                Settled(str(row.id), definition.name, scope_key, CONFLICT, None, prerequisites)
            )
            continue
        await savepoint.commit()
        await _mark(conn, str(row.id), status, CONVERTED)
        touched.add((str(row.state_id), scope_key))
        done.append(
            Settled(
                str(row.id), definition.name, scope_key, CONVERTED, applied.change_id, prerequisites
            )
        )
    if touched:
        await states.refresh(conn, story_id, sorted(touched, key=lambda k: (k[0], k[1] or "")))
    return done


async def conflicted(
    conn: AsyncConnection, story_id: str, outcomes: Mapping[str, str]
) -> list[Settled]:
    """The ``conflict``s a settlement of ``outcomes`` left (#214), read back: each active
    ``held`` or ``pending`` row that ``outcomes`` decide ``confirmed`` and that is still waiting
    (a settlement converts or releases every other one), in position order. A resumed staged
    turn's completion narrates them as held pending (#213)."""
    from epistrel.core import states  # here: this module is part of ``core.states``

    if not outcomes:
        return []
    c = state_changes.c
    rows = list(
        await conn.execute(
            select(state_changes)
            .where(
                c.story_id == story_id,
                c.status.in_(sorted(WAITING)),
                active_only(state_changes),
                c.prerequisite_effect_ids.overlap(sorted(outcomes)),
            )
            .order_by(c.narrative_turn, c.slot_k, c.beat, c.step, c.id)
        )
    )
    if not rows:
        return []
    by_id = {d.id: d for d in await states.definitions(conn, story_id)}
    found: list[Settled] = []
    for row in rows:
        definition = by_id.get(str(row.state_id))
        if definition is None or decide(row.prerequisite_effect_ids, outcomes) != CONFIRMED:
            continue
        found.append(
            Settled(
                str(row.id),
                definition.name,
                None if row.scope_key is None else str(row.scope_key),
                CONFLICT,
                None,
                tuple(str(p) for p in row.prerequisite_effect_ids),
            )
        )
    return found


async def _mark(conn: AsyncConnection, change_id: str, status: str, result: str) -> None:
    c = state_changes.c
    await conn.execute(
        update(state_changes).where(c.id == change_id, c.status == status).values(status=result)
    )


__all__ = [
    "AT_INTENT",
    "CONFIRMED",
    "CONFLICT",
    "CONVERTED",
    "DROPPED",
    "FAILED",
    "NOBODY",
    "RELEASED",
    "UNKNOWN",
    "WAITING",
    "Classified",
    "Outcome",
    "Settled",
    "SettlementCheck",
    "classify",
    "conflicted",
    "contingency_of",
    "decide",
    "held_amounts",
    "held_items",
    "release",
    "settle",
]
