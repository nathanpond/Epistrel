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
the story (404) → the cast limit (``max_story_cast``, FR-MULTI-12: the story's full
characters, weight ``major``), a name the story already has (ignoring case) and the
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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
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

from epistrel.core import audit, clock, ownership, pause, providers
from epistrel.core import bench as benches
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.enums import (
    CharacterWeight,
    OperationKind,
    OperationStatus,
    Provenance,
    StoryStatus,
)
from epistrel.core.epistemic import modeling
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.index import drop_many, reindex_story
from epistrel.core.erasure.records import character_record, idempotency_id, index_rows, story_record
from epistrel.core.errors import (
    AlreadyBenched,
    CharacterBenched,
    InvalidRequest,
    NotBenched,
    StoryArchived,
    StoryBusy,
    StoryCharacterNotFound,
    StoryEntityNotFound,
    StoryNotFound,
    ViewpointUnresolved,
)
from epistrel.core.events import Operation
from epistrel.core.events.registry import EventPayload
from epistrel.core.events.types import (
    CastMember,
    CharacterBenchedEvent,
    CharacterBenchModeChanged,
    CharacterDefined,
    CharacterReactivated,
    CharacterWeightSet,
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
from epistrel.core.membership import Position
from epistrel.core.memory.story_partitions import claim, schedule_drop, story_partition
from epistrel.core.ownership import Owner, RelationFilter
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles.effective import ProfileVersionRef, StoryProfileRef
from epistrel.core.profiles.impact import ImpactReport
from epistrel.core.providers import ProviderBindings
from epistrel.core.ratings import require_assignable
from epistrel.core.resolution.entities import add_entities, character_spec
from epistrel.core.settings_schema import (
    CHARACTER_VIEWPOINT_PREFIX,
    COLUMN_SETTINGS,
    STORY_CAST_LIMIT_MAX,
    SUPERSEDED_STORY_SETTINGS,
    StorySettings,
    superseded,
)
from epistrel.core.states import define_state, validate_state_tie_break
from epistrel.core.states.model import world_clock_definition
from epistrel.core.tables import (
    amendment_parts,
    amendments,
    audit_log,
    backfill_chunks,
    backfill_conflicts,
    bundle_jobs,
    capability_coverage,
    certification_runs,
    character_snapshots,
    characters,
    client_events,
    config_changes,
    content_ratings,
    dependency_edges,
    deployment_state,
    directives,
    engine_profiles,
    events,
    genesis_deferred,
    guardrail_check_results,
    idempotency_keys,
    image_costs,
    image_jobs,
    image_reservations,
    jobs,
    lite_summary_segments,
    media_assets,
    operations,
    placement,
    policies,
    rating_check_jobs,
    relationships,
    rewrite_questions,
    rewrite_sessions,
    scene_state,
    stories,
    story_backfills,
    story_heads,
    story_profile_overrides,
    story_texts,
    subject_references,
    timers,
    transcript_messages,
    turn_queue,
    worker_heartbeats,
)
from epistrel.core.tables.beliefs import (
    belief_state,
    belief_transitions,
    beliefs,
    nested_belief_state,
)
from epistrel.core.tables.bench import character_bench
from epistrel.core.tables.characters import DEFLECTION_STYLE_MAX, TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.tables.commitments import promissory_ledger, thunks
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.detection import extraction_shadow
from epistrel.core.tables.divergences import divergences
from epistrel.core.tables.escalations import escalations
from epistrel.core.tables.exposures import exposures
from epistrel.core.tables.facts import facts
from epistrel.core.tables.feedback import feedback_history, message_feedback
from epistrel.core.tables.hot_set import overflow_episodes
from epistrel.core.tables.media import MediaKind
from epistrel.core.tables.memories import memories
from epistrel.core.tables.outbox import (
    external_effect_outbox,
    outbox_outcomes,
    reconciliation_marks,
)
from epistrel.core.tables.ownership import EntityType
from epistrel.core.tables.propositions import (
    closed_sets,
    entities,
    entity_aliases,
    predicate_proposals,
    predicates,
    propositions,
)
from epistrel.core.tables.providers import story_provider_bindings
from epistrel.core.tables.readset import proposition_changes
from epistrel.core.tables.scheduled_events import scheduled_events
from epistrel.core.tables.states import (
    external_values,
    state_changes,
    state_definitions,
    state_proposals,
    state_values,
)
from epistrel.core.tables.story_images import story_images
from epistrel.core.tables.utterances import utterances
from epistrel.core.trimmed import trim_as_sent
from epistrel.testing import pauses

Database = AsyncEngine | AsyncConnection

CAST_MIN = 1
CHARACTER_NAME_MAX = 80
DEFINITION_MAX = 20_000
STORY_NAME_MAX = 200
SAFE_FALLBACK_MAX = 500

STORY_TABLES: tuple[Table, ...] = (
    # Dependency edges (#220): ids only, no foreign key; first here, so a fork maps and copies
    # them after every record they name (``fork.ROW_FILTERS``).
    dependency_edges,
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
    # Provider bindings (#204): a story's ids at its providers; a fork reads the same values.
    story_provider_bindings,
    policies,
    genesis_deferred,
    character_snapshots,
    characters,
    turn_queue,
    jobs,
    # Image jobs (#256): transient work, deleted with the story; not copied by a fork.
    image_jobs,
    rating_check_jobs,
    guardrail_check_results,
    timers,
    # Commitments (#146): derived tables, before the operations they reference (the ledger
    # names thunks, and a thunk the fact that resolved it; none of them has a foreign key).
    promissory_ledger,
    thunks,
    # Bitemporal canon (#133): a derived table, before the operations it references.
    facts,
    # Divergences (#166): a derived table, before the beliefs it names (a deferred foreign key)
    # and the operations it references.
    divergences,
    # Belief partitions (#159): a derived table, before the operations it references, and its
    # projection (rebuilt by a fork).
    beliefs,
    belief_transitions,
    belief_state,
    nested_belief_state,
    # Exposures (#164): a derived table, before the operations it references.
    exposures,
    # States (#185): the projection (rebuilt by a fork), then the changes and the declarations,
    # derived tables before the operations they reference (no foreign key between them).
    state_values,
    state_changes,
    state_definitions,
    # External values (#205): the projection of the ``external_read`` events (rebuilt by a
    # fork), no foreign key.
    external_values,
    # Engine-proposed States (#192): the review queue (no foreign key, no membership).
    state_proposals,
    # Scheduled events (#196): a derived table (each lifecycle step a version), before the
    # operations it references; copied by a fork through the fork point like ``facts``.
    scheduled_events,
    # Character bench (#831): a derived table (each bench and reactivation a version), before
    # the operations it references; copied by a fork through the fork point.
    character_bench,
    # Story images (#261): a derived table, before the operations it references (the placing
    # and the deleting one); copied by a fork by reference to the same assets.
    story_images,
    # The outbox (#210): the outcome records (append-only, no foreign key), then the
    # outbox (a derived table), before the operations it references; #211's reconciliation
    # marks (ids and reason codes, no foreign key) first.
    reconciliation_marks,
    outbox_outcomes,
    external_effect_outbox,
    # Memory records (#135): a derived table, before the operations it references.
    memories,
    # Speech acts (#150): a derived table, before the operations it references.
    utterances,
    # The hot set's overflow episodes (#147): maintenance state, no foreign key.
    overflow_episodes,
    # Claim detection's shadow samples (#148): a derived table, before the operations it names.
    extraction_shadow,
    # Escalation records (#155): a derived table, before the operations it names.
    escalations,
    # The structured read set's change index (#136), before the operations it references.
    proposition_changes,
    # The structured stores (#131): story rows only (core predicates have no story).
    predicate_proposals,
    closed_sets,
    propositions,
    predicates,
    entity_aliases,
    entities,
    # Amendments (#221): the parts before the amendments they belong to; neither is derived
    # (FR-STORE-21), and a fork copies the amendments of its copied operations.
    amendment_parts,
    amendments,
    # Rewrite sessions (#223): the questions before their sessions, after the amendments that
    # name a session; neither is copied by a fork.
    rewrite_questions,
    rewrite_sessions,
    # Directives (#226): an author's directions; not copied by a fork.
    directives,
    # Story exports (#249): the jobs and their archives; not copied by a fork.
    bundle_jobs,
    # Backfill (#228): the chunks and conflicts before the backfill they belong to, and the
    # coverage (no foreign key); a fork copies only the coverage, clipped to its fork point.
    backfill_chunks,
    backfill_conflicts,
    story_backfills,
    capability_coverage,
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
        image_jobs.name,
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
        extraction_shadow.name,
        escalations.name,
        state_proposals.name,
        reconciliation_marks.name,
        backfill_chunks.name,
        backfill_conflicts.name,
        story_backfills.name,
        rewrite_questions.name,
        rewrite_sessions.name,
        directives.name,
        bundle_jobs.name,
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
opens the episodes it needs), and claim detection's shadow samples (#148: an eval of the
source's renders, which ``GET /admin/evals/extraction-shadow`` counts once), and the escalation
records (#155: the source's turns' entailment calls, which ``GET /admin/evals/escalation-yield``
counts once), and the engine's State proposals (#192: the source's review queue, whose evidence
may point past the fork point; the fork's own analysis proposes again), and the reconciliation
marks (#211: the source's listings; the fork's outbox copies are records, never listed), and the
image jobs (#256: work in progress, like ``jobs``). Every
other table
in ``STORY_TABLES`` is copied by ``core.fork``, so a new story-scoped table is copied unless it
is named here."""

STORY_ID_EXCEPTIONS: frozenset[str] = frozenset(
    {audit_log.name, certification_runs.name, image_reservations.name, image_costs.name}
)
"""Deployment-level tables that carry a ``story_id`` but outlive the story: deletion nulls the
column instead of removing the row. ``audit_log`` (#56), ``certification_runs`` (#233: a
story's certification run is a deployment record) and ``image_reservations`` (#262, owner
decision 13: image accounting; the records the story's jobs still hold are released first, and
the limit counters and the records' ``counter_keys`` are untouched) with ``image_costs`` (#269:
the attempts' costs, so the deployment's usage report keeps them)."""

STORY_DELETE_SETTING = "epistrel.allow_story_delete"
"""The transaction setting the ``events`` trigger reads to let one story's events be deleted."""

CONFIG_SCOPE_STORY = "story"


# -- request models -------------------------------------------------------------------------

CharacterName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=CHARACTER_NAME_MAX),
    BeforeValidator(trim_as_sent(CHARACTER_NAME_MAX)),
]


class ParticipationSpec(BaseModel):
    """A story character's participation role (FR-MULTI-7; #176): ``primary``, ``reactive`` or
    ``silent``, and whether it is ``fixed`` (a bound the engine never crosses on its own:
    escalation never raises a fixed ``reactive`` or ``silent`` character, budget planning never
    lowers a fixed ``primary`` one to reactive narration). ``PATCH
    /stories/{id}/characters/{cid}/participation``'s body, and a cast member's ``participation``
    at creation. Default: ``reactive``, not fixed."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"role": "primary", "fixed": True}]},
    )

    role: Literal["primary", "reactive", "silent"]
    fixed: StrictBool = False


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
    participation: ParticipationSpec | None = Field(
        default=None,
        description="Its participation role (FR-MULTI-7; #176): default `reactive`, not fixed. "
        "The player's character has none (422).",
    )

    @model_validator(mode="after")
    def _player_has_no_role(self) -> CharacterSpec:
        if self.is_player and self.participation is not None:
            raise ValueError("the player's character has no participation role")
        return self


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

    participation: ParticipationSpec | None = Field(
        default=None,
        description="Its participation role (FR-MULTI-7; #176): default `reactive`, not fixed.",
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
    characters: list[CharacterSpec] = Field(
        min_length=CAST_MIN,
        description="The cast, the player's character included: at most the deployment's "
        "`max_story_cast` (FR-MULTI-12; default 50, at most 200), or 422 `validation_error` "
        '`{cap: "cast_max", limit, actual}`.',
        json_schema_extra={"maxItems": STORY_CAST_LIMIT_MAX},
    )
    content_rating_id: str | None = None
    settings: CreationSettings = Field(default_factory=CreationSettings)
    owner: Owner | None = Field(
        default=None,
        description="The story's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )
    provider_bindings: ProviderBindings = Field(
        default_factory=dict,
        description="The story's ids at its external-data providers (#204): "
        "`{provider: {external_ids}}`, each provider registered and each binding giving every "
        "`{binding.<key>}` its manifest uses (422 `binding_incomplete`).",
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
    from a scenario's (#119), ``None`` otherwise. ``weight`` and ``modeled_at`` (#163) are an
    omniscient read's too."""

    id: str
    name: str
    is_player: bool
    true_age: int | None = None
    promoted_to: tuple[str, ...] | None = None
    role_id: str | None = None
    source: CatalogVersionRef | None = None
    weight: str | None = None
    """``minor`` or ``major`` (FR-EPI-10; #163); ``None`` unless the read is omniscient."""
    modeled_at: Position | None = None
    """The position the character was modeled at (0 for the genesis cast; #163); ``None``
    unless the read is omniscient."""
    participation: dict[str, Any] | None = None
    """``{role, fixed}`` (FR-MULTI-7; #176); ``None`` for the player's character."""
    bench: dict[str, Any] | None = None
    """``{mode, since_turn}`` while the character is benched (FR-MULTI-13; #831), else
    ``None``."""


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
    engine_profile: StoryProfileRef | None = None
    """The engine profile the story runs under and the version it runs on (#113): its pin, else
    the profile's latest, and whether it is pinned. Read by ``get_story`` and ``list_stories``
    (#527)."""
    pause: dict[str, Any] | None = None
    """While the story is paused (#118): ``{job_id, cause, progress {done, total}}``, the check
    (or backfill, whose ``progress`` carries its own members too, and ``backfill_id`` and
    ``conflicts_open`` while it waits for its conflicts; #783) that paused it. Read by
    ``get_story`` only."""
    scenario: ScenarioOrigin | None = None
    """The scenario version the story was created from (#119); ``None`` for a blank story.
    Read by ``get_story`` only."""
    amendment_checks_pending: dict[str, Any] | None = None
    """#239: ``{count, session_id}`` of the open support-check questions about the story's
    amendments (``None`` when none); filled by ``Engine.get_story``."""
    cast: dict[str, int] | None = None
    """#831 (FR-MULTI-13): ``{active, benched}``: the full characters ``max_story_cast`` counts
    (weight ``major``, not benched) and the benched characters. Read by ``get_story`` only."""
    pruning: dict[str, Any] | None = None
    """#703 (FR-STORE-32): ``{policy, last_prune_at, next_run_at}``: the story's
    ``prune_policy`` (``None``: off), its latest pruning (manual or by the policy) and, while
    the policy is on, when the daily ``prune_run`` runs next. Read by ``get_story`` only."""


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
    operation (``story_created``) exist together or not at all. A cast larger than the
    deployment's ``max_story_cast`` is 422 ``cast_max`` (FR-MULTI-12). The story's memories
    partition is made first, in a short transaction of its own (#784, ADR-187)."""
    _check_unique_names(spec.characters)
    story_id = story_id or new_ulid()
    async with story_partition(db, story_id):
        return await _create_story_from(db, spec, principal=principal, story_id=story_id)


async def _create_story_from(
    db: Database, spec: StorySpec, *, principal: str, story_id: str
) -> CreatedStory:
    cast = [(new_ulid(), character) for character in spec.characters]
    settings = _stored_settings(spec, cast)
    document, columns = split_settings(settings)
    async with _transaction(db) as conn:
        await claim(conn, story_id)  # #875: the story's key, held to this commit
        # FR-MULTI-12 (#828): the cast against the deployment's limit in force now, after the
        # duplicate names and before the rating and the bindings.
        limit = await story_cast_limit(conn)
        if len(spec.characters) > limit:
            raise cast_max(limit, len(spec.characters))
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
        # The story's ids at its providers (#204), checked against each manifest.
        await providers.bind_all(conn, story_id, spec.provider_bindings)
        # #228: which capabilities' data the story has from its first turn.
        from epistrel.core.backfill import coverage

        await coverage.seed_story(conn, story_id)

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
                        **_participation_values(character.participation, default=True),
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
        # Every story's world clock (FR-CLOCK-1; #193), inert while its profile has it off.
        await define_state(conn, op, story_id, world_clock_definition(), builtin=True)
        # #188 (FR-MULTI-8): a state:<name> tie-break names a number State the genesis made
        # (#202 creates a scenario's States before this check).
        await validate_state_tie_break(
            conn, story_id, stored_tie_break(settings), field="body.settings.simultaneous_tie_break"
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


def stored_tie_break(settings: Mapping[str, Any]) -> Any:
    """The stored ``simultaneous_tie_break``: the story's own, else the one genesis kept
    ``inactive`` (#181)."""
    found = settings.get("simultaneous_tie_break")
    if found is not None:
        return found
    inactive = settings.get("inactive")
    entry = inactive.get("simultaneous_tie_break") if isinstance(inactive, Mapping) else None
    return entry.get("value") if isinstance(entry, Mapping) else None


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


def cast_max(limit: int, actual: int, *, field: str = "body") -> InvalidRequest:
    """FR-MULTI-12's refusal: 422 ``validation_error`` ``{cap: "cast_max", limit, actual}``,
    ``actual`` being the full characters the request would leave the story with."""
    return InvalidRequest(
        f"the story's cast holds at most {limit} full characters",
        {"field": field, "cap": "cast_max", "limit": limit, "actual": actual},
    )


async def story_cast_limit(conn: AsyncConnection) -> int:
    """The deployment's ``max_story_cast`` (FR-MULTI-12), read in ``conn``'s transaction."""
    from epistrel.core.config_changes import read_story_cast_limit

    return await read_story_cast_limit(conn)


async def _major_count(conn: AsyncConnection, story_id: str) -> int:
    """The story's characters of weight ``major`` (FR-EPI-10), benched or not."""
    return int(
        await conn.scalar(
            select(func.count())
            .select_from(characters)
            .where(
                characters.c.story_id == story_id,
                characters.c.weight == CharacterWeight.MAJOR.value,
            )
        )
        or 0
    )


async def _active_cast_count(conn: AsyncConnection, story_id: str) -> int:
    """The story's full characters, the ones FR-MULTI-12's limit counts: weight ``major``
    (FR-EPI-10), the player's character included. A ``minor`` character (introduced by a turn
    or modeled lazily) never counts, and neither does a benched one (FR-MULTI-13; #831)."""
    query = (
        select(func.count())
        .select_from(characters)
        .where(
            characters.c.story_id == story_id,
            characters.c.weight == CharacterWeight.MAJOR.value,
        )
    )
    benched = sorted(await benches.bench_states(conn, story_id))
    if benched:
        query = query.where(characters.c.id.not_in(benched))
    return int(await conn.scalar(query) or 0)


async def _cast_full(
    conn: AsyncConnection,
    story_id: str,
    *,
    already_counted: bool = False,
    reactivation: bool = False,
) -> None:
    """422 ``validation_error`` (``cast_max``) when the story already holds as many full
    characters as the deployment's ``max_story_cast`` allows (FR-MULTI-12): one more would
    pass it. A story above a lowered limit is refused the same way. Benched characters do not
    count (FR-MULTI-13), but benched and unbenched full characters together never pass
    ``STORY_CAST_LIMIT_MAX`` (``limit`` 200). ``already_counted``: the request's character is
    in the active count already (a reactivation of an unbenched ``major`` character, #845), so
    ``actual`` is the count itself, not one more. ``reactivation``: the character is already
    in the benched-plus-unbenched total, which a reactivation never changes, so the 200
    ceiling is not checked (#886)."""
    more = 0 if already_counted else 1
    limit = await story_cast_limit(conn)
    count = await _active_cast_count(conn, story_id)
    if count >= limit:
        raise cast_max(limit, count + more)
    if reactivation:
        return
    total = await _major_count(conn, story_id)
    if total >= STORY_CAST_LIMIT_MAX:
        raise cast_max(STORY_CAST_LIMIT_MAX, total + more)


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
                **_participation_values(request.participation),
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


# -- modeling (#163) -------------------------------------------------------------------------


class CharacterWeightRequest(BaseModel):
    """``POST /stories/{id}/characters/{cid}/weight``: the narrative weight to give the
    character (FR-EPI-10)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [{"weight": "major"}]}
    )

    weight: Literal["minor", "major"]


class ModelEntityRequest(BaseModel):
    """``POST /stories/{id}/entities/{eid}/model``: the author's directive to model an entity as
    a character (FR-EPI-9's explicit salience). Its one true age is required (FR-CAT-12);
    ``definition`` defaults to the entity's name."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [{"true_age": 45, "definition": "The neighbor across the street."}]
        },
    )

    true_age: Annotated[StrictInt, Field(ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)]
    definition: Annotated[str, Field(min_length=1, max_length=4_000)] | None = None


@dataclass(frozen=True, slots=True)
class CharacterWeighted:
    """A character as a weight change or a modeling directive left it (the omniscient item:
    its weight and the position it was modeled at) and the story's revision after."""

    character: CharacterView
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


async def _character_view(conn: AsyncConnection, story_id: str, character_id: str) -> CharacterView:
    row = (
        await conn.execute(
            select(characters).where(
                characters.c.story_id == story_id, characters.c.id == character_id
            )
        )
    ).one_or_none()
    if row is None:
        raise StoryCharacterNotFound(
            f"story {story_id} has no character {character_id}",
            {"story_id": story_id, "character_id": character_id},
        )
    return CharacterView(
        id=str(row.id),
        name=str(row.name),
        is_player=bool(row.is_player),
        true_age=int(row.true_age),
        promoted_to=tuple(str(i) for i in row.promoted_to or ()),
        role_id=row.role_id,
        source=None
        if row.source_catalog_id is None or row.source_catalog_version is None
        else CatalogVersionRef(
            catalog_id=str(row.source_catalog_id), version=int(row.source_catalog_version)
        ),
        weight=str(row.weight),
        modeled_at=_modeled_at(row),
        participation=_participation_of(row),
        bench=await _bench_document(conn, story_id, character_id),
    )


async def _bench_document(
    conn: AsyncConnection, story_id: str, character_id: str
) -> dict[str, Any] | None:
    found = await benches.bench_of(conn, story_id, character_id)
    return None if found is None else found.document()


async def _story_exists(conn: AsyncConnection, story_id: str) -> None:
    exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
    if exists is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})


