"""Post-effect versions of an external value (FR-EXT-20, FR-EXT-2; #215), at unit level.

A turn's read is version 1 and never changes. A confirmed effect adds the next version at its
own position (``orchestrator.providers.pinned.settle_value``): the confirmation's resulting
value, else one recorded post-effect read, else ``Unknown``. ``core.states.external
.pinned_value_at`` answers the latest version at or before a position, so a version at an
inter-turn slot ``N⁺k`` orders after turn N and before N+1, and the owner lookup verifies a
claim against the version at the claim's position. The end-to-end S-EFFECT-CRASH post-effect
scenario is #212's (``tests/scenario/test_s_effect_crash.py``)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import projections
from epistrel.core.enums import ExternalValueSource
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.states import external
from epistrel.core.states import verify as sv
from epistrel.core.states.external import ExternalPin, Unknown
from epistrel.core.verify import rules
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from epistrel.orchestrator.providers.port import WriteOutcome
from tests import hot_set_support as hs
from tests import provider_support as ps
from tests import state_support as ss

pytestmark = pytest.mark.integration


@dataclass
class World:
    story: str
    cal: str
    gold: Any
    provider: Any
    fake: ps.ScriptedProvider
    reads: pinned.PinnedReads


async def _world(engine: AsyncEngine, **fields: Any) -> World:
    """A story with Cal, whose gold a write-capable in-process provider owns (``fields``
    override its registration), and a ``PinnedReads`` reaching that provider."""
    name = ps.unique_name("bank")
    provider = await ps.register(engine, ps.in_process_spec(name, writable=["gold"], **fields))
    story, (cal,) = await hs.cast(engine, ["Cal"])
    await ss.set_level(engine, story, "on")
    gold = await ps.external_gold(engine, story, name)
    fake = ps.ScriptedProvider(name)
    registry = InProcessRegistry()
    registry.register(name, read=fake.read, write=fake.write)
    return World(story, cal, gold, provider, fake, pinned.PinnedReads(engine, story, registry))


def _outcome(status: str = "confirmed", resulting: dict[str, Any] | None = None) -> pinned.Outcome:
    return pinned.Outcome(
        outbox_id=new_ulid(),
        effect_id=new_ulid(),
        answer=WriteOutcome(status, resulting_values=resulting),  # type: ignore[arg-type]
        answered_at=datetime.now(UTC),
    )


async def _settle(
    engine: AsyncEngine, w: World, outcome: pinned.Outcome, position: Position, *, slot: bool
) -> ExternalPin | Unknown | None:
    """Record ``outcome`` for Cal's gold at ``position`` (in the turn's own transaction, or in
    the next inter-turn slot) and commit."""
    target = await pinned.outcome_target(engine, w.story, w.gold, w.cal, position)
    assert target is not None
    async with ps.outcome_op(engine, w.story, turn=None if slot else position.turn) as op:
        found = await pinned.settle_value(op, w.reads, target, outcome, w.provider)
        await op.commit()
    return found


async def _at(engine: AsyncEngine, w: World, position: Position) -> ExternalPin | Unknown:
    async with engine.connect() as conn:
        return await external.pinned_value_at(conn, w.story, w.gold, w.cal, position)


def _claim(w: World, amount: int) -> Any:
    return sv.proposition(str(w.gold.id), w.cal, sv.number_literal(amount))


async def _verdict(engine: AsyncEngine, w: World, amount: int, position: Position) -> Any:
    """The owner lookup's verdict on "Cal has ``amount`` gold" at ``position`` (#191)."""
    async with engine.connect() as conn:
        owned = await sv.read(conn, w.story, [w.cal], position)
    found = sv.verdict(_claim(w, amount), owned)
    return found.kind if found.reason is None else (found.kind, found.reason)


SUPPORTED, CONTRADICTED = rules.VerdictKind.SUPPORTED, rules.VerdictKind.CONTRADICTED
PROVIDER_UNKNOWN = (rules.VerdictKind.UNCERTAIN, rules.VerdictReason.PROVIDER_UNKNOWN)


