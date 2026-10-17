"""The scenario document: every field of a scenario, tagged with its class (FR-SCN-1..4,10,11;
docs/02 §4.10; #108).

A scenario's draft and each of its published versions are one JSON document that
``ScenarioDraft`` validates, in groups:

- ``public_card``: ``name``, ``public_description`` and ``media`` (a cover image, gallery
  images, and videos with captions, by media asset id);
- ``metadata``: ``labels`` and ``content_rating_id``;
- ``world``: the world ``description``, named ``locations`` and named ``entities`` with aliases;
- ``cast``: the character ``roles`` (FR-SCN-3) and ``max_characters_at_start`` (FR-SCN-4);
- ``starting_state``: seeded world memories, States with initial values, the starting clock,
  pre-scheduled events, author-declared predicates, closed sets and rules, and the ``opening``;
- ``drivers``: plot hooks, objectives, director guidance, planned reveals, milestones, endings;
- ``story_settings_defaults``: what the story is (its settings, story-scoped policies and the
  external providers it requires); ``recommendation``: the engine profile and capabilities it
  recommends; ``visual_style``: the art-style prompts; ``creation_rules``: what a story
  creator may override.

**Field classes (FR-SCN-11).** Every leaf field is declared ``Annotated[..., FieldClass.<class>]``
with one of the eight classes; a ``configuration`` field also carries its ``Use``
(``story_setting``, ``policy`` or ``creation_only``; docs/02 §4.10). The class fixes where a
field may go: only ``knowledge_seed`` fields become story truth, ``director`` fields reach only
the director (or the Lite prompt's author notes), and so on. ``scenario_schema_classes()`` is the
single source of the mapping ``{dotted path: (class, use)}`` (lists as ``[]``:
``cast.roles[].public_label``) that genesis (#122) and the validation report (#111) read, and
``classification_problems`` is the build-time check that no field is left untagged.

**Shape only, in M3.** States, the clock, scheduled events, predicates, closed sets and rules
are stored as given (JSON objects); their semantics arrive in M4 to M6. A seed's audience is a
list of tokens — the levels ``self``, ``acquaintances``, ``perceivable`` and ``public``, role
ids, or named groups — stored as written; routing it is M5's.

**Projections (FR-SCN-10).** ``ScenarioCard`` holds only the public card: the entry's envelope,
the name, public description, media, labels, content rating, and each role's id, public label,
setup description, kind and fill mode. ``ScenarioFull`` adds the owner, the timestamps and every
field of the document.
"""

from __future__ import annotations

import functools
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator
from pydantic.fields import FieldInfo

from epistrel.core.catalog.service import CardRating, Revision
from epistrel.core.enums import EntryStatus
from epistrel.core.ids import Ulid
from epistrel.core.json_numbers import require_finite
from epistrel.core.ownership import Owner
from epistrel.core.policies.model import AgeCondition, Rule, Target
from epistrel.core.settings_schema import (
    AUDIT_RETENTION_MAX_DAYS,
    AUDIT_RETENTION_MIN_DAYS,
    CAST_LIMIT_MAX,
    CAST_LIMIT_MIN,
    DEFAULT_MAX_CHARACTERS_AT_START,
    DeploymentSettings,
    TurnConcurrency,
)


class FieldClass(StrEnum):
    """FR-SCN-11's eight classes: where a scenario field is stored and where it may go."""

    public_card = "public_card"
    knowledge_seed = "knowledge_seed"
    story_rule = "story_rule"
    starting_state = "starting_state"
    director = "director"
    configuration = "configuration"
    recommendation = "recommendation"
    style = "style"


class Use(StrEnum):
    """How a ``configuration`` field is used (docs/02 §4.10): applied to the story's settings,
    applied as its policies, or read only while the story is created."""

    story_setting = "story_setting"
    policy = "policy"
    creation_only = "creation_only"


SCHEMA_VERSION: Final = 1
"""The document's format; every stored scenario says which it was written in."""

NAME_MAX = 120
TEXT_MAX = 8_000
"""Every free-text field (FR-SCN-2's prose: descriptions, seeds, guidance)."""
ALIAS_MAX = 60
ALIASES_MAX = 20
LABEL_MAX = 40
LABELS_MAX = 30
CAPTION_MAX = 500
PROMPT_MAX = 2_000
ROLES_MAX = 32
SEEDS_MAX = 200
"""Seeds in one scenario, all lists together: world memories, role memories, reveal truths."""
LOCATIONS_MAX = 100
ENTITIES_MAX = 100
RELATIONSHIPS_MAX = 64
GALLERY_MAX = 20
VIDEOS_MAX = 20
AUDIENCE_MAX = 40
ITEMS_MAX = 100
STRUCTURES_MAX = 200
POLICIES_MAX = 50
NAMES_MAX = 50
OVERRIDABLE_MAX = 100
PATH_MAX = 200
RATINGS_MAX = 50
AGE_MAX = 150
"""The oldest true age an eligibility range may name (#50's bound on true ages)."""

