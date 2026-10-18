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

Every story has one owner (``owner_principal``, FR-AUTH-5): the requesting principal, or the
``owner`` the creation names; ``core.ownership`` transfers it and keeps its relations. Deletion
removes the story's relations and records the deletion there (``ownership.drop_entity``). The
list filters by owner and relation only when a request asks for it (FR-AUTH-8).

Every story runs under one engine profile (``engine_profile_id``, FR-PROF-1; #106): a story is
created under the deployment's default profile, read with the deployment row held ``FOR SHARE``
so the default cannot change underneath the insert, and a fork keeps its source's.

**Ad-hoc characters** (FR-CAT-7, FR-CAT-12; #114): ``add_story_character`` adds a character to a
running story, from #50's free-text shape (name, definition, a required true age, an optional safe
fallback line and deflection style) plus optional ``images`` (media asset ids, by reference), never
as a player. It is story-scoped: a ``characters`` row with no ``source_catalog_id``, never a
catalog entry (it becomes one only by promotion, ``core.catalog.promote``). The order of checks:
the story (404) → the cast cap (``CAST_MAX``), a name the story already has (ignoring case) and the
images (each a live image asset) → 409 ``story_archived`` → the expected revision → 423
``story_busy`` while a turn is in flight → the input gate over the definition (and the deflection
style), with the new character's age in the cast → one ``control`` operation that bumps the
revision, as #60's story-text edits do (the cast is a member of the Lite read set, so a turn that
began before it regenerates). The operation records ``character_defined`` (id, name, ``is_player``;
never the age) and inserts the row at the next ordinal, re-checking the cap, the name, the images
and in-flight turns under the story lock. The operation has no position, like the edits: a
truncation keeps the character, and every fork copies it.

**Deflection styles** (FR-GUARD-7; #115): a character of a new story's cast, and one added later,
may carry a ``deflection_style``: how it refuses or changes the subject, in its own voice. It is
stored on the ``characters`` row, checked by the input gate like the definition (it reaches every
Lite prompt), and rendered with the character's author notes, so a reply regenerated after a
guardrail violation stays in character.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import Table, and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, ownership, pause
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.enums import OperationKind, OperationStatus, Provenance, StoryStatus
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.index import drop_many, reindex_story
from epistrel.core.erasure.records import character_record, idempotency_id, index_rows, story_record
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
    CharacterDefined,
    ScenarioOrigin,
    StoryArchivedEvent,
    StoryCreated,
    StoryDeletedEvent,
    StoryUnarchivedEvent,
)
from epistrel.core.genesis.models import CreationReport
from epistrel.core.idempotency import Scope
from epistrel.core.ids import Ulid, new_ulid
from epistrel.core.media.assets import assert_referenceable
from epistrel.core.ownership import Owner, RelationFilter
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles.effective import ProfileVersionRef
from epistrel.core.profiles.impact import ImpactReport
from epistrel.core.ratings import require_assignable
from epistrel.core.resolution.entities import add_entities, character_spec
from epistrel.core.settings_schema import (
    CHARACTER_VIEWPOINT_PREFIX,
    COLUMN_SETTINGS,
    SUPERSEDED_STORY_SETTINGS,
    StorySettings,
    superseded,
)
from epistrel.core.tables import (
    audit_log,
    character_snapshots,
    characters,
    client_events,
    config_changes,
    content_ratings,
    deployment_state,
    engine_profiles,
    events,
    genesis_deferred,
    guardrail_check_results,
    idempotency_keys,
    jobs,
    lite_summary_segments,
    media_assets,
    operations,
    placement,
    policies,
    rating_check_jobs,
    relationships,
    scene_state,
    stories,
    story_heads,
    story_profile_overrides,
    story_texts,
    subject_references,
    timers,
    transcript_messages,
    turn_queue,
    worker_heartbeats,
)
from epistrel.core.tables.characters import DEFLECTION_STYLE_MAX, TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.tables.commitments import promissory_ledger, thunks
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.facts import facts
from epistrel.core.tables.feedback import feedback_history, message_feedback
from epistrel.core.tables.hot_set import overflow_episodes
from epistrel.core.tables.media import MediaKind
from epistrel.core.tables.memories import memories
from epistrel.core.tables.ownership import EntityType
from epistrel.core.tables.propositions import (
    closed_sets,
    entities,
    entity_aliases,
    predicate_proposals,
    predicates,
    propositions,
)
from epistrel.core.tables.readset import proposition_changes
from epistrel.core.trimmed import trim_as_sent

