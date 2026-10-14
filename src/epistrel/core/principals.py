"""Principal lookup: every entity linked to a principal, by type, owned and related
(FR-AUTH-7; docs/02 §4.10; #117).

A principal is an opaque string, never resolved (FR-AUTH-4), so a lookup of a principal that
nothing names answers empty lists, never 404. ``system`` is looked up like any other principal:
it owns the presets.

- **Owned** (``owner_principal``): stories, catalog characters, scenarios, personas, media
  assets and engine profiles. Each type is ``{count, items, next_cursor}``. An item is
  ``{id, name, status, version, listing_status}`` (``null`` where the type has none), and a
  character's item also names its companion (``companion: {id}``). Companions are left out of
  ``scenarios``, items and count alike, because a companion follows its character (#116).
  Archived stories and entries, retired media and deleted profiles are listed with their
  status. A media ``candidate`` is private (FR-IMG-7) and is not listed.
- **Related** (``entity_relations``): for each type, the entities with at least one relation
  naming the principal, ``{id, relations: [names]}``, newest relation first. An entity both
  owned and related is listed in both.

Pages are keyset pages, newest first: on ``(updated_at, id)``, or ``(created_at, id)`` for the
tables without ``updated_at`` (stories, media), and on the newest relation's ``created_at`` for
related entities. One ``limit`` (1 to 200, default 50) applies to every type, and each type has
its own cursor. The cursor uses #110's encoding (``core.catalog.search``), bound to the
principal, the section and the type, so a cursor sent for another type or principal is 422
``cursor_invalid``. This module and ``core.ownership`` are the only readers of
``entity_relations`` (``tests/test_ownership_rules.py``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Text, case, cast, func, literal, null, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.expression import ColumnElement, Select

from epistrel.core.catalog.search import Position, decode_cursor, encode_cursor
from epistrel.core.context import principal_problem
from epistrel.core.errors import CursorInvalid, InvalidRequest
from epistrel.core.tables import (
    character_catalog,
    engine_profiles,
    entity_relations,
    media_assets,
    personas,
    scenario_catalog,
    stories,
)
from epistrel.core.tables.media import MediaState
from epistrel.core.tables.ownership import EntityType

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
CURSOR_MAX = 256
LISTED_MAX = 1000
"""How many ids a report's ``remaining`` lists per type (its counts are exact)."""

TYPES: dict[str, EntityType] = {
    "stories": EntityType.STORY,
    "characters": EntityType.CHARACTER,
    "scenarios": EntityType.SCENARIO,
    "personas": EntityType.PERSONA,
    "media": EntityType.MEDIA,
    "engine_profiles": EntityType.ENGINE_PROFILE,
}
"""The lookup's type keys, in their fixed order, and the entity type each names."""
TYPE_KEYS: dict[EntityType, str] = {value: key for key, value in TYPES.items()}
RELATED_PREFIX = "related."
"""A related page's cursor key: ``related.<type>`` (an owned page's is ``<type>``)."""
CURSOR_KEYS = (*TYPES, *(f"{RELATED_PREFIX}{key}" for key in TYPES))


# -- results ----------------------------------------------------------------------------------


class CompanionRef(BaseModel):
    """A character's companion scenario (#116), which follows it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str


class OwnedItem(BaseModel):
    """One entity the principal owns: its id, name, status, latest version and listing status
    (``null`` where its type has none), and a character's companion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str | None = Field(
        description="The story's or profile's name, a catalog entry's draft card name, a "
        "persona's name, or a media asset's caption (else its alt text)."
    )
    status: str | None = Field(
        description="A story's or catalog entry's status, a media asset's state (`live`, "
        "`retired`), a profile's `active` or `deleted`; `null` for a persona."
    )
    version: int | None = Field(description="The latest published version, where versioned.")
    listing_status: str | None = Field(description="A catalog entry's listing status.")
    companion: CompanionRef | None = Field(
        default=None, description="A character's companion scenario, which follows it."
    )


class OwnedPage(BaseModel):
    """The principal's entities of one type: how many there are, a page of them newest first,
    and the cursor of the next page (``null`` on the last)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    items: list[OwnedItem]
    next_cursor: str | None


class RelatedItem(BaseModel):
    """One entity with relations naming the principal, and those relations' names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    relations: list[str]


