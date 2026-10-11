"""Story lifecycle: create with an inline cast, archive, unarchive, delete, and read (FR-ADMIN-1).

Creation (#50, built on #45's genesis helper) validates everything first: the cast (1 to 20
characters, names unique ignoring case, each with a required true age), the settings (the full
``StorySettings`` schema, unknown keys refused), the default output viewpoint (a character
**name**, resolved to ``character:<id>``) and the rating (the named one if assignable, else the
deployment default, #48). It then inserts the story row and commits one ``genesis`` operation
whose ``story_created`` event carries the cast's ids, names and ``is_player`` (never ages) and
whose commit step inserts the ``characters`` rows. If the genesis commit fails, the story row
is removed again, so a failed creation leaves no story.

Archive and unarchive are control operations that do not bump the timeline revision; an
archived story is read-only (``Operation`` refuses revision-bumping work with
``story_archived``). Delete is one transaction of enumerated ``DELETE … WHERE story_id``
statements over ``STORY_TABLES`` (FR-STORE-16): no foreign key cascades, no soft delete. It is
recorded by a story-less operation, so the record outlives the story, and by a
``story_deleted`` row in the deployment-level ``audit_log``, whose rows for the story are kept
with ``story_id`` nulled (#56).

Stories are self-contained (FR-STORE-14): no row of a story-scoped table names another story.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints
from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit
from epistrel.core.enums import OperationKind, OperationStatus, Provenance, StoryStatus
from epistrel.core.errors import (
    InvalidRequest,
    StoryArchived,
    StoryBusy,
    StoryNotFound,
    ViewpointUnresolved,
)
from epistrel.core.events import Operation
from epistrel.core.events.types import (
    CastMember,
    StoryArchivedEvent,
    StoryCreated,
    StoryDeletedEvent,
    StoryUnarchivedEvent,
)
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import require_assignable
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, StorySettings
from epistrel.core.tables import (
    audit_log,
    characters,
    client_events,
    config_changes,
    content_ratings,
    deployment_state,
    events,
    operations,
    policies,
    stories,
    story_heads,
    story_texts,
    transcript_messages,
)
from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

Database = AsyncEngine | AsyncConnection

CAST_MIN = 1
CAST_MAX = 20
CHARACTER_NAME_MAX = 80
DEFINITION_MAX = 20_000
STORY_NAME_MAX = 200
SAFE_FALLBACK_MAX = 500

STORY_TABLES: tuple[Table, ...] = (
    client_events,
    events,
    transcript_messages,
    story_heads,
    story_texts,
    policies,
    characters,
    operations,
)
"""Every table with a ``story_id`` column, in the order deletion empties them (events before the
operations they belong to). ``tests/integration/test_s_delete.py`` checks it against
``information_schema``, so a new story-scoped table cannot be missed. The ``stories`` row goes
last; story-scoped ``config_changes`` rows (``scope = 'story'``) go by ``scope_id``."""

STORY_ID_EXCEPTIONS: frozenset[str] = frozenset({audit_log.name})
"""Deployment-level tables that carry a ``story_id`` but outlive the story: deletion nulls the
column instead of removing the row. ``audit_log`` (#56) is the one member."""

STORY_DELETE_SETTING = "epistrel.allow_story_delete"
"""The transaction setting the ``events`` trigger reads to let one story's events be deleted."""

CONFIG_SCOPE_STORY = "story"


# -- request models -------------------------------------------------------------------------

CharacterName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=CHARACTER_NAME_MAX)
]


