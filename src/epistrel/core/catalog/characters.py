"""Catalog characters: one draft per entry and immutable, numbered published versions
(FR-CAT-1..5,9,10, FR-CAT-12, FR-RATE-3; FR-SCN-10; docs/02 §4.10; #103, #115, #116).

An entry is a ``catalog.character_catalog`` row; its draft is a JSON document that
``CharacterDraft`` validates (``core.catalog.models``: the public card, the private definition,
the metadata and the visual identity), and its versions are ``catalog.character_versions``
rows. The lifecycle — create, save (a JSON merge patch compared on ``draft_revision``), publish,
archive, unarchive, delete, and the reads in the ``card`` and ``full`` projections — is the
generic catalog service's (``core.catalog.service``, #108), which scenarios share; this module
supplies the character's document checks and its projections.

**Saving** stores what validates as a draft (a name alone is enough) and refuses an alias equal
to the name or to another alias and a reference image without an audience (422 at the field). A
save also checks what it adds, as a scenario's does: a rating must be assignable (422
``rating_retired``) and each image and reference image live (422 ``media_retired``).
**Publishing** runs the catalog validation (``core.catalog.validation``, #111): the draft must be
a valid ``CharacterDraft`` holding everything ``models.PUBLISH_REQUIRED`` names, a true age, a
content rating that can be assigned, an audience on every reference image, a primary image when
the deployment's ``require_primary_image`` is on, and live images (422 ``publish_invalid`` with
``details.report`` naming each missing field). A published version records its rating's
version. A character carries no world truths of its own: those belong to a scenario, normally
the character's companion scenario (FR-CAT-5).

**The companion** (``core.catalog.companion``, #116): every character is created with its
companion scenario, which is archived, unarchived and deleted with it and takes its rating
until the companion's own is set. Publishing the character makes a companion version in the
same transaction, pinned to the new character version: a copy of the latest published
companion content (the defaults on the first publish), or, with ``with_companion``, the
companion's draft; ``get_companion`` reads it.

A read returns one projection (FR-SCN-10): ``card`` (the default), only the public card (the
images as ``{primary, gallery}`` of ``{media_id, alt_text}``, the labels and the rating), or
``full``, which adds the entry's envelope, the aliases, every definition field and the visual
identity, with each image's and reference image's live media row inlined. Both show the rating
the version recorded, or for a draft the rating's current version. A listing
(``core.catalog.search``, #110) shows each entry as a ``CharacterListItem``: its card, its owner
and ``updated_at``. A character's companion (``scenario_catalog.companion_of``, #116) follows
its listing status (FR-CAT-11) and its owner (FR-AUTH-5); ``full`` names it (``companion_id``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog import companion
from epistrel.core.catalog.checks.character_documents import (
    images_of,
    rating_id_of,
    reference_images_of,
)
from epistrel.core.catalog.companion import CharacterPublished, CompanionPublished
from epistrel.core.catalog.models import (
    BACKGROUND_FLAVOR_MAX,
    GALLERY_MAX,
    NAME_MAX,
    PERSONALITY_MAX,
    PUBLIC_DESCRIPTION_MAX,
    SAFE_FALLBACK_MAX,
    Audience,
    CardImage,
    CharacterCard,
    CharacterCardImages,
    CharacterCreate,
    CharacterDefinition,
    CharacterDefinitionPatch,
    CharacterDraft,
    CharacterFull,
    CharacterImageDetail,
    CharacterImages,
    CharacterImagesDetail,
    CharacterImagesPatch,
    CharacterListItem,
    CharacterMetadata,
    CharacterMetadataPatch,
    CharacterPatch,
    CharacterPublicCard,
    CharacterPublicCardPatch,
    CharacterPublishRequest,
    Name,
    ReferenceImageDetail,
    VisualIdentityDetail,
    audience_errors,
    character_rules,
)
from epistrel.core.catalog.report import ValidationReport
from epistrel.core.catalog.scenario_models import ScenarioCard, ScenarioFull
from epistrel.core.catalog.service import (
    FIRST_DRAFT_REVISION,
    REVISION_MAX,
    CardRating,
    CatalogService,
    Follower,
    Projection,
    Published,
    PublishRequest,
    Shown,
    VersionSummary,
    card_rating,
    merge_patch,
)
from epistrel.core.catalog.validation import RUNNER
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import CharacterNotFound, InvalidRequest
from epistrel.core.media.assets import assert_referenceable, rating_version
from epistrel.core.ports import Ports
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import media_assets
from epistrel.core.tables.catalog import character_catalog, character_versions, scenario_catalog
from epistrel.core.tables.ownership import EntityType

ENTITY_TYPE = EntityType.CHARACTER.value
"""The entry type an audit row and ``core.ownership`` (#104) name."""
DRAFT_MAX_BYTES = 256 * 1024
"""The largest draft, as canonical JSON (UTF-8 bytes)."""


def _validated(document: Any) -> dict[str, Any]:
    """``document`` as a stored draft: 422 ``validation_error`` at ``body.<field>`` for what the
    model refuses, then for what the save rules refuse (an alias that is the name or another
    alias; a reference image without an audience), every finding listed."""
    try:
        draft = CharacterDraft.model_validate(document)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None
    refused = [*character_rules(draft), *audience_errors(draft)]
    if refused:
        raise InvalidRequest.from_errors(refused, prefix=("body",))
    return draft.document()


# -- projections (FR-SCN-10) ------------------------------------------------------------------


def _group(document: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    found: Any = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found if isinstance(found, Mapping) else {}


async def _media_rows(conn: AsyncConnection, media_ids: Sequence[str]) -> dict[str, Any]:
    """The live media rows of ``media_ids`` (one query), by id; an id no row holds is absent."""
    wanted = sorted(set(media_ids))
    if not wanted:
        return {}
    rows = await conn.execute(
        select(
            media_assets.c.id,
            media_assets.c.alt_text,
            media_assets.c.caption,
            media_assets.c.mime_type,
            media_assets.c.width,
            media_assets.c.height,
            media_assets.c.generation_metadata,
        ).where(media_assets.c.id.in_(wanted))
    )
    return {str(row.id): row for row in rows}


def _card_image(media_id: str, rows: Mapping[str, Any]) -> CardImage:
    row = rows.get(media_id)
    return CardImage(media_id=media_id, alt_text=None if row is None else row.alt_text)


def _detail(media_id: str, rows: Mapping[str, Any]) -> dict[str, Any]:
    """An image's media row as ``full`` inlines it (every field ``null`` without a row)."""
    row = rows.get(media_id)
    if row is None:
        return {"media_id": media_id}
    return {
        "media_id": media_id,
        "alt_text": row.alt_text,
        "caption": row.caption,
        "mime_type": row.mime_type,
        "width": row.width,
        "height": row.height,
        "generation_metadata": row.generation_metadata,
    }


def _ids(images: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """The primary image's id and the gallery's, from a stored ``public_card.images``."""
    primary = images.get("primary")
    gallery = images.get("gallery")
    return (
        primary if isinstance(primary, str) else None,
        [m for m in (gallery if isinstance(gallery, list) else []) if isinstance(m, str)],
    )


async def card_images(conn: AsyncConnection, document: Mapping[str, Any]) -> CharacterCardImages:
    """The images a character's card shows: the primary image and the gallery, each with its
    asset's alt text (``null`` for an asset that is gone)."""
    primary, gallery = _ids(_group(document, "public_card", "images"))
    rows = await _media_rows(conn, [m for m in (primary, *gallery) if m is not None])
    return CharacterCardImages(
        primary=None if primary is None else _card_image(primary, rows),
        gallery=[_card_image(media_id, rows) for media_id in gallery],
    )


def _audience(value: Any) -> Audience | None:
    return Audience(value) if value in {a.value for a in Audience} else None


async def _full_media(
    conn: AsyncConnection, document: Mapping[str, Any]
) -> tuple[CharacterImagesDetail, VisualIdentityDetail | None]:
    """``full``'s images and visual identity: each image and reference image with its live
    media row (alt text, caption, MIME type, dimensions, generation metadata as stored)."""
    primary, gallery = _ids(_group(document, "public_card", "images"))
    identity = document.get("visual_identity")
    references = [
        item
        for item in (identity.get("reference_images") if isinstance(identity, Mapping) else None)
        or []
        if isinstance(item, Mapping) and isinstance(item.get("media_id"), str)
    ]
    rows = await _media_rows(
        conn,
        [
            *(m for m in (primary, *gallery) if m is not None),
            *(str(item["media_id"]) for item in references),
        ],
    )
    images = CharacterImagesDetail(
        primary=None if primary is None else CharacterImageDetail(**_detail(primary, rows)),
        gallery=[CharacterImageDetail(**_detail(media_id, rows)) for media_id in gallery],
    )
    if not isinstance(identity, Mapping):
        return images, None
    shown = {key: value for key, value in identity.items() if key != "reference_images"}
    return images, VisualIdentityDetail(
        **shown,
        reference_images=[
            ReferenceImageDetail(
                **_detail(str(item["media_id"]), rows), audience=_audience(item.get("audience"))
            )
            for item in references
        ],
    )


def _strings(values: Any) -> list[str]:
    return [v for v in (values if isinstance(values, list) else []) if isinstance(v, str)]


async def _project(
    conn: AsyncConnection,
    entry: Any,
    document: Mapping[str, Any],
    *,
    version: int | None,
    published_at: datetime | None,
    draft_revision: int | None,
    projection: Projection,
    version_row: Any | None,
) -> CharacterCard | CharacterFull:
    public_card = _group(document, "public_card")
    metadata = _group(document, "metadata")
    shown: dict[str, Any] = {
        "id": entry.id,
        "version": version,
        "name": public_card.get("name"),
        "public_description": public_card.get("public_description"),
        "labels": _strings(metadata.get("labels")),
        "content_rating": await card_rating(conn, rating_id_of(document), version_row),
        "listing_status": entry.listing_status,
        "status": EntryStatus(entry.status),
    }
    if projection is Projection.CARD:
        return CharacterCard(**shown, images=await card_images(conn, document))
    images, identity = await _full_media(conn, document)
    return CharacterFull(
        **shown,
        images=images,
        aliases=_strings(metadata.get("aliases")),
        owner_principal=entry.owner_principal,
        current_version=entry.current_version,
        draft_revision=draft_revision,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        published_at=published_at,
        companion_id=await companion.companion_id(conn, entry.id),
        definition=CharacterDefinition.model_validate(document.get("definition") or {}),
        visual_identity=identity,
    )


async def version_card(conn: AsyncConnection, entry: Any, version_row: Any) -> CharacterCard:
    """The public card of a published ``version_row`` of catalog character ``entry`` (its
    ``id``, ``listing_status`` and ``status``): what a ``card`` read of that version shows. A
    story's creation report shows its catalog cast this way (#122)."""
    document: Mapping[str, Any] = (
        version_row.content if isinstance(version_row.content, Mapping) else {}
    )
    found = await _project(
        conn,
        entry,
        document,
        version=int(version_row.version),
        published_at=version_row.published_at,
        draft_revision=None,
        projection=Projection.CARD,
        version_row=version_row,
    )
    assert isinstance(found, CharacterCard)  # noqa: S101 - the card projection
    return found


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


# -- the service -------------------------------------------------------------------------------


class CharacterCatalog(CatalogService):
    """Catalog characters on the generic catalog service."""

    entity_type = EntityType.CHARACTER
    noun = "catalog character"
    id_key = "character_id"
    catalog = character_catalog
    versions = character_versions
    not_found_error = CharacterNotFound
    list_item = CharacterListItem
    document_model = CharacterDraft

    def max_bytes(self) -> int:
        return DRAFT_MAX_BYTES  # read at call time, so a test may lower it

    async def created(
        self,
        conn: AsyncConnection,
        entry_id: str,
        document: Mapping[str, Any],
        *,
        owner: str,
        listing_status: str,
    ) -> None:
        """A character is created with its companion scenario (FR-CAT-5; #116)."""
        await companion.create(conn, entry_id, document, owner=owner, listing_status=listing_status)

    async def saved(
        self,
        conn: AsyncConnection,
        entry_id: str,
        document: Mapping[str, Any],
        previous: Mapping[str, Any],
    ) -> None:
        """A changed content rating reaches the companion's draft (until set there; #116)."""
        await companion.follow_rating(conn, entry_id, document, previous)

    async def archived(self, conn: AsyncConnection, entry_id: str, archived: bool) -> None:
        """The companion is archived and unarchived with its character (FR-CAT-5; #116)."""
        await companion.set_archived(conn, entry_id, archived)

    async def dropped(
        self, conn: AsyncConnection, entry_id: str, *, principal: str
    ) -> list[tuple[EntityType, str]]:
        """The companion is deleted with its character (FR-CAT-5; #116)."""
        return await companion.drop(conn, entry_id, principal=principal)

    async def publish(
        self,
        db: AsyncEngine,
        entry_id: str,
        request: PublishRequest,
        *,
        principal: str,
        ports: Ports | None = None,
        with_companion: bool = False,
    ) -> CharacterPublished:
        """Publish the draft, and a companion version with it (FR-CAT-10;
        ``core.catalog.companion``)."""
        return await companion.publish(
            self,
            db,
            entry_id,
            request,
            with_companion=with_companion,
            principal=principal,
            ports=ports or Ports(),
        )

    async def followers(self, conn: AsyncConnection, entry_id: str) -> list[Follower]:
        """The character's companion scenarios (``companion_of``; #116 creates them), locked
        after the character, in id order: their listing status follows the character's."""
        rows = await conn.execute(
            select(scenario_catalog.c.id, scenario_catalog.c.listing_status)
            .where(scenario_catalog.c.companion_of == entry_id)
            .order_by(scenario_catalog.c.id)
            .with_for_update()
        )
        return [
            Follower(EntityType.SCENARIO, scenario_catalog, str(row.id), str(row.listing_status))
            for row in rows
        ]

    def stored(self, document: Any) -> dict[str, Any]:
        return _validated(document)

    async def checked(
        self, conn: AsyncConnection, document: Mapping[str, Any], previous: Any
    ) -> None:
        """What a create or save adds must be usable: a content rating it sets must be
        assignable (422 ``rating_retired``) and each image and reference image it adds live (422
        ``media_retired``, held ``FOR SHARE`` until the write commits), as a scenario's save
        checks them. An asset retired since it was added does not block unrelated edits; a
        publish reports it (``media_retired``)."""
        rating = rating_id_of(document)
        if rating is not None and rating != rating_id_of(previous):
            await require_assignable(conn, rating, field="body.metadata.content_rating_id")
        before = {
            media_id for _, media_id in (*images_of(previous), *reference_images_of(previous))
        }
        for field, media_id in (*images_of(document), *reference_images_of(document)):
            if media_id not in before:
                await assert_referenceable(conn, [media_id], field=f"body.{field}")

    async def version_values(
        self, conn: AsyncConnection, document: Mapping[str, Any], report: ValidationReport
    ) -> dict[str, Any]:
        """A version records the version of the content rating it was published under."""
        rating = rating_id_of(document)
        return {
            "content_rating_version_id": None
            if rating is None
            else await rating_version(conn, rating)
        }

    async def project(self, conn: AsyncConnection, shown: Shown, projection: Projection) -> Any:
        return await _project(
            conn,
            shown.entry,
            shown.document,
            version=shown.version,
            published_at=shown.published_at,
            draft_revision=shown.draft_revision,
            projection=projection,
            version_row=shown.version_row,
        )

    async def envelope(self, conn: AsyncConnection, entry: Any) -> Envelope:
        return Envelope(
            id=entry.id,
            status=EntryStatus(entry.status),
            listing_status=entry.listing_status,
            current_version=entry.current_version,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            published_at=await self.published_at(conn, entry.id, entry.current_version),
            companion_id=await companion.companion_id(conn, entry.id),
        )


SERVICE = CharacterCatalog(RUNNER)
"""The character catalog's service, publishing through the catalog validation (#111)."""


# -- writes -----------------------------------------------------------------------------------


async def create(db: AsyncEngine, request: CharacterCreate, *, principal: str) -> DraftWrite:
    """A new entry with ``request``'s draft fields at draft revision 1 (status ``draft``, at
    the deployment's first listing status value), owned by ``request.owner`` or else
    ``principal``."""
    character, revision = await SERVICE.create(
        db, request.document(), owner=request.owner or principal, principal=principal
    )
    return DraftWrite(character=character, config_revision=revision)


async def save(
    db: AsyncEngine, character_id: str, patch: CharacterPatch, *, principal: str
) -> DraftWrite:
    """Apply ``patch`` to the draft (see ``core.catalog.service``): 404 for an unknown entry,
    422 ``entry_archived``, 409 ``draft_stale``, 422 for an invalid result, 413 for a too large
    one."""
    character, revision = await SERVICE.save(
        db, character_id, patch.document(), patch.draft_revision, principal=principal
    )
    return DraftWrite(character=character, config_revision=revision)


async def publish(
    db: AsyncEngine,
    character_id: str,
    request: PublishRequest,
    *,
    principal: str,
    ports: Ports | None = None,
    with_companion: bool = False,
) -> CharacterPublished:
    """Publish the draft as version ``current_version + 1``, and a companion version with it:
    the latest published companion content (the defaults on the first publish), or with
    ``with_companion`` the companion's draft (``request.companion_draft_revision``), pinned to
    the new version, in one transaction (``core.catalog.companion``)."""
    return await SERVICE.publish(
        db,
        character_id,
        request,
        principal=principal,
        ports=ports,
        with_companion=with_companion,
    )


async def archive(db: AsyncEngine, character_id: str, *, principal: str) -> StatusChange:
    """Make the entry ``archived``; an archived entry is left as it is (``changed`` false)."""
    envelope, changed, revision = await SERVICE.set_archived(
        db, character_id, True, principal=principal
    )
    return StatusChange(character=envelope, changed=changed, config_revision=revision)


async def unarchive(db: AsyncEngine, character_id: str, *, principal: str) -> StatusChange:
    """Restore an archived entry to ``published`` (``draft`` if it was never published); any
    other entry is left as it is (``changed`` false)."""
    envelope, changed, revision = await SERVICE.set_archived(
        db, character_id, False, principal=principal
    )
    return StatusChange(character=envelope, changed=changed, config_revision=revision)


async def delete(db: AsyncEngine, character_id: str, *, principal: str) -> DeletedCharacter:
    """Remove the entry and its versions (from any status) and write a
    ``catalog_entry_deleted`` audit row; 404 for an unknown entry. Its relations go too, recorded
    as a ``deleted`` ownership event (#104). Stories created from it keep their snapshot and
    provenance (FR-CAT-9)."""
    versions_removed = await SERVICE.delete(db, character_id, principal=principal)
    return DeletedCharacter(id=character_id, versions=versions_removed)


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
    found: CharacterCard | CharacterFull = await SERVICE.get(
        db, character_id, version=version, draft=draft, projection=projection
    )
    return found


async def versions(db: AsyncEngine, character_id: str) -> list[VersionSummary]:
    """Every published version of the entry, oldest first; 404 for an unknown entry."""
    return await SERVICE.list_versions(db, character_id)


async def get_companion(
    db: AsyncEngine,
    character_id: str,
    *,
    version: int | None = None,
    draft: bool = False,
    projection: Projection = Projection.CARD,
) -> ScenarioCard | ScenarioFull:
    """The character's companion scenario, as a scenario read gives it; its draft while it was
    never published; 404 for an unknown character (``core.catalog.companion.get``)."""
    return await companion.get(
        db, character_id, version=version, draft=draft, projection=projection
    )


__all__ = [
    "BACKGROUND_FLAVOR_MAX",
    "DRAFT_MAX_BYTES",
    "ENTITY_TYPE",
    "FIRST_DRAFT_REVISION",
    "GALLERY_MAX",
    "NAME_MAX",
    "PERSONALITY_MAX",
    "PUBLIC_DESCRIPTION_MAX",
    "REVISION_MAX",
    "SAFE_FALLBACK_MAX",
    "SERVICE",
    "Audience",
    "CardImage",
    "CardRating",
    "CharacterCard",
    "CharacterCardImages",
    "CharacterCatalog",
    "CharacterCreate",
    "CharacterDefinition",
    "CharacterDefinitionPatch",
    "CharacterDraft",
    "CharacterFull",
    "CharacterImageDetail",
    "CharacterImages",
    "CharacterImagesDetail",
    "CharacterImagesPatch",
    "CharacterListItem",
    "CharacterMetadata",
    "CharacterMetadataPatch",
    "CharacterPatch",
    "CharacterPublicCard",
    "CharacterPublicCardPatch",
    "CharacterPublishRequest",
    "CharacterPublished",
    "CompanionPublished",
    "DeletedCharacter",
    "DraftWrite",
    "Envelope",
    "Name",
    "Projection",
    "PublishRequest",
    "Published",
    "ReferenceImageDetail",
    "StatusChange",
    "VersionSummary",
    "VisualIdentityDetail",
    "archive",
    "card_images",
    "create",
    "delete",
    "get",
    "get_companion",
    "merge_patch",
    "publish",
    "save",
    "unarchive",
    "version_card",
    "versions",
]
