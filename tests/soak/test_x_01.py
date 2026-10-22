"""X-01: long-horizon consistency, the M4 part (docs/04 §12; #147; NFR-SCALE-2, FR-MEM-6,8,
FR-STORE-30). A ``soak`` test: deselected unless ``-m`` names the marker, run weekly by
``soak.yml`` at 20,000 turns (``EPISTREL_SOAK_TURNS`` sets fewer for a local run).

One structured story on a database of its own (``embedding_support.embedding_db``) plays
narration-only stub turns; every tenth turn also writes five memories Bobby holds, the embed job
runs every 25 turns (so the story's vectors grow), and every overflow episode's retirement job
runs then too. The budgets are lowered (Bobby 300, the story's vectors 1,500) so retirement runs
many times over the run, consolidation off (nothing is ever covered, so every episode ends at
its deadline or sooner).

Asserted, at every checkpoint (every ``CHECK_EVERY`` turns) and at the end:

- the retrieved context of a query stays within its token budget (FR-MEM-6), and a retrieval
  issues the same number of statements whatever the story's length (the bounded cost, counted,
  never timed);
- outside an open episode, Bobby's hot memories are within budget + his pins and the story's hot
  vectors within budget + its pins (FR-MEM-8, NFR-SCALE-2); every episode closed at or before its
  deadline, and none open past it; every overflow is visible (an open episode's excess is what
  the gauge sums);
- active records grow linearly (a least-squares line through the checkpoints, R² ≥ 0.99), and
  total storage (``pg_total_relation_size`` over the story tables and their indexes) grows with
  a slope, relative to its size, within 1.5 times the active records' (linear with a bounded
  constant; sublinear storage is not asserted).

Latency is reported (each checkpoint's retrieval and commit milliseconds are printed for the
run's log), not gated here: #153's ``perf.yml`` gates the timing budgets on the measured range.

``test_x_01__consolidation`` (#143, #492; FR-MEM-6) is the same bound with consolidation on: a
5,000-turn story (``EPISTREL_SOAK_CONSOLIDATION_TURNS`` sets fewer for a local run) whose
episodic memories are written in bulk (``consolidation_support.bulk_turns``, as #143's 300-turn
``test_s_consolidation_race__bounded`` does), and whose due windows run through the worker's own
``execute`` after every 50 turns with the profile's dials: every level-1 window and every
level-2 fold. At every checkpoint the context a retrieval assembles at the head has the
profile's ``retrieval_token_budget`` and its items' tokens stay within it.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import text

from epistrel.core.memory import pins, retirement
from epistrel.core.memory.consolidation import Scope, covered_to, settings_of
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.retrieval import pipeline
from epistrel.core.retrieval.assemble import tokens_of
from epistrel.orchestrator.jobs.handlers.consolidation import schedule_in
from epistrel.orchestrator.jobs.handlers.reembed import EMBED
from tests import consolidation_support as cs
from tests import hot_set_support as h
from tests import retrieval_support as r
from tests.embedding_support import Scratch, embedding_db, reset

pytestmark = [pytest.mark.soak, pytest.mark.integration]

TURNS = int(os.environ.get("EPISTREL_SOAK_TURNS", "20000"))
CHECK_EVERY = max(50, TURNS // 40)
BOBBY_BUDGET = 300
VECTOR_BUDGET = 1_500
LAG = 50
QUERY = "Tell me about the rain on the quay."
CONSOLIDATION_TURNS = int(os.environ.get("EPISTREL_SOAK_CONSOLIDATION_TURNS", "5000"))
BLOCK = 50
"""Turns written between two runs of the due consolidation windows (one default window)."""
CONSOLIDATION_CHECK_EVERY = 250


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url) as s:
        await reset(s.db)
        yield s


def _fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Least squares: slope, intercept and R²."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return slope, intercept, 1.0 - ss_res / ss_tot if ss_tot else 1.0


async def _storage(s: Scratch) -> int:
    async with s.db.connect() as conn:
        tables = list(
            await conn.scalars(
                text(
                    "SELECT DISTINCT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'story_id'"
                )
            )
        )
        total = 0
        for table in tables:
            total += int(
                await conn.scalar(
                    text("SELECT pg_total_relation_size(CAST(:t AS regclass))"), {"t": table}
                )
                or 0
            )
        return total


async def _active(s: Scratch, story: str) -> int:
    async with s.db.connect() as conn:
        return int(
            await conn.scalar(
                text("SELECT count(*) FROM memories WHERE story_id = :s AND membership = 'active'"),
                {"s": story},
            )
            or 0
        )


@pytest.mark.part("M4", "NFR-SCALE-2; FR-MEM-6,8; FR-STORE-30")
async def test_x_01(scratch: Scratch) -> None:
    """X-01 (M4): the story plays ``TURNS`` turns and every checkpoint holds the bounded
    context, the hot set's bounds outside its overflow episodes, every episode's deadline, and
    linear growth of active records and of total storage (see the module docstring)."""
    s = scratch
    db = s.db
    w = await h.story(
        db,
        memories_per_character=BOBBY_BUDGET,
        vectors_per_story=VECTOR_BUDGET,
        retirement_max_lag=LAG,
    )
    story, bobby = w.story_id, w.characters["Bobby"]
    xs: list[float] = []
    active: list[float] = []
    storage: list[float] = []
    retrieval_statements: set[int] = set()
    for n in range(1, TURNS + 1):
        started = time.perf_counter()
        if n % 10 == 0:
            await h.held(db, story, bobby, 5, importance=0.9)
        else:
            await h.narrate(db, story, 1)
        commit_ms = (time.perf_counter() - started) * 1000
        if n % 25 == 0:
            await h.run(db, EMBED, story, limit=5)
            await h.run(db, h.RETIREMENT, story, limit=20)
        if n % CHECK_EVERY:
            continue
        started = time.perf_counter()
        with r.statements(db) as seen:
            async with db.begin() as conn:
                found = await pipeline.retrieve(
                    conn, story, pipeline.RetrievalQuery(QUERY), touch=False
                )
        retrieval_ms = (time.perf_counter() - started) * 1000
        retrieval_statements.add(len(seen))
        assert found.context.used <= found.context.budget
        async with db.connect() as conn:
            counts = await retirement.exact_counts(conn, story)
            held_pins = await pins.pin_counts(conn, story)
            all_episodes = await retirement.episodes(conn, story)
            head = await retirement.head_turn(conn, story)
        open_scopes = {(e.scope, e.scope_id) for e in all_episodes if e.open}
        if ("character", bobby) not in open_scopes:
            assert counts.characters.get(bobby, 0) <= BOBBY_BUDGET + held_pins.by_character.get(
                bobby, 0
            )
        if ("story", story) not in open_scopes:
            assert counts.story_vectors <= VECTOR_BUDGET + held_pins.memory_pins
        for e in all_episodes:
            if e.open:
                assert head <= e.deadline_turn, f"episode {e.id} is past its deadline"
                assert e.excess > 0
            else:
                assert e.closed_turn is not None
                assert e.closed_turn <= e.deadline_turn
        xs.append(float(n))
        active.append(float(await _active(s, story)))
        storage.append(float(await _storage(s)))
        print(
            f"X-01 turn={n} active={int(active[-1])} storage={int(storage[-1])} "
            f"episodes={len(all_episodes)} commit_ms={commit_ms:.1f} "
            f"retrieval_ms={retrieval_ms:.1f}"
        )
    assert len(retrieval_statements) == 1, retrieval_statements
    assert len(xs) >= 5
    a_slope, _, a_r2 = _fit(xs, active)
    s_slope, _, _ = _fit(xs, storage)
    assert a_r2 >= 0.99, a_r2
    relative_active = a_slope * xs[-1] / active[-1]
    relative_storage = s_slope * xs[-1] / storage[-1]
    print(f"X-01 fit active_slope={a_slope:.3f} r2={a_r2:.5f} storage_slope={s_slope:.1f}")
    assert relative_storage <= 1.5 * relative_active, (relative_storage, relative_active)
    async with db.connect() as conn:
        closed = [e for e in await retirement.episodes(conn, story) if not e.open]
    assert closed, "the run overflowed at least once"


def _line(n: int) -> str:
    """A long line, so the retrieved candidates overflow the budget and it binds."""
    return (
        f"on day {n} the rain on the quay soaked the nets, the harbour bell rang twice, "
        "and the gulls fought over the catch while the tide turned at the old stone pier; "
        "the fishermen hauled their boats up the slipway, the lamplighter came early, "
        "the chandler shuttered his windows against the squall, and the ferry from the "
        "island waited out the weather behind the breakwater until the evening bell"
    )


@pytest.mark.part("M4", "FR-MEM-6")
async def test_x_01__consolidation(scratch: Scratch) -> None:
    """X-01 (M4), FR-MEM-6 with consolidation on: on a ``CONSOLIDATION_TURNS`` (5,000) turn
    story, every window consolidated as the profile configures it (level 1 every 50 memories,
    level 2 over every five windows), the context a retrieval assembles at the head at every
    checkpoint has the profile's ``retrieval_token_budget`` and never exceeds it, its items'
    tokens recounted. The consolidated outputs reach the context, and consolidation kept up:
    by the end every turn but the last block's is covered at level 1 and level 2 ran."""
    s = scratch
    db = s.db
    models = s.lib.models
    models.stub.when(
        "",
        cs.answer(
            facts=[("Rain soaks the quay most days.", "observation", [0])],
            reflections=[("The harbour is a wet, busy place.", "observation", [1, 2])],
        ),
        purpose="consolidation",
    )
    w = await cs.story(db)
    story, bobby = w.story_id, w.characters["Bobby"]
    async with db.connect() as conn:
        budget = (
            await effective_configuration(conn, story, validate=False)
        ).settings.memory.episodic.retrieval_token_budget
        assert (await settings_of(conn, story)).enabled
    kinds_seen: set[str] = set()
    checkpoints = 0
    binding = 0  # checkpoints whose candidates overflowed the budget (items dropped for it)
    played = 0
    started = time.perf_counter()
    while played < CONSOLIDATION_TURNS:
        count = min(BLOCK, CONSOLIDATION_TURNS - played)
        await cs.bulk_turns(db, story, count, actor=bobby, name="Bobby", line=_line)
        played += count
        async with db.begin() as conn:
            await schedule_in(conn, story)
        outcomes = await cs.run_jobs(db, models, story, limit=50)
        assert all(str(o) == "committed" for o in outcomes), outcomes
        if played % CONSOLIDATION_CHECK_EVERY and played < CONSOLIDATION_TURNS:
            continue
        async with db.begin() as conn:
            found = await pipeline.retrieve(
                conn, story, pipeline.RetrievalQuery(QUERY), touch=False
            )
        context = found.context
        recounted = sum(tokens_of(item.text) for item in context.items)
        assert context.budget == budget, (played, context.budget)
        assert recounted <= budget, (played, recounted)
        assert context.used <= budget, (played, context.used)
        kinds_seen |= {m.kind for m in found.memories}
        binding += any(d.reason == "budget" for d in context.dropped)
        checkpoints += 1
        print(
            f"X-01 consolidation turn={played} used={recounted}/{budget} "
            f"items={len(context.items)} dropped={len(context.dropped)} "
            f"windows={len(await cs.windows(db, story))} "
            f"elapsed_s={time.perf_counter() - started:.1f}"
        )
    assert checkpoints >= CONSOLIDATION_TURNS // CONSOLIDATION_CHECK_EVERY
    assert binding == checkpoints, (binding, checkpoints)  # the budget bound every time
    assert {"semantic", "reflection"} <= kinds_seen, kinds_seen
    async with db.connect() as conn:
        level1 = await covered_to(conn, story, Scope().key, 1)
        level2 = await covered_to(conn, story, Scope().key, 2)
    assert level1 >= CONSOLIDATION_TURNS - BLOCK, level1
    assert level2 > 0, level2