async def test_pinned_value_at_orders_turn_slot_and_next_turn(
    engine: AsyncEngine, states_delivered: None
) -> None:
    """Turn N reads 300; a late confirmation (#217) at slot ``N⁺1`` says 250; an ``Unknown``
    at ``N⁺2``; turn N+1 reads 400. Each position answers the latest version at or before it
    (``Position.key()``): a slot orders after its turn and before the next one. The slot
    versions extend turn N's read (versions 2 and 3); N+1's read starts its own chain."""
    w = await _world(engine)
    turn, read_n = await ps.record_read(engine, w.story, w.gold, w.cal, 300)
    late = await _settle(
        engine, w, _outcome(resulting={"gold": 250}), Position(turn, 0, 1), slot=True
    )
    assert isinstance(late, ExternalPin)
    assert (late.value, late.version) == (Decimal(250), 2)
    w.fake.script("gold", ps.Fail())
    unknown = await _settle(engine, w, _outcome(), Position(turn, 0, 2), slot=True)
    assert isinstance(unknown, Unknown)
    assert unknown.pin is not None
    next_turn, read_next = await ps.record_read(engine, w.story, w.gold, w.cal, 400)
    assert next_turn == turn + 1

    def summary(found: ExternalPin | Unknown) -> tuple[Any, ...]:
        if isinstance(found, Unknown):
            return ("unknown", None if found.pin is None else found.pin.version)
        return (found.value, found.version, found.source.value)

    expected = [
        (Position(turn), (300, 1, "read")),
        (Position.end_of(turn), (300, 1, "read")),
        (Position(turn, 0, 1), (250, 2, "confirmation")),
        (Position(turn, 0, 1, 7, 7), (250, 2, "confirmation")),
        (Position(turn, 0, 2), ("unknown", 3)),
        (Position.end_of(turn, 2), ("unknown", 3)),
        (Position(next_turn), (400, 1, "read")),
    ]
    assert [summary(await _at(engine, w, p)) for p, _ in expected] == [e for _, e in expected]
    assert summary(await _at(engine, w, Position(turn - 1))) == ("unknown", None)

    rows = await ps.external_value_rows(engine, w.story)
    assert [(r["version"], r["base_event_id"], r["source"]) for r in rows] == [
        (1, read_n, ExternalValueSource.READ),
        (2, read_n, ExternalValueSource.CONFIRMATION),
        (3, read_n, ExternalValueSource.UNKNOWN),
        (1, read_next, ExternalValueSource.READ),
    ]
    async with engine.connect() as conn:
        after = await external.version_base(
            conn, w.story, str(w.gold.id), w.cal, Position(next_turn, 0, 0, 1)
        )
    assert after == external.VersionBase(read_next, 2)


async def test_confirmation_version(engine: AsyncEngine, states_delivered: None) -> None:
    """A provider returning resulting values: the confirmation's 250 is version 2 at the
    outcome step's position, recorded on the ``external_outcome`` event (source
    ``confirmation``), with no read of the provider. Version 1 (300) is unchanged."""
    w = await _world(engine)
    turn, read_n = await ps.record_read(engine, w.story, w.gold, w.cal, 300)
    before = await ps.external_value_rows(engine, w.story)
    at = Position(turn, 0, 0, 2, 1)
    found = await _settle(engine, w, _outcome(resulting={"gold": 250}), at, slot=False)
    assert isinstance(found, ExternalPin)
    assert (found.value, found.version, found.source, found.position) == (
        Decimal(250),
        2,
        ExternalValueSource.CONFIRMATION,
        at,
    )
    assert w.fake.reads == []
    (event,) = await ps.external_outcomes(engine, w.story)
    assert (event.narrative_turn, event.beat, event.step) == (turn, 2, 1)
    assert event.payload["version"] == {
        "state_id": str(w.gold.id),
        "scope_key": w.cal,
        "value": 250,
        "version": 2,
        "base_event_id": read_n,
        "source": "confirmation",
    }
    rows = await ps.external_value_rows(engine, w.story)
    assert rows[0] == before[0]  # version 1 is never updated
    assert (rows[1]["value"], rows[1]["position_beat"], rows[1]["position_step"]) == (250, 2, 1)


