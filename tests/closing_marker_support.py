"""Helpers for #471's closing-marker tests (ADR-189): the pre-#471 anti-join kept as an oracle,
and the checks ``tests/property/test_bitemp.py``'s BITEMP parts run.

A version is closed by the rows whose ``supersedes`` names it. Canon reads used to find them
with an anti-join over ``supersedes`` (a GIN probe per candidate row); they now find them by
primary key through the version's ``closed_by``, the stored inverse written with each closer.

- ``check_equivalence``: random histories of commits (trimming or closing what they overlap),
  negations, supersedes, two writes in one operation, truncations, untruncations,
  regenerations, plain turns, erasures (a fact row deleted under ``epistrel.erasure``, the
  closer's row included), forks (the history goes on in the fork) and excluded operations
  (``membership.excluding``). After every step, ``closed_by`` holds exactly the existing rows
  whose ``supersedes`` names the version (plus only erased ids), and the current reads answer
  as the oracle does. At the end, every read kind (``canon``, ``canon_at``,
  ``canon_as_recorded``, ``_load``'s current flag) answers as the oracle does at every narrative
  position, every step's record-time, and with each closing operation excluded.
- ``check_read_sql``: the read functions' compiled SQL has no containment test of
  ``supersedes`` and finds closers through ``closed_by``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import ColumnElement, cast, exists, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import membership
from epistrel.core.canon import facts as canon
from epistrel.core.db import make_engine
from epistrel.core.enums import RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.membership import active_as_of, active_only
from epistrel.core.tables.facts import facts
from epistrel.core.tables.propositions import entities
from tests.auth_support import auth_settings
from tests.canon_support import World, at, commit, run, supersede, world
from tests.event_support import play_turn, scalar
from tests.fork_support import fork
from tests.projection_support import truncate, untruncate

PEOPLE = ("Bobby", "Ann")
PLACES = ("kitchen", "garden", "school")
STORY_TIMES = (-5, 25, 60, 110)


def oracle_closed_by(
    f: Any, record_time: datetime | None, through: canon.Position | None = None
) -> ColumnElement[bool]:
    """The pre-#471 ``canon._closed_by``: some row in the timeline (at ``record_time``, or
    now; at or before ``through`` when given) whose ``supersedes`` contains ``f``'s id."""
    s = canon._CLOSER
    where = [
        s.c.story_id == f.c.story_id,
        s.c.supersedes.contains(cast(array([f.c.id]), ARRAY(facts.c.id.type))),
    ]
    if record_time is None:
        where.append(active_only(s))
    else:
        where += [s.c.tx_from <= record_time, active_as_of(s, record_time)]
    if through is not None:
        where.append(canon._through(s, through))
    return exists().where(*where)


@contextmanager
def oracle() -> Iterator[None]:
    """Canon reads inside the block decide closure with ``oracle_closed_by``."""
    marker = canon._closed_by
    canon._closed_by = oracle_closed_by
    try:
        yield
    finally:
        canon._closed_by = marker


# -- histories ----------------------------------------------------------------------------------

person = st.integers(0, len(PEOPLE) - 1)
place = st.integers(0, len(PLACES) - 1)
start = st.integers(0, 11)
length = st.integers(1, 6)
steps = st.lists(
    st.one_of(
        st.tuples(st.just("commit"), person, place, start, length, st.booleans()),
        st.tuples(st.just("negate"), person, place, start, length),
        st.tuples(st.just("supersede"), st.integers(0, 50), place, st.booleans()),
        st.tuples(st.just("double"), person, place, place, start),
        st.tuples(st.just("truncate"), st.integers(0, 2)),
        st.tuples(st.just("untruncate")),
        st.tuples(st.just("regenerate"), person, place, start),
        st.tuples(st.just("turn")),
        st.tuples(st.just("erase"), st.integers(0, 50), st.booleans()),
        st.tuples(st.just("fork"), st.integers(0, 3)),
        st.tuples(st.just("exclude"), st.integers(0, 50)),
    ),
    min_size=4,
    max_size=18,
)


@dataclass
class Tally:
    closures: int = 0
    reopened_by_exclusion: int = 0
    dangling: int = 0
    forked_closed: int = 0
    seen: Counter[str] = field(default_factory=Counter)


@dataclass
class Run:
    w: World
    record_times: list[datetime] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    erased: set[str] = field(default_factory=set)


