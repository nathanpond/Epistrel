"""Scenarios in the catalog: drafts and immutable, numbered published versions (FR-SCN-1..4,10;
docs/02 §4.10; #108).

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
- **Publishing** needs a valid draft with a ``public_card.name`` and, on every seed, a holder, an
  audience and a disclosure (``publish_invalid`` with the report; #111 replaces the validator
  with its full one). The version records the content rating's current version.
- **References.** The media sweep counts scenario drafts and versions as references
  (``core.media.assets.MEDIA_REFERENCE_SOURCES``), and every draft write and publish indexes the
  subjects the scenario names (``core.erasure.index``): the catalog characters its roles name,
  and every catalog character or persona its prose mentions, so erasure (#124) finds them.

``effective_cast_limit`` (``scenario_models``) gives the starting-cast limit a story created from
a scenario has; #119 calls it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog.scenario_models import (
    RESERVED_ROLE_IDS,
    SEEDS_MAX,
    WORLD_HOLDER,
    CastLimits,
    ScenarioCard,
    ScenarioCardMedia,
    ScenarioCardMediaSet,
    ScenarioCreate,
    ScenarioDraft,
    ScenarioFull,
    ScenarioPatch,
    card_roles,
)
from epistrel.core.catalog.service import (
    CardRating,
    CatalogService,
    Projection,
    Published,
    PublishReport,
    PublishRequest,
    ReportEntry,
    Shown,
    ValidationContext,
    VersionSummary,
    schema_report,
)
from epistrel.core.config_changes import read_settings
from epistrel.core.enums import EntryStatus
from epistrel.core.erasure.index import Declared, drop_references, index_references
from epistrel.core.errors import CastLimitExceeded, InvalidRequest, ScenarioNotFound
from epistrel.core.ids import new_ulid
from epistrel.core.media.assets import assert_referenceable, rating_version
from epistrel.core.ports import Ports
from epistrel.core.ratings import require_assignable
from epistrel.core.tables import content_rating_versions, content_ratings, media_assets
from epistrel.core.tables.catalog import scenario_catalog, scenario_versions
from epistrel.core.tables.media import SubjectKind, subject_references
from epistrel.core.tables.ownership import EntityType

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

Loc = tuple[str | int, ...]


def _error(loc: Loc, kind: str, message: str) -> dict[str, Any]:
    """One finding, in the shape of a pydantic error (``InvalidRequest.from_errors``)."""
    return {"loc": loc, "type": kind, "msg": message}


def rule_errors(draft: ScenarioDraft) -> list[dict[str, Any]]:
    """Every save rule ``draft`` breaks (see the module docstring), each at its location in the
    document; ``[]`` when it keeps them all."""
    errors: list[dict[str, Any]] = []
    roles = draft.cast.roles
    role_ids = {role.id for role in roles}
    location_ids: set[str] = set()
    for index, location in enumerate(draft.world.locations):
        if location.id in location_ids:
            errors.append(
                _error(
                    ("world", "locations", index, "id"),
                    "location_id_duplicate",
                    f"another location is `{location.id}`; location ids are unique",
                )
            )
        location_ids.add(location.id)
    entity_ids: set[str] = set()
    for index, entity in enumerate(draft.world.entities):
        if entity.id in entity_ids:
            errors.append(
                _error(
                    ("world", "entities", index, "id"),
                    "entity_id_duplicate",
                    f"another entity is `{entity.id}`; entity ids are unique",
                )
            )
        entity_ids.add(entity.id)

    seen: set[str] = set()
    players = 0
    for index, role in enumerate(roles):
        at: Loc = ("cast", "roles", index)
        if role.id in RESERVED_ROLE_IDS:
            errors.append(
                _error(
                    (*at, "id"),
                    "role_id_reserved",
                    f"`{role.id}` is reserved: a seed's holder or audience uses it",
                )
            )
        elif role.id in seen:
            errors.append(
                _error(
                    (*at, "id"),
                    "role_id_duplicate",
                    f"another role is `{role.id}`; role ids are unique",
                )
            )
        seen.add(role.id)
        if role.kind == "player":
            players += 1
            if players > 1:
                errors.append(
                    _error((*at, "kind"), "player_role_limit", "a scenario has one player role")
                )
            if role.fill != "open":
                errors.append(
                    _error(
                        (*at, "fill"),
                        "player_role_fill",
                        "the player role is filled by the player's persona: its fill mode is "
                        "`open`",
                    )
                )
        if role.fill in ("locked", "default") and role.character is None:
            errors.append(
                _error(
                    (*at, "character"),
                    "role_character_required",
                    f"a `{role.fill}` role names the catalog character that fills it",
                )
            )
        if role.fill == "open" and role.character is not None:
            errors.append(
                _error(
                    (*at, "character"),
                    "role_character_not_allowed",
                    "an `open` role names no character: the story creator picks one",
                )
            )
        character = role.character
        if (
            character is not None
            and character.version is None
            and "version" in character.model_fields_set
        ):
            errors.append(
                _error(
                    (*at, "character", "version"),
                    "role_version_null",
                    "omit `version` to use the latest published version at story creation",
                )
            )
        for rel_index, relationship in enumerate(role.relationships):
            where: Loc = (*at, "relationships", rel_index, "to_role")
            if relationship.to_role == role.id:
                errors.append(
                    _error(where, "relationship_self", "a role has no relationship to itself")
                )
            elif relationship.to_role not in role_ids:
                errors.append(_unknown_role(where, relationship.to_role))
        if role.starting_location is not None and role.starting_location not in location_ids:
            errors.append(_unknown_location((*at, "starting_location"), role.starting_location))

    opening = draft.starting_state.opening
    if opening.starting_location is not None and opening.starting_location not in location_ids:
        errors.append(
            _unknown_location(
                ("starting_state", "opening", "starting_location"), opening.starting_location
            )
        )
    for index, role_id in enumerate(opening.roles_present):
        if role_id not in role_ids:
            errors.append(
                _unknown_role(("starting_state", "opening", "roles_present", index), role_id)
            )
    for index, reveal in enumerate(draft.drivers.planned_reveals):
        for role_index, role_id in enumerate(reveal.allowed_roles):
            if role_id not in role_ids:
                errors.append(
                    _unknown_role(
                        ("drivers", "planned_reveals", index, "allowed_roles", role_index), role_id
                    )
                )
    viewpoint = draft.story_settings_defaults.default_output_viewpoint
    if viewpoint is not None and viewpoint not in role_ids:
        errors.append(
            _unknown_role(("story_settings_defaults", "default_output_viewpoint"), viewpoint)
        )

    seed_ids: set[str] = set()
    for count, (loc, seed) in enumerate(draft.seeds(), start=1):
        if count == SEEDS_MAX + 1:
            errors.append(
                _error(
                    loc,
                    "too_many_seeds",
                    f"a scenario holds at most {SEEDS_MAX} seeds, all lists together",
                )
            )
        if seed.holder is not None and seed.holder != WORLD_HOLDER and seed.holder not in role_ids:
            errors.append(_unknown_role((*loc, "holder"), seed.holder))
        if seed.id is not None:
            if seed.id in seed_ids:
                errors.append(
                    _error(
                        (*loc, "id"),
                        "seed_id_duplicate",
                        f"another seed is `{seed.id}`; seed ids are unique",
                    )
                )
            seed_ids.add(seed.id)
    return errors


def _unknown_role(loc: Loc, role_id: str) -> dict[str, Any]:
    return _error(loc, "unknown_role", f"no role of this scenario is `{role_id}`")


def _unknown_location(loc: Loc, location_id: str) -> dict[str, Any]:
    return _error(loc, "unknown_location", f"no location of this scenario is `{location_id}`")


def stored_document(document: Any) -> dict[str, Any]:
    """``document`` as a stored scenario draft: valid as ``ScenarioDraft``, keeping every save
    rule, each seed with an ``id``; 422 ``validation_error`` at ``body.<path>`` otherwise (every
    finding in ``details.errors``)."""
    try:
        draft = ScenarioDraft.model_validate(document)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None
    errors = rule_errors(draft)
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
        fields[_dotted((*loc, "text"))] = seed.text
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


def _dotted(loc: Loc) -> str:
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else part)
    return path


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


# -- the publish validation --------------------------------------------------------------------


def publish_report(document: Any) -> PublishReport:
    """What stands in the way of publishing ``document``: it must be a valid scenario draft
    keeping the save rules, it must name the scenario, and every seed must declare its holder,
    audience and disclosure. #111 replaces this builder with its full validation."""
    invalid = schema_report(ScenarioDraft, document)
    if invalid is not None:
        return invalid
    draft = ScenarioDraft.model_validate(document)
    errors = [
        ReportEntry(code=str(e["type"]), field=_dotted(e["loc"]), message=str(e["msg"]))
        for e in rule_errors(draft)
    ]
    if draft.public_card.name is None:
        errors.append(
            ReportEntry(
                code="field_required",
                field="public_card.name",
                message="a published scenario needs a name",
            )
        )
    for loc, seed in draft.seeds():
        for name in ("holder", "audience", "disclosure"):
            if getattr(seed, name) is None:
                errors.append(
                    ReportEntry(
                        code="field_required",
                        field=_dotted((*loc, name)),
                        message=f"a published seed declares its {name}",
                    )
                )
    return PublishReport(errors=tuple(errors))


