"""S-UNDO-SWAP, M2 part: undo a rewind after new turns were written (docs/04 §7; #66). One
turn per message: player-only turns written through ``Operation``. M4's consolidation clause
is ``test_s_undo_swap__consolidation`` (#143), its resolution clause
``test_s_undo_swap__resolutions`` (#154)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline, variants
from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.enums import OpenSlot, OperationKind, Provenance
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.memory.consolidation import Window
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.propositions.registry import auto_register
from epistrel.core.variants import ChainRef, SwapRequest
from epistrel.orchestrator.jobs.handlers.consolidation import consolidate_window
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import consolidation_support as cs
from tests.auth_support import auth_settings
from tests.canon_support import run as canon_run
from tests.concurrency_support import assert_single_chain, chain
from tests.event_support import new_story, rows
from tests.fork_support import fork, owned_rows
from tests.projection_support import snapshot, truncate, untruncate

MESSAGES = 1_000
CHECKPOINT = 800
PRIMES = 50
PRINCIPAL = "p-player"


async def _message(engine: AsyncEngine, story: str, text: str) -> None:
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("player_input", {"text": text}, provenance=Provenance.PLAYER)
        await op.commit()


async def _operations(engine: AsyncEngine, story: str) -> dict[str, dict[str, Any]]:
    """Every committed non-control operation of the story, without ``membership_tx`` (a flip
    re-stamps it; the swap's row diff is about membership)."""
    found = await rows(
        engine,
        "SELECT * FROM operations WHERE story_id = :s AND status = 'committed' "
        "AND kind <> 'control'",
        s=story,
    )
    return {
        r.operation_id: {k: v for k, v in r._mapping.items() if k != "membership_tx"} for r in found
    }


async def _memberships(engine: AsyncEngine, story: str, variant: int) -> set[str]:
    found = await rows(
        engine,
        "SELECT DISTINCT membership FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND turn_variant = :v AND narrative_turn > :c",
        s=story,
        v=variant,
        c=CHECKPOINT,
    )
    return {r.membership for r in found}


async def _said(engine: AsyncEngine, story: str) -> list[str]:
    """The story's player lines along its active chain, in turn order."""
    found = await rows(
        engine,
        "SELECT text FROM transcript_messages WHERE story_id = :s AND membership = 'active' "
        "ORDER BY narrative_turn, seq",
        s=story,
    )
    return [r.text for r in found]


async def _swap(engine: AsyncEngine, story: str, turn: int, variant: int) -> timeline.Swapped:
    return await timeline.swap(
        engine,
        story,
        SwapRequest(to_variant_chain=ChainRef(turn=turn, variant=variant)),
        principal=PRINCIPAL,
    )


@pytest.mark.part("M2", "FR-STORE-28")
async def test_s_undo_swap(engine: AsyncEngine) -> None:
    """Play to 1,000, truncate to 800, write 801'-850'; undoing the truncation defaults to a
    swap: 801-1,000 active, 801'-850' dormant, one active variant per position in one chain.
    Swapping back restores the exact prior state (row diff of operations and projections),
    and either chain can be forked into a complete story independent of the source and of
    each other (#66 AC3). Complement: an undo with no new turns written
    is a plain reactivation, not a swap."""
    story = await new_story(engine)
    for n in range(1, MESSAGES + 1):
        await _message(engine, story, f"message {n}")
    await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=CHECKPOINT), principal=PRINCIPAL
    )
    for n in range(CHECKPOINT + 1, CHECKPOINT + PRIMES + 1):
        await _message(engine, story, f"message {n}'")
    await assert_single_chain(engine, story)
    assert await _memberships(engine, story, 1) == {"dormant"}  # shelved under 801'
    primes_ops = await _operations(engine, story)
    primes_projections = await snapshot(engine, story)

    undone = await timeline.untruncate(engine, story, principal=PRINCIPAL)
    assert undone.swapped
    assert undone.active_chain == ChainRef(turn=CHECKPOINT + 1, variant=1)
    assert (undone.restored_operations, undone.dormant_operations) == (MESSAGES - CHECKPOINT, 50)
    assert undone.next_turn_index == MESSAGES + 1
    await assert_single_chain(engine, story)
    assert await chain(engine, story) == [(n, 1) for n in range(1, MESSAGES + 1)]
    assert await _memberships(engine, story, 1) == {"active"}
    assert await _memberships(engine, story, 2) == {"dormant"}
    state = await timeline.undo_state(engine, story)
    assert state.undo_available  # a swap is undone by another swap
    assert [(c.from_turn, c.variant, c.through_turn) for c in state.dormant_chains] == [
        (CHECKPOINT + 1, 2, CHECKPOINT + PRIMES)
    ]

    back = await _swap(engine, story, CHECKPOINT + 1, 2)
    assert back.active_chain == ChainRef(turn=CHECKPOINT + 1, variant=2)
    assert back.next_turn_index == CHECKPOINT + PRIMES + 1
    await assert_single_chain(engine, story)
    assert await _operations(engine, story) == primes_ops
    assert await snapshot(engine, story) == primes_projections
    again = await _swap(engine, story, CHECKPOINT + 1, 2)  # already active: nothing written
    assert again.operation_id is None
    assert again.timeline_revision == back.timeline_revision

    # Promotion by fork (#67 copies): either chain is selectable, complete through its tip.
    original, through = await variants.fork_selection(
        engine, story, ChainRef(turn=CHECKPOINT + 1, variant=1), None
    )
    assert original is not None
    assert through == MESSAGES
    assert (original.branch, len(original.operation_ids)) == (CHECKPOINT, MESSAGES - CHECKPOINT)
    assert not set(original.operation_ids) & {
        op for op, row in primes_ops.items() if row["membership"] == "active"
    }
    active, through = await variants.fork_selection(engine, story, None, None)
    assert (active, through) == (None, CHECKPOINT + PRIMES)
    for wrong in (CHECKPOINT, MESSAGES + 1):
        with pytest.raises(InvalidRequest):
            await variants.fork_selection(
                engine, story, ChainRef(turn=CHECKPOINT + 1, variant=1), wrong
            )

    # ...and both forks are complete and independent (#66 AC3, through #67's fork).
    source_before = await owned_rows(engine, story)
    promoted = await fork(engine, story, variant_chain=ChainRef(turn=CHECKPOINT + 1, variant=1))
    current = await fork(engine, story)
    partial = await fork(
        engine, story, CHECKPOINT + 20, variant_chain=ChainRef(turn=CHECKPOINT + 1, variant=1)
    )
    assert promoted.forked_from.variant_chain == ChainRef(turn=CHECKPOINT + 1, variant=1)
    assert (promoted.next_turn_index, current.next_turn_index, partial.next_turn_index) == (
        MESSAGES + 1,
        CHECKPOINT + PRIMES + 1,
        CHECKPOINT + 21,
    )
    originals = [f"message {n}" for n in range(1, MESSAGES + 1)]
    primes = [f"message {n}'" for n in range(CHECKPOINT + 1, CHECKPOINT + PRIMES + 1)]
    assert await _said(engine, promoted.story_id) == originals
    assert await _said(engine, current.story_id) == originals[:CHECKPOINT] + primes
    assert await _said(engine, partial.story_id) == originals[: CHECKPOINT + 20]
    assert await chain(engine, promoted.story_id) == [(n, 1) for n in range(1, MESSAGES + 1)]
    assert await chain(engine, current.story_id) == await chain(engine, story)
    for forked in (promoted, current, partial):
        await assert_single_chain(engine, forked.story_id)
    after = await owned_rows(engine, story)  # the source gained only its fork records
    added = {table: after[table] - source_before[table] for table in after}
    assert {t for t, new in added.items() if new} == {"events", "operations"}
    assert len(added["events"]) == 3
    assert all(",fork," in row for row in added["events"])
    assert {t: source_before[t] - after[t] for t in after} == {t: set() for t in after}
    others = {f.story_id: await owned_rows(engine, f.story_id) for f in (current, partial)}
    others[story] = after
    await _message(engine, promoted.story_id, "message 1001, only in the promoted fork")
    assert (await chain(engine, promoted.story_id))[-1] == (MESSAGES + 1, 1)
    for other, before in others.items():
        assert await owned_rows(engine, other) == before

    # Complement: no new turns over the tail, so undoing the truncation reactivates it.
    await timeline.truncate(
        engine, story, timeline.TruncateRequest(to_turn=CHECKPOINT + 10), principal=PRINCIPAL
    )
    plain = await timeline.untruncate(engine, story, principal=PRINCIPAL)
    assert (plain.swapped, plain.active_chain, plain.restored_operations) == (
        False,
        None,
        PRIMES - 10,
    )
    await assert_single_chain(engine, story)
    assert await _operations(engine, story) == primes_ops