Database = AsyncEngine | AsyncConnection

CAST_MIN = 1
CAST_MAX = 20
CHARACTER_NAME_MAX = 80
DEFINITION_MAX = 20_000
STORY_NAME_MAX = 200
SAFE_FALLBACK_MAX = 500

STORY_TABLES: tuple[Table, ...] = (
    subject_references,
    feedback_history,
    message_feedback,
    client_events,
    events,
    transcript_messages,
    lite_summary_segments,
    story_heads,
    story_texts,
    # The structured projections over canon (#137): caches, rebuilt by a fork.
    scene_state,
    placement,
    relationships,
    story_profile_overrides,
    policies,
    genesis_deferred,
    character_snapshots,
    characters,
    turn_queue,
    jobs,
    rating_check_jobs,
    guardrail_check_results,
    timers,
    # Commitments (#146): derived tables, before the operations they reference (the ledger
    # names thunks, and a thunk the fact that resolved it; none of them has a foreign key).
    promissory_ledger,
    thunks,
    # Bitemporal canon (#133): a derived table, before the operations it references.
    facts,
    # Memory records (#135): a derived table, before the operations it references.
    memories,
    # The hot set's overflow episodes (#147): maintenance state, no foreign key.
    overflow_episodes,
    # The structured read set's change index (#136), before the operations it references.
    proposition_changes,
    # The structured stores (#131): story rows only (core predicates have no story).
    predicate_proposals,
    closed_sets,
    propositions,
    predicates,
    entity_aliases,
    entities,
    operations,
)
"""Every table with a ``story_id`` column, in the order deletion empties them (events before the
operations they belong to). ``tests/integration/test_s_delete.py`` checks it against
``information_schema``, so a new story-scoped table cannot be missed. The ``stories`` row goes
last; story-scoped ``config_changes`` and ``idempotency_keys`` rows (``scope = 'story'``) go by
``scope_id``, and a deployment-scoped key bound to one of the story's operations (its creation)
goes with that operation."""

STORY_TABLES_NOT_COPIED: frozenset[str] = frozenset(
    {
        jobs.name,
        turn_queue.name,
        timers.name,
        audit_log.name,
        idempotency_keys.name,
        worker_heartbeats.name,
        subject_references.name,
        message_feedback.name,
        feedback_history.name,
        rating_check_jobs.name,
        guardrail_check_results.name,
        proposition_changes.name,
        overflow_episodes.name,
    }
)
"""Story-scoped tables a fork (#67) leaves behind: work in progress (``jobs``, the turn queue,
``timers``, worker heartbeats; #71), deployment-level audit
records, idempotency keys (a fork answers its own requests), the subject-reference index
(#105: a story's rows name its records, so #123 re-indexes the fork), and message feedback
(#112: a rating belongs to the message it rated, FR-FB-2; the fork's copy of a message is a
new message that starts unrated, and copying would count one rating twice), and a rating
change's check and verdict cache (#118: a check is the source's work, and the cache's record
keys name the source's events), the read set's change index (#136: it records what changed
under the source's turns; the fork's turns begin from its own revision), and the hot set's
overflow episodes (#147: maintenance state; the fork recounts its counters and its next commit
opens the episodes it needs). Every other table
in ``STORY_TABLES`` is copied by ``core.fork``, so a new story-scoped table is copied unless it
is named here."""

STORY_ID_EXCEPTIONS: frozenset[str] = frozenset({audit_log.name})
"""Deployment-level tables that carry a ``story_id`` but outlive the story: deletion nulls the
column instead of removing the row. ``audit_log`` (#56) is the one member."""