async def test_post_effect_read(engine: AsyncEngine, states_delivered: None) -> None:
    """A provider that returns success only: one post-effect read (240, another writer's
    value) is recorded as an ``external_read`` with ``source = post_effect_read``, version 2
    of turn N's read; the outcome event records no version."""
    w = await _world(engine, returns_resulting_values=False)
    turn, read_n = await ps.record_read(engine, w.story, w.gold, w.cal, 300)
    w.fake.script("gold", 240)
    at = Position(turn, 0, 0, 2, 1)
    found = await _settle(engine, w, _outcome(resulting={"gold": 250}), at, slot=False)
    assert isinstance(found, ExternalPin)
    assert (found.value, found.version, found.source) == (
        Decimal(240),
        2,
        ExternalValueSource.POST_EFFECT_READ,
    )
    assert len(w.fake.reads) == 1
    assert w.reads.live_reads == 1
    (_, post) = await ps.external_reads(engine, w.story)
    assert (post.beat, post.step) == (2, 1)
    assert {k: post.payload[k] for k in ("source", "value", "version", "base_event_id")} == {
        "source": "post_effect_read",
        "value": 240,
        "version": 2,
        "base_event_id": read_n,
    }
    (event,) = await ps.external_outcomes(engine, w.story)
    assert "version" not in event.payload
    assert event.payload["status"] == "confirmed"
    rows = await ps.external_value_rows(engine, w.story)
    assert [(r["version"], r["value"], r["base_event_id"]) for r in rows] == [
        (1, 300, read_n),
        (2, 240, read_n),
    ]


async def test_unknown_when_neither(engine: AsyncEngine, states_delivered: None) -> None:
    """No resulting values and the post-effect read fails: ``Unknown`` is recorded as version
    2 (the outcome event's version, source ``unknown``, no value), after exactly one read
    attempt (#206's policy does not apply mid-stage: no ``last_known`` stand-in). A failed
    outcome versions nothing and reads nothing."""
    w = await _world(engine, returns_resulting_values=False)
    turn, read_n = await ps.record_read(engine, w.story, w.gold, w.cal, 300)
    w.fake.script("gold", ps.Fail())
    at = Position(turn, 0, 0, 2, 1)
    found = await _settle(engine, w, _outcome(), at, slot=False)
    assert isinstance(found, Unknown)
    assert found.pin is not None
    assert (found.pin.version, found.pin.value) == (2, None)
    assert len(w.fake.reads) == 1
    (event,) = await ps.external_outcomes(engine, w.story)
    assert event.payload["version"]["source"] == "unknown"
    assert event.payload["version"]["value"] is None
    assert event.payload["version"]["base_event_id"] == read_n

    failed = await _settle(engine, w, _outcome("failed"), Position(turn, 0, 0, 3), slot=False)
    assert failed is None
    assert len(w.fake.reads) == 1
    assert "version" not in (await ps.external_outcomes(engine, w.story))[1].payload


async def test_verification_per_position(engine: AsyncEngine, states_delivered: None) -> None:
    """The owner lookup (#191) at a claim's Position: balance 300 read at the intent position,
    then

    - a confirmation version 250 at P: "250 left" is supported at P, and at the intent
      position it is checked against 300 (contradicted; "300" supported there);
    - ``Unknown`` at P: "250 left" is ``uncertain(provider_unknown)``; "paid 50" is no
      ``state_value`` claim at all, so the ``Unknown`` does not touch it;
    - a post-effect read of 240 at P: a claim at P is checked against 240 and a planted
      "250" is contradicted.
    """
    confirmed = await _world(engine)
    turn, _ = await ps.record_read(engine, confirmed.story, confirmed.gold, confirmed.cal, 300)
    at = Position(turn, 0, 0, 2, 1)
    intent = Position(turn, 0, 0, 1)
    await _settle(engine, confirmed, _outcome(resulting={"gold": 250}), at, slot=False)
    assert await _verdict(engine, confirmed, 250, at) == SUPPORTED
    assert await _verdict(engine, confirmed, 250, intent) == CONTRADICTED
    assert await _verdict(engine, confirmed, 300, intent) == SUPPORTED

    unknown = await _world(engine, returns_resulting_values=False)
    turn, _ = await ps.record_read(engine, unknown.story, unknown.gold, unknown.cal, 300)
    unknown.fake.script("gold", ps.Fail())
    await _settle(engine, unknown, _outcome(), Position(turn, 0, 0, 2, 1), slot=False)
    assert await _verdict(engine, unknown, 250, Position(turn, 0, 0, 2, 1)) == PROVIDER_UNKNOWN
    assert await _verdict(engine, unknown, 300, Position(turn, 0, 0, 1)) == SUPPORTED
    tracked = sv.tracked([unknown.gold])
    keys = frozenset({unknown.cal})

    def detect(amount: int, sentence: str) -> Any:
        return sv.state_claim(
            tracked, "gold", amount, subject=unknown.cal, scope_keys=keys, sentence=sentence
        )

    assert detect(50, "Cal paid 50 gold for the sword.") is None
    assert detect(250, "Cal has 250 gold left.") == _claim(unknown, 250)

    read = await _world(engine, returns_resulting_values=False)
    turn, _ = await ps.record_read(engine, read.story, read.gold, read.cal, 300)
    read.fake.script("gold", 240)
    at = Position(turn, 0, 0, 2, 1)
    await _settle(engine, read, _outcome(), at, slot=False)
    assert await _verdict(engine, read, 240, at) == SUPPORTED
    assert await _verdict(engine, read, 250, at) == CONTRADICTED


