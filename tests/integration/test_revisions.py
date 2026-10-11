"""Timeline revisions and the FR-CONC-4 commit decision (#47), driven through ``Operation``."""

from __future__ import annotations

from functools import partial

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import (
    LiteReadSet,
    RevalidationRequired,
    decide,
    run_with_revalidation,
)
from epistrel.core.enums import OperationKind
from epistrel.core.errors import ChainBroken, InvalidRequest, RevisionConflict, TimelineChanged
from epistrel.core.events import Operation
from epistrel.core.events.operation import CommitStep
from epistrel.core.projections import rebuild
from epistrel.core.views import corrected_history
from tests.concurrency_support import (
    HeldModel,
    assert_single_chain,
    chain,
    change_setting,
    event_count,
    operation_row,
    regenerate,
    reject_render,
    revision,
    set_instructions,
    set_pins,
    start_turn,
    summarise,
    truth_outside_read_set,
)
from tests.event_support import execute_in_tx, new_story, play_turn, rows, scalar
from tests.projection_support import snapshot, swap, truncate, untruncate


async def test_revision_moves_once_per_truth_change(engine: AsyncEngine) -> None:
    """Each truth-changing commit bumps `timeline_revision` by exactly one, inside its own
    transaction. Complement: settings, archive-like control, derived, maintenance and audit
    work leave it unchanged."""
    story = await new_story(engine)
    rev = await revision(engine, story)

    async def bumped(work: object) -> None:
        nonlocal rev
        await work  # type: ignore[misc]
        assert await revision(engine, story) == rev + 1
        rev += 1

    async def unchanged(work: object) -> None:
        await work  # type: ignore[misc]
        assert await revision(engine, story) == rev

    await bumped(play_turn(engine, story, "one"))
    await bumped(play_turn(engine, story, "two"))
    await bumped(play_turn(engine, story, "three"))
    await bumped(set_pins(engine, story, "the key is under the mat"))
    await bumped(set_instructions(engine, story, "keep it short"))
    await bumped(truncate(engine, story, 2))
    await bumped(untruncate(engine, story))
    await bumped(truncate(engine, story, 2))
    await bumped(play_turn(engine, story, "three, differently"))
    await bumped(swap(engine, story))
    await bumped(regenerate(engine, story))

    await unchanged(change_setting(engine, story))
    await unchanged(summarise(engine, story, 2))
    await unchanged(reject_render(engine, story, 2))
    await unchanged(rebuild(engine, story, principal="p-operator"))
    async with Operation(
        engine, story_id=story, kind=OperationKind.CONTROL, principal="p", bumps_revision=False
    ) as op:
        op.append("config_changed", {"setting": "archived", "new": True})
        await op.commit()
    await unchanged(change_setting(engine, story))


