"""Catalog characters: one draft per entry and immutable, numbered published versions
(FR-CAT-1,3,9; FR-SCN-10; docs/02 §4.10; #103).

An entry is a ``catalog.character_catalog`` row: its owner (the ``owner`` a creation names, else
the requesting principal; FR-AUTH-5), its listing status (``private`` until #110 makes the set
configurable), its status (``draft`` until the first publish, then ``published``, or
``archived``), its latest published version (``current_version``, ``None`` before the first
publish) and its one draft: a JSON document that ``CharacterDraft`` validates, with the
``draft_revision`` every save and publish compares (FR-CAT-3).

- **Create** stores the request's draft fields at draft revision 1, status ``draft``.
- **Save** applies a JSON merge patch (RFC 7386: a key sets a field, ``null`` removes it) to the
  draft, validates the result, caps it at 256 KiB of canonical JSON (413 ``draft_too_large``) and
  writes it with a compare-and-set on ``draft_revision``. A save based on an older revision is
  409 ``draft_stale`` (``details.current_draft_revision``) and changes nothing, so of two saves
  from the same revision exactly one succeeds. Every save bumps the revision, even one that
  changes nothing.
- **Publish** compares ``draft_revision`` the same way, under the entry's row lock, validates the
  draft (422 ``publish_invalid`` with the report: #103 needs a valid draft with a ``name``; #111
  and #115 add their rules), inserts version ``current_version + 1`` with the draft as its
  content, sets ``published`` and bumps the revision: the draft stays equal to what was published,
  and the next save needs the new revision. Publishing an unchanged draft still makes a version.
- **Versions are immutable**: a trigger refuses any UPDATE or DELETE of ``character_versions``
  (SQLSTATE ``EPI01``) unless the transaction set ``epistrel.erasure = on``, which only delete
  (and #124's erasure) does.
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
``full``, which adds the entry's envelope and every definition field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, control, ownership
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import (
    CharacterNotFound,
    DraftStale,
    DraftTooLarge,
    EntryArchived,
    InvalidRequest,
    NotPublished,
    PublishInvalid,
    field_path,
)
from epistrel.core.idempotency import canonical
from epistrel.core.ids import new_ulid
from epistrel.core.ownership import Owner
from epistrel.core.tables import deployment_state
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.catalog import ERASURE_GUC, character_catalog, character_versions
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses

ENTITY_TYPE = EntityType.CHARACTER.value
"""The entry type an audit row and ``core.ownership`` (#104) name."""
LISTING_STATUS = "private"
"""Every new entry's listing status until #110 makes the values configurable (FR-CAT-11)."""
FIRST_DRAFT_REVISION = 1
NAME_MAX = 120
PUBLIC_DESCRIPTION_MAX = 4_000
PERSONALITY_MAX = 4_000
DRAFT_MAX_BYTES = 256 * 1024
"""The largest draft, as canonical JSON (UTF-8 bytes)."""
REVISION_MAX = 2**31 - 1
"""``draft_revision`` and ``version`` are ``INTEGER`` columns: a larger value is a 422 at the
boundary, never a database error (#280's lesson)."""

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
Revision = Annotated[int, Field(ge=1, le=REVISION_MAX)]


# -- the draft document -----------------------------------------------------------------------


class CharacterDefinition(BaseModel):
    """The private character definition (FR-CAT-2): who the character is. It is seeded into
    every story the character joins and is otherwise returned only by the ``full`` projection.
    Until #115 adds the other fields it holds ``personality``."""

    model_config = ConfigDict(extra="forbid")

    personality: str | None = Field(
        default=None,
        max_length=PERSONALITY_MAX,
        description="Personality and temperament (private).",
    )


class CharacterDraft(BaseModel):
    """A catalog character's draft document (FR-CAT-2; interim until #115): the public card's
    ``name`` and ``public_description``, and the private ``definition``. A draft may lack any
    field; publishing needs a ``name``. A character carries no world truths of its own: those
    belong to a scenario, normally the character's companion scenario (FR-CAT-5)."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = Field(default=None, description="The character's name (public).")
    public_description: str | None = Field(
        default=None,
        max_length=PUBLIC_DESCRIPTION_MAX,
        description="Free text for the public card; never seeded into a story.",
    )
    definition: CharacterDefinition = Field(
        default_factory=CharacterDefinition,
        description="The private definition: returned only by the `full` projection.",
    )

    def document(self) -> dict[str, Any]:
        """The stored form: JSON, with the fields that are not set left out."""
        stored: dict[str, Any] = self.model_dump(
            mode="json", exclude_none=True, include=set(CharacterDraft.model_fields)
        )
        return stored


class CharacterCreate(CharacterDraft):
    """``POST /catalog/characters``: the new entry's first draft — any of the draft's fields —
    and, optionally, its ``owner``: an opaque principal, by default the requesting one, never
    ``system`` (FR-AUTH-5). Ownership confers no permission (FR-AUTH-8)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Mara Venn",
                    "public_description": "Keeps the light on Gull Rock.",
                    "definition": {"personality": "Patient, private and slow to trust."},
                }
            ]
        }
    )

    owner: Owner | None = Field(
        default=None,
        description="The entry's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )


class CharacterDefinitionPatch(BaseModel):
    """The ``definition`` part of a draft patch: a key sets that field, ``null`` removes it."""

    model_config = ConfigDict(extra="forbid")

    personality: str | None = Field(default=None, max_length=PERSONALITY_MAX)


class CharacterPatch(BaseModel):
    """``PATCH /catalog/characters/{id}``: a JSON merge patch (RFC 7386) over the draft — a key
    sets that field, ``null`` removes it, and ``definition`` merges the same way — with the
    ``draft_revision`` it is based on (FR-CAT-3)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "draft_revision": 1,
                    "public_description": "Keeps the light on Gull Rock, and the logbook.",
                    "definition": {"personality": "Patient and wry."},
                }
            ]
        },
    )

    draft_revision: Revision = Field(
        description="The draft revision this patch is based on; a stale one is 409 `draft_stale`."
    )
    name: Name | None = None
    public_description: str | None = Field(default=None, max_length=PUBLIC_DESCRIPTION_MAX)
    definition: CharacterDefinitionPatch | None = None

    def document(self) -> dict[str, Any]:
        """The merge patch itself: the fields the request set (``null`` included), without
        ``draft_revision``."""
        patch: dict[str, Any] = self.model_dump(
            mode="json", exclude_unset=True, exclude={"draft_revision"}
        )
        return patch


class PublishRequest(BaseModel):
    """``POST /catalog/characters/{id}/publish``: the draft revision being published
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


def _validated(document: Any) -> dict[str, Any]:
    """``document`` as a stored draft; 422 ``validation_error`` at ``body.<field>``."""
    try:
        draft = CharacterDraft.model_validate(document)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None
    return draft.document()


def _check_size(document: Mapping[str, Any]) -> None:
    size = len(canonical(document).encode())
    if size > DRAFT_MAX_BYTES:
        raise DraftTooLarge(
            f"the draft would be {size} bytes; the limit is {DRAFT_MAX_BYTES}",
            {"size": size, "limit": DRAFT_MAX_BYTES},
        )


# -- projections (FR-SCN-10) ------------------------------------------------------------------


class Projection(StrEnum):
    """A catalog read's projection (FR-SCN-10): ``card`` (the default) or ``full``."""

    CARD = "card"
    FULL = "full"


class CardImage(BaseModel):
    """An image on a public card (FR-CAT-4; #105, #115): the media asset and its alt text."""

    model_config = ConfigDict(extra="forbid")

    media_id: str
    alt_text: str | None = None


class CardRating(BaseModel):
    """The content rating on a public card (FR-RATE-3; #115): the rating, the version the shown
    version was published under, and the rating's name."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version_id: str
    name: str


class CharacterCard(BaseModel):
    """A catalog character's public card (``projection=card``, the default; FR-CAT-2,
    FR-SCN-10): only what a player-facing client may show, never a definition field.
    ``version`` is the version shown; a draft read shows the draft with the latest published
    version (``null`` before the first publish). Until #115, ``images`` and ``labels`` are empty
    and ``content_rating`` is ``null``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int | None
    name: str | None
    public_description: str | None
    images: list[CardImage]
    labels: list[str]
    content_rating: CardRating | None
    listing_status: str
    status: EntryStatus