async def _consolidate(
    engine: AsyncEngine, models: ModelAccessLayer, story: str, first: int, last: int, line: str
) -> None:
    models.stub.script("consolidation", [cs.answer(facts=[(line, "observation", [0])])])
    done = await consolidate_window(engine, story, Window(first, last), models=models)
    assert str(done.outcome) == "committed"


async def _active(engine: AsyncEngine, story: str) -> set[str]:
    return {r.text for r in await cs.outputs(engine, story, membership="active")}


@pytest.mark.part("M4", "FR-STORE-29")
async def test_s_undo_swap__consolidation(
    engine: AsyncEngine, pg_url: str, drained_embedding_jobs: None
) -> None:
    """S-UNDO-SWAP's M4 consolidation clause (#143; docs/04 §7, shared with #130/#133/#154).
    Twenty turns, windows 1-5, 6-10 and 11-15 consolidated; truncate to 10, write 11'-15' and
    consolidate them; undo the truncation (a swap): the original chain's 11-15 output is active
    again, the 11'-15' output dormant with its chain, and consolidation reruns for the restored
    chain: its uncovered 16-20 is consolidated. Then a plain reactivation: with 11-15
    consolidated, truncating to 12 enqueues 11-12, which commits; undoing the truncation
    restores the original 11-15 output, which wins over the rerun (superseded, FR-MEM-10)."""
    models = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    models.bind(engine)
    quiet = cs.dials(window_memories=1_000, cadence_turns=1_000)
    w = await cs.story(engine, quiet)
    story, bobby = w.story_id, w.characters["Bobby"]
    await cs.bulk_turns(engine, story, 20, actor=bobby, name="Bobby", does=True)
    for first, line in ((1, "Early days."), (6, "Middle days."), (11, "Late days.")):
        await _consolidate(engine, models, story, first, first + 4, line)
    await truncate(engine, story, 10)
    assert await _active(engine, story) == {"Early days.", "Middle days."}
    await cs.bulk_turns(engine, story, 5, actor=bobby, name="Bobby", does=True)
    await _consolidate(engine, models, story, 11, 15, "Other days.")
    assert await _active(engine, story) == {"Early days.", "Middle days.", "Other days."}
    await untruncate(engine, story)  # a swap: new turns were written over the tail
    assert await _active(engine, story) == {"Early days.", "Middle days.", "Late days."}
    assert [
        r.membership_text for r in await cs.outputs(engine, story) if r.text == "Other days."
    ] == ["dormant"]
    (job,) = await cs.jobs(engine, story, "queued")
    assert (job.payload["from_turn"], job.payload["to_turn"]) == (16, 20)
    models.stub.script("consolidation", [cs.answer(facts=[("Last days.", "observation", [0])])])
    assert await cs.run_jobs(engine, models, story) == ["committed"]
    assert "Last days." in await _active(engine, story)

    v = await cs.story(engine, quiet)
    other = v.story_id
    await cs.bulk_turns(engine, other, 15, actor=v.characters["Ann"], name="Ann", does=True)
    await _consolidate(engine, models, other, 1, 10, "First ten.")
    await _consolidate(engine, models, other, 11, 15, "Original five.")
    await truncate(engine, other, 12)
    (rerun,) = await cs.jobs(engine, other, "queued")
    assert (rerun.payload["from_turn"], rerun.payload["to_turn"]) == (11, 12)
    models.stub.script("consolidation", [cs.answer(facts=[("Rerun two.", "observation", [0])])])
    assert await cs.run_jobs(engine, models, other) == ["committed"]
    assert await _active(engine, other) == {"First ten.", "Rerun two."}
    await untruncate(engine, other)  # nothing written over the tail: a plain reactivation
    assert await _active(engine, other) == {"First ten.", "Original five."}
    superseded = await cs.outputs(engine, other, membership="superseded")
    assert [r.text for r in superseded] == ["Rerun two."]