class ScenarioValidator:
    """The scenario's publish validation (``publish_report``) behind the ``Validator``
    protocol; #111 replaces it with its runner."""

    async def validate(self, draft: Any, ctx: ValidationContext) -> PublishReport:
        return publish_report(draft)


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
    rating's current version."""
    if version_row is not None:
        version_id = version_row.content_rating_version_id
    elif draft.metadata.content_rating_id is not None:
        version_id = await conn.scalar(
            select(content_ratings.c.current_version_id).where(
                content_ratings.c.id == draft.metadata.content_rating_id
            )
        )
    else:
        version_id = None
    if version_id is None:
        return None
    row = (
        await conn.execute(
            select(content_rating_versions.c.rating_id, content_rating_versions.c.name).where(
                content_rating_versions.c.version_id == version_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return CardRating(id=row.rating_id, version_id=version_id, name=row.name)


class ScenarioCatalog(CatalogService):
    """Scenarios on the generic catalog service (see the module docstring)."""

    entity_type = EntityType.SCENARIO
    noun = "scenario"
    id_key = "scenario_id"
    catalog = scenario_catalog
    versions = scenario_versions
    not_found_error = ScenarioNotFound

    def max_bytes(self) -> int:
        return DRAFT_MAX_BYTES  # read at call time, so a test may lower it

    def stored(self, document: Any) -> dict[str, Any]:
        return stored_document(document)

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
        self, conn: AsyncConnection, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        rating = _content_rating(document)
        return {
            "content_rating_version_id": None
            if rating is None
            else await rating_version(conn, rating)
        }

    async def recorded(
        self,
        conn: AsyncConnection,
        entry_id: str,
        document: Mapping[str, Any],
        *,
        version: int | None,
    ) -> None:
        draft = ScenarioDraft.model_validate(document)
        await index_references(
            conn,
            DRAFT_RECORD if version is None else VERSION_RECORD,
            entry_id if version is None else f"{entry_id}@{version}",
            story_id=None,
            fields=indexed_fields(draft),
            subjects=declared_subjects(draft),
        )

    async def forgotten(self, conn: AsyncConnection, entry_id: str) -> None:
        await drop_references(conn, DRAFT_RECORD, entry_id)
        await conn.execute(
            sql_delete(subject_references).where(
                and_(
                    subject_references.c.record_table == VERSION_RECORD,
                    subject_references.c.record_id.like(f"{entry_id}@%"),
                )
            )
        )

    async def project(self, conn: AsyncConnection, shown: Shown, projection: Projection) -> Any:
        entry = shown.entry
        draft = ScenarioDraft.model_validate(shown.document)
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


SERVICE = ScenarioCatalog(ScenarioValidator())
"""The scenario catalog's service."""


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
    ``draft``, listing status ``private``), owned by ``request.owner`` or else ``principal``."""
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
    "ScenarioValidator",
    "VersionSummary",
    "archive",
    "create",
    "declared_subjects",
    "delete",
    "get",
    "indexed_fields",
    "media_references",
    "publish",
    "publish_report",
    "rule_errors",
    "save",
    "stored_document",
    "unarchive",
    "versions",
]