class CharacterSpec(BaseModel):
    """One character of a new story's cast. ``true_age`` is required (FR-CAT-12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: CharacterName
    definition: str = Field(min_length=1, max_length=DEFINITION_MAX)
    """Free text the Lite pipeline renders verbatim (structured characters arrive in M3)."""
    true_age: StrictInt = Field(ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)
    is_player: StrictBool = False
    safe_fallback_text: str | None = Field(default=None, max_length=SAFE_FALLBACK_MAX)
    """What the character says when every regenerated reply fails its guardrails (#68)."""


class CreationSettings(StorySettings):
    """The settings a story is created with: the full ``StorySettings`` schema, unknown keys
    refused. ``default_output_viewpoint`` names a character of the cast **by name** here; it is
    stored as ``character:<id>``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StorySpec(BaseModel):
    """``POST /stories``: a blank story with its cast, rating and settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, max_length=STORY_NAME_MAX)
    characters: list[CharacterSpec] = Field(min_length=CAST_MIN, max_length=CAST_MAX)
    content_rating_id: str | None = None
    settings: CreationSettings = Field(default_factory=CreationSettings)


# -- results --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RatingRef:
    """A story's rating as responses show it: the rating, its current version, its name."""

    id: str
    version_id: str
    name: str


@dataclass(frozen=True, slots=True)
class CharacterRef:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class CreatedStory:
    story_id: str
    characters: tuple[CharacterRef, ...]
    timeline_revision: int
    content_rating: RatingRef


@dataclass(frozen=True, slots=True)
class CharacterView:
    """A character as a read returns it; ``true_age`` is ``None`` unless the read is omniscient."""

    id: str
    name: str
    is_player: bool
    true_age: int | None = None


@dataclass(frozen=True, slots=True)
class StoryView:
    id: str
    name: str | None
    status: StoryStatus
    timeline_revision: int
    next_turn_index: int
    content_rating: RatingRef
    settings: dict[str, Any]
    created_at: datetime
    archived_at: datetime | None
    characters: tuple[CharacterView, ...] = ()


@dataclass(frozen=True, slots=True)
class StatusChange:
    """The result of archive or unarchive: ``changed`` is false for an idempotent repeat."""

    status: StoryStatus
    timeline_revision: int
    changed: bool


# -- create ---------------------------------------------------------------------------------


async def create_story(
    db: Database,
    spec: StorySpec | None = None,
    *,
    principal: str,
    title: str | None = None,
    story_id: str | None = None,
    content_rating_id: str | None = None,
) -> str:
    """Create a story and return its id (#45's helper, kept for the event-store tests).

    Without ``spec`` the story gets a one-character cast (a player character) named ``title``;
    ``content_rating_id`` overrides the spec's.
    """
    if spec is None:
        spec = StorySpec(
            name=title,
            characters=[
                CharacterSpec(
                    name="Player", definition="The player character.", true_age=30, is_player=True
                )
            ],
        )
    if content_rating_id is not None:
        spec = spec.model_copy(update={"content_rating_id": content_rating_id})
    created = await create_story_from(db, spec, principal=principal, story_id=story_id)
    return created.story_id


async def create_story_from(
    db: Database, spec: StorySpec, *, principal: str, story_id: str | None = None
) -> CreatedStory:
    """Create a story from ``spec`` atomically: the story row, its cast and its genesis
    operation (``story_created``) exist together or not at all."""
    _check_unique_names(spec.characters)
    story_id = story_id or new_ulid()
    cast = [(new_ulid(), character) for character in spec.characters]
    settings = _stored_settings(spec, cast)
    async with _transaction(db) as conn:
        rating = await _rating_ref(conn, await resolve_story_rating(conn, spec.content_rating_id))
        await conn.execute(
            insert(stories).values(
                id=story_id, name=spec.name, settings=settings, content_rating_id=rating.id
            )
        )

    async def insert_cast(conn: AsyncConnection) -> None:
        # One multi-row VALUES (not executemany) so ``age_recorded_at`` can be the commit's
        # ``transaction_timestamp()``.
        await conn.execute(
            insert(characters).values(
                [
                    {
                        "id": character_id,
                        "story_id": story_id,
                        "ordinal": ordinal,
                        "name": character.name,
                        "definition": character.definition,
                        "true_age": character.true_age,
                        "age_recorded_at": func.transaction_timestamp(),
                        "is_player": character.is_player,
                        "safe_fallback_text": character.safe_fallback_text,
                    }
                    for ordinal, (character_id, character) in enumerate(cast)
                ]
            )
        )

    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.GENESIS,
        principal=principal,
        provenance=Provenance.CONSUMER,
    )
    try:
        async with op:
            op.append(
                "story_created",
                StoryCreated(
                    name=spec.name,
                    characters=[
                        CastMember(
                            id=character_id, name=character.name, is_player=character.is_player
                        )
                        for character_id, character in cast
                    ],
                    settings=settings,
                    content_rating_id=rating.id,
                    rating_version_id=rating.version_id,
                ),
            )
            op.add_commit_step(insert_cast)
            await op.commit()
    except BaseException as exc:
        try:
            async with _transaction(db) as conn:
                await _delete_story_rows(conn, story_id)
        except Exception as cleanup:  # the original error stays the error
            exc.add_note(
                f"story {story_id} could not be removed after a failed creation: "
                f"{type(cleanup).__name__}"
            )
        raise
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return CreatedStory(
        story_id=story_id,
        characters=tuple(CharacterRef(id=cid, name=c.name) for cid, c in cast),
        timeline_revision=op.timeline_revision,
        content_rating=rating,
    )