SLUG_PATTERN = r"^[a-z0-9_]{1,40}$"
"""A role, location or entity id: chosen by the author, unique in its list."""
LABEL_PATTERN = rf"^[A-Za-z0-9 _-]{{1,{LABEL_MAX}}}$"
"""A label as sent; it is stored trimmed and lower-cased (``RPG`` is ``rpg``)."""
AUDIENCE_PATTERN = r"^[a-z0-9_]([a-z0-9 _-]{0,58}[a-z0-9_])?$"
"""An audience token: a level, a role id or a named group (``family``, ``the crew``)."""
PROVIDER_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
CAPABILITY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PROFILE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
DOTTED_PATH_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"

WORLD_HOLDER = "world"
"""The holder of a seed that no role holds."""
AUDIENCE_LEVELS = ("self", "acquaintances", "perceivable", "public")
"""FR-SCN-8's audience levels; more (named groups) need no schema change."""
RESERVED_ROLE_IDS = frozenset({WORLD_HOLDER, *AUDIENCE_LEVELS})
"""Ids a role cannot take, because a seed's holder or audience names them with another
meaning. ``npc`` and ``player`` are not reserved (#116's companion roles use them)."""


def _non_blank(value: str) -> str:
    """``value`` without surrounding whitespace; a blank one is refused."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _label(value: str) -> str:
    """A label as stored: trimmed and lower-cased; a blank one is refused."""
    return _non_blank(value).lower()


def _unique[T](values: list[T]) -> list[T]:
    if len(set(values)) != len(values):
        raise ValueError("lists a value more than once")
    return values


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(_non_blank)]
"""A name (a scenario's, a location's, a role's public label): trimmed, never blank."""
Prose = Annotated[str, Field(min_length=1, max_length=TEXT_MAX), AfterValidator(_non_blank)]
"""Free text that must say something (a seed, a setup description, a plot hook)."""
Text = Annotated[str, Field(max_length=TEXT_MAX)]
"""Free text, stored as written."""
Alias = Annotated[str, Field(min_length=1, max_length=ALIAS_MAX), AfterValidator(_non_blank)]
Label = Annotated[str, Field(pattern=LABEL_PATTERN), AfterValidator(_label)]
Slug = Annotated[str, Field(pattern=SLUG_PATTERN)]
AudienceToken = Annotated[str, Field(pattern=AUDIENCE_PATTERN)]
Caption = Annotated[str, Field(max_length=CAPTION_MAX)]
Prompt = Annotated[str, Field(max_length=PROMPT_MAX)]
Age = Annotated[int, Field(ge=0, le=AGE_MAX)]
CastLimit = Annotated[int, Field(ge=CAST_LIMIT_MIN, le=CAST_LIMIT_MAX)]
Structure = Annotated[dict[str, Any], AfterValidator(require_finite)]
"""A State, the clock, a scheduled event, a predicate, a closed set or a rule (and a seed's
valid time, a role's State values, the creation bounds): stored as given (a JSON object) in M3;
M4 to M6 give them their semantics. Every number in it must be finite, so the stored document
round-trips (``core.json_numbers``)."""

RoleKind = Literal["npc", "player"]
FillMode = Literal["open", "default", "locked"]
Participation = Literal["primary", "reactive", "silent"]
Disclosure = Literal["open", "guarded", "secret"]
SeedKind = Literal["canon", "belief", "nested", "scheduled"]
InteractionMode = Literal["sequential", "simultaneous"]
PlayerAuthority = Literal["own_character", "co_author"]
RetconTiming = Literal["include_if_possible", "ask_author"]
BackfillConflictPolicy = Literal["last_mention", "first_mention", "ask"]


class ScenarioModel(BaseModel):
    """Base of every model of the scenario document: unknown keys are refused, and
    ``scenario_schema_classes`` walks exactly these models."""

    model_config = ConfigDict(extra="forbid")


# -- the public card ---------------------------------------------------------------------------


class ScenarioVideo(ScenarioModel):
    """A video on the scenario's public card: the media asset and the caption shown with it."""

    media_id: Annotated[Ulid, FieldClass.public_card]
    caption: Annotated[Caption | None, FieldClass.public_card] = None


class ScenarioMedia(ScenarioModel):
    """The public card's media (FR-SCN-2, FR-MEDIA-1): a cover image, gallery images and
    videos, each a live media asset when it is added."""

    cover: Annotated[Ulid | None, FieldClass.public_card] = None
    gallery: Annotated[list[Ulid], FieldClass.public_card] = Field(
        default_factory=list, max_length=GALLERY_MAX
    )
    videos: list[ScenarioVideo] = Field(default_factory=list, max_length=VIDEOS_MAX)


class ScenarioPublicCard(ScenarioModel):
    """What a player browsing scenarios may see: the name, a public description (never
    seeded into a story) and media."""

    name: Annotated[Name | None, FieldClass.public_card] = None
    public_description: Annotated[Text | None, FieldClass.public_card] = None
    media: ScenarioMedia = Field(default_factory=ScenarioMedia)


class ScenarioMetadata(ScenarioModel):
    """Search and filtering metadata, returned with the card and never seeded: labels that
    describe the scenario and its content rating."""

    labels: Annotated[list[Label], FieldClass.public_card] = Field(
        default_factory=list, max_length=LABELS_MAX
    )
    content_rating_id: Annotated[Ulid | None, FieldClass.public_card] = None

    @field_validator("labels")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        return _unique(value)


# -- the world ---------------------------------------------------------------------------------


class ScenarioLocation(ScenarioModel):
    """A named place in the world: an id roles and the opening start at, a name and a
    description."""

    id: Annotated[Slug, FieldClass.knowledge_seed]
    name: Annotated[Name, FieldClass.knowledge_seed]
    description: Annotated[Text | None, FieldClass.knowledge_seed] = None


EntityKind = Literal["person", "place", "organization", "item", "event", "other"]
"""A scenario entity's kind (``core.propositions.model.ENTITY_KINDS``; #139)."""

EVENT_TYPES_MAX = 50


class ScenarioEntity(ScenarioModel):
    """A named entity of the world (an organisation, an item, a place) with its aliases and,
    optionally, its ``kind`` (#139: without one it is ``other``)."""

    id: Annotated[Slug, FieldClass.knowledge_seed]
    name: Annotated[Name, FieldClass.knowledge_seed]
    kind: Annotated[EntityKind | None, FieldClass.knowledge_seed] = None
    aliases: Annotated[list[Alias], FieldClass.knowledge_seed] = Field(
        default_factory=list, max_length=ALIASES_MAX
    )
    description: Annotated[Text | None, FieldClass.knowledge_seed] = None


class ScenarioWorld(ScenarioModel):
    """The world: its description (setting and lore, seeded as canon), named locations, named
    entities and, optionally, the story's own event types (#139: occasion words beyond the
    engine's vocabulary, each its own word, e.g. ``joust``)."""

    description: Annotated[Text | None, FieldClass.knowledge_seed] = None
    locations: list[ScenarioLocation] = Field(default_factory=list, max_length=LOCATIONS_MAX)
    entities: list[ScenarioEntity] = Field(default_factory=list, max_length=ENTITIES_MAX)
    event_types: Annotated[list[Alias] | None, FieldClass.knowledge_seed] = Field(
        default=None, max_length=EVENT_TYPES_MAX
    )

    @field_validator("event_types")
    @classmethod
    def _distinct_types(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _unique(value)


# -- seeds -------------------------------------------------------------------------------------


class Seed(ScenarioModel):
    """A seeded item (FR-SCN-3,8): its ``text``, its ``holder`` (a role id, or ``world``), its
    ``audience`` (who starts out knowing it) and its ``disclosure`` (how the holder treats it:
    ``open``, ``guarded`` or ``secret``), with an optional ``kind`` (``canon``, ``belief``,
    ``nested`` or ``scheduled``) and ``valid_time``. ``id`` is assigned by the server when a
    save stores the seed; send it back to keep it. A draft may leave the holder, audience and
    disclosure out; publishing needs all three."""

    id: Annotated[Ulid | None, FieldClass.knowledge_seed] = None
    text: Annotated[Prose, FieldClass.knowledge_seed]
    holder: Annotated[Slug | None, FieldClass.knowledge_seed] = None
    audience: Annotated[list[AudienceToken] | None, FieldClass.knowledge_seed] = Field(
        default=None, min_length=1, max_length=AUDIENCE_MAX
    )
    disclosure: Annotated[Disclosure | None, FieldClass.knowledge_seed] = None
    kind: Annotated[SeedKind | None, FieldClass.knowledge_seed] = None
    valid_time: Annotated[Structure | None, FieldClass.knowledge_seed] = None

    @field_validator("audience")
    @classmethod
    def _distinct(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _unique(value)


# -- the cast ----------------------------------------------------------------------------------


class RoleCharacter(ScenarioModel):
    """The catalog character a ``default`` or ``locked`` role is filled with: its id and,
    optionally, a version (omitted: the latest published when a story is created)."""

    catalog_id: Annotated[Ulid, FieldClass.configuration, Use.creation_only]
    version: Annotated[Revision | None, FieldClass.configuration, Use.creation_only] = None


class RoleEligibility(ScenarioModel):
    """Who may fill a role (FR-SCN-3): a character needs every label listed, one of the
    content ratings listed (when any are), and a true age within ``age_range``
    (``[min, max]``, inclusive, either end open). Never applied to the player's persona."""

    labels: Annotated[list[Label], FieldClass.configuration, Use.creation_only] = Field(
        default_factory=list, max_length=LABELS_MAX
    )
    content_ratings: Annotated[list[Ulid], FieldClass.configuration, Use.creation_only] = Field(
        default_factory=list, max_length=RATINGS_MAX
    )
    age_range: Annotated[
        tuple[Age | None, Age | None] | None, FieldClass.configuration, Use.creation_only
    ] = None

    @field_validator("labels", "content_ratings")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        return _unique(value)

    @field_validator("age_range")
    @classmethod
    def _ordered(
        cls, value: tuple[int | None, int | None] | None
    ) -> tuple[int | None, int | None] | None:
        if value is not None:
            youngest, oldest = value
            if youngest is not None and oldest is not None and youngest > oldest:
                raise ValueError("the youngest age is above the oldest")
        return value


class RoleRelationship(ScenarioModel):
    """A relationship of the role to another role (``mother_of``, ``sibling_of``), seeded as
    canon and as beliefs of the roles that know it. Free text; no inverse is added."""

    to_role: Annotated[Slug, FieldClass.knowledge_seed]
    name: Annotated[Name, FieldClass.knowledge_seed]


class NameOverrides(ScenarioModel):
    """Names the role gives whoever fills it in the story: ``canonical`` replaces the
    character's name, ``aliases`` are added to its own."""

    canonical: Annotated[Name | None, FieldClass.knowledge_seed] = None
    aliases: Annotated[list[Alias], FieldClass.knowledge_seed] = Field(
        default_factory=list, max_length=ALIASES_MAX
    )


class ScenarioRole(ScenarioModel):
    """A character role (a slot; FR-SCN-3). ``public_label`` and ``setup_description`` are
    shown to the player at setup. ``kind`` is ``npc`` or ``player`` (at most one player role);
    ``fill`` is ``open`` (the creator picks or defines a character), ``default`` (pre-filled
    with ``character``, replaceable) or ``locked`` (fixed to ``character``). A role defines
    situation, not personality: whoever fills it brings their own."""

    id: Annotated[Slug, FieldClass.public_card]
    public_label: Annotated[Name, FieldClass.public_card]
    setup_description: Annotated[Prose, FieldClass.public_card]
    kind: Annotated[RoleKind, FieldClass.configuration, Use.creation_only]
    required: Annotated[bool, FieldClass.configuration, Use.creation_only] = True
    fill: Annotated[FillMode, FieldClass.configuration, Use.creation_only] = "open"
    character: RoleCharacter | None = None
    eligibility: RoleEligibility | None = None
    seeded_memories: list[Seed] = Field(default_factory=list, max_length=SEEDS_MAX)
    relationships: list[RoleRelationship] = Field(
        default_factory=list, max_length=RELATIONSHIPS_MAX
    )
    state_initial_values: Annotated[Structure | None, FieldClass.starting_state] = None
    starting_location: Annotated[Slug | None, FieldClass.knowledge_seed] = None
    participation: Annotated[Participation | None, FieldClass.configuration, Use.story_setting] = (
        None
    )
    name_overrides: NameOverrides | None = None


class ScenarioCast(ScenarioModel):
    """The cast: the roles, and the most character slots (player slots included) a story
    starts with (FR-SCN-4; ``null``: the deployment's default)."""

    roles: list[ScenarioRole] = Field(default_factory=list, max_length=ROLES_MAX)
    max_characters_at_start: Annotated[
        CastLimit | None, FieldClass.configuration, Use.creation_only
    ] = None


# -- the starting state ------------------------------------------------------------------------


class ScenarioOpening(ScenarioModel):
    """How the story opens: the opening narration (turn 1), where it starts and which roles are
    present."""

    narration: Annotated[Text | None, FieldClass.starting_state] = None
    starting_location: Annotated[Slug | None, FieldClass.knowledge_seed] = None
    roles_present: Annotated[list[Slug], FieldClass.starting_state] = Field(
        default_factory=list, max_length=ROLES_MAX
    )


class ScenarioStartingState(ScenarioModel):
    """The story's starting state: seeded world memories, State trackers with initial values,
    the starting clock, pre-scheduled events, author-declared predicates, closed sets and rules
    (each stored as given in M3), and the opening."""

    seeded_memories: list[Seed] = Field(default_factory=list, max_length=SEEDS_MAX)
    states: Annotated[list[Structure], FieldClass.starting_state] = Field(
        default_factory=list, max_length=STRUCTURES_MAX
    )
    clock: Annotated[Structure | None, FieldClass.starting_state] = None
    scheduled_events: Annotated[list[Structure], FieldClass.knowledge_seed] = Field(
        default_factory=list, max_length=STRUCTURES_MAX
    )
    predicates: Annotated[list[Structure], FieldClass.story_rule] = Field(
        default_factory=list, max_length=STRUCTURES_MAX
    )
    closed_sets: Annotated[list[Structure], FieldClass.story_rule] = Field(
        default_factory=list, max_length=STRUCTURES_MAX
    )
    rules: Annotated[list[Structure], FieldClass.story_rule] = Field(
        default_factory=list, max_length=STRUCTURES_MAX
    )
    opening: ScenarioOpening = Field(default_factory=ScenarioOpening)


# -- the story drivers -------------------------------------------------------------------------


class PlannedReveal(ScenarioModel):
    """A hidden truth (seeded as secret canon) with the roles allowed to learn it and when."""

    truth: Seed
    allowed_roles: Annotated[list[Slug], FieldClass.director] = Field(
        default_factory=list, max_length=ROLES_MAX
    )
    conditions: Annotated[Text | None, FieldClass.director] = None


class ScenarioDrivers(ScenarioModel):
    """What drives the plot: hooks, objectives, hidden director guidance, planned reveals, and
    optional milestones and endings. Instructions to the storytelling, never facts."""

    plot_hooks: Annotated[list[Prose], FieldClass.director] = Field(
        default_factory=list, max_length=ITEMS_MAX
    )
    objectives: Annotated[list[Prose], FieldClass.director] = Field(
        default_factory=list, max_length=ITEMS_MAX
    )
    director_guidance: Annotated[Text | None, FieldClass.director] = None
    planned_reveals: list[PlannedReveal] = Field(default_factory=list, max_length=ITEMS_MAX)
    milestones: Annotated[list[Prose], FieldClass.director] = Field(
        default_factory=list, max_length=ITEMS_MAX
    )
    endings: Annotated[list[Prose], FieldClass.director] = Field(
        default_factory=list, max_length=ITEMS_MAX
    )


# -- configuration -----------------------------------------------------------------------------

PolicyName = Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_non_blank)]
PolicyMessage = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_non_blank)]
ProviderName = Annotated[str, Field(pattern=PROVIDER_PATTERN)]
CapabilityName = Annotated[str, Field(pattern=CAPABILITY_PATTERN)]
ProfileId = Annotated[str, Field(pattern=PROFILE_ID_PATTERN)]
DottedPath = Annotated[str, Field(max_length=PATH_MAX, pattern=DOTTED_PATH_PATTERN)]


