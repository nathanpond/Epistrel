"""Helpers for the fork tests (#67): fork through the core, every row a story owns, the ids it
owns, and an id-free picture of a story's content to compare a fork with its source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import fork as forking
from epistrel.core.stories import STORY_ID_EXCEPTIONS
from epistrel.core.variants import ChainRef
from epistrel.core.views import corrected_history
from epistrel.testing.normalize import Normalizer, history

FORK_PRINCIPAL = "p-forker"


async def fork(
    engine: AsyncEngine,
    story_id: str,
    at_turn: int | None = None,
    variant_chain: ChainRef | None = None,
    **fields: Any,
) -> forking.Forked:
    return await forking.fork(
        engine,
        story_id,
        forking.ForkRequest(at_turn=at_turn, variant_chain=variant_chain, **fields),
        principal=FORK_PRINCIPAL,
    )


async def owned_rows(engine: AsyncEngine, story_id: str) -> dict[str, set[str]]:
    """Every row of ``story_id`` as text, per table: each table with a ``story_id`` column (from
    ``information_schema``, so nothing is missed), the story row, and its story-scoped
    ``config_changes``."""
    owned: dict[str, set[str]] = {}
    async with engine.connect() as conn:
        scoped = await conn.scalars(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'story_id'"
            )
        )
        for table in sorted(set(scoped) - STORY_ID_EXCEPTIONS):
            found = await conn.scalars(
                text(f'SELECT t::text FROM "{table}" t WHERE story_id = :s'),  # noqa: S608
                {"s": story_id},
            )
            owned[table] = set(found)
        owned["stories"] = set(
            await conn.scalars(text("SELECT t::text FROM stories t WHERE id = :s"), {"s": story_id})
        )
        owned["config_changes"] = set(
            await conn.scalars(
                text(
                    "SELECT t::text FROM config_changes t WHERE scope = 'story' AND scope_id = :s"
                ),
                {"s": story_id},
            )
        )
    return owned


async def owned_ids(engine: AsyncEngine, story_id: str) -> set[str]:
    """The ids of the rows ``story_id`` owns: the story, its operations, events, characters,
    policies and configuration changes."""
    async with engine.connect() as conn:
        found = await conn.scalars(
            text(
                "SELECT id::text FROM stories WHERE id = :s "
                "UNION SELECT operation_id::text FROM operations WHERE story_id = :s "
                "UNION SELECT id::text FROM events WHERE story_id = :s "
                "UNION SELECT id::text FROM characters WHERE story_id = :s "
                "UNION SELECT id FROM policies WHERE story_id = :s "
                "UNION SELECT change_id::text FROM config_changes "
                "WHERE scope = 'story' AND scope_id = :s"
            ),
            {"s": story_id},
        )
        return set(found)


@dataclass(frozen=True)
class Content:
    """A story's content without ids: its corrected history (minus ``skip_operation``'s events),
    its pinned memories and instructions, its head, settings and cast."""

    history: list[Any]
    texts: Any
    heads: Any
    settings: Any
    cast: Any
    next_turn_index: int


async def content(
    engine: AsyncEngine,
    story_id: str,
    through_turn: int | None = None,
    *,
    skip_operation: str | None = None,
) -> Content:
    normalize = Normalizer()
    events = [
        e
        for e in await corrected_history(engine, story_id, through_turn)
        if e.operation_id != skip_operation
    ]
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT s.settings, s.next_turn_index, s.turn_concurrency, "
                    "(SELECT to_jsonb(x) - 'story_id' FROM story_texts x WHERE x.story_id = s.id) "
                    "AS texts, "
                    "(SELECT to_jsonb(h) - 'story_id' FROM story_heads h WHERE h.story_id = s.id) "
                    "AS heads, "
                    "(SELECT jsonb_agg(to_jsonb(c) - 'story_id' - 'created_at' "
                    "ORDER BY c.ordinal) FROM characters c WHERE c.story_id = s.id) AS cast "
                    "FROM stories s WHERE s.id = :s"
                ),
                {"s": story_id},
            )
        ).one()
    found = history(events, normalize)
    return Content(
        history=found,
        texts=normalize(row.texts),
        heads=normalize(row.heads),
        settings=normalize({**row.settings, "turn_concurrency": row.turn_concurrency}),
        cast=normalize(row.cast),
        next_turn_index=int(row.next_turn_index),
    )


def mentions(rows: dict[str, set[str]], ids: set[str]) -> dict[str, list[str]]:
    """The rows (per table) whose text names any of ``ids``."""
    found: dict[str, list[str]] = {}
    for table, texts in rows.items():
        hits = [t for t in texts if any(i in t for i in ids)]
        if hits:
            found[table] = hits
    return found
