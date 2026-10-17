"""TIME (M4), the registry slices: derived records follow the membership of the operation
that produced them (FR-STORE-29; TIME (c), docs/04 §6; #130's ``test_membership_registry``).

TIME (M4) has one file, this one: #133 (facts, ``test_time__m4_facts``) and #135 (memories) add
their (c) slices here.

The real derived tables land with their stories (facts #133, memories #135, utterances #150,
thunks, ledger and resolutions #146/#154) and register in ``core.membership.DERIVED_TABLES``.
This test proves the generic machinery first, over a test-only derived table that the fixture
creates outside ``metadata`` and drops afterwards (so the drift check and story deletion's
``information_schema`` scan never see it; the suite runs serially), with ``DERIVED_TABLES``
snapshotted and restored around it. It has no text column (#123's subject scan ignores it).

The complement runs first: the fixture table, built with the derived columns but not registered,
fails the schema check. Then: the columns, types, nullability, foreign key and indexes; the
flip as the first in-transaction membership hook of truncate, untruncate and swap, in the
control's own transaction; stacked truncations keep their stamp; ``superseded`` rows and
valid-time are never touched; a regenerate and a turn written over a rewound tail take derived
rows dormant; a fork flattens them; ``active_only`` reads see exactly the active continuation's
rows; and with nothing registered, a flip issues no statement (the Lite path).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import (
    TIMESTAMP,
    Column,
    MetaData,
    Table,
    event,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.schema import CreateIndex, CreateTable

from epistrel.core import fork as forking
from epistrel.core import timeline
from epistrel.core.canon import facts as canon
from epistrel.core.enums import Membership
from epistrel.core.ids import new_ulid
from epistrel.core.membership import (
    DERIVED_COLUMNS,
    DERIVED_INDEX_COLUMNS,
    DERIVED_TABLES,
    DerivedTable,
    active_only,
    active_only_sql,
    derived_columns,
    derived_index_names,
    derived_indexes,
    flip_by_sources,
    flip_membership,
    follow_membership_change,
    insert_derived,
    position_of,
    register_derived,
    tx_time_of,
    valid_time_of,
)
from epistrel.core.stories import STORY_TABLES
from epistrel.core.tables import operations
from epistrel.core.tables.base import ulid_type
from epistrel.core.variants import ChainRef
from tests.canon_support import at, commit, supersede, world
from tests.concurrency_support import regenerate
from tests.event_support import interleave, new_story, play_turn
from tests.fork_support import fork
from tests.projection_support import swap, truncate, untruncate

NAME = "derived_fixture"
VALID_FROM = datetime(1999, 1, 1, tzinfo=UTC)
VALID_TO = datetime(1999, 1, 2, tzinfo=UTC)

NOT_DERIVED: frozenset[str] = frozenset(
    {"operations", "transcript_messages", "lite_summary_segments"}
)
"""Tables with ``operation_id`` and ``membership`` that are not derived records: the operations
themselves and the M2 projections, which keep their own membership handling."""

EXPECTED_TYPES: dict[str, tuple[str, str, str]] = {
    "operation_id": ("character", "bpchar", "NO"),
    "source_event_ids": ("ARRAY", "_bpchar", "NO"),
    "narrative_turn": ("integer", "int4", "YES"),
    "turn_variant": ("smallint", "int2", "YES"),
    "membership": ("USER-DEFINED", "membership", "NO"),
    "membership_tx": ("timestamp with time zone", "timestamptz", "NO"),
}
"""``information_schema`` data type, udt name and nullability of each derived column."""


# -- the schema check -------------------------------------------------------------------------


async def schema_findings(conn: AsyncConnection) -> list[str]:
    """What the registry gets wrong against the live database: tables carrying derived
    columns that are not registered, and registered tables whose columns, foreign key or
    indexes are not the standard ones."""
    found = await conn.execute(
        text(
            "SELECT table_schema, table_name, column_name, data_type, udt_name, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
    )
    tables: dict[str, dict[str, tuple[str, str, str]]] = {}
    for r in found:
        tables.setdefault(r.table_name, {})[r.column_name] = (
            r.data_type,
            r.udt_name,
            r.is_nullable,
        )
    findings = [
        f"unregistered derived table {name}"
        for name, columns in sorted(tables.items())
        if {"operation_id", "membership"} <= columns.keys()
        and name not in NOT_DERIVED
        and name not in DERIVED_TABLES
    ]
    for name, entry in DERIVED_TABLES.items():
        columns = tables.get(name)
        if columns is None:
            findings.append(f"registered derived table {name} does not exist")
            continue
        for column, expected in EXPECTED_TYPES.items():
            if columns.get(column) != expected:
                findings.append(f"{name}.{column} is {columns.get(column)}, not {expected}")
        fk = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "WHERE c.contype = 'f' AND t.relname = :t AND a.attname = 'operation_id' "
                "AND c.confrelid = 'public.operations'::regclass"
            ),
            {"t": name},
        )
        if not fk:
            findings.append(f"{name}.operation_id has no foreign key to operations")
        indexes = await conn.execute(
            text(
                "SELECT i.relname AS name, array_agg(a.attname ORDER BY k.n) AS columns "
                "FROM pg_index x JOIN pg_class t ON t.oid = x.indrelid "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, n) "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                "WHERE t.relname = :t GROUP BY i.relname"
            ),
            {"t": name},
        )
        have = {r.name: tuple(r.columns) for r in indexes}
        for index, columns_of in zip(derived_index_names(name), DERIVED_INDEX_COLUMNS, strict=True):
            if have.get(index) != columns_of:
                findings.append(f"{name} lacks index {index} on {columns_of}")
        for column in entry.operation_columns:
            if column not in columns:
                findings.append(f"{name} has no operation column {column}")
    return findings


# -- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def registry() -> Iterator[dict[str, DerivedTable]]:
    """``DERIVED_TABLES`` as shipped, restored after the test."""
    shipped = dict(DERIVED_TABLES)
    try:
        yield shipped
    finally:
        DERIVED_TABLES.clear()
        DERIVED_TABLES.update(shipped)


def _fixture_table() -> Table:
    md = MetaData()
    operations.to_metadata(md)  # resolves the derived columns' foreign key; never created
    return Table(
        NAME,
        md,
        Column("id", ulid_type(), primary_key=True),
        Column("story_id", ulid_type(), nullable=False),
        *derived_columns(),
        Column("valid_from", TIMESTAMP(timezone=True), nullable=True),
        Column("valid_to", TIMESTAMP(timezone=True), nullable=True),
        *derived_indexes(NAME),
    )


@pytest.fixture
async def derived(engine: AsyncEngine, registry: dict[str, DerivedTable]) -> AsyncIterator[Table]:
    """The test-only derived table, created (not registered) and dropped afterwards."""
    table = _fixture_table()
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {NAME}"))
        await conn.execute(CreateTable(table))
        for index in table.indexes:
            await conn.execute(CreateIndex(index))
    try:
        yield table
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {NAME}"))


def _register(table: Table) -> DerivedTable:
    return register_derived(DerivedTable(table, valid_time=("valid_from", "valid_to")))


# -- helpers ----------------------------------------------------------------------------------


async def _derive(engine: AsyncEngine, table: Table, story: str, op_id: str) -> str:
    """One derived row of operation ``op_id``, at its position, from its events."""
    async with engine.begin() as conn:
        found = (
            await conn.execute(
                text("SELECT narrative_turn, turn_variant FROM operations WHERE operation_id = :o"),
                {"o": op_id},
            )
        ).one()
        sources = list(
            await conn.scalars(
                text("SELECT id FROM events WHERE operation_id = :o ORDER BY seq"), {"o": op_id}
            )
        )
        row_id = new_ulid()
        await insert_derived(
            conn,
            table,
            [
                {
                    "id": row_id,
                    "story_id": story,
                    "operation_id": op_id,
                    "source_event_ids": sources,
                    "narrative_turn": found.narrative_turn,
                    "turn_variant": found.turn_variant,
                    "valid_from": VALID_FROM,
                    "valid_to": VALID_TO,
                }
            ],
        )
    return row_id


async def _rows(engine: AsyncEngine, table: Table, story: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(select(table).where(table.c.story_id == story))
        return {r.id: r for r in found}


async def _states(engine: AsyncEngine, table: Table, story: str) -> dict[str, str]:
    return {k: str(r.membership) for k, r in (await _rows(engine, table, story)).items()}


async def _supersede(engine: AsyncEngine, table: Table, row_id: str) -> None:
    """Mark a row ``superseded`` the way its own table would (a flip never does)."""
    async with engine.begin() as conn:
        await conn.execute(
            update(table).where(table.c.id == row_id).values(membership="superseded")
        )


async def _active(engine: AsyncEngine, table: Table, story: str) -> set[str]:
    async with engine.connect() as conn:
        found = await conn.scalars(
            select(table.c.id).where(table.c.story_id == story, active_only(table))
        )
        return set(found)


async def _op_tx(engine: AsyncEngine, op_id: str) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(
            select(operations.c.membership_tx).where(operations.c.operation_id == op_id)
        )


async def _story(engine: AsyncEngine, table: Table, turns: int) -> tuple[str, dict[int, str]]:
    story = await new_story(engine)
    rows: dict[int, str] = {}
    for n in range(1, turns + 1):
        op = await play_turn(engine, story, f"turn {n}")
        rows[n] = await _derive(engine, table, story, op.operation_id)
    return story, rows


# -- the test ---------------------------------------------------------------------------------


@pytest.mark.part("M4", "FR-STORE-29")
async def test_time__membership_registry(engine: AsyncEngine, derived: Table) -> None:
    table = derived
    # Complement first: derived columns without a registration fail the schema check.
    async with engine.connect() as conn:
        assert await schema_findings(conn) == [f"unregistered derived table {NAME}"]
    entry = _register(table)
    async with engine.connect() as conn:
        assert await schema_findings(conn) == []
    assert entry is DERIVED_TABLES[NAME]
    assert set(DERIVED_COLUMNS) <= set(table.c.keys())

    # The flip is the first in-transaction membership hook.
    assert timeline._HOOKS[timeline.HookPhase.IN_TRANSACTION][0] is follow_membership_change

    story, rows = await _story(engine, table, 4)
    before = await _rows(engine, table, story)
    assert set((await _states(engine, table, story)).values()) == {"active"}

    # Truncate to 2: turns 3 and 4's rows go discarded, stamped in the truncation's own
    # transaction (the same membership_tx as their operations); valid-time and position untouched.
    await truncate(engine, story, 2)
    after = await _rows(engine, table, story)
    assert {n: str(after[r].membership) for n, r in rows.items()} == {
        1: "active",
        2: "active",
        3: "discarded",
        4: "discarded",
    }
    for n in (3, 4):
        row = after[rows[n]]
        assert row.membership_tx == await _op_tx(engine, row.operation_id)
        assert row.membership_tx > before[rows[n]].membership_tx
    for n, r in rows.items():
        assert (after[r].valid_from, after[r].valid_to) == (VALID_FROM, VALID_TO)
        assert (after[r].narrative_turn, after[r].turn_variant) == (n, 1)
    assert after[rows[1]].membership_tx == before[rows[1]].membership_tx
    assert await _active(engine, table, story) == {rows[1], rows[2]}

    # A stacked truncation leaves the already-discarded rows' stamps alone; a superseded row
    # is never touched by a flip.
    await _supersede(engine, table, rows[1])
    stamped = (await _rows(engine, table, story))[rows[4]].membership_tx
    await truncate(engine, story, 1)
    now = await _rows(engine, table, story)
    assert str(now[rows[2]].membership) == "discarded"
    assert now[rows[4]].membership_tx == stamped
    assert str(now[rows[1]].membership) == "superseded"

    # Untruncate (LIFO) twice: everything returns, the superseded row stays superseded.
    await untruncate(engine, story)
    assert str((await _rows(engine, table, story))[rows[2]].membership) == "active"
    await untruncate(engine, story)
    assert _by_turn(await _states(engine, table, story), rows) == {
        1: "superseded",
        2: "active",
        3: "active",
        4: "active",
    }

    # A later in-transaction hook that fails rolls the derived flip back with the edit.
    async def refuse(conn: AsyncConnection, change: timeline.MembershipChange) -> None:
        raise RuntimeError("refused")

    timeline.on_membership_change(refuse)
    try:
        with pytest.raises(RuntimeError):
            await truncate(engine, story, 2)
    finally:
        timeline.remove_membership_hook(refuse)
    assert _by_turn(await _states(engine, table, story), rows)[4] == "active"


def _by_turn(states: dict[str, str], rows: dict[int, str]) -> dict[int, str]:
    return {n: states[r] for n, r in rows.items()}


@pytest.mark.part("M4", "FR-STORE-29")
async def test_time__membership_undo_swap(engine: AsyncEngine, derived: Table) -> None:
    """Undoing a rewind brings back exactly what that continuation had decided, and nothing
    decided in the abandoned branch leaks into it (and back again on the next swap)."""
    table = derived
    _register(table)
    story, original = await _story(engine, table, 4)
    await truncate(engine, story, 2)
    newer: dict[int, str] = {}
    for n in (3, 4):
        op = await play_turn(engine, story, f"turn {n} again")
        newer[n] = await _derive(engine, table, story, op.operation_id)
    # The first turn written over the rewound tail shelved it: its rows are dormant now.
    states = await _states(engine, table, story)
    assert {states[original[n]] for n in (3, 4)} == {"dormant"}
    assert await _active(engine, table, story) == {
        original[1],
        original[2],
        newer[3],
        newer[4],
    }

    result = await untruncate(engine, story)
    assert result is not None
    assert result.swapped
    states = await _states(engine, table, story)
    assert await _active(engine, table, story) == set(original.values())
    assert {states[newer[n]] for n in (3, 4)} == {"dormant"}

    assert await swap(engine, story) is not None
    assert await _active(engine, table, story) == {
        original[1],
        original[2],
        newer[3],
        newer[4],
    }
    states = await _states(engine, table, story)
    assert {states[original[n]] for n in (3, 4)} == {"dormant"}


@pytest.mark.part("M4", "FR-STORE-29")
async def test_time__membership_regenerate_and_fork(
    engine: AsyncEngine, derived: Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regenerate retires the old variant's rows to dormant; a fork copies a registered
    table's rows of its copied operations as active, superseded rows kept, with source events
    renumbered to the fork's."""
    table = derived
    _register(table)
    story, rows = await _story(engine, table, 3)
    op = await regenerate(engine, story)
    again = await _derive(engine, table, story, op.operation_id)
    states = await _states(engine, table, story)
    assert states[rows[3]] == "dormant"
    assert await _active(engine, table, story) == {rows[1], rows[2], again}
    await _supersede(engine, table, rows[1])

    # Fork the dormant chain (turn 3, variant 1): its rows are active in the fork.
    monkeypatch.setattr(forking, "STORY_TABLES", (table, *STORY_TABLES))
    forked = await fork(engine, story, variant_chain=ChainRef(turn=3, variant=1))
    copied = await _rows(engine, table, forked.story_id)
    assert sorted(
        (r.narrative_turn, r.turn_variant, str(r.membership)) for r in copied.values()
    ) == [
        (1, 1, "superseded"),
        (2, 1, "active"),
        (3, 1, "active"),
    ]
    async with engine.connect() as conn:
        fork_events = set(
            await conn.scalars(
                text("SELECT id FROM events WHERE story_id = :s"), {"s": forked.story_id}
            )
        )
        fork_ops = set(
            await conn.scalars(
                select(operations.c.operation_id).where(operations.c.story_id == forked.story_id)
            )
        )
    for r in copied.values():
        assert r.source_event_ids
        assert set(r.source_event_ids) <= fork_events
        assert r.operation_id in fork_ops
    # The source is untouched by the fork.
    assert await _states(engine, table, story) == {**states, rows[1]: "superseded"}


