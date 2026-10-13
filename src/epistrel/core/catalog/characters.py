"""Catalog characters: one draft per entry and immutable, numbered published versions
(FR-CAT-1,3,9; FR-SCN-10; docs/02 §4.10; #103).

An entry is a ``catalog.character_catalog`` row; its draft is a JSON document that
``CharacterDraft`` validates, and its versions are ``catalog.character_versions`` rows. The
lifecycle — create, save (a JSON merge patch compared on ``draft_revision``), publish, archive,
unarchive, delete, and the reads in the ``card`` and ``full`` projections — is the generic
catalog service's (``core.catalog.service``, #108), which scenarios share; this module supplies
the character's document, its publish validation and its projections.

Publishing runs the catalog validation (``core.catalog.validation``, #111): the draft must be a
valid ``CharacterDraft`` with a ``name`` (422 ``publish_invalid`` with ``details.report``); #115
adds its rules there. A character carries no world truths of its own: those belong to a
scenario, normally the character's companion scenario (FR-CAT-5).

**The interim draft** (#103, until #115 gives it its full shape) holds the public card's
``name`` and ``public_description`` at the top level and the private ``definition``. #114 added
the fields a character promoted from a story brings (FR-CAT-8), at the paths #115 plans for
them: the definition's ``true_age``, ``background_flavor`` and ``safe_fallback_text``, the
public card's images (``public_card.images``: a ``primary`` image and a ``gallery``, media ids
by reference) and the content rating (``metadata.content_rating_id``). A save checks what it
adds, as a scenario's does: a rating must be assignable (422 ``rating_retired``) and each image
live (422 ``media_retired``). A published version records its rating's version.

A read returns one projection (FR-SCN-10): ``card`` (the default), only the public card, or
``full``, which adds the entry's envelope and every definition field. Both show the images (the
primary first, then the gallery, each with its asset's alt text) and the rating (the version a
published version recorded, or for a draft the rating's current version). A listing
(``core.catalog.search``, #110) shows each entry as a ``CharacterListItem``: its card, its owner
and ``updated_at``. A character's companion scenarios (``scenario_catalog.companion_of``, #116)
follow its listing status (FR-CAT-11).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog.checks.character_documents import images_of, rating_id_of
from epistrel.core.catalog.report import ValidationReport
from epistrel.core.catalog.service import (
    FIRST_DRAFT_REVISION,
    REVISION_MAX,
    CardRating,
    CatalogService,
    Follower,
    Projection,
    Published,
    PublishRequest,
    Revision,
    Shown,
    VersionSummary,
    card_rating,
    merge_patch,
)
from epistrel.core.catalog.validation import RUNNER
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import CharacterNotFound, InvalidRequest
from epistrel.core.ids import Ulid
from epistrel.core.media.assets import assert_referenceable, rating_version
from epistrel.core.ownership import Owner
from epistrel.core.ports import Ports
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import media_assets
from epistrel.core.tables.catalog import character_catalog, character_versions, scenario_catalog
from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.tables.ownership import EntityType

ENTITY_TYPE = EntityType.CHARACTER.value
"""The entry type an audit row and ``core.ownership`` (#104) name."""
NAME_MAX = 120
PUBLIC_DESCRIPTION_MAX = 4_000
PERSONALITY_MAX = 4_000
BACKGROUND_FLAVOR_MAX = 4_000
SAFE_FALLBACK_MAX = 500
GALLERY_MAX = 20
DRAFT_MAX_BYTES = 256 * 1024
"""The largest draft, as canonical JSON (UTF-8 bytes)."""

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]


# -- the draft document -----------------------------------------------------------------------


class CharacterDefinition(BaseModel):
    """The private character definition (FR-CAT-2): who the character is. It is seeded into
    every story the character joins and is otherwise returned only by the ``full`` projection.
    Until #115 adds the other fields it holds ``personality``, the character's ``true_age``
    (FR-CAT-12), ``background_flavor`` (free text: a definition promoted from a story lands
    here, FR-CAT-8) and ``safe_fallback_text`` (the line the character says when every
    regenerated reply fails its guardrails, #68)."""

    model_config = ConfigDict(extra="forbid")

    personality: str | None = Field(
        default=None,
        max_length=PERSONALITY_MAX,
        description="Personality and temperament (private).",
    )
    true_age: StrictInt | None = Field(
        default=None,
        ge=TRUE_AGE_MIN,
        le=TRUE_AGE_MAX,
        description="The character's true age (FR-CAT-12): 0 to 150. Never on the card.",
    )
    background_flavor: str | None = Field(
        default=None,
        max_length=BACKGROUND_FLAVOR_MAX,
        description="Optional background, as free text (private).",
    )
    safe_fallback_text: str | None = Field(
        default=None,
        max_length=SAFE_FALLBACK_MAX,
        description="What the character says when every regenerated reply fails its guardrails "
        "(private).",
    )


