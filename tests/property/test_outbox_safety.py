"""OUTBOX-SAFETY — external effects follow their turn's membership and are never sent twice
where that is unsafe (FR-EXT-12, FR-EXT-18; docs/04 §6; #210's dispatch clauses).

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
- ``expire``: the row's lease timer at the lease's end (``outbox.expire``).

Asserted per example, against Postgres:

- no effect is ever sent whose operation was not ``active`` when it was claimed (the claim's
  membership re-read, in the claim's transaction);
- every claimed effect is recorded with an outcome or listed by the reconciliation status, and
  none is ever ``cancelled``;
- no effect is sent twice to the provider that does not deduplicate;
- a retry under one idempotency key never produces a second dispatch: every send carries its
  row's id, and a row is sent once per claim (a repeated dispatch job of a claimed or settled
  row sends nothing).

The complement (``CASES``, one explicit example each, #514's pattern): an active-origin row is
sent exactly once and confirmed; a row whose turn was rewound past is cancelled unsent; a
claimed row's lease lapsing with the provider that does not deduplicate makes it ``unknown``
once; and with the one that does, it is re-sent under the same key.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, timeline
from epistrel.core import outbox as core_outbox
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.variants import ChainRef, SwapRequest, list_chains
from epistrel.orchestrator.jobs.handlers import outbox
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from tests import provider_support as ps
from tests.auth_support import auth_settings
from tests.event_support import new_story

PRINCIPAL = "p-test"
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
    # the swap) are cancelled, never sent, while the restored turn's own rows are sent.
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


@dataclass
class World:
    engine: AsyncEngine
    story: str
    registry: InProcessRegistry
    fakes: dict[str, ps.ScriptedProvider]
    dedupes: dict[str, bool]
    claimed_active: dict[str, bool] = field(default_factory=dict)
    """Each row ever claimed: whether its operation was active at that claim."""


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


async def _step(w: World, kind: str, pick: int, tally: Tally) -> None:
    engine, story = w.engine, w.story
    if kind == "turn":
        await _turn(w)
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
    if done == outbox.CANCELLED:
        assert not active
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
    for row in rows.values():
        if row.attempts:
            assert row.status != "cancelled", row.id
            if row.status != "confirmed":
                assert row.id in listed, row.id
                assert listed[row.id].reason in ("unconfirmed", "failed", "unknown")
        if row.status == "cancelled":
            assert row.attempts == 0


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
    assert covered == {f.name for f in fields(Tally)} - {"examples"}
    for name, (counter, _) in CASES.items():
        assert by_case[name].examples >= 1, name
        assert getattr(by_case[name], counter) > 0, (name, by_case[name])
    assert tally.examples == EXAMPLES + len(CASES)
