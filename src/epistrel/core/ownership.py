"""Ownership and relations: one owner and any number of relations per entity, recorded and
never interpreted (FR-AUTH-5,6,8; docs/02 §4.10; #104).

Every story, catalog character, scenario, persona, media asset and engine profile has exactly
one **owner** principal (``owner_principal``). It is set at creation: the requesting principal,
or the ``owner`` the request names, never ``system`` (``Owner``). It changes only by an explicit
**transfer**, here, or by #117's offboarding, which uses the same functions. An entity may also
carry any number of **relations**: consumer-named (principal, relation) pairs in
``entity_relations``. Every change is recorded in ``ownership_events`` with the acting
principal and the transaction time.

**Nothing gates on them** (FR-AUTH-8). Owners and relations exist for listing, attribution and
offboarding. No read, write or story creation consults them, and no code interprets a relation
name. The only code that reads ``entity_relations`` is this module (relation CRUD, the list
filters and ``drop_entity``) and principal lookup (#117). ``tests/test_ownership_rules.py`` scans
``src/`` to keep it that way.

**The registry** (``REGISTRY``) maps each type to its table, URL segment, path parameter and
companion rule. It includes tables that later stories create. A type whose table does not exist
yet is not delivered: calls on it answer 404, and the registry-generated tests leave it out until
its routes are mounted (``api.routes.ownership.mount_ownership_routes``). Columns are looked up
at runtime, so ``updated_at`` and ``companion_of`` are used where a table has them.

**Companions follow their character** (FR-AUTH-5). Transferring a character also transfers every
scenario whose ``companion_of`` names it, in the same transaction and with one event per entity.
A companion's owner and relations cannot be changed on their own (422
``companion_follows_character``). The rule takes effect when #108's and #116's tables have the
column.

**Writes.** Each write is one transaction under the entity's row lock: ``FOR UPDATE`` for a
transfer, ``FOR SHARE`` for a relation change. The transaction writes the ``ownership_events``
rows and one committed ``control`` operation by the acting principal (``core.control``), and binds
the call's ``Idempotency-Key`` to it. For a story the operation is the story's own, so it and
its key go when the story is deleted. A write that changes nothing (a transfer to the current
owner, or adding a relation that exists) records nothing. Writes are allowed whatever the
entity's state: an archived story or entry, or a story with a turn in flight. A transfer bumps
``updated_at`` where the table has one. Ownership is not story truth, so no story event is
written and no revision moves.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import CHAR, TIMESTAMP, Text, and_, column, delete, exists, insert, null, select
from sqlalchemy import table as table_clause
from sqlalchemy import text as sql_text
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.expression import ColumnElement, TableClause

from epistrel.core import clock, control
from epistrel.core.context import PRINCIPAL_PATTERN, SYSTEM_PRINCIPAL, principal_problem
from epistrel.core.errors import (
    CompanionFollowsCharacter,
    EntityNotFound,
    InvalidRequest,
    RelationNotFound,
)
from epistrel.core.ids import ULID_LENGTH, new_ulid
from epistrel.core.tables import deployment_state, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.ownership import (
    RELATION_NAME_PATTERN,
    EntityType,
    OwnershipEventKind,
    entity_relations,
    ownership_events,
)

# -- the registry -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Link:
    """A companion rule: the entities of ``entity_type`` whose ``column`` names another entity's
    id follow that entity's owner."""

    entity_type: EntityType
    column: str


@dataclass(frozen=True, slots=True)
class OwnedType:
    """One owned entity type: its table, the URL segment and path parameter its routes use
    (``/{segment}/{{{id_param}}}/owner``), a noun for messages, and its companion rule:
    ``companions`` follow it, and it ``follows`` another type when its link column is set."""

    entity_type: EntityType
    schema: str
    table: str
    segment: str
    id_param: str
    noun: str
    companions: tuple[Link, ...] = ()
    follows: Link | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


COMPANION_COLUMN = "companion_of"
"""The scenario column naming the character a companion belongs to (#116)."""