async def set_character_weight(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    request: CharacterWeightRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> CharacterWeighted:
    """Set a character's narrative weight (FR-EPI-10; #163): a control operation recording
    ``character_weight_set``, prospective (what the character already holds stays as it is;
    nothing promotes automatically). 404 for an unknown story or character; 422 ``is_player``
    for the player's character (always ``major``, FR-EPI-12); 409 ``story_archived``; 409
    ``revision_conflict`` for a stale expected revision; 423 ``story_busy`` while a turn is in
    flight; 422 ``cast_max`` for a ``minor`` made ``major`` when the story already holds the
    deployment's ``max_story_cast`` full characters (FR-MULTI-12). Setting the weight it has
    records nothing new but still answers it."""
    async with db.connect() as conn, conn.begin():
        await _story_exists(conn, story_id)
        view = await _character_view(conn, story_id, character_id)
    if view.is_player:
        raise InvalidRequest(
            "the player's character is always major (FR-EPI-12)",
            {"field": "character_id", "reason": "is_player"},
        )
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    async with db.connect() as conn:
        await _refuse_busy(conn, story_id, turns_only=True)
        await refuse_benched(conn, story_id, character_id)
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )

    async def set_weight(conn: AsyncConnection) -> None:
        await _refuse_busy(conn, story_id, turns_only=True, own=op.operation_id)
        await refuse_benched(conn, story_id, character_id)
        if request.weight == CharacterWeight.MAJOR.value:
            # FR-MULTI-12 (#828): making a minor character full takes a place in the cast,
            # under the story lock; a major one staying major is never refused.
            current = await conn.scalar(
                select(characters.c.weight).where(
                    characters.c.story_id == story_id, characters.c.id == character_id
                )
            )
            if current != CharacterWeight.MAJOR.value:
                await _cast_full(conn, story_id)
        await conn.execute(
            update(characters)
            .where(characters.c.story_id == story_id, characters.c.id == character_id)
            .values(weight=request.weight)
        )

    async with op:
        op.append(
            "character_weight_set",
            CharacterWeightSet(
                character_id=character_id, weight=request.weight, previous=view.weight or "major"
            ),
        )
        op.add_commit_step(set_weight)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    async with db.connect() as conn:
        after = await _character_view(conn, story_id, character_id)
    return CharacterWeighted(character=after, timeline_revision=op.timeline_revision)


