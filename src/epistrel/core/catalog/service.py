"""The catalog lifecycle, written once for every kind of entry (FR-CAT-1,3; FR-SCN-1,10; #103,
#108).

A catalog entry is one row of its kind's catalog table: its owner (the ``owner`` a creation
names, else the requesting principal; FR-AUTH-5), its listing status (``private`` until #110
makes the set configurable), its status (``draft`` until the first publish, then
``published``, or ``archived``), its latest published version (``current_version``, ``None``
before the first publish) and its one draft: a JSON document with the ``draft_revision`` every
save and publish compares (FR-CAT-3). Its versions are rows of the kind's versions table.

``CatalogService`` holds the lifecycle; a kind (catalog characters, scenarios) subclasses it and
fills in its hooks: the tables and nouns, how a document is validated and stored (``stored``),
the checks a save makes against the database (``checked``), the version's extra columns
(``version_values``), what a write records beside the row (``recorded``/``forgotten``), and the
projections (``project``, ``envelope``). Publishing asks the kind's ``Validator`` for a
``PublishReport`` (#111 replaces the validators with its full runner).

- **Create** stores the request's draft at draft revision 1, status ``draft``.
- **Save** applies a JSON merge patch (RFC 7386: a key sets a field, ``null`` removes it, an
  array replaces the stored one) to the draft, validates the result, caps it (413
  ``draft_too_large``) and writes it with a compare-and-set on ``draft_revision``. A save based
  on an older revision is 409 ``draft_stale`` (``details.current_draft_revision``) and changes
  nothing, so of two saves from the same revision exactly one succeeds. Every save bumps the
  revision, even one that changes nothing.
- **Publish** compares ``draft_revision`` the same way, under the entry's row lock, validates the
  draft (422 ``publish_invalid`` with the report), inserts version ``current_version + 1`` with
  the draft as its content, sets ``published`` and bumps the revision: the draft stays equal to
  what was published, and the next save needs the new revision. Publishing an unchanged draft
  still makes a version.
- **Versions are immutable**: a trigger refuses any UPDATE or DELETE of a version (SQLSTATE
  ``EPI01``) unless the transaction set ``epistrel.erasure = on``, which only delete (and #124's
  erasure) does.
- **Archive** makes the entry ``archived``: it still reads in both projections, but saves and
  publishes are 422 ``entry_archived`` (and #119 refuses it for story creation). **Unarchive**
  restores ``published``, or ``draft`` when nothing was ever published. Both answer with nothing
  changed when there is nothing to change. **Delete** removes the entry and its versions, from
  any status, and writes a ``catalog_entry_deleted`` audit row. It also removes the entry's
  relations and records a ``deleted`` ownership event (``core.ownership.drop_entity``, #104).

The owner and the relations are ``core.ownership``'s: transfers and relation changes happen
there, and nothing here reads them to decide anything (FR-AUTH-8).

Every write is one transaction that also records a committed, story-less ``control`` operation
with the requesting principal and binds the call's ``Idempotency-Key`` to it
(``core.control``), so a refused write leaves no row behind. No write reads or touches a story
table: a story keeps its snapshot and its ``source_catalog_id@version`` provenance whatever
happens to the entry (FR-CAT-9). Timestamps come from ``core.clock``.

A read returns one projection (FR-SCN-10): ``card`` (the default), only the public card, or
``full``, which adds the entry's envelope and every private field.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Table, func, insert, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, control, ownership
from epistrel.core.config_changes import read_settings
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import (
    DraftStale,
    DraftTooLarge,
    EntryArchived,
    EpistrelError,
    InvalidRequest,
    NotPublished,
    PublishInvalid,
    field_path,
)
from epistrel.core.idempotency import canonical
from epistrel.core.ids import new_ulid
from epistrel.core.ports import Ports
from epistrel.core.settings_schema import DeploymentSettings
from epistrel.core.tables import deployment_state
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.catalog import ERASURE_GUC
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses

LISTING_STATUS = "private"
"""Every new entry's listing status until #110 makes the values configurable (FR-CAT-11)."""
FIRST_DRAFT_REVISION = 1
REVISION_MAX = 2**31 - 1
"""``draft_revision`` and ``version`` are ``INTEGER`` columns: a larger value is a 422 at the
boundary, never a database error (#280's lesson)."""

