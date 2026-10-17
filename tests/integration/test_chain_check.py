"""The commit's chain check (FR-STORE-28, TIME (f); #281): a head check on a chain known to be
valid, the full check otherwise, and the same refusals as the full check alone.

``chain_violation`` is the check every commit used to run, unchanged. Forcing it on every
commit (``_head_holds`` patched to fail) reproduces the old behaviour, so the differential test
runs each way of breaking the chain twice, once under the old check and once under the new one,
on identical stories, and compares the outcomes. The cost test counts statements and the rows
the planner reads (``EXPLAIN ANALYZE``) on a short and a long story. It measures no time.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.errors import ChainBroken
from epistrel.core.events import Operation
from epistrel.core.events import operation as operation_module
from epistrel.core.events.operation import chain_violation
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import execute_in_tx, new_story, play_turn, scalar
from tests.fork_support import fork
from tests.projection_support import swap, truncate, untruncate

ACTIVE = "kind = 'turn' AND status = 'committed' AND membership = 'active'"


EXPLAIN = "EXPLAIN (ANALYZE, FORMAT JSON) "


def _sql(statement: str) -> str:
    """``statement`` with ``ACTIVE`` spelled out (the rows ``uq_operations_active_turn`` covers)."""
    return statement.replace("ACTIVE", ACTIVE)


BREAKERS: dict[str, str] = {
    # Refused by the full check.
    "gap": _sql(
        "UPDATE operations SET membership = 'discarded' WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 2"
    ),
    "dormant_head": _sql(
        "UPDATE operations SET membership = 'dormant' WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 4"
    ),
    "orphan": _sql(
        "UPDATE operations SET parent_variant = 7 WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 3"
    ),
    "orphan_of_head": _sql(
        "UPDATE operations SET parent_variant = 7 WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 4"
    ),
    "index_ahead": "UPDATE stories SET next_turn_index = next_turn_index + 1 WHERE id = :s",
    "turn_zero": _sql(
        "UPDATE operations SET narrative_turn = 0 WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 1"
    ),
    "moved": _sql(
        "UPDATE operations SET narrative_turn = 9 WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 2"
    ),
    "revived_variant": (
        "UPDATE operations SET membership = 'active' WHERE story_id = :s "
        "AND kind = 'turn' AND narrative_turn = 4 AND turn_variant = 1"
    ),
    # Accepted by the full check (in some modes).
    "index_behind": "UPDATE stories SET next_turn_index = next_turn_index - 1 WHERE id = :s",
    "tail_discard": _sql(
        "UPDATE operations SET membership = 'discarded' WHERE story_id = :s AND ACTIVE "
        "AND narrative_turn = 4; UPDATE stories SET next_turn_index = 4 WHERE id = :s"
    ),
    "report": _sql(
        """UPDATE operations SET turn_report = '{"x": 1}' WHERE story_id = :s AND ACTIVE """
        "AND narrative_turn = 2"
    ),
    "noop": _sql(
        "UPDATE operations SET membership = membership, parent_variant = parent_variant "
        "WHERE story_id = :s AND ACTIVE"
    ),
}
"""Ways a change can leave the active turns of a 4-turn story (turn 4 regenerated, so variant 1
at turn 4 is dormant) not one chain, and changes that keep it one."""

MODES = ("turn_step", "work_step", "control_step", "outside")
"""Where the change happens: a commit step of a plain turn (the hot path), of a non-turn
operation that is no control action, of a control action, or SQL outside any operation followed
by a plain turn."""


async def _story(engine: AsyncEngine) -> str:
    story = await new_story(engine)
    for n in range(1, 5):
        await play_turn(engine, story, f"turn {n}")
    await regenerate(engine, story)
    assert await chain(engine, story) == [(1, 1), (2, 1), (3, 1), (4, 2)]
    return story


async def _apply(conn: AsyncConnection, story: str, sql: str) -> None:
    for statement in sql.split("; "):
        await conn.execute(text(statement), {"s": story})


def _turn(engine: AsyncEngine, story: str) -> Operation:
    return Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p")


async def _commit(op: Operation, step: Callable[[AsyncConnection], Awaitable[None]]) -> None:
    async with op:
        if op.kind is OperationKind.TURN:
            op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": "and so on"})
        op.add_commit_step(step)
        await op.commit()


async def _run(engine: AsyncEngine, mode: str, sql: str) -> tuple[Any, ...]:
    """Break (or keep) a fresh story's chain in ``mode``; the outcome and the resulting chain."""
    story = await _story(engine)

    async def step(conn: AsyncConnection) -> None:
        await _apply(conn, story, sql)

    async def nothing(conn: AsyncConnection) -> None:
        return None

    if mode in ("outside", "turn_step"):
        op = _turn(engine, story)
    else:
        control = mode == "control_step"
        op = Operation(
            engine,
            story_id=story,
            kind=OperationKind.CONTROL,
            principal="p",
            bumps_revision=control,
            control_action=control,
        )
    try:
        if mode == "outside":
            async with engine.begin() as conn:
                await _apply(conn, story, sql)
            step = nothing
        await _commit(op, step)
        outcome: tuple[Any, ...] = ("committed",)
    except ChainBroken as refused:
        outcome = ("chain_broken", {k: v for k, v in refused.details.items() if k != "story_id"})
    except IntegrityError:
        outcome = ("integrity_error",)
    index = await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
    return (*outcome, await chain(engine, story), index)


