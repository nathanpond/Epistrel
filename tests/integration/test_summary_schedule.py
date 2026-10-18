"""Scheduling the Lite rolling summary after a turn (FR-LITE-2, NFR-PERF-3; #282): the same
decision as before, read from what inclusion last yielded while nothing it reads has changed.

``summary_state(...).needs_work`` is the decision every scheduling used to compute, unchanged
(the job's run still uses it). The differential test wraps the scheduler's ``needs_work`` so
that every scheduling point of a story, through turns, compress and merge runs, a fork,
truncate, untruncate, regenerate, swap, SQL outside any operation and settings changes,
compares the shipped decision and stored ``Inclusion`` with that reference on the same
connection. The trigger tests pin down when migration 0046 bumps ``stories.summary_epoch`` and
when a computed inclusion is not stored. The cost test counts statements and the rows the
planner reads (``EXPLAIN ANALYZE``) on a short and a long story. It measures no time.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import summary
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.stories import delete_story
from epistrel.core.summary import Inclusion, included_segments, summary_state
from epistrel.core.tables import lite_summary_segments, operations
from epistrel.orchestrator.jobs.handlers import lite_summary
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.auth_support import auth_settings
from tests.concurrency_support import regenerate
from tests.event_support import execute_in_tx, new_story, play_turn, rows, scalar
from tests.fork_support import fork
from tests.profile_support import store_overrides
from tests.projection_support import swap, truncate, untruncate
from tests.summary_support import run_due_jobs, segment_rows, summary_jobs

SETTINGS = {"lite_summary_chunk": 2}
"""One turn per level-0 segment."""
OVERRIDES = {
    "memory": {"rolling_summary": {"window_messages": 4, "compress_threshold_tokens": 100}}
}
"""Four verbatim rows (two turns) and a 100-token cap, so two long segments merge."""
LONG = "Wren and Ada walked from the well to the mill and spoke of the brass key again. " * 3
EXPLAIN = "EXPLAIN (ANALYZE, FORMAT JSON) "


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


async def _story(engine: AsyncEngine, overrides: dict[str, Any] = OVERRIDES) -> str:
    story = await new_story(engine)
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
            {"s": story, "p": json.dumps(SETTINGS)},
        ),
    )
    await store_overrides(engine, story, overrides)
    return story


async def _epoch(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT summary_epoch FROM stories WHERE id = :s", s=story))


async def _cached(engine: AsyncEngine, story: str) -> Any:
    return await scalar(engine, "SELECT summary_inclusion FROM stories WHERE id = :s", s=story)


# -- the differential test ---------------------------------------------------------------------


@dataclass
class Checked:
    """Every scheduling decision, each compared with the pre-#282 one on the same connection."""

    decisions: list[tuple[str, str, bool]] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)
    step: str = "start"


def _checked(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Checked, Callable[[AsyncConnection, str], Awaitable[bool]]]:
    """Wrap ``core.summary.needs_work`` (the scheduler's call) so each call asserts it equals
    ``summary_state(...).needs_work`` and that the ``Inclusion`` it used equals the one computed
    from ``included_segments``; counts the calls that had to recompute inclusion."""
    shipped = summary.needs_work
    computing = summary.included_segments
    seen = Checked()
    inside = [False]

    async def counted(conn: AsyncConnection, story_id: str, **kwargs: Any) -> Any:
        if inside[0]:
            seen.recomputed.append(seen.step)
        return await computing(conn, story_id, **kwargs)

    async def needs_work(conn: AsyncConnection, story_id: str) -> bool:
        expected = (await summary_state(conn, story_id)).needs_work
        reference = Inclusion.of(await computing(conn, story_id))
        inside[0] = True
        try:
            decided = await shipped(conn, story_id)
            stored = await summary.inclusion(conn, story_id)
        finally:
            inside[0] = False
        assert decided == expected, (seen.step, story_id)
        assert stored == reference, (seen.step, story_id, stored, reference)
        seen.decisions.append((seen.step, story_id, decided))
        return decided

    monkeypatch.setattr(summary, "included_segments", counted)
    monkeypatch.setattr(summary, "needs_work", needs_work)
    return seen, needs_work