def _check_unique_names(cast: Sequence[CharacterSpec]) -> None:
    seen: dict[str, int] = {}
    for index, character in enumerate(cast):
        key = character.name.casefold()
        if key in seen:
            raise InvalidRequest(
                f"characters {seen[key]} and {index} have the same name",
                {"field": f"body.characters[{index}].name", "reason": "duplicate"},
            )
        seen[key] = index


def _stored_settings(spec: StorySpec, cast: Sequence[tuple[str, CharacterSpec]]) -> dict[str, Any]:
    """The settings document stored on the story: the default viewpoint resolved by name to
    ``character:<id>``, or the sole ``is_player`` character's when none is named."""
    named = spec.settings.default_output_viewpoint
    field = "body.settings.default_output_viewpoint"
    if named is None:
        players = [character_id for character_id, c in cast if c.is_player]
        viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{players[0]}" if len(players) == 1 else None
    else:  # ``omniscient`` was refused by ``StorySettings`` (422 ``validation_error``)
        matches = [cid for cid, c in cast if c.name.casefold() == named.strip().casefold()]
        if not matches:
            raise ViewpointUnresolved(
                "the default output viewpoint names no character of the cast",
                {"field": field, "reason": "unknown_character"},
            )
        viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{matches[0]}"
    values = spec.settings.model_dump(mode="json")
    values["default_output_viewpoint"] = viewpoint
    return StorySettings.model_validate(values).model_dump(mode="json")


async def resolve_story_rating(conn: AsyncConnection, content_rating_id: str | None) -> str:
    """The rating a new story gets: the named one if assignable, else the deployment default.

    Holds the deployment row and the rating's head ``FOR SHARE`` until the caller commits, so
    neither the default nor the rating can change underneath the insert.
    """
    if content_rating_id is None:
        content_rating_id = (
            await conn.execute(
                select(deployment_state.c.default_content_rating_id)
                .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
                .with_for_update(read=True)
            )
        ).scalar_one()
    return await require_assignable(conn, content_rating_id)


async def _rating_ref(conn: AsyncConnection, rating_id: str) -> RatingRef:
    row = (
        await conn.execute(
            select(content_ratings.c.current_version_id, content_ratings.c.name).where(
                content_ratings.c.id == rating_id
            )
        )
    ).one()
    return RatingRef(id=rating_id, version_id=row.current_version_id, name=row.name)


def validate_spec(spec: StorySpec) -> None:
    """The request checks creation makes before anything else (names, default viewpoint), so a
    caller can run them ahead of its own prechecks (#44's order: body validation first)."""
    _check_unique_names(spec.characters)
    _stored_settings(spec, [(new_ulid(), character) for character in spec.characters])