@contextmanager
def _old_check(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every commit that checks the chain runs the full check, as before #281."""
    with monkeypatch.context() as patched:
        patched.setattr(Operation, "_head_holds", lambda self, probe: False)
        yield


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("breaker", sorted(BREAKERS))
async def test_chain_check_refuses_what_the_full_check_refuses(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, breaker: str, mode: str
) -> None:
    """FR-STORE-28: for every way of breaking the chain, and for changes that keep it one
    chain, the commit has the same outcome under the head check as under the full check alone
    (refused with the same `ChainBroken` details, or committed with the same chain)."""
    with _old_check(monkeypatch):
        old = await _run(engine, mode, BREAKERS[breaker])
    new = await _run(engine, mode, BREAKERS[breaker])
    assert new == old
    if breaker in ("gap", "orphan", "dormant_head", "moved", "turn_zero"):
        assert new[0] == "chain_broken"
    if breaker in ("report", "noop"):
        assert new[0] == "committed"


async def test_existing_refusal_is_unchanged(engine: AsyncEngine) -> None:
    """FR-STORE-28: the original failure case (a control action discarding a middle turn) is
    still refused with the full check's details and leaves nothing behind."""
    story = await _story(engine)

    async def discard_middle(conn: AsyncConnection) -> None:
        await _apply(conn, story, BREAKERS["gap"])

    op = Operation(
        engine,
        story_id=story,
        kind=OperationKind.CONTROL,
        principal="p",
        bumps_revision=True,
        control_action=True,
    )
    with pytest.raises(ChainBroken) as refused:
        await _commit(op, discard_middle)
    assert refused.value.details == {
        "story_id": story,
        "active_turns": 3,
        "highest_turn": 4,
        "next_turn_index": 5,
        "orphaned_turns": 1,
    }
    assert await chain(engine, story) == [(1, 1), (2, 1), (3, 1), (4, 2)]


async def _unchecked(engine: AsyncEngine, story: str) -> bool:
    return bool(await scalar(engine, "SELECT chain_unchecked FROM stories WHERE id = :s", s=story))


async def test_chain_unchecked_marks_changes_the_head_check_cannot_see(
    engine: AsyncEngine,
) -> None:
    """FR-STORE-28 (#281): a turn's own commit leaves `chain_unchecked` clear; SQL outside an
    operation that moves an active turn sets it, a failed commit keeps it, and the next passing
    check clears it. Control actions clear what they set. A fork starts unchecked. A change
    outside the active turn set, or a write that changes nothing, sets nothing."""
    story = await new_story(engine)
    assert not await _unchecked(engine, story)
    for n in range(1, 4):
        await play_turn(engine, story, f"turn {n}")
    assert not await _unchecked(engine, story)

    # Not the chain: another column, an unchanged value, a non-turn row.
    await execute_in_tx(
        engine,
        (
            _sql("UPDATE operations SET turn_report = '{}' WHERE story_id = :s AND ACTIVE"),
            {"s": story},
        ),
        (
            _sql("UPDATE operations SET membership = 'active' WHERE story_id = :s AND ACTIVE"),
            {"s": story},
        ),
        (
            "UPDATE operations SET membership = 'dormant' WHERE story_id = :s AND kind <> 'turn'",
            {"s": story},
        ),
    )
    assert not await _unchecked(engine, story)
    await execute_in_tx(
        engine,
        (
            "UPDATE operations SET membership = 'active' WHERE story_id = :s AND kind <> 'turn'",
            {"s": story},
        ),
    )

    # A broken chain stays marked until it is repaired; the repair's next check clears it.
    await execute_in_tx(engine, (BREAKERS["gap"], {"s": story}))
    assert await _unchecked(engine, story)
    with pytest.raises(ChainBroken):
        await play_turn(engine, story, "refused")
    assert await _unchecked(engine, story)
    await execute_in_tx(
        engine,
        (
            "UPDATE operations SET membership = 'active' WHERE story_id = :s AND kind = 'turn' "
            "AND status = 'committed' AND narrative_turn = 2",
            {"s": story},
        ),
    )
    await play_turn(engine, story, "turn 4")
    assert not await _unchecked(engine, story)
    await assert_single_chain(engine, story)

    # Control actions run the full check and clear the mark they set.
    await truncate(engine, story, 2)
    assert not await _unchecked(engine, story)
    await play_turn(engine, story, "turn 3, again")
    await swap(engine, story)
    assert not await _unchecked(engine, story)
    await truncate(engine, story, 1)
    await untruncate(engine, story)
    await regenerate(engine, story)
    assert not await _unchecked(engine, story)
    await assert_single_chain(engine, story)

    forked = await fork(engine, story)
    assert await _unchecked(engine, forked.story_id)
    await play_turn(engine, forked.story_id, "on the fork")
    assert not await _unchecked(engine, forked.story_id)
    await assert_single_chain(engine, forked.story_id)


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


async def _long_story(engine: AsyncEngine, turns: int) -> str:
    """A story with ``turns`` active turns, written as rows (no events): only the chain check
    reads them. One real turn after it runs the full check that clears ``chain_unchecked``."""
    story = await new_story(engine)
    await execute_in_tx(
        engine,
        (
            "INSERT INTO operations (operation_id, story_id, kind, status, principal, "
            "narrative_turn, turn_variant, parent_variant, membership, membership_tx, "
            "committed_at) "
            "SELECT lpad(to_hex(n), 6, '0') || substr(:s, 7), :s, 'turn', 'committed', 'p', n, "
            "1, CASE WHEN n > 1 THEN 1 END, 'active', transaction_timestamp(), "
            "transaction_timestamp() FROM generate_series(1, :n) AS n",
            {"s": story, "n": turns},
        ),
        ("UPDATE stories SET next_turn_index = :n + 1 WHERE id = :s", {"s": story, "n": turns}),
    )
    assert await _unchecked(engine, story)
    await play_turn(engine, story, "the first real turn")
    assert not await _unchecked(engine, story)
    return story


def _rows_read(plan: dict[str, Any]) -> int:
    """Rows every node over ``operations`` produced or filtered out, across its loops."""
    total = 0
    if plan.get("Relation Name") == "operations":
        loops = int(plan.get("Actual Loops", 1))
        total += int(plan.get("Actual Rows", 0)) * loops
        total += int(plan.get("Rows Removed by Filter", 0)) * loops
        total += int(plan.get("Rows Removed by Index Recheck", 0)) * loops
    for child in plan.get("Plans", ()):
        total += _rows_read(child)
    return total


def _scans(plan: dict[str, Any]) -> set[tuple[str, str | None]]:
    found = set()
    if plan.get("Relation Name") == "operations":
        found.add((plan["Node Type"], plan.get("Index Name")))
    for child in plan.get("Plans", ()):
        found |= _scans(child)
    return found


async def _explain(engine: AsyncEngine, statement: str, parameters: Any) -> dict[str, Any]:
    async with engine.connect() as conn:
        raw = await conn.exec_driver_sql(EXPLAIN + statement, parameters)
        found = raw.scalar_one()
    plan = found if isinstance(found, list) else json.loads(found)
    return dict(plan[0]["Plan"])


async def _turn_commit_cost(
    engine: AsyncEngine, story: str, full_checks: list[str]
) -> tuple[int, dict[str, Any], int]:
    """(statements of one plain turn commit's transaction, the head check's plan, the full
    check's rows read on the same story)."""
    op = _turn(engine, story)
    await op.begin()
    op.append("player_input", {"text": "measured"}, provenance=Provenance.PLAYER)
    op.append("message_rendered", {"text": "measured reply"})
    with _statements(engine) as seen:
        await op.commit()
    probes = [s for s in seen if "chain_unchecked" in s[0] and s[0].lstrip().startswith("SELECT")]
    assert len(probes) == 1, [s[0] for s in seen]
    assert full_checks == [], "the full check ran"
    head = await _explain(engine, *probes[0])
    async with engine.connect() as conn:
        full_rows = int(
            await conn.scalar(
                text(_sql("SELECT count(*) FROM operations WHERE story_id = :s AND ACTIVE")),
                {"s": story},
            )
            or 0
        )
    return len(seen), head, full_rows


async def test_turn_commit_chain_check_cost_is_independent_of_story_length(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-PERF-3, FR-STORE-28 (#281): a plain turn commit on a 2,000-turn story issues the same
    statements as on a 5-turn one, and its chain check reads the same handful of index rows
    (`EXPLAIN ANALYZE` of the statement the commit ran, through `uq_operations_active_turn`).
    The full check it replaces reads every active turn: 2,002 against 6. Planner statistics
    are refreshed first, so the plan is the one a story of that size gets."""
    short, long = await _long_story(engine, 4), await _long_story(engine, 2_000)
    async with engine.connect() as conn:
        await conn.execute(text("ANALYZE operations"))
        await conn.commit()

    full_checks: list[str] = []

    async def counted(conn: AsyncConnection, story_id: str) -> dict[str, Any] | None:
        full_checks.append(story_id)
        return await chain_violation(conn, story_id)

    monkeypatch.setattr(operation_module, "chain_violation", counted)
    short_statements, short_plan, short_full = await _turn_commit_cost(engine, short, full_checks)
    long_statements, long_plan, long_full = await _turn_commit_cost(engine, long, full_checks)

    assert short_statements == long_statements
    assert _rows_read(short_plan) == _rows_read(long_plan) <= 4
    for plan in (short_plan, long_plan):
        scans = _scans(plan)
        assert scans, plan
        assert all(index == "uq_operations_active_turn" for _, index in scans), scans
    assert (short_full, long_full) == (6, 2_002)
    async with engine.connect() as conn:
        assert await chain_violation(conn, long) is None