Revision = Annotated[int, Field(ge=1, le=REVISION_MAX)]


# -- requests and reports ----------------------------------------------------------------------


class PublishRequest(BaseModel):
    """``POST /catalog/{characters|scenarios}/{id}/publish``: the draft revision being published
    (FR-CAT-3); a stale one is 409 ``draft_stale``."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"draft_revision": 2}]}
    )

    draft_revision: Revision


def merge_patch(target: Any, patch: Any) -> Any:
    """``patch`` applied to ``target`` by RFC 7386 (neither argument is changed)."""
    if not isinstance(patch, Mapping):
        return patch
    merged: dict[str, Any] = dict(target) if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = merge_patch(merged.get(key), value)
    return merged


class Projection(StrEnum):
    """A catalog read's projection (FR-SCN-10): ``card`` (the default) or ``full``."""

    CARD = "card"
    FULL = "full"


class CardRating(BaseModel):
    """The content rating on a public card (FR-RATE-3; #115): the rating, the version the shown
    version was published under, and the rating's name."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ReportEntry:
    """One finding of the publish validation: a stable ``code``, the draft ``field`` it concerns
    (a dotted path) and a message."""

    code: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class PublishReport:
    """What the publish validation found (#111 replaces the builders): ``errors`` stop a
    publish; ``unavailable`` names checks that could not run."""

    errors: tuple[ReportEntry, ...] = ()
    warnings: tuple[ReportEntry, ...] = ()
    unavailable: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        def entries(found: tuple[ReportEntry, ...]) -> list[dict[str, str]]:
            return [{"code": e.code, "field": e.field, "message": e.message} for e in found]

        return {
            "errors": entries(self.errors),
            "warnings": entries(self.warnings),
            "unavailable": list(self.unavailable),
        }


def schema_report(model: type[BaseModel], document: Any) -> PublishReport | None:
    """The report of ``document`` failing to validate as ``model``: one ``ReportEntry`` per
    failure, at its dotted path; ``None`` when it validates."""
    try:
        model.model_validate(document)
    except ValidationError as exc:
        return PublishReport(
            errors=tuple(
                ReportEntry(
                    code=str(error["type"]),
                    field=field_path(error.get("loc", ())),
                    message=str(error["msg"]),
                )
                for error in exc.errors()
            )
        )
    return None


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """What a ``Validator`` may consult: the publish's connection (inside its transaction, the
    entry locked), the deployment's settings, and the ports to the Orchestrator (#103)."""

    conn: AsyncConnection
    settings: DeploymentSettings
    ports: Ports = field(default_factory=Ports)


class Validator(Protocol):
    """A kind's publish validation (#108's protocol; #111 supplies the full runner): the report
    on the stored draft document. Errors refuse the publish (422 ``publish_invalid``)."""

    async def validate(self, draft: Any, ctx: ValidationContext) -> PublishReport: ...


# -- results -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Published:
    """The version a publish made, the draft's new revision, and the validation report."""

    id: str
    version: int
    draft_revision: int
    status: EntryStatus
    published_at: datetime
    report: PublishReport
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class VersionSummary:
    """One published version: its number, when and by whom it was published."""

    version: int
    published_at: datetime
    published_by: str


@dataclass(frozen=True, slots=True)
class Shown:
    """What a read or a write shows: the entry row, the document (a version's content or the
    draft), the version it is (``None`` for a draft never published), when that version was
    published, the version row's extra columns (``None`` for a draft read), and, on a draft
    read, the draft's revision."""

    entry: Any
    document: Mapping[str, Any]
    version: int | None
    published_at: datetime | None
    version_row: Any | None = None
    draft_revision: int | None = None


