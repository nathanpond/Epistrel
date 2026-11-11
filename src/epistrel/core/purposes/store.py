"""The consumer purpose store: register, version, archive and read (FR-MODEL-14; #824).

**Writes** follow every deployment-scoped configuration write (#48): take
``lock_deployment`` (which also serialises the live-purpose count against concurrent
creations), move ``config_revision`` by exactly one, and record one ``config_changes`` row
(class ``prospective``: a change applies from the next call, and a call under way keeps the
version it resolved) with the acting principal, all in one transaction. The row's setting is
``consumer_purpose:<name>``, and its values name the version and a SHA-256 of the version's
canonical JSON, never the template or schema text. Moving the revision is also what tells the
Orchestrator's resolution cache to re-read the store.

- ``register`` checks, in order, the name (``name_invalid``; ``name_taken`` for a built-in
  name or one already registered, archived ones included), the templates
  (``template_invalid``), the output schema (``schema_invalid``) and the live-purpose limit
  (``purpose_limit``: more than ``PURPOSE_LIMIT`` live purposes; archived ones do not count),
  then writes the purpose at version 1.
- ``replace`` refuses an unknown name (404 ``not_found``) and an archived purpose (410
  ``purpose_archived``), then an ``expected_version`` that is not the latest (409
  ``version_stale``), then checks the templates and schema, and writes version n+1. A
  replacement identical to the latest version writes nothing (``changed: false``).
- ``archive`` sets ``archived_at`` (an archived purpose's calls are refused with 410, and its
  versions stay readable); archiving an archived purpose changes nothing. There is no
  unarchive, and the name stays taken.

**Reads.** ``listing`` pages every purpose, archived ones included, by name; ``read`` gives the
latest version or one named (404 ``version_not_found``); ``versions`` lists a purpose's
versions, oldest first; ``live_names`` and ``find`` serve the model configuration and the
Orchestrator's resolution.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import (
    bump_config_revision,
    lock_deployment,
    read_settings,
    record_config_change,
)
from epistrel.core.errors import (
    CursorInvalid,
    PurposeArchived,
    PurposeNotFound,
    PurposeVersionNotFound,
    PurposeVersionStale,
)
from epistrel.core.ids import new_ulid
from epistrel.core.purposes.validation import (
    DESCRIPTION_MAX,
    NAME_TAKEN,
    PURPOSE_LIMIT,
    PURPOSE_LIMIT_REACHED,
    PurposeRefused,
    canonical_json,
    check_name,
    check_schema,
    check_template,
)
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.tables.consumer_purposes import consumer_purpose_versions, consumer_purposes

SETTING_FAMILY: Final = "consumer_purpose"
"""The ``config_changes`` setting family of a consumer purpose's writes."""
DEFAULT_PAGE: Final = 50
MAX_PAGE: Final = 200
VERSION_MAX: Final = 1_000_000

P = consumer_purposes.c
V = consumer_purpose_versions.c


def setting(name: str) -> str:
    """The ``config_changes`` setting of purpose ``name``'s writes."""
    return f"{SETTING_FAMILY}:{name}"


# -- bodies ------------------------------------------------------------------------------------


Description = Annotated[str, Field(max_length=DESCRIPTION_MAX)]


class _VersionFields(BaseModel):
    """What each version holds (every field but ``name``, which is the purpose's own)."""

    model_config = ConfigDict(extra="forbid")

    description: Description = Field(
        description=f"What the purpose does, for operators (at most {DESCRIPTION_MAX} characters)."
    )
    template: str = Field(
        description="The Jinja2 template of the user message (sandboxed, `StrictUndefined`, "
        "built-in filters only; variables `inputs`, `viewpoint` and `story`; at most 32 KB)."
    )
    system_template: str | None = Field(
        default=None,
        description="An optional template rendered after Epistrel's fixed system preamble "
        "(the rating guidance and the instruction to answer only with JSON matching the "
        "schema); the same rules as `template`.",
    )
    output_schema: dict[str, Any] = Field(
        description="The JSON Schema of the reply, in the subset the strict structured path "
        "sends as it is: an object root; `type` among string, number, integer, boolean, "
        "array and object; `properties`, `required` (every property) and "
        "`additionalProperties: false` on every object; `items` on every array; `enum`, "
        "`const`, `description`, `title`, `minLength`/`maxLength`, `pattern`, "
        "`minItems`/`maxItems`, `minimum`/`maximum`; depth at most 6; at most 16 KB."
    )


