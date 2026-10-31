"""OUTBOX-SAFETY — external effects follow their turn's membership and are never sent twice
where that is unsafe (FR-EXT-12,13,18; docs/04 §6; #210's dispatch clauses, #211's fork clause).

Hypothesis draws an interleaving of steps over one story whose turns write outbox rows for two
in-process providers (one that deduplicates dispatch by idempotency key, one that does not),
with each provider's answers drawn (``confirmed``, ``failed``, ``unknown``):

- ``turn``: a new turn writing one or two ``intended`` rows (``core.outbox.write_intended`` in
  the turn's commit, as #187's commit path does);
- ``truncate``: rewind to an earlier turn (its later turns ``discarded``);
- ``regenerate``: rewind to a turn and commit a new variant of it (the old one ``dormant``);
- ``swap``: make another chain active again (the chain swapped away from ``dormant``);
- ``dispatch``: the dispatcher's claim and send of a row (``outbox.dispatch``);
- ``claim``: a claim whose worker dies before it sends (``outbox.claim`` alone);
- ``expire``: the row's lease timer at the lease's end (``outbox.expire``);
- ``fork`` (``test_outbox_safety__fork`` only): fork the story at a drawn turn.

Asserted per example, against Postgres:

- no effect is ever sent whose operation was not ``active`` when it was claimed (the claim's
  membership re-read, in the claim's transaction, and #211's cancellation in the flip's own
  transaction);
- every claimed effect is recorded with an outcome or listed by the reconciliation status, and
  none is ever ``cancelled``; one whose turn left the active timeline after its claim is listed
  ``reconciliation_required`` (#211), and a cancelled one whose turn came back is listed
  ``cancelled_on_reactivation``;
- no effect is sent twice to the provider that does not deduplicate;
- a retry under one idempotency key never produces a second dispatch: every send carries its
  row's id, and a row is sent once per claim (a repeated dispatch job of a claimed or settled
  row sends nothing).

The complement (``CASES``, one explicit example each, #514's pattern): an active-origin row is
sent exactly once and confirmed; a row whose turn was rewound past is cancelled unsent; a
claimed row's lease lapsing with the provider that does not deduplicate makes it ``unknown``
once; and with the one that does, it is re-sent under the same key.

``test_outbox_safety__fork`` (#211, FR-EXT-13) adds ``fork`` steps and asserts the fork clause:
no fork ever holds an executable copy of an effect. Every fork's copies are records
(``executable = false``) under new ids (never a source idempotency key), no dispatch or recovery
sweep ever sends or enqueues one, a fork lists nothing for reconciliation, and the fork's
``effects_pending_in_source`` is exactly the source's ``intended``, ``claimed`` and ``unknown``
rows of the copied turns. Its complement cases: a fork over an undispatched effect lists it
pending, and a fork after a confirmed one lists nothing.

``test_outbox_safety__stages`` (#212, FR-EXT-15,16,19) plays staged turns end to end (a
synchronous provider) and asserts the stage clause: no committed beat, at any stage, asserts an
external outcome that was not confirmed, or a conditional effect that was not committed, when
its stage committed; and no committed stage is rolled back by a later stage's failure.

``test_outbox_safety__settlement`` (#217, FR-EXT-21) draws purchases (an outbox row and a sword
grant ``conditional`` on it), plain turns, the dispatcher's answers, late settlements,
truncations and their undoing, and asserts the settlement clause: no late settlement is ever
positioned or valid-timed before the head at which its outcome arrived (an inter-turn slot of
that head or a later turn, its story-time not before that head's end), and every confirmed
outcome is either settled in the active timeline or listed for reconciliation.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, timeline, world_clock
from epistrel.core import outbox as core_outbox
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind, Provenance, StateChangeStatus, StateContingency
from epistrel.core.events import Operation
from epistrel.core.fork import ForkRequest, fork
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.core.states import contingency
from epistrel.core.variants import ChainRef, SwapRequest, list_chains
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs.handlers import outbox
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from tests import canon_support as c
from tests import late_settlement_support as ls
from tests import projection_support as projections
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import new_story
from tests.memory_support import use_structured

PRINCIPAL = "p-test"
PENDING = ("intended", "claimed", "unknown")
"""The statuses a fork's response lists as pending in its source (#211)."""
EXAMPLES = 40
OUTCOMES = ("confirmed", "failed", "unknown")

KINDS = (
    "turn",
    "turn",
    "dispatch",
    "dispatch",
    "claim",
    "expire",
    "truncate",
    "regenerate",
    "swap",
)
"""The step kinds drawn (a turn and a dispatch twice as often as the rest)."""
Step = tuple[str, int]
"""A step's kind and its pick (which row, turn or chain, modulo what exists)."""

steps = st.lists(
    st.tuples(
        st.sampled_from(KINDS),
        st.integers(min_value=0, max_value=50),
    ),
    min_size=3,
    max_size=14,
)
answers = st.lists(st.sampled_from(OUTCOMES), min_size=1, max_size=4)
Drawn = tuple[list[Step], list[str], list[str]]
"""The steps, the deduplicating provider's answers, the other provider's."""
drawn_examples = st.tuples(steps, answers, answers)


@dataclass
class Tally:
    examples: int = 0
    sent_once: int = 0
    cancelled: int = 0
    dormant: int = 0
    unknown: int = 0
    resent: int = 0
    forks: int = 0
    fork_pending: int = 0
    fork_quiet: int = 0

    def add(self, other: Tally) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))