async def test_scheduling_decides_as_before_at_every_point(
    engine: AsyncEngine, models: ModelAccessLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-LITE-2 (#282): at every scheduling point, and after every change made outside one,
    the shipped decision equals ``summary_state(...).needs_work`` and the stored inclusion
    equals the computed one. Complements asserted: both decisions occur, plain turns on an
    unchanged summary reuse the stored inclusion (no recomputation, no epoch bump), and every
    kind of change does make the next scheduling recompute."""
    seen, needs_work = _checked(monkeypatch)
    stub = models.stub
    stub.script("lite_summary", [f"{n}: {LONG}" for n in range(200)])
    story = await _story(engine)

    async def check(step: str, story_id: str = story) -> bool:
        seen.step = step
        async with engine.begin() as conn:
            return await needs_work(conn, story_id)

    async def turns(step: str, *lines: str, story_id: str = story) -> None:
        seen.step = step
        for line in lines:
            await play_turn(engine, story_id, line)

    await turns("first turns", "line 1", "line 2", "line 3", "line 4")
    assert [j.status for j in await summary_jobs(engine, story)] == ["queued"]
    seen.step = "job"
    await run_due_jobs(engine, models, story)
    assert any(r.level > 0 for r in await segment_rows(engine, story))  # it merged
    await turns("after the job", "line 5")

    # Plain turns on an unchanged summary reuse what is stored.
    epoch = await _epoch(engine, story)
    await turns("plain", "line 6", "line 7")
    assert await _epoch(engine, story) == epoch
    assert "plain" not in seen.recomputed
    assert await check("plain, checked") is True  # turns 5-7 wait, outside the window

    seen.step = "job again"
    await run_due_jobs(engine, models, story)
    await check("after the second job")

    seen.step = "fork"
    copy = (await fork(engine, story, at_turn=6)).story_id
    await check("fork, checked", copy)
    seen.step = "fork's job"
    await run_due_jobs(engine, models, copy)
    await check("fork after its job", copy)
    await check("source after the fork")

    seen.step = "truncate"
    assert await truncate(engine, story, 1) is not None
    await check("truncated")
    seen.step = "untruncate"
    assert await untruncate(engine, story) is not None
    await check("untruncated")

    seen.step = "truncate to 2"
    assert await truncate(engine, story, 2) is not None
    seen.step = "regenerate"
    await regenerate(engine, story)
    await check("regenerated")
    seen.step = "swap"
    assert await swap(engine, story) is not None
    await check("swapped")
    await turns("after the swap", "line 8")

    # Changes outside any operation: a segment's source, text and row; settings.
    segments = await segment_rows(engine, story)
    level0 = next(r for r in segments if r.level == 0 and r.membership == "superseded")
    merged = next(r for r in segments if r.level > 0 and r.membership == "active")
    for step, sql, params in (
        (
            "a source dormant",
            "UPDATE operations SET membership = 'dormant' WHERE operation_id = :o",
            {"o": level0.source_versions[0]["operation_id"]},
        ),
        (
            "the source back",
            "UPDATE operations SET membership = 'active' WHERE operation_id = :o",
            {"o": level0.source_versions[0]["operation_id"]},
        ),
        (
            "a merged segment's operation discarded",
            "UPDATE operations SET membership = 'discarded' WHERE operation_id = :o",
            {"o": merged.operation_id},
        ),
        (
            "and active again",
            "UPDATE operations SET membership = 'active' WHERE operation_id = :o",
            {"o": merged.operation_id},
        ),
        (
            "a text shortened",
            "UPDATE lite_summary_segments SET text = 'short' WHERE segment_id = :g",
            {"g": merged.segment_id},
        ),
        (
            "a text lengthened",
            "UPDATE lite_summary_segments SET text = repeat('long ', 400) WHERE segment_id = :g",
            {"g": merged.segment_id},
        ),
        (
            "a row deleted",
            "DELETE FROM lite_summary_segments WHERE segment_id = :g",
            {"g": merged.segment_id},
        ),
    ):
        seen.recomputed.clear()
        await execute_in_tx(engine, (sql, params))
        await check(step)
        assert seen.recomputed == [step], step  # the change made it recompute

    seen.step = "settings"
    await store_overrides(engine, story, {"memory": {"rolling_summary": {"window_messages": 40}}})
    assert await check("a wider window") is False
    await store_overrides(engine, story, {"memory": {"rolling_summary": {"enabled": False}}})
    assert await check("summary off") is False
    await store_overrides(engine, story, OVERRIDES)
    await check("summary on")

    decided = {d for _, _, d in seen.decisions}
    assert decided == {True, False}
    assert len(seen.recomputed) < len(seen.decisions)


# -- the triggers ------------------------------------------------------------------------------


async def _segmented(engine: AsyncEngine, turns: int) -> str:
    """A story of ``turns`` active turns (``turns`` a power of two) whose summary is a merge
    tree written as rows (no events): a level-0 segment per turn and pairwise merges up to one
    segment covering them all, every child superseded; its own consolidation operation each."""
    story = await _story(engine)
    turn_ops = [
        {
            "operation_id": new_ulid(),
            "story_id": story,
            "kind": OperationKind.TURN.value,
            "status": "committed",
            "principal": "p",
            "narrative_turn": n,
            "turn_variant": 1,
            "parent_variant": 1 if n > 1 else None,
            "membership": "active",
        }
        for n in range(1, turns + 1)
    ]
    segments: list[dict[str, Any]] = []
    consolidations: list[dict[str, Any]] = []
    level = [
        (n, n, [{"operation_id": op["operation_id"], "membership": "active"}])
        for n, op in enumerate(turn_ops, start=1)
    ]
    depth = 0
    while True:
        made = []
        for covers_from, covers_to, sources in level:
            operation_id = new_ulid()
            consolidations.append(
                {
                    "operation_id": operation_id,
                    "story_id": story,
                    "kind": OperationKind.CONSOLIDATION.value,
                    "status": "committed",
                    "principal": "p",
                    "narrative_turn": covers_to,
                    "turn_variant": 1,
                    "parent_variant": None,
                    "membership": "active",
                }
            )
            segments.append(
                {
                    "segment_id": operation_id,
                    "story_id": story,
                    "seq": len(segments) + 1,
                    "operation_id": operation_id,
                    "covers_from": covers_from,
                    "covers_to": covers_to,
                    "level": depth,
                    "turn_variant": 1,
                    "text": f"Turns {covers_from}-{covers_to}. " + "x" * 300,
                    "membership": "active" if len(level) == 1 else "superseded",
                    "source_versions": sources,
                }
            )
            made.append((covers_from, covers_to, operation_id))
        if len(made) == 1:
            break
        level = [
            (a[0], b[1], [{"operation_id": x[2], "membership": "active"} for x in (a, b)])
            for a, b in zip(made[::2], made[1::2], strict=True)
        ]
        depth += 1
    async with engine.begin() as conn:
        await conn.execute(insert(operations), turn_ops + consolidations)
        await conn.execute(insert(lite_summary_segments), segments)
        await conn.execute(
            text("UPDATE stories SET next_turn_index = :n WHERE id = :s"),
            {"n": turns + 1, "s": story},
        )
    return story


async def _inclusion(engine: AsyncEngine, story: str) -> Inclusion:
    async with engine.begin() as conn:
        return await summary.inclusion(conn, story)


async def test_the_epoch_moves_exactly_when_an_input_of_inclusion_changes(
    engine: AsyncEngine,
) -> None:
    """Migration 0046's triggers (#282): a segment row's insert, update or delete, and an
    operation entering or leaving "committed and active", bump the story's epoch once per
    transaction; a plain turn's commit, an operation started (``in_flight``) and a change
    between two inactive memberships do not. A stored inclusion is used only at its epoch."""
    story = await _segmented(engine, 4)
    assert await _cached(engine, story) is None
    first = await _inclusion(engine, story)
    assert first == Inclusion(covered_to=4, tokens=first.tokens, segments=1)
    epoch = await _epoch(engine, story)
    assert (await _cached(engine, story))["epoch"] == epoch

    async def moved(*statements: tuple[str, dict[str, Any]]) -> int:
        before = await _epoch(engine, story)
        await execute_in_tx(engine, *statements)
        return await _epoch(engine, story) - before

    await play_turn(engine, story, "a plain turn")
    assert await _epoch(engine, story) == epoch
    started = Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p")
    await started.begin()
    assert await _epoch(engine, story) == epoch
    await started.abandon()
    assert await _epoch(engine, story) == epoch  # in_flight to abandoned: never active

    top = (
        await rows(
            engine,
            "SELECT * FROM lite_summary_segments WHERE story_id = :s AND membership = 'active'",
            s=story,
        )
    )[0]
    child = top.source_versions[0]["operation_id"]
    update_text = "UPDATE lite_summary_segments SET text = :t WHERE segment_id = :g"
    assert await moved((update_text, {"t": "a", "g": top.segment_id})) == 1
    assert (
        await moved(
            (update_text, {"t": "b", "g": top.segment_id}),
            (update_text, {"t": "c", "g": top.segment_id}),
            ("UPDATE operations SET membership = 'dormant' WHERE operation_id = :o", {"o": child}),
        )
        == 1
    )  # once per transaction
    assert (
        await moved(
            ("UPDATE operations SET membership = 'discarded' WHERE operation_id = :o", {"o": child})
        )
        == 0
    )  # dormant to discarded: inclusion reads only "committed and active"
    assert (
        await moved(
            ("UPDATE operations SET membership = 'active' WHERE operation_id = :o", {"o": child})
        )
        == 1
    )
    assert (
        await moved(
            (
                "INSERT INTO operations (operation_id, story_id, kind, status, principal, "
                "membership) VALUES (:o, :s, 'maintenance', 'committed', 'p', 'active')",
                {"o": new_ulid(), "s": story},
            )
        )
        == 1
    )
    assert (
        await moved(
            ("DELETE FROM lite_summary_segments WHERE segment_id = :g", {"g": top.segment_id})
        )
        == 1
    )
    after = await _inclusion(engine, story)
    assert after == Inclusion.of(await _included(engine, story))
    assert (after.covered_to, after.segments) == (4, 2)  # the children stand in for the top

    # A whole-story deletion bumps nothing (and is not refused).
    await delete_story(engine, story, principal="p-author")
    assert await scalar(engine, "SELECT count(*) FROM stories WHERE id = :s", s=story) == 0


async def _included(engine: AsyncEngine, story: str) -> tuple[summary.Segment, ...]:
    async with engine.connect() as conn:
        return await included_segments(conn, story)


async def test_an_inclusion_is_stored_only_for_the_epoch_it_was_computed_at(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#282: a computed inclusion is not stored while another transaction holds the story row
    (it does not wait either), nor when the epoch moved while it was computed; stored inside a
    transaction that changed an input, a later change in that transaction still bumps the
    epoch, so the stored value is never stale."""
    story = await _segmented(engine, 4)

    # The story row is locked (a commit is running): computed, not stored, no wait.
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM stories WHERE id = :s FOR UPDATE"), {"s": story})
        found = await _inclusion(engine, story)
        await holder.rollback()
    assert found == Inclusion.of(await _included(engine, story))
    assert await _cached(engine, story) is None

    # A change commits while it is computed: not stored for the epoch it read.
    computing = summary.included_segments

    async def meanwhile(conn: AsyncConnection, story_id: str, **kwargs: Any) -> Any:
        found = await computing(conn, story_id, **kwargs)
        await execute_in_tx(
            engine,
            (
                "UPDATE lite_summary_segments SET text = 'changed' WHERE story_id = :s "
                "AND membership = 'active'",
                {"s": story_id},
            ),
        )
        return found

    monkeypatch.setattr(summary, "included_segments", meanwhile)
    stale = await _inclusion(engine, story)
    monkeypatch.setattr(summary, "included_segments", computing)
    assert await _cached(engine, story) is None
    assert stale != Inclusion.of(await _included(engine, story))  # it read the old text
    assert await _inclusion(engine, story) == Inclusion.of(await _included(engine, story))
    assert (await _cached(engine, story))["epoch"] == await _epoch(engine, story)

    # Changed, stored and changed again in one transaction: the second change bumps too.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE lite_summary_segments SET text = 'one' WHERE story_id = :s"),
            {"s": story},
        )
        await summary.inclusion(conn, story)
        await conn.execute(
            text("UPDATE lite_summary_segments SET text = repeat('two ', 200) WHERE story_id = :s"),
            {"s": story},
        )
    cached = await _cached(engine, story)
    assert cached["epoch"] != await _epoch(engine, story)
    assert await _inclusion(engine, story) == Inclusion.of(await _included(engine, story))


# -- the cost test -----------------------------------------------------------------------------


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[tuple[str, Any]]]:
    seen: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        seen.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _rows_read(plan: dict[str, Any]) -> int:
    """Rows every scan node produced or filtered out, across its loops."""
    total = 0
    if "Relation Name" in plan:
        loops = int(plan.get("Actual Loops", 1))
        total += int(plan.get("Actual Rows", 0)) * loops
        total += int(plan.get("Rows Removed by Filter", 0)) * loops
        total += int(plan.get("Rows Removed by Index Recheck", 0)) * loops
    for child in plan.get("Plans", ()):
        total += _rows_read(child)
    return total


