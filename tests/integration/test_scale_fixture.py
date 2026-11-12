"""#490: the scale fixture's bulk builder writes the rows its commit-path oracle writes.

``build_scale_fixture`` writes the turns' canon in bulk and refolds the projections once; with
``by_commit=True`` it writes each fact through ``canon.commit_fact`` with the projections
maintained as in play. Each test builds one spec both ways, each in a migrated database of its
own (the fixture pins the deployment's vector space), and compares the tables row for row.

- ``test_the_bulk_build_matches_the_commit_path``: ``ScaleSpec.scaled(1_000)``, every table
  P-01, P-02 and P-06 read (``READ_BY_THE_SCALE_SUITES``) and the tables the bulk builder
  derives in one pass (``DERIVED_IN_ONE_PASS``).
- ``test_the_bulk_build_supersedes_as_commit_fact_does``: a small spec whose canon plan also
  overlaps and repeats facts, so versions are closed, trimmed into remainders and rewritten.

Rows are compared as multisets, with timestamps left out and every id replaced by its row's
natural key (``natural_keys``): an event by its ``seq``; a fact by its subject, predicate,
holder, valid-time and object, and the ``seq`` of the event that wrote it; a proposition by the
``seq`` of the ``fact_committed`` event naming it; a memory by its turn, beat and text; an
operation by its kind, turn, variant and order; an entity by its kind and name; a character by
its name. ``maintenance`` operations are left out. The active turns must be one chain, and the
story's ``timeline_revision`` and ``next_turn_index`` must be the oracle's.
"""

from __future__ import annotations

import re
import secrets
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.db import make_engine
from epistrel.core.events.operation import chain_violation
from epistrel.testing.fixtures import scale
from tests.auth_support import auth_settings
from tests.conformance.conftest import migrated_database

pytestmark = pytest.mark.integration

READ_BY_THE_SCALE_SUITES = (
    "amendment_parts",
    "capability_coverage",
    "character_bench",
    "characters",
    "deployment_state",
    "engine_profile_versions",
    "engine_profiles",
    "entities",
    "entity_aliases",
    "erasure_ledger",
    "events",
    "facts",
    "memories",
    "model_config",
    "operations",
    "placement",
    "predicates",
    "propositions",
    "relationships",
    "scene_state",
    "stories",
    "story_profile_overrides",
    "thunks",
)
"""Every table P-01, P-02 and P-06 read once their fixture is built (their smoke runs'
statements, captured on 2026-11-12)."""
DERIVED_IN_ONE_PASS = (
    "belief_state",
    "external_values",
    "lite_summary_segments",
    "nested_belief_state",
    "proposition_changes",
    "state_values",
    "story_heads",
    "story_texts",
    "subject_references",
    "transcript_messages",
)
"""The other tables the bulk builder writes differently: the change index, the subject
references it derives once instead of per commit, and every other projection (it refolds only
those over canon)."""
TABLES = READ_BY_THE_SCALE_SUITES + DERIVED_IN_ONE_PASS
ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
"""An id, alone or inside a string (``character:<id>``)."""


# -- natural keys ------------------------------------------------------------------------------