async def test_verification_inside_the_turn(engine: AsyncEngine, states_delivered: None) -> None:
    """Inside the turn (the pins installed as Core's ``PinSource``), the version a writer
    recorded answers from its position on, and the read before it: the turn's pins are
    version-aware."""
    w = await _world(engine)
    turn, read_n = await ps.record_read(engine, w.story, w.gold, w.cal, 300)
    ref = (str(w.gold.id), w.cal)
    w.reads._turn = turn
    w.reads._pins[ref] = pinned.PinnedValue(
        pin=ExternalPin(ref[0], ref[1], Decimal(300), Position(turn), event_id=read_n),
        provider=w.provider.name,
        value_name="gold",
        query_hash="0" * 64,
        raw_hash="",
        read_at=datetime.now(UTC),
    )
    at = Position(turn, 0, 0, 2, 1)
    target = await pinned.outcome_target(engine, w.story, w.gold, w.cal, at)
    assert target is not None
    async with ps.outcome_op(engine, w.story, turn=turn) as op:
        await pinned.settle_value(
            op, w.reads, target, _outcome(resulting={"gold": 250}), w.provider
        )
        # Staged, not committed: only the pins can answer it.
        with external.pinning(w.reads):
            assert await _verdict(engine, w, 250, at) == SUPPORTED
            assert await _verdict(engine, w, 300, Position(turn, 0, 0, 1)) == SUPPORTED
            assert await _verdict(engine, w, 250, Position(turn, 0, 0, 2)) == CONTRADICTED
        # Left uncommitted: the operation is abandoned.
    assert w.reads.values()[0].pin.version == 1  # the read itself is never replaced


async def test_rebuild_makes_no_adapter_call(engine: AsyncEngine, states_delivered: None) -> None:
    """A projection rebuild refolds every version (the read, a post-effect read, a
    confirmation, an ``Unknown``) from the ``external_read`` and ``external_outcome`` events,
    exactly as recorded, with every adapter call failing: none is made (FR-STORE-17)."""
    w = await _world(engine, returns_resulting_values=False)
    turn, _ = await ps.record_read(engine, w.story, w.gold, w.cal, 300)
    w.fake.script("gold", 240)
    await _settle(engine, w, _outcome(), Position(turn, 0, 0, 2), slot=False)
    w.fake.script("gold", ps.Fail())
    await _settle(engine, w, _outcome(), Position(turn, 0, 0, 3), slot=False)
    w.provider = replace(w.provider, returns_resulting_values=True)
    await _settle(engine, w, _outcome(resulting={"gold": 230}), Position(turn, 0, 1), slot=True)
    recorded = await ps.external_value_rows(engine, w.story)
    assert [(r["version"], r["value"], r["source"]) for r in recorded] == [
        (1, 300, "read"),
        (2, 240, "post_effect_read"),
        (3, None, "unknown"),
        (4, 230, "confirmation"),
    ]
    calls = w.fake.calls
    await projections.rebuild(engine, w.story)
    assert await ps.external_value_rows(engine, w.story) == recorded
    assert w.fake.calls == calls