async def require_writable(db: AsyncEngine, story_id: str) -> None:
    """404 when the story does not exist, 409 ``story_archived`` when it is archived: the
    read-only precheck of every truth-changing call, before its gate or operation (#56)."""
    async with db.connect() as conn:
        status = await conn.scalar(select(stories.c.status).where(stories.c.id == story_id))
    if status is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    if status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )


# -- archive --------------------------------------------------------------------------------


async def archive_story(db: AsyncEngine, story_id: str, *, principal: str) -> StatusChange:
    """Make the story read-only (``story_archived`` control event; no revision bump)."""
    return await _set_archived(db, story_id, archived=True, principal=principal)


async def unarchive_story(db: AsyncEngine, story_id: str, *, principal: str) -> StatusChange:
    """Make an archived story writable again (``story_unarchived``)."""
    return await _set_archived(db, story_id, archived=False, principal=principal)


async def _set_archived(
    db: AsyncEngine, story_id: str, *, archived: bool, principal: str
) -> StatusChange:
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(stories.c.status, stories.c.timeline_revision).where(
                    stories.c.id == story_id
                )
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        status = StoryStatus(row.status)
        is_archived = status is StoryStatus.ARCHIVED
        if is_archived == archived:  # idempotent: nothing to do, nothing written
            return StatusChange(status, int(row.timeline_revision), changed=False)
        await _refuse_busy(conn, story_id, turns_only=True)
    target = StoryStatus.ARCHIVED if archived else StoryStatus.ACTIVE
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=False,
    )

    async def apply(conn: AsyncConnection) -> None:
        await _refuse_busy(conn, story_id, turns_only=True, own=op.operation_id)
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(
                status=target.value,
                archived_at=func.transaction_timestamp() if archived else None,
            )
        )

    async with op:
        op.append(
            "story_archived" if archived else "story_unarchived",
            StoryArchivedEvent() if archived else StoryUnarchivedEvent(),
        )
        op.add_commit_step(apply)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return StatusChange(target, op.timeline_revision, changed=True)


async def _refuse_busy(
    conn: AsyncConnection, story_id: str, *, turns_only: bool, own: str | None = None
) -> None:
    """423 ``story_busy`` while another operation of the story (a turn, with ``turns_only``) is
    in flight."""
    query = select(operations.c.operation_id).where(
        operations.c.story_id == story_id,
        operations.c.status == OperationStatus.IN_FLIGHT.value,
    )
    if turns_only:
        query = query.where(operations.c.kind == OperationKind.TURN.value)
    if own is not None:
        query = query.where(operations.c.operation_id != own)
    busy = await conn.scalar(query.limit(1))
    if busy is not None:
        raise StoryBusy(
            "the story has an operation in flight; retry when it finishes",
            {"reason": "in_flight", "operation_id": busy, "story_id": story_id},
        )


# -- delete ---------------------------------------------------------------------------------


async def delete_story(db: AsyncEngine, story_id: str, *, principal: str) -> dict[str, int]:
    """Remove every row of ``story_id`` and nothing else (FR-STORE-16); return rows per table.

    One transaction under the story row lock: 404 when the story does not exist, 423
    ``story_busy`` while any of its operations is in flight. Archived stories can be deleted.
    The deletion is recorded by a story-less ``control`` operation whose ``story_deleted``
    audit event names the story and the acting principal, and by a ``story_deleted``
    ``audit_log`` row (null ``story_id``; the story's earlier audit rows are detached).
    """
    async with db.connect() as conn:
        exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
    if exists is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    removed: dict[str, int] = {}

    async def remove(conn: AsyncConnection) -> None:
        locked = await conn.scalar(
            select(stories.c.id).where(stories.c.id == story_id).with_for_update()
        )
        if locked is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        await _refuse_busy(conn, story_id, turns_only=False)
        removed.update(await _delete_story_rows(conn, story_id))
        await audit.record(
            conn,
            audit.AuditKind.STORY_DELETED,
            principal=principal,
            details={"story_id": story_id, "operation_id": op.operation_id},
        )

    op = Operation(
        db,
        story_id=None,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=False,
    )
    async with op:
        op.append("story_deleted", StoryDeletedEvent(story_id=story_id))
        op.add_commit_step(remove)
        await op.commit()
    return removed