async def natural_keys(conn: AsyncConnection, story_id: str) -> dict[str, Any]:
    """Every id of the story's rows, mapped to its row's natural key (see the module
    docstring)."""
    keys: dict[str, Any] = {story_id: ("story",)}

    async def rows(sql: str) -> list[Any]:
        return list(await conn.execute(text(sql), {"s": story_id}))

    for r in await rows("SELECT id, name FROM characters WHERE story_id = :s"):
        keys[r.id] = ("character", r.name)
    for r in await rows("SELECT id, kind, canonical_name FROM entities WHERE story_id = :s"):
        keys[r.id] = ("entity", r.kind, r.canonical_name)
    order: Counter[tuple[Any, ...]] = Counter()
    for r in await rows(
        "SELECT operation_id, kind, narrative_turn, turn_variant FROM operations "
        "WHERE story_id = :s ORDER BY operation_id"
    ):
        group = (r.kind, r.narrative_turn, r.turn_variant)
        keys[r.operation_id] = ("operation", *group, order[group])
        order[group] += 1
    seqs: dict[str, int] = {}
    for r in await rows("SELECT id, seq, type, payload FROM events WHERE story_id = :s"):
        keys[r.id] = ("event", r.seq)
        seqs[r.id] = r.seq
        if r.type in ("fact_committed", "fact_superseded"):
            keys[r.payload["proposition_id"]] = ("proposition", r.seq)
    for r in await rows(
        "SELECT f.id, f.subject_id, f.predicate_id, f.holder, f.valid_from, f.valid_to, "
        "f.source_event_ids, p.object_id FROM facts f JOIN propositions p "
        "ON p.id = f.proposition_id WHERE f.story_id = :s"
    ):
        written_by = tuple(sorted(seqs.get(e, -1) for e in r.source_event_ids))
        keys[r.id] = (
            "fact",
            keys.get(r.subject_id),
            r.predicate_id,
            r.holder,
            r.valid_from,
            r.valid_to,
            keys.get(r.object_id),
            written_by,
        )
    for r in await rows("SELECT id, narrative_turn, beat, text FROM memories WHERE story_id = :s"):
        keys[r.id] = ("memory", r.narrative_turn, r.beat, r.text)
    return keys


def normalized(value: Any, keys: Mapping[str, Any]) -> Any:
    """``value`` with every id, alone or inside a string, replaced by its natural key (an id
    with none reads ``<id>``)."""
    if isinstance(value, str):
        if value in keys:
            return keys[value]
        return ULID.sub(lambda m: repr(keys.get(m.group(), "<id>")), value)
    if isinstance(value, Mapping):
        return tuple(
            sorted((repr(normalized(str(k), keys)), normalized(v, keys)) for k, v in value.items())
        )
    if isinstance(value, list | tuple):
        return tuple(normalized(v, keys) for v in value)
    if isinstance(value, Range):
        return ("range", value.lower, value.upper, value.bounds)
    return value


async def snapshot(db: AsyncEngine, story_id: str) -> dict[str, Counter[Any]]:
    """``TABLES``' rows of the story (a deployment-wide table whole), normalized, with
    timestamp columns left out, as multisets."""
    found: dict[str, Counter[Any]] = {}
    async with db.connect() as conn:
        keys = await natural_keys(conn, story_id)
        for table in TABLES:
            columns = [
                (str(r.column_name), str(r.data_type))
                for r in await conn.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t ORDER BY column_name"
                    ),
                    {"t": table},
                )
            ]
            names = {n for n, _ in columns}
            kept = [n for n, t in columns if "timestamp" not in t and n != "embedding"]
            where = (
                "id = :s"
                if table == "stories"
                else "story_id = :s"
                if "story_id" in names
                else "true"
            )
            if table == "operations":
                where += " AND kind <> 'maintenance'"
            select = ", ".join(f'"{n}"' for n in kept)
            rows = await conn.execute(
                text(f"SELECT {select} FROM {table} WHERE {where}"),  # noqa: S608 - fixed names
                {"s": story_id},
            )
            found[table] = Counter(
                tuple((n, normalized(v, keys)) for n, v in zip(kept, row, strict=True))
                for row in rows
            )
    return found


def differences(bulk: Mapping[str, Counter[Any]], oracle: Mapping[str, Counter[Any]]) -> list[str]:
    """Each table whose rows differ, with its counts and a few rows only one side has."""
    out: list[str] = []
    for table in TABLES:
        mine, theirs = bulk[table], oracle[table]
        if mine == theirs:
            continue
        extra = list((mine - theirs).elements())[:2]
        missing = list((theirs - mine).elements())[:2]
        out.append(
            f"{table}: bulk {mine.total()} rows, oracle {theirs.total()}; "
            f"only bulk {extra}; only oracle {missing}"
        )
    return out


# -- building ----------------------------------------------------------------------------------