@pytest.mark.part("M4", "FR-STORE-29")
async def test_time__membership_helpers(
    engine: AsyncEngine, derived: Table, registry: dict[str, DerivedTable]
) -> None:
    """The coordinate helpers keep position, valid-time and transaction-time apart; the
    shipped registry is story-scoped; nothing registered means a flip issues no statement."""
    table = derived
    # The shipped registry is a subset of the story tables (deleted with the story, forked).
    assert set(registry) <= {t.name for t in STORY_TABLES}

    # Lite path: with nothing registered, a flip (and so every control action) adds no SQL.
    DERIVED_TABLES.clear()
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        async with engine.begin() as conn:
            assert await flip_membership(conn, ["X" * 26], Membership.DISCARDED) == 0
            assert await flip_by_sources(conn, ["X" * 26]) == 0
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert [s for s in statements if NAME in s] == []
    assert not any("UPDATE" in s for s in statements)

    # Unregistered tables are refused by every helper.
    with pytest.raises(KeyError):
        active_only(table)
    _register(table)
    alias = table.alias("d")
    assert str(active_only(alias).compile()) == "d.membership = :membership_1"
    assert active_only_sql("d") == "d.membership = 'active'"
    with pytest.raises(ValueError, match="alias"):
        active_only_sql("d; DROP TABLE x")
    assert [c.name for c in position_of(alias)] == ["narrative_turn", "turn_variant"]
    valid = valid_time_of(table)
    assert valid is not None
    assert [c.name for c in valid] == ["valid_from", "valid_to"]
    assert tx_time_of(table) is None
    # A flip cannot set superseded; a derived row cannot name its own membership.
    async with engine.begin() as conn:
        with pytest.raises(ValueError, match="cannot set membership superseded"):
            await flip_membership(conn, ["X" * 26], Membership.SUPERSEDED)
        with pytest.raises(ValueError, match="sets membership itself"):
            await insert_derived(
                conn, table, [{"id": new_ulid(), "story_id": "S", "membership": "dormant"}]
            )

    # flip_by_sources: rows whose sources overlap go with them, and come back.
    story, rows = await _story(engine, table, 2)
    second = (await _rows(engine, table, story))[rows[2]]
    sources = second.source_event_ids
    async with engine.begin() as conn:
        assert await flip_by_sources(conn, sources[:1], story_id=story) == 1
    assert await _active(engine, table, story) == {rows[1]}
    async with engine.begin() as conn:
        assert await flip_by_sources(conn, sources, Membership.ACTIVE, story_id=story) == 1
        # Already active: nothing re-stamped.
        assert (
            await flip_membership(conn, [second.operation_id], Membership.ACTIVE, story_id=story)
            == 0
        )
    assert await _active(engine, table, story) == set(rows.values())


