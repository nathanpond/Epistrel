"""Companion scenarios (FR-CAT-5, FR-CAT-10, FR-RATE-5; docs/02 §4.10; #116): one per catalog
character, created with it, versioned beside it and pinned to it.

**Creation.** Creating a catalog character (``CatalogService.create_in``, so a promotion from a
story, #114, too) creates its companion in the same transaction (``create``): a
``scenario_catalog`` row with ``companion_of`` = the character, the character's owner and
listing status, status ``draft``, at draft revision 1, whose draft is the companion's defaults
(``companion_slots.defaults``: the ``npc`` slot locked to the character with no version yet, the
``player`` slot, the character's name and content rating, an empty world). This is service code,
not a database trigger; and the two slots are kept by the scenario save (422
``companion_slots_fixed``) and by the validation's companion check, not by a constraint, since
roles live in the document (jsonb).

**Following the character.** The companion's owner and listing status always equal its
character's: they change with the character's (#104, #110) and never alone (422
``companion_follows_character``), and so do archiving, unarchiving and deletion (``set_archived``,
``drop``: a deleted companion records its own ``deleted`` ownership event and
``catalog_entry_deleted`` audit row). Its content rating starts as the character's, and its
draft takes the character's rating whenever the character's changes (``follow_rating``) until
the rating is set on the companion itself (``content_rating_explicit``). Nothing else of the
character is followed.

**Publishing (FR-CAT-10).** A character and its companion keep separate version sequences,
linked by the pin (``companion_slots``). Publishing the character (``publish``) is one
transaction, under row locks taken character first, then companion, as every companion path
takes them:

1. the character's draft is validated (422 ``publish_invalid``) and inserted as version n+1,
   which becomes the character's latest;
2. a companion version is made (``copy_companion``): with ``with_companion``, the companion's
   draft; otherwise the latest published companion content, or, on the character's first
   publish, the companion's defaults (an unpublished companion draft is never published by
   this). It is pinned to n+1, validated as part of the publish (the copy needs no world or
   opening; with its draft, the full rules apply), and inserted as version k+1 with
   ``pinned_character_version`` = n+1;
3. the companion's draft is pinned to n+1 (with ``with_companion``, it is what was published),
   at the next draft revision.

Any refusal or failure rolls everything back: no character version exists without its
companion version. Every refusal names the side it is about (``details.entity``: ``character``
or ``companion``, with its id). Each draft's revision is compared before preparing and again
under the locks (FR-CAT-3): a stale ``draft_revision`` or ``companion_draft_revision`` is 409
``draft_stale``, and so is a companion published since a character-alone publish read it. The
validation's prepare phase (input policies, which may call a model) runs before the
transaction (FR-CONC-2). The companion published alone is the scenario service's
(``ScenarioCatalog.publish_alone``).

A rating difference between the two warns and never blocks (FR-RATE-5): the companion's report
carries ``rating_looser`` (the validation's rating-mismatch and companion checks).

``get`` reads a character's companion (``GET /catalog/characters/{id}/companion``): what a
scenario read gives, and the draft while the companion was never published.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control
from epistrel.core.catalog import scenarios
from epistrel.core.catalog.checks.character_documents import name_of, rating_id_of
from epistrel.core.catalog.companion_slots import defaults, pinned_columns, repinned
from epistrel.core.catalog.models import CharacterPublishRequest
from epistrel.core.catalog.report import DRAFT, ValidationReport, VersionLabel
from epistrel.core.catalog.scenario_models import ScenarioCard, ScenarioFull
from epistrel.core.catalog.service import (
    FIRST_DRAFT_REVISION,
    CatalogService,
    Projection,
    Published,
    PublishRequest,
    config_revision,
)
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import (
    CharacterNotFound,
    DraftStale,
    EntryArchived,
    EpistrelError,
    InvalidRequest,
    ScenarioNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.ports import Ports
from epistrel.core.tables.catalog import character_catalog, scenario_catalog, scenario_versions
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses

SCENARIOS = scenarios.SERVICE
"""The scenario catalog's service: a companion is a scenario."""

COPY_WAIVED = frozenset({"world_required", "opening_required"})
"""What a companion version copied by a character-alone publish need not hold (FR-CAT-10: the
defaults are "its two slots and an empty world")."""

CHARACTER = "character"
COMPANION = "companion"


@dataclass(frozen=True, slots=True)
class CompanionPublished:
    """The companion version a character's publish made: the companion, its new version, its
    draft's new revision (the draft is pinned to the new character version) and the validation
    report of the version (its warnings, ``rating_looser`` among them, and the checks this
    deployment cannot run)."""

    id: str
    version: int
    draft_revision: int
    report: ValidationReport