async def _rows(engine: AsyncEngine, story: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT * FROM facts WHERE story_id = :s"), {"s": story})
        return {str(r.id): r for r in found}


async def _head(engine: AsyncEngine, story: str) -> int:
    found = await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
    return int(found) - 1


async def _record_time(engine: AsyncEngine) -> datetime:
    async with engine.begin() as conn:
        found = await conn.scalar(text("SELECT transaction_timestamp()"))
    assert isinstance(found, datetime)
    return found


async def _world_of(engine: AsyncEngine, story: str, cast_of: dict[str, str]) -> World:
    """The fork's people and places, by name."""
    async with engine.connect() as conn:
        named = {
            str(r.canonical_name): str(r.id)
            for r in await conn.execute(
                select(entities.c.id, entities.c.canonical_name).where(entities.c.story_id == story)
            )
        }
    return World(
        story,
        cast_of,
        {p: named[p] for p in PEOPLE},
        {p: named[p] for p in PLACES},
    )


async def _apply(engine: AsyncEngine, r: Run, step: tuple[Any, ...], tally: Tally) -> None:
    story = r.w.story_id
    kind = step[0]
    tally.seen[kind] += 1
    people = [r.w.people[p] for p in PEOPLE]
    places = [r.w.places[p] for p in PLACES]
    if kind == "commit":
        _, who, where, lo, n, open_start = step
        await commit(
            engine,
            story,
            at(people[who], places[where], None if open_start else lo * 10, (lo + n) * 10),
        )
    elif kind == "negate":
        _, who, where, lo, n = step
        await commit(
            engine, story, at(people[who], places[where], lo * 10, (lo + n) * 10, negated=True)
        )
    elif kind == "supersede":
        async with engine.connect() as conn:
            current = sorted(await canon.canon(conn, story), key=lambda f: f.id)
        if not current:
            return
        old = current[step[1] % len(current)]
        new = at(old.subject_id, places[step[2]], old.valid_from, old.valid_to)
        await supersede(engine, story, old.id, new.negation() if step[3] else new)
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
        target = await _head(engine, story) - 1 - step[1]
        if target >= 0:
            await truncate(engine, story, target)
    elif kind == "untruncate":
        await untruncate(engine, story)
    elif kind == "regenerate":
        last = await _head(engine, story)
        if last >= 1:
            _, who, where, lo = step
            await commit(engine, story, at(people[who], places[where], lo * 10, None), turn=last)
    elif kind == "erase":
        rows = await _rows(engine, story)
        pool = sorted(k for k, v in rows.items() if v.supersedes) if step[2] else sorted(rows)
        if not pool:
            return
        victim = pool[step[1] % len(pool)]
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL epistrel.erasure = 'on'"))
            await conn.execute(
                text("DELETE FROM facts WHERE story_id = :s AND id = :i"), {"s": story, "i": victim}
            )
        r.erased.add(victim)
    elif kind == "fork":
        head = await _head(engine, story)
        forked = await fork(engine, story, at_turn=max(0, head - step[1]))
        r.w = await _world_of(engine, forked.story_id, r.w.characters)
        r.record_times.clear()
        r.excluded.clear()
        r.erased.clear()
        rows = await _rows(engine, forked.story_id)
        tally.forked_closed += sum(1 for v in rows.values() if v.closed_by)
    elif kind == "exclude":
        rows = await _rows(engine, story)
        operations = sorted({str(v.operation_id) for v in rows.values()})
        if operations:
            r.excluded = [operations[step[1] % len(operations)]]
    else:
        await play_turn(engine, story, "a plain turn")


# -- the checks ---------------------------------------------------------------------------------


def _check_marker(rows: dict[str, Any], erased: set[str], tally: Tally) -> None:
    """``closed_by`` is the inverse of ``supersedes`` over the existing rows; any other id in
    it is a row an erasure deleted."""
    closers: dict[str, set[str]] = {}
    for fact_id, row in rows.items():
        for closed in row.supersedes:
            closers.setdefault(str(closed), set()).add(fact_id)
    for fact_id, row in rows.items():
        stored = {str(c) for c in row.closed_by}
        assert len(stored) == len(row.closed_by), (fact_id, "a closer is stored once")
        assert stored & set(rows) == closers.get(fact_id, set()), fact_id
        dangling = stored - set(rows)
        assert dangling <= erased, (fact_id, dangling)
        tally.dangling += len(dangling)
    tally.closures = max(tally.closures, len(closers))


Answers = dict[tuple[Any, ...], list[str]]


async def _answers(
    conn: AsyncConnection,
    story: str,
    *,
    positions: list[canon.Position | None],
    record_times: list[datetime],
    loaded: list[str],
) -> Answers:
    found: Answers = {}
    for position in positions:
        rows = await canon.canon(conn, story, through_position=position)
        found[("canon", position)] = [f.id for f in rows]
        for t in STORY_TIMES:
            rows = await canon.canon_at(conn, story, story_time=t, through_position=position)
            found[("canon_at", position, t)] = [f.id for f in rows]
    for index, record_time in enumerate(record_times):
        rows = await canon.canon_as_recorded(conn, story, record_time=record_time)
        found[("as_recorded", index)] = [f.id for f in rows]
    for fact_id in loaded:
        _, current = await canon._load(conn, story, fact_id)
        found[("load", fact_id)] = [str(current)]
    return found


async def _compare(
    engine: AsyncEngine,
    story: str,
    *,
    positions: list[canon.Position | None],
    record_times: list[datetime],
    loaded: list[str],
    exclusions: list[list[str]],
    tally: Tally,
) -> None:
    """Every read answers with the marker as with the oracle, with each exclusion."""
    async with engine.connect() as conn:
        plain: Answers | None = None
        for excluded in exclusions:
            with membership.excluding(excluded):
                marker = await _answers(
                    conn, story, positions=positions, record_times=record_times, loaded=loaded
                )
                with oracle():
                    expected = await _answers(
                        conn, story, positions=positions, record_times=record_times, loaded=loaded
                    )
            assert marker == expected, (excluded, _first_difference(marker, expected))
            if not excluded:
                plain = marker
            elif plain is not None and any(
                set(marker[k]) - set(plain[k]) for k in marker if k[0] != "load"
            ):
                tally.reopened_by_exclusion += 1


def _first_difference(found: Answers, expected: Answers) -> tuple[Any, ...]:
    for key in expected:
        if found.get(key) != expected[key]:
            return key, found.get(key), expected[key]
    return ()


async def _closing_operations(engine: AsyncEngine, story: str) -> list[str]:
    rows = await _rows(engine, story)
    return sorted({str(v.operation_id) for v in rows.values() if v.supersedes})


async def _example(url: str, history: list[tuple[Any, ...]], tally: Tally) -> None:
    engine = make_engine(auth_settings(url))
    try:
        r = Run(await world(engine))
        await play_turn(engine, r.w.story_id, "the first turn")
        for step in history:
            await _apply(engine, r, step, tally)
            story = r.w.story_id
            _check_marker(await _rows(engine, story), r.erased, tally)
            head = await _head(engine, story)
            await _compare(
                engine,
                story,
                positions=[None, (max(0, head - 1), 0)],
                record_times=r.record_times[-1:],
                loaded=[],
                exclusions=[[], r.excluded] if r.excluded else [[]],
                tally=tally,
            )
            r.record_times.append(await _record_time(engine))

        # Every read kind, at every narrative position and every step's record-time, with
        # each closing operation left out in turn.
        story = r.w.story_id
        head = await _head(engine, story)
        closing = await _closing_operations(engine, story)
        await _compare(
            engine,
            story,
            positions=[None, *((t, 0) for t in range(head + 1)), (head, 1)],
            record_times=r.record_times,
            loaded=sorted(await _rows(engine, story)),
            exclusions=[[], *([o] for o in closing[:4])],
            tally=tally,
        )
    finally:
        await engine.dispose()


def check_equivalence(pg_url: str, *, examples: int = 200) -> None:
    """``test_bitemp__closing_marker`` (see the module docstring)."""
    tally = Tally()

    @settings(
        max_examples=examples,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(history=steps)
    # A closer erased, a fork of closed versions that goes on writing, and a truncation that
    # reopens what a discarded closer closed: a random draw guarantees none of them.
    @example(
        history=[
            ("commit", 0, 0, 1, 4, False),
            ("commit", 0, 1, 2, 4, False),
            ("supersede", 0, 2, False),
            ("double", 1, 0, 1, 3),
            ("fork", 0),
            ("commit", 0, 0, 3, 2, False),
            ("exclude", 0),
            ("erase", 0, True),
            ("truncate", 0),
            ("untruncate",),
        ]
    )
    def equivalent(history: list[tuple[Any, ...]]) -> None:
        asyncio.run(_example(pg_url, history, tally))

    equivalent()
    assert tally.closures > 0
    assert tally.dangling > 0
    assert tally.forked_closed > 0
    assert tally.reopened_by_exclusion > 0
    assert {"commit", "supersede", "erase", "fork", "truncate", "exclude"} <= set(tally.seen)


# -- the read path's SQL ------------------------------------------------------------------------


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def check_read_sql() -> None:
    """The selection ``canon``, ``canon_at``, ``canon_as_recorded`` and the read set
    (``KeyedLookup.canon_context``, through ``canon_at``) run, now, through a position and at a
    record-time, tests no ``supersedes`` containment, and finds closers by ``closed_by``."""
    f = canon._CANON
    shapes = {
        "now": canon._canon_where(f, "S", story_time=3),
        "through": canon._canon_where(f, "S", story_time=3, through_position=(4, 0)),
        "recorded": canon._canon_where(f, "S", record_time=datetime(2026, 1, 1)),
    }
    for name, where in shapes.items():
        sql = _compiled(canon._select(f).where(*where))
        assert "closer.id = ANY (f.closed_by)" in sql, name
        assert "supersedes @>" not in sql, name
        assert "supersedes" not in sql.split("WHERE", 1)[1], name
    with oracle():
        old = _compiled(canon._select(f).where(*canon._canon_where(f, "S")))
    assert "closer.supersedes @>" in old  # the check can see a containment test