class ScenarioPolicy(ScenarioModel):
    """A story-scoped guardrail policy every story from the scenario gets (FR-GUARD): a name,
    the targets it applies to (default: text output), a deterministic or model-assisted rule,
    an optional age condition, and the message a refusal carries. It can add to the global and
    rating policies, never relax one."""

    name: Annotated[PolicyName, FieldClass.configuration, Use.policy]
    applies_to: Annotated[list[Target] | None, FieldClass.configuration, Use.policy] = Field(
        default=None, min_length=1
    )
    rule: Annotated[Rule, FieldClass.configuration, Use.policy]
    age_condition: Annotated[AgeCondition | None, FieldClass.configuration, Use.policy] = None
    message: Annotated[PolicyMessage, FieldClass.configuration, Use.policy]


class StorySettingsDefaults(ScenarioModel):
    """The settings a story created from the scenario starts with (what the story is, not how
    hard the engine works): each ``null`` leaves the deployment's or story's default. The
    output viewpoint is a role id, which genesis turns into that role's character. Also the
    story-scoped ``policies`` and the external providers the scenario requires. Unknown
    setting names are refused."""

    interaction_mode: Annotated[
        InteractionMode | None, FieldClass.configuration, Use.story_setting
    ] = None
    turn_concurrency: Annotated[
        TurnConcurrency | None, FieldClass.configuration, Use.story_setting
    ] = None
    player_authority: Annotated[
        PlayerAuthority | None, FieldClass.configuration, Use.story_setting
    ] = None
    default_output_viewpoint: Annotated[
        Slug | None, FieldClass.configuration, Use.story_setting
    ] = None
    ambiguous_retcon_timing: Annotated[
        RetconTiming | None, FieldClass.configuration, Use.story_setting
    ] = None
    backfill_conflict_policy: Annotated[
        BackfillConflictPolicy | None, FieldClass.configuration, Use.story_setting
    ] = None
    audit_retention_days: Annotated[int | None, FieldClass.configuration, Use.story_setting] = (
        Field(default=None, ge=AUDIT_RETENTION_MIN_DAYS, le=AUDIT_RETENTION_MAX_DAYS)
    )
    policies: list[ScenarioPolicy] = Field(default_factory=list, max_length=POLICIES_MAX)
    required_providers: Annotated[list[ProviderName], FieldClass.configuration, Use.policy] = Field(
        default_factory=list, max_length=NAMES_MAX
    )

    @field_validator("required_providers")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        return _unique(value)