def _seq_scans(plan: dict[str, Any]) -> set[str]:
    found = {plan["Relation Name"]} if plan.get("Node Type") == "Seq Scan" else set()
    for child in plan.get("Plans", ()):
        found |= _seq_scans(child)
    return found


async def _explained_rows(engine: AsyncEngine, statement: str, parameters: Any) -> int:
    """The rows ``statement`` reads with sequential scans disabled. A small table's sequential
    scan reads the whole table, so its count follows the rest of the database (other tests'
    stories, stale statistics); with them off the planner must use an index, which every
    statement on the scheduling path has, keyed by the story or a primary key. A statement
    without one would still scan, and fails here."""
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        raw = await conn.exec_driver_sql(EXPLAIN + statement, parameters)
        found = raw.scalar_one()
        await conn.rollback()
    plan = dict((found if isinstance(found, list) else json.loads(found))[0]["Plan"])
    assert not _seq_scans(plan), (statement, plan)
    return _rows_read(plan)


async def _scheduling_cost(engine: AsyncEngine, story: str) -> tuple[bool, list[int]]:
    """One plain turn's scheduling: its decision and, per statement, the rows its plan reads
    (``-1`` for a write, which is not explained)."""
    with _statements(engine) as seen:
        decided = await lite_summary.schedule(engine, story)
    cost = []
    for statement, parameters in seen:
        if statement.lstrip().upper().startswith("SELECT"):
            cost.append(await _explained_rows(engine, statement, parameters))
        else:
            cost.append(-1)
    return decided, cost