@asynccontextmanager
async def built(pg_url: str, spec: scale.ScaleSpec, *, by_commit: bool) -> Any:
    """``spec`` built one way in a migrated database of its own (dropped afterwards)."""
    with migrated_database(pg_url, f"epistrel_scale_{secrets.token_hex(4)}") as url:
        db = make_engine(auth_settings(url))
        try:
            fixture = await scale.build_scale_fixture(db, spec, by_commit=by_commit)
            yield db, fixture
        finally:
            await db.dispose()


async def both_ways(pg_url: str, spec: scale.ScaleSpec) -> dict[str, Counter[Any]]:
    """Build ``spec`` both ways and assert the rows are the same (module docstring); answers
    the oracle's rows."""
    async with built(pg_url, spec, by_commit=True) as (oracle_db, oracle):
        expected = await snapshot(oracle_db, oracle.story.story_id)
        expected_story = await story_counters(oracle_db, oracle.story.story_id)
    async with built(pg_url, spec, by_commit=False) as (bulk_db, bulk):
        found = await snapshot(bulk_db, bulk.story.story_id)
        story = await story_counters(bulk_db, bulk.story.story_id)
        async with bulk_db.connect() as conn:
            assert await chain_violation(conn, bulk.story.story_id) is None
    assert (bulk.story.facts, bulk.story.events, bulk.story.memories, bulk.story.head) == (
        oracle.story.facts,
        oracle.story.events,
        oracle.story.memories,
        oracle.story.head,
    )
    assert story == expected_story
    assert all(expected[t] for t in ("events", "facts", "memories", "placement"))
    assert differences(found, expected) == []
    return expected


async def story_counters(db: AsyncEngine, story_id: str) -> tuple[int, int]:
    async with db.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT timeline_revision, next_turn_index FROM stories WHERE id = :s"),
                {"s": story_id},
            )
        ).one()
    return int(row.timeline_revision), int(row.next_turn_index)


# -- the tests ---------------------------------------------------------------------------------


async def test_the_bulk_build_matches_the_commit_path(pg_url: str) -> None:
    """``ScaleSpec.scaled(1_000)`` built in bulk and by commit: the same rows in every table
    the scale suites read and every table the bulk builder derives in one pass."""
    await both_ways(pg_url, scale.ScaleSpec.scaled(1_000))


@contextmanager
def overlapping_canon(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The canon plan with, after each ``located_at``, the subject at another place over the
    second half of its window and on into the gap (closing the first and keeping a remainder of
    it), the same window again at the same place (closing and rewriting it), and each ``knows``
    twice (the second already current: no version, no event)."""
    plan_of = scale.canon_plan

    def plan(spec: scale.ScaleSpec) -> list[scale.Planned]:
        out: list[scale.Planned] = []
        places = sorted({p[2] for p in plan_of(spec) if p[0] == scale.LOCATED_AT})
        for planned in plan_of(spec):
            predicate, subject, obj, lo, hi = planned
            out.append(planned)
            if predicate == scale.LOCATED_AT and lo is not None and hi is not None:
                other = places[(places.index(obj) + 1) % len(places)]
                out.append((predicate, subject, other, (lo + hi) // 2, hi + 3))
                out.append((predicate, subject, other, (lo + hi) // 2, hi + 3))
            elif predicate == scale.KNOWS:
                out.append(planned)
        return out

    monkeypatch.setattr(scale, "canon_plan", plan)
    yield


async def test_the_bulk_build_supersedes_as_commit_fact_does(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small spec whose canon closes, trims and rewrites versions (``overlapping_canon``):
    the bulk builder's supersede chains, remainders, closing markers, events and change index
    are the commit path's."""
    with overlapping_canon(monkeypatch):
        expected = await both_ways(pg_url, scale.ScaleSpec.scaled(60, characters=3, dimension=8))
    versions = [dict(row) for row in expected["facts"].elements()]
    closed = [v for v in versions if v["closed_by"]]
    assert closed
    assert all(v["superseded_by"] is not None for v in closed)
    assert any(len(v["closed_by"]) > 1 for v in closed)  # a new version and a remainder
    assert any(v["lineage_id"] != v["id"] and v["supersedes"] for v in versions)  # a remainder
