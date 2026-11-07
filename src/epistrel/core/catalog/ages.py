"""The image safety path's canonical-age reader (FR-IMG-12, FR-CAT-12; #257).

- ``true_age_at(conn, story_id, character_id, position)``: a story character's **current** true
  age at a story position, ``characters.true_age`` plus the full years of story time
  (``core.world_clock.full_years``) elapsed from the character's creation position (genesis, or
  ``modeled_at`` for a lazily modeled character, ADR-171) to ``position``
  (``core.world_clock.story_time_at``; a slot position included). A story that runs the clock
  off ages nobody. At the story's head this is the same rule as
  ``orchestrator.policies.age.current_age``, which Core cannot import; a unit test asserts the
  two agree.
- ``true_ages_at(conn, story_id, position)``: the same for every character of the story, read
  once.
- ``catalog_true_age(document)``: a catalog character's true age as its draft or version
  writes it (``definition.true_age``, through ``checks.character_documents.true_age_of``): a
  catalog entry has no clock.

Other readers of ``true_age`` stay. What keeps ages out of the image generation path is the
import-linter contract ``images-generation-path-has-no-ages`` (``orchestrator.images.adapters``
and ``orchestrator.images.port`` import neither this module nor ``core.images.safety``), not
this module's uniqueness.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.checks.character_documents import true_age_of
from epistrel.core.membership import POSITION_FIELDS, Position
from epistrel.core.tables import characters


def catalog_true_age(document: Mapping[str, Any] | None) -> int | None:
    """A catalog character's true age as written (``definition.true_age``), or ``None``."""
    return true_age_of(document)


async def true_ages_at(conn: AsyncConnection, story_id: str, position: Position) -> dict[str, int]:
    """Every story character's current true age at ``position``, by character id (see the
    module docstring)."""
    from epistrel.core import world_clock  # here: the clock reads States, which import Core

    rows = (
        await conn.execute(
            select(
                characters.c.id,
                characters.c.true_age,
                *(characters.c[f"modeled_{n}"] for n in POSITION_FIELDS),
            ).where(characters.c.story_id == story_id)
        )
    ).all()
    if not rows:
        return {}
    if await world_clock.clock_level(conn, story_id) is None:
        return {str(row.id): int(row.true_age) for row in rows}
    basis = await world_clock.time_basis(conn, story_id)
    now = await world_clock.story_time_at(conn, story_id, position)
    found: dict[str, int] = {}
    for row in rows:
        created = Position.of_columns(dict(row._mapping), "modeled") or Position(0)
        elapsed = max(0, now - basis.through(created))
        found[str(row.id)] = int(row.true_age) + world_clock.full_years(elapsed)
    return found


async def true_age_at(
    conn: AsyncConnection, story_id: str, character_id: str, position: Position
) -> int | None:
    """``character_id``'s current true age at ``position`` (``None``: no such character in the
    story)."""
    return (await true_ages_at(conn, story_id, position)).get(character_id)


__all__ = ["catalog_true_age", "true_age_at", "true_ages_at"]