CASES: dict[str, tuple[str, Drawn]] = {
    # One turn, its rows dispatched: each sent once and confirmed.
    "sent_once": (
        "sent_once",
        ([("turn", 0), ("dispatch", 0), ("dispatch", 1)], ["confirmed"], ["confirmed"]),
    ),
    # Two turns; rewound to the first; the second's rows dispatched: cancelled, never sent.
    "cancelled": (
        "cancelled",
        (
            [("turn", 0), ("turn", 1), ("truncate", 1), ("dispatch", 2), ("dispatch", 3)],
            ["confirmed"],
            ["confirmed"],
        ),
    ),
    # Turn 1 regenerated, then swapped back to: the regenerated variant's rows (dormant after
    # the swap) are cancelled, never sent, and so are the restored turn's own (#211: cancelled
    # when the regeneration retired them, listed ``cancelled_on_reactivation``, never re-sent).
    "dormant": (
        "dormant",
        (
            [("regenerate", 0), ("swap", 0), ("dispatch", 2), ("dispatch", 0)],
            ["confirmed"],
            ["confirmed"],
        ),
    ),
    # Claimed, the worker dies, the lease lapses: the non-deduplicating provider's row is unknown.
    "unknown": (
        "unknown",
        (
            [("turn", 1), ("claim", 0), ("claim", 1), ("expire", 0), ("expire", 1)],
            ["confirmed"],
            ["confirmed"],
        ),
    ),
    # The deduplicating provider's claimed row is re-sent under the same key at the lease's end.
    "resent": (
        "resent",
        (
            [("turn", 0), ("claim", 0), ("claim", 1), ("expire", 0), ("expire", 1)],
            ["unknown"],
            ["confirmed"],
        ),
    ),
}
"""One explicit example per complement case, with the tally field it must raise."""

FORK_KINDS = (*KINDS, "fork", "fork")
"""``test_outbox_safety__fork``'s step kinds."""
FORK_CASES: dict[str, tuple[str, Drawn]] = {
    # A fork over the first turn's undispatched rows: both listed pending; the source still
    # sends its own, never the fork's copies.
    "fork_pending": (
        "fork_pending",
        ([("fork", 1), ("dispatch", 0)], ["confirmed"], ["confirmed"]),
    ),
    # Both rows confirmed, then a fork: nothing pending in the source.
    "fork_quiet": (
        "fork_quiet",
        ([("dispatch", 0), ("dispatch", 1), ("fork", 1)], ["confirmed"], ["confirmed"]),
    ),
}


@dataclass
class World:
    engine: AsyncEngine
    story: str
    registry: InProcessRegistry
    fakes: dict[str, ps.ScriptedProvider]
    dedupes: dict[str, bool]
    claimed_active: dict[str, bool] = field(default_factory=dict)
    """Each row ever claimed: whether its operation was active at that claim."""
    forks: list[str] = field(default_factory=list)
    """Every fork of the story (#211)."""


async def _providers(engine: AsyncEngine) -> tuple[str, str]:
    """The two providers (registered once per run): one deduplicating, one not."""
    dedupe, plain = ps.unique_name("dedupe"), ps.unique_name("plain")
    await ps.register(engine, ps.write_capable_spec(dedupe, dedupes=True))
    await ps.register(engine, ps.write_capable_spec(plain, dedupes=False))
    return dedupe, plain


async def _turn(w: World, *, turn: int | None = None) -> None:
    async with Operation(
        w.engine, story_id=w.story, kind=OperationKind.TURN, principal=PRINCIPAL, turn=turn
    ) as op:
        op.append("player_input", {"text": "buy"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "the coins change hands"})

        async def write(conn: AsyncConnection) -> None:
            rows = [
                core_outbox.Intended(
                    id=new_ulid(),
                    provider_name=name,
                    value_name="gold",
                    scope_key=None,
                    effect={"kind": "change", "state": "gold", "delta": -5, "reason": "a sword"},
                    beat=2,
                )
                for name in sorted(w.fakes)
            ]
            await core_outbox.write_intended(conn, op, rows)

        op.add_commit_step(write)
        await op.commit()


async def _rows(engine: AsyncEngine, story: str) -> list[Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT o.*, op.membership::text AS origin FROM external_effect_outbox o "
                "JOIN operations op ON op.operation_id = o.operation_id "
                "WHERE o.story_id = :s ORDER BY o.created_at, o.provider_name, o.id"
            ),
            {"s": story},
        )
        return list(found)


async def _head(engine: AsyncEngine, story: str) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT coalesce(max(narrative_turn), 0) FROM operations WHERE story_id = :s "
                "AND kind = 'turn' AND membership = 'active' AND status = 'committed'"
            ),
            {"s": story},
        )
    return int(found or 0)


async def _fork(w: World, pick: int, tally: Tally) -> None:
    """Fork the story at a drawn turn; the fork clause holds for the copy at once (#211)."""
    engine, story = w.engine, w.story
    head = await _head(engine, story)
    at = pick % (head + 1)
    forked = await fork(engine, story, ForkRequest(at_turn=at), principal=PRINCIPAL)
    w.forks.append(forked.story_id)
    tally.forks += 1
    source = await _rows(engine, story)
    copied = {
        r.id
        for r in source
        if r.origin == "active" and (r.narrative_turn is None or r.narrative_turn <= at)
    }
    pending = [r.id for r in source if r.id in copied and r.status in PENDING]
    assert sorted(p.outbox_id for p in forked.effects_pending_in_source) == sorted(pending)
    for listed in forked.effects_pending_in_source:
        assert listed.status in PENDING
    if pending:
        tally.fork_pending += 1
    elif copied:
        tally.fork_quiet += 1
    copies = await _rows(engine, forked.story_id)
    assert len(copies) == len(copied)
    assert {r.id for r in copies}.isdisjoint({r.id for r in source})
    for row in copies:
        assert row.executable is False
        assert await outbox.dispatch(engine, row.id, registry=w.registry) == outbox.SKIPPED