REGISTRY: dict[EntityType, OwnedType] = {
    EntityType.STORY: OwnedType(
        EntityType.STORY, "public", "stories", "stories", "story_id", "story"
    ),
    EntityType.CHARACTER: OwnedType(
        EntityType.CHARACTER,
        "catalog",
        "character_catalog",
        "catalog/characters",
        "character_id",
        "catalog character",
        companions=(Link(EntityType.SCENARIO, COMPANION_COLUMN),),
    ),
    EntityType.SCENARIO: OwnedType(
        EntityType.SCENARIO,
        "catalog",
        "scenario_catalog",
        "catalog/scenarios",
        "scenario_id",
        "scenario",
        follows=Link(EntityType.CHARACTER, COMPANION_COLUMN),
    ),
    EntityType.PERSONA: OwnedType(
        EntityType.PERSONA, "public", "personas", "personas", "persona_id", "persona"
    ),
    EntityType.MEDIA: OwnedType(
        EntityType.MEDIA, "public", "media_assets", "media", "media_id", "media asset"
    ),
    EntityType.ENGINE_PROFILE: OwnedType(
        EntityType.ENGINE_PROFILE,
        "public",
        "engine_profiles",
        "engine-profiles",
        "profile_id",
        "engine profile",
    ),
}
"""Every owned type (FR-AUTH-5): scenarios are #108's (companions #116's), personas #114's, media
#105's and engine profiles #106's. Their tables are skipped at runtime until they exist."""

SEGMENTS: dict[str, EntityType] = {owned.segment: t for t, owned in REGISTRY.items()}
"""URL segment → entity type."""

_COLUMN_TYPES: dict[str, Any] = {
    "id": CHAR(ULID_LENGTH),
    "owner_principal": Text(),
    "updated_at": TIMESTAMP(timezone=True),
    COMPANION_COLUMN: CHAR(ULID_LENGTH),
}


def entity_type_of(value: EntityType | str) -> EntityType:
    """``value`` as an ``EntityType``; 422 ``validation_error`` for an unknown one."""
    try:
        return EntityType(value)
    except ValueError:
        raise InvalidRequest(
            f"unknown entity type {value!r}; one of: {', '.join(t.value for t in EntityType)}",
            {"field": "entity_type"},
        ) from None


def _table(owned: OwnedType, names: Iterable[str]) -> TableClause:
    """A lightweight handle on ``owned``'s table with the columns ``names``."""
    return table_clause(
        owned.table, *(column(n, _COLUMN_TYPES[n]) for n in names), schema=owned.schema
    )


async def columns_of(conn: AsyncConnection, owned: OwnedType) -> frozenset[str]:
    """``owned``'s table's column names; empty when the table does not exist (yet)."""
    found = await conn.scalars(
        sql_text(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(CAST(:name AS text)) "
            "AND attnum > 0 AND NOT attisdropped"
        ),
        {"name": owned.qualified},
    )
    return frozenset(str(name) for name in found)


async def delivered(conn: AsyncConnection) -> list[EntityType]:
    """The owned types whose tables exist, in registry order."""
    return [t for t, owned in REGISTRY.items() if await columns_of(conn, owned)]


# -- request fields -----------------------------------------------------------------------------

RELATION_NAME = re.compile(RELATION_NAME_PATTERN)
RELATION_FIELD_PATTERN = r"^[\t ]*[A-Za-z0-9_-]{1,64}[\t ]*$"
"""A relation name as sent: trimmed of spaces and tabs, then ``RELATION_NAME_PATTERN``."""
RELATION_FILTER_PATTERN = r"^[A-Za-z0-9_-]{1,64}:[\t ]*([^\x00-\x1f\x7f]{1,256}[\t ]*)?$"
"""A ``relation`` list filter: ``name:principal``, or ``name:`` for any principal. It splits at
the first colon (a relation name holds none). The colon is required, so a bare word such as
``true`` or ``0`` is never a filter: a query string cannot tell it from a wrongly typed value."""

_RESERVED = f"must not be `{SYSTEM_PRINCIPAL}`, which is reserved for background work"


