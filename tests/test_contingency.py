"""``core.states.contingency`` (#216; FR-STATE-14, FR-STATE-12 across systems, FR-EXT-21's
settlement position). Unmarked: S-CONDITIONAL-EFFECTS, X-07 M6 and OUTBOX-SAFETY's contingency
part are the gated oracles; this pins the classification defaults, the dry-run's treatment of
pending and held sides (#189's availability included), ``settle()``'s convert/release/drop
branches, settlement by a new row with no backdating, ``settle()`` on a released row, and the
append-only trigger's settlement transitions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states
from epistrel.core.enums import (
    Membership,
    OperationKind,
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
    StateContingency,
)
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, flip_membership
from epistrel.core.states import authorize as a
from epistrel.core.states import contingency as ct
from epistrel.core.states import effects as rules
from epistrel.core.states import preconditions as pc
from epistrel.core.states.model import validate_definition
from epistrel.core.turns.beats import DeclaredEffect
from tests import canon_support as c
from tests import state_support as ss
from tests.event_support import rows
from tests.memory_support import use_structured
from tests.test_u_preconditions import CAL, MIRA, SWORD, _canon

C, R, U = StateContingency.CONDITIONAL, StateContingency.RESERVATION, StateContingency.UNCONDITIONAL
PAYMENT, PAYOUT = new_ulid(), new_ulid()

GOLD = validate_definition(
    {
        "name": "gold",
        "type": "number",
        "scope": "character",
        "initial_value": 0,
        "owner": "external",
        "provider_id": "bank",
    }
)
BLADES = validate_definition(
    {
        "name": "blades",
        "type": "number",
        "scope": "character",
        "constraints": {"non_negative": True},
        "initial_value": 0,
        "entity_id": SWORD,
        "transfer": {"counterparties": ["gold"]},
    }
)
DEFINITIONS = {d.name: d for d in (GOLD, BLADES)}


def _kind(**kw: Any) -> StateContingency:
    fields: dict[str, Any] = {
        "declared": None,
        "delta": Decimal(1),
        "transfer": True,
        "counterparty_external": True,
        "turn_external": True,
    }
    fields.update(kw)
    return ct.contingency_of(**fields)


def test_contingency_defaults() -> None:
    """A transfer against an external side: incoming conditional, outgoing a reservation; a
    non-transfer change, or a transfer between two story-owned sides, unconditional; nothing is
    contingent in a turn that requests no external effect."""
    assert _kind() is C
    assert _kind(delta=Decimal(-1)) is R
    assert _kind(transfer=False, counterparty_external=False) is U
    assert _kind(counterparty_external=False) is U
    assert _kind(turn_external=False) is U
    assert _kind(delta=Decimal(-1), turn_external=False) is U
    # A declared contingency wins; a declared reservation holds only a decrease.
    assert _kind(declared="unconditional") is U
    assert _kind(declared="conditional", delta=Decimal(-1)) is C
    assert _kind(declared="reservation", delta=Decimal(-3), transfer=False) is R
    assert _kind(declared="reservation", delta=Decimal(3)) is C
    assert _kind(declared="reservation", delta=None, transfer=False) is C
    assert _kind(declared="conditional", turn_external=False) is U
    # Statuses at intent.
    assert ct.AT_INTENT[U] is StateChangeStatus.COMMITTED
    assert ct.AT_INTENT[R] is StateChangeStatus.HELD
    assert ct.AT_INTENT[C] is StateChangeStatus.PENDING


def test_contingency_prerequisites() -> None:
    """Prerequisites: the cited external effects, else the transfer's own external side, else
    every external effect of the turn; an unconditional change waits on none."""
    turn = [PAYMENT, PAYOUT]
    common: dict[str, Any] = {
        "declared": None,
        "delta": Decimal(1),
        "transfer": True,
        "counterparty_external": True,
        "turn_external": turn,
    }
    found = ct.classify(**common, own=[PAYMENT])
    assert (found.contingency, found.status, found.prerequisites) == (
        C,
        StateChangeStatus.PENDING,
        (PAYMENT,),
    )
    assert ct.classify(**common, own=[PAYMENT], cited=[PAYOUT]).prerequisites == (PAYOUT,)
    assert ct.classify(**common).prerequisites == (PAYMENT, PAYOUT)
    plain = ct.classify(**{**common, "transfer": False, "counterparty_external": False})
    assert (plain.contingency, plain.prerequisites) == (U, ())
    assert ct.classify(**{**common, "turn_external": []}, own=[PAYMENT]).prerequisites == ()


def test_contingency_decide() -> None:
    """Partial success settles each row by its own prerequisites."""
    assert ct.decide([PAYMENT], {PAYMENT: "confirmed"}) == ct.CONFIRMED
    assert ct.decide([PAYMENT, PAYOUT], {PAYMENT: "confirmed", PAYOUT: "failed"}) == ct.FAILED
    assert ct.decide([PAYMENT, PAYOUT], {PAYMENT: "confirmed"}) is None
    assert ct.decide([PAYMENT], {PAYMENT: "unknown"}) is None
    assert ct.decide([], {PAYMENT: "confirmed"}) is None


def _planned(beat: int, effect: DeclaredEffect, actor: str | None = None) -> rules.Planned:
    return rules.Planned(beat, 0, actor, effect, a.Source("director"))


def _blades(delta: int, *, to: str | None = None, gold: int | None = None) -> DeclaredEffect:
    """Cal's blades by ``delta``: against the external gold (``gold``), or to ``to``'s."""
    if gold is not None:
        other: tuple[str, str | None, Any] = ("gold", CAL, gold)
    else:
        other = ("blades", to, None)
    return DeclaredEffect(
        state="blades",
        reason="a blade",
        scope_key=CAL,
        delta=delta,
        kind="transfer",
        counterparty=other,
    )


def test_dry_run_pending_and_held() -> None:
    """A pending grant adds nothing the later effects see (the sword is not Cal's yet); a held
    sale takes the sword out of what the later effects may spend; an unconditional effect in
    the same turn commits as usual."""
    bought = _planned(2, _blades(1, gold=-500))
    passed, refused = rules.dry_run(
        DEFINITIONS, {}, [bought, _planned(3, _blades(-1, to=MIRA))], turn_external=True
    )
    assert passed[0].new == passed[0].old == Decimal(0)  # pending: no value impact
    assert [f.constraint for f in refused] == ["non_negative"]
    # Complement: in a turn with no external effect the same grant counts.
    passed, refused = rules.dry_run(
        DEFINITIONS, {}, [bought, _planned(3, _blades(-1, to=MIRA))], turn_external=False
    )
    assert refused == []
    assert passed[0].new == Decimal(1)
    # A held sale: the later give-away is refused (the bounds, with no preconditions read).
    held: dict[tuple[str, str | None], Any] = {("blades", CAL): Decimal(1)}
    sold = _planned(2, _blades(-1, gold=500))
    passed, refused = rules.dry_run(
        DEFINITIONS, held, [sold, _planned(3, _blades(-1, to=MIRA))], turn_external=True
    )
    assert passed[0].new == Decimal(0)
    assert [f.constraint for f in refused] == ["non_negative"]


def test_reservation_availability() -> None:
    """#189's availability: an item held by an earlier turn (``Canon.reserved``) or by the turn
    itself (``Context.reserve``) cannot be spent by anyone but its beneficiary."""
    canon = _canon()
    canon.reserved = {SWORD: ct.NOBODY}
    context = pc.Context(canon, {})
    away = a.StateEffect(
        state="blades",
        scope_key=CAL,
        kind=a.TRANSFER,
        delta=Decimal(-1),
        counterparty=a.Counterparty("blades", MIRA),
        beat=3,
    )
    found = pc.evaluate(away, a.Source("character", MIRA), BLADES, context)
    assert found is not None
    assert found.reason == pc.reason_of(pc.AVAILABILITY)
    # Its beneficiary may take it.
    canon.reserved = {SWORD: MIRA}
    assert pc.evaluate(away, a.Source("character", MIRA), BLADES, context) is None
    # Nothing held: nothing refused.
    canon.reserved = {}
    assert pc.evaluate(away, a.Source("character", MIRA), BLADES, context) is None
    # The turn's own reservation, laid over canon by the dry-run, refuses a later beat's.
    sold = _planned(2, _blades(-1, gold=500))
    gift = rules.Planned(3, 0, MIRA, _blades(-1, to=MIRA), a.Source("character", MIRA))
    _, refused = rules.dry_run(
        DEFINITIONS,
        {("blades", CAL): Decimal(2)},
        [sold, gift],
        preconditions=pc.Context(canon, {}),
        turn_external=True,
    )
    assert [(f.tag, f.constraint) for f in refused] == [
        (rules.STATE_UNPERMITTED, pc.reason_of(pc.AVAILABILITY))
    ]


def test_grant_prose() -> None:
    """A pending grant may not be narrated as held; handing over the coins may be."""
    grant = rules.Pending("blades", None, CAL, ("blade", "sword"), Decimal(0), Decimal(1), True)
    assert rules.outcome_spans("Cal now holds the sword.", [grant])
    assert rules.outcome_spans("The sword is his now.", [grant])
    assert not rules.outcome_spans("Cal hands over the coins for the sword.", [grant])
    assert not rules.outcome_spans("Cal counts out the coins for the sword.", [grant])
    assert not rules.outcome_spans("Cal reaches for the sword.", [grant])
    assert not rules.outcome_spans("If the payment clears, the sword is his.", [grant])
    # Not a grant (an external request): possession words alone do not assert it.
    request = rules.Pending("blades", None, CAL, ("blade", "sword"), Decimal(0), Decimal(1))
    assert not rules.outcome_spans("Cal holds the sword.", [request])


# -- on Postgres ---------------------------------------------------------------------------------


async def _story(engine: AsyncEngine) -> tuple[str, str]:
    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(engine, story, ss.gold(authorization={"sources": ["any_character"]}))
    return story, cal


async def _settle(
    engine: AsyncEngine, story: str, outcomes: dict[str, str], position: Position
) -> tuple[Operation, list[ct.Settled]]:
    async def write(conn: AsyncConnection, op: Operation) -> list[ct.Settled]:
        return await ct.settle(conn, op, outcomes, position)

    op, found = await c.run(engine, story, write, kind=OperationKind.CONTROL, turn=position.turn)
    return op, list(found)


def _waiting(status: StateChangeStatus, amount: int, cal: str, *ids: str) -> Any:
    return ss.delta(
        "gold",
        amount,
        scope_key=cal,
        contingency=R if status is StateChangeStatus.HELD else C,
        status=status,
        prerequisite_effect_ids=ids,
    )


async def _status(engine: AsyncEngine, change_id: str) -> str:
    (row,) = await rows(engine, "SELECT status FROM state_changes WHERE id = :i", i=change_id)
    return str(row.status)


@pytest.mark.integration
async def test_settle_by_new_row(engine: AsyncEngine, states_delivered: None) -> None:
    """A pending row written at turn 10 and settled at ``14⁺1`` is absent from ``value_of``
    through 14 and present through ``14⁺1``: the intent row is marked ``converted`` and a new
    ``committed`` row (``source = settlement``) carries the change at the settling position;
    discarding the settling operation removes it. Never backdated: a settlement positioned
    before its intent is refused."""
    story, cal = await _story(engine)
    await ss.change(engine, story, ss.set_to("gold", 100, scope_key=cal))
    while True:
        op, _ = await ss.change(engine, story, ss.delta("gold", 0, scope_key=cal))
        if op.narrative_turn == 9:
            break
    intent_op, (intent,) = await ss.change(
        engine, story, _waiting(StateChangeStatus.PENDING, 50, cal, PAYMENT)
    )
    assert intent_op.narrative_turn == 10
    assert isinstance(intent, states.Applied)
    while op.narrative_turn != 14:
        op, _ = await ss.change(engine, story, ss.delta("gold", 0, scope_key=cal))
    assert await ss.value(engine, story, "gold", cal) == 100

    # Never backdated.
    with pytest.raises(ValueError, match="backdating"):
        await _settle(engine, story, {PAYMENT: "confirmed"}, Position(9))
    assert await _status(engine, intent.change_id) == "pending"

    slot = Position(14, op.turn_variant or 0, 1)
    settling, (done,) = await _settle(engine, story, {PAYMENT: "confirmed"}, slot)
    assert (done.change_id, done.result) == (intent.change_id, ct.CONVERTED)
    assert await _status(engine, intent.change_id) == "converted"
    (new,) = await rows(engine, "SELECT * FROM state_changes WHERE id = :i", i=done.settlement_id)
    assert (new.status, new.source, new.settles_change_id) == (
        "committed",
        "settlement",
        intent.change_id,
    )
    assert (new.narrative_turn, new.slot_k, new.operation_id) == (14, 1, settling.operation_id)
    assert list(new.prerequisite_effect_ids) == [PAYMENT]
    assert await ss.value(engine, story, "gold", cal, through=ss.at(10)) == 100
    assert await ss.value(engine, story, "gold", cal, through=ss.at(14)) == 100
    assert await ss.value(engine, story, "gold", cal, through=ss.at(14, 1)) == 150
    assert await ss.value(engine, story, "gold", cal) == 150
    # Settled once: a second confirmation finds nothing waiting.
    assert (await _settle(engine, story, {PAYMENT: "confirmed"}, slot))[1] == []

    # Discarding the settling operation drops the change; the intent row stays converted.
    async with engine.begin() as conn:
        await flip_membership(conn, [settling.operation_id], Membership.DISCARDED, story_id=story)
        await states.refresh(conn, story, [(new.state_id, cal)])
    assert await ss.value(engine, story, "gold", cal) == 100
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)


@pytest.mark.integration
async def test_settle_release_drop_unknown(engine: AsyncEngine, states_delivered: None) -> None:
    """A failed prerequisite releases a hold and drops a conditional row, with no new row;
    ``unknown`` (or an undecided prerequisite) leaves them; partial success settles each row by
    its own prerequisites. ``settle()`` on a released row changes nothing (a confirmation after
    an expired hold is #218's)."""
    story, cal = await _story(engine)
    await ss.change(engine, story, ss.set_to("gold", 100, scope_key=cal))
    smith, stable = new_ulid(), new_ulid()
    op, found = await ss.change(
        engine,
        story,
        _waiting(StateChangeStatus.HELD, -30, cal, smith),
        _waiting(StateChangeStatus.PENDING, 20, cal, stable),
        _waiting(StateChangeStatus.PENDING, 5, cal, smith, stable),
    )
    held, pending, both = (f.change_id for f in found if isinstance(f, states.Applied))
    turn = op.narrative_turn or 0
    count = len(await ss.change_rows(engine, story))
    slot = Position(turn, op.turn_variant or 0, 1)

    # Unknown: nothing moves.
    assert (await _settle(engine, story, {smith: "unknown", stable: "unknown"}, slot))[1] == []
    assert [await _status(engine, i) for i in (held, pending, both)] == [
        "held",
        "pending",
        "pending",
    ]
    # Partial success: the smith's payment fails, the stable's is undecided.
    _, done = await _settle(engine, story, {smith: "failed"}, slot)
    assert {(d.change_id, d.result) for d in done} == {(held, ct.RELEASED), (both, ct.DROPPED)}
    assert len(await ss.change_rows(engine, story)) == count  # no new row
    assert await _status(engine, pending) == "pending"
    _, done = await _settle(engine, story, {stable: "confirmed"}, Position(turn, 0, 2))
    assert [(d.change_id, d.result) for d in done] == [(pending, ct.CONVERTED)]
    assert await ss.value(engine, story, "gold", cal) == 120

    # A confirmation for the released hold changes nothing.
    _, done = await _settle(engine, story, {smith: "confirmed"}, Position(turn, 0, 3))
    assert done == []
    assert await _status(engine, held) == "released"
    assert await ss.value(engine, story, "gold", cal) == 120
    # ``release`` moves a held row once, and never a released one.
    async with engine.begin() as conn:
        assert await ct.release(conn, story, held) is False


@pytest.mark.integration
async def test_settlement_conflict(engine: AsyncEngine, states_delivered: None) -> None:
    """A confirmed settlement the bounds now refuse (the gold was spent meanwhile) commits
    nothing: ``conflict`` (#217 lists ``settlement_conflict``)."""
    story, cal = await _story(engine)
    await ss.change(engine, story, ss.set_to("gold", 40, scope_key=cal))
    op, (held,) = await ss.change(engine, story, _waiting(StateChangeStatus.HELD, -30, cal, PAYOUT))
    await ss.change(engine, story, ss.delta("gold", -20, scope_key=cal))
    assert isinstance(held, states.Applied)
    _, (done,) = await _settle(
        engine, story, {PAYOUT: "confirmed"}, Position((op.narrative_turn or 0) + 1, 0, 1)
    )
    assert done.result == ct.CONFLICT
    assert await _status(engine, held.change_id) == "held"
    assert await ss.value(engine, story, "gold", cal) == 20


@pytest.mark.integration
async def test_append_only_settlement_transitions(
    engine: AsyncEngine, states_delivered: None
) -> None:
    """The trigger admits a status update only along a settlement; any other is refused, and
    every other column is still refused."""
    story, cal = await _story(engine)
    await ss.change(engine, story, ss.set_to("gold", 10, scope_key=cal))
    _, found = await ss.change(
        engine,
        story,
        _waiting(StateChangeStatus.HELD, -1, cal, PAYOUT),
        ss.delta("gold", 1, scope_key=cal),
    )
    held, plain = (f.change_id for f in found if isinstance(f, states.Applied))

    async def update(change_id: str, status: str) -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE state_changes SET status = :s WHERE id = :i"),
                {"s": status, "i": change_id},
            )

    for change_id, status in ((plain, "held"), (held, "committed"), (held, "pending")):
        with pytest.raises(DBAPIError, match="append-only"):
            await update(change_id, status)
    await update(held, "released")
    with pytest.raises(DBAPIError, match="append-only"):
        await update(held, "converted")  # released is final
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE state_changes SET reason = 'x' WHERE id = :i"), {"i": plain}
            )
    assert (await rows(engine, "SELECT kind FROM state_changes WHERE id = :i", i=held))[
        0
    ].kind == StateChangeKind.DELTA.value
    assert StateChangeSource.SETTLEMENT.value == "settlement"
