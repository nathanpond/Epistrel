"""I-08: migrations (docs/04 §4; NFR-REL-2). Forward/backward integrity: every revision
downgrades and upgrades again to the same schema, with story data in place surviving a
round trip of the revisions above it; and a projection rebuild after a schema change restores
exactly the state the incremental path had built.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import projections
from epistrel.core.db import make_engine, make_sync_engine
from tests.auth_support import auth_settings
from tests.concurrency_support import set_instructions, set_pins, summarise
from tests.conftest import alembic_config
from tests.event_support import new_story, play_turn
from tests.projection_support import Snapshot, snapshot

SCHEMA = {
    # Both schemas the engine's objects live in: ``public`` and ``catalog`` (#103).
    "schemas": "SELECT nspname FROM pg_namespace WHERE nspname IN ('public', 'catalog')",
    "columns": (
        "SELECT table_schema || '.' || table_name, column_name, data_type, udt_name, "
        "is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema IN ('public', 'catalog')"
    ),
    "indexes": (
        "SELECT schemaname || '.' || tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname IN ('public', 'catalog')"
    ),
    "constraints": (
        "SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) "
        "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname IN ('public', 'catalog')"
    ),
    "triggers": (
        "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) FROM pg_trigger "
        "WHERE NOT tgisinternal"
    ),
    "functions": (
        "SELECT n.nspname || '.' || p.proname, "
        "md5(regexp_replace(p.prosrc, '\\s+', ' ', 'g')) FROM pg_proc p "
        "JOIN pg_namespace n "
        "ON n.oid = p.pronamespace WHERE n.nspname IN ('public', 'catalog')"
    ),
    "enums": (
        "SELECT n.nspname || '.' || t.typname, e.enumlabel, e.enumsortorder FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid JOIN pg_namespace n ON n.oid = t.typnamespace"
    ),
    "extensions": "SELECT extname FROM pg_extension",
}


KEPT_ON_DOWNGRADE = {("public.operation_status", "queued")}
"""Enum labels a downgrade cannot remove (PostgreSQL has no ``DROP VALUE``): 0015 adds
``queued`` and documents that its downgrade leaves it, after failing any queued operation."""


def _same(up: dict[str, list[tuple[Any, ...]]], down: dict[str, list[tuple[Any, ...]]]) -> None:
    """``up`` equals ``down`` except for the enum labels in ``KEPT_ON_DOWNGRADE``."""
    assert {a: v for a, v in up.items() if a != "enums"} == {
        a: v for a, v in down.items() if a != "enums"
    }
    labels = {(t, label) for t, label, _ in up["enums"]}
    kept = {(t, label) for t, label, _ in down["enums"]} - labels
    assert kept <= KEPT_ON_DOWNGRADE
    assert labels <= {(t, label) for t, label, _ in down["enums"]}


def _schema(url: str) -> dict[str, list[tuple[Any, ...]]]:
    """The database's schema as sorted rows per aspect (no data), without Alembic's own
    ``alembic_version`` bookkeeping table."""
    engine = make_sync_engine(url)
    try:
        with engine.connect() as conn:
            return {
                aspect: sorted(
                    tuple(map(str, r))
                    for r in conn.execute(text(query))
                    if "alembic_version" not in str(r[0])
                )
                for aspect, query in SCHEMA.items()
            }
    finally:
        engine.dispose()


def _revisions(url: str) -> list[str]:
    """Every revision id, base first."""
    script = ScriptDirectory.from_config(alembic_config(url))
    return [r.revision for r in reversed(list(script.walk_revisions()))]


@pytest.mark.part("M2", "NFR-REL-2")
def test_i_08__round_trip(scratch_database: str) -> None:
    """From head, step down one revision at a time to base and back up again: at every revision
    the schema on the way up equals the schema on the way down (function bodies compared with
    whitespace collapsed; only the documented ``KEPT_ON_DOWNGRADE`` enum labels may linger on
    the way down), and head is reached again. Every schema the engine owns is compared
    (``SCHEMA``).

    Complement asserted: base is empty and head differs from the first revision, so the equal
    fingerprints are not vacuous."""
    cfg = alembic_config(scratch_database)
    revisions = _revisions(scratch_database)
    command.upgrade(cfg, "head")
    down: dict[str, dict[str, list[tuple[Any, ...]]]] = {revisions[-1]: _schema(scratch_database)}
    for target in reversed(revisions[:-1]):
        command.downgrade(cfg, target)
        down[target] = _schema(scratch_database)
    command.downgrade(cfg, "base")
    empty = _schema(scratch_database)
    assert not any(empty[a] for a in ("columns", "indexes", "constraints", "triggers"))
    assert empty["schemas"] == [("public",)]  # the downgrade removed every other schema
    for target in revisions:
        command.upgrade(cfg, target)
        _same(_schema(scratch_database), down[target])
    assert down[revisions[-1]] != down[revisions[0]]


async def _seed(url: str) -> tuple[str, Snapshot, list[tuple[Any, ...]]]:
    """A story with turns, pins, instructions and summary segments: its id, its projections as
    built incrementally, and its log (operations and events as text)."""
    engine = make_engine(auth_settings(url))
    try:
        story = await new_story(engine)
        for n in range(1, 6):
            await play_turn(engine, story, f"turn {n}")
        await set_pins(engine, story, "the key is under the mat")
        await set_instructions(engine, story, "keep it short")
        await summarise(engine, story, 2)
        await summarise(engine, story, 4)
        return story, await snapshot(engine, story), await _log(engine, story)
    finally:
        await engine.dispose()


async def _log(engine: AsyncEngine, story: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT 'o', o::text FROM operations o WHERE story_id = :s "
                "UNION ALL SELECT 'e', e::text FROM events e WHERE story_id = :s ORDER BY 1, 2"
            ),
            {"s": story},
        )
        return [tuple(r) for r in found]


async def _rebuilt(url: str, story: str) -> tuple[Snapshot, Snapshot, list[tuple[Any, ...]]]:
    """The projections as the schema change left them, then after a rebuild; and the log."""
    engine = make_engine(auth_settings(url))
    try:
        before = await snapshot(engine, story)
        log = await _log(engine, story)
        await projections.rebuild(engine, story)
        return before, await snapshot(engine, story), log
    finally:
        await engine.dispose()


@pytest.mark.part("M2", "NFR-REL-2")
def test_i_08__rebuild(scratch_database: str) -> None:
    """With a story in place, a schema change to a projection (downgrading below
    ``0019_lite_summary`` drops the segment table; upgrading recreates it empty) leaves the
    event log intact, and a projection rebuild restores exactly the projections the incremental
    path had built.

    Complement asserted: straight after the schema change the segment projection is empty, so
    the rebuild is what restored it."""
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "head")
    story, built, log = asyncio.run(_seed(scratch_database))
    assert built.segments, "the seed must produce summary segments"

    command.downgrade(cfg, "0018")
    command.upgrade(cfg, "head")
    changed, rebuilt, log_after = asyncio.run(_rebuilt(scratch_database, story))
    assert log_after[: len(log)] == log  # the rebuild may only add its own maintenance rows
    assert changed.segments == ()
    assert (changed.transcript, changed.heads, changed.texts) == (
        built.transcript,
        built.heads,
        built.texts,
    )
    assert rebuilt == built