def principal_rule(label: str) -> Callable[[str], str]:
    """The principal rule for a field that names a principal (an owner, a related principal):
    spaces and tabs trimmed, 1 to 256 characters, no control character, never ``system``, as for
    ``X-Epistrel-Principal``. Returns the trimmed value; raises ``ValueError`` (a 422 at the
    field)."""

    def check(value: str) -> str:
        found = value.strip(" \t")
        problem = principal_problem(found)
        if problem is None and found == SYSTEM_PRINCIPAL:
            problem = _RESERVED
        if problem is not None:
            raise ValueError(f"{label} {problem}")
        return found

    return check


def relation_name_problem(value: str) -> str | None:
    """Why ``value`` (already trimmed) is not a relation name, or ``None``."""
    if RELATION_NAME.fullmatch(value):
        return None
    return "must be 1 to 64 of A-Z, a-z, 0-9, '_' or '-' (case-sensitive)"


def _trimmed(value: str) -> str:
    """The name without the spaces and tabs ``RELATION_FIELD_PATTERN`` lets surround it."""
    return value.strip(" \t")


Owner = Annotated[str, Field(pattern=PRINCIPAL_PATTERN), AfterValidator(principal_rule("owner"))]
"""An ``owner`` a creation names (FR-AUTH-5): the principal rule (``principal_rule``)."""

Principal = Annotated[
    str, Field(pattern=PRINCIPAL_PATTERN), AfterValidator(principal_rule("principal"))
]
"""A principal a transfer or a relation names: the principal rule (``principal_rule``)."""

RelationName = Annotated[str, Field(pattern=RELATION_FIELD_PATTERN), AfterValidator(_trimmed)]
"""A relation's name: free text the consumer chooses, trimmed, 1 to 64 of ``A-Z a-z 0-9 _ -``,
case-sensitive. Epistrel stores it and never interprets it (FR-AUTH-6)."""


class OwnerTransferRequest(BaseModel):
    """``PUT /{type}/{id}/owner``: the principal that becomes the entity's owner (opaque; never
    ``system``). A transfer to the current owner changes nothing. Ownership confers no permission
    (FR-AUTH-8)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"principal": "p-7f3a9c"}]}
    )

    principal: Principal = Field(description="The new owner principal.")


class RelationRequest(BaseModel):
    """``POST /{type}/{id}/relations``: a relation to add, the related ``principal`` and the
    ``relation``'s name (consumer-defined, e.g. ``editor``, ``co_author``, ``player``). Epistrel
    never interprets the name (FR-AUTH-6,8)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"principal": "p-7f3a9c", "relation": "editor"}]},
    )

    principal: Principal = Field(description="The related principal (opaque; never `system`).")
    relation: RelationName = Field(
        description="The relation's name: 1 to 64 of `A-Z a-z 0-9 _ -`, case-sensitive."
    )


def path_relation(value: str, *, field: str = "path.relation") -> str:
    """A relation name a call names outside a body (a removal); 422 at ``field``."""
    found = value.strip(" \t")
    problem = relation_name_problem(found)
    if problem is not None:
        raise InvalidRequest(f"relation {problem}", {"field": field})
    return found


def path_principal(value: str, *, field: str = "path.principal") -> str:
    """A related principal a call names outside a body; the principal rule, 422 at ``field``."""
    try:
        return principal_rule("principal")(value)
    except ValueError as exc:
        raise InvalidRequest(str(exc), {"field": field}) from None


# -- list filters -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationFilter:
    """``relation=name:principal``, or ``relation=name:`` for any principal (``None``)."""

    relation: str
    principal: str | None = None


RELATION_FILTER_FORM = "relation filter `name:principal` (or `name:` for any principal)"


def relation_filter_problem(value: str) -> str | None:
    """Why ``value`` is not a ``relation`` filter, or ``None``."""
    name, colon, who = value.partition(":")
    problem = relation_name_problem(name)
    if problem is not None:
        return f"{RELATION_FILTER_FORM}: the name {problem}"
    if not colon:
        return f"{RELATION_FILTER_FORM}: the colon is missing"
    found = who.strip(" \t")
    problem = principal_problem(found) if found else None
    if problem is not None:
        return f"{RELATION_FILTER_FORM}: the principal {problem}"
    return None