async def model_entity(
    db: AsyncEngine,
    story_id: str,
    entity_id: str,
    request: ModelEntityRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> CharacterWeighted:
    """The author's directive to model one of the story's entities as a character (FR-EPI-9;
    #163's ``core.epistemic.modeling.maybe_model`` with trigger ``salience``): a control
    operation that creates the ``characters`` row (``weight = minor``, the entity's name, the
    request's true age, ``true_age_source = declared``, modeled at the story's head), adopts the
    entity and writes its ``age`` fact. 404 for an unknown story or entity; 422
    ``already_modeled`` for an entity that is a character, ``not_a_person`` for a place, item,
    organization or event; 422 ``allow_new_arrivals`` when the story refuses new arrivals; 409
    ``story_archived``; 409 ``revision_conflict``; 423 ``story_busy`` while a turn is in flight.
    A ``minor`` character never counts against the story cast limit (``max_story_cast``,
    FR-MULTI-12), so this is never refused by it."""
    # Imported here: ``core.turns.beats`` reaches this module through the turn package.
    from epistrel.core.turns.beats import write_character_facts

    async with db.connect() as conn, conn.begin():
        await _story_exists(conn, story_id)
        entity = (
            await conn.execute(
                select(entities).where(entities.c.story_id == story_id, entities.c.id == entity_id)
            )
        ).one_or_none()
        if entity is None:
            raise StoryEntityNotFound(
                f"story {story_id} has no entity {entity_id}",
                {"story_id": story_id, "entity_id": entity_id},
            )
        stored = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
        arrivals = bool(dict(stored or {}).get("allow_new_arrivals", True))
    if entity.character_id is not None:
        raise InvalidRequest(
            "the entity is a character already",
            {
                "field": "entity_id",
                "reason": "already_modeled",
                "character_id": entity.character_id,
            },
        )
    if entity.kind not in ("person", "other"):
        raise InvalidRequest(
            "only a person is modeled as a character",
            {"field": "entity_id", "reason": "not_a_person", "kind": entity.kind},
        )
    if not arrivals:
        raise InvalidRequest(
            "the story allows no new arrivals",
            {"field": "entity_id", "reason": "allow_new_arrivals"},
        )
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    async with db.connect() as conn:
        await _refuse_busy(conn, story_id, turns_only=True)
        head = int(
            await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
            or 1
        )
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )
    made: list[Any] = []
    definition = request.definition or str(entity.canonical_name)

    async def model(conn: AsyncConnection) -> None:
        await _refuse_busy(conn, story_id, turns_only=True, own=op.operation_id)
        candidate = modeling.Candidate(
            modeling.SALIENCE, "author", 0, definition, str(entity.canonical_name), entity_id
        )
        found = await modeling.maybe_model(
            conn,
            op,
            modeling.ModelRequest(candidate, request.true_age, age_source="declared"),
            position=Position(max(0, head - 1)),
            base_revision=op.base_revision,
        )
        if found is None:
            raise InvalidRequest(
                "the entity is a character already",
                {"field": "entity_id", "reason": "already_modeled"},
            )
        made.append(found)
        await write_character_facts(conn, op, [(found.character_id, request.true_age, 0)])
        await reindex_story(conn, story_id)

    async with op:
        op.add_commit_step(model)
        await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    async with db.connect() as conn:
        after = await _character_view(conn, story_id, made[0].character_id)
    return CharacterWeighted(character=after, timeline_revision=op.timeline_revision)