@dataclass(frozen=True, slots=True)
class CharacterPublished(Published):
    """A character's publish: the version it made (``Published``) and the companion version
    made with it (``companion``)."""

    companion: CompanionPublished = field(kw_only=True)


# -- reads ---------------------------------------------------------------------------------------


async def companion_of(conn: AsyncConnection, character_id: str, *, lock: bool = False) -> Any:
    """The companion row of ``character_id`` (``None`` when it has none), locked ``FOR UPDATE``
    with ``lock``."""
    query = select(scenario_catalog).where(scenario_catalog.c.companion_of == character_id)
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()


async def companion_id(conn: AsyncConnection, character_id: str) -> str | None:
    """The id of ``character_id``'s companion scenario (``None`` when it has none)."""
    found = await conn.scalar(
        select(scenario_catalog.c.id).where(scenario_catalog.c.companion_of == character_id)
    )
    return None if found is None else str(found)


async def get(
    db: AsyncEngine,
    character_id: str,
    *,
    version: int | None = None,
    draft: bool = False,
    projection: Projection = Projection.CARD,
) -> ScenarioCard | ScenarioFull:
    """``character_id``'s companion scenario, as a scenario read gives it (``version``,
    ``draft`` and ``projection`` as there): 404 for an unknown character. A read of the latest
    version of a companion never published gives its draft."""
    async with db.connect() as conn:
        known = await conn.scalar(
            select(character_catalog.c.id).where(character_catalog.c.id == character_id)
        )
        if known is None:
            raise CharacterNotFound(
                f"catalog character {character_id} does not exist", {"character_id": character_id}
            )
        row = await companion_of(conn, character_id)
    if row is None:
        raise ScenarioNotFound(
            f"catalog character {character_id} has no companion scenario",
            {"character_id": character_id},
        )
    if version is None and not draft and row.current_version is None:
        draft = True
    found: ScenarioCard | ScenarioFull = await SCENARIOS.get(
        db, str(row.id), version=version, draft=draft, projection=projection
    )
    return found


# -- following the character --------------------------------------------------------------------


async def create(
    conn: AsyncConnection,
    character_id: str,
    document: Mapping[str, Any],
    *,
    owner: str,
    listing_status: str,
) -> str:
    """Create the companion of the character ``character_id`` (its draft ``document``) inside
    the character's creation (see the module docstring); answers its id."""
    draft = scenarios.stored_document(
        defaults(character_id, name=name_of(document), rating_id=rating_id_of(document), pin=None),
        companion=True,
    )
    scenario_id = new_ulid()
    now = clock.now()
    await conn.execute(
        insert(scenario_catalog).values(
            id=scenario_id,
            owner_principal=owner,
            listing_status=listing_status,
            status=EntryStatus.DRAFT.value,
            current_version=None,
            draft=draft,
            draft_revision=FIRST_DRAFT_REVISION,
            companion_of=character_id,
            content_rating_explicit=False,
            created_at=now,
            updated_at=now,
        )
    )
    await SCENARIOS.recorded(conn, scenario_id, draft, version=None)
    return scenario_id


