"""Scenarios in the catalog: drafts and immutable, numbered published versions (FR-SCN-1..4,10,
FR-CAT-5,10; docs/02 §4.10; #108, #116).

A scenario is a ``catalog.scenario_catalog`` row; its draft and versions are documents that
``ScenarioDraft`` validates (``core.catalog.scenario_models``). The lifecycle — create, save (a
JSON merge patch compared on ``draft_revision``), publish, archive, unarchive, delete, the reads
in the ``card`` and ``full`` projections and the version list — is the generic catalog
service's (``core.catalog.service``), shared with catalog characters. This module adds what is
a scenario's own:

- **The save rules** (422 ``validation_error`` at ``body.<path>``, on create and on every save,
  over the whole merged draft): role ids are unique and none is reserved (``world`` and the
  audience levels); at most one role is the player's (zero is allowed), and it is ``open``; a
  ``locked`` or ``default`` role names its ``character`` and an ``open`` one names none; a
  role's ``character.version`` is a version or omitted (the latest published at story creation),
  never ``null``; a relationship names another declared role, never its own; every starting
  location names a declared location and every role a seed's holder, a reveal or the opening
  names is declared (a holder may also be ``world``); location, entity and seed ids are unique;
  a scenario holds at most 200 seeds. Every seed without an ``id`` gets one (a ULID) when it is
  stored.
- **The checks against the database**, inside the save's transaction: a
  ``max_characters_at_start`` above the deployment's ceiling is 422 ``cast_limit_exceeded``
  (FR-SCN-4); a content rating the save sets must be assignable (422 ``rating_retired``), and
  each media asset it adds must be live (422 ``media_retired``, held ``FOR SHARE`` until the
  save commits).
- **Publishing** runs the catalog validation (``core.catalog.validation``, #111): any error is
  422 ``publish_invalid`` with ``details.report``; warnings and the checks the deployment cannot
  run ride on the answer and are stored with the version (``scenario_versions.report``). The
  version records the content rating's current version. ``validate`` is the same validation as
  a dry run, of the draft or a published version, that writes nothing.
- **References.** The media sweep counts scenario drafts and versions as references
  (``core.media.assets.MEDIA_REFERENCE_SOURCES``), and every draft write and publish indexes the
  subjects the scenario names (``core.erasure.index``): the catalog characters its roles name,
  and every catalog character or persona its prose mentions, so erasure (#124) finds them.

``effective_cast_limit`` (``scenario_models``) gives the starting-cast limit a story created from
a scenario has; #119 calls it.

**A companion** (``companion_of``, #116; created with its character by
``core.catalog.companion``) keeps its two slots: a save that adds, removes or changes one (the
``id``, ``kind``, ``fill`` or ``character`` of a role, matched by id) is 422
``companion_slots_fixed``, checked before the save rules, and its locked role may name its pin
``null`` before the character's first publish. A save naming its content rating sets it
explicitly (``content_rating_explicit``). Published alone, it is pinned to its character's latest
published version (``publish_alone``). Its listing status, archiving and deletion follow its
character's and are refused on their own (422 ``companion_follows_character``; FR-CAT-5,
FR-CAT-11, #110); ``full`` shows its pin (``pinned_character_version``). A listing
(``core.catalog.search``) shows a scenario as a ``ScenarioListItem`` and leaves companions out
unless asked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control
from epistrel.core.catalog.companion_slots import (
    NPC_ROLE,
    PLAYER_ROLE,
    pin_of,
    pinned_columns,
    repinned,
    slot_change,
)
from epistrel.core.catalog.report import DRAFT, ValidationReport, ValidationResult
from epistrel.core.catalog.scenario_models import (
    CastLimits,
    ScenarioCard,
    ScenarioCardMedia,
    ScenarioCardMediaSet,
    ScenarioCreate,
    ScenarioDraft,
    ScenarioFull,
    ScenarioListItem,
    ScenarioPatch,
    card_roles,
)
from epistrel.core.catalog.scenario_rules import dotted, rule_errors
from epistrel.core.catalog.service import (
    CardRating,
    CatalogService,
    Projection,
    Published,
    PublishRequest,
    Shown,
    ValidateRequest,
    VersionSummary,
    card_rating,
    config_revision,
)
from epistrel.core.catalog.validation import RUNNER, validate_entry
from epistrel.core.config_changes import read_settings
from epistrel.core.enums import EntryStatus
from epistrel.core.erasure.index import (
    Declared,
    Record,
    drop_prefixed,
    drop_references,
    index_records,
)
from epistrel.core.erasure.records import text_fields
from epistrel.core.errors import (
    CastLimitExceeded,
    CompanionSlotsFixed,
    InvalidRequest,
    ScenarioNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.media.assets import assert_referenceable, rating_version
from epistrel.core.ports import Ports
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import media_assets
from epistrel.core.tables.catalog import character_catalog, scenario_catalog, scenario_versions
from epistrel.core.tables.media import SubjectKind
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses

ENTITY_TYPE = EntityType.SCENARIO.value
"""The entry type an audit row and ``core.ownership`` (#104) name."""
DRAFT_MAX_BYTES = 1024 * 1024
"""The largest scenario draft, as canonical JSON (UTF-8 bytes): a world, its cast and their
seeds."""
DRAFT_RECORD = scenario_catalog.name
"""The ``subject_references.record_table`` of a scenario's draft (``record_id``: its id)."""
VERSION_RECORD = scenario_versions.name
"""The ``subject_references.record_table`` of a published version (``record_id``:
``<id>@<version>``)."""


# -- the save rules ----------------------------------------------------------------------------


def stored_document(document: Any, *, companion: bool = False) -> dict[str, Any]:
    """``document`` as a stored scenario draft: valid as ``ScenarioDraft``, keeping every save
    rule (a ``companion``'s, #116, may name its locked character's version ``null``), each seed
    with an ``id``; 422 ``validation_error`` at ``body.<path>`` otherwise (every finding in
    ``details.errors``)."""
    try:
        draft = ScenarioDraft.model_validate(document)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None
    errors = rule_errors(draft, companion=companion)
    if errors:
        raise InvalidRequest.from_errors(errors, prefix=("body",))
    for _, seed in draft.seeds():
        if seed.id is None:
            seed.id = new_ulid()
    return draft.document()


# -- references --------------------------------------------------------------------------------


def _group(document: Mapping[str, Any] | None, *path: str) -> Mapping[str, Any]:
    """The object at ``path`` in a stored document (``{}`` when anything on the way is not
    one)."""
    found: Any = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def media_references(document: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """``(field, media id)`` for each media asset the document's public card shows: the
    cover, the gallery images and the videos, with their request field (``body.…``)."""
    media = _group(document, "public_card", "media")
    found: list[tuple[str, str]] = []
    cover = media.get("cover")
    if isinstance(cover, str):
        found.append(("body.public_card.media.cover", cover))
    gallery = media.get("gallery")
    for index, media_id in enumerate(gallery if isinstance(gallery, list) else []):
        if isinstance(media_id, str):
            found.append((f"body.public_card.media.gallery[{index}]", media_id))
    videos = media.get("videos")
    for index, video in enumerate(videos if isinstance(videos, list) else []):
        if isinstance(video, Mapping) and isinstance(video.get("media_id"), str):
            found.append((f"body.public_card.media.videos[{index}].media_id", video["media_id"]))
    return found


def _content_rating(document: Mapping[str, Any] | None) -> str | None:
    found = _group(document, "metadata").get("content_rating_id")
    return found if isinstance(found, str) else None


def indexed_fields(draft: ScenarioDraft) -> dict[str, str | None]:
    """The scenario's prose, by dotted path: every text a subject's name or alias may appear
    in (``core.erasure.index`` scans them)."""
    fields: dict[str, str | None] = {
        "public_card.name": draft.public_card.name,
        "public_card.public_description": draft.public_card.public_description,
        "world.description": draft.world.description,
        "starting_state.opening.narration": draft.starting_state.opening.narration,
        "drivers.director_guidance": draft.drivers.director_guidance,
        "visual_style.prompt": draft.visual_style.prompt,
        "visual_style.negative_prompt": draft.visual_style.negative_prompt,
    }
    for index, video in enumerate(draft.public_card.media.videos):
        fields[f"public_card.media.videos[{index}].caption"] = video.caption
    for index, location in enumerate(draft.world.locations):
        fields[f"world.locations[{index}].name"] = location.name
        fields[f"world.locations[{index}].description"] = location.description
    for index, entity in enumerate(draft.world.entities):
        fields[f"world.entities[{index}].name"] = entity.name
        fields[f"world.entities[{index}].description"] = entity.description
        for alias_index, alias in enumerate(entity.aliases):
            fields[f"world.entities[{index}].aliases[{alias_index}]"] = alias
    for index, role in enumerate(draft.cast.roles):
        at = f"cast.roles[{index}]"
        fields[f"{at}.public_label"] = role.public_label
        fields[f"{at}.setup_description"] = role.setup_description
        for rel_index, relationship in enumerate(role.relationships):
            fields[f"{at}.relationships[{rel_index}].name"] = relationship.name
        if role.name_overrides is not None:
            fields[f"{at}.name_overrides.canonical"] = role.name_overrides.canonical
            for alias_index, alias in enumerate(role.name_overrides.aliases):
                fields[f"{at}.name_overrides.aliases[{alias_index}]"] = alias
    for loc, seed in draft.seeds():
        fields[dotted((*loc, "text"))] = seed.text
    drivers = draft.drivers
    for name, items in (
        ("plot_hooks", drivers.plot_hooks),
        ("objectives", drivers.objectives),
        ("milestones", drivers.milestones),
        ("endings", drivers.endings),
    ):
        for index, item in enumerate(items):
            fields[f"drivers.{name}[{index}]"] = item
    for index, reveal in enumerate(drivers.planned_reveals):
        fields[f"drivers.planned_reveals[{index}].conditions"] = reveal.conditions
    return fields


def declared_subjects(draft: ScenarioDraft) -> list[Declared]:
    """The catalog characters the scenario's roles are filled with, by id."""
    return [
        (
            SubjectKind.CATALOG_CHARACTER,
            role.character.catalog_id,
            f"cast.roles[{index}].character.catalog_id",
        )
        for index, role in enumerate(draft.cast.roles)
        if role.character is not None
    ]


def report_fields(report: Any) -> dict[str, str | None]:
    """The texts of a published version's validation report (#111): each warning's ``text``
    (it quotes the version) and ``source`` (it can name a ``default`` role's character)."""
    warnings = report.get("warnings") if isinstance(report, Mapping) else None
    fields: dict[str, str | None] = {}
    for index, warning in enumerate(warnings if isinstance(warnings, list) else []):
        if not isinstance(warning, Mapping):
            continue
        for key in ("text", "source"):
            value = warning.get(key)
            if isinstance(value, str):
                fields[f"report.warnings[{index}].{key}"] = value
    return fields


def _indexed(document: Any) -> tuple[dict[str, str | None], tuple[Declared, ...]]:
    """A stored document's indexed fields and declared subjects. A document that no longer
    validates as a draft (stored before a later rule, or planted) is walked whole, every role's
    ``character.catalog_id`` declared, so the rebuild never fails on it."""
    try:
        draft = ScenarioDraft.model_validate(document)
    except ValidationError:
        roles = _group(document, "cast").get("roles")
        declared: list[Declared] = []
        for index, role in enumerate(roles if isinstance(roles, list) else []):
            found = role.get("character") if isinstance(role, Mapping) else None
            catalog_id = found.get("catalog_id") if isinstance(found, Mapping) else None
            if isinstance(catalog_id, str):
                declared.append(
                    (
                        SubjectKind.CATALOG_CHARACTER,
                        catalog_id,
                        f"cast.roles[{index}].character.catalog_id",
                    )
                )
        return text_fields(document), tuple(declared)
    return indexed_fields(draft), tuple(declared_subjects(draft))


def draft_record(entry_id: str, document: Any) -> Record:
    """A scenario draft's subject-index record (see ``indexed_fields``)."""
    fields, subjects = _indexed(document)
    return Record(DRAFT_RECORD, entry_id, None, fields, subjects)


def version_record(entry_id: str, version: int, document: Any, report: Any) -> Record:
    """A published version's subject-index record (``<id>@<version>``): its content's fields and
    its report's (``report_fields``)."""
    fields, subjects = _indexed(document)
    return Record(
        VERSION_RECORD, f"{entry_id}@{version}", None, {**fields, **report_fields(report)}, subjects
    )


# -- results -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioDraftWrite:
    """A created or saved scenario draft (the ``full`` draft read) and the deployment's
    ``config_revision``, which catalog writes leave as it is."""

    scenario: ScenarioFull
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class ScenarioEnvelope:
    """A scenario without its document: what archive and unarchive answer with."""

    id: str
    status: EntryStatus
    listing_status: str
    current_version: int | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    companion_of: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioStatusChange:
    """The scenario after archive or unarchive; ``changed`` is false when there was nothing
    to do."""

    scenario: ScenarioEnvelope
    changed: bool
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class DeletedScenario:
    """A deleted scenario and how many published versions went with it."""

    id: str
    versions: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- the service -------------------------------------------------------------------------------


async def _card_media(conn: AsyncConnection, draft: ScenarioDraft) -> ScenarioCardMediaSet:
    media = draft.public_card.media
    wanted = [m for m in (media.cover, *media.gallery) if m is not None]
    wanted += [video.media_id for video in media.videos]
    known: dict[str, Any] = {}
    if wanted:
        rows = await conn.execute(
            select(media_assets.c.id, media_assets.c.alt_text, media_assets.c.caption).where(
                media_assets.c.id.in_(wanted)
            )
        )
        known = {str(row.id): row for row in rows}

    def item(media_id: str, caption: str | None = None) -> ScenarioCardMedia:
        row = known.get(media_id)
        return ScenarioCardMedia(
            media_id=media_id,
            alt_text=None if row is None else row.alt_text,
            caption=caption if caption is not None else (None if row is None else row.caption),
        )

    return ScenarioCardMediaSet(
        cover=None if media.cover is None else item(media.cover),
        gallery=[item(media_id) for media_id in media.gallery],
        videos=[item(video.media_id, video.caption) for video in media.videos],
    )


async def _card_rating(
    conn: AsyncConnection, draft: ScenarioDraft, version_row: Any | None
) -> CardRating | None:
    """The rating a card shows: the version a published version recorded, or for a draft the
    rating's current version (``service.card_rating``, which catalog characters share)."""
    return await card_rating(conn, draft.metadata.content_rating_id, version_row)


class ScenarioCatalog(CatalogService):
    """Scenarios on the generic catalog service (see the module docstring)."""

    entity_type = EntityType.SCENARIO
    noun = "scenario"
    id_key = "scenario_id"
    catalog = scenario_catalog
    versions = scenario_versions
    not_found_error = ScenarioNotFound
    list_item = ScenarioListItem
    document_model = ScenarioDraft

    def max_bytes(self) -> int:
        return DRAFT_MAX_BYTES  # read at call time, so a test may lower it

    def leader(self, entry: Any) -> str | None:
        """A companion's character (``companion_of``, #116): its listing status, archiving and
        deletion follow the character's (FR-CAT-5, FR-CAT-11)."""
        found = entry.companion_of
        return None if found is None else str(found)

    def stored(self, document: Any) -> dict[str, Any]:
        return stored_document(document)

    def saving(
        self, entry: Any, merged: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A companion's slots stay as they are (FR-CAT-5): a save that adds, removes or changes
        one (``companion_slots.slot_change``) is 422 ``companion_slots_fixed``, checked before
        the save rules; a slot echoed unchanged is accepted, and the locked role may name its
        pin, ``null`` before the character's first publish."""
        character_id = self.leader(entry)
        if character_id is None:
            return self.stored(merged)
        change = slot_change(entry.draft, merged)
        if change is not None:
            role, field = change
            raise CompanionSlotsFixed(
                f"scenario {entry.id} is the companion of catalog character {character_id}: its "
                f"slots (`{NPC_ROLE}` locked to the character, and `{PLAYER_ROLE}`) cannot be "
                "added, removed or changed; every other field can",
                {
                    "scenario_id": str(entry.id),
                    "character_id": character_id,
                    "role": role,
                    "field": f"body.{field}",
                },
            )
        return stored_document(merged, companion=True)

    def save_values(self, entry: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
        """A save that names a companion's content rating (even to the value it has, or to
        remove it) sets it explicitly: from then on the companion keeps its own rating instead
        of taking its character's (#116)."""
        if self.leader(entry) is None or "metadata" not in patch:
            return {}
        metadata = patch["metadata"]
        if metadata is None or (isinstance(metadata, Mapping) and "content_rating_id" in metadata):
            return {"content_rating_explicit": True}
        return {}

    async def checked(
        self, conn: AsyncConnection, document: Mapping[str, Any], previous: Any
    ) -> None:
        cast = _group(document, "cast").get("max_characters_at_start")
        limits = CastLimits.of((await read_settings(conn)).as_settings())
        if isinstance(cast, int) and limits.ceiling is not None and cast > limits.ceiling:
            raise CastLimitExceeded(
                f"`max_characters_at_start` is {cast}; the deployment's ceiling is "
                f"{limits.ceiling}",
                {
                    "field": "body.cast.max_characters_at_start",
                    "max_characters_at_start": cast,
                    "ceiling": limits.ceiling,
                },
            )
        rating = _content_rating(document)
        if rating is not None and rating != _content_rating(previous):
            await require_assignable(conn, rating, field="body.metadata.content_rating_id")
        before = {media_id for _, media_id in media_references(previous)}
        for field, media_id in media_references(document):
            if media_id not in before:
                await assert_referenceable(conn, [media_id], field=field)

    async def version_values(
        self, conn: AsyncConnection, document: Mapping[str, Any], report: ValidationReport
    ) -> dict[str, Any]:
        rating = _content_rating(document)
        return {
            "content_rating_version_id": None
            if rating is None
            else await rating_version(conn, rating),
            "report": report.as_json(),
        }

    async def recorded(
        self,
        conn: AsyncConnection,
        entry_id: str,
        document: Mapping[str, Any],
        *,
        version: int | None,
    ) -> None:
        if version is None:
            await index_records(conn, [draft_record(entry_id, document)])
            return
        report = await conn.scalar(
            select(scenario_versions.c.report).where(
                scenario_versions.c.catalog_id == entry_id,
                scenario_versions.c.version == version,
            )
        )
        await index_records(conn, [version_record(entry_id, version, document, report)])

    async def forgotten(self, conn: AsyncConnection, entry_id: str) -> None:
        await drop_references(conn, DRAFT_RECORD, entry_id)
        await drop_prefixed(conn, VERSION_RECORD, f"{entry_id}@")

    async def publish_read(
        self,
        db: AsyncEngine,
        read: Any,
        request: PublishRequest,
        *,
        principal: str,
        ports: Ports,
    ) -> Published:
        """A scenario's publish; a companion's is pinned to its character (``publish_alone``)."""
        if self.leader(read) is None:
            return await super().publish_read(db, read, request, principal=principal, ports=ports)
        return await self.publish_alone(db, read, request, principal=principal, ports=ports)

    async def publish_alone(
        self,
        db: AsyncEngine,
        read: Any,
        request: PublishRequest,
        *,
        principal: str,
        ports: Ports,
    ) -> Published:
        """A companion published alone (FR-CAT-10, "companion edited alone"): its draft becomes
        the next version, pinned to its character's latest published version, never to a
        character draft, and the character's version does not change. Under the row locks the
        character's publishes take too, in their order (the character ``FOR SHARE``, then the
        companion), so the pin is the latest. Before the character's first publish there is no
        version to pin: the validation reports ``character_not_published`` (422
        ``publish_invalid``). The full validation applies: a world and an opening too."""
        entry_id = str(read.id)
        character_id = str(read.companion_of)
        latest = select(character_catalog.c.current_version).where(
            character_catalog.c.id == character_id
        )
        async with db.connect() as conn:
            pin = await conn.scalar(latest)
        # Outside any transaction (FR-CONC-2), like any publish's prepare phase.
        prepared = await self.prepare_for(
            db, entry_id, DRAFT, repinned(read.draft, pin), ports=ports
        )
        async with db.begin() as conn:
            pin = await conn.scalar(latest.with_for_update(read=True))
            entry = await self.entry(conn, entry_id, lock=True)
            self.refuse(entry, entry_id, request.draft_revision)
            await pauses.pause(pauses.CATALOG_PUBLISH_LOCKED)
            document = repinned(entry.draft, pin)
            report = await self.validated(
                conn, entry_id, DRAFT, document, prepared=prepared, ports=ports
            )
            version = int(entry.current_version or 0) + 1
            draft_revision = int(entry.draft_revision) + 1
            now = clock.now()
            await self.insert_version(
                conn,
                entry_id,
                version,
                document,
                report,
                principal=principal,
                now=now,
                values=pinned_columns(character_id, pin),
            )
            status = await self.mark_published(
                conn,
                entry_id,
                version=version,
                draft_revision=draft_revision,
                now=now,
                draft=document,
            )
            await control.record_control_operation(conn, actor=principal)
            await self.recorded(conn, entry_id, document, version=version)
            if document != entry.draft:
                await self.recorded(conn, entry_id, document, version=None)
            revision = await config_revision(conn)
        return Published(
            id=entry_id,
            version=version,
            draft_revision=draft_revision,
            status=status,
            published_at=now,
            report=report,
            config_revision=revision,
        )

    async def project(self, conn: AsyncConnection, shown: Shown, projection: Projection) -> Any:
        entry = shown.entry
        document = shown.document
        pin: int | None = None
        if projection is Projection.FULL and self.leader(entry) is not None:
            # A companion's pin (#116), which only ``full`` shows: a version's lives in its
            # ``pinned_character_version`` (null once that character version is erased), and is
            # projected into the locked role's ``character.version``; a draft's is in the draft.
            if shown.version_row is not None:
                pin = shown.version_row.pinned_character_version
                document = repinned(document, pin)
            else:
                pin = pin_of(document)
        draft = ScenarioDraft.model_validate(document)
        if projection is Projection.FULL:
            return ScenarioFull(
                id=entry.id,
                status=EntryStatus(entry.status),
                listing_status=entry.listing_status,
                owner_principal=entry.owner_principal,
                version=shown.version,
                current_version=entry.current_version,
                draft_revision=shown.draft_revision,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                published_at=shown.published_at,
                companion_of=entry.companion_of,
                pinned_character_version=pin,
                **{name: getattr(draft, name) for name in ScenarioDraft.model_fields},
            )
        return ScenarioCard(
            id=entry.id,
            version=shown.version,
            listing_status=entry.listing_status,
            status=EntryStatus(entry.status),
            companion_of=entry.companion_of,
            name=draft.public_card.name,
            public_description=draft.public_card.public_description,
            media=await _card_media(conn, draft),
            labels=list(draft.metadata.labels),
            content_rating=await _card_rating(conn, draft, shown.version_row),
            roles=card_roles(draft),
        )

    async def envelope(self, conn: AsyncConnection, entry: Any) -> ScenarioEnvelope:
        return ScenarioEnvelope(
            id=entry.id,
            status=EntryStatus(entry.status),
            listing_status=entry.listing_status,
            current_version=entry.current_version,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            published_at=await self.published_at(conn, entry.id, entry.current_version),
            companion_of=entry.companion_of,
        )


SERVICE = ScenarioCatalog(RUNNER)
"""The scenario catalog's service, publishing through the catalog validation (#111)."""


# -- writes ------------------------------------------------------------------------------------


def _creation_document(request: ScenarioCreate) -> dict[str, Any]:
    """The draft a creation stores: the request's groups as sent (``owner`` left out), so the
    save rules see each explicit ``null`` the body holds."""
    document: dict[str, Any] = request.model_dump(
        mode="json", exclude_unset=True, exclude={"owner"}
    )
    return document


async def create(db: AsyncEngine, request: ScenarioCreate, *, principal: str) -> ScenarioDraftWrite:
    """A new scenario whose draft holds ``request``'s groups, at draft revision 1 (status
    ``draft``, at the deployment's first listing status value), owned by ``request.owner`` or
    else ``principal``."""
    scenario, revision = await SERVICE.create(
        db, _creation_document(request), owner=request.owner or principal, principal=principal
    )
    return ScenarioDraftWrite(scenario=scenario, config_revision=revision)


async def save(
    db: AsyncEngine, scenario_id: str, patch: ScenarioPatch, *, principal: str
) -> ScenarioDraftWrite:
    """Apply ``patch`` to the draft (see ``core.catalog.service``): 404 for an unknown
    scenario, 422 ``entry_archived``, 409 ``draft_stale``, 422 for an invalid result (the save
    rules), 422 ``cast_limit_exceeded``, ``rating_retired`` or ``media_retired``, 413 for a too
    large one."""
    scenario, revision = await SERVICE.save(
        db, scenario_id, patch.document(), patch.draft_revision, principal=principal
    )
    return ScenarioDraftWrite(scenario=scenario, config_revision=revision)


async def publish(
    db: AsyncEngine,
    scenario_id: str,
    request: PublishRequest,
    *,
    principal: str,
    ports: Ports | None = None,
) -> Published:
    """Publish the draft as version ``current_version + 1`` (see ``core.catalog.service``)."""
    return await SERVICE.publish(db, scenario_id, request, principal=principal, ports=ports)


async def validate(
    db: AsyncEngine,
    scenario_id: str,
    request: ValidateRequest | None = None,
    *,
    version: int | None = None,
    ports: Ports | None = None,
) -> ValidationResult:
    """Validate the draft, or published version ``version``, as a dry run that writes nothing
    (``core.catalog.validation.validate_entry``): 404 for an unknown scenario or version, 409
    ``draft_stale`` when ``request.draft_revision`` is no longer the draft's, 422 when both are
    named. Archived scenarios validate too."""
    return await validate_entry(
        db,
        SERVICE,
        scenario_id,
        draft_revision=None if request is None else request.draft_revision,
        version=version,
        ports=ports,
    )


async def archive(db: AsyncEngine, scenario_id: str, *, principal: str) -> ScenarioStatusChange:
    """Make the scenario ``archived``; an archived one is left as it is (``changed`` false)."""
    envelope, changed, revision = await SERVICE.set_archived(
        db, scenario_id, True, principal=principal
    )
    return ScenarioStatusChange(scenario=envelope, changed=changed, config_revision=revision)


async def unarchive(db: AsyncEngine, scenario_id: str, *, principal: str) -> ScenarioStatusChange:
    """Restore an archived scenario to ``published`` (``draft`` if never published); any other
    is left as it is (``changed`` false)."""
    envelope, changed, revision = await SERVICE.set_archived(
        db, scenario_id, False, principal=principal
    )
    return ScenarioStatusChange(scenario=envelope, changed=changed, config_revision=revision)


async def delete(db: AsyncEngine, scenario_id: str, *, principal: str) -> DeletedScenario:
    """Remove the scenario and its versions (from any status), its subject-reference rows and
    its relations (a ``deleted`` ownership event), and write a ``catalog_entry_deleted`` audit
    row; 404 for an unknown scenario. Stories created from it keep their snapshot (FR-CAT-9)."""
    removed = await SERVICE.delete(db, scenario_id, principal=principal)
    return DeletedScenario(id=scenario_id, versions=removed)


# -- reads -------------------------------------------------------------------------------------


async def get(
    db: AsyncEngine,
    scenario_id: str,
    *,
    version: int | None = None,
    draft: bool = False,
    projection: Projection = Projection.CARD,
) -> ScenarioCard | ScenarioFull:
    """The latest published version, version ``version``, or (``draft``) the draft, as
    ``projection``. 404 for an unknown scenario or version; 422 ``not_published`` before the
    first publish; 422 when both ``version`` and ``draft`` are given."""
    found: ScenarioCard | ScenarioFull = await SERVICE.get(
        db, scenario_id, version=version, draft=draft, projection=projection
    )
    return found


async def versions(db: AsyncEngine, scenario_id: str) -> list[VersionSummary]:
    """Every published version of the scenario, oldest first; 404 for an unknown one."""
    return await SERVICE.list_versions(db, scenario_id)


__all__ = [
    "DRAFT_MAX_BYTES",
    "DRAFT_RECORD",
    "ENTITY_TYPE",
    "SERVICE",
    "VERSION_RECORD",
    "DeletedScenario",
    "Projection",
    "PublishRequest",
    "Published",
    "ScenarioCatalog",
    "ScenarioDraftWrite",
    "ScenarioEnvelope",
    "ScenarioStatusChange",
    "ValidateRequest",
    "VersionSummary",
    "archive",
    "create",
    "declared_subjects",
    "delete",
    "get",
    "indexed_fields",
    "media_references",
    "publish",
    "rule_errors",
    "save",
    "stored_document",
    "unarchive",
    "validate",
    "versions",
]