# -- participation (#176) -------------------------------------------------------------------

PARTICIPATION_SETTING = "participation"
"""A participation change's ``config_changes`` setting: ``participation:<character id>``."""


@dataclass(frozen=True, slots=True)
class ParticipationSet:
    """A character as a participation change left it, the story's configuration revision after
    it, and whether anything changed."""

    character: CharacterView
    config_revision: int
    timeline_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


async def set_participation(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    request: ParticipationSpec,
    *,
    principal: str,
) -> ParticipationSet:
    """Set a story character's participation role (FR-MULTI-7; #176): a story configuration
    write, prospective (a turn already running keeps the roles it resolved). Under the story
    row lock: 404 for an unknown story or character; 422 ``is_player`` for the player's
    character (it has no role); 409 ``story_archived``. A change moves the story's
    ``config_revision`` by one and records one ``config_changes`` row (setting
    ``participation:<character id>``, class ``prospective``, ``{role, fixed}`` before and after)
    with its ``config_changed`` event; setting the role it has writes nothing."""
    from epistrel.core.config_changes import ConfigScope, record_config_change
    from epistrel.core.settings_schema import CharacterSettings, ConfigClass, tag_of

    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(stories.c.config_revision, stories.c.timeline_revision, stories.c.status)
                .where(stories.c.id == story_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        view = await _character_view(conn, story_id, character_id)
        if view.is_player:
            raise InvalidRequest(
                "the player's character has no participation role (FR-MULTI-7)",
                {"field": "character_id", "reason": "is_player"},
            )
        await refuse_benched(conn, story_id, character_id)
        before = dict(view.participation or {"role": "reactive", "fixed": False})
        wanted = request.model_dump(mode="json")
        config_revision = int(row.config_revision)
        if before == wanted:
            return ParticipationSet(
                view, config_revision, int(row.timeline_revision), changed=False
            )
        if row.status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
            )
        config_revision += 1
        await conn.execute(
            update(stories).where(stories.c.id == story_id).values(config_revision=config_revision)
        )
        await conn.execute(
            update(characters)
            .where(characters.c.story_id == story_id, characters.c.id == character_id)
            .values(**_participation_values(request))
        )
        await record_config_change(
            conn,
            config_revision=config_revision,
            setting=f"{PARTICIPATION_SETTING}:{character_id}",
            old=before,
            new=wanted,
            actor=principal,
            change_class=tag_of(CharacterSettings.model_fields["participation"])
            or ConfigClass.interpretive,
            scope=ConfigScope.STORY,
            scope_id=story_id,
        )
        after = await _character_view(conn, story_id, character_id)
    return ParticipationSet(after, config_revision, int(row.timeline_revision), changed=True)