class ScenarioRecommendation(ScenarioModel):
    """The engine profile and capabilities the scenario recommends (FR-PROF-7): compared with
    the story's profile at creation, producing warnings, never blocks."""

    engine_profile_id: Annotated[ProfileId | None, FieldClass.recommendation] = None
    capabilities: Annotated[list[CapabilityName], FieldClass.recommendation] = Field(
        default_factory=list, max_length=NAMES_MAX
    )

    @field_validator("capabilities")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        return _unique(value)


class VisualStyle(ScenarioModel):
    """The art style every image generated for the scenario uses (FR-IMG-2): never in a
    text-generation prompt."""

    prompt: Annotated[Prompt | None, FieldClass.style] = None
    negative_prompt: Annotated[Prompt | None, FieldClass.style] = None


class CreationRules(ScenarioModel):
    """What a story creator may change at creation: ``overridable`` dotted paths (a path names
    the field and everything under it), and optional ``bounds`` for settings."""

    overridable: Annotated[list[DottedPath], FieldClass.configuration, Use.creation_only] = Field(
        default_factory=list, max_length=OVERRIDABLE_MAX
    )
    bounds: Annotated[Structure | None, FieldClass.configuration, Use.creation_only] = None

    @field_validator("overridable")
    @classmethod
    def _distinct(cls, value: list[str]) -> list[str]:
        return _unique(value)