async def test_scheduling_cost_is_independent_of_story_length(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-PERF-3, FR-LITE-2 (#282): with the summary unchanged since the last scheduling, a
    plain turn's scheduling on a 2,052-turn story with 4,095 segments issues the same statements
    as on an 8-turn story with 7 segments, every one of them reads the same number of rows
    (``EXPLAIN ANALYZE``, every one through an index), and ``included_segments`` is never
    called. Unrelated stories are seeded around them (and the session's other tests may have
    left more), so the counts are independent of the other stories too. Complement: computing
    inclusion, as every scheduling did before, reads every segment: 4,095 against 7."""
    for _ in range(20):
        await _story(engine)  # unrelated stories with their own overrides
    short = await _segmented(engine, 4)
    others = [await _segmented(engine, 256) for _ in range(3)]
    long = await _segmented(engine, 2_048)
    for story in others:
        await play_turn(engine, story, "elsewhere")
    for story in (short, long):
        for n in (1, 2, 3):
            await play_turn(engine, story, f"tail {n}")  # their scheduling stores inclusion
    async with engine.connect() as conn:
        for table in ("operations", "lite_summary_segments", "transcript_messages", "stories"):
            await conn.execute(text(f"ANALYZE {table}"))
        await conn.commit()

    computed: list[str] = []
    computing = summary.included_segments

    async def counted(conn: AsyncConnection, story_id: str, **kwargs: Any) -> Any:
        computed.append(story_id)
        return await computing(conn, story_id, **kwargs)

    monkeypatch.setattr(summary, "included_segments", counted)
    epochs = [await _epoch(engine, s) for s in (short, long)]
    await play_turn(engine, short, "measured")
    await play_turn(engine, long, "measured")
    assert [await _epoch(engine, s) for s in (short, long)] == epochs
    short_decided, short_cost = await _scheduling_cost(engine, short)
    long_decided, long_cost = await _scheduling_cost(engine, long)
    assert computed == []
    assert short_decided is long_decided is True  # turns after the tree wait outside the window
    assert len(short_cost) == len(long_cost)
    assert short_cost == long_cost

    full = [
        await scalar(
            engine, "SELECT count(*) FROM lite_summary_segments WHERE story_id = :s", s=story
        )
        for story in (short, long)
    ]
    assert full == [7, 4_095]
    assert sum(long_cost) < full[1], long_cost
    async with engine.connect() as conn:
        assert (await included_segments(conn, long))[-1].covers_to == 2_048