def parse_relation_filter(value: str, *, field: str = "query.relation") -> RelationFilter:
    """``name:principal``, or ``name:`` for any principal (split at the first colon; spaces and
    tabs around the principal are trimmed); 422 at ``field``."""
    problem = relation_filter_problem(value)
    if problem is not None:
        raise InvalidRequest(problem, {"field": field})
    name, _, who = value.partition(":")
    return RelationFilter(name, who.strip(" \t") or None)


def owner_filter(value: str | None, *, field: str = "query.owner") -> str | None:
    """An ``owner`` filter, trimmed of spaces and tabs (``system`` may be filtered on: presets
    and background work own entities); 422 at ``field`` for anything a principal cannot be."""
    if value is None:
        return None
    found = value.strip(" \t")
    problem = principal_problem(found)
    if problem is not None:
        raise InvalidRequest(f"owner {problem}", {"field": field})
    return found


def list_filters(
    entity_type: EntityType,
    entity_id: ColumnElement[Any],
    owner_column: ColumnElement[Any],
    *,
    owner: str | None = None,
    relations: Sequence[RelationFilter] = (),
) -> list[ColumnElement[bool]]:
    """The WHERE clauses of an ``owner``/``relation`` list filter over a query of
    ``entity_type`` (``entity_id`` and ``owner_column`` are its columns): one per filter, so
    repeated relations AND. Only a request that asks for a filter gets one (FR-AUTH-8): with
    neither, the list is everything."""
    clauses: list[ColumnElement[bool]] = []
    if owner is not None:
        clauses.append(owner_column == owner)
    for wanted in relations:
        match = and_(
            entity_relations.c.entity_type == entity_type.value,
            entity_relations.c.entity_id == entity_id,
            entity_relations.c.relation == wanted.relation,
        )
        if wanted.principal is not None:
            match = and_(match, entity_relations.c.principal == wanted.principal)
        clauses.append(exists().where(match))
    return clauses


# -- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnerTransfer:
    """A transfer's outcome: the owner now and before (equal when nothing changed). A story's
    ``timeline_revision``, or else the deployment's ``config_revision``; neither moved."""

    entity_type: EntityType
    entity_id: str
    owner_principal: str
    previous_owner: str
    changed: bool
    timeline_revision: int | None = None
    config_revision: int | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class Relation:
    """One relation: the related principal, the relation's name, and who added it when."""

    principal: str
    relation: str
    created_at: datetime
    created_by: str


@dataclass(frozen=True, slots=True)
class RelationWrite:
    """An added relation (``created``), or the one that already existed (nothing recorded)."""

    entity_type: EntityType
    entity_id: str
    relation: Relation
    created: bool
    timeline_revision: int | None = None
    config_revision: int | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class RelationRemoved:
    """A removed relation."""

    entity_type: EntityType
    entity_id: str
    principal: str
    relation: str
    timeline_revision: int | None = None
    config_revision: int | None = None
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class Relations:
    """An entity's owner and its relations, by relation name, then principal (byte order, so
    ``Editor`` comes before ``editor`` whatever the database's collation)."""

    entity_type: EntityType
    entity_id: str
    owner_principal: str
    items: tuple[Relation, ...]


# -- rows ---------------------------------------------------------------------------------------

Lock = Literal["update", "share"] | None


def _not_found(owned: OwnedType, entity_id: str) -> EntityNotFound:
    return EntityNotFound(
        f"{owned.noun} {entity_id} does not exist",
        {"entity_type": owned.entity_type.value, "entity_id": entity_id},
    )


async def _columns(conn: AsyncConnection, owned: OwnedType, entity_id: str) -> frozenset[str]:
    found = await columns_of(conn, owned)
    if not found:
        raise _not_found(owned, entity_id)
    return found


async def _entity(
    conn: AsyncConnection,
    owned: OwnedType,
    entity_id: str,
    columns: frozenset[str],
    *,
    lock: Lock = None,
) -> Any:
    """The entity's owner (and its companion link, where it has one), locked as ``lock``
    says; 404 when it does not exist."""
    names = ["id", "owner_principal"]
    if owned.follows is not None and owned.follows.column in columns:
        names.append(owned.follows.column)
    handle = _table(owned, names)
    query = select(*handle.c).where(handle.c.id == entity_id)
    if lock == "update":
        query = query.with_for_update()
    elif lock == "share":
        query = query.with_for_update(read=True)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise _not_found(owned, entity_id)
    return row