# -- the document ------------------------------------------------------------------------------


class ScenarioDraft(ScenarioModel):
    """A scenario's draft document (FR-SCN-2): the public card, metadata, world, cast,
    starting state, story drivers, story settings defaults, recommendation, visual style and
    creation rules. Every group may be left out of a draft; a published scenario needs a
    ``public_card.name`` and every seed's holder, audience and disclosure (#111 adds its
    rules). Only the public card and the metadata ever reach a ``card`` read."""

    schema_version: Annotated[Literal[1], FieldClass.configuration, Use.creation_only] = (
        SCHEMA_VERSION
    )
    public_card: ScenarioPublicCard = Field(default_factory=ScenarioPublicCard)
    metadata: ScenarioMetadata = Field(default_factory=ScenarioMetadata)
    world: ScenarioWorld = Field(default_factory=ScenarioWorld)
    cast: ScenarioCast = Field(default_factory=ScenarioCast)
    starting_state: ScenarioStartingState = Field(default_factory=ScenarioStartingState)
    drivers: ScenarioDrivers = Field(default_factory=ScenarioDrivers)
    story_settings_defaults: StorySettingsDefaults = Field(default_factory=StorySettingsDefaults)
    recommendation: ScenarioRecommendation = Field(default_factory=ScenarioRecommendation)
    visual_style: VisualStyle = Field(default_factory=VisualStyle)
    creation_rules: CreationRules = Field(default_factory=CreationRules)

    def document(self) -> dict[str, Any]:
        """The stored form: JSON, with the fields that are not set left out."""
        stored: dict[str, Any] = self.model_dump(
            mode="json", exclude_none=True, include=set(ScenarioDraft.model_fields)
        )
        return stored

    def seeds(self) -> Iterator[tuple[tuple[str | int, ...], Seed]]:
        """Every seed with its location: world memories, each role's memories, and each
        planned reveal's truth."""
        for index, seed in enumerate(self.starting_state.seeded_memories):
            yield ("starting_state", "seeded_memories", index), seed
        for role_index, role in enumerate(self.cast.roles):
            for index, seed in enumerate(role.seeded_memories):
                yield ("cast", "roles", role_index, "seeded_memories", index), seed
        for index, reveal in enumerate(self.drivers.planned_reveals):
            yield ("drivers", "planned_reveals", index, "truth"), reveal.truth