# -- facts (#133) -----------------------------------------------------------------------------


async def _facts(engine: AsyncEngine, story: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT * FROM facts WHERE story_id = :s"), {"s": story})
        return {str(r.id): r for r in found}


def _objects(found: list[Any]) -> list[str | None]:
    return [f.proposition.object_id for f in found]


@pytest.mark.integration
@pytest.mark.part("M4", "FR-STORE-9; FR-BITEMP-5")
async def test_time__m4_facts(engine: AsyncEngine) -> None:
    """TIME (c) for facts: every fact carries the source event and operation that produced it
    and follows that operation's membership; the three coordinates are asserted independently
    by three queries on one fixture, and narrative position never comes from ``seq``.

    The fixture: turn 1 puts Bobby in the kitchen over [0, 100); background, audit and
    control work moves ``seq`` between turns; turn 2 writes a flashback (valid decades
    earlier); turn 3 supersedes the kitchen with the garden. Complement: a query by one
    coordinate gives a different answer than either other coordinate would."""
    assert DERIVED_TABLES["facts"].valid_time == ("valid_from", "valid_to")
    assert DERIVED_TABLES["facts"].tx_time == ("tx_from", "tx_to")
    async with engine.connect() as conn:
        assert await schema_findings(conn) == []

    w = await world(engine)
    story = w.story_id
    bobby, kitchen, garden = w.people["Bobby"], w.places["kitchen"], w.places["garden"]
    first = await commit(engine, story, at(bobby, kitchen, 0, 100))
    await interleave(engine, story, 1)
    flashback = await commit(engine, story, at(bobby, garden, -1_000_000, -900_000))
    await interleave(engine, story, 2)
    second = await supersede(engine, story, first.id, at(bobby, garden, 0, 100))
    rows = await _facts(engine, story)

    # FR-STORE-9: source events and operation; position from the operation, never seq.
    for fact_id, turn in ((first.id, 1), (flashback.id, 2), (second.id, 3)):
        row = rows[fact_id]
        async with engine.connect() as conn:
            op = (
                await conn.execute(
                    select(operations).where(operations.c.operation_id == row.operation_id)
                )
            ).one()
            sources = await conn.execute(
                text("SELECT id, seq, operation_id FROM events WHERE id = ANY(:ids)"),
                {"ids": list(row.source_event_ids)},
            )
            found = list(sources)
        assert found
        assert {str(e.operation_id) for e in found} == {row.operation_id}
        assert (row.narrative_turn, row.turn_variant) == (op.narrative_turn, op.turn_variant)
        assert row.narrative_turn == turn
        assert all(e.seq != turn for e in found)
        assert row.membership_tx == op.membership_tx

    async with engine.connect() as conn:
        # Narrative position: the corrected view through turn 2 still has the kitchen (the
        # garden is turn 3's), while valid-time and record-time both say otherwise.
        through = await canon.canon_at(conn, story, story_time=50, through_position=(2, 0))
        now = await canon.canon_at(conn, story, story_time=50)
        assert _objects(through) == [kitchen]
        assert _objects(now) == [garden]
        # Valid-time: the flashback is true decades before the kitchen, though written after it.
        early = await canon.canon_at(conn, story, story_time=-950_000)
        assert [f.id for f in early] == [flashback.id]
        assert rows[flashback.id].narrative_turn > rows[first.id].narrative_turn
        # Transaction-time: the record at turn 2's commit said kitchen and the flashback.
        recorded = await canon.canon_as_recorded(
            conn, story, record_time=rows[flashback.id].tx_from
        )
        assert {f.id for f in recorded} == {first.id, flashback.id}
        at_turn_one = await canon.canon_as_recorded(conn, story, record_time=rows[first.id].tx_from)
        assert [f.id for f in at_turn_one] == [first.id]

    # Membership follows the operation: a regenerate of turn 3 takes the garden dormant and the
    # kitchen is current again; neither time coordinate moves.
    before = {k: (r.valid_from, r.valid_to, r.tx_from, r.tx_to) for k, r in rows.items()}
    again = await regenerate(engine, story)
    after = await _facts(engine, story)
    assert str(after[second.id].membership) == "dormant"
    assert {k: (r.valid_from, r.valid_to, r.tx_from, r.tx_to) for k, r in after.items()} == before
    async with engine.connect() as conn:
        assert _objects(await canon.canon_at(conn, story, story_time=50)) == [kitchen]
    assert again.narrative_turn == 3

    # A fork at turn 2 copies the kitchen and the flashback, not the garden; the kitchen, closed
    # in the source by a version the fork does not have, is current in the fork.
    forked = await fork(engine, story, at_turn=2)
    copied = await _facts(engine, forked.story_id)
    assert len(copied) == 2
    assert {str(r.membership) for r in copied.values()} == {"active"}
    kitchen_copy = next(r for r in copied.values() if r.valid_from == 0)
    assert kitchen_copy.superseded_by is None
    assert kitchen_copy.tx_to == rows[first.id].tx_to
    async with engine.connect() as conn:
        in_fork = await canon.canon_at(conn, forked.story_id, story_time=50)
        fork_ops = set(
            await conn.scalars(
                select(operations.c.operation_id).where(operations.c.story_id == forked.story_id)
            )
        )
        fork_props = set(
            await conn.scalars(
                text("SELECT id FROM propositions WHERE story_id = :s"), {"s": forked.story_id}
            )
        )
    assert [f.id for f in in_fork] == [kitchen_copy.id]
    for r in copied.values():
        assert r.operation_id in fork_ops
        assert r.proposition_id in fork_props
        assert r.id not in rows