def _refuse_companion(owned: OwnedType, row: Any) -> None:
    """422 ``companion_follows_character`` when ``row`` is a companion."""
    if owned.follows is None:
        return
    parent = row._mapping.get(owned.follows.column)
    if parent is not None:
        raise CompanionFollowsCharacter(
            f"{owned.noun} {row.id} is the companion of catalog character {parent}: its owner "
            "and relations follow the character's; change them on the character",
            {
                "entity_type": owned.entity_type.value,
                "entity_id": str(row.id),
                "character_id": str(parent),
            },
        )


async def _companions(
    conn: AsyncConnection, owned: OwnedType, entity_id: str
) -> list[tuple[OwnedType, str, str, frozenset[str]]]:
    """``(type, id, owner, columns)`` of every entity that follows ``entity_id``, locked."""
    found: list[tuple[OwnedType, str, str, frozenset[str]]] = []
    for link in owned.companions:
        child = REGISTRY[link.entity_type]
        child_columns = await columns_of(conn, child)
        if link.column not in child_columns:
            continue
        handle = _table(child, ["id", "owner_principal", link.column])
        rows = await conn.execute(
            select(handle.c.id, handle.c.owner_principal)
            .where(handle.c[link.column] == entity_id)
            .order_by(handle.c.id)
            .with_for_update()
        )
        found.extend((child, str(r.id), str(r.owner_principal), child_columns) for r in rows)
    return found


async def _revisions(
    conn: AsyncConnection, owned: OwnedType, entity_id: str
) -> tuple[int | None, int | None]:
    """``(timeline_revision, config_revision)``: the story's own, or the deployment's."""
    if owned.entity_type is EntityType.STORY:
        timeline = await conn.scalar(
            select(stories.c.timeline_revision).where(stories.c.id == entity_id)
        )
        return int(timeline or 0), None
    config = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return None, int(config or 0)


def _story_of(owned: OwnedType, entity_id: str) -> str | None:
    """The story a write's ``control`` operation belongs to: the entity, when it is a story."""
    return entity_id if owned.entity_type is EntityType.STORY else None


async def record_event(
    conn: AsyncConnection,
    kind: OwnershipEventKind,
    entity_type: EntityType,
    entity_id: str,
    *,
    old: Any,
    new: Any,
    principal: str,
    job_id: str | None = None,
) -> None:
    """One ``ownership_events`` row in ``conn``'s transaction (``at``: the transaction's time).
    #110 records listing-status changes and #117 its job's changes through it."""
    await conn.execute(
        insert(ownership_events).values(
            id=new_ulid(),
            entity_type=entity_type.value,
            entity_id=entity_id,
            kind=kind.value,
            old=null() if old is None else old,
            new=null() if new is None else new,
            acting_principal=principal,
            job_id=job_id,
        )
    )


def _pair(principal: str, relation: str) -> dict[str, str]:
    return {"principal": principal, "relation": relation}


# -- writes -------------------------------------------------------------------------------------