# -- bench and reactivate (FR-MULTI-13; #831) ------------------------------------------------


async def refuse_benched(conn: AsyncConnection, story_id: str, character_id: str) -> None:
    """409 ``character_benched`` when ``character_id`` is benched at the story's head
    (FR-MULTI-13): its participation and weight writes are refused while it is."""
    if await benches.bench_of(conn, story_id, character_id) is not None:
        raise CharacterBenched(
            "the character is benched; reactivate it first",
            {"character_id": character_id},
        )


class BenchRequest(BaseModel):
    """``POST /stories/{id}/characters/{cid}/bench``: the bench's ``mode`` (FR-MULTI-13).
    ``sequestered`` (the default): the benched character perceives nothing. ``present``
    (FR-MULTI-14; #832): it stays in the scene and perceives what happens where it is, but is
    never rendered and never speaks."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [{"mode": "sequestered"}]}
    )

    mode: Literal["sequestered", "present"] = "sequestered"


@dataclass(frozen=True, slots=True)
class BenchChanged:
    """A character after a bench or a reactivation (FR-MULTI-13): the character as a read lists
    it (``bench`` set while benched) and the story's revision after."""

    character: CharacterView
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


async def _bench_prechecks(
    db: AsyncEngine, story_id: str, character_id: str, expected_revision: int | None
) -> CharacterView:
    """404 for an unknown story or character, then 409 ``story_archived`` (and a paused
    story's refusal), then 409 ``revision_conflict`` for a stale expected revision."""
    async with db.connect() as conn:
        await _story_exists(conn, story_id)
        view = await _character_view(conn, story_id, character_id)
    await require_writable(db, story_id)
    await check_expected_revision(db, story_id, expected_revision)
    return view


