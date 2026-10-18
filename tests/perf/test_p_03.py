"""P-03: turn-commit latency (docs/04), the M2 part (NFR-PERF-3; #76): an atomic turn commit
completes within p95 ≤ 250 ms, excluding model time.

The span timed is ``Operation.commit()`` of a Lite ``turn`` operation, exactly as the pipeline
commits one, with no model call anywhere: under the story row lock, the FR-CONC-4 decision over
the Lite read set, positioning, the two beats' events and every projection, the turn report
(a commit step), the #61 ``done`` row with its result built in the transaction, the turn slot's
release. The summary port's scheduling after the transaction (#72) is muted, as it is not
part of the atomic commit; P-04 reports it as the ``post_commit`` stage. Everything before it
(the turn slot, the key's claim, the read phase and staging) is set up outside the timing,
round by round.

``test_p_03`` is the requirement's measurement: the reference workload's story
(``epistrel.testing.perf``: 10,000 turns, i.e. 20,000 events, and 50 pinned memories), 50 warm-up
rounds, then 1,000 timed ones. pytest-benchmark runs them (``pedantic``, one commit a round, the
round's setup untimed) and the gate reads its per-round timings. Seeding that story and the
rounds take minutes, so it is ``perf_long``: deselected unless ``-m`` names the marker, and run
every night by ``perf.yml`` (``-m "perf_quick or perf_long"``, ``--benchmark-json`` uploaded).
``test_p_03__quick`` is PR CI's subset (``perf_quick``): the same commit on a 2,000-event story,
200 timed rounds after 20 warm-up. Each writes its figures to
``build/perf/p_03_<events>_events.json``.

**Thresholds.** 250 ms is NFR-PERF-3's own budget, unchanged for both. Measured locally (Apple
silicon, Postgres in Docker): p50 31 ms, p95 34 ms, max 94 ms on the 20,000-event story, and p50
17 ms, p95 21 ms, max 26 ms on the 2,000-event one. Those figures predate #281: the FR-STORE-28
chain check then read every active turn, so the commit grew with the story and the quick subset
sat lower; it now checks only the chain's head, with index lookups (``test_chain_check``). The
margin to 250 ms is what absorbs a slower CI runner or the full suite's load, while a regression
that multiplies the commit's cost still fails. 2 s for any single commit is the complement (a
stuck lock, or a commit waiting on another transaction): well under the commit's 5 s
``lock_timeout`` and twenty times the slowest commit seen locally.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.config import Settings
from epistrel.core import turn_gate
from epistrel.core.concurrency import LiteReadSet, decide
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.history import turn_beats
from epistrel.core.idempotency import IdempotentCall, ResultCodec, Scope, calling
from epistrel.core.tables import operations
from epistrel.core.turns import stage_turn
from epistrel.engine import Engine
from epistrel.testing import perf
from tests.auth_support import auth_settings

ROUNDS = 1_000
WARMUP = 50
QUICK_TURNS = 1_000
QUICK_ROUNDS = 200
QUICK_WARMUP = 20
P95_BUDGET_S = 0.250
"""NFR-PERF-3."""
STUCK_S = 2.0
"""No single commit may take longer (the complement: a stuck lock)."""
LEASE_S = 60.0
PRINCIPAL = "p-perf"
CODEC: ResultCodec[dict[str, Any]] = ResultCodec.of(dict[str, Any])
RESULT_DIR = Path("build/perf")


@dataclass
class Round:
    """One prepared commit: its operation and the idempotent call it runs under."""

    op: Operation
    call: IdempotentCall


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


class Bench:
    """The seeded story and the per-round preparation, on one event loop (``runner``)."""

    def __init__(self, runner: asyncio.Runner, settings: Settings) -> None:
        self.runner = runner
        self.db = make_engine(settings)
        self.epistrel = Engine(settings, db=self.db)
        self.ctx = RequestContext(principal=PRINCIPAL)
        self.story: perf.SeededStory | None = None
        self.viewpoint = ""
        self.rounds = 0

    async def seed(self, turns: int) -> perf.SeededStory:
        self.story = await perf.seed_story(
            self.epistrel, self.ctx, "P-03 reference story", turns=turns
        )
        self.viewpoint = f"character:{self.story.player_id}"
        return self.story

    async def prepare(self) -> Round:
        assert self.story is not None
        story_id = self.story.story_id
        self.rounds += 1
        key = f"p03-{self.rounds}"
        op = Operation(
            self.db,
            story_id=story_id,
            kind=OperationKind.TURN,
            principal=PRINCIPAL,
            idempotency_key=key,
            viewpoint=self.viewpoint,
        )
        op.read_set_source = LiteReadSet()
        held = await turn_gate.acquire(self.db, story_id, op.operation_id, lease_s=LEASE_S)
        assert held is None, held
        op.holds_turn_slot = True
        call = IdempotentCall(
            self.db,
            scope=Scope.STORY,
            scope_id=story_id,
            key=key,
            operation="respond",
            principal=PRINCIPAL,
            codec=CODEC,
            wait_s=5.0,
        )
        with calling(call):
            await call.claim({"story_id": story_id, "round": self.rounds})
        await op.begin()
        stage_turn(
            op,
            actor_character_id=self.story.player_id,
            say=f"Round {self.rounds}: the lantern is lit.",
            do=None,
            reply="The innkeeper nods and pours another cup.",
            render_character_ids=[self.story.other_id],
            model="perf",
            prompt_hash="0" * 64,
            finish_reason="stop",
        )

        async def report(conn: AsyncConnection) -> None:
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id == op.operation_id)
                .values(turn_report={"model_calls": [], "call_counts": {}, "elapsed_ms": 0})
            )

        async def result(conn: AsyncConnection, committed: Operation) -> dict[str, Any]:
            beats = await turn_beats(conn, story_id, committed.operation_id, self.viewpoint)
            return {
                "operation_id": committed.operation_id,
                "timeline_revision": committed.timeline_revision,
                "beats": len(beats),
            }

        op.add_commit_step(report)
        call.result_in_commit = result
        return Round(op, call)

    async def commit(self, prepared: Round) -> None:
        # The summary port's post-commit scheduling is outside the atomic commit: it is muted
        # here and measured apart in P-04 (the ``post_commit`` stage).
        with calling(prepared.call), perf.summaries_muted():
            await prepared.op.commit(decide=decide)

    async def finish(self, prepared: Round) -> None:
        assert prepared.call.stored_in_commit
        await prepared.call.finish(None)

    async def close(self) -> None:
        try:
            if self.story is not None:
                await self.epistrel.delete_story(self.ctx, self.story.story_id)
        finally:
            await self.epistrel.models.aclose()
            await self.db.dispose()


@pytest.mark.perf_long
@pytest.mark.part("M2", "NFR-PERF-3")
def test_p_03(stub_settings: Settings, benchmark: Any) -> None:
    """1,000 turn commits on a 20,000-event story: p95 ≤ 250 ms (the nightly ``perf.yml``).

    Complement asserted: no single commit takes 2 s or more (a stuck lock fails the run even
    when the p95 holds), and every round committed exactly one turn with its result stored
    under its key."""
    _measured(stub_settings, benchmark, turns=perf.TURNS, rounds=ROUNDS, warmup=WARMUP)


@pytest.mark.perf_quick
@pytest.mark.part("M2", "NFR-PERF-3")
def test_p_03__quick(stub_settings: Settings, benchmark: Any) -> None:
    """PR CI's subset: 200 turn commits on a 2,000-event story, the same p95 ≤ 250 ms.

    Complement asserted: as ``test_p_03``, no single commit takes 2 s or more."""
    _measured(stub_settings, benchmark, turns=QUICK_TURNS, rounds=QUICK_ROUNDS, warmup=QUICK_WARMUP)


def _measured(settings: Settings, benchmark: Any, *, turns: int, rounds: int, warmup: int) -> None:
    """Seed a reference story of ``turns`` turns, time ``rounds`` commits after ``warmup``,
    write the figures to ``build/perf/`` and gate them."""
    with asyncio.Runner() as runner:
        bench = Bench(runner, settings)
        try:
            story = runner.run(bench.seed(turns))
            assert story.turns == turns
            pending: list[Round] = []

            def setup() -> tuple[tuple[Round], dict[str, Any]]:
                if pending:  # the previous round's call is finished outside the timing
                    runner.run(bench.finish(pending.pop()))
                prepared = runner.run(bench.prepare())
                pending.append(prepared)
                return (prepared,), {}

            def target(prepared: Round) -> None:
                runner.run(bench.commit(prepared))

            benchmark.pedantic(
                target, setup=setup, rounds=rounds, warmup_rounds=warmup, iterations=1
            )
            if pending:
                runner.run(bench.finish(pending.pop()))
            timings = list(benchmark.stats.stats.data)
            counted = runner.run(_committed_turns(bench, story.story_id))
        finally:
            runner.run(bench.close())

    assert len(timings) == rounds, "run without --benchmark-disable: P-03 needs every round"
    p50, p95, worst = (
        perf.percentile(timings, 50),
        perf.percentile(timings, 95),
        max(timings),
    )
    summary = {
        "rounds": len(timings),
        "warmup": warmup,
        "events_before": turns * 2,
        "p50_ms": round(p50 * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
        "max_ms": round(worst * 1000, 2),
        "budget_p95_ms": P95_BUDGET_S * 1000,
        "stuck_ms": STUCK_S * 1000,
    }
    out = RESULT_DIR / f"p_03_{turns * 2}_events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"P-03 commit latency: {json.dumps(summary)}")
    assert counted == turns + warmup + rounds, counted
    assert worst < STUCK_S, summary
    assert p95 <= P95_BUDGET_S, summary


async def _committed_turns(bench: Bench, story_id: str) -> int:
    from sqlalchemy import func, select

    async with bench.db.connect() as conn:
        found = await conn.scalar(
            select(func.count())
            .select_from(operations)
            .where(
                operations.c.story_id == story_id,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == "committed",
            )
        )
    return int(found or 0)