async def transfer_owner(
    db: AsyncEngine,
    entity_type: EntityType,
    entity_id: str,
    new_owner: str,
    *,
    principal: str,
) -> OwnerTransfer:
    """Make ``new_owner`` the owner of the entity and of every entity that follows it, recorded
    as one ``transfer`` event per entity with ``principal`` as the actor. 404 for an unknown
    entity, 422 ``companion_follows_character`` for a companion. A transfer to the current owner
    changes and records nothing."""
    owned = REGISTRY[entity_type]
    async with db.begin() as conn:
        columns = await _columns(conn, owned, entity_id)
        row = await _entity(conn, owned, entity_id, columns, lock="update")
        _refuse_companion(owned, row)
        previous = str(row.owner_principal)
        changed = previous != new_owner
        if changed:
            moved = [(owned, entity_id, previous, columns)]
            moved += await _companions(conn, owned, entity_id)
            now = clock.now()
            for item, item_id, old, item_columns in moved:
                if old == new_owner:
                    continue
                values: dict[str, Any] = {"owner_principal": new_owner}
                if "updated_at" in item_columns:
                    values["updated_at"] = now
                handle = _table(item, ["id", *values])
                await conn.execute(
                    sql_update(handle).where(handle.c.id == item_id).values(**values)
                )
                await record_event(
                    conn,
                    OwnershipEventKind.TRANSFER,
                    item.entity_type,
                    item_id,
                    old=old,
                    new=new_owner,
                    principal=principal,
                )
            await control.record_control_operation(
                conn, actor=principal, story_id=_story_of(owned, entity_id)
            )
        timeline, config = await _revisions(conn, owned, entity_id)
    return OwnerTransfer(
        entity_type=entity_type,
        entity_id=entity_id,
        owner_principal=new_owner,
        previous_owner=previous,
        changed=changed,
        timeline_revision=timeline,
        config_revision=config,
    )


async def add_relation(
    db: AsyncEngine,
    entity_type: EntityType,
    entity_id: str,
    related: str,
    relation: str,
    *,
    principal: str,
) -> RelationWrite:
    """Relate ``related`` to the entity as ``relation`` (``created``); a relation that already
    exists is returned as it is and nothing is recorded. 404 for an unknown entity, 422
    ``companion_follows_character`` for a companion."""
    owned = REGISTRY[entity_type]
    async with db.begin() as conn:
        columns = await _columns(conn, owned, entity_id)
        _refuse_companion(owned, await _entity(conn, owned, entity_id, columns, lock="share"))
        key = and_(
            entity_relations.c.entity_type == entity_type.value,
            entity_relations.c.entity_id == entity_id,
            entity_relations.c.principal == related,
            entity_relations.c.relation == relation,
        )
        stored = (
            await conn.execute(
                pg_insert(entity_relations)
                .values(
                    entity_type=entity_type.value,
                    entity_id=entity_id,
                    principal=related,
                    relation=relation,
                    created_by=principal,
                )
                .on_conflict_do_nothing()
                .returning(entity_relations.c.created_at, entity_relations.c.created_by)
            )
        ).one_or_none()
        created = stored is not None
        if stored is None:
            stored = (
                await conn.execute(
                    select(entity_relations.c.created_at, entity_relations.c.created_by).where(key)
                )
            ).one()
        else:
            await record_event(
                conn,
                OwnershipEventKind.RELATION_ADDED,
                entity_type,
                entity_id,
                old=None,
                new=_pair(related, relation),
                principal=principal,
            )
            await control.record_control_operation(
                conn, actor=principal, story_id=_story_of(owned, entity_id)
            )
        timeline, config = await _revisions(conn, owned, entity_id)
    return RelationWrite(
        entity_type=entity_type,
        entity_id=entity_id,
        relation=Relation(
            principal=related,
            relation=relation,
            created_at=stored.created_at,
            created_by=str(stored.created_by),
        ),
        created=created,
        timeline_revision=timeline,
        config_revision=config,
    )


async def remove_relation(
    db: AsyncEngine,
    entity_type: EntityType,
    entity_id: str,
    related: str,
    relation: str,
    *,
    principal: str,
) -> RelationRemoved:
    """Remove the entity's ``relation`` to ``related``, recorded with ``principal`` as the
    actor. 404 for an unknown entity or an absent relation, 422
    ``companion_follows_character`` for a companion."""
    owned = REGISTRY[entity_type]
    async with db.begin() as conn:
        columns = await _columns(conn, owned, entity_id)
        _refuse_companion(owned, await _entity(conn, owned, entity_id, columns, lock="share"))
        removed = await conn.scalar(
            delete(entity_relations)
            .where(
                entity_relations.c.entity_type == entity_type.value,
                entity_relations.c.entity_id == entity_id,
                entity_relations.c.principal == related,
                entity_relations.c.relation == relation,
            )
            .returning(entity_relations.c.relation)
        )
        if removed is None:
            raise RelationNotFound(
                f"{owned.noun} {entity_id} has no relation {relation!r} to that principal",
                {
                    "entity_type": entity_type.value,
                    "entity_id": entity_id,
                    "relation": relation,
                    "principal": related,
                },
            )
        await record_event(
            conn,
            OwnershipEventKind.RELATION_REMOVED,
            entity_type,
            entity_id,
            old=_pair(related, relation),
            new=None,
            principal=principal,
        )
        await control.record_control_operation(
            conn, actor=principal, story_id=_story_of(owned, entity_id)
        )
        timeline, config = await _revisions(conn, owned, entity_id)
    return RelationRemoved(
        entity_type=entity_type,
        entity_id=entity_id,
        principal=related,
        relation=relation,
        timeline_revision=timeline,
        config_revision=config,
    )


