"""The catalog lifecycle, written once for every kind of entry (FR-CAT-1,3; FR-SCN-1,10; #103,
#108).

A catalog entry is one row of its kind's catalog table: its owner (the ``owner`` a creation
names, else the requesting principal; FR-AUTH-5), its listing status (one of the deployment's
``listing_status_values``; ``core.catalog.listing``, #110), its status (``draft`` until the
first publish, then ``published``, or ``archived``), its latest published version
(``current_version``, ``None`` before the first publish) and its one draft: a JSON document with
the ``draft_revision`` every save and publish compares (FR-CAT-3). Its versions are rows of the
kind's versions table.

``CatalogService`` holds the lifecycle; a kind (catalog characters, scenarios) subclasses it and
fills in its hooks: the tables and nouns, how a document is validated and stored (``stored``),
the checks a save makes against the database (``checked``), the version's extra columns
(``version_values``), what a write records beside the row (``recorded``/``forgotten``), the
projections (``project``, ``envelope``, and the listings' ``list_item``), and the entries that
follow one (``followers``: a character's companion scenarios). Publishing asks the kind's
``Validator`` (``core.catalog.validation``'s runner, #111) for a ``ValidationReport``.

- **Create** stores the request's draft at draft revision 1, status ``draft``, at the first of
  the deployment's listing status values (read with the deployment row ``FOR SHARE``, so it
  cannot be removed meanwhile).
- **Save** applies a JSON merge patch (RFC 7386: a key sets a field, ``null`` removes it, an
  array replaces the stored one) to the draft, validates the result, caps it (413
  ``draft_too_large``) and writes it with a compare-and-set on ``draft_revision``. A save based
  on an older revision is 409 ``draft_stale`` (``details.current_draft_revision``) and changes
  nothing, so of two saves from the same revision exactly one succeeds. Every save bumps the
  revision, even one that changes nothing.
- **Publish** compares ``draft_revision`` the same way, prepares the draft's validation outside
  any transaction (the input policies may call a model, #111), then, under the entry's row lock,
  compares the revision again and validates the draft: any error is 422 ``publish_invalid`` with
  ``details.report`` and writes nothing. Otherwise it inserts version ``current_version + 1``
  with the draft as its content, sets ``published`` and bumps the revision: the draft stays
  equal to what was published, and the next save needs the new revision. The answer carries
  the report (its warnings and unavailable checks), and an INFO line logs its codes and
  locations. Publishing an unchanged draft still makes a version.
- **Versions are immutable**: a trigger refuses any UPDATE or DELETE of a version (SQLSTATE
  ``EPI01``) unless the transaction set ``epistrel.erasure = on``, which only delete (and #124's
  erasure) does.
- **Archive** makes the entry ``archived``: it still reads in both projections, but saves and
  publishes are 422 ``entry_archived`` (and #119 refuses it for story creation). **Unarchive**
  restores ``published``, or ``draft`` when nothing was ever published. Both answer with nothing
  changed when there is nothing to change. **Delete** removes the entry and its versions, from
  any status, and writes a ``catalog_entry_deleted`` audit row. It also removes the entry's
  relations and records a ``deleted`` ownership event (``core.ownership.drop_entity``, #104).
- **Set the listing status** (FR-CAT-11; #110) changes the entry's value, and its followers',
  to one of the deployment's values, recorded per entry as a ``listing_status`` ownership event
  (``core.catalog.listing``); allowed in any status. A follower (a companion) is refused alone.

The owner and the relations are ``core.ownership``'s: transfers and relation changes happen
there, and nothing here reads them, or the listing status, to decide anything (FR-AUTH-8).

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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Table, func, insert, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, control, ownership
from epistrel.core.catalog import listing
from epistrel.core.catalog.checks.base import Preparation, Subject, ValidationContext
from epistrel.core.catalog.report import DRAFT, ValidationReport, VersionLabel, log_report
from epistrel.core.config_changes import read_settings
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import (
    CompanionFollowsCharacter,
    DraftStale,
    DraftTooLarge,
    EntryArchived,
    EpistrelError,
    InvalidRequest,
    NotPublished,
    PublishInvalid,
)
from epistrel.core.idempotency import canonical
from epistrel.core.ids import new_ulid
from epistrel.core.ports import Ports
from epistrel.core.tables import deployment_state
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.catalog import ERASURE_GUC
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.ownership import EntityType, OwnershipEventKind
from epistrel.testing import pauses

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


class ValidateRequest(BaseModel):
    """``POST /catalog/scenarios/{id}/validate`` (#111): optionally the draft revision the
    caller holds, so the report is known to be about that draft (a stale one is 409
    ``draft_stale``). Without it, the current draft is validated; ``?version=n`` validates a
    published version instead."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"draft_revision": 2}, {}]}
    )

    draft_revision: Revision | None = Field(
        default=None,
        description="The draft revision the caller holds; omit it to validate the current draft.",
    )


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


