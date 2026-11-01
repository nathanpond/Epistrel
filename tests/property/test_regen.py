"""REGEN — the commit boundary is the regeneration boundary (FR-COMMIT-8, NFR-DETERM-2; docs/04
§6; #154).

Hypothesis draws a story of 5 to 20 turns, each turn doing some of: commit a fact (``noted``, a
fresh value per turn), introduce a thunk (a fresh item's ``knocked_for``, its object open), depend
on the oldest open thunk an earlier turn introduced (resolving it with the parcel). It rewinds to
a random message M (truncating what follows) and regenerates M with fresh actions, the way #66's
regenerate commits: a new ``turn`` operation at M whose commit retires M's active variant.

Asserted per example, against Postgres:

- facts committed before M (and the position-less ones) are byte-identical after the
  regeneration (the complement: nothing earlier moves);
- every fact M committed is ``revised`` and out of the timeline, and the new variant's commits
  replace them, so a coin result committed in M re-rolls on M's own regeneration;
- the thunks M resolved are open again (unless the new M resolves them) and the thunks M
  introduced left with it;
- swapping back to M's original variant restores its facts to ``committed`` and the thunks it
  resolved, with every pre-M row still identical (FR-STORE-29's symmetry);
- NFR-DETERM-2: regenerating M a second time from the restored state, with the same actions,
  reverts exactly the same facts and leaves the same outcome: the outcome set (which facts
  revert, which stay, keyed by position and value) is a function of the log alone.

The complement (the runs exercised each case: a revert, a reopen, a re-roll, a restore) holds by
construction (#514): ``CASES`` names one explicit example per case, each must exercise its own
case, and the random draws run on top of them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, fields
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline
from epistrel.core.canon import facts as canon
from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.db import make_engine
from epistrel.core.enums import FactCommitState, Membership, OpenSlot, RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.variants import ChainRef, SwapRequest
from tests import commit_support as c
from tests.auth_support import auth_settings
from tests.canon_support import PRINCIPAL, run

ITEMS = 24
EXAMPLES = 60
"""Fresh items per story: one thunk subject per turn that introduces one."""

Actions = tuple[bool, bool, bool]
"""A turn's actions: (commit a fact, introduce a thunk, depend on an open thunk)."""

actions = st.tuples(st.booleans(), st.booleans(), st.booleans())
Drawn = tuple[list[Actions], int, Actions]
"""A story's turns, the message M to regenerate, and the regeneration's actions."""
stories = st.tuples(
    st.lists(actions, min_size=5, max_size=20),
    st.integers(min_value=1, max_value=20),
    actions,
)


@dataclass
class Tally:
    examples: int = 0
    reverted: int = 0
    reopened: int = 0
    rerolled: int = 0
    restored: int = 0

    def add(self, other: Tally) -> None:
        self.examples += other.examples
        self.reverted += other.reverted
        self.reopened += other.reopened
        self.rerolled += other.rerolled
        self.restored += other.restored


QUIET: Actions = (False, False, False)
NOTE: Actions = (True, False, False)
CASES: dict[str, tuple[str, Drawn]] = {
    # M (turn 3) commits a fact; its regeneration commits none, so the fact reverts.
    "revert": ("reverted", ([NOTE] * 5, 3, QUIET)),
    # Turn 1 introduces a thunk and M (turn 2) resolves it; the regeneration does not.
    "reopen": ("reopened", ([(False, True, False), (False, False, True)] + [QUIET] * 3, 2, QUIET)),
    # M (turn 3) commits a fact and so does its regeneration: the coin re-rolls.
    "reroll": ("rerolled", ([NOTE] * 5, 3, NOTE)),
    # M is the last turn (no rewind) and commits a fact and a thunk; swapping back restores it.
    "restore": ("restored", ([QUIET] * 4 + [(True, True, False)], 20, QUIET)),
}
"""One explicit example per case the complement asserts, with the tally field it must raise."""


@dataclass
class Story:
    k: c.Knock
    noted: str
    items: list[str]
    used: list[str] = field(default_factory=list)


async def _story(engine: AsyncEngine) -> Story:
    k = await c.knock_story(engine)
    async with engine.begin() as conn:
        noted = (await registry.auto_register(conn, k.story_id, "noted", object_type="str")).id
        items = [
            await store.add_entity(conn, k.story_id, "item", f"item {n}") for n in range(ITEMS)
        ]
    return Story(k, noted, items)