class PurposeCreate(_VersionFields):
    """``POST /admin/purposes``: a new consumer purpose at version 1."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "vote_reason",
                    "description": "A contestant's in-character reason for a vote.",
                    "template": "Explain why {{ inputs.voter }} voted for {{ inputs.target }}.",
                    "output_schema": {
                        "type": "object",
                        "properties": {"reason": {"type": "string", "maxLength": 500}},
                        "required": ["reason"],
                        "additionalProperties": False,
                    },
                }
            ]
        },
    )

    name: str = Field(
        description="The purpose's name: 3 to 48 characters, a lowercase letter then lowercase "
        "letters, digits and `_`. Immutable; never a built-in purpose's name."
    )


class PurposeReplace(_VersionFields):
    """``PUT /admin/purposes/{name}``: the next version, replacing every per-version field."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "expected_version": 1,
                    "description": "A contestant's in-character reason for a vote.",
                    "template": "In {{ inputs.voter }}'s voice, explain the vote for "
                    "{{ inputs.target }}.",
                    "output_schema": {
                        "type": "object",
                        "properties": {"reason": {"type": "string", "maxLength": 500}},
                        "required": ["reason"],
                        "additionalProperties": False,
                    },
                }
            ]
        },
    )

    expected_version: StrictInt = Field(
        ge=1,
        le=VERSION_MAX,
        description="The latest version the edit was based on (409 `version_stale` otherwise).",
    )


class ConsumerPurpose(BaseModel):
    """A consumer purpose at one version."""

    name: str
    version: int
    latest_version: int
    description: str
    template: str
    system_template: str | None
    output_schema: dict[str, Any]
    created_at: datetime
    """When the purpose was registered."""
    version_created_at: datetime
    version_created_by: str
    archived_at: datetime | None


class PurposeSummary(BaseModel):
    """A consumer purpose as the list shows it (its latest version's description)."""

    name: str
    latest_version: int
    description: str
    created_at: datetime
    updated_at: datetime
    """When its latest version was written."""
    archived_at: datetime | None


class PurposePage(BaseModel):
    """One page of consumer purposes, by name."""

    items: list[PurposeSummary]
    next_cursor: str | None


class PurposeVersionInfo(BaseModel):
    """One version of a consumer purpose, as the versions list shows it."""

    version: int
    description: str
    created_at: datetime
    created_by: str


@dataclass(frozen=True)
class PurposeWrite:
    """A store write: the purpose as it now stands (latest version), the deployment's
    ``config_revision`` after it, and whether anything changed."""

    purpose: ConsumerPurpose
    config_revision: int
    changed: bool = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True)
class PurposeRow:
    """``consumer_purposes``' row: what resolution needs before reading a version."""

    id: str
    name: str
    archived_at: datetime | None
    latest_version: int


# -- reads -------------------------------------------------------------------------------------


def _latest() -> Any:
    return (
        select(func.max(V.version))
        .where(V.purpose_id == P.id)
        .correlate(consumer_purposes)
        .scalar_subquery()
    )


async def find(conn: AsyncConnection, name: str) -> PurposeRow | None:
    """The purpose ``name`` (archived or not) with its latest version; ``None`` when unknown.
    (Writes hold the deployment lock, which serialises every write of the store.)"""
    query = select(P.id, P.name, P.archived_at, _latest().label("latest")).where(P.name == name)
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return PurposeRow(row.id, row.name, row.archived_at, int(row.latest))


async def live_names(conn: AsyncConnection) -> list[str]:
    """Every live (not archived) consumer purpose's name, sorted."""
    return list(await conn.scalars(select(P.name).where(P.archived_at.is_(None)).order_by(P.name)))