async def follow_rating(
    conn: AsyncConnection,
    character_id: str,
    document: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> None:
    """A character save that changes the character's content rating gives its companion's
    draft the same rating, at the next draft revision, unless the companion's rating was set
    explicitly."""
    rating = rating_id_of(document)
    if rating == rating_id_of(previous):
        return
    row = await companion_of(conn, character_id, lock=True)
    if row is None or row.content_rating_explicit or rating_id_of(row.draft) == rating:
        return
    draft = dict(row.draft)
    metadata = dict(draft.get("metadata") or {})
    if rating is None:
        metadata.pop("content_rating_id", None)
    else:
        metadata["content_rating_id"] = rating
    draft["metadata"] = metadata
    await conn.execute(
        update(scenario_catalog)
        .where(scenario_catalog.c.id == row.id)
        .values(
            draft=draft,
            draft_revision=scenario_catalog.c.draft_revision + 1,
            updated_at=clock.now(),
        )
    )


async def set_archived(conn: AsyncConnection, character_id: str, archived: bool) -> None:
    """Archive (``archived``) or unarchive the character's companion with it, in its
    transaction (a companion already in that state is left as it is)."""
    row = await companion_of(conn, character_id, lock=True)
    if row is not None and (row.status == EntryStatus.ARCHIVED.value) != archived:
        await SCENARIOS.set_status(conn, row, archived)


async def drop(
    conn: AsyncConnection, character_id: str, *, principal: str
) -> list[tuple[EntityType, str]]:
    """Delete the character's companion (its versions, relations and subject references) in the
    character's delete, before the character's row goes; answers it for its audit row."""
    row = await companion_of(conn, character_id, lock=True)
    if row is None:
        return []
    await SCENARIOS.remove(conn, str(row.id), principal=principal)
    return [(EntityType.SCENARIO, str(row.id))]


# -- publishing ----------------------------------------------------------------------------------


def _about(entity: str, error: EpistrelError) -> EpistrelError:
    """``error`` naming the side of a character's publish it is about (``details.entity``)."""
    error.details = {"entity": entity, **error.details}
    return error


def companion_revision(request: PublishRequest, with_companion: bool) -> int | None:
    """The companion draft revision a character's publish names: required with
    ``with_companion``, refused without (422 ``validation_error`` at
    ``body.companion_draft_revision``)."""
    revision = (
        request.companion_draft_revision if isinstance(request, CharacterPublishRequest) else None
    )
    if with_companion and revision is None:
        raise InvalidRequest(
            "`companion_draft_revision` is required to publish the companion's draft with the "
            "character (`with_companion=true`)",
            {"field": "body.companion_draft_revision"},
        )
    if not with_companion and revision is not None:
        raise InvalidRequest(
            "`companion_draft_revision` is only sent with `with_companion=true`",
            {"field": "body.companion_draft_revision"},
        )
    return revision


def _refuse_companion(row: Any, revision: int) -> None:
    """A companion draft published with its character must be editable at ``revision``."""
    if row.status == EntryStatus.ARCHIVED.value:
        raise _about(
            COMPANION,
            EntryArchived(
                f"scenario {row.id} is archived; unarchive its character to change it",
                {"scenario_id": str(row.id)},
            ),
        )
    if row.draft_revision != revision:
        raise _about(COMPANION, SCENARIOS.stale(row, str(row.id), revision))


def _moved(read: Any, row: Any | None) -> DraftStale:
    """409 ``draft_stale``: the companion ``read`` before the transaction changed before its
    lock was taken (published since, so the content a character-alone publish would copy is no
    longer the latest; or made again)."""
    return DraftStale(
        f"scenario {read.id}, the companion of catalog character {read.companion_of}, changed "
        "while the character was being published; read both again and retry",
        {
            "entity": COMPANION,
            "scenario_id": str(read.id),
            "draft_revision": int(read.draft_revision),
            "current_draft_revision": None if row is None else int(row.draft_revision),
        },
    )


def _refuse_character(
    service: CatalogService, entry: Any, character_id: str, draft_revision: int
) -> None:
    """The character's own refusals (422 ``entry_archived``, 409 ``draft_stale``), naming it."""
    try:
        service.refuse(entry, character_id, draft_revision)
    except EpistrelError as error:
        raise _about(CHARACTER, error) from None


async def _ensure(db: AsyncEngine, service: CatalogService, character_id: str) -> Any:
    """The character's companion, created first (in a transaction of its own, under the
    character's row lock) when it has none: every character has one, made with it, but a row
    removed by hand is made again rather than leave a character version without a companion."""
    async with db.connect() as conn:
        row = await companion_of(conn, character_id)
    if row is not None:
        return row
    async with db.begin() as conn:
        entry = await service.entry(conn, character_id, lock=True)
        if await companion_of(conn, character_id) is None:
            await create(
                conn,
                character_id,
                entry.draft,
                owner=str(entry.owner_principal),
                listing_status=str(entry.listing_status),
            )
    async with db.connect() as conn:
        return await companion_of(conn, character_id)


async def _content(
    db: AsyncEngine, character: Any, row: Any, pin: int, with_companion: bool
) -> tuple[dict[str, Any], VersionLabel, frozenset[str]]:
    """The companion version a character's publish makes, pinned to ``pin``: its content, the
    version label its report names and the codes it waives (see the module docstring)."""
    if with_companion:
        return repinned(row.draft, pin), DRAFT, frozenset()
    version = int(row.current_version or 0) + 1
    if row.current_version is None:
        content = defaults(
            str(character.id),
            name=name_of(character.draft),
            rating_id=rating_id_of(character.draft),
            pin=pin,
        )
        return scenarios.stored_document(content, companion=True), version, COPY_WAIVED
    async with db.connect() as conn:
        latest: Mapping[str, Any] = (
            await conn.execute(
                select(scenario_versions.c.content).where(
                    scenario_versions.c.catalog_id == row.id,
                    scenario_versions.c.version == row.current_version,
                )
            )
        ).scalar_one()
    return repinned(latest, pin), version, COPY_WAIVED


async def copy_companion(
    conn: AsyncConnection,
    row: Any,
    *,
    character_id: str,
    pin: int,
    content: Mapping[str, Any],
    label: VersionLabel,
    waived: frozenset[str],
    prepared: Mapping[str, Any],
    with_companion: bool,
    principal: str,
    ports: Ports,
    now: datetime,
) -> CompanionPublished:
    """The companion step of a character's publish, after the character's version is inserted
    and before the transaction commits: validate the companion version (``content``, pinned to
    ``pin``; 422 ``publish_invalid`` naming the companion), insert it, and pin the companion's
    draft (with ``with_companion``, make it what was published) at its next revision."""
    scenario_id = str(row.id)
    report = await SCENARIOS.validated(
        conn,
        scenario_id,
        label,
        content,
        prepared=prepared,
        ports=ports,
        waived=waived,
        details={"entity": COMPANION},
    )
    version = int(row.current_version or 0) + 1
    await SCENARIOS.insert_version(
        conn,
        scenario_id,
        version,
        content,
        report,
        principal=principal,
        now=now,
        values=pinned_columns(character_id, pin),
    )
    draft_revision = int(row.draft_revision) + 1
    await SCENARIOS.mark_published(
        conn,
        scenario_id,
        version=version,
        draft_revision=draft_revision,
        now=now,
        draft=content if with_companion else repinned(row.draft, pin),
    )
    await SCENARIOS.recorded(conn, scenario_id, content, version=version)
    return CompanionPublished(
        id=scenario_id, version=version, draft_revision=draft_revision, report=report
    )


async def publish(
    service: CatalogService,
    db: AsyncEngine,
    character_id: str,
    request: PublishRequest,
    *,
    with_companion: bool = False,
    principal: str,
    ports: Ports,
) -> CharacterPublished:
    """Publish the character's draft as version n+1 and its companion's version with it, in one
    transaction (see the module docstring)."""
    revision = companion_revision(request, with_companion)
    async with db.connect() as conn:
        read = await service.entry(conn, character_id)
    _refuse_character(service, read, character_id, request.draft_revision)
    companion = await _ensure(db, service, character_id)
    if revision is not None:
        _refuse_companion(companion, revision)
    pin = int(read.current_version or 0) + 1
    content, label, waived = await _content(db, read, companion, pin, with_companion)
    # Outside any transaction (FR-CONC-2); the revisions compared under the locks below prove
    # that both prepared documents are the ones published.
    prepared = await service.prepare_for(db, character_id, DRAFT, read.draft, ports=ports)
    prepared_companion = await SCENARIOS.prepare_for(
        db, str(companion.id), label, content, ports=ports, waived=waived
    )
    async with db.begin() as conn:
        entry = await service.entry(conn, character_id, lock=True)
        _refuse_character(service, entry, character_id, request.draft_revision)
        row = await companion_of(conn, character_id, lock=True)
        if (
            row is None
            or row.id != companion.id
            or (revision is None and row.current_version != companion.current_version)
        ):
            raise _moved(companion, row)
        if revision is not None:
            _refuse_companion(row, revision)
        await pauses.pause(pauses.CATALOG_PUBLISH_LOCKED)
        report = await service.validated(
            conn,
            character_id,
            DRAFT,
            entry.draft,
            prepared=prepared,
            ports=ports,
            details={"entity": CHARACTER},
        )
        draft_revision = int(entry.draft_revision) + 1
        now = clock.now()
        await service.insert_version(
            conn, character_id, pin, entry.draft, report, principal=principal, now=now
        )
        # The new version is the character's latest before the companion's is validated, so the
        # companion's checks see the character as published at it.
        status = await service.mark_published(
            conn, character_id, version=pin, draft_revision=draft_revision, now=now
        )
        published = await copy_companion(
            conn,
            row,
            character_id=character_id,
            pin=pin,
            content=content,
            label=label,
            waived=waived,
            prepared=prepared_companion,
            with_companion=with_companion,
            principal=principal,
            ports=ports,
            now=now,
        )
        await control.record_control_operation(conn, actor=principal)
        await service.recorded(conn, character_id, entry.draft, version=pin)
        config = await config_revision(conn)
    return CharacterPublished(
        id=character_id,
        version=pin,
        draft_revision=draft_revision,
        status=status,
        published_at=now,
        report=report,
        config_revision=config,
        companion=published,
    )


__all__ = [
    "COPY_WAIVED",
    "CharacterPublished",
    "CompanionPublished",
    "companion_id",
    "companion_of",
    "companion_revision",
    "copy_companion",
    "create",
    "drop",
    "follow_rating",
    "get",
    "publish",
    "set_archived",
]