def _already(character_id: str, found: benches.Bench) -> AlreadyBenched:
    return AlreadyBenched(
        "the character is benched already",
        {"character_id": character_id, "mode": found.mode, "since_turn": found.since_turn},
    )


def _not_benched(character_id: str) -> NotBenched:
    return NotBenched("the character is not benched", {"character_id": character_id})


async def _reactivation_room(
    conn: AsyncConnection, story_id: str, character_id: str, *, benched: bool
) -> None:
    """422 ``cast_max`` when reactivating a full (``major``) character would pass the story's
    ``max_story_cast`` (FR-MULTI-12); a ``minor`` one never counts. It runs before the 409
    ``not_benched`` (#845): an unbenched ``major`` character in a full cast is 422 too, its
    ``actual`` the count it is already part of. Only the active count is checked: the
    character is in the 200 total already, benched or not (#886)."""
    weight = await conn.scalar(
        select(characters.c.weight).where(
            characters.c.story_id == story_id, characters.c.id == character_id
        )
    )
    if weight == CharacterWeight.MAJOR.value:
        await _cast_full(conn, story_id, already_counted=not benched, reactivation=True)


async def _source_event(conn: AsyncConnection, operation_id: str, type_: str) -> str:
    found = await conn.scalar(
        select(events.c.id).where(events.c.operation_id == operation_id, events.c.type == type_)
    )
    return str(found)


async def _bench_change(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    *,
    principal: str,
    idempotency_key: str | None,
    check: Callable[[AsyncConnection], Awaitable[EventPayload]],
    write: Callable[[AsyncConnection, Operation, EventPayload, str], Awaitable[None]],
    event_type: str,
) -> BenchChanged:
    """The shared body of a bench and a reactivation: an inter-turn slot operation (FR-MULTI-13)
    holding the story's turn slot (423 ``story_busy`` while a turn is in flight, or a wait under
    ``queue``), ``check`` deciding the event (its 422 and 409 refusals) before anything is
    written and again under the story lock, then the event and ``write``'s version row."""
    from epistrel.core.states.api import turn_slot  # here: States import this module
    from epistrel.core.world_clock.slots import open_inter_turn

    operation_id = new_ulid()
    async with turn_slot(db, story_id, operation_id):
        async with db.connect() as conn:
            await _refuse_busy(conn, story_id, turns_only=True)
            payload = await check(conn)
        op = open_inter_turn(
            db,
            story_id,
            source=Provenance.CONSUMER,
            principal=principal,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
        )
        op.holds_turn_slot = True

        async def commit(conn: AsyncConnection) -> None:
            await _refuse_busy(conn, story_id, turns_only=True, own=op.operation_id)
            again = await check(conn)
            if again != payload:
                raise StoryBusy(
                    "the character changed while the request was prepared; retry",
                    {"reason": "in_flight", "story_id": story_id},
                )
            await write(conn, op, payload, await _source_event(conn, operation_id, event_type))

        async with op:
            op.append(event_type, payload, provenance=Provenance.CONSUMER)
            op.add_commit_step(commit)
            await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    async with db.connect() as conn:
        after = await _character_view(conn, story_id, character_id)
    return BenchChanged(character=after, timeline_revision=op.timeline_revision)


async def _write_version(
    conn: AsyncConnection,
    op: Operation,
    *,
    character_id: str,
    mode: str,
    benched: bool,
    source_event_id: str,
    previous: Mapping[str, Any] | None = None,
) -> None:
    from epistrel.core.membership import insert_derived
    from epistrel.core.world_clock.slots import slot_position

    position = slot_position(op)
    await insert_derived(
        conn,
        character_bench,
        [
            {
                "id": new_ulid(),
                "story_id": op.story_id,
                "character_id": character_id,
                "mode": mode,
                "benched": benched,
                "previous_role": None if previous is None else previous["role"],
                "previous_fixed": None if previous is None else previous["fixed"],
                "operation_id": op.operation_id,
                "source_event_ids": [source_event_id],
                "narrative_turn": position.turn,
                "turn_variant": position.variant,
                "slot_k": position.slot_k,
                "beat": position.beat,
                "step": position.step,
            }
        ],
    )