async def _version(conn: AsyncConnection, found: PurposeRow, version: int) -> ConsumerPurpose:
    row = (
        await conn.execute(
            select(consumer_purpose_versions, P.created_at.label("purpose_created_at"))
            .join(consumer_purposes, P.id == V.purpose_id)
            .where(V.purpose_id == found.id, V.version == version)
        )
    ).one_or_none()
    if row is None:
        raise PurposeVersionNotFound(
            f"consumer purpose {found.name!r} has no version {version}",
            {"purpose": found.name, "version": version},
        )
    return ConsumerPurpose(
        name=found.name,
        version=row.version,
        latest_version=found.latest_version,
        description=row.description,
        template=row.template,
        system_template=row.system_template,
        output_schema=dict(row.output_schema),
        created_at=row.purpose_created_at,
        version_created_at=row.created_at,
        version_created_by=row.created_by,
        archived_at=found.archived_at,
    )


def _not_found(name: str) -> PurposeNotFound:
    return PurposeNotFound(f"no consumer purpose is registered as {name!r}", {"purpose": name})


async def read(conn: AsyncConnection, name: str, version: int | None = None) -> ConsumerPurpose:
    """The purpose at ``version`` (default: its latest); archived purposes still read."""
    found = await find(conn, name)
    if found is None:
        raise _not_found(name)
    return await _version(conn, found, version or found.latest_version)


async def versions(conn: AsyncConnection, name: str) -> list[PurposeVersionInfo]:
    """Every version of ``name``, oldest first."""
    found = await find(conn, name)
    if found is None:
        raise _not_found(name)
    rows = await conn.execute(
        select(V.version, V.description, V.created_at, V.created_by)
        .where(V.purpose_id == found.id)
        .order_by(V.version)
    )
    return [
        PurposeVersionInfo(
            version=r.version,
            description=r.description,
            created_at=r.created_at,
            created_by=r.created_by,
        )
        for r in rows
    ]