STORY_DELETE_SETTING = "epistrel.allow_story_delete"
"""The transaction setting the ``events`` trigger reads to let one story's events be deleted."""

CONFIG_SCOPE_STORY = "story"


# -- request models -------------------------------------------------------------------------

CharacterName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=CHARACTER_NAME_MAX),
    BeforeValidator(trim_as_sent(CHARACTER_NAME_MAX)),
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
    deflection_style: str | None = Field(
        default=None,
        min_length=1,
        max_length=DEFLECTION_STYLE_MAX,
        description="How the character refuses or changes the subject, in its own voice "
        "(FR-GUARD-7): rendered with its author notes, so a reply regenerated after a "
        "guardrail violation stays in character. Checked by the input gate.",
    )


IMAGES_MAX = 20
"""The most images a character added in a story may show (a promoted draft's primary image and
gallery hold them all, #115's limits)."""


class AdHocCharacter(BaseModel):
    """``POST /stories/{id}/characters``: a character the author adds to a running story
    (FR-CAT-7): #50's free-text shape — its name, its definition, its required true age
    (FR-CAT-12) and optionally its safe fallback line (#68) and deflection style (FR-GUARD-7) —
    and optionally ``images``, media asset ids it shows (each a live image), by reference. It is
    never a player character and never a catalog entry."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "Dale",
                    "definition": "A delivery driver in a hurry, polite and nosy.",
                    "true_age": 52,
                }
            ]
        },
    )

    name: CharacterName
    definition: str = Field(min_length=1, max_length=DEFINITION_MAX)
    """Free text the Lite pipeline renders verbatim."""
    true_age: StrictInt = Field(ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)
    safe_fallback_text: str | None = Field(default=None, max_length=SAFE_FALLBACK_MAX)
    deflection_style: str | None = Field(
        default=None,
        min_length=1,
        max_length=DEFLECTION_STYLE_MAX,
        description="How the character refuses or changes the subject, in its own voice "
        "(FR-GUARD-7). Checked by the input gate.",
    )
    images: list[Ulid] = Field(
        default_factory=list,
        max_length=IMAGES_MAX,
        description="Media asset ids the character shows (images, each live), in order.",
    )

    @field_validator("images")
    @classmethod
    def _distinct_images(cls, images: list[str]) -> list[str]:
        if len(set(images)) != len(images):
            raise ValueError("lists a media asset more than once")
        return images


class CreationSettings(StorySettings):
    """The settings a story is created with: the full ``StorySettings`` schema, unknown keys
    refused (M2's engine dials with the profile setting that replaced each, #113: a story
    created under its profile overrides them afterwards). ``default_output_viewpoint`` names a
    character of the cast **by name** here; it is stored as ``character:<id>``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _superseded(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if key in SUPERSEDED_STORY_SETTINGS:
                    raise superseded(key, f"body.settings.{key}")
        return data


class StorySpec(BaseModel):
    """``POST /stories``: a blank story with its cast, rating and settings, and optionally its
    ``owner`` (default: the requesting principal; never ``system``). Ownership confers no
    permission (FR-AUTH-5,8)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "The Lantern Road",
                    "characters": [
                        {
                            "name": "Wren",
                            "definition": "A traveller, the player's character.",
                            "true_age": 24,
                            "is_player": True,
                        },
                        {
                            "name": "Ada",
                            "definition": "An innkeeper who trusts no one.",
                            "true_age": 41,
                        },
                    ],
                    "settings": {"turn_concurrency": "queue", "default_output_viewpoint": "Wren"},
                }
            ]
        },
    )

    name: str | None = Field(default=None, max_length=STORY_NAME_MAX)
    characters: list[CharacterSpec] = Field(min_length=CAST_MIN, max_length=CAST_MAX)
    content_rating_id: str | None = None
    settings: CreationSettings = Field(default_factory=CreationSettings)
    owner: Owner | None = Field(
        default=None,
        description="The story's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )


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
    role_id: str | None = None
    """The scenario role the character fills (a story created from a scenario, #119)."""


@dataclass(frozen=True, slots=True)
class CreatedStory:
    """A new story: its id, cast, revision and rating. A story created from a scenario (#119)
    also names the scenario version, the engine profile it runs under, its creation report and
    #109's impact report; a blank story leaves them ``None``."""

    story_id: str
    characters: tuple[CharacterRef, ...]
    timeline_revision: int
    content_rating: RatingRef
    scenario: ScenarioOrigin | None = None
    engine_profile: ProfileVersionRef | None = None
    report: CreationReport | None = None
    impact: ImpactReport | None = None
    applied: Literal[True] = True
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class CatalogVersionRef:
    """A catalog entry's version, by identifiers (a story character's ``source``, #119)."""

    catalog_id: str
    version: int


@dataclass(frozen=True, slots=True)
class CharacterView:
    """A character as a read returns it; ``true_age`` (FR-CAT-12) and ``promoted_to`` (the
    catalog entries promoted from it, FR-CAT-8) are ``None`` unless the read is omniscient.
    ``role_id`` and ``source`` (the catalog version it was created from) are a story created
    from a scenario's (#119), ``None`` otherwise."""

    id: str
    name: str
    is_player: bool
    true_age: int | None = None
    promoted_to: tuple[str, ...] | None = None
    role_id: str | None = None
    source: CatalogVersionRef | None = None


@dataclass(frozen=True, slots=True)
class StoryCharacterAdded:
    """A character added to a running story (FR-CAT-7): its id, name and place in the cast, and
    the story's revision after the addition."""

    id: str
    name: str
    ordinal: int
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


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
    owner_principal: str
    characters: tuple[CharacterView, ...] = ()
    engine_profile: ProfileVersionRef | None = None
    """The engine profile the story runs under and the version it runs on (#113): its pin, else
    the profile's latest. Read by ``get_story`` only (a list leaves it ``None``)."""
    pause: dict[str, Any] | None = None
    """While the story is paused (#118): ``{job_id, cause, progress {done, total}}``, the check
    that paused it. Read by ``get_story`` only."""
    scenario: ScenarioOrigin | None = None
    """The scenario version the story was created from (#119); ``None`` for a blank story.
    Read by ``get_story`` only."""


@dataclass(frozen=True, slots=True)
class StatusChange:
    """The result of archive or unarchive: ``changed`` is false for an idempotent repeat."""

    status: StoryStatus
    timeline_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class DeletedStory:
    """What a deletion removed: rows per table (the story's ``config_changes`` and the
    ``stories`` row included)."""

    removed: dict[str, int]
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


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
    document, columns = split_settings(settings)
    async with _transaction(db) as conn:
        rating = await _rating_ref(conn, await resolve_story_rating(conn, spec.content_rating_id))
        await conn.execute(
            insert(stories).values(
                id=story_id,
                name=spec.name,
                owner_principal=spec.owner or principal,
                settings=document,
                content_rating_id=rating.id,
                engine_profile_id=await profiles.default_profile(conn),
                **columns,
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
                        "deflection_style": character.deflection_style,
                    }
                    for ordinal, (character_id, character) in enumerate(cast)
                ]
            )
        )
        await index_rows(conn, characters, characters.c.story_id == story_id, character_record)
        # Every character is an entity of the story, Lite included (FR-PROP-6; #139).
        await add_entities(
            conn,
            story_id,
            [character_spec(character_id, character.name) for character_id, character in cast],
            operation_id=op.operation_id,
        )
        # The story row after its cast (#123): a name that mentions a character needs the
        # character to exist when the row is scanned.
        await index_rows(conn, stories, stories.c.id == story_id, story_record)

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