class Validator(Protocol):
    """A kind's publish validation (#111: ``core.catalog.validation.Runner``). ``prepare`` runs
    outside any transaction (it may call a model); ``validate`` runs inside the publish's, the
    entry locked, and answers the report. Its errors refuse the publish (422
    ``publish_invalid``)."""

    async def prepare(self, subject: Subject, env: Preparation) -> Mapping[str, Any]: ...

    async def validate(self, subject: Subject, ctx: ValidationContext) -> ValidationReport: ...


# -- results -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Published:
    """The version a publish made, the draft's new revision, and the validation report."""

    id: str
    version: int
    draft_revision: int
    status: EntryStatus
    published_at: datetime
    report: ValidationReport
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


@dataclass(frozen=True, slots=True)
class Follower:
    """An entry that follows another (a character's companion scenario, #116): its type, its
    table, its id and its listing status, as read, locked, by ``CatalogService.followers``."""

    entity_type: EntityType
    table: Table
    id: str
    listing_status: str


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
    list_item: ClassVar[type[BaseModel]]
    """A listing's item (#110): the kind's card plus ``owner_principal``, ``updated_at`` and,
    for a search, ``rank``."""
    document_model: ClassVar[type[BaseModel]]
    """The kind's draft document (``CharacterDraft``, ``ScenarioDraft``): what validation
    checks a document against (#111)."""

    def __init__(self, validator: Validator) -> None:
        self.validator = validator

    def subject(self, entry_id: str, version: VersionLabel, document: Mapping[str, Any]) -> Subject:
        """What a validation of ``document`` (``version``, or the draft) checks (#111)."""
        return Subject(
            entity_type=self.entity_type,
            entry_id=entry_id,
            version=version,
            document=document,
            model=self.document_model,
        )

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
        self, conn: AsyncConnection, document: Mapping[str, Any], report: ValidationReport
    ) -> dict[str, Any]:
        """The version row's columns besides its key, content and publisher (``report``: what
        the publish's validation found)."""
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

    async def followers(self, conn: AsyncConnection, entry_id: str) -> list[Follower]:
        """The entries that follow ``entry_id``'s listing status (its companions), locked
        ``FOR UPDATE`` in id order inside ``conn``'s transaction. None by default."""
        return []

    def leader(self, entry: Any) -> str | None:
        """The id of the entry ``entry`` follows (a companion's character), or ``None``."""
        return None

    async def list_card(self, conn: AsyncConnection, shown: Shown, *, rank: float | None) -> Any:
        """``shown`` as a listing's item: exactly the single-entry read's card, plus the owner,
        ``updated_at`` and, for a search, the rank (#110)."""
        card = await self.project(conn, shown, Projection.CARD)
        return self.list_item.model_validate(
            {
                **card.model_dump(),
                "owner_principal": shown.entry.owner_principal,
                "updated_at": shown.entry.updated_at,
                "rank": rank,
            }
        )

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

    def stale(self, entry: Any, entry_id: str, draft_revision: int) -> DraftStale:
        """409 ``draft_stale``: ``entry``'s draft is no longer at ``draft_revision``."""
        return DraftStale(
            f"the draft of {self.noun} {entry_id} is at revision {entry.draft_revision}, not "
            f"{draft_revision}; read it again and retry",
            {
                self.id_key: entry_id,
                "draft_revision": draft_revision,
                "current_draft_revision": int(entry.draft_revision),
            },
        )

    def conflict(self, entry: Any, entry_id: str, draft_revision: int) -> EpistrelError:
        """Why a save or publish based on ``draft_revision`` cannot change ``entry``: 422
        ``entry_archived`` for an archived entry, else 409 ``draft_stale``."""
        if entry.status == EntryStatus.ARCHIVED.value:
            return EntryArchived(
                f"{self.noun} {entry_id} is archived; unarchive it to change it",
                {self.id_key: entry_id},
            )
        return self.stale(entry, entry_id, draft_revision)

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
        """A new entry whose draft is ``document`` at draft revision 1 (status ``draft``, at
        the first of the deployment's listing status values), owned by ``owner``; ``values``
        are further columns. Answers the draft in ``full`` and the deployment's
        ``config_revision``."""
        draft = self.stored(document)
        self.check_size(draft)
        entry_id = new_ulid()
        now = clock.now()
        async with db.begin() as conn:
            first = (await listing.allowed_values(conn, lock=True))[0]
            await conn.execute(
                insert(self.catalog).values(
                    id=entry_id,
                    owner_principal=owner,
                    listing_status=first,
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
        ports = ports or Ports()
        async with db.connect() as conn:
            read = await self.entry(conn, entry_id)
        self.refuse(read, entry_id, request.draft_revision)
        # Outside any transaction: the input policies may call a model (FR-CONC-2). The revision
        # compared under the lock below proves the draft is the one prepared from.
        settings = (await read_settings(db)).as_settings()
        prepared = await self.validator.prepare(
            self.subject(entry_id, DRAFT, read.draft), Preparation(db, settings, ports)
        )
        async with db.begin() as conn:
            entry = await self.entry(conn, entry_id, lock=True)
            self.refuse(entry, entry_id, request.draft_revision)
            await pauses.pause(pauses.CATALOG_PUBLISH_LOCKED)
            context = ValidationContext(
                conn=conn,
                settings=(await read_settings(conn)).as_settings(),
                ports=ports,
                prepared=prepared,
            )
            report = await self.validator.validate(
                self.subject(entry_id, DRAFT, entry.draft), context
            )
            log_report(report, entity_type=self.entity_type.value, entry_id=entry_id)
            if not report.ok:
                raise PublishInvalid(
                    f"the draft of {self.noun} {entry_id} cannot be published",
                    {self.id_key: entry_id, "report": report.as_json()},
                )
            version = int(entry.current_version or 0) + 1
            draft_revision = int(entry.draft_revision) + 1
            now = clock.now()
            extra = await self.version_values(conn, entry.draft, report)
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

    async def set_listing_status(
        self, db: AsyncEngine, entry_id: str, value: str, *, principal: str
    ) -> listing.ListingStatusChange:
        """Set the entry's listing status to ``value`` (FR-CAT-11), and its followers' with it,
        in one transaction: 422 ``invalid_listing_status`` unless ``value`` is one of the
        deployment's values (read with the deployment row ``FOR SHARE``), 404 for an unknown
        entry, 422 ``companion_follows_character`` for a follower. Each entry whose value
        changes is updated (``updated_at`` too) and recorded as a ``listing_status`` ownership
        event by ``principal``, and the listing-status hooks run for it; the write records one
        ``control`` operation. Setting the entry's current value changes and records nothing (a
        follower out of step is left as it is then). Allowed whatever the entry's status."""
        async with db.begin() as conn:
            allowed = await listing.allowed_values(conn, lock=True)
            listing.refuse_unknown(value, allowed, field="body.value")
            await pauses.pause(pauses.CATALOG_LISTING_STATUS_LOCKED)
            entry = await self.entry(conn, entry_id, lock=True)
            leader = self.leader(entry)
            if leader is not None:
                raise CompanionFollowsCharacter(
                    f"{self.noun} {entry_id} is the companion of catalog character {leader}: its "
                    "listing status follows the character's; change it on the character",
                    {
                        "entity_type": self.entity_type.value,
                        "entity_id": entry_id,
                        "character_id": leader,
                    },
                )
            previous = str(entry.listing_status)
            changed = previous != value
            if changed:
                moved = [Follower(self.entity_type, self.catalog, entry_id, previous)]
                moved += await self.followers(conn, entry_id)
                now = clock.now()
                for item in moved:
                    if item.listing_status == value:
                        continue
                    await conn.execute(
                        update(item.table)
                        .where(item.table.c.id == item.id)
                        .values(listing_status=value, updated_at=now)
                    )
                    await ownership.record_event(
                        conn,
                        OwnershipEventKind.LISTING_STATUS,
                        item.entity_type,
                        item.id,
                        old=item.listing_status,
                        new=value,
                        principal=principal,
                    )
                    await listing.run_hooks(
                        conn, item.entity_type, item.id, item.listing_status, value, principal
                    )
                await control.record_control_operation(conn, actor=principal)
            revision = await config_revision(conn)
        return listing.ListingStatusChange(
            entity_type=self.entity_type,
            id=entry_id,
            listing_status=value,
            previous=previous,
            changed=changed,
            config_revision=revision,
        )

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
    "REVISION_MAX",
    "CardRating",
    "CatalogService",
    "Follower",
    "Projection",
    "PublishRequest",
    "Published",
    "Revision",
    "Shown",
    "ValidateRequest",
    "ValidationContext",
    "Validator",
    "VersionSummary",
    "config_revision",
    "merge_patch",
]