def _cursor(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")


def _after(cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode()
    except binascii.Error, UnicodeError, ValueError:
        raise CursorInvalid(
            "the cursor is not one this list gave",
            {"field": "query.cursor", "reason": "malformed"},
        ) from None


async def listing(
    conn: AsyncConnection, *, limit: int = DEFAULT_PAGE, cursor: str | None = None
) -> PurposePage:
    """One page of every consumer purpose (archived ones included), by name."""
    latest = _latest()
    query = (
        select(
            P.name,
            P.created_at,
            P.archived_at,
            V.version,
            V.description,
            V.created_at.label("updated_at"),
        )
        .join(consumer_purpose_versions, (V.purpose_id == P.id) & (V.version == latest))
        .order_by(P.name)
        .limit(limit + 1)
    )
    if cursor is not None:
        query = query.where(P.name > _after(cursor))
    rows = list(await conn.execute(query))
    page = rows[:limit]
    items = [
        PurposeSummary(
            name=r.name,
            latest_version=r.version,
            description=r.description,
            created_at=r.created_at,
            updated_at=r.updated_at,
            archived_at=r.archived_at,
        )
        for r in page
    ]
    more = len(rows) > limit
    return PurposePage(items=items, next_cursor=_cursor(page[-1].name) if more and page else None)


# -- writes ------------------------------------------------------------------------------------


def _check_version(fields: _VersionFields) -> dict[str, Any]:
    """The version's columns, after the template and schema checks (in that order)."""
    check_template(fields.template, "body.template")
    if fields.system_template is not None:
        check_template(fields.system_template, "body.system_template")
    schema = check_schema(fields.output_schema)
    return {
        "description": fields.description,
        "template": fields.template,
        "system_template": fields.system_template,
        "output_schema": schema,
    }


def _digest(columns: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(columns)).encode("utf-8")).hexdigest()


def _change(version: int, columns: Mapping[str, Any]) -> dict[str, Any]:
    """What a change row records of a version: its number and digest, never its text."""
    return {"version": version, "sha256": _digest(columns)}


async def register(
    db: AsyncEngine, request: PurposeCreate, *, reserved: Collection[str], principal: str
) -> PurposeWrite:
    """Register a consumer purpose at version 1 (see the module docstring)."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        check_name(request.name, reserved)
        if await find(conn, request.name) is not None:
            raise PurposeRefused(
                NAME_TAKEN,
                "body.name",
                f"a consumer purpose is already registered as {request.name!r}",
            )
        columns = _check_version(request)
        live = await conn.scalar(
            select(func.count()).select_from(consumer_purposes).where(P.archived_at.is_(None))
        )
        if int(live or 0) >= PURPOSE_LIMIT:
            raise PurposeRefused(
                PURPOSE_LIMIT_REACHED,
                "body.name",
                f"the deployment already holds {PURPOSE_LIMIT} live consumer purposes; "
                "archive one first",
            )
        revision = await bump_config_revision(conn, current)
        purpose_id = new_ulid()
        await conn.execute(
            insert(consumer_purposes).values(id=purpose_id, name=request.name, created_by=principal)
        )
        await conn.execute(
            insert(consumer_purpose_versions).values(
                purpose_id=purpose_id, version=1, created_by=principal, **columns
            )
        )
        await record_config_change(
            conn,
            config_revision=revision,
            setting=setting(request.name),
            old=None,
            new=_change(1, columns),
            actor=principal,
            change_class=ConfigClass.prospective,
        )
        return PurposeWrite(await read(conn, request.name), revision)


def _live(found: PurposeRow | None, name: str) -> PurposeRow:
    """``found``, or 404 when unknown and 410 when archived."""
    if found is None:
        raise _not_found(name)
    if found.archived_at is not None:
        raise PurposeArchived(f"consumer purpose {name!r} is archived", {"purpose": name})
    return found


async def replace(
    db: AsyncEngine, name: str, request: PurposeReplace, *, principal: str
) -> PurposeWrite:
    """Write the next version of ``name`` (see the module docstring)."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        found = _live(await find(conn, name), name)
        if request.expected_version != found.latest_version:
            raise PurposeVersionStale(
                f"consumer purpose {name!r} is at version {found.latest_version}, not "
                f"{request.expected_version}",
                {
                    "purpose": name,
                    "expected_version": request.expected_version,
                    "current_version": found.latest_version,
                },
            )
        columns = _check_version(request)
        latest = await _version(conn, found, found.latest_version)
        before = {
            "description": latest.description,
            "template": latest.template,
            "system_template": latest.system_template,
            "output_schema": latest.output_schema,
        }
        if canonical_json(before) == canonical_json(columns):
            return PurposeWrite(latest, current.config_revision, changed=False)
        version = found.latest_version + 1
        revision = await bump_config_revision(conn, current)
        await conn.execute(
            insert(consumer_purpose_versions).values(
                purpose_id=found.id, version=version, created_by=principal, **columns
            )
        )
        await record_config_change(
            conn,
            config_revision=revision,
            setting=setting(name),
            old=_change(found.latest_version, before),
            new=_change(version, columns),
            actor=principal,
            change_class=ConfigClass.prospective,
        )
        return PurposeWrite(await read(conn, name), revision)


async def archive(db: AsyncEngine, name: str, *, principal: str) -> PurposeWrite:
    """Archive ``name`` (see the module docstring); a repeat changes nothing."""
    async with db.begin() as conn:
        current = await lock_deployment(conn)
        found = await find(conn, name)
        if found is None:
            raise _not_found(name)
        if found.archived_at is not None:
            return PurposeWrite(await read(conn, name), current.config_revision, changed=False)
        revision = await bump_config_revision(conn, current)
        await conn.execute(
            update(consumer_purposes)
            .where(P.id == found.id)
            .values(archived_at=func.clock_timestamp())
        )
        await record_config_change(
            conn,
            config_revision=revision,
            setting=setting(name),
            old={"archived": False},
            new={"archived": True},
            actor=principal,
            change_class=ConfigClass.prospective,
        )
        return PurposeWrite(await read(conn, name), revision)


async def revision_and_listing(
    db: AsyncEngine, *, limit: int = DEFAULT_PAGE, cursor: str | None = None
) -> tuple[int, PurposePage]:
    """The deployment's ``config_revision`` and one page of purposes."""
    async with db.connect() as conn:
        revision = (await read_settings(conn)).config_revision
        return revision, await listing(conn, limit=limit, cursor=cursor)


__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "SETTING_FAMILY",
    "VERSION_MAX",
    "ConsumerPurpose",
    "PurposeCreate",
    "PurposePage",
    "PurposeReplace",
    "PurposeRow",
    "PurposeSummary",
    "PurposeVersionInfo",
    "PurposeWrite",
    "archive",
    "find",
    "listing",
    "live_names",
    "read",
    "register",
    "replace",
    "revision_and_listing",
    "setting",
    "versions",
]