async def _act(conn: AsyncConnection, op: Operation, s: Story, act: Actions, value: str) -> None:
    commit, introduce, depend = act
    story = s.k.story_id
    if commit:
        await canon.commit_fact(
            conn,
            story,
            Proposition(
                subject_id=s.k.cal,
                predicate_id=s.noted,
                literal=TypedLiteral(kind=LiteralKind.STR, value=value),
                valid_time=Interval.of(0, None),
            ),
            provenance=RecordProvenance.RENDER,
            operation=op,
            beat=2,
        )
    if depend:
        open_ = await conn.execute(
            text(
                "SELECT id FROM thunks WHERE story_id = :s AND membership = 'active' "
                "AND narrative_turn < :t ORDER BY id"
            ),
            {"s": story, "t": op.narrative_turn},
        )
        for (thunk_id,) in open_:
            found = await lifecycle.read_thunk(conn, story, thunk_id)
            if found is None or not found.open:
                continue
            claim = Proposition(
                subject_id=str(found.sketch.subject_id),
                predicate_id=s.k.knocked_for,
                object_id=s.k.parcel,
                valid_time=Interval.of(0, None),
            )
            await lifecycle.depend(conn, op, thunk_id, claim, beat=3, channel="do", on=True)
            break
    if introduce:
        item = s.items[len(s.used) % ITEMS]
        s.used.append(item)
        await lifecycle.introduce(
            conn,
            op,
            ThunkSketch(
                subject_text="the item",
                relation_text="knocked_for",
                object_text="something",
                predicate_id=s.k.knocked_for,
                subject_id=item,
                open_slot=OpenSlot.OBJECT,
            ),
            valid_time=0,
            beat=4,
            on=True,
        )


async def _turn(
    engine: AsyncEngine, s: Story, act: Actions, value: str, *, turn: int | None = None
) -> Operation:
    async def step(conn: AsyncConnection, op: Operation) -> None:
        await _act(conn, op, s, act, value)

    op, _ = await run(engine, s.k.story_id, step, turn=turn)
    return op


async def _facts(engine: AsyncEngine, story: str) -> dict[str, dict[str, Any]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT * FROM facts WHERE story_id = :s ORDER BY id"), {"s": story}
        )
        return {str(r.id): dict(r._mapping) for r in found}


async def _thunks(engine: AsyncEngine, story: str) -> dict[str, lifecycle.Thunk]:
    async with engine.connect() as conn:
        ids = list(
            await conn.scalars(
                text("SELECT id FROM thunks WHERE story_id = :s ORDER BY id"), {"s": story}
            )
        )
        found = {i: await lifecycle.read_thunk(conn, story, i) for i in ids}
    return {k: v for k, v in found.items() if v is not None}


def _turn_of(row: dict[str, Any]) -> tuple[int, int] | None:
    at = row["committed_at_position"]
    return None if at is None else (int(at["turn"]), int(at["variant"]))


def _outcome(
    rows: dict[str, dict[str, Any]], m: int, old: int, newest: int
) -> list[tuple[Any, ...]]:
    """A regeneration's outcome without ids: each fact before M, of M's original variant and of
    the newest variant, by position, predicate, subject and beat, with its commit state and
    membership (which facts reverted, which stayed). Other variants of M are left out."""
    found: list[tuple[Any, ...]] = []
    for row in rows.values():
        at = _turn_of(row)
        if at is not None and at[0] == m and at[1] not in (old, newest):
            continue
        tag = "before" if at is None or at[0] < m else ("old" if at[1] == old else "new")
        found.append(
            (
                tag,
                None if at is None else at[0],
                row["predicate_id"],
                row["subject_id"],
                row["beat"],
                row["thunk_id"] is not None,
                str(row["commit_state"]),
                str(row["membership"]),
            )
        )
    return sorted(found, key=repr)