EXAMPLE: dict[str, Any] = {
    "public_card": {
        "name": "Sunday Dinner",
        "public_description": "The whole family is home for the weekend.",
    },
    "metadata": {"labels": ["family", "slice of life"]},
    "world": {
        "description": "A farmhouse outside a small river town.",
        "locations": [{"id": "kitchen", "name": "The kitchen"}],
    },
    "cast": {
        "roles": [
            {
                "id": "mother",
                "public_label": "Mother",
                "setup_description": "Her children are home for the weekend.",
                "kind": "npc",
                "fill": "open",
                "eligibility": {"age_range": [40, 70]},
                "seeded_memories": [
                    {
                        "text": "The house is being sold.",
                        "holder": "mother",
                        "audience": ["self"],
                        "disclosure": "secret",
                    }
                ],
                "starting_location": "kitchen",
            },
            {
                "id": "player",
                "public_label": "The eldest",
                "setup_description": "Back home for the first time in a year.",
                "kind": "player",
                "relationships": [{"to_role": "mother", "name": "child_of"}],
            },
        ],
        "max_characters_at_start": 6,
    },
    "starting_state": {
        "opening": {
            "narration": "Rain on the windows; the roast is late.",
            "starting_location": "kitchen",
            "roles_present": ["mother", "player"],
        }
    },
    "drivers": {"director_guidance": "Keep the sale secret until the meal ends."},
}
"""A draft that saves: what the request bodies' OpenAPI examples show."""