class RelatedPage(BaseModel):
    """The entities of one type related to the principal: their count, a page newest relation
    first, and the next page's cursor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    items: list[RelatedItem]
    next_cursor: str | None


class OwnedEntities(BaseModel):
    """What the principal owns, by type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stories: OwnedPage
    characters: OwnedPage
    scenarios: OwnedPage
    personas: OwnedPage
    media: OwnedPage
    engine_profiles: OwnedPage


class RelatedEntities(BaseModel):
    """What the principal is related to, by type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stories: RelatedPage
    characters: RelatedPage
    scenarios: RelatedPage
    personas: RelatedPage
    media: RelatedPage
    engine_profiles: RelatedPage


class PrincipalLookup(BaseModel):
    """Every entity linked to ``principal`` (FR-AUTH-7), owned and related, by type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: str
    owned: OwnedEntities
    related: RelatedEntities


# -- the queries --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Owned:
    """How one type's owned page is read: its table, keyset time, columns, and the rows it
    leaves out."""

    table: Any
    at: ColumnElement[Any]
    name: ColumnElement[Any]
    status: ColumnElement[Any]
    version: ColumnElement[Any]
    listing_status: ColumnElement[Any]
    companion: ColumnElement[Any]
    listed: ColumnElement[bool] | None = None
    live: ColumnElement[bool] | None = None
    """What the report's ``remaining`` counts: ``None`` for every row (hard deletes)."""


def _card_name(table: Any) -> ColumnElement[Any]:
    name: ColumnElement[Any] = table.c.draft["public_card"]["name"].astext
    return name


def _companion_of(character_id: ColumnElement[Any]) -> ColumnElement[Any]:
    return (
        select(scenario_catalog.c.id)
        .where(scenario_catalog.c.companion_of == character_id)
        .limit(1)
        .scalar_subquery()
    )


_NONE: ColumnElement[Any] = null()

OWNED: dict[EntityType, _Owned] = {
    EntityType.STORY: _Owned(
        stories,
        stories.c.created_at,
        stories.c.name,
        stories.c.status,
        _NONE,
        _NONE,
        _NONE,
    ),
    EntityType.CHARACTER: _Owned(
        character_catalog,
        character_catalog.c.updated_at,
        _card_name(character_catalog),
        cast(character_catalog.c.status, Text),
        character_catalog.c.current_version,
        character_catalog.c.listing_status,
        _companion_of(character_catalog.c.id),
    ),
    EntityType.SCENARIO: _Owned(
        scenario_catalog,
        scenario_catalog.c.updated_at,
        _card_name(scenario_catalog),
        cast(scenario_catalog.c.status, Text),
        scenario_catalog.c.current_version,
        scenario_catalog.c.listing_status,
        _NONE,
        listed=scenario_catalog.c.companion_of.is_(None),
    ),
    EntityType.PERSONA: _Owned(
        personas, personas.c.updated_at, personas.c.name, _NONE, _NONE, _NONE, _NONE
    ),
    EntityType.MEDIA: _Owned(
        media_assets,
        media_assets.c.created_at,
        func.coalesce(media_assets.c.caption, media_assets.c.alt_text),
        media_assets.c.state,
        _NONE,
        _NONE,
        _NONE,
        listed=media_assets.c.state != MediaState.CANDIDATE.value,
        live=media_assets.c.state == MediaState.LIVE.value,
    ),
    EntityType.ENGINE_PROFILE: _Owned(
        engine_profiles,
        engine_profiles.c.updated_at,
        engine_profiles.c.name,
        case((engine_profiles.c.deleted_at.is_(None), literal("active")), else_=literal("deleted")),
        engine_profiles.c.current_version,
        _NONE,
        _NONE,
        live=engine_profiles.c.deleted_at.is_(None),
    ),
}


def _owned_where(spec: _Owned, principal: str, *, live: bool = False) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = [spec.table.c.owner_principal == principal]
    if spec.listed is not None:
        where.append(spec.listed)
    if live and spec.live is not None:
        where.append(spec.live)
    return where


def _related(entity_type: EntityType, principal: str) -> Any:
    """One row per entity of ``entity_type`` with a relation naming ``principal``: its id, the
    newest such relation's time, and the relations' names (byte order)."""
    return (
        select(
            entity_relations.c.entity_id.label("id"),
            func.max(entity_relations.c.created_at).label("at"),
            func.array_agg(entity_relations.c.relation).label("relations"),
        )
        .where(
            entity_relations.c.entity_type == entity_type.value,
            entity_relations.c.principal == principal,
        )
        .group_by(entity_relations.c.entity_id)
        .subquery()
    )