async def _resolution_rows(engine: AsyncEngine, story: str) -> dict[str, dict[str, Any]]:
    """The story's fact rows without ``membership_tx`` (a flip re-stamps it)."""
    found = await rows(engine, "SELECT * FROM facts WHERE story_id = :s ORDER BY id", s=story)
    return {r.id: {k: v for k, v in r._mapping.items() if k != "membership_tx"} for r in found}


async def _thunk_states(engine: AsyncEngine, story: str, *ids: str) -> dict[str, tuple[Any, ...]]:
    found: dict[str, tuple[Any, ...]] = {}
    async with engine.connect() as conn:
        for thunk_id in ids:
            t = await lifecycle.read_thunk(conn, story, thunk_id)
            assert t is not None
            found[thunk_id] = (t.state.value, t.resolved_fact_id, t.resolution_operation_id)
    return found


@pytest.mark.part("M4", "FR-STORE-29")
async def test_s_undo_swap__resolutions(engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    """S-UNDO-SWAP's M4 resolution clause (#154; docs/04 §7). Turn 1 narrates two deferred
    thunks (who knocked, who rang); the original chain's turn 3 resolves "who rang" (Bobby).
    Truncate to 2 (the ring is open again), write 3'-4', where 3' resolves the knock (Ann).
    Undoing the truncation is a swap: the knock, resolved only in the newer chain, reverts to
    unresolved (its fact ``revised`` and dormant), the original chain's own resolution of the
    ring returns (its fact ``committed`` and active again), and consolidation reruns for the
    restored chain (a queued job). Swapping back restores the exact prior state: every fact row
    (but its membership stamp) and both thunks' effective states."""
    quiet = cs.dials(window_memories=1_000, cadence_turns=1_000)
    w = await cs.story(engine, quiet)
    story, bobby, ann = w.story_id, w.people["Bobby"], w.people["Ann"]
    async with engine.begin() as conn:
        door = await store.add_entity(conn, story, "item", "the door")
        knocked = (await auto_register(conn, story, "knocked_at")).id
        rang = (await auto_register(conn, story, "rang_at")).id

    def sketch(predicate: str, relation: str) -> ThunkSketch:
        return ThunkSketch(
            subject_text="someone",
            relation_text=relation,
            object_text="the door",
            predicate_id=predicate,
            object_id=door,
            open_slot=OpenSlot.SUBJECT,
        )

    async def narrate(conn: Any, op: Operation) -> tuple[str, str]:
        a = await lifecycle.introduce(conn, op, sketch(knocked, "knocked_at"), valid_time=10)
        b = await lifecycle.introduce(conn, op, sketch(rang, "rang_at"), valid_time=10)
        assert a is not None
        assert b is not None
        return a.id, b.id

    knock, ring = await cs_turn(engine, story, narrate)

    def resolve(thunk_id: str, predicate: str, who: str) -> Any:
        async def step(conn: Any, op: Operation) -> lifecycle.Dependence:
            claim = Proposition(
                subject_id=who,
                predicate_id=predicate,
                object_id=door,
                valid_time=Instant.of(10, Granularity.MINUTE),
            )
            return await lifecycle.depend(conn, op, thunk_id, claim, beat=2, channel="do")

        return step

    await cs.bulk_turns(engine, story, 1, actor=w.characters["Bobby"], name="Bobby", does=True)
    original = await cs_turn(engine, story, resolve(ring, rang, bobby))  # turn 3
    assert original.kind == "committed"
    await cs.bulk_turns(engine, story, 2, actor=w.characters["Bobby"], name="Bobby", does=True)
    await truncate(engine, story, 2)
    assert (await _thunk_states(engine, story, ring))[ring][0] == "open"
    newer = await cs_turn(engine, story, resolve(knock, knocked, ann))  # turn 3'
    assert newer.kind == "committed"
    await cs.bulk_turns(engine, story, 1, actor=w.characters["Ann"], name="Ann", does=True)
    prior_rows = await _resolution_rows(engine, story)
    prior_thunks = await _thunk_states(engine, story, knock, ring)
    assert (prior_thunks[knock][0], prior_thunks[ring][0]) == ("resolved", "open")

    queued = {j.id for j in await cs.jobs(engine, story, "queued")}
    await untruncate(engine, story)  # new turns were written over the tail: a swap
    states = await _thunk_states(engine, story, knock, ring)
    assert states[knock][0] == "open"
    assert states[ring] == ("resolved", original.fact.id, original.fact.operation_id)
    after = await _resolution_rows(engine, story)
    assert (str(after[newer.fact.id]["commit_state"]), after[newer.fact.id]["membership"]) == (
        "revised",
        "dormant",
    )
    assert (
        str(after[original.fact.id]["commit_state"]),
        after[original.fact.id]["membership"],
    ) == (
        "committed",
        "active",
    )
    rerun = [j for j in await cs.jobs(engine, story, "queued") if j.id not in queued]
    assert rerun  # consolidation reruns for the restored chain (#143)

    await _swap(engine, story, 3, 2)  # back to the newer chain
    assert await _resolution_rows(engine, story) == prior_rows
    assert await _thunk_states(engine, story, knock, ring) == prior_thunks


async def cs_turn(engine: AsyncEngine, story: str, step: Any) -> Any:
    """Run ``step`` in a new turn's commit (#146's commit step)."""
    _, found = await canon_run(engine, story, step)
    return found