class ScenarioCreate(ScenarioDraft):
    """``POST /catalog/scenarios``: the new scenario's first draft — any of the draft's groups
    — and, optionally, its ``owner``: an opaque principal, by default the requesting one, never
    ``system`` (FR-AUTH-5). Ownership confers no permission (FR-AUTH-8)."""

    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE]})

    owner: Owner | None = Field(
        default=None,
        description="The scenario's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )


GROUPS = tuple(name for name in ScenarioDraft.model_fields if name != "schema_version")
"""The document's groups, in order: what a patch may change."""

_GROUP = (
    "A JSON merge patch over the draft's `{group}` group (see `ScenarioCreate`): a key sets "
    "that field, `null` removes it, an object merges, an array replaces the stored one."
)


class ScenarioPatch(BaseModel):
    """``PATCH /catalog/scenarios/{id}``: a JSON merge patch (RFC 7386) over the draft, group
    by group — a key sets that field, ``null`` removes it, an object merges into the stored one
    and an array replaces it (``roles``, ``seeded_memories``, …) — with the ``draft_revision``
    it is based on (FR-CAT-3). The merged draft is validated as a whole: an invalid field is 422
    at ``body.<path>``, wherever the field came from."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "draft_revision": 1,
                    "public_card": {"public_description": "Everyone is home, for once."},
                    "drivers": {"plot_hooks": ["The will is read after dessert."]},
                }
            ]
        },
    )

    draft_revision: Revision = Field(
        description="The draft revision this patch is based on; a stale one is 409 `draft_stale`."
    )
    public_card: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="public_card")
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="metadata")
    )
    world: dict[str, Any] | None = Field(default=None, description=_GROUP.format(group="world"))
    cast: dict[str, Any] | None = Field(default=None, description=_GROUP.format(group="cast"))
    starting_state: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="starting_state")
    )
    drivers: dict[str, Any] | None = Field(default=None, description=_GROUP.format(group="drivers"))
    story_settings_defaults: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="story_settings_defaults")
    )
    recommendation: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="recommendation")
    )
    visual_style: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="visual_style")
    )
    creation_rules: dict[str, Any] | None = Field(
        default=None, description=_GROUP.format(group="creation_rules")
    )

    def document(self) -> dict[str, Any]:
        """The merge patch itself: the groups the request set (``null`` included), without
        ``draft_revision``. The groups are kept as parsed (a JSON dump would turn a number
        too large for a float into ``null``, which a merge patch reads as "remove"); the
        merged draft is validated as a whole."""
        patch: dict[str, Any] = self.model_dump(exclude_unset=True, exclude={"draft_revision"})
        return patch


# -- field classes (FR-SCN-11) -----------------------------------------------------------------


def _optional_inner(annotation: Any) -> Any:
    """``X`` for ``X | None`` (any union with ``None`` and one other member), else
    ``annotation``."""
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            return members[0]
    return annotation


def _group(annotation: Any) -> tuple[type[ScenarioModel], bool] | None:
    """``(model, is_list)`` when a field holds a scenario model (``M``, ``M | None``) or a list
    of them; ``None`` for a leaf."""
    inner = _optional_inner(annotation)
    if isinstance(inner, type) and issubclass(inner, ScenarioModel):
        return inner, False
    if typing.get_origin(inner) is list:
        (item,) = typing.get_args(inner)
        if isinstance(item, type) and issubclass(item, ScenarioModel):
            return item, True
    return None


def _tags(info: FieldInfo) -> tuple[list[FieldClass], list[Use]]:
    classes = [item for item in info.metadata if isinstance(item, FieldClass)]
    uses = [item for item in info.metadata if isinstance(item, Use)]
    return classes, uses


def _fields(model: type[BaseModel], prefix: str = "") -> Iterator[tuple[str, FieldInfo, bool]]:
    """Every field of ``model`` as ``(dotted path, field, is_group)``, groups walked: a list's
    items are ``name[]``."""
    for name, info in model.model_fields.items():
        group = _group(info.annotation)
        if group is None:
            yield f"{prefix}{name}", info, False
            continue
        child, is_list = group
        yield f"{prefix}{name}", info, True
        yield from _fields(child, f"{prefix}{name}{'[]' if is_list else ''}.")


def classification_problems(model: type[BaseModel]) -> list[str]:
    """Every field of ``model`` that breaks FR-SCN-11's tagging: a leaf with no class (or two),
    a ``configuration`` leaf without exactly one ``Use``, a ``Use`` on another class, and a
    group that carries a class of its own (its leaves carry theirs). The build-time check:
    ``[]`` for the scenario document."""
    problems: list[str] = []
    for path, info, is_group in _fields(model):
        classes, uses = _tags(info)
        if is_group:
            if classes or uses:
                problems.append(f"{path}: a group carries no class; its fields do")
            continue
        if not classes:
            problems.append(f"{path}: untagged")
            continue
        if len(classes) > 1:
            problems.append(f"{path}: more than one class")
            continue
        if classes[0] is FieldClass.configuration and len(uses) != 1:
            problems.append(f"{path}: a configuration field needs exactly one use")
        elif classes[0] is not FieldClass.configuration and uses:
            problems.append(f"{path}: only a configuration field has a use")
    return problems


ClassMap = dict[str, tuple[FieldClass, Use | None]]


def field_classes(model: type[BaseModel]) -> ClassMap:
    """``{dotted path: (class, use)}`` for every leaf of ``model``; ``ValueError`` when a field
    is untagged or mis-tagged (``classification_problems``)."""
    problems = classification_problems(model)
    if problems:
        raise ValueError("scenario fields without a valid class: " + "; ".join(problems))
    found: ClassMap = {}
    for path, info, is_group in _fields(model):
        if not is_group:
            classes, uses = _tags(info)
            found[path] = (classes[0], uses[0] if uses else None)
    return found


@functools.cache
def _scenario_classes() -> tuple[tuple[str, tuple[FieldClass, Use | None]], ...]:
    return tuple(field_classes(ScenarioDraft).items())


def scenario_schema_classes() -> ClassMap:
    """Every scenario field's class (and, for ``configuration``, its use), by dotted path, list
    items as ``[]`` (``cast.roles[].public_label``): the one map genesis (#122) and the
    validation report (#111) read (FR-SCN-11)."""
    return dict(_scenario_classes())


# -- the starting-cast limit (FR-SCN-4) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CastLimits:
    """The deployment's starting-cast settings: the default every scenario without its own
    value gets, and the optional ceiling no scenario or story creation may exceed."""

    default: int = DEFAULT_MAX_CHARACTERS_AT_START
    ceiling: int | None = None

    @classmethod
    def of(cls, settings: DeploymentSettings) -> CastLimits:
        return cls(
            default=settings.max_characters_at_start_default,
            ceiling=settings.max_characters_at_start_ceiling,
        )


def effective_cast_limit(scenario: int | None, request: int | None, deployment: CastLimits) -> int:
    """The most character slots, player slots included, a story may start with (FR-SCN-4):
    the creation ``request``'s override, else the ``scenario``'s own value, else the
    ``deployment``'s default — capped by the deployment's ceiling when one is set. Characters
    introduced later in a story are not counted."""
    if request is not None:
        chosen = request
    elif scenario is not None:
        chosen = scenario
    else:
        chosen = deployment.default
    return chosen if deployment.ceiling is None else min(chosen, deployment.ceiling)


# -- projections (FR-SCN-10) -------------------------------------------------------------------


class ScenarioCardMedia(BaseModel):
    """One media asset on a scenario's card: its id, its alt text (the asset's), and its
    caption (a video's caption in the scenario, else the asset's)."""

    model_config = ConfigDict(extra="forbid")

    media_id: str
    alt_text: str | None
    caption: str | None


class ScenarioCardMediaSet(BaseModel):
    """The card's media: the cover image, the gallery images and the videos."""

    model_config = ConfigDict(extra="forbid")

    cover: ScenarioCardMedia | None
    gallery: list[ScenarioCardMedia]
    videos: list[ScenarioCardMedia]


class ScenarioCardRole(BaseModel):
    """A role as the card shows it: what a player choosing who fills it needs, nothing
    private."""

    model_config = ConfigDict(extra="forbid")

    id: str
    public_label: str
    setup_description: str
    kind: RoleKind
    fill: FillMode


class ScenarioCard(BaseModel):
    """A scenario's public card (``projection=card``, the default; FR-SCN-10): the entry's
    envelope, the name, public description, media, labels and content rating, and each role's
    public label and setup description with its kind and fill mode — never a world
    description, seed, secret, director guidance or any other private field. ``version`` is the
    version shown (a draft read shows the draft with the latest published version, ``null``
    before the first publish); ``companion_of`` names the character whose companion this is
    (#116)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int | None
    listing_status: str
    status: EntryStatus
    companion_of: str | None
    name: str | None
    public_description: str | None
    media: ScenarioCardMediaSet
    labels: list[str]
    content_rating: CardRating | None
    roles: list[ScenarioCardRole]


class ScenarioListItem(ScenarioCard):
    """A scenario as a listing shows it (``GET /catalog/scenarios``; #110): exactly its public
    card (what a single read answers with ``projection=card``, ``companion_of`` included), plus
    its ``owner_principal`` (recorded for listing and attribution only, FR-AUTH-8) and when it
    last changed (``updated_at``: a draft save, publish, archive, listing-status change or owner
    transfer). ``rank`` is present on a search (``q``): its text-search rank, the order's first
    key."""

    owner_principal: str
    updated_at: datetime
    rank: float | None = Field(default=None, exclude_if=lambda rank: rank is None)


class ScenarioFull(BaseModel):
    """A scenario in full (``projection=full``; FR-SCN-10): the entry's envelope and every field
    of the document. ``draft_revision`` is present only on a draft read (``?draft=true``): the
    revision the next save or publish must send. ``published_at`` is when ``version`` was
    published. ``owner_principal`` is the entry's owner (FR-AUTH-5), recorded for listing and
    attribution only. A companion (``companion_of``, #116) also shows ``pinned_character_version``:
    the version of its character the shown version or draft is pinned to (``null`` before the
    character's first publish, and for every other scenario); its locked role's
    ``character.version`` holds the same."""

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
    companion_of: str | None
    pinned_character_version: int | None
    schema_version: Literal[1]
    public_card: ScenarioPublicCard
    metadata: ScenarioMetadata
    world: ScenarioWorld
    cast: ScenarioCast
    starting_state: ScenarioStartingState
    drivers: ScenarioDrivers
    story_settings_defaults: StorySettingsDefaults
    recommendation: ScenarioRecommendation
    visual_style: VisualStyle
    creation_rules: CreationRules


def card_roles(draft: ScenarioDraft) -> list[ScenarioCardRole]:
    """Each role of ``draft`` as a scenario's card shows it, in the document's order: its id,
    public label, setup description, kind and fill mode. The one builder of a card's roles
    (``ScenarioCatalog.project`` calls it)."""
    return [
        ScenarioCardRole(
            id=role.id,
            public_label=role.public_label,
            setup_description=role.setup_description,
            kind=role.kind,
            fill=role.fill,
        )
        for role in draft.cast.roles
    ]


__all__ = [
    "AUDIENCE_LEVELS",
    "GROUPS",
    "RESERVED_ROLE_IDS",
    "SCHEMA_VERSION",
    "SEEDS_MAX",
    "WORLD_HOLDER",
    "CastLimits",
    "ClassMap",
    "CreationRules",
    "FieldClass",
    "NameOverrides",
    "PlannedReveal",
    "RoleCharacter",
    "RoleEligibility",
    "RoleRelationship",
    "ScenarioCard",
    "ScenarioCardMedia",
    "ScenarioCardMediaSet",
    "ScenarioCardRole",
    "ScenarioCast",
    "ScenarioCreate",
    "ScenarioDraft",
    "ScenarioDrivers",
    "ScenarioEntity",
    "ScenarioFull",
    "ScenarioListItem",
    "ScenarioLocation",
    "ScenarioMedia",
    "ScenarioMetadata",
    "ScenarioModel",
    "ScenarioOpening",
    "ScenarioPatch",
    "ScenarioPolicy",
    "ScenarioPublicCard",
    "ScenarioRecommendation",
    "ScenarioRole",
    "ScenarioStartingState",
    "ScenarioVideo",
    "ScenarioWorld",
    "Seed",
    "StorySettingsDefaults",
    "Use",
    "VisualStyle",
    "card_roles",
    "classification_problems",
    "effective_cast_limit",
    "field_classes",
    "scenario_schema_classes",
]
