"""The catalog characters a scenario's ``locked`` and ``default`` roles are filled with, read once
per validation (#111). Not a check: the role-character, eligibility, media, rating and
personality checks share it.

A role names ``character {catalog_id, version?}``. The version used is the pinned one, or, when
the role leaves it out, the character's latest published version (what story creation takes).
Everything is read through the validation's connection, so a publish sees what its own
transaction wrote (#116's paired publish validates its companion against the character version
it has just inserted).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, tuple_

from epistrel.core.catalog.checks.base import Subject, ValidationContext
from epistrel.core.catalog.checks.character_documents import rating_id_of
from epistrel.core.catalog.checks.scenario import draft_of, filled_roles, role_field
from epistrel.core.catalog.scenario_models import ScenarioRole
from epistrel.core.tables import content_rating_versions
from epistrel.core.tables.catalog import character_catalog, character_versions

MEMO = "filled_roles"


@dataclass(frozen=True, slots=True)
class FilledRole:
    """A ``locked`` or ``default`` role and its character: the catalog row (``None`` when the
    character does not exist), the version row used (``None`` when there is none: the pinned
    version is missing, or nothing is published), and that version's content rating, by its
    stored version id (``None`` when the version recorded none: one published before a rating
    was required, #115) and the rating's id (the stored version's, else the one the document
    names)."""

    index: int
    role: ScenarioRole
    catalog_id: str
    pinned: int | None
    entry: Any | None
    version: Any | None
    rating_version_id: str | None
    rating_id: str | None

    @property
    def field(self) -> str:
        """``cast.roles[i].character``: where the role names its character."""
        return role_field(self.index, "character")

    @property
    def source(self) -> str:
        """``catalog_id@version`` of the version used (``catalog_id`` alone without one)."""
        if self.version is None:
            return self.catalog_id
        return f"{self.catalog_id}@{self.version.version}"

    @property
    def document(self) -> Mapping[str, Any] | None:
        """The version's content (``None`` without a version)."""
        if self.version is None or not isinstance(self.version.content, Mapping):
            return None
        content: Mapping[str, Any] = self.version.content
        return content


def _number(pinned: int | None, entry: Any | None) -> int | None:
    """The version a role uses: its pin, else the character's latest published version."""
    if pinned is not None:
        return pinned
    if entry is None or entry.current_version is None:
        return None
    return int(entry.current_version)


async def _entries(ctx: ValidationContext, ids: list[str]) -> dict[str, Any]:
    if not ids:
        return {}
    rows = await ctx.conn.execute(
        select(
            character_catalog.c.id,
            character_catalog.c.status,
            character_catalog.c.current_version,
        ).where(character_catalog.c.id.in_(ids))
    )
    return {str(row.id): row for row in rows}


async def _versions(ctx: ValidationContext, wanted: set[tuple[str, int]]) -> dict[Any, Any]:
    if not wanted:
        return {}
    rows = await ctx.conn.execute(
        select(
            character_versions.c.catalog_id,
            character_versions.c.version,
            character_versions.c.content,
            character_versions.c.content_rating_version_id,
        ).where(
            tuple_(character_versions.c.catalog_id, character_versions.c.version).in_(
                sorted(wanted)
            )
        )
    )
    return {(str(row.catalog_id), int(row.version)): row for row in rows}


async def _rating_heads(ctx: ValidationContext, version_ids: set[str]) -> dict[str, str]:
    if not version_ids:
        return {}
    rows = await ctx.conn.execute(
        select(content_rating_versions.c.version_id, content_rating_versions.c.rating_id).where(
            content_rating_versions.c.version_id.in_(sorted(version_ids))
        )
    )
    return {str(row.version_id): str(row.rating_id) for row in rows}


async def filled(subject: Subject, ctx: ValidationContext) -> list[FilledRole]:
    """Every ``locked`` and ``default`` role of the scenario with its character (see the module
    docstring), in role order; read once per validation."""
    found = subject.memo.get(MEMO)
    if isinstance(found, list):
        return found
    roles = list(filled_roles(draft_of(subject)))
    entries = await _entries(ctx, sorted({character.catalog_id for _, _, character in roles}))
    numbers = [
        _number(character.version, entries.get(character.catalog_id)) for _, _, character in roles
    ]
    wanted = {
        (character.catalog_id, number)
        for (_, _, character), number in zip(roles, numbers, strict=True)
        if number is not None and character.catalog_id in entries
    }
    versions = await _versions(ctx, wanted)
    heads = await _rating_heads(
        ctx,
        {
            str(row.content_rating_version_id)
            for row in versions.values()
            if row.content_rating_version_id is not None
        },
    )
    result: list[FilledRole] = []
    for (index, role, character), number in zip(roles, numbers, strict=True):
        row = None if number is None else versions.get((character.catalog_id, number))
        stored = None if row is None else row.content_rating_version_id
        rating_version = None if stored is None else str(stored)
        rating_id = None if rating_version is None else heads.get(rating_version)
        if rating_id is None and row is not None and isinstance(row.content, Mapping):
            rating_id = rating_id_of(row.content)
        result.append(
            FilledRole(
                index=index,
                role=role,
                catalog_id=character.catalog_id,
                pinned=character.version,
                entry=entries.get(character.catalog_id),
                version=row,
                rating_version_id=rating_version,
                rating_id=rating_id,
            )
        )
    subject.memo[MEMO] = result
    return result


__all__ = ["FilledRole", "filled"]