async def test_derived_work_cannot_declare_a_bump(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    for kind in (OperationKind.CONSOLIDATION, OperationKind.MAINTENANCE):
        with pytest.raises(InvalidRequest, match="never changes story truth"):
            Operation(engine, story_id=story, kind=kind, principal="p", bumps_revision=True)
    with pytest.raises(InvalidRequest, match="bumps the revision"):
        Operation(
            engine,
            story_id=story,
            kind=OperationKind.CONTROL,
            principal="p",
            bumps_revision=False,
            control_action=True,
        )
    with pytest.raises(InvalidRequest, match="timeline revision"):
        Operation(
            engine, story_id=None, kind=OperationKind.CONTROL, principal="p", bumps_revision=True
        )
    async with Operation(engine, story_id=None, kind=OperationKind.CONTROL, principal="p") as op:
        with pytest.raises(InvalidRequest, match="revalidates"):
            await op.commit(decide=lambda state: pytest.fail("not reached"))


async def test_base_and_read_set_are_captured(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    held = await start_turn(engine, story, "two")
    status, base, read_set, attempts, _, _ = await operation_row(engine, held.op.operation_id)
    assert (status, base, attempts) == ("in_flight", 2, [])
    assert set(read_set) == {
        "window",
        "segments",
        "pins",
        "instructions",
        "definitions",
        "guidance_text",
        "generation_settings",
    }
    assert all(len(digest) == 64 for digest in read_set.values())
    await held.release()
    result = await held.task
    assert result.attempts == [
        {
            "attempt": 1,
            "base_revision": 2,
            "current_revision": 2,
            "outcome": "commit",
            "committed_revision": 3,
        }
    ]
    assert (await operation_row(engine, held.op.operation_id))[3] == result.attempts


async def test_unchanged_revision_commits(engine: AsyncEngine) -> None:
    """Background work while the turn generates does not move the revision: plain commit."""
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    held = await start_turn(engine, story, "two")
    await summarise(engine, story, 1)
    await change_setting(engine, story)
    await held.release()
    result = await held.task
    assert [a["outcome"] for a in result.attempts] == ["commit"]
    assert held.model.calls == 1


async def test_rebase_when_the_read_set_is_untouched(engine: AsyncEngine) -> None:
    """The revision moved but no prompt input changed: rebase, no second generation."""
    story = await new_story(engine)
    held = await start_turn(engine, story, "one")
    await truth_outside_read_set(engine, story)
    await held.release()
    result = await held.task
    assert [a["outcome"] for a in result.attempts] == ["rebase"]
    assert result.attempts[0]["committed_revision"] == 3  # base 1; the outside change made 2
    assert held.model.calls == 1
    assert await chain(engine, story) == [(1, 1)]


async def test_rebase_runs_the_recheck(engine: AsyncEngine) -> None:
    """A failing deterministic re-check at rebase regenerates instead."""
    story = await new_story(engine)
    verdicts = iter([False, True])
    model = HeldModel("one")
    op = Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p")

    async def no_inputs(conn: AsyncConnection) -> None:
        return None

    async def generate(op: Operation, inputs: object) -> None:
        if model.calls == 0:
            await truth_outside_read_set(engine, story)
        model.calls += 1
        op.append("player_input", {"text": "one"})

    result = await run_with_revalidation(
        op,
        begin=no_inputs,
        generate=generate,
        decide=partial(decide, recheck=lambda: next(verdicts)),
        read_set=LiteReadSet(),
    )
    assert [(a["outcome"], a.get("reason")) for a in result.attempts] == [
        ("regenerate", "rebase_failed"),
        ("commit", None),
    ]


async def test_touched_read_set_regenerates_then_conflicts(engine: AsyncEngine) -> None:
    """A pins edit touches the read set: the same operation regenerates against a new base,
    up to the budget, then 409 `revision_conflict`; nothing of it is committed."""
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    held = await start_turn(engine, story, "two", budget=1)
    first_id = held.op.operation_id
    await set_pins(engine, story, "a")
    await held.release()
    assert not held.done
    assert held.model.calls == 2
    assert held.op.operation_id == first_id
    status, base, _, attempts, _, _ = await operation_row(engine, first_id)
    assert (status, base) == ("in_flight", 3)
    assert attempts == [
        {
            "attempt": 1,
            "base_revision": 2,
            "current_revision": 3,
            "outcome": "regenerate",
            "reason": "read_set_touched",
            "touched": ["pins"],
        }
    ]

    await set_instructions(engine, story, "b")
    await held.release()
    with pytest.raises(RevisionConflict) as caught:
        await held.task
    assert caught.value.details == {
        "current_revision": 4,
        "attempts": 2,
        "reason": "read_set_touched",
    }
    assert caught.value.http_status == 409
    status, _, _, attempts, turn, _ = await operation_row(engine, first_id)
    assert (status, turn) == ("abandoned", None)
    assert [a["touched"] for a in attempts] == [["pins"], ["instructions"]]
    assert await event_count(engine, first_id) == 0
    assert await chain(engine, story) == [(1, 1)]


async def test_regenerated_turn_commits_on_its_new_base(engine: AsyncEngine) -> None:
    """Two players: the second one's turn regenerates over the first one's reply."""
    story = await new_story(engine)
    first = await start_turn(engine, story, "player one")
    second = await start_turn(engine, story, "player two")
    await first.release()
    assert [a["outcome"] for a in (await first.task).attempts] == ["commit"]
    await second.release()
    assert second.model.calls == 2
    await second.release()
    result = await second.task
    assert [(a["outcome"], a["base_revision"]) for a in result.attempts] == [
        ("regenerate", 1),
        ("commit", 2),
    ]
    assert result.attempts[0]["touched"] == ["window"]
    assert await chain(engine, story) == [(1, 1), (2, 1)]
    assert await revision(engine, story) == 3


async def test_timeline_changed_on_any_control_action(engine: AsyncEngine) -> None:
    """Truncate, untruncate, swap and regenerate each abandon a turn begun before them."""
    story = await new_story(engine)
    for n in range(1, 4):
        await play_turn(engine, story, f"turn {n}")

    async def held_through(action: object) -> None:
        held = await start_turn(engine, story, "late")
        control = await action  # type: ignore[misc]
        await held.release()
        with pytest.raises(TimelineChanged) as caught:
            await held.task
        assert caught.value.details == {
            "current_revision": await revision(engine, story),
            "control_operation_id": control.operation_id,
        }
        assert (await operation_row(engine, held.op.operation_id))[0] == "abandoned"
        assert await event_count(engine, held.op.operation_id) == 0
        assert held.model.calls == 1
        await assert_single_chain(engine, story)

    await held_through(truncate(engine, story, 1))
    await held_through(untruncate(engine, story))
    await held_through(truncate(engine, story, 1))
    await play_turn(engine, story, "turn 2, new")
    await held_through(swap(engine, story))
    await held_through(regenerate(engine, story))


async def test_regenerate_retires_the_old_variant(engine: AsyncEngine) -> None:
    """A regenerated head turn makes the old variant dormant in the same commit; the
    transcript follows, and a rebuild agrees with the incremental projection."""
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    await play_turn(engine, story, "two")
    again = await regenerate(engine, story)
    assert (again.narrative_turn, again.turn_variant) == (2, 2)
    assert await chain(engine, story) == [(1, 1), (2, 2)]
    (old,) = await rows(
        engine,
        "SELECT membership FROM operations WHERE story_id = :s AND narrative_turn = 2 "
        "AND turn_variant = 1",
        s=story,
    )
    assert old.membership == "dormant"
    history = [e.payload for e in await corrected_history(engine, story)]
    assert {"text": "two"} not in [p.model_dump() for p in history]
    transcript = await rows(
        engine,
        "SELECT turn_variant, membership FROM transcript_messages WHERE story_id = :s "
        "AND narrative_turn = 2 ORDER BY seq",
        s=story,
    )
    assert [tuple(r) for r in transcript] == [
        (1, "dormant"),
        (1, "dormant"),
        (2, "active"),
        (2, "active"),
    ]
    incremental = await snapshot(engine, story)
    await rebuild(engine, story)
    assert await snapshot(engine, story) == incremental


async def test_one_active_variant_per_turn_is_enforced(engine: AsyncEngine) -> None:
    """The partial unique index refuses a second active committed variant at a turn."""
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    await regenerate(engine, story)
    with pytest.raises(IntegrityError, match="uq_operations_active_turn"):
        await execute_in_tx(
            engine,
            (
                "UPDATE operations SET membership = 'active' WHERE story_id = :s "
                "AND kind = 'turn' AND turn_variant = 1",
                {"s": story},
            ),
        )
    # Complement: dormant and discarded variants at the same turn are not constrained.
    await truncate(engine, story, 0)
    assert await chain(engine, story) == []
    count = await scalar(
        engine,
        "SELECT count(*) FROM operations WHERE story_id = :s AND narrative_turn = 1",
        s=story,
    )
    assert count == 2


async def test_commit_refuses_to_break_the_chain(engine: AsyncEngine) -> None:
    """A commit that would leave a gap in the active chain raises `ChainBroken` and writes
    nothing. Complement: the same step with the index moved accordingly commits."""
    story = await new_story(engine)
    turns = [await play_turn(engine, story, f"turn {n}") for n in range(1, 4)]
    before = await revision(engine, story)

    def discard_middle(next_index: int | None):  # type: ignore[no-untyped-def]
        async def step(conn: AsyncConnection) -> None:
            await conn.execute(
                text("UPDATE operations SET membership = 'discarded' WHERE operation_id = :o"),
                {"o": turns[1 if next_index is None else 2].operation_id},
            )
            if next_index is not None:
                await conn.execute(
                    text("UPDATE stories SET next_turn_index = :n WHERE id = :s"),
                    {"n": next_index, "s": story},
                )

        return step

    op = Operation(
        engine,
        story_id=story,
        kind=OperationKind.CONTROL,
        principal="p",
        bumps_revision=True,
        control_action=True,
    )
    with pytest.raises(ChainBroken):
        await _commit_with_step(op, discard_middle(None))
    assert (await operation_row(engine, op.operation_id))[0] == "failed"
    assert await revision(engine, story) == before
    assert await chain(engine, story) == [(1, 1), (2, 1), (3, 1)]

    async with Operation(
        engine,
        story_id=story,
        kind=OperationKind.CONTROL,
        principal="p",
        bumps_revision=True,
        control_action=True,
    ) as ok:
        ok.add_commit_step(discard_middle(3))
        await ok.commit()
    assert await chain(engine, story) == [(1, 1), (2, 1)]


async def test_unhandled_signal_leaves_the_operation_abandoned(engine: AsyncEngine) -> None:
    """Calling `commit(decide=...)` directly: a regenerate signal escaping the block marks
    the operation abandoned, not failed; a generation error marks it failed."""
    story = await new_story(engine)
    op = Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p")
    op.read_set_source = LiteReadSet()
    with pytest.raises(RevalidationRequired):
        await _commit_after_a_pins_edit(engine, story, op)
    status, _, _, attempts, _, _ = await operation_row(engine, op.operation_id)
    assert status == "abandoned"
    assert [a["outcome"] for a in attempts] == ["regenerate"]
    assert await event_count(engine, op.operation_id) == 0

    class Boom(Exception):
        pass

    async def explode(op: Operation, inputs: object) -> None:
        raise Boom

    failing = Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p")
    with pytest.raises(Boom):
        await run_with_revalidation(failing, begin=lambda conn: _none(), generate=explode)
    assert (await operation_row(engine, failing.operation_id))[0] == "failed"


async def _none() -> None:
    return None


async def _commit_with_step(op: Operation, step: CommitStep) -> None:
    async with op:
        op.add_commit_step(step)
        await op.commit()


async def _commit_after_a_pins_edit(engine: AsyncEngine, story: str, op: Operation) -> None:
    """Let the regenerate signal escape the ``async with`` block."""
    async with op:
        op.append("player_input", {"text": "x"})
        await set_pins(engine, story, "changed")
        await op.commit(decide=decide)