async def config_revision(conn: AsyncConnection) -> int:
    """The deployment's ``config_revision`` (catalog writes leave it as it is)."""
    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


# -- the service -------------------------------------------------------------------------------


class CatalogService(ABC):
    """The lifecycle of one kind of catalog entry (see the module docstring)."""

    entity_type: ClassVar[EntityType]
    noun: ClassVar[str]
    """How messages name an entry: ``catalog character``, ``scenario``."""
    id_key: ClassVar[str]
    """The ``details`` key that names the entry: ``character_id``, ``scenario_id``."""
    catalog: ClassVar[Table]
    versions: ClassVar[Table]
    not_found_error: ClassVar[type[EpistrelError]]

    def __init__(self, validator: Validator) -> None:
        self.validator = validator

    # -- hooks -------------------------------------------------------------------------------

    @abstractmethod
    def max_bytes(self) -> int:
        """The largest draft, as canonical JSON (UTF-8 bytes)."""

    @abstractmethod
    def stored(self, document: Any) -> dict[str, Any]:
        """``document`` as a stored draft: validated, normalised, every rule that needs no
        database checked; 422 ``validation_error`` at ``body.<field>`` otherwise."""

    async def checked(
        self, conn: AsyncConnection, document: Mapping[str, Any], previous: Any
    ) -> None:
        """The checks a create or save makes against the database, inside its transaction
        (``previous``: the draft before a save, ``None`` on create). None by default."""
        return None

    async def version_values(
        self, conn: AsyncConnection, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The version row's columns besides its key, content and publisher."""
        return {"content_rating_version_id": None}

    async def recorded(
        self,
        conn: AsyncConnection,
        entry_id: str,
        document: Mapping[str, Any],
        *,
        version: int | None,
    ) -> None:
        """What a draft write (``version`` ``None``) or a publish records beside the row, in its
        transaction. Nothing by default."""
        return None

    async def forgotten(self, conn: AsyncConnection, entry_id: str) -> None:
        """What a delete removes beside the rows, in its transaction. Nothing by default."""
        return None

    @abstractmethod
    async def project(self, conn: AsyncConnection, shown: Shown, projection: Projection) -> Any:
        """``shown`` as ``projection`` (FR-SCN-10)."""

    @abstractmethod
    async def envelope(self, conn: AsyncConnection, entry: Any) -> Any:
        """The entry without a document: what archive and unarchive answer with."""

    # -- rows --------------------------------------------------------------------------------

    def not_found(self, entry_id: str, version: int | None = None) -> EpistrelError:
        if version is None:
            return self.not_found_error(
                f"{self.noun} {entry_id} does not exist", {self.id_key: entry_id}
            )
        return self.not_found_error(
            f"{self.noun} {entry_id} has no version {version}",
            {self.id_key: entry_id, "version": version},
        )

    async def entry(self, conn: AsyncConnection, entry_id: str, *, lock: bool = False) -> Any:
        query = select(self.catalog).where(self.catalog.c.id == entry_id)
        if lock:
            query = query.with_for_update()
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            raise self.not_found(entry_id)
        return row

    async def published_at(
        self, conn: AsyncConnection, entry_id: str, version: int | None
    ) -> datetime | None:
        if version is None:
            return None
        found: datetime | None = await conn.scalar(
            select(self.versions.c.published_at).where(
                self.versions.c.catalog_id == entry_id, self.versions.c.version == version
            )
        )
        return found

    def check_size(self, document: Mapping[str, Any]) -> None:
        limit = self.max_bytes()
        size = len(canonical(document).encode())
        if size > limit:
            raise DraftTooLarge(
                f"the draft would be {size} bytes; the limit is {limit}",
                {"size": size, "limit": limit},
            )

    def conflict(self, entry: Any, entry_id: str, draft_revision: int) -> EpistrelError:
        """Why a save or publish based on ``draft_revision`` cannot change ``entry``: 422
        ``entry_archived`` for an archived entry, else 409 ``draft_stale``."""
        if entry.status == EntryStatus.ARCHIVED.value:
            return EntryArchived(
                f"{self.noun} {entry_id} is archived; unarchive it to change it",
                {self.id_key: entry_id},
            )
        return DraftStale(
            f"the draft of {self.noun} {entry_id} is at revision {entry.draft_revision}, not "
            f"{draft_revision}; read it again and retry",
            {
                self.id_key: entry_id,
                "draft_revision": draft_revision,
                "current_draft_revision": int(entry.draft_revision),
            },
        )

    def refuse(self, entry: Any, entry_id: str, draft_revision: int) -> None:
        """Raise ``conflict`` unless ``entry`` is editable at ``draft_revision``."""
        if entry.status == EntryStatus.ARCHIVED.value or entry.draft_revision != draft_revision:
            raise self.conflict(entry, entry_id, draft_revision)

    async def draft_shown(self, conn: AsyncConnection, entry_id: str) -> Any:
        """The draft in ``full``, as a create or save answers it."""
        entry = await self.entry(conn, entry_id)
        shown = Shown(
            entry=entry,
            document=entry.draft,
            version=entry.current_version,
            published_at=await self.published_at(conn, entry_id, entry.current_version),
            draft_revision=int(entry.draft_revision),
        )
        return await self.project(conn, shown, Projection.FULL)

    # -- writes ------------------------------------------------------------------------------

    async def create(
        self,
        db: AsyncEngine,
        document: Any,
        *,
        owner: str,
        principal: str,
        values: Mapping[str, Any] | None = None,
    ) -> tuple[Any, int]:
        """A new entry whose draft is ``document`` at draft revision 1 (status ``draft``,
        listing status ``private``), owned by ``owner``; ``values`` are further columns. Answers
        the draft in ``full`` and the deployment's ``config_revision``."""
        draft = self.stored(document)
        self.check_size(draft)
        entry_id = new_ulid()
        now = clock.now()
        async with db.begin() as conn:
            await conn.execute(
                insert(self.catalog).values(
                    id=entry_id,
                    owner_principal=owner,
                    listing_status=LISTING_STATUS,
                    status=EntryStatus.DRAFT.value,
                    current_version=None,
                    draft=draft,
                    draft_revision=FIRST_DRAFT_REVISION,
                    created_at=now,
                    updated_at=now,
                    **dict(values or {}),
                )
            )
            await self.checked(conn, draft, None)
            await control.record_control_operation(conn, actor=principal)
            await self.recorded(conn, entry_id, draft, version=None)
            return await self.draft_shown(conn, entry_id), await config_revision(conn)

    async def save(
        self,
        db: AsyncEngine,
        entry_id: str,
        patch: Mapping[str, Any],
        draft_revision: int,
        *,
        principal: str,
    ) -> tuple[Any, int]:
        """Apply the merge patch ``patch`` to the draft (see the module docstring): 404 for an
        unknown entry, 422 ``entry_archived``, 409 ``draft_stale``, 422 for an invalid result,
        413 for a too large one. Answers the draft in ``full`` and the ``config_revision``."""
        async with db.connect() as conn:
            entry = await self.entry(conn, entry_id)
        self.refuse(entry, entry_id, draft_revision)
        document = self.stored(merge_patch(entry.draft, patch))
        self.check_size(document)
        await pauses.pause(pauses.CATALOG_SAVE_BEFORE_CAS)
        async with db.begin() as conn:
            saved = await conn.scalar(
                update(self.catalog)
                .where(
                    self.catalog.c.id == entry_id,
                    self.catalog.c.draft_revision == draft_revision,
                    self.catalog.c.status != EntryStatus.ARCHIVED.value,
                )
                .values(
                    draft=document,
                    draft_revision=self.catalog.c.draft_revision + 1,
                    updated_at=clock.now(),
                )
                .returning(self.catalog.c.id)
            )
            if saved is None:
                # Another save, publish or archive committed since the read (or the entry is
                # gone).
                raise self.conflict(await self.entry(conn, entry_id), entry_id, draft_revision)
            await self.checked(conn, document, entry.draft)
            await control.record_control_operation(conn, actor=principal)
            await self.recorded(conn, entry_id, document, version=None)
            return await self.draft_shown(conn, entry_id), await config_revision(conn)

    async def publish(
        self,
        db: AsyncEngine,
        entry_id: str,
        request: PublishRequest,
        *,
        principal: str,
        ports: Ports | None = None,
    ) -> Published:
        """Publish the draft as version ``current_version + 1`` (see the module docstring)."""
        async with db.begin() as conn:
            entry = await self.entry(conn, entry_id, lock=True)
            self.refuse(entry, entry_id, request.draft_revision)
            await pauses.pause(pauses.CATALOG_PUBLISH_LOCKED)
            settings = (await read_settings(conn)).as_settings()
            context = ValidationContext(conn=conn, settings=settings, ports=ports or Ports())
            report = await self.validator.validate(entry.draft, context)
            if report.errors:
                raise PublishInvalid(
                    f"the draft of {self.noun} {entry_id} cannot be published",
                    {self.id_key: entry_id, "report": report.as_json()},
                )
            version = int(entry.current_version or 0) + 1
            draft_revision = int(entry.draft_revision) + 1
            now = clock.now()
            extra = await self.version_values(conn, entry.draft)
            await conn.execute(
                insert(self.versions).values(
                    catalog_id=entry_id,
                    version=version,
                    content=entry.draft,
                    published_at=now,
                    published_by=principal,
                    **extra,
                )
            )
            # The answer reports the status the entry row now holds, not the one intended.
            stored_status = await conn.scalar(
                update(self.catalog)
                .where(self.catalog.c.id == entry_id)
                .values(
                    status=EntryStatus.PUBLISHED.value,
                    current_version=version,
                    draft_revision=draft_revision,
                    updated_at=now,
                )
                .returning(self.catalog.c.status)
            )
            await control.record_control_operation(conn, actor=principal)
            await self.recorded(conn, entry_id, entry.draft, version=version)
            revision = await config_revision(conn)
        return Published(
            id=entry_id,
            version=version,
            draft_revision=draft_revision,
            status=EntryStatus(stored_status),
            published_at=now,
            report=report,
            config_revision=revision,
        )

    async def set_archived(
        self, db: AsyncEngine, entry_id: str, archived: bool, *, principal: str
    ) -> tuple[Any, bool, int]:
        """Archive (``archived``) or unarchive the entry: ``(envelope, changed,
        config_revision)``. An entry already in that state is left as it is."""
        async with db.begin() as conn:
            entry = await self.entry(conn, entry_id, lock=True)
            if (entry.status == EntryStatus.ARCHIVED.value) == archived:
                return await self.envelope(conn, entry), False, await config_revision(conn)
            if archived:
                status = EntryStatus.ARCHIVED
            elif entry.current_version is not None:
                status = EntryStatus.PUBLISHED
            else:
                status = EntryStatus.DRAFT
            await conn.execute(
                update(self.catalog)
                .where(self.catalog.c.id == entry_id)
                .values(status=status.value, updated_at=clock.now())
            )
            await control.record_control_operation(conn, actor=principal)
            shown = await self.envelope(conn, await self.entry(conn, entry_id))
            return shown, True, await config_revision(conn)

    async def delete(self, db: AsyncEngine, entry_id: str, *, principal: str) -> int:
        """Remove the entry and its versions (from any status) and write a
        ``catalog_entry_deleted`` audit row; 404 for an unknown entry. Its relations go too,
        recorded as a ``deleted`` ownership event (#104). Stories created from it keep their
        snapshot and provenance (FR-CAT-9). Answers how many versions went with it."""
        async with db.begin() as conn:
            await self.entry(conn, entry_id, lock=True)
            await ownership.drop_entity(conn, self.entity_type, entry_id, principal=principal)
            versions = await conn.scalar(
                select(func.count())
                .select_from(self.versions)
                .where(self.versions.c.catalog_id == entry_id)
            )
            await self.forgotten(conn, entry_id)
            # The one exemption the versions' immutability trigger knows (shared with #124).
            await conn.execute(select(func.set_config(ERASURE_GUC, "on", True)))
            await conn.execute(sql_delete(self.catalog).where(self.catalog.c.id == entry_id))
            operation_id = await control.record_control_operation(conn, actor=principal)
            await audit.record(
                conn,
                AuditKind.CATALOG_ENTRY_DELETED,
                principal=principal,
                details={
                    "entity_type": self.entity_type.value,
                    "entity_id": entry_id,
                    "operation_id": operation_id,
                },
            )
        return int(versions or 0)

    # -- reads -------------------------------------------------------------------------------

    async def get(
        self,
        db: AsyncEngine,
        entry_id: str,
        *,
        version: int | None = None,
        draft: bool = False,
        projection: Projection = Projection.CARD,
    ) -> Any:
        """The latest published version, version ``version``, or (``draft``) the draft, as
        ``projection``. 404 for an unknown entry or version; 422 ``not_published`` for the
        published entry of one never published; 422 when both ``version`` and ``draft`` are
        given."""
        if draft and version is not None:
            raise InvalidRequest(
                "read one version (`version`) or the draft (`draft=true`), not both",
                {"field": "query.version"},
            )
        async with db.connect() as conn:
            entry = await self.entry(conn, entry_id)
            if draft:
                shown = Shown(
                    entry=entry,
                    document=entry.draft,
                    version=entry.current_version,
                    published_at=await self.published_at(conn, entry_id, entry.current_version),
                    draft_revision=int(entry.draft_revision),
                )
                return await self.project(conn, shown, projection)
            wanted = version if version is not None else entry.current_version
            if wanted is None:
                raise NotPublished(
                    f"{self.noun} {entry_id} has not been published; read its draft",
                    {self.id_key: entry_id},
                )
            found = (
                await conn.execute(
                    select(self.versions).where(
                        self.versions.c.catalog_id == entry_id,
                        self.versions.c.version == wanted,
                    )
                )
            ).one_or_none()
            if found is None:
                raise self.not_found(entry_id, wanted)
            shown = Shown(
                entry=entry,
                document=found.content,
                version=wanted,
                published_at=found.published_at,
                version_row=found,
            )
            return await self.project(conn, shown, projection)

    async def list_versions(self, db: AsyncEngine, entry_id: str) -> list[VersionSummary]:
        """Every published version of the entry, oldest first; 404 for an unknown entry."""
        async with db.connect() as conn:
            await self.entry(conn, entry_id)
            found = await conn.execute(
                select(
                    self.versions.c.version,
                    self.versions.c.published_at,
                    self.versions.c.published_by,
                )
                .where(self.versions.c.catalog_id == entry_id)
                .order_by(self.versions.c.version)
            )
            return [
                VersionSummary(
                    version=int(row.version),
                    published_at=row.published_at,
                    published_by=row.published_by,
                )
                for row in found
            ]


__all__ = [
    "FIRST_DRAFT_REVISION",
    "LISTING_STATUS",
    "REVISION_MAX",
    "CardRating",
    "CatalogService",
    "Projection",
    "PublishReport",
    "PublishRequest",
    "Published",
    "ReportEntry",
    "Revision",
    "Shown",
    "ValidationContext",
    "Validator",
    "VersionSummary",
    "config_revision",
    "merge_patch",
    "schema_report",
]