async def _step(w: World, kind: str, pick: int, tally: Tally) -> None:
    engine, story = w.engine, w.story
    if kind == "turn":
        await _turn(w)
        return
    if kind == "fork":
        await _fork(w, pick, tally)
        return
    if kind in ("truncate", "regenerate"):
        head = await _head(engine, story)
        if head == 0:
            return
        to = pick % (head + 1) if kind == "truncate" else 1 + pick % head
        if to < head:
            await timeline.truncate(
                engine, story, timeline.TruncateRequest(to_turn=to), principal=PRINCIPAL
            )
        if kind == "regenerate":
            await _turn(w, turn=to)
        return
    if kind == "swap":
        others = [c for c in await list_chains(engine, story) if c.membership.value == "dormant"]
        if not others:
            return
        chosen = others[pick % len(others)]
        await timeline.swap(
            engine,
            story,
            SwapRequest(to_variant_chain=ChainRef(turn=chosen.turn, variant=chosen.variant)),
            principal=PRINCIPAL,
        )
        return
    rows = await _rows(engine, story)
    if not rows:
        return
    row = rows[pick % len(rows)]
    if kind == "expire":
        before = sum(len(f.idempotency_keys) for f in w.fakes.values())
        later = clock.now() + outbox.CLAIM_LEASE + timedelta(seconds=1)
        with clock.frozen_at(later):
            done = await outbox.expire(engine, row.id, registry=w.registry)
        after = sum(len(f.idempotency_keys) for f in w.fakes.values())
        if done.startswith("resent:"):
            assert after == before + 1
            tally.resent += 1
        elif done == "unknown" and row.status == "claimed":
            tally.unknown += 1
        return
    active = row.origin == "active"
    sent_before = Counter(k for f in w.fakes.values() for k in f.idempotency_keys)
    if kind == "claim":
        claimed = await outbox.claim(engine, row.id, registry=w.registry)
        if isinstance(claimed, outbox.Claimed):
            w.claimed_active[row.id] = active
        elif claimed == outbox.CANCELLED:
            assert not active
            tally.cancelled += 1
        return
    status = row.status
    done = await outbox.dispatch(engine, row.id, registry=w.registry)
    sent = Counter(k for f in w.fakes.values() for k in f.idempotency_keys) - sent_before
    if status == "intended" and done not in (outbox.CANCELLED, outbox.SKIPPED, outbox.HELD):
        w.claimed_active[row.id] = active
    if done == outbox.CANCELLED or status == "cancelled":
        # Cancelled by the claim's re-read, or (#211) by the flip that took its turn out; a
        # turn brought back later keeps its rows cancelled, never sent.
        assert done != outbox.CANCELLED or not active
        assert not sent
        tally.cancelled += 1
        tally.dormant += row.origin == "dormant"
    if status != "intended":
        # A repeated dispatch of a claimed or settled row sends nothing.
        assert not sent, (row.id, status, done)
    if sent:
        assert set(sent) == {row.id}
        assert sum(sent.values()) == 1
        if done == "confirmed" and status == "intended":
            tally.sent_once += 1


async def _check(w: World) -> None:
    rows = {r.id: r for r in await _rows(w.engine, w.story)}
    listed = {e.outbox_id: e for e in (await core_outbox.listings(w.engine, w.story)).listings}
    for name, fake in w.fakes.items():
        keys = Counter(fake.idempotency_keys)
        for key, sends in keys.items():
            # Sent only after a claim that saw its operation active, and only as its own row.
            assert w.claimed_active.get(key) is True, key
            assert rows[key].provider_name == name
            # At most one send per claim (a claim whose worker died sent nothing).
            assert sends <= rows[key].attempts, (key, sends, rows[key].attempts)
            if not w.dedupes[name]:
                assert sends == 1, key
    marks = await _marks(w.engine, w.story)
    for row in rows.values():
        if row.attempts:
            assert row.status != "cancelled", row.id
            if row.status != "confirmed":
                assert row.id in listed, row.id
                assert listed[row.id].reason in (
                    "unconfirmed",
                    "failed",
                    "unknown",
                    "reconciliation_required",
                )
            marked = "reconciliation_required" in marks.get(row.id, ())
            if row.origin != "active" and row.status in ("claimed", "unknown", "confirmed"):
                # #211: claimed before its turn left the timeline (a failed one was settled
                # first and stays ``failed``): listed, never compensated.
                assert marked, row.id
            if marked:
                assert listed[row.id].reason == "reconciliation_required", row.id
        if row.status == "cancelled":
            assert row.attempts == 0
            if row.origin == "active" and row.id in listed:
                assert listed[row.id].reason == "cancelled_on_reactivation", row.id
        if row.status == "intended":
            # #211: a pending row whose turn is out was cancelled in the flip's transaction.
            assert row.origin == "active", row.id
    for forked in w.forks:
        # The fork clause: no fork ever holds an executable copy of an effect.
        copies = await _rows(w.engine, forked)
        assert not any(r.executable for r in copies), forked
        assert (await core_outbox.listings(w.engine, forked)).listings == []
        for fake in w.fakes.values():
            assert {r.id for r in copies}.isdisjoint(fake.idempotency_keys)