async def bench_character(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    request: BenchRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> BenchChanged:
    """Bench a story character (FR-MULTI-13; see ``core.bench``). Refusals, in order: 404 for an
    unknown story or character; 409 ``story_archived`` (and a paused story's refusal); 409
    ``revision_conflict``; 423 ``story_busy`` while a turn is in flight; 422
    ``validation_error`` for the player's character (``player_not_benchable``); 409
    ``already_benched`` (in any mode)."""
    view = await _bench_prechecks(db, story_id, character_id, expected_revision)

    async def check(conn: AsyncConnection) -> EventPayload:
        if view.is_player:
            raise InvalidRequest(
                "the player's character cannot be benched",
                {"field": "character_id", "reason": "player_not_benchable"},
            )
        found = await benches.bench_of(conn, story_id, character_id)
        if found is not None:
            raise _already(character_id, found)
        current = await _character_view(conn, story_id, character_id)
        role = dict(current.participation or {"role": "reactive", "fixed": False})
        return CharacterBenchedEvent(
            character_id=character_id,
            mode=request.mode,
            previous_role=str(role["role"]),
            previous_fixed=bool(role["fixed"]),
        )

    async def write(
        conn: AsyncConnection, op: Operation, payload: EventPayload, source: str
    ) -> None:
        assert isinstance(payload, CharacterBenchedEvent)  # noqa: S101 - check's own payload
        await _write_version(
            conn,
            op,
            character_id=character_id,
            mode=payload.mode,
            benched=True,
            source_event_id=source,
            previous={"role": payload.previous_role, "fixed": payload.previous_fixed},
        )

    return await _bench_change(
        db,
        story_id,
        character_id,
        principal=principal,
        idempotency_key=idempotency_key,
        check=check,
        write=write,
        event_type="character_benched",
    )


async def reactivate_character(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> BenchChanged:
    """Reactivate a benched story character (FR-MULTI-13): its participation role is restored
    from its bench. Refusals, in order: 404; 409 ``story_archived`` (and a paused story's
    refusal); 409 ``revision_conflict``; 423 ``story_busy``; 422 ``cast_max`` when the story's
    active cast is full (a ``minor`` character never counts); 409 ``not_benched``."""
    await _bench_prechecks(db, story_id, character_id, expected_revision)

    async def check(conn: AsyncConnection) -> EventPayload:
        # 422 before 409 (#845), here and in the re-check under the story lock.
        found = await benches.bench_of(conn, story_id, character_id)
        await _reactivation_room(conn, story_id, character_id, benched=found is not None)
        if found is None:
            raise _not_benched(character_id)
        return CharacterReactivated(
            character_id=character_id,
            mode=found.mode,
            restored_role=found.previous_role or "reactive",
            restored_fixed=bool(found.previous_fixed),
        )

    async def write(
        conn: AsyncConnection, op: Operation, payload: EventPayload, source: str
    ) -> None:
        assert isinstance(payload, CharacterReactivated)  # noqa: S101 - check's own payload
        await conn.execute(
            update(characters)
            .where(characters.c.story_id == story_id, characters.c.id == character_id)
            .values(
                participation_role=payload.restored_role,
                participation_fixed=payload.restored_fixed,
            )
        )
        await _write_version(
            conn,
            op,
            character_id=character_id,
            mode=payload.mode,
            benched=False,
            source_event_id=source,
        )

    return await _bench_change(
        db,
        story_id,
        character_id,
        principal=principal,
        idempotency_key=idempotency_key,
        check=check,
        write=write,
        event_type="character_reactivated",
    )


class BenchModeRequest(BaseModel):
    """``PATCH /stories/{id}/characters/{cid}/bench``: the benched character's new ``mode``
    (FR-MULTI-14; #832)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [{"mode": "present"}]}
    )

    mode: Literal["sequestered", "present"]


async def set_bench_mode(
    db: AsyncEngine,
    story_id: str,
    character_id: str,
    request: BenchModeRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> BenchChanged:
    """Switch a benched character's mode (FR-MULTI-14; #832): an inter-turn slot operation
    recording ``character_bench_mode_changed`` and one more ``benched`` version in the new mode,
    so the switch takes effect from the next turn and each earlier turn keeps the mode it had.
    The bench's ``previous_role`` carries over, so a reactivation still restores it. A switch to
    the mode it has is a 200 no-op: no event, no revision change. Refusals, in order: 404; 409
    ``story_archived`` (and a paused story's refusal); 423 ``story_busy`` while a turn is in
    flight; 409 ``not_benched``."""
    view = await _bench_prechecks(db, story_id, character_id, None)
    if view.bench is not None and view.bench["mode"] == request.mode:
        async with db.connect() as conn:
            revision = await conn.scalar(
                select(stories.c.timeline_revision).where(stories.c.id == story_id)
            )
        return BenchChanged(character=view, timeline_revision=int(revision or 0))

    async def check(conn: AsyncConnection) -> EventPayload:
        found = await benches.bench_of(conn, story_id, character_id)
        if found is None:
            raise _not_benched(character_id)
        return CharacterBenchModeChanged(
            character_id=character_id, mode=request.mode, previous_mode=found.mode
        )

    async def write(
        conn: AsyncConnection, op: Operation, payload: EventPayload, source: str
    ) -> None:
        assert isinstance(payload, CharacterBenchModeChanged)  # noqa: S101 - check's own payload
        found = await benches.bench_of(conn, story_id, character_id)
        assert found is not None  # noqa: S101 - check refused an unbenched character
        await _write_version(
            conn,
            op,
            character_id=character_id,
            mode=payload.mode,
            benched=True,
            source_event_id=source,
            previous={"role": found.previous_role, "fixed": found.previous_fixed},
        )

    return await _bench_change(
        db,
        story_id,
        character_id,
        principal=principal,
        idempotency_key=idempotency_key,
        check=check,
        write=write,
        event_type="character_bench_mode_changed",
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
    if running is None:
        # #262: an image job of the story generating or checking is running work too; #264:
        # one waiting on the paused story is not (it holds no lease, and goes with the story).
        running = await conn.scalar(
            select(image_jobs.c.id)
            .where(
                image_jobs.c.story_id == story_id,
                image_jobs.c.status.in_(("generating", "checking")),
                or_(
                    image_jobs.c.status_detail.is_(None),
                    image_jobs.c.status_detail["waiting_on"].astext.is_(None),
                ),
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
    The story's ``memories`` partition is dropped after the commit, by the
    ``memory_partition_drop`` job enqueued in the same transaction (#865; the hourly sweep
    catches any the job misses).
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
        # #865: the story's memories partition is dropped by a job that runs after this commits
        # (a detach here would hold ACCESS EXCLUSIVE on ``memories`` until the commit).
        await schedule_drop(conn, story_id)
        await pauses.pause(pauses.STORY_DELETE_BEFORE_COMMIT)

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


async def purge_story_rows(conn: AsyncConnection, story_id: str) -> dict[str, int]:
    """#250: every row of ``story_id``, the story row included, deleted in the caller's
    transaction with no deletion record (an import's refusal or reset: the story was never
    served)."""
    return await _delete_story_rows(conn, story_id)


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
    from epistrel.core.bundles import delete_story_bundles
    from epistrel.core.images.limits import release_for_story

    # #704: the story's exports and their archive files go with it.
    removed["bundle_exports"] = await delete_story_bundles(conn, story_id)
    # #262: the reservations its image jobs still hold are released and every record of the
    # story is kept with ``story_id`` nulled (deployment accounting); then the jobs go.
    await release_for_story(conn, story_id)
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
    await conn.execute(  # #233: a certification run outlives the story it certified
        update(certification_runs)
        .where(certification_runs.c.story_id == story_id)
        .values(story_id=None)
    )
    result = await conn.execute(delete(stories).where(stories.c.id == story_id))
    removed[stories.name] = result.rowcount
    return removed


# -- read -----------------------------------------------------------------------------------


_runs_on = func.coalesce(stories.c.engine_profile_version_pin, engine_profiles.c.current_version)
"""The profile version a story runs on: its pin, else the profile's latest (#113)."""


def _story_columns() -> tuple[Any, ...]:
    """A story's columns for a read, with its rating's and its engine profile's: a query of them
    selects from ``_stories_from()``."""
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
        stories.c.engine_profile_id,
        _runs_on.label("profile_version"),
        stories.c.engine_profile_version_pin,
    )


def _stories_from() -> Any:
    """``stories`` with its rating and its engine profile, what ``_story_columns`` reads."""
    return stories.join(content_ratings, content_ratings.c.id == stories.c.content_rating_id).join(
        engine_profiles, engine_profiles.c.id == stories.c.engine_profile_id
    )


def _view(row: Any, cast: tuple[CharacterView, ...] = ()) -> StoryView:
    return StoryView(
        engine_profile=StoryProfileRef(
            id=str(row.engine_profile_id),
            version=int(row.profile_version),
            pinned=row.engine_profile_version_pin is not None,
        ),
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


def _participation_values(
    spec: ParticipationSpec | None, *, default: bool = False
) -> dict[str, Any]:
    """The ``characters`` columns of ``spec`` (#176); ``default``: the defaults when ``None`` (a
    multi-row insert names every column in every row)."""
    if spec is None:
        return {"participation_role": "reactive", "participation_fixed": False} if default else {}
    return {"participation_role": spec.role, "participation_fixed": spec.fixed}


def _participation_of(row: Any) -> dict[str, Any] | None:
    """A character's ``{role, fixed}`` as a read lists it (#176); ``None`` for the player's."""
    if bool(row.is_player):
        return None
    return {
        "role": str(getattr(row, "participation_role", None) or "reactive"),
        "fixed": bool(getattr(row, "participation_fixed", False)),
    }


def _modeled_at(row: Any) -> Position:
    return Position(
        int(row.modeled_turn),
        int(row.modeled_variant),
        int(row.modeled_slot_k),
        int(row.modeled_beat),
        int(row.modeled_step),
    )


PAUSE_KEYS = ("job_id", "cause", "progress", "backfill_id", "conflicts_open")
"""The members of ``pause.details`` a story read shows (#783: the last two only when a backfill
waits for its conflicts)."""


async def get_story(db: AsyncEngine, story_id: str, *, omniscient: bool = False) -> StoryView:
    """The story with its cast in order and its engine profile at the version it runs on. True
    ages only when ``omniscient`` (FR-CAT-12)."""
    async with db.connect() as conn, conn.begin():
        row = (
            await conn.execute(
                select(*_story_columns(), stories.c.scenario_id, stories.c.scenario_version)
                .select_from(_stories_from())
                .where(stories.c.id == story_id)
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        paused = None
        if pause.is_paused(row.status):
            found = await pause.details(conn, story_id)
            paused = {k: found[k] for k in PAUSE_KEYS if k in found}
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
                    characters.c.weight,
                    characters.c.modeled_turn,
                    characters.c.modeled_variant,
                    characters.c.modeled_slot_k,
                    characters.c.modeled_beat,
                    characters.c.modeled_step,
                    characters.c.participation_role,
                    characters.c.participation_fixed,
                )
                .where(characters.c.story_id == story_id)
                .order_by(characters.c.ordinal)
            )
        ).all()
        benched = await benches.bench_states(conn, story_id)
        active = await _active_cast_count(conn, story_id)
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
                weight=str(c.weight) if omniscient else None,
                modeled_at=_modeled_at(c) if omniscient else None,
                participation=_participation_of(c),
                bench=benched[c.id].document() if c.id in benched else None,
            )
            for c in cast
        ),
    )
    scenario = (
        None
        if row.scenario_id is None
        else ScenarioOrigin(id=str(row.scenario_id), version=int(row.scenario_version))
    )
    async with db.connect() as conn:
        pruning = await _pruning(conn, story_id, view.settings)
    return dataclasses.replace(
        view,
        pause=paused,
        scenario=scenario,
        pruning=pruning,
        cast={"active": active, "benched": sum(1 for c in view.characters if c.bench)},
    )