async def drop_entity(
    conn: AsyncConnection, entity_type: EntityType, entity_id: str, *, principal: str
) -> None:
    """Every delete path's ownership step, in its transaction and before the entity's row goes:
    remove the entity's relations and record a ``deleted`` event whose ``old`` names the owner
    and the relations removed. #50's story delete and #103's catalog delete call it, and so do
    the delete paths of #105, #106, #108, #114 and #116."""
    owned = REGISTRY[entity_type]
    handle = _table(owned, ["id", "owner_principal"])
    owner = await conn.scalar(select(handle.c.owner_principal).where(handle.c.id == entity_id))
    removed = await conn.execute(
        delete(entity_relations)
        .where(
            entity_relations.c.entity_type == entity_type.value,
            entity_relations.c.entity_id == entity_id,
        )
        .returning(entity_relations.c.principal, entity_relations.c.relation)
    )
    relations = sorted(
        (_pair(r.principal, r.relation) for r in removed),
        key=lambda pair: (pair["relation"], pair["principal"]),
    )
    await record_event(
        conn,
        OwnershipEventKind.DELETED,
        entity_type,
        entity_id,
        old={"owner_principal": owner, "relations": relations},
        new=None,
        principal=principal,
    )


# -- reads --------------------------------------------------------------------------------------


async def list_relations(db: AsyncEngine, entity_type: EntityType, entity_id: str) -> Relations:
    """The entity's owner and relations; 404 for an unknown entity."""
    owned = REGISTRY[entity_type]
    async with db.connect() as conn:
        columns = await _columns(conn, owned, entity_id)
        row = await _entity(conn, owned, entity_id, columns)
        found = await conn.execute(
            select(
                entity_relations.c.principal,
                entity_relations.c.relation,
                entity_relations.c.created_at,
                entity_relations.c.created_by,
            )
            .where(
                entity_relations.c.entity_type == entity_type.value,
                entity_relations.c.entity_id == entity_id,
            )
            .order_by(
                entity_relations.c.relation.collate("C"), entity_relations.c.principal.collate("C")
            )
        )
        items = tuple(
            Relation(
                principal=r.principal,
                relation=r.relation,
                created_at=r.created_at,
                created_by=r.created_by,
            )
            for r in found
        )
    return Relations(
        entity_type=entity_type,
        entity_id=entity_id,
        owner_principal=str(row.owner_principal),
        items=items,
    )


__all__ = [
    "COMPANION_COLUMN",
    "REGISTRY",
    "RELATION_FIELD_PATTERN",
    "RELATION_FILTER_PATTERN",
    "SEGMENTS",
    "EntityType",
    "Link",
    "OwnedType",
    "Owner",
    "OwnerTransfer",
    "OwnerTransferRequest",
    "OwnershipEventKind",
    "Principal",
    "Relation",
    "RelationFilter",
    "RelationName",
    "RelationRemoved",
    "RelationRequest",
    "RelationWrite",
    "Relations",
    "add_relation",
    "columns_of",
    "delivered",
    "drop_entity",
    "entity_type_of",
    "list_filters",
    "list_relations",
    "owner_filter",
    "parse_relation_filter",
    "path_principal",
    "path_relation",
    "principal_rule",
    "record_event",
    "relation_filter_problem",
    "relation_name_problem",
    "remove_relation",
    "transfer_owner",
]
