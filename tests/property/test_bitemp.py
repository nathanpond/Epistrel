"""BITEMP: monotonic validity (FR-BITEMP-1..5; docs/04 §6), against PostgreSQL (#133).

Random sequences of commits (trimming or closing what they overlap), negations, supersedes,
retcons (a wider span under the author's authority), two writes in one operation, truncations,
untruncations, regenerations and plain turns, over one functional predicate (``located_at``:
one place per person at a time). After every step:

- **nothing is deleted or rewritten**: every version keeps its proposition, operation, valid-time,
  ``tx_from`` and narrative position; ``tx_to`` and ``superseded_by`` are set at most once, and
  ``tx_to`` is the record-time of the first row that closed the version;
- **one current version per fact**: in the corrected view, no lineage has two current versions;
- **no overlapping valid intervals** for one functional key (a person's affirmed places), nor
  for one proposition (either polarity);
- the **two questions agree with the versions**: ``canon_at(T)`` is exactly the current
  versions covering T.

At the end the **history is reconstructed by replaying versions**: ``canon_as_recorded`` at each
step's record-time returns exactly what was current then. A row's membership history beyond its
last change lives in the control events, so a checkpoint is compared where the rows' current
membership determines their membership at that time (``membership.active_as_of``), which holds
for most; the test asserts that it does for the first checkpoint and for most of the rest.

Complement: the sequences do supersede (closures happen), and truncations do discard versions
that later come back.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.db import make_engine
from epistrel.core.enums import RecordProvenance
from epistrel.core.events import Operation
from tests.auth_support import auth_settings
from tests.canon_support import LOCATED_AT, World, at, commit, run, supersede, world
from tests.event_support import play_turn, scalar
from tests.projection_support import truncate, untruncate

PEOPLE = ("Bobby", "Ann")
PLACES = ("kitchen", "garden", "school")
TIMES = range(-15, 135, 7)
"""Story-times ``canon_at`` is checked at after every step."""

IMMUTABLE = (
    "proposition_id",
    "lineage_id",
    "operation_id",
    "subject_id",
    "predicate_id",
    "valid_from",
    "valid_to",
    "valid_from_inner",
    "valid_to_inner",
    "tx_from",
    "narrative_turn",
    "turn_variant",
    "provenance",
    "authority",
    "visibility",
    "supersedes",
)

person = st.integers(0, len(PEOPLE) - 1)
place = st.integers(0, len(PLACES) - 1)
start = st.integers(0, 11)
length = st.integers(1, 6)
steps = st.lists(
    st.one_of(
        st.tuples(st.just("commit"), person, place, start, length, st.booleans()),
        st.tuples(st.just("open"), person, place, start),
        st.tuples(st.just("negate"), person, place, start, length),
        st.tuples(st.just("supersede"), st.integers(0, 50), place, st.booleans()),
        st.tuples(st.just("retcon"), st.integers(0, 50), place),
        st.tuples(st.just("double"), person, place, place, start),
        st.tuples(st.just("truncate"), st.integers(0, 2)),
        st.tuples(st.just("untruncate")),
        st.tuples(st.just("regenerate"), person, place, start),
        st.tuples(st.just("turn")),
    ),
    min_size=5,
    max_size=30,
)


@dataclass
class Checkpoint:
    record_time: datetime
    current: set[str]
    active: set[str]


@dataclass
class Tally:
    closures: int = 0
    discarded_then_back: int = 0
    checked: int = 0
    skipped: int = 0
    seen: Counter[str] = field(default_factory=Counter)


async def _rows(engine: AsyncEngine, story: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT * FROM facts WHERE story_id = :s"), {"s": story})
        return {str(r.id): r for r in found}


async def _current(engine: AsyncEngine, story: str) -> list[canon.Fact]:
    async with engine.connect() as conn:
        return await canon.canon(conn, story)


def _covers(f: canon.Fact, t: int) -> bool:
    lo = f.valid_from_inner
    hi = f.valid_to_inner
    return (lo is None or lo <= t) and (hi is None or t < hi)


def _overlap(a: canon.Fact, b: canon.Fact) -> bool:
    lo = max(x for x in (a.valid_from, b.valid_from, -(10**12)) if x is not None)
    hi = min(x for x in (a.valid_to, b.valid_to, 10**12) if x is not None)
    return lo < hi


async def _head(engine: AsyncEngine, story: str) -> int:
    found = await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
    return int(found) - 1


async def _apply(engine: AsyncEngine, w: World, step: tuple[Any, ...], tally: Tally) -> None:
    story = w.story_id
    kind = step[0]
    tally.seen[kind] += 1
    people = [w.people[p] for p in PEOPLE]
    places = [w.places[p] for p in PLACES]
    if kind == "commit":
        _, who, where, lo, n, open_start = step
        await commit(
            engine,
            story,
            at(people[who], places[where], None if open_start else lo * 10, (lo + n) * 10),
        )
    elif kind == "open":
        _, who, where, lo = step
        await commit(engine, story, at(people[who], places[where], lo * 10, None))
    elif kind == "negate":
        _, who, where, lo, n = step
        await commit(
            engine, story, at(people[who], places[where], lo * 10, (lo + n) * 10, negated=True)
        )
    elif kind in ("supersede", "retcon"):
        current = sorted(await _current(engine, story), key=lambda f: f.id)
        if not current:
            return
        old = current[step[1] % len(current)]
        lo = None if old.valid_from is None else old.valid_from - (15 if kind == "retcon" else 0)
        hi = None if old.valid_to is None else old.valid_to + (15 if kind == "retcon" else 0)
        new = at(old.subject_id, places[step[2]], lo, hi)
        if kind == "retcon":
            await supersede(engine, story, old.id, new, authority="author")
        elif step[3]:
            await supersede(engine, story, old.id, new.negation())
        else:
            await supersede(engine, story, old.id, new)
    elif kind == "double":
        _, who, first, second, lo = step

        async def both(conn: AsyncConnection, op: Operation) -> None:
            for where, span in ((first, (lo * 10, lo * 10 + 40)), (second, (lo * 10 + 20, None))):
                await canon.commit_fact(
                    conn,
                    story,
                    at(people[who], places[where], *span),
                    provenance=RecordProvenance.RENDER,
                    operation=op,
                )

        await run(engine, story, both)
    elif kind == "truncate":
        last = await _head(engine, story)
        target = last - 1 - step[1]
        if target >= 0:
            await truncate(engine, story, target)
    elif kind == "untruncate":
        await untruncate(engine, story)
    elif kind == "regenerate":
        last = await _head(engine, story)
        if last >= 1:
            _, who, where, lo = step
            await commit(engine, story, at(people[who], places[where], lo * 10, None), turn=last)
    else:
        await play_turn(engine, story, "a plain turn")


def _check(before: dict[str, Any], rows: dict[str, Any], current: list[canon.Fact]) -> int:
    """The per-step invariants (the module docstring); answers the number of closed rows."""
    assert set(before) <= set(rows), "a fact version was deleted"
    for fact_id, old in before.items():
        new = rows[fact_id]
        for column in IMMUTABLE:
            assert getattr(new, column) == getattr(old, column), (fact_id, column)
        for column in ("tx_to", "superseded_by"):
            if getattr(old, column) is not None:
                assert getattr(new, column) == getattr(old, column), (fact_id, column)
    closers: dict[str, list[Any]] = {}
    for row in rows.values():
        for closed in row.supersedes:
            closers.setdefault(str(closed), []).append(row)
    for fact_id, row in rows.items():
        if fact_id in closers:
            first = min(closers[fact_id], key=lambda r: r.tx_from)
            assert row.tx_to == first.tx_from, fact_id
            assert row.tx_from < row.tx_to
            assert row.superseded_by is not None
        else:
            assert row.tx_to is None, fact_id
            assert row.superseded_by is None, fact_id
    lineages = Counter(f.lineage_id for f in current)
    assert all(n == 1 for n in lineages.values()), lineages
    for i, a in enumerate(current):
        for b in current[i + 1 :]:
            same_key = (
                a.subject_id == b.subject_id and a.predicate_id == b.predicate_id == LOCATED_AT
            )
            both_affirmed = (
                a.proposition.polarity.value == b.proposition.polarity.value == "affirmed"
            )
            same_shape = a.proposition.normalized_key() == b.proposition.normalized_key()
            if (same_key and both_affirmed) or same_shape:
                assert not _overlap(a, b), (a.id, b.id)
    return len(closers)


async def _record_time(engine: AsyncEngine) -> datetime:
    async with engine.begin() as conn:
        found = await conn.scalar(text("SELECT transaction_timestamp()"))
    assert isinstance(found, datetime)
    return found


async def _example(url: str, history: list[tuple[Any, ...]], tally: Tally) -> None:
    engine = make_engine(auth_settings(url))
    try:
        w = await world(engine)
        story = w.story_id
        await play_turn(engine, story, "the first turn")
        checkpoints: list[Checkpoint] = []
        rows = await _rows(engine, story)
        ever_inactive: set[str] = set()
        for step in history:
            await _apply(engine, w, step, tally)
            before, rows = rows, await _rows(engine, story)
            current = await _current(engine, story)
            tally.closures = max(tally.closures, _check(before, rows, current))
            async with engine.connect() as conn:
                for t in TIMES:
                    expected = {f.id for f in current if _covers(f, t)}
                    found = await canon.canon_at(conn, story, story_time=t)
                    assert {f.id for f in found} == expected, t
            active = {k for k, r in rows.items() if str(r.membership) == "active"}
            back = ever_inactive & active
            tally.discarded_then_back += len(back)
            ever_inactive |= set(rows) - active
            checkpoints.append(
                Checkpoint(await _record_time(engine), {f.id for f in current}, active)
            )

        # Replaying versions reconstructs the record at every checkpoint.
        rows = await _rows(engine, story)
        for index, point in enumerate(checkpoints):
            inferred = {
                k
                for k, r in rows.items()
                if r.tx_from <= point.record_time
                and (str(r.membership) == "active" or r.membership_tx > point.record_time)
            }
            known = {k for k, r in rows.items() if r.tx_from <= point.record_time}
            if inferred != point.active & known:
                assert index > 0, "the first checkpoint is always reconstructible"
                tally.skipped += 1
                continue
            async with engine.connect() as conn:
                found = await canon.canon_as_recorded(conn, story, record_time=point.record_time)
            assert {f.id for f in found} == point.current, index
            tally.checked += 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.part("M4", "FR-BITEMP-1,2,3,4,5")
def test_bitemp(pg_url: str) -> None:
    """See the module docstring."""
    tally = Tally()

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(history=steps)
    # The complement needs a version a truncation discarded and an untruncation brought back;
    # a random draw does not guarantee one, so this example always does (#446's flake class).
    @example(
        history=[
            ("commit", 0, 0, 1, 2, False),
            ("commit", 1, 1, 3, 2, False),
            ("supersede", 0, 2, False),
            ("retcon", 0, 1),
            ("double", 0, 0, 1, 5),
            ("truncate", 0),
            ("untruncate",),
        ]
    )
    def monotonic(history: list[tuple[Any, ...]]) -> None:
        asyncio.run(_example(pg_url, history, tally))

    monotonic()
    # Complement: the runs exercised what the invariants are about.
    assert tally.closures > 0
    assert tally.discarded_then_back > 0
    assert tally.checked > 0
    assert tally.checked >= tally.skipped
    assert {"commit", "supersede", "retcon", "truncate", "untruncate", "double"} <= set(tally.seen)