async def _pruning(
    conn: AsyncConnection, story_id: str, settings: Mapping[str, Any]
) -> dict[str, Any]:
    """#703: the story's ``pruning`` block (see ``StoryView.pruning``)."""
    from epistrel.core.tables import events, recurring_tasks

    policy = dict(settings or {}).get("prune_policy")
    last = await conn.scalar(
        select(events.c.tx_time)
        .where(events.c.story_id == story_id, events.c.type == "pruning")
        .order_by(events.c.seq.desc())
        .limit(1)
    )
    next_run = None
    if isinstance(policy, Mapping) and policy:
        next_run = await conn.scalar(
            select(recurring_tasks.c.next_run_at).where(recurring_tasks.c.name == "prune_run")
        )
    return {
        "policy": dict(policy) if isinstance(policy, Mapping) else None,
        "last_prune_at": None if last is None else last.isoformat(),
        "next_run_at": None if next_run is None else next_run.isoformat(),
    }


async def list_stories(
    db: AsyncEngine,
    *,
    status: StoryStatus | None = None,
    owner: str | None = None,
    relations: Sequence[RelationFilter] = (),
    engine_profile: str | None = None,
    engine_profile_version: int | None = None,
) -> list[StoryView]:
    """The deployment's stories, newest first (no cast), each with the engine profile it runs
    under: every story, unless the request filters by ``status``, by ``owner``, by
    ``relations`` (a relation name, with or without its principal; several must all hold), or by
    ``engine_profile`` (any version it runs on, or only ``engine_profile_version``; #527).
    Nothing else narrows the list (FR-AUTH-8). 422 for a version without its profile, or one
    outside 1 to ``VERSION_MAX``."""
    if engine_profile_version is not None:
        field = {"field": "query.engine_profile_version"}
        if engine_profile is None:
            raise InvalidRequest("engine_profile_version needs engine_profile", field)
        if not 1 <= engine_profile_version <= profiles.VERSION_MAX:
            raise InvalidRequest(
                f"engine_profile_version must be 1 to {profiles.VERSION_MAX}", field
            )
    query = select(*_story_columns()).select_from(_stories_from())
    if status is not None:
        query = query.where(stories.c.status == status.value)
    if engine_profile is not None:
        query = query.where(stories.c.engine_profile_id == engine_profile)
    if engine_profile_version is not None:
        query = query.where(_runs_on == engine_profile_version)
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