async def _marks(engine: AsyncEngine, story: str) -> dict[str, set[str]]:
    """Each row's open reconciliation marks (#211)."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT outbox_id, reason FROM reconciliation_marks WHERE story_id = :s "
                "AND resolved_by IS NULL"
            ),
            {"s": story},
        )
        marks: dict[str, set[str]] = {}
        for outbox_id, reason in found:
            marks.setdefault(str(outbox_id), set()).add(str(reason))
        return marks


async def _example(url: str, providers: tuple[str, str], drawn: Drawn, tally: Tally) -> None:
    script, dedupe_answers, plain_answers = drawn
    engine = make_engine(auth_settings(url))
    try:
        story = await new_story(engine, PRINCIPAL)
        dedupe, plain = providers
        registry = InProcessRegistry()
        fakes = {dedupe: ps.ScriptedProvider(dedupe), plain: ps.ScriptedProvider(plain)}
        fakes[dedupe].script_writes("gold", *dedupe_answers)
        fakes[plain].script_writes("gold", *plain_answers)
        for name, fake in fakes.items():
            registry.register(name, read=fake.read, write=fake.write)
            await ps.bind(engine, story, name, {"player": "p-1"})
        w = World(engine, story, registry, fakes, {dedupe: True, plain: False})
        # A story starts at turn 1 with its first effects (the picks name rows by index).
        await _turn(w)
        for kind, pick in script:
            await _step(w, kind, pick, tally)
            await _check(w)
        if w.forks:
            await outbox.recover(engine)
            for forked in w.forks:
                assert await ps.outbox_jobs(engine, forked) == []
        tally.examples += 1
        await ps.drop_outbox_jobs(engine, story)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.part("M6", "FR-EXT-12,18")
def test_outbox_safety(pg_url: str) -> None:
    """See the module docstring."""
    tally = Tally()
    by_case = {name: Tally() for name in CASES}

    async def setup() -> tuple[str, str]:
        engine = make_engine(auth_settings(pg_url))
        try:
            return await _providers(engine)
        finally:
            await engine.dispose()

    providers = asyncio.run(setup())

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=drawn_examples)
    def safety(drawn: Drawn) -> None:
        one = Tally()
        asyncio.run(_example(pg_url, providers, drawn, one))
        tally.add(one)
        for name, (_, case) in CASES.items():
            if drawn == case:
                by_case[name].add(one)

    for _, case in CASES.values():
        safety = example(drawn=case)(safety)
    safety()
    covered = {counter for counter, _ in CASES.values()}
    assert covered == {f.name for f in fields(Tally)} - {"examples", *FORK_TALLIES}
    for name, (counter, _) in CASES.items():
        assert by_case[name].examples >= 1, name
        assert getattr(by_case[name], counter) > 0, (name, by_case[name])
    assert tally.examples == EXAMPLES + len(CASES)


FORK_TALLIES = ("forks", "fork_pending", "fork_quiet")
FORK_EXAMPLES = 20
fork_steps = st.lists(
    st.tuples(st.sampled_from(FORK_KINDS), st.integers(min_value=0, max_value=50)),
    min_size=3,
    max_size=12,
)
fork_examples = st.tuples(fork_steps, answers, answers)


@pytest.mark.integration
@pytest.mark.part("M6", "FR-EXT-13")
def test_outbox_safety__fork(pg_url: str) -> None:
    """See the module docstring (the fork clause, #211)."""
    tally = Tally()
    by_case = {name: Tally() for name in FORK_CASES}

    async def setup() -> tuple[str, str]:
        engine = make_engine(auth_settings(pg_url))
        try:
            return await _providers(engine)
        finally:
            await engine.dispose()

    providers = asyncio.run(setup())

    @settings(
        max_examples=FORK_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=fork_examples)
    def safety(drawn: Drawn) -> None:
        one = Tally()
        asyncio.run(_example(pg_url, providers, drawn, one))
        tally.add(one)
        for name, (_, case) in FORK_CASES.items():
            if drawn == case:
                by_case[name].add(one)

    for _, case in FORK_CASES.values():
        safety = example(drawn=case)(safety)
    safety()
    for name, (counter, _) in FORK_CASES.items():
        assert by_case[name].examples >= 1, name
        assert getattr(by_case[name], counter) > 0, (name, by_case[name])
    assert tally.forks > 0
    assert tally.examples == FORK_EXAMPLES + len(FORK_CASES)


# -- #216: contingent State effects (FR-STATE-14) ------------------------------------------------

CONTINGENCY_EXAMPLES = 25
CONTINGENT_KINDS = ("intent", "intent", "turn", "settle", "settle", "truncate")
contingent_steps = st.lists(
    st.tuples(
        st.sampled_from(CONTINGENT_KINDS),
        st.integers(min_value=0, max_value=50),
        st.sampled_from(OUTCOMES),
    ),
    min_size=2,
    max_size=10,
)


@dataclass
class Ledger:
    engine: AsyncEngine
    story: str
    cal: str
    effects: list[str] = field(default_factory=list)
    """Every external effect id an intent row waits on."""
    settled_at: dict[str, tuple[int, int]] = field(default_factory=dict)
    """Each settling operation's ``(turn, slot_k)``."""
    slots: Counter[int] = field(default_factory=Counter)


async def _ledger_story(engine: AsyncEngine) -> Ledger:

    world = await c.world(engine)
    story, cal = world.story_id, world.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(engine, story, ss.gold())
    await ss.change(engine, story, ss.set_to("gold", 100, scope_key=cal))
    return Ledger(engine, story, cal)


async def _contingent_step(w: Ledger, kind: str, pick: int, outcome: str) -> None:

    head = await _head(w.engine, w.story)
    if kind == "intent":
        effect = new_ulid()
        w.effects.append(effect)
        held = pick % 2 == 0
        await ss.change(
            w.engine,
            w.story,
            ss.delta(
                "gold",
                -(pick % 7 + 1) if held else pick % 7 + 1,
                scope_key=w.cal,
                contingency=StateContingency.RESERVATION if held else StateContingency.CONDITIONAL,
                status=StateChangeStatus.HELD if held else StateChangeStatus.PENDING,
                prerequisite_effect_ids=(effect,),
            ),
        )
    elif kind == "turn":
        await ss.change(w.engine, w.story, ss.delta("gold", 0, scope_key=w.cal))
    elif kind == "settle" and w.effects:
        effect = w.effects[pick % len(w.effects)]
        w.slots[head] += 1
        position = Position(head, 0, w.slots[head])

        async def write(conn: AsyncConnection, op: Operation) -> Any:
            found = await conn.scalar(
                text(
                    "SELECT turn_variant FROM operations WHERE story_id = :s AND kind = 'turn' "
                    "AND narrative_turn = :t AND membership = 'active'"
                ),
                {"s": w.story, "t": head},
            )
            at = Position(head, int(found or 0), position.slot_k)
            return await contingency.settle(conn, op, {effect: outcome}, at)

        op, _ = await c.run(w.engine, w.story, write, kind=OperationKind.CONTROL, turn=head)
        w.settled_at[op.operation_id] = (head, position.slot_k)
    elif kind == "truncate" and head > 2:
        await projections.truncate(w.engine, w.story, 2 + pick % (head - 2))


async def _contingent_check(w: Ledger) -> None:

    changes = [r for r in await ss.change_rows(w.engine, w.story) if r.scope_key == w.cal]
    by_id = {str(r.id): r for r in changes}
    active = [r for r in changes if r.membership == "active"]
    for r in changes:
        if r.source != "settlement":
            continue
        intent = by_id[str(r.settles_change_id)]
        # Never positioned before its settling operation, nor before its intent.
        assert (r.narrative_turn, r.slot_k) == w.settled_at[str(r.operation_id)]
        assert (r.narrative_turn, r.slot_k, r.beat) > (
            intent.narrative_turn,
            intent.slot_k,
            intent.beat,
        )
        assert intent.status == "converted"
    for r in active:
        if r.settles_change_id is not None:
            others = [
                o for o in active if o.settles_change_id == r.settles_change_id and o.id != r.id
            ]
            assert not others  # settled once
    # No contingent change is folded into a value before it commits: every view's value is the
    # committed rows' alone, by their own positions.
    head = await _head(w.engine, w.story)
    (seed,) = [r for r in active if r.kind == "set"]
    for turn in range(int(seed.narrative_turn), head + 1):
        for slot in range(
            0, 1 + max((r.slot_k for r in active if r.narrative_turn == turn), default=0)
        ):
            through = Position.end_of(turn, slot)
            expected = Decimal(100) + sum(
                (
                    Decimal(r.delta)
                    for r in active
                    if r.status == "committed"
                    and r.delta is not None
                    and (r.narrative_turn, r.slot_k) <= (turn, slot)
                    and r.kind != "set"
                ),
                Decimal(0),
            )
            assert await ss.value(w.engine, w.story, "gold", w.cal, through=through) == expected


async def _contingent_example(
    url: str, drawn: list[tuple[str, int, str]], seen: Counter[str]
) -> None:

    engine = make_engine(auth_settings(url))
    try:
        w = await _ledger_story(engine)
        for kind, pick, outcome in drawn:
            await _contingent_step(w, kind, pick, outcome)
            await _contingent_check(w)
        for r in await ss.change_rows(engine, w.story):
            seen[str(r.status)] += 1
        await ss.set_level(engine, w.story, None)
    finally:
        await engine.dispose()


CONTINGENT_CASE = [
    ("intent", 1, "confirmed"),
    ("turn", 0, "confirmed"),
    ("settle", 0, "confirmed"),
    ("intent", 2, "failed"),
    ("settle", 1, "failed"),
    ("intent", 4, "unknown"),
    ("settle", 2, "unknown"),
    ("truncate", 0, "confirmed"),
]
"""The complement: a settlement converts, a failure releases, ``unknown`` leaves a row held,
and a truncation past a settlement drops it."""


@pytest.mark.integration
@pytest.mark.part("M6", "FR-STATE-14")
def test_outbox_safety__contingency(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """#216 (FR-STATE-14): over drawn interleavings of contingent intents (``held`` and
    ``pending`` rows waiting on external effects), plain turns, settlements with drawn outcomes
    at the head's next inter-turn slot, and truncations: no conditional effect is folded into a
    value before it commits (every view through every turn and slot is the committed rows'
    alone), no settlement is positioned before its settling operation or its intent, and none
    is settled twice."""

    ss.deliver(monkeypatch)
    seen: Counter[str] = Counter()

    @settings(
        max_examples=CONTINGENCY_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=contingent_steps)
    @example(drawn=CONTINGENT_CASE)
    def safety(drawn: list[tuple[str, int, str]]) -> None:
        asyncio.run(_contingent_example(pg_url, drawn, seen))

    safety()
    assert {"converted", "released", "held"} <= set(seen), seen


# -- #212: staged turns (FR-EXT-15,16,19) --------------------------------------------------------

STAGED_EXAMPLES = 8
STAGED_ANSWERS = ("confirmed", "failed", "unknown", "hang")
INTENT_SAYS = {
    "attempt": "Mira wraps the sword in oilcloth.",
    "outcome": "Mira wraps the sword in oilcloth. The purchase went through.",
    "holds": "Mira wraps the sword in oilcloth. Cal now holds the sword.",
}
OUTCOME_SAYS = {
    "done": "The smith nods: the purchase went through.",
    "holds": "Cal now holds the sword.",
    "pending": "The smith counts the coins.",
}
staged_turns = st.lists(
    st.tuples(
        st.sampled_from(STAGED_ANSWERS),
        st.sampled_from(sorted(INTENT_SAYS)),
        st.sampled_from(sorted(OUTCOME_SAYS)),
    ),
    min_size=1,
    max_size=2,
)
StagedDrawn = tuple[list[tuple[str, str, str]], bool]
"""The staged turns (the game's answer, what the intent and the outcome step say) and whether
the last turn's completion fails."""
staged_examples = st.tuples(staged_turns, st.booleans())
STAGED_CASE: StagedDrawn = (
    [("confirmed", "outcome", "done"), ("failed", "holds", "holds")],
    True,
)
"""The complement: a confirmed purchase narrated done at its outcome step (and repaired at its
intent), then a refused one whose possession sentences are repaired at both stages, whose
completion then fails, leaving its intent and outcome committed."""


class Boom(Exception):
    """An induced failure of a staged turn's completion."""


async def _staged_story(client: Any, engine: AsyncEngine, epistrel: Any) -> tuple[str, str, Any]:
    """A story whose gold a synchronous provider owns (it waits 1 s) and whose story-owned
    sword Cal buys with it (a ``conditional`` grant): the story, Cal and the fake."""
    from epistrel.core.propositions import store
    from epistrel.core.states import StateDefinition
    from tests import effect_support as es
    from tests.story_support import override
    from tests.structured_turn_support import structured_story

    name = ps.unique_name("bank")
    await ps.register(
        engine,
        ps.write_capable_spec(name, confirmation="synchronous", confirm_wait_max_seconds=1),
    )
    fake = ps.ScriptedProvider(name, default=1000).attach(epistrel)
    story, ids = await structured_story(client, "Cal", "Mira")
    await override(client, story, {"budgets": {"model_calls": 50}})
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    async with engine.begin() as conn:
        sword = await store.add_entity(conn, story, "item", "sword")
    await ss.define_all(
        engine,
        story,
        StateDefinition.model_validate(
            {
                "name": "sword",
                "type": "number",
                "scope": "character",
                "constraints": {"non_negative": True},
                "initial_value": 0,
                "entity_id": sword,
                "transfer": {"counterparties": ["gold"]},
                "authorization": {"sources": ["any_character", "director"]},
            }
        ),
    )
    return story, ids["Cal"], fake


def _buying(cal: str) -> dict[str, Any]:
    return {
        "kind": "transfer",
        "state": "sword",
        "scope_key": cal,
        "delta": 1,
        "counterparty": {"state": "gold", "scope_key": cal, "delta": -500},
        "reason": "the sword",
    }


async def _pending_at(engine: AsyncEngine, story: str, cal: str) -> dict[str, Any]:
    """What a beat of the turn may not assert while unsettled: Cal's gold change (the
    exchange) and the sword's conditional grant, as #209's and #216's rules read them."""
    from epistrel.core import states
    from epistrel.core.states import effects as rules

    async with engine.connect() as conn:
        found = {d.name: d for d in await states.definitions(conn, story)}
    gold, sword = found["gold"], found["sword"]
    return {
        "gold": rules.Pending(
            "gold", gold.id, cal, rules.terms_of(gold, {}).words, Decimal(1000), Decimal(500)
        ),
        "sword": rules.Pending(
            "sword", sword.id, cal, ("sword",), Decimal(0), Decimal(1), grant=True
        ),
    }


async def _staged_check(engine: AsyncEngine, story: str, cal: str, operation_id: str) -> None:
    """The stage clause over one staged turn: no committed beat asserts an external outcome
    that was not confirmed, or a conditional effect that was not committed, when its stage
    committed (an intent beat: nothing was; an outcome-step beat: the turn's outcome)."""
    from epistrel.core.states import effects as rules

    pending = await _pending_at(engine, story, cal)
    async with engine.connect() as conn:
        beats = list(
            await conn.execute(
                text(
                    "SELECT beat, payload->>'text' AS text FROM events WHERE operation_id = :o "
                    "AND type LIKE 'beat_%' ORDER BY seq"
                ),
                {"o": operation_id},
            )
        )
        outcome = await conn.scalar(
            text(
                "SELECT payload->>'status' FROM events WHERE operation_id = :o "
                "AND type = 'external_outcome'"
            ),
            {"o": operation_id},
        )
        first_outcome = await conn.scalar(
            text(
                "SELECT min(beat) FROM events WHERE operation_id = :o AND type = 'external_outcome'"
            ),
            {"o": operation_id},
        )
        granted = await conn.scalar(
            text(
                "SELECT count(*) FROM state_changes WHERE operation_id = :o "
                "AND source = 'settlement' AND status = 'committed'"
            ),
            {"o": operation_id},
        )
    for b in beats:
        at_intent = first_outcome is None or b.beat < first_outcome
        unsettled = [
            p
            for name, p in pending.items()
            if at_intent
            or (name == "gold" and outcome != "confirmed")
            or (name == "sword" and not granted)
        ]
        assert rules.outcome_spans(b.text, unsettled) == [], (b.beat, b.text, outcome, granted)


async def _staged_example(url: str, drawn: StagedDrawn, seen: Counter[str]) -> None:
    from epistrel.orchestrator.structured import staged as staged_rules
    from tests import effect_support as es
    from tests import multi_support as ms
    from tests import staged_support as sp
    from tests.respond_support import respond_client
    from tests.structured_turn_support import beat

    script, fail_last = drawn
    settings_ = auth_settings(url, model_adapter="stub")
    engine = make_engine(settings_)
    try:
        async with respond_client(settings_, engine) as (client, epistrel):
            stub = epistrel.models.stub
            story, cal, fake = await _staged_story(client, engine, epistrel)
            for n, (answer, intent_says, outcome_says) in enumerate(script):
                failing = fail_last and n == len(script) - 1
                fake.script_writes("gold", ps.Hang() if answer == "hang" else answer)
                stub.reset()
                sp.outcome_step(stub, beat("narration", OUTCOME_SAYS[outcome_says]))
                ms.renders_for(
                    stub,
                    "Mira",
                    es.render(beat("do", INTENT_SAYS[intent_says], effects=[_buying(cal)])),
                )
                es.narrator(stub, es.render(beat("narration", "The forge glows.")))
                stub.script(
                    "regeneration",
                    [
                        json.dumps({"text": t, "effects": []})
                        for t in (INTENT_SAYS["attempt"], OUTCOME_SAYS["pending"]) * 4
                    ],
                )
                before = await sp.revision(engine, story)
                original = staged_rules.write_report

                async def boom(*_: Any, **__: Any) -> None:
                    raise Boom("the completion fails")

                staged_rules.write_report = boom if failing else original
                try:
                    answer_ = await sp.buy(client, story, cal)
                except Boom:
                    answer_ = None
                finally:
                    staged_rules.write_report = original
                op = await sp.staged_op(engine, story)
                await _staged_check(engine, story, cal, op.operation_id)
                if failing:
                    # The failed completion rolled back alone: intent and outcome stand.
                    assert answer_ is None or answer_.status_code >= 500
                    assert op.turn_stage == "outcome_recorded"
                    assert await sp.revision(engine, story) == before + 2
                    assert await sp.stage_names(engine, story) == ["intent", "claim", "outcome"]
                    seen["failed_completion"] += 1
                else:
                    assert answer_ is not None
                    assert answer_.status_code == 200, answer_.text
                    assert op.turn_stage == "completed"
                    assert await sp.revision(engine, story) == before + 3
                    assert await sp.stage_names(engine, story) == list(staged_rules.STAGES)
                    seen[answer] += 1
            await ps.drop_outbox_jobs(engine, story)
            await ss.set_level(engine, story, None)
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.part("M6", "FR-EXT-15,16,19")
def test_outbox_safety__stages(
    pg_url: str, monkeypatch: pytest.MonkeyPatch, drained_embedding_jobs: None
) -> None:
    """#212 (FR-EXT-15,16,19): over drawn staged turns (the game confirms, refuses, answers
    ambiguously or never answers; each turn's intent and outcome step drawn to narrate the
    attempt, the outcome or the possession of the conditional grant; the last turn's completion
    possibly failing), no committed beat, at any stage, asserts an external outcome that was not
    confirmed, or a conditional effect that was not committed, when that beat's stage committed
    (#209's and #216's own rules, ``core.states.effects.outcome_spans``); every completed turn
    moved the revision exactly three times and logged its five stages; and a failed completion
    rolls back alone, never the committed intent or outcome."""
    ss.deliver(monkeypatch)
    seen: Counter[str] = Counter()

    @settings(
        max_examples=STAGED_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=staged_examples)
    @example(drawn=STAGED_CASE)
    def safety(drawn: StagedDrawn) -> None:
        asyncio.run(_staged_example(pg_url, drawn, seen))

    safety()
    assert seen["confirmed"] >= 1, seen
    assert seen["failed_completion"] >= 1, seen


# -- #217: late settlement (FR-EXT-21) ------------------------------------------------------------

SETTLEMENT_EXAMPLES = 20
SETTLEMENT_KINDS = (
    "intent",
    "intent",
    "turn",
    "answer",
    "answer",
    "settle",
    "settle",
    "truncate",
    "untruncate",
)
settlement_steps = st.lists(
    st.tuples(
        st.sampled_from(SETTLEMENT_KINDS),
        st.integers(min_value=0, max_value=50),
        st.sampled_from(OUTCOMES),
    ),
    min_size=3,
    max_size=12,
)


@dataclass
class Late:
    engine: AsyncEngine
    story: str
    cal: str
    provider: str
    registry: InProcessRegistry
    fake: ps.ScriptedProvider
    arrived: dict[str, tuple[str, int]] = field(default_factory=dict)
    """Each outbox row's deciding outcome record and the head at which it arrived."""
    ran: set[str] = field(default_factory=set)
    """The rows whose settlement job has run."""


async def _late_story(engine: AsyncEngine, provider: str) -> Late:
    world = await c.world(engine)
    story, cal = world.story_id, world.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(
        engine,
        story,
        ss.gold(name="sword", unit=None, aliases=("blade",), constraints={"non_negative": True}),
    )
    registry = InProcessRegistry()
    fake = ps.ScriptedProvider(provider)
    registry.register(provider, read=fake.read, write=fake.write)
    await ps.bind(engine, story, provider, {"player": "p-1"})
    return Late(engine, story, cal, provider, registry, fake)


async def _late_step(w: Late, kind: str, pick: int, outcome: str, seen: Counter[str]) -> None:
    head = await _head(w.engine, w.story)
    if kind == "intent":
        await ls.intent(
            w.engine,
            w.story,
            w.provider,
            w.cal,
            [ls.Side(w.cal, 1, StateContingency.CONDITIONAL)],
        )
    elif kind == "turn":
        await ls.turn(w.engine, w.story)
    elif kind == "answer":
        open_rows = [r for r in await _rows(w.engine, w.story) if r.status == "intended"]
        if not open_rows:
            return
        row = open_rows[pick % len(open_rows)]
        w.fake.script_writes("gold", outcome)
        await outbox.dispatch(w.engine, str(row.id), registry=w.registry)
        decided = [
            o
            for o in await ps.outbox_outcomes(w.engine, w.story)
            if o.outbox_id == row.id and o.kind in ("confirmed", "failed")
        ]
        if decided:
            w.arrived[str(row.id)] = (str(decided[0].id), head)
    elif kind == "settle":
        waiting = sorted(i for i in w.arrived if i not in w.ran)
        if not waiting:
            return
        outbox_id = waiting[pick % len(waiting)]
        done = await settlement.settle(w.engine, w.arrived[outbox_id][0], registry=w.registry)
        w.ran.add(outbox_id)
        seen[done.result] += 1
    elif kind == "truncate" and head > 1:
        await projections.truncate(w.engine, w.story, 1 + pick % (head - 1))
    elif kind == "untruncate":
        await projections.untruncate(w.engine, w.story)


async def _late_check(w: Late, seen: Counter[str]) -> None:
    changes = await ss.change_rows(w.engine, w.story)
    async with w.engine.connect() as conn:
        for r in changes:
            if r.source != "settlement":
                continue
            (outbox_id,) = [str(p) for p in r.prerequisite_effect_ids]
            _, arrived_at = w.arrived[outbox_id]
            # Positioned in an inter-turn slot at or after the head it arrived at, never before.
            assert int(r.slot_k) >= 1, r
            assert int(r.narrative_turn) >= arrived_at, (r, arrived_at)
            kind = await conn.scalar(
                text("SELECT kind::text FROM operations WHERE operation_id = :o"),
                {"o": r.operation_id},
            )
            assert kind == "inter_turn"
            # Valid-timed at the slot, never before the arrival head's end.
            floor = await world_clock.story_time_at(conn, w.story, Position.end_of(arrived_at))
            assert r.valid_time_minutes is None or r.valid_time_minutes >= floor, r
    # Every confirmed outcome whose settlement ran is settled in the active timeline or listed.
    listed = {e.outbox_id for e in (await core_outbox.listings(w.engine, w.story)).listings}
    active = [r for r in changes if r.membership == "active"]
    for row in await _rows(w.engine, w.story):
        if row.status != "confirmed" or str(row.id) not in w.ran:
            continue
        citing = [r for r in active if str(row.id) in [str(p) for p in r.prerequisite_effect_ids]]
        waiting = [r for r in citing if r.status in ("pending", "held")]
        converted = [r for r in citing if r.status == "converted"]
        settled = all(any(str(o.settles_change_id) == str(r.id) for o in active) for r in converted)
        if str(row.id) in listed:
            seen["listed"] += 1
            continue
        # Not listed: its origin is active and its settlement is in the active timeline.
        assert row.origin == "active", (row.id, row.origin)
        assert not waiting, (row.id, [(r.status, r.source) for r in citing])
        assert settled, (row.id, [(r.status, r.source) for r in citing])
        seen["settled_in_timeline"] += 1


async def _late_example(
    url: str, provider: str, drawn: list[tuple[str, int, str]], seen: Counter[str]
) -> None:
    engine = make_engine(auth_settings(url))
    try:
        w = await _late_story(engine, provider)
        for kind, pick, outcome in drawn:
            await _late_step(w, kind, pick, outcome, seen)
            await _late_check(w, seen)
        await ls.drop_jobs(engine, w.story)
        await ss.set_level(engine, w.story, None)
    finally:
        await engine.dispose()


SETTLEMENT_CASES = [
    # Arrived after the next turn: settled at that turn's slot, never at the purchase turn.
    [
        ("intent", 0, "confirmed"),
        ("turn", 0, "confirmed"),
        ("answer", 0, "confirmed"),
        ("turn", 0, "confirmed"),
        ("settle", 0, "confirmed"),
    ],
    # Settled, then its slot truncated away: listed ``confirmed_unsettled``; undone: settled.
    [
        ("intent", 0, "confirmed"),
        ("answer", 0, "confirmed"),
        ("settle", 0, "confirmed"),
        ("turn", 0, "confirmed"),
        ("truncate", 0, "confirmed"),
        ("untruncate", 0, "confirmed"),
    ],
    # The origin rewound before the confirmation was settled: ``confirmed_discarded_origin``.
    [
        ("turn", 0, "confirmed"),
        ("intent", 0, "confirmed"),
        ("answer", 0, "confirmed"),
        ("truncate", 0, "confirmed"),
        ("settle", 0, "confirmed"),
    ],
]
"""The complement: settled in the timeline, listed after a truncation, and listed with a
discarded origin."""


@pytest.mark.integration
@pytest.mark.part("M6", "FR-EXT-21")
def test_outbox_safety__settlement(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """See the module docstring (the settlement clause, #217)."""
    ss.deliver(monkeypatch)
    seen: Counter[str] = Counter()

    async def setup() -> str:
        engine = make_engine(auth_settings(pg_url))
        try:
            name = ps.unique_name("late")
            await ps.register(engine, ps.write_capable_spec(name, dedupes=True))
            return name
        finally:
            await engine.dispose()

    provider = asyncio.run(setup())

    @settings(
        max_examples=SETTLEMENT_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=settlement_steps)
    def safety(drawn: list[tuple[str, int, str]]) -> None:
        asyncio.run(_late_example(pg_url, provider, drawn, seen))

    for case in SETTLEMENT_CASES:
        safety = example(drawn=case)(safety)
    safety()
    assert seen["settled_in_timeline"] > 0, seen
    assert seen["listed"] > 0, seen
    assert seen[settlement.SETTLED] > 0, seen
    assert seen[settlement.DISCARDED] > 0, seen