class CharacterFull(BaseModel):
    """A catalog character in full (``projection=full``; FR-SCN-10): the entry's envelope, the
    public card's fields and the private ``definition``. ``draft_revision`` is present only on a
    draft read (``?draft=true``): the revision the next save or publish must send.
    ``published_at`` is when ``version`` was published; ``companion_id`` names the character's
    companion scenario (#116; ``null`` until then). ``owner_principal`` is the entry's owner
    (FR-AUTH-5), recorded for listing and attribution only."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: EntryStatus
    listing_status: str
    owner_principal: str
    version: int | None
    current_version: int | None
    draft_revision: int | None = Field(default=None, exclude_if=lambda revision: revision is None)
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    companion_id: str | None
    name: str | None
    public_description: str | None
    images: list[CardImage]
    labels: list[str]
    content_rating: CardRating | None
    definition: CharacterDefinition


def _project(
    entry: Any,
    document: Mapping[str, Any],
    *,
    version: int | None,
    published_at: datetime | None,
    draft_revision: int | None,
    projection: Projection,
) -> CharacterCard | CharacterFull:
    card: dict[str, Any] = {
        "id": entry.id,
        "version": version,
        "name": document.get("name"),
        "public_description": document.get("public_description"),
        "images": [],
        "labels": [],
        "content_rating": None,
        "listing_status": entry.listing_status,
        "status": EntryStatus(entry.status),
    }
    if projection is Projection.CARD:
        return CharacterCard(**card)
    return CharacterFull(
        **card,
        owner_principal=entry.owner_principal,
        current_version=entry.current_version,
        draft_revision=draft_revision,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        published_at=published_at,
        companion_id=None,
        definition=CharacterDefinition.model_validate(document.get("definition") or {}),
    )


# -- results ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftWrite:
    """A created or saved draft (the ``full`` draft read) and the deployment's
    ``config_revision``, which catalog writes leave as it is."""

    character: CharacterFull
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class ReportEntry:
    """One finding of the publish validation: a stable ``code``, the draft ``field`` it concerns
    (a dotted path) and a message."""

    code: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class PublishReport:
    """What the publish validation found (#111 replaces the builder): ``errors`` stop a publish;
    ``unavailable`` names checks that could not run."""

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
class Envelope:
    """An entry without a document: what archive and unarchive answer with."""

    id: str
    status: EntryStatus
    listing_status: str
    current_version: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    companion_id: str | None = None


@dataclass(frozen=True, slots=True)
class StatusChange:
    """The entry after archive or unarchive; ``changed`` is false when there was nothing to do."""

    character: Envelope
    changed: bool
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class DeletedCharacter:
    """A deleted entry and how many published versions went with it."""

    id: str
    versions: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class VersionSummary:
    """One published version: its number, when and by whom it was published."""

    version: int
    published_at: datetime
    published_by: str


# -- rows -------------------------------------------------------------------------------------

_ENTRY = (
    character_catalog.c.id,
    character_catalog.c.owner_principal,
    character_catalog.c.listing_status,
    character_catalog.c.status,
    character_catalog.c.current_version,
    character_catalog.c.draft,
    character_catalog.c.draft_revision,
    character_catalog.c.created_at,
    character_catalog.c.updated_at,
)


def _not_found(character_id: str, version: int | None = None) -> CharacterNotFound:
    if version is None:
        return CharacterNotFound(
            f"catalog character {character_id} does not exist", {"character_id": character_id}
        )
    return CharacterNotFound(
        f"catalog character {character_id} has no version {version}",
        {"character_id": character_id, "version": version},
    )


async def _entry(conn: AsyncConnection, character_id: str, *, lock: bool = False) -> Any:
    query = select(*_ENTRY).where(character_catalog.c.id == character_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise _not_found(character_id)
    return row


async def _published_at(
    conn: AsyncConnection, character_id: str, version: int | None
) -> datetime | None:
    if version is None:
        return None
    found: datetime | None = await conn.scalar(
        select(character_versions.c.published_at).where(
            character_versions.c.catalog_id == character_id,
            character_versions.c.version == version,
        )
    )
    return found


async def _config_revision(conn: AsyncConnection) -> int:
    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


def _conflict(entry: Any, character_id: str, draft_revision: int) -> EntryArchived | DraftStale:
    """Why a save or publish based on ``draft_revision`` cannot change ``entry``: 422
    ``entry_archived`` for an archived entry, else 409 ``draft_stale``."""
    if entry.status == EntryStatus.ARCHIVED.value:
        return EntryArchived(
            f"catalog character {character_id} is archived; unarchive it to change it",
            {"character_id": character_id},
        )
    return DraftStale(
        f"the draft of catalog character {character_id} is at revision "
        f"{entry.draft_revision}, not {draft_revision}; read it again and retry",
        {
            "character_id": character_id,
            "draft_revision": draft_revision,
            "current_draft_revision": int(entry.draft_revision),
        },
    )


def _refuse(entry: Any, character_id: str, draft_revision: int) -> None:
    """Raise ``_conflict`` unless ``entry`` is editable at ``draft_revision``."""
    if entry.status == EntryStatus.ARCHIVED.value or entry.draft_revision != draft_revision:
        raise _conflict(entry, character_id, draft_revision)


async def _draft_write(conn: AsyncConnection, character_id: str) -> DraftWrite:
    entry = await _entry(conn, character_id)
    character = _project(
        entry,
        entry.draft,
        version=entry.current_version,
        published_at=await _published_at(conn, character_id, entry.current_version),
        draft_revision=int(entry.draft_revision),
        projection=Projection.FULL,
    )
    assert isinstance(character, CharacterFull)  # noqa: S101 - asked for the full projection
    return DraftWrite(character=character, config_revision=await _config_revision(conn))


async def _envelope(conn: AsyncConnection, entry: Any) -> Envelope:
    return Envelope(
        id=entry.id,
        status=EntryStatus(entry.status),
        listing_status=entry.listing_status,
        current_version=entry.current_version,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        published_at=await _published_at(conn, entry.id, entry.current_version),
    )


# -- writes -----------------------------------------------------------------------------------


async def create(db: AsyncEngine, request: CharacterCreate, *, principal: str) -> DraftWrite:
    """A new entry with ``request``'s draft fields at draft revision 1 (status ``draft``,
    listing status ``private``), owned by ``request.owner`` or else ``principal``."""
    document = request.document()
    _check_size(document)
    character_id = new_ulid()
    now = clock.now()
    async with db.begin() as conn:
        await conn.execute(
            insert(character_catalog).values(
                id=character_id,
                owner_principal=request.owner or principal,
                listing_status=LISTING_STATUS,
                status=EntryStatus.DRAFT.value,
                current_version=None,
                draft=document,
                draft_revision=FIRST_DRAFT_REVISION,
                created_at=now,
                updated_at=now,
            )
        )
        await control.record_control_operation(conn, actor=principal)
        return await _draft_write(conn, character_id)


async def save(
    db: AsyncEngine, character_id: str, patch: CharacterPatch, *, principal: str
) -> DraftWrite:
    """Apply ``patch`` to the draft (see the module docstring): 404 for an unknown entry, 422
    ``entry_archived``, 409 ``draft_stale``, 422 for an invalid result, 413 for a too large one."""
    async with db.connect() as conn:
        entry = await _entry(conn, character_id)
    _refuse(entry, character_id, patch.draft_revision)
    document = _validated(merge_patch(entry.draft, patch.document()))
    _check_size(document)
    await pauses.pause(pauses.CATALOG_SAVE_BEFORE_CAS)
    async with db.begin() as conn:
        saved = await conn.scalar(
            update(character_catalog)
            .where(
                character_catalog.c.id == character_id,
                character_catalog.c.draft_revision == patch.draft_revision,
                character_catalog.c.status != EntryStatus.ARCHIVED.value,
            )
            .values(
                draft=document,
                draft_revision=character_catalog.c.draft_revision + 1,
                updated_at=clock.now(),
            )
            .returning(character_catalog.c.id)
        )
        if saved is None:
            # Another save, publish or archive committed since the read (or the entry is gone).
            raise _conflict(await _entry(conn, character_id), character_id, patch.draft_revision)
        await control.record_control_operation(conn, actor=principal)
        return await _draft_write(conn, character_id)


def publish_report(document: Any) -> PublishReport:
    """What stands in the way of publishing ``document``: it must be a valid draft
    (``CharacterDraft``), and it must name the character. #111 replaces this builder and #115
    adds the content rules (true age, content rating, images)."""
    try:
        draft = CharacterDraft.model_validate(document)
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
    if draft.name is None:
        return PublishReport(
            errors=(
                ReportEntry(
                    code="field_required",
                    field="name",
                    message="a published character needs a name",
                ),
            )
        )
    return PublishReport()


async def publish(
    db: AsyncEngine, character_id: str, request: PublishRequest, *, principal: str
) -> Published:
    """Publish the draft as version ``current_version + 1`` (see the module docstring)."""
    async with db.begin() as conn:
        entry = await _entry(conn, character_id, lock=True)
        _refuse(entry, character_id, request.draft_revision)
        await pauses.pause(pauses.CATALOG_PUBLISH_LOCKED)
        report = publish_report(entry.draft)
        if report.errors:
            raise PublishInvalid(
                f"the draft of catalog character {character_id} cannot be published",
                {"character_id": character_id, "report": report.as_json()},
            )
        version = int(entry.current_version or 0) + 1
        draft_revision = int(entry.draft_revision) + 1
        now = clock.now()
        await conn.execute(
            insert(character_versions).values(
                catalog_id=character_id,
                version=version,
                content=entry.draft,
                # The draft names no content rating until #115 (``metadata.content_rating_id``).
                content_rating_version_id=None,
                published_at=now,
                published_by=principal,
            )
        )
        # The answer reports the status the entry row now holds, not the one intended.
        stored_status = await conn.scalar(
            update(character_catalog)
            .where(character_catalog.c.id == character_id)
            .values(
                status=EntryStatus.PUBLISHED.value,
                current_version=version,
                draft_revision=draft_revision,
                updated_at=now,
            )
            .returning(character_catalog.c.status)
        )
        await control.record_control_operation(conn, actor=principal)
        config_revision = await _config_revision(conn)
    return Published(
        id=character_id,
        version=version,
        draft_revision=draft_revision,
        status=EntryStatus(stored_status),
        published_at=now,
        report=report,
        config_revision=config_revision,
    )


async def archive(db: AsyncEngine, character_id: str, *, principal: str) -> StatusChange:
    """Make the entry ``archived``; an archived entry is left as it is (``changed`` false)."""
    return await _set_archived(db, character_id, True, principal=principal)


async def unarchive(db: AsyncEngine, character_id: str, *, principal: str) -> StatusChange:
    """Restore an archived entry to ``published`` (``draft`` if it was never published); any
    other entry is left as it is (``changed`` false)."""
    return await _set_archived(db, character_id, False, principal=principal)


async def _set_archived(
    db: AsyncEngine, character_id: str, archived: bool, *, principal: str
) -> StatusChange:
    async with db.begin() as conn:
        entry = await _entry(conn, character_id, lock=True)
        if (entry.status == EntryStatus.ARCHIVED.value) == archived:
            return StatusChange(
                character=await _envelope(conn, entry),
                changed=False,
                config_revision=await _config_revision(conn),
            )
        if archived:
            status = EntryStatus.ARCHIVED
        elif entry.current_version is not None:
            status = EntryStatus.PUBLISHED
        else:
            status = EntryStatus.DRAFT
        await conn.execute(
            update(character_catalog)
            .where(character_catalog.c.id == character_id)
            .values(status=status.value, updated_at=clock.now())
        )
        await control.record_control_operation(conn, actor=principal)
        return StatusChange(
            character=await _envelope(conn, await _entry(conn, character_id)),
            changed=True,
            config_revision=await _config_revision(conn),
        )


async def delete(db: AsyncEngine, character_id: str, *, principal: str) -> DeletedCharacter:
    """Remove the entry and its versions (from any status) and write a
    ``catalog_entry_deleted`` audit row; 404 for an unknown entry. Its relations go too, recorded
    as a ``deleted`` ownership event (#104). Stories created from it keep their snapshot and
    provenance (FR-CAT-9)."""
    async with db.begin() as conn:
        await _entry(conn, character_id, lock=True)
        await ownership.drop_entity(conn, EntityType.CHARACTER, character_id, principal=principal)
        versions = await conn.scalar(
            select(func.count())
            .select_from(character_versions)
            .where(character_versions.c.catalog_id == character_id)
        )
        # The one exemption the versions' immutability trigger knows (shared with #124).
        await conn.execute(select(func.set_config(ERASURE_GUC, "on", True)))
        await conn.execute(
            sql_delete(character_catalog).where(character_catalog.c.id == character_id)
        )
        operation_id = await control.record_control_operation(conn, actor=principal)
        await audit.record(
            conn,
            AuditKind.CATALOG_ENTRY_DELETED,
            principal=principal,
            details={
                "entity_type": ENTITY_TYPE,
                "entity_id": character_id,
                "operation_id": operation_id,
            },
        )
    return DeletedCharacter(id=character_id, versions=int(versions or 0))


# -- reads ------------------------------------------------------------------------------------


async def get(
    db: AsyncEngine,
    character_id: str,
    *,
    version: int | None = None,
    draft: bool = False,
    projection: Projection = Projection.CARD,
) -> CharacterCard | CharacterFull:
    """The latest published version, version ``version``, or (``draft``) the draft, as
    ``projection``. 404 for an unknown entry or version; 422 ``not_published`` for the published
    character of an entry never published; 422 when both ``version`` and ``draft`` are given."""
    if draft and version is not None:
        raise InvalidRequest(
            "read one version (`version`) or the draft (`draft=true`), not both",
            {"field": "query.version"},
        )
    async with db.connect() as conn:
        entry = await _entry(conn, character_id)
        if draft:
            return _project(
                entry,
                entry.draft,
                version=entry.current_version,
                published_at=await _published_at(conn, character_id, entry.current_version),
                draft_revision=int(entry.draft_revision),
                projection=projection,
            )
        wanted = version if version is not None else entry.current_version
        if wanted is None:
            raise NotPublished(
                f"catalog character {character_id} has not been published; read its draft",
                {"character_id": character_id},
            )
        found = (
            await conn.execute(
                select(character_versions.c.content, character_versions.c.published_at).where(
                    character_versions.c.catalog_id == character_id,
                    character_versions.c.version == wanted,
                )
            )
        ).one_or_none()
    if found is None:
        raise _not_found(character_id, wanted)
    return _project(
        entry,
        found.content,
        version=wanted,
        published_at=found.published_at,
        draft_revision=None,
        projection=projection,
    )


async def versions(db: AsyncEngine, character_id: str) -> list[VersionSummary]:
    """Every published version of the entry, oldest first; 404 for an unknown entry."""
    async with db.connect() as conn:
        await _entry(conn, character_id)
        found = await conn.execute(
            select(
                character_versions.c.version,
                character_versions.c.published_at,
                character_versions.c.published_by,
            )
            .where(character_versions.c.catalog_id == character_id)
            .order_by(character_versions.c.version)
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
    "DRAFT_MAX_BYTES",
    "ENTITY_TYPE",
    "LISTING_STATUS",
    "REVISION_MAX",
    "CardImage",
    "CardRating",
    "CharacterCard",
    "CharacterCreate",
    "CharacterDefinition",
    "CharacterDefinitionPatch",
    "CharacterDraft",
    "CharacterFull",
    "CharacterPatch",
    "DeletedCharacter",
    "DraftWrite",
    "Envelope",
    "Projection",
    "PublishReport",
    "PublishRequest",
    "Published",
    "ReportEntry",
    "StatusChange",
    "VersionSummary",
    "archive",
    "create",
    "delete",
    "get",
    "merge_patch",
    "publish",
    "publish_report",
    "save",
    "unarchive",
    "versions",
]