def _distinct(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("lists a media asset more than once")
    return values


class CharacterImages(BaseModel):
    """The public card's images (FR-CAT-4; #115's shape): a ``primary`` image and a ``gallery``
    (up to 20, distinct), as media asset ids. The assets are referenced, never copied; each must
    be ``live`` when a save adds it."""

    model_config = ConfigDict(extra="forbid")

    primary: Ulid | None = Field(default=None, description="The primary image's media id.")
    gallery: list[Ulid] = Field(
        default_factory=list, max_length=GALLERY_MAX, description="More images, in order."
    )

    @field_validator("gallery")
    @classmethod
    def _distinct_gallery(cls, value: list[str]) -> list[str]:
        return _distinct(value)


class CharacterPublicCard(BaseModel):
    """The public card's part of the interim draft that lives under ``public_card`` (#115 moves
    ``name`` and ``public_description`` here too): its ``images``."""

    model_config = ConfigDict(extra="forbid")

    images: CharacterImages = Field(default_factory=CharacterImages)


class CharacterMetadata(BaseModel):
    """The draft's metadata (#115's shape): its ``content_rating_id`` (FR-RATE-3), which must be
    assignable when a save sets it."""

    model_config = ConfigDict(extra="forbid")

    content_rating_id: Ulid | None = Field(default=None, description="The content rating's id.")


class CharacterDraft(BaseModel):
    """A catalog character's draft document (FR-CAT-2; interim until #115): the public card's
    ``name`` and ``public_description``, its images (``public_card.images``), the private
    ``definition`` and the content rating (``metadata.content_rating_id``). A draft may lack any
    field; publishing needs a ``name``. A character carries no world truths of its own: those
    belong to a scenario, normally the character's companion scenario (FR-CAT-5)."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = Field(default=None, description="The character's name (public).")
    public_description: str | None = Field(
        default=None,
        max_length=PUBLIC_DESCRIPTION_MAX,
        description="Free text for the public card; never seeded into a story.",
    )
    public_card: CharacterPublicCard | None = Field(
        default=None, description="The card's images (public)."
    )
    definition: CharacterDefinition = Field(
        default_factory=CharacterDefinition,
        description="The private definition: returned only by the `full` projection.",
    )
    metadata: CharacterMetadata | None = Field(
        default=None, description="The content rating (public, on the card)."
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
    true_age: StrictInt | None = Field(default=None, ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)
    background_flavor: str | None = Field(default=None, max_length=BACKGROUND_FLAVOR_MAX)
    safe_fallback_text: str | None = Field(default=None, max_length=SAFE_FALLBACK_MAX)


class CharacterImagesPatch(BaseModel):
    """The images part of a draft patch: ``primary`` set or removed (``null``), and ``gallery``
    replaced as a whole."""

    model_config = ConfigDict(extra="forbid")

    primary: Ulid | None = None
    gallery: list[Ulid] | None = Field(default=None, max_length=GALLERY_MAX)


class CharacterPublicCardPatch(BaseModel):
    """The ``public_card`` part of a draft patch."""

    model_config = ConfigDict(extra="forbid")

    images: CharacterImagesPatch | None = None


class CharacterMetadataPatch(BaseModel):
    """The ``metadata`` part of a draft patch."""

    model_config = ConfigDict(extra="forbid")

    content_rating_id: Ulid | None = None


class CharacterPatch(BaseModel):
    """``PATCH /catalog/characters/{id}``: a JSON merge patch (RFC 7386) over the draft — a key
    sets that field, ``null`` removes it, and ``definition``, ``public_card`` and ``metadata``
    merge the same way (an array, the gallery, replaces the stored one) — with the
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
    public_card: CharacterPublicCardPatch | None = None
    definition: CharacterDefinitionPatch | None = None
    metadata: CharacterMetadataPatch | None = None

    def document(self) -> dict[str, Any]:
        """The merge patch itself: the fields the request set (``null`` included), without
        ``draft_revision``."""
        patch: dict[str, Any] = self.model_dump(
            mode="json", exclude_unset=True, exclude={"draft_revision"}
        )
        return patch


def _validated(document: Any) -> dict[str, Any]:
    """``document`` as a stored draft; 422 ``validation_error`` at ``body.<field>``."""
    try:
        draft = CharacterDraft.model_validate(document)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None
    return draft.document()


# -- projections (FR-SCN-10) ------------------------------------------------------------------


class CardImage(BaseModel):
    """An image on a public card (FR-CAT-4; #105, #115): the media asset and its alt text."""

    model_config = ConfigDict(extra="forbid")

    media_id: str
    alt_text: str | None = None


class CharacterCard(BaseModel):
    """A catalog character's public card (``projection=card``, the default; FR-CAT-2,
    FR-SCN-10): only what a player-facing client may show, never a definition field.
    ``version`` is the version shown; a draft read shows the draft with the latest published
    version (``null`` before the first publish). ``images`` are the primary image, then the
    gallery, each with its asset's alt text; ``content_rating`` is the rating's version a
    published version recorded (for a draft, the rating's current version). Until #115,
    ``labels`` are empty."""

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


class CharacterListItem(CharacterCard):
    """A catalog character as a listing shows it (``GET /catalog/characters``; #110): exactly
    its public card (what a single read answers with ``projection=card``), plus its
    ``owner_principal`` (recorded for listing and attribution only, FR-AUTH-8) and when it last
    changed (``updated_at``: a draft save, publish, archive, listing-status change or owner
    transfer). ``rank`` is present on a search (``q``): its text-search rank, the order's first
    key."""

    owner_principal: str
    updated_at: datetime
    rank: float | None = Field(default=None, exclude_if=lambda rank: rank is None)


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


async def card_images(conn: AsyncConnection, document: Mapping[str, Any]) -> list[CardImage]:
    """The images a character's card shows: the primary image, then the gallery, each with its
    asset's alt text (``null`` for an asset that is gone)."""
    wanted = [media_id for _, media_id in images_of(document)]
    if not wanted:
        return []
    rows = await conn.execute(
        select(media_assets.c.id, media_assets.c.alt_text).where(media_assets.c.id.in_(wanted))
    )
    alt = {str(row.id): row.alt_text for row in rows}
    return [CardImage(media_id=media_id, alt_text=alt.get(media_id)) for media_id in wanted]


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
    card: dict[str, Any] = {
        "id": entry.id,
        "version": version,
        "name": document.get("name"),
        "public_description": document.get("public_description"),
        "images": await card_images(conn, document),
        "labels": [],
        "content_rating": await card_rating(conn, rating_id_of(document), version_row),
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
        assignable (422 ``rating_retired``) and each image it adds live (422 ``media_retired``,
        held ``FOR SHARE`` until the write commits), as a scenario's save checks them."""
        rating = rating_id_of(document)
        if rating is not None and rating != rating_id_of(previous):
            await require_assignable(conn, rating, field="body.metadata.content_rating_id")
        before = {media_id for _, media_id in images_of(previous)}
        for field, media_id in images_of(document):
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
) -> Published:
    """Publish the draft as version ``current_version + 1`` (see ``core.catalog.service``)."""
    return await SERVICE.publish(db, character_id, request, principal=principal, ports=ports)


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


__all__ = [
    "DRAFT_MAX_BYTES",
    "ENTITY_TYPE",
    "FIRST_DRAFT_REVISION",
    "REVISION_MAX",
    "SERVICE",
    "CardImage",
    "CardRating",
    "CharacterCard",
    "CharacterCatalog",
    "CharacterCreate",
    "CharacterDefinition",
    "CharacterDefinitionPatch",
    "CharacterDraft",
    "CharacterFull",
    "CharacterListItem",
    "CharacterPatch",
    "DeletedCharacter",
    "DraftWrite",
    "Envelope",
    "Projection",
    "PublishRequest",
    "Published",
    "StatusChange",
    "VersionSummary",
    "archive",
    "create",
    "delete",
    "get",
    "merge_patch",
    "publish",
    "save",
    "unarchive",
    "versions",
]