def fingerprint(principal: str, key: str) -> str:
    """What a page's cursor binds: the principal, the section and the type."""
    canonical = json.dumps({"principal": principal, "page": key}, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _position(principal: str, key: str, cursor: str | None) -> Position | None:
    if cursor is None:
        return None
    field = f"query.cursor[{key}]"
    if len(cursor) > CURSOR_MAX:
        raise InvalidRequest(f"cursor is at most {CURSOR_MAX} characters", {"field": field})
    try:
        return decode_cursor(cursor, fingerprint(principal, key), ranked=False)
    except CursorInvalid as exc:
        raise CursorInvalid(exc.message, {**exc.details, "field": field}) from None


def _next(principal: str, key: str, rows: list[Any], limit: int) -> str | None:
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return encode_cursor(Position(None, last.at, str(last.id)), fingerprint(principal, key))


async def _owned_page(
    conn: AsyncConnection, entity_type: EntityType, principal: str, limit: int, cursor: str | None
) -> OwnedPage:
    key = TYPE_KEYS[entity_type]
    spec = OWNED[entity_type]
    where = _owned_where(spec, principal)
    count = int(await conn.scalar(select(func.count()).select_from(spec.table).where(*where)) or 0)
    position = _position(principal, key, cursor)
    if position is not None:
        where.append(tuple_(spec.at, spec.table.c.id) < tuple_(position.updated_at, position.id))
    query: Select[Any] = (
        select(
            spec.table.c.id,
            spec.at.label("at"),
            spec.name.label("name"),
            spec.status.label("status"),
            spec.version.label("version"),
            spec.listing_status.label("listing_status"),
            spec.companion.label("companion"),
        )
        .where(*where)
        .order_by(spec.at.desc(), spec.table.c.id.desc())
        .limit(limit + 1)
    )
    rows = list((await conn.execute(query)).all())
    items = [
        OwnedItem(
            id=str(row.id),
            name=row.name,
            status=None if row.status is None else str(row.status),
            version=row.version,
            listing_status=row.listing_status,
            companion=None if row.companion is None else CompanionRef(id=str(row.companion)),
        )
        for row in rows[:limit]
    ]
    return OwnedPage(count=count, items=items, next_cursor=_next(principal, key, rows, limit))


async def _related_page(
    conn: AsyncConnection, entity_type: EntityType, principal: str, limit: int, cursor: str | None
) -> RelatedPage:
    key = f"{RELATED_PREFIX}{TYPE_KEYS[entity_type]}"
    grouped = _related(entity_type, principal)
    count = int(await conn.scalar(select(func.count()).select_from(grouped)) or 0)
    query = select(grouped.c.id, grouped.c.at, grouped.c.relations)
    position = _position(principal, key, cursor)
    if position is not None:
        query = query.where(
            tuple_(grouped.c.at, grouped.c.id) < tuple_(position.updated_at, position.id)
        )
    rows = list(
        (
            await conn.execute(
                query.order_by(grouped.c.at.desc(), grouped.c.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    items = [
        RelatedItem(id=str(row.id), relations=sorted(row.relations, key=lambda n: n.encode()))
        for row in rows[:limit]
    ]
    return RelatedPage(count=count, items=items, next_cursor=_next(principal, key, rows, limit))


def lookup_principal_id(value: str, *, field: str = "path.principal_id") -> str:
    """A principal a lookup names: trimmed of spaces and tabs, 1 to 256 characters, no control
    character (``system`` allowed); 422 at ``field``."""
    found = value.strip(" \t")
    problem = principal_problem(found)
    if problem is not None:
        raise InvalidRequest(f"principal {problem}", {"field": field})
    return found


async def lookup(
    db: AsyncEngine,
    principal: str,
    *,
    limit: int = DEFAULT_LIMIT,
    cursors: Mapping[str, str] | None = None,
) -> PrincipalLookup:
    """Every entity linked to ``principal``, owned and related, a page of each type (see the
    module docstring). ``cursors`` maps a page's key (``stories``, ``related.stories``, …) to
    the cursor its previous page gave."""
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be 1 to {MAX_LIMIT}", {"field": "query.limit"})
    given = dict(cursors or {})
    unknown = sorted(set(given) - set(CURSOR_KEYS))
    if unknown:
        raise InvalidRequest(
            f"no page is named {unknown[0]!r}; one of: {', '.join(CURSOR_KEYS)}",
            {"field": f"query.cursor[{unknown[0]}]"},
        )
    async with db.connect() as conn, conn.begin():
        await conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        owned = {
            key: await _owned_page(conn, entity_type, principal, limit, given.get(key))
            for key, entity_type in TYPES.items()
        }
        related = {
            key: await _related_page(
                conn, entity_type, principal, limit, given.get(f"{RELATED_PREFIX}{key}")
            )
            for key, entity_type in TYPES.items()
        }
    return PrincipalLookup(
        principal=principal,
        owned=OwnedEntities(**owned),
        related=RelatedEntities(**related),
    )


# -- what an offboarding reads (#117) -------------------------------------------------------------


async def related_entities(
    conn: AsyncConnection, principal: str
) -> list[tuple[EntityType, str, str]]:
    """``(type, id, owner)`` of every entity with a relation naming ``principal``, by type in
    the lookup's order, then id."""
    found: list[tuple[EntityType, str, str]] = []
    for entity_type in TYPES.values():
        table = OWNED[entity_type].table
        rows = await conn.execute(
            select(entity_relations.c.entity_id, table.c.owner_principal)
            .join(table, table.c.id == entity_relations.c.entity_id)
            .where(
                entity_relations.c.entity_type == entity_type.value,
                entity_relations.c.principal == principal,
            )
            .distinct()
            .order_by(entity_relations.c.entity_id)
        )
        found.extend((entity_type, str(row.entity_id), str(row.owner_principal)) for row in rows)
    return found


async def related_principals(
    conn: AsyncConnection, entity_type: EntityType, entity_ids: Sequence[str], *, besides: str
) -> dict[str, set[str]]:
    """For each of ``entity_ids`` that has any, the principals other than ``besides`` its
    relations name (a shared story's other players)."""
    if not entity_ids:
        return {}
    rows = await conn.execute(
        select(entity_relations.c.entity_id, entity_relations.c.principal)
        .where(
            entity_relations.c.entity_type == entity_type.value,
            entity_relations.c.entity_id.in_(list(entity_ids)),
            entity_relations.c.principal != besides,
        )
        .distinct()
    )
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(str(row.entity_id), set()).add(str(row.principal))
    return found


# -- what is still linked (an offboarding's report) ----------------------------------------------


class Remaining(BaseModel):
    """The live entities still owned by or related to a principal, by type: their count and
    up to ``LISTED_MAX`` ids. Retired media and deleted profiles are not counted: they are
    retired, kept only while something still uses them (FR-MEDIA-2, FR-PROF-1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owned: dict[str, list[str]]
    owned_counts: dict[str, int]
    related: dict[str, list[str]]
    related_counts: dict[str, int]


async def remaining(conn: AsyncConnection, principal: str) -> Remaining:
    """What is still linked to ``principal``: owned entities that are live (every story,
    catalog entry and persona; live media; undeleted profiles) and every related entity."""
    owned: dict[str, list[str]] = {}
    owned_counts: dict[str, int] = {}
    related: dict[str, list[str]] = {}
    related_counts: dict[str, int] = {}
    for key, entity_type in TYPES.items():
        spec = OWNED[entity_type]
        where = _owned_where(spec, principal, live=True)
        owned_counts[key] = int(
            await conn.scalar(select(func.count()).select_from(spec.table).where(*where)) or 0
        )
        owned[key] = [
            str(found)
            for found in await conn.scalars(
                select(spec.table.c.id).where(*where).order_by(spec.table.c.id).limit(LISTED_MAX)
            )
        ]
        grouped = _related(entity_type, principal)
        related_counts[key] = int(await conn.scalar(select(func.count()).select_from(grouped)) or 0)
        related[key] = [
            str(found)
            for found in await conn.scalars(
                select(grouped.c.id).order_by(grouped.c.id).limit(LISTED_MAX)
            )
        ]
    return Remaining(
        owned=owned, owned_counts=owned_counts, related=related, related_counts=related_counts
    )


__all__ = [
    "CURSOR_KEYS",
    "CURSOR_MAX",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "RELATED_PREFIX",
    "TYPES",
    "TYPE_KEYS",
    "CompanionRef",
    "OwnedEntities",
    "OwnedItem",
    "OwnedPage",
    "PrincipalLookup",
    "RelatedEntities",
    "RelatedItem",
    "RelatedPage",
    "Remaining",
    "lookup",
    "lookup_principal_id",
    "related_entities",
    "related_principals",
    "remaining",
]