async def _delete_story_rows(conn: AsyncConnection, story_id: str) -> dict[str, int]:
    """``DELETE … WHERE story_id`` over every story table, then the story row (in the caller's
    transaction). Sets ``epistrel.allow_story_delete`` so the events trigger lets exactly this
    story's events go."""
    await conn.execute(select(func.set_config(STORY_DELETE_SETTING, story_id, True)))
    removed: dict[str, int] = {}
    for table in STORY_TABLES:
        result = await conn.execute(delete(table).where(table.c.story_id == story_id))
        removed[table.name] = result.rowcount
    result = await conn.execute(
        delete(config_changes).where(
            config_changes.c.scope == CONFIG_SCOPE_STORY, config_changes.c.scope_id == story_id
        )
    )
    removed[config_changes.name] = result.rowcount
    await audit.detach_story(conn, story_id)  # kept, no longer naming the story
    result = await conn.execute(delete(stories).where(stories.c.id == story_id))
    removed[stories.name] = result.rowcount
    return removed


# -- read -----------------------------------------------------------------------------------


def _story_columns() -> tuple[Any, ...]:
    return (
        stories.c.id,
        stories.c.name,
        stories.c.status,
        stories.c.timeline_revision,
        stories.c.next_turn_index,
        stories.c.settings,
        stories.c.created_at,
        stories.c.archived_at,
        stories.c.content_rating_id,
        content_ratings.c.current_version_id,
        content_ratings.c.name.label("rating_name"),
    )


def _view(row: Any, cast: tuple[CharacterView, ...] = ()) -> StoryView:
    return StoryView(
        id=row.id,
        name=row.name,
        status=StoryStatus(row.status),
        timeline_revision=int(row.timeline_revision),
        next_turn_index=int(row.next_turn_index),
        content_rating=RatingRef(
            id=row.content_rating_id, version_id=row.current_version_id, name=row.rating_name
        ),
        settings=dict(row.settings or {}),
        created_at=row.created_at,
        archived_at=row.archived_at,
        characters=cast,
    )


async def get_story(db: AsyncEngine, story_id: str, *, omniscient: bool = False) -> StoryView:
    """The story with its cast in order. True ages only when ``omniscient`` (FR-CAT-12)."""
    async with db.connect() as conn, conn.begin():
        row = (
            await conn.execute(
                select(*_story_columns())
                .join(content_ratings, content_ratings.c.id == stories.c.content_rating_id)
                .where(stories.c.id == story_id)
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        cast = (
            await conn.execute(
                select(
                    characters.c.id,
                    characters.c.name,
                    characters.c.is_player,
                    characters.c.true_age,
                )
                .where(characters.c.story_id == story_id)
                .order_by(characters.c.ordinal)
            )
        ).all()
    return _view(
        row,
        tuple(
            CharacterView(
                id=c.id,
                name=c.name,
                is_player=bool(c.is_player),
                true_age=int(c.true_age) if omniscient else None,
            )
            for c in cast
        ),
    )


async def list_stories(db: AsyncEngine, *, status: StoryStatus | None = None) -> list[StoryView]:
    """The deployment's stories, newest first, optionally of one status (no cast)."""
    query = select(*_story_columns()).join(
        content_ratings, content_ratings.c.id == stories.c.content_rating_id
    )
    if status is not None:
        query = query.where(stories.c.status == status.value)
    async with db.connect() as conn:
        found = (
            await conn.execute(query.order_by(stories.c.created_at.desc(), stories.c.id.desc()))
        ).all()
    return [_view(row) for row in found]


# -- helpers --------------------------------------------------------------------------------


@asynccontextmanager
async def _transaction(db: Database) -> AsyncIterator[AsyncConnection]:
    if isinstance(db, AsyncConnection):
        async with db.begin():
            yield db
        return
    async with db.begin() as conn:
        yield conn