async def _example(url: str, drawn: Drawn, tally: Tally) -> None:
    turns, pick, again = drawn
    engine = make_engine(auth_settings(url))
    try:
        s = await _story(engine)
        story = s.k.story_id
        for n, act in enumerate(turns, start=1):
            await _turn(engine, s, act, f"coin {n}")
        m = min(pick, len(turns))
        if m < len(turns):
            await timeline.truncate(
                engine, story, timeline.TruncateRequest(to_turn=m), principal=PRINCIPAL
            )
        before = await _facts(engine, story)
        thunks_before = await _thunks(engine, story)
        old_variant = next(
            int(r["turn_variant"])
            for r in await _ops(engine, story)
            if r["narrative_turn"] == m and r["membership"] == "active"
        )
        old_m = {k for k, r in before.items() if _turn_of(r) == (m, old_variant)}
        m_ops = _op_ids(await _ops(engine, story), m, old_variant)
        resolved_by_m = {
            t.id
            for t in thunks_before.values()
            if t.state.value == "resolved"
            and (t.narrative_turn or 0) < m
            and t.resolution_operation_id in m_ops
        }

        await _turn(engine, s, again, f"coin {m} again", turn=m)  # regenerate M
        after = await _facts(engine, story)
        thunks_after = await _thunks(engine, story)
        for fact_id, row in before.items():
            at = _turn_of(row)
            if at is None or at[0] < m:
                assert after[fact_id] == row, fact_id  # sticky to every later message
        for fact_id in old_m:
            assert str(after[fact_id]["commit_state"]) == FactCommitState.REVISED.value
            assert str(after[fact_id]["membership"]) == Membership.DORMANT.value
        tally.reverted += sum(
            1 for i in old_m if before[i]["commit_state"] == FactCommitState.COMMITTED.value
        )
        new_m = {k for k in after if k not in before}
        if again[0] and any(before[i]["predicate_id"] == s.noted for i in old_m):
            async with engine.connect() as conn:
                current = await canon.canon(conn, story, predicate=s.noted, subject=s.k.cal)
            values = {f.proposition.literal.value for f in current if f.proposition.literal}
            assert f"coin {m} again" in values  # re-rolled
            assert f"coin {m}" not in values
            tally.rerolled += 1
        for thunk_id in resolved_by_m:
            found = thunks_after[thunk_id]
            again_resolved = found.resolution_operation_id is not None and (
                found.resolved_fact_id in new_m
            )
            assert found.open or again_resolved, thunk_id
            tally.reopened += int(found.open)
        for thunk_id, thunk in thunks_before.items():
            if thunk.narrative_turn == m:
                async with engine.connect() as conn:
                    member = await conn.scalar(
                        text("SELECT membership FROM thunks WHERE id = :i"), {"i": thunk_id}
                    )
                assert str(member) == Membership.DORMANT.value
        new_variant = old_variant + 1
        first_outcome = _outcome(after, m, old_variant, new_variant)

        # Swap back: M's original variant and its commits return; nothing earlier moves.
        await timeline.swap(
            engine,
            story,
            SwapRequest(to_variant_chain=ChainRef(turn=m, variant=old_variant)),
            principal=PRINCIPAL,
        )
        back = await _facts(engine, story)
        for fact_id in old_m:
            assert back[fact_id]["commit_state"] == before[fact_id]["commit_state"]
            assert str(back[fact_id]["membership"]) == Membership.ACTIVE.value
            tally.restored += 1
        for fact_id in new_m:
            if _turn_of(after[fact_id]) == (m, new_variant):
                assert str(back[fact_id]["commit_state"]) == FactCommitState.REVISED.value
        for fact_id, row in before.items():
            at = _turn_of(row)
            if at is None or at[0] < m:
                assert back[fact_id] == row, fact_id
        restored = await _thunks(engine, story)
        for thunk_id in resolved_by_m:
            assert restored[thunk_id].state.value == "resolved"

        # NFR-DETERM-2: the same regeneration from the same state, the same outcome. (The swap
        # restored M's original chain with the tail the rewind had discarded: rewind again.)
        if m < len(turns):
            await timeline.truncate(
                engine, story, timeline.TruncateRequest(to_turn=m), principal=PRINCIPAL
            )
        await _turn(engine, s, again, f"coin {m} again", turn=m)
        second = await _facts(engine, story)
        for fact_id in old_m:
            assert str(second[fact_id]["commit_state"]) == FactCommitState.REVISED.value
        assert _outcome(second, m, old_variant, new_variant + 1) == first_outcome
        tally.examples += 1
    finally:
        await engine.dispose()


async def _ops(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT operation_id, narrative_turn, turn_variant, membership FROM operations "
                "WHERE story_id = :s AND kind = 'turn' AND status = 'committed'"
            ),
            {"s": story},
        )
        return [dict(r._mapping) for r in found]


def _op_ids(ops: list[dict[str, Any]], turn: int, variant: int) -> set[str]:
    return {
        str(o["operation_id"])
        for o in ops
        if o["narrative_turn"] == turn and o["turn_variant"] == variant
    }


@pytest.mark.integration
@pytest.mark.part("M4", "FR-COMMIT-8; NFR-DETERM-2")
def test_regen(pg_url: str) -> None:
    """See the module docstring."""
    tally = Tally()
    by_case = {name: Tally() for name in CASES}

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=stories)
    def boundary(drawn: Drawn) -> None:
        one = Tally()
        asyncio.run(_example(pg_url, drawn, one))
        tally.add(one)
        for name, (_, case) in CASES.items():
            if drawn == case:
                by_case[name].add(one)

    for _, case in CASES.values():
        boundary = example(drawn=case)(boundary)
    boundary()
    # Complement, by construction (#514): each case's own explicit example exercised it, whatever
    # the random draws did; the draws ran on top (explicit examples do not count toward
    # ``max_examples``).
    covered = {counter for counter, _ in CASES.values()}
    assert covered == {f.name for f in fields(Tally)} - {"examples"}  # a case per counter
    for name, (counter, _) in CASES.items():
        assert by_case[name].examples >= 1, name
        assert getattr(by_case[name], counter) > 0, (name, by_case[name])
    assert tally.examples == EXAMPLES + len(CASES)
    assert tally.reverted > 0
    assert tally.reopened > 0
    assert tally.rerolled > 0
    assert tally.restored > 0