def split_settings(settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(document, columns)``: the ``stories.settings`` document and the column-backed
    settings (``turn_concurrency``, ``turn_queue_max``; #63), from one settings dump."""
    document = {k: v for k, v in settings.items() if k not in COLUMN_SETTINGS}
    columns = {k: settings[k] for k in COLUMN_SETTINGS if k in settings}
    return document, columns


def settings_of(row: Any) -> dict[str, Any]:
    """A story row's settings as one document: ``settings`` plus the column-backed ones."""
    return {
        **dict(row.settings or {}),
        "turn_concurrency": row.turn_concurrency,
        "turn_queue_max": row.turn_queue_max,
    }


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


async def rating_ref(conn: AsyncConnection, rating_id: str) -> RatingRef:
    """The rating as responses show it: its id, current version and name."""
    return await _rating_ref(conn, rating_id)


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
    """404 when the story does not exist, 409 ``story_archived`` when it is archived, 409
    ``story_paused`` while a check pauses it (#118): the read-only precheck of every
    truth-changing call, before its gate or operation (#56)."""
    async with db.connect() as conn:
        status = await conn.scalar(select(stories.c.status).where(stories.c.id == story_id))
        if status is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        if status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it",
                {"story_id": story_id},
            )
        await pause.refuse(conn, story_id, status)


# -- ad-hoc characters (FR-CAT-7; #114) -----------------------------------------------------

CharacterGate = Callable[[Sequence[tuple[str, str]]], Awaitable[None]]
"""The input gate over ``(locator, text)`` pairs (the Engine's, which owns the Orchestrator);
raises 422 ``guardrail_violation``."""


async def _cast_full(conn: AsyncConnection, story_id: str) -> None:
    """422 ``validation_error`` when the story's cast already holds ``CAST_MAX`` characters."""
    count = int(
        await conn.scalar(
            select(func.count()).select_from(characters).where(characters.c.story_id == story_id)
        )
        or 0
    )
    if count >= CAST_MAX:
        raise InvalidRequest(
            f"the story's cast holds at most {CAST_MAX} characters",
            {"field": "body", "cap": "cast_max", "limit": CAST_MAX, "actual": count + 1},
        )


async def _name_taken(conn: AsyncConnection, story_id: str, name: str) -> None:
    """422 ``validation_error`` at ``body.name`` when the story has a character of that name,
    ignoring case (the ``uq_characters_story_name`` index's rule)."""
    found = await conn.scalar(
        select(characters.c.id)
        .where(
            characters.c.story_id == story_id,
            func.lower(characters.c.name) == func.lower(name),
        )
        .limit(1)
    )
    if found is not None:
        raise InvalidRequest(
            "the story already has a character of this name",
            {"field": "body.name", "reason": "duplicate"},
        )


async def _images_usable(conn: AsyncConnection, images: Sequence[str]) -> None:
    """Every image must be a live asset (422 ``media_retired`` at ``body.images[i]``, held
    ``FOR SHARE`` until the caller's transaction ends) and an image, not a video (422
    ``validation_error``, ``reason: not_an_image``)."""
    if not images:
        return
    await assert_referenceable(conn, images, field="body.images")
    kinds = {
        str(row.id): str(row.kind)
        for row in await conn.execute(
            select(media_assets.c.id, media_assets.c.kind).where(media_assets.c.id.in_(images))
        )
    }
    for index, media_id in enumerate(images):
        if kinds.get(media_id) != MediaKind.IMAGE.value:
            raise InvalidRequest(
                f"media asset {media_id} is not an image",
                {"field": f"body.images[{index}]", "reason": "not_an_image"},
            )


async def _addable(conn: AsyncConnection, story_id: str, request: AdHocCharacter) -> None:
    await _cast_full(conn, story_id)
    await _name_taken(conn, story_id, request.name)
    await _images_usable(conn, request.images)


async def add_story_character(
    db: AsyncEngine,
    story_id: str,
    request: AdHocCharacter,
    *,
    principal: str,
    gate: CharacterGate,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> StoryCharacterAdded:
    """Add ``request`` to the story's cast (FR-CAT-7; see the module docstring for the rules and
    their order)."""
    async with db.connect() as conn, conn.begin():
        exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
        if exists is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        await _addable(conn, story_id, request)
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    async with db.connect() as conn:
        await _refuse_busy(conn, story_id, turns_only=True)
    texts = [("definition", request.definition)]
    if request.deflection_style is not None:
        texts.append(("deflection_style", request.deflection_style))
    await gate(texts)
    character_id = new_ulid()
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )
    ordinal: list[int] = []

    async def insert_character(conn: AsyncConnection) -> None:
        # Under the story lock: the checks again (another addition, a turn or a retirement may
        # have come in since), then the row at the next ordinal.
        await _refuse_busy(conn, story_id, turns_only=True, own=op.operation_id)
        await _addable(conn, story_id, request)
        last = await conn.scalar(
            select(func.max(characters.c.ordinal)).where(characters.c.story_id == story_id)
        )
        ordinal.append(int(last if last is not None else -1) + 1)
        await conn.execute(
            insert(characters).values(
                id=character_id,
                story_id=story_id,
                ordinal=ordinal[0],
                name=request.name,
                definition=request.definition,
                true_age=request.true_age,
                age_recorded_at=func.transaction_timestamp(),
                is_player=False,
                safe_fallback_text=request.safe_fallback_text,
                deflection_style=request.deflection_style,
                images=list(request.images),
            )
        )
        # The character's entity (FR-PROP-6; #139), then the new name may already be in the
        # story's records: they are scanned again (FR-ADMIN-6; #123), the new rows with them.
        await add_entities(
            conn,
            story_id,
            [character_spec(character_id, request.name)],
            operation_id=op.operation_id,
        )
        await reindex_story(conn, story_id)

    async with op:
        op.append(
            "character_defined",
            CharacterDefined(character_id=character_id, name=request.name, is_player=False),
        )
        op.add_commit_step(insert_character)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return StoryCharacterAdded(
        id=character_id,
        name=request.name,
        ordinal=ordinal[0],
        timeline_revision=op.timeline_revision,
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
        await pause.refuse(conn, story_id, status)
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


async def _refuse_running_job(conn: AsyncConnection, story_id: str) -> None:
    """423 ``story_busy`` (``reason: job_running``) while a job of the story holds a live lease
    (#70); a job whose lease lapsed is abandoned work and is deleted with the story."""
    running = await conn.scalar(
        select(jobs.c.id)
        .where(
            jobs.c.story_id == story_id,
            jobs.c.status == "running",
            jobs.c.lease_until > clock.now(),
        )
        .limit(1)
    )
    if running is not None:
        raise StoryBusy(
            "a background job of the story is running; retry when it finishes",
            {"reason": "job_running", "job_id": running, "story_id": story_id},
        )


# -- delete ---------------------------------------------------------------------------------


async def delete_story(db: AsyncEngine, story_id: str, *, principal: str) -> dict[str, int]:
    """Remove every row of ``story_id`` and nothing else (FR-STORE-16); return rows per table.

    One transaction under the story row lock: 404 when the story does not exist, 423
    ``story_busy`` while any of its operations is in flight or one of its jobs runs (#70).
    Archived stories can be deleted, and so can a story the erasure reconciler blocked (#125).
    The deletion is recorded by a story-less ``control`` operation whose ``story_deleted``
    audit event names the story and the acting principal, and by a ``story_deleted``
    ``audit_log`` row (null ``story_id``; the story's earlier audit rows are detached). The
    story's relations are removed and a ``deleted`` ownership event names its owner (#104).
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
        await _refuse_running_job(conn, story_id)
        # The story's relations go too, and the deletion is recorded with its owner (#104).
        await ownership.drop_entity(conn, EntityType.STORY, story_id, principal=principal)
        removed.update(await _delete_story_rows(conn, story_id))
        # A story blocked by the erasure reconciler may be deleted: its pending erasures are
        # settled ``story_deleted`` (#125), since nothing of it is left to erase.
        await erasure_ledger.settle_deleted(conn, story_id)
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
    # The story's idempotency keys go too (#61): its own (scope ``story``) and any
    # deployment-scoped key bound to one of its operations (the creation's), so no stored
    # result outlives the story.
    result = await conn.execute(
        delete(idempotency_keys)
        .where(
            or_(
                and_(
                    idempotency_keys.c.scope == Scope.STORY.value,
                    idempotency_keys.c.scope_id == story_id,
                ),
                idempotency_keys.c.operation_id.in_(
                    select(operations.c.operation_id).where(operations.c.story_id == story_id)
                ),
            )
        )
        .returning(idempotency_keys.c.scope, idempotency_keys.c.scope_id, idempotency_keys.c.key)
    )
    keys = result.all()
    removed[idempotency_keys.name] = len(keys)
    # A deployment-scoped result's rows are catalog-scoped: they do not go with the story's.
    await drop_many(
        conn, idempotency_keys.name, [idempotency_id(k.scope, k.scope_id, k.key) for k in keys]
    )
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
        stories.c.owner_principal,
        stories.c.status,
        stories.c.timeline_revision,
        stories.c.next_turn_index,
        stories.c.settings,
        stories.c.turn_concurrency,
        stories.c.turn_queue_max,
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
        settings=settings_of(row),
        created_at=row.created_at,
        archived_at=row.archived_at,
        owner_principal=row.owner_principal,
        characters=cast,
    )


async def get_story(db: AsyncEngine, story_id: str, *, omniscient: bool = False) -> StoryView:
    """The story with its cast in order and its engine profile at the version it runs on. True
    ages only when ``omniscient`` (FR-CAT-12)."""
    runs_on = func.coalesce(
        stories.c.engine_profile_version_pin, engine_profiles.c.current_version
    ).label("profile_version")
    async with db.connect() as conn, conn.begin():
        row = (
            await conn.execute(
                select(
                    *_story_columns(),
                    stories.c.engine_profile_id,
                    stories.c.scenario_id,
                    stories.c.scenario_version,
                    runs_on,
                )
                .join(content_ratings, content_ratings.c.id == stories.c.content_rating_id)
                .join(engine_profiles, engine_profiles.c.id == stories.c.engine_profile_id)
                .where(stories.c.id == story_id)
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        paused = None
        if pause.is_paused(row.status):
            found = await pause.details(conn, story_id)
            paused = {k: found[k] for k in ("job_id", "cause", "progress")}
        cast = (
            await conn.execute(
                select(
                    characters.c.id,
                    characters.c.name,
                    characters.c.is_player,
                    characters.c.true_age,
                    characters.c.promoted_to,
                    characters.c.role_id,
                    characters.c.source_catalog_id,
                    characters.c.source_catalog_version,
                )
                .where(characters.c.story_id == story_id)
                .order_by(characters.c.ordinal)
            )
        ).all()
    view = _view(
        row,
        tuple(
            CharacterView(
                id=c.id,
                name=c.name,
                is_player=bool(c.is_player),
                true_age=int(c.true_age) if omniscient else None,
                promoted_to=tuple(str(i) for i in c.promoted_to or ()) if omniscient else None,
                role_id=c.role_id,
                source=None
                if c.source_catalog_id is None or c.source_catalog_version is None
                else CatalogVersionRef(
                    catalog_id=str(c.source_catalog_id), version=int(c.source_catalog_version)
                ),
            )
            for c in cast
        ),
    )
    profile = ProfileVersionRef(id=str(row.engine_profile_id), version=int(row.profile_version))
    scenario = (
        None
        if row.scenario_id is None
        else ScenarioOrigin(id=str(row.scenario_id), version=int(row.scenario_version))
    )
    return dataclasses.replace(view, engine_profile=profile, pause=paused, scenario=scenario)


async def list_stories(
    db: AsyncEngine,
    *,
    status: StoryStatus | None = None,
    owner: str | None = None,
    relations: Sequence[RelationFilter] = (),
) -> list[StoryView]:
    """The deployment's stories, newest first (no cast): every story, unless the request filters
    by ``status``, by ``owner``, or by ``relations`` (a relation name, with or without its
    principal; several must all hold). Nothing else narrows the list (FR-AUTH-8)."""
    query = select(*_story_columns()).join(
        content_ratings, content_ratings.c.id == stories.c.content_rating_id
    )
    if status is not None:
        query = query.where(stories.c.status == status.value)
    filters = ownership.list_filters(
        EntityType.STORY, stories.c.id, stories.c.owner_principal, owner=owner, relations=relations
    )
    if filters:
        query = query.where(*filters)
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
