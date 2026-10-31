"""The request of a story's creation from a scenario and its report (FR-SCN-6; #119), and the
player half (FR-CAT-6; #114).

``StoryFromScenario`` is ``POST /stories``' body when it names ``scenario`` (``is_scenario_body``):
the scenario version, the role assignments (``SlotChoice``: a catalog ``character@version`` or
an ad-hoc definition), the players, overrides and the creation's options. ``CreationReport`` is
#111's report with counts. ``core.genesis.create`` holds the rules.

The player half:

A story created from a scenario names, for each player, the role they fill and the persona they
play as: ``players: {role_id: PlayerAssignment}``, where an assignment is the player's
``principal`` and their ``persona``, either a saved one (``{id}``) or an ad-hoc one
(``{ad_hoc: {name, gender?, age, description}}``, exactly a persona's create body without its
owner). A player plays a persona, never a catalog character, and the scenario fixes which role
the player fills (FR-CAT-6). #119's genesis applies two checks, each answering the creation
report's entries (``core.catalog.report``: outcome ``rejected``, so any one refuses the
creation with nothing written):

- ``player_role_entries`` (pure): ``player_role_fixed`` for a ``players`` key that names a role
  of kind ``npc``, and for a role of kind ``player`` that ``slots`` also fills (a catalog
  character or an ad-hoc definition on the player's role). A ``players`` key or ``slots`` key
  that names no role of the scenario is 422 ``validation_error`` at it.
- ``resolve_personas`` (reads, writes nothing): ``persona_required`` for a saved persona's
  ``id`` that is a catalog character's id; an id that is neither a persona's nor a catalog
  character's is 422 ``validation_error`` at ``body.players.<role>.persona.id``. It answers the
  saved personas it found, by role, for the snapshot (``core.catalog.personas.snapshot``).

Neither check consults an owner or a relation (FR-AUTH-8): any principal's persona may be
played, and a persona's owner need not be the player. The player's persona is never held to the
role's eligibility filters (FR-SCN-3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.models import CharacterCard
from epistrel.core.catalog.personas import Persona, PersonaFields, persona_of
from epistrel.core.catalog.report import Entry, EntryLocation, Outcome, VersionLabel
from epistrel.core.catalog.scenario_models import (
    DOTTED_PATH_PATTERN,
    PATH_MAX,
    SLUG_PATTERN,
    CastLimit,
    FieldClass,
)
from epistrel.core.catalog.service import Revision
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import Ulid
from epistrel.core.ownership import Owner, Principal
from epistrel.core.profiles.impact import CAMEL_CASE, TwoStageRequest
from epistrel.core.providers import ProviderBindings
from epistrel.core.tables import character_catalog, personas
from epistrel.core.tables.characters import DEFLECTION_STYLE_MAX, TRUE_AGE_MAX, TRUE_AGE_MIN

PERSONA_REQUIRED = "persona_required"
"""A player's persona names a catalog character: a player plays a persona (FR-CAT-6)."""
PLAYER_ROLE_FIXED = "player_role_fixed"
"""A player put in a role the scenario did not make the player's, or the player's role filled
as a character slot (FR-CAT-6: the scenario fixes which role the player fills)."""

PERSONA_RULE = "a player plays a persona, never a catalog character (FR-CAT-6)"
ROLE_RULE = "the scenario fixes which role the player fills (FR-CAT-6)"
PLAYER = "player"
"""The role kind a player fills (``ScenarioRole.kind``)."""


class PersonaRef(BaseModel):
    """A saved persona, by id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Ulid = Field(description="The saved persona's id.")


class AdHocPersona(BaseModel):
    """A persona given for this story only: a persona's fields, saved nowhere."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ad_hoc: PersonaFields


class PlayerAssignment(BaseModel):
    """One player of a new story: the player's ``principal`` (recorded as a ``player`` relation
    on the story, #119) and the ``persona`` they play as, saved or ad hoc."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {"principal": "p-7f3a9c", "persona": {"id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"}},
                {
                    "principal": "p-7f3a9c",
                    "persona": {
                        "ad_hoc": {"name": "Sam", "age": 23, "description": "Quick to anger."}
                    },
                },
            ]
        },
    )

    principal: Principal = Field(description="The player's principal (opaque; never `system`).")
    persona: PersonaRef | AdHocPersona


def _rejected(
    code: str,
    scenario_id: str,
    version: VersionLabel,
    role: str,
    field: str,
    rule: str,
    *,
    source: str | None = None,
) -> Entry:
    return Entry(
        code=code,
        location=EntryLocation(entry=scenario_id, version=version, role=role, field=field),
        outcome=Outcome.rejected,
        source=source,
        rule=rule,
    )


def player_role_entries(
    scenario_id: str,
    version: VersionLabel,
    roles: Mapping[str, str],
    players: Iterable[str],
    slots: Iterable[str],
) -> list[Entry]:
    """The ``player_role_fixed`` entries of a creation that fills ``roles`` (role id → kind,
    ``npc`` or ``player``, in the scenario's order) with ``players`` and ``slots`` (the role ids
    each names): a player in an ``npc`` role, and a ``player`` role filled as a slot. 422
    ``validation_error`` for a key that names no role of the scenario."""
    named = list(players)
    filled = list(slots)
    for where, keys in (("players", named), ("slots", filled)):
        for key in keys:
            if key not in roles:
                raise InvalidRequest(
                    f"the scenario has no role {key!r}", {"field": f"body.{where}.{key}"}
                )
    entries: list[Entry] = []
    for role_id in named:
        if roles[role_id] != PLAYER:
            entries.append(
                _rejected(
                    PLAYER_ROLE_FIXED,
                    scenario_id,
                    version,
                    role_id,
                    f"players.{role_id}",
                    ROLE_RULE,
                )
            )
    for role_id in filled:
        if roles[role_id] == PLAYER:
            entries.append(
                _rejected(
                    PLAYER_ROLE_FIXED,
                    scenario_id,
                    version,
                    role_id,
                    f"slots.{role_id}",
                    ROLE_RULE,
                )
            )
    return entries


async def resolve_personas(
    conn: AsyncConnection,
    scenario_id: str,
    version: VersionLabel,
    players: Mapping[str, PlayerAssignment],
) -> tuple[dict[str, Persona], list[Entry]]:
    """The saved personas ``players`` name, by role, and a ``persona_required`` entry for each
    id that is a catalog character's; 422 ``validation_error`` at
    ``body.players.<role>.persona.id`` for an id that is neither. Ad-hoc personas need no
    lookup. Reads only, in ``conn``'s transaction."""
    wanted = {
        role_id: assignment.persona.id
        for role_id, assignment in players.items()
        if isinstance(assignment.persona, PersonaRef)
    }
    if not wanted:
        return {}, []
    ids = sorted(set(wanted.values()))
    saved = {
        str(row.id): row
        for row in await conn.execute(select(personas).where(personas.c.id.in_(ids)))
    }
    catalog = set(
        await conn.scalars(
            select(character_catalog.c.id).where(
                character_catalog.c.id.in_([i for i in ids if i not in saved])
            )
        )
    )
    found: dict[str, Persona] = {}
    entries: list[Entry] = []
    for role_id, persona_id in wanted.items():
        if persona_id in saved:
            found[role_id] = persona_of(saved[persona_id])
        elif persona_id in catalog:
            entries.append(
                _rejected(
                    PERSONA_REQUIRED,
                    scenario_id,
                    version,
                    role_id,
                    f"players.{role_id}.persona.id",
                    PERSONA_RULE,
                    source=persona_id,
                )
            )
        else:
            raise InvalidRequest(
                f"no persona has the id {persona_id}",
                {"field": f"body.players.{role_id}.persona.id"},
            )
    return found, entries


# -- the request (#119) ----------------------------------------------------------------------

RoleId = Annotated[str, Field(pattern=SLUG_PATTERN)]
OverridePath = Annotated[str, Field(max_length=PATH_MAX, pattern=DOTTED_PATH_PATTERN)]
NAME_MAX = 80
"""The longest ad-hoc character name (#50's ``CharacterSpec``)."""
DEFINITION_MAX = 20_000
STORY_NAME_MAX = 200
SAFE_FALLBACK_MAX = 500
Projection = Literal["card", "full"]


class ScenarioRef(BaseModel):
    """The scenario a story is created from: its id and, optionally, a published version
    (default: the latest published; any older published version may be named)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Ulid
    version: Revision | None = None


class AdHocSlotCharacter(BaseModel):
    """An ad-hoc character for an ``open`` or ``default`` role: #50's free-text shape (#114's
    ad-hoc shape) — its name, its definition, its required true age (FR-CAT-12) and optionally
    its safe fallback line and deflection style. Story-scoped: never a catalog entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
    definition: str = Field(min_length=1, max_length=DEFINITION_MAX)
    true_age: StrictInt = Field(ge=TRUE_AGE_MIN, le=TRUE_AGE_MAX)
    safe_fallback_text: str | None = Field(default=None, max_length=SAFE_FALLBACK_MAX)
    deflection_style: str | None = Field(
        default=None, min_length=1, max_length=DEFLECTION_STYLE_MAX
    )


class SlotChoice(BaseModel):
    """Who fills one NPC role: a catalog character (``catalog_id`` and optionally a published
    ``version``; default the latest published) or an ad-hoc one (``ad_hoc``), never both."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {"catalog_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3", "version": 2},
                {"ad_hoc": {"name": "Dale", "definition": "A nosy neighbour.", "true_age": 52}},
            ]
        },
    )

    catalog_id: Ulid | None = None
    version: Revision | None = None
    ad_hoc: AdHocSlotCharacter | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if (self.catalog_id is None) == (self.ad_hoc is None):
            raise ValueError("name a catalog character (catalog_id) or an ad_hoc one, not both")
        if self.version is not None and self.catalog_id is None:
            raise ValueError("a version names a catalog character's version (catalog_id)")
        return self


class StoryFromScenario(TwoStageRequest):
    """``POST /stories`` with a ``scenario`` (FR-SCN-6): the scenario (and version), who fills
    each NPC role (``slots``: a ``locked`` role and a ``default`` role kept as pre-filled need
    no entry; re-supplying a locked role's own ``character@version`` is accepted), each
    player's principal and persona (``players``, by the scenario's player role), the
    permitted ``overrides`` (dotted paths over ``story_settings_defaults``, and
    ``cast.max_characters_at_start``, each named by the scenario's
    ``creation_rules.overridable``), an optional content rating, engine profile and
    starting-cast limit, ``strict`` (refuse on any creation conflict, FR-SCN-7), the story's
    ``name`` (default: the scenario's) and ``owner``, the report's ``projection`` (``card``:
    counts; ``full``: every entry), and ``ignore_warnings`` (FR-PROF-7: create even when the
    profile leaves a recommended capability unmet). ``characters`` is #50's blank-story field
    and is refused beside a scenario."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "scenario": {"id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"},
                    "slots": {"mother": {"catalog_id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C4"}},
                    "players": {
                        "player": {
                            "principal": "p-7f3a9c",
                            "persona": {"id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"},
                        }
                    },
                    "overrides": {"story_settings_defaults.turn_concurrency": "queue"},
                }
            ]
        },
    )

    scenario: ScenarioRef
    slots: dict[RoleId, SlotChoice] = Field(default_factory=dict)
    players: dict[RoleId, PlayerAssignment] = Field(default_factory=dict)
    overrides: dict[OverridePath, Any] = Field(
        default_factory=dict,
        description="Dotted paths under `story_settings_defaults` (or "
        "`cast.max_characters_at_start`) the scenario's `creation_rules.overridable` names, "
        "each with its value (validated by the scenario's field model).",
    )
    content_rating_id: str | None = None
    engine_profile_id: str | None = None
    engine_profile_version: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)] | None = Field(
        default=None,
        description="Pin the story to this version of its engine profile (#181): it no longer "
        "follows the profile's edits until `PUT /stories/{id}/engine-profile` moves it. Left "
        "out, the story follows the profile's latest version. A version the profile does not "
        "have is 422 `validation_error`.",
    )
    max_characters_at_start: CastLimit | None = None
    strict: StrictBool = False
    ignore_warnings: StrictBool = Field(
        default=False,
        validation_alias=AliasChoices("ignore_warnings", CAMEL_CASE),
        description="Create even when the profile leaves a recommended capability unmet "
        "(FR-PROF-7); `ignoreWarnings` is the same flag for camelCase clients.",
    )
    name: str | None = Field(default=None, max_length=STORY_NAME_MAX)
    owner: Owner | None = Field(
        default=None,
        description="The story's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )
    projection: Projection = "card"
    provider_bindings: ProviderBindings = Field(
        default_factory=dict,
        description="The story's ids at its external-data providers (#204): "
        "`{provider: {external_ids}}`, each provider registered and each binding giving every "
        "`{binding.<key>}` its manifest uses (422 `binding_incomplete`).",
    )


def is_scenario_body(body: Any) -> bool:
    """Whether a ``POST /stories`` body is a creation from a scenario (it names ``scenario``)."""
    return isinstance(body, Mapping) and "scenario" in body


# -- the creation report (#119, #122) ---------------------------------------------------------


class ClassCapabilityCounts(BaseModel):
    """How many items fall under each field class and each capability (non-zero keys only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    by_class: dict[str, int] = Field(default_factory=dict)
    by_capability: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def of(cls, items: Iterable[DeferredItem | InactiveItem]) -> ClassCapabilityCounts:
        by_class: dict[str, int] = {}
        by_capability: dict[str, int] = {}
        for item in items:
            capability = (
                item.capability_needed if isinstance(item, DeferredItem) else item.capability
            )
            by_class[item.field_class.value] = by_class.get(item.field_class.value, 0) + 1
            by_capability[capability] = by_capability.get(capability, 0) + 1
        return cls(by_class=by_class, by_capability=by_capability)


MaterializedOutcome = Literal[
    "canon",
    "memory",
    "deferred",
    "merge_conflict",
    "entity",
    "rule",
    "belief",
    "nested",
    "belief_conflict",
    "visibility_conflict",
    "seed_audience_unfilled",
]
"""What structured genesis did with an item (#149): ``canon`` (facts written), ``memory`` (a
seed memory written), ``deferred`` (a part waits for a capability the story lacks),
``merge_conflict`` (a fact lost the merge and was not written), ``entity`` (a place, an entity
or a name override recorded), ``rule`` (a story rule registered). With partitions (#173):
``belief`` (first-order beliefs seeded), ``nested`` (nested beliefs seeded), ``belief_conflict``
(a belief lost to another of the same holder and layer), ``visibility_conflict`` (the same fact
from a higher source with another audience won) and ``seed_audience_unfilled`` (an audience
token named an unfilled role or the player's, and was dropped)."""


class OutcomeCounts(BaseModel):
    """How many ``materialized`` entries name each outcome (non-zero keys only; #149)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    by_outcome: dict[str, int] = Field(default_factory=dict)


class CreationCounts(BaseModel):
    """How many errors, merge ``conflicts`` (always 0 under Lite, which detects none: FR-SCN-7;
    a structured creation's ``merge_conflict`` entries, #149) and warnings the creation found,
    and what it deferred and left inactive, by field class and capability (#122). A structured
    creation adds ``materialized``: its entries by outcome (absent under Lite)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    errors: int = 0
    conflicts: int = 0
    warnings: int = 0
    deferred: ClassCapabilityCounts = Field(default_factory=ClassCapabilityCounts)
    inactive: ClassCapabilityCounts = Field(default_factory=ClassCapabilityCounts)
    materialized: OutcomeCounts | None = Field(default=None, exclude_if=lambda v: v is None)


class MaterializedEntry(BaseModel):
    """One item structured genesis materialized (#149): its dotted ``field_ref``, its location
    (#111's shape), what happened to it (``outcomes``, in that order) and, for an item that
    lost the fact merge, the ``winner`` and ``loser`` (seed ids, or ``<role>:<field>`` for a
    character's definition prose) and the ``rule`` that decided it (#140's)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_ref: str
    location: EntryLocation
    outcomes: list[MaterializedOutcome]
    winner: str | None = Field(default=None, exclude_if=lambda v: v is None)
    loser: str | None = Field(default=None, exclude_if=lambda v: v is None)
    rule: str | None = Field(default=None, exclude_if=lambda v: v is None)


class DeferredItem(BaseModel):
    """A scenario item the story cannot use yet (FR-SCN-11; a ``genesis_deferred`` row): the
    dotted ``field_ref`` with indices, its class, its location (#111's shape), the one
    capability it needs (a #106 module name) and its ``payload`` (``value``: the item's JSON as
    the scenario holds it; ``characters``: the story character ids genesis resolved, by role)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_ref: str
    field_class: FieldClass
    location: EntryLocation
    capability_needed: str
    payload: dict[str, Any]


class InactiveItem(BaseModel):
    """A setting the story keeps for a capability it lacks (``stories.settings.inactive``): its
    name, value, the capability it waits for, its class and location."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    value: Any
    capability: str
    field_class: FieldClass
    location: EntryLocation


class CastSource(BaseModel):
    """The catalog version a story character was created from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str
    version: int


class CatalogCastCard(CharacterCard):
    """A catalog character of the new story's cast: its public card (#115's, of the version the
    story took), the story character's id, its role and its source version."""

    kind: Literal["catalog"] = "catalog"
    character_id: str
    role_id: str
    source: CastSource


class MemberCastCard(BaseModel):
    """An ad-hoc character or the player of the new story's cast: its name, its kind, the story
    character's id and its role (nothing private: no definition, persona description or age)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ad_hoc", "player"]
    character_id: str
    role_id: str
    name: str


CastCard = Annotated[CatalogCastCard | MemberCastCard, Field(discriminator="kind")]


class CreationReport(BaseModel):
    """What a creation from a scenario found (FR-SCN-5,6,10,11): the new story's id (``null``
    when nothing was created) and its ``cast`` as cards, the ``counts``, ``unavailable`` (the
    checks the deployment could not run) and, under the ``full`` projection only (``null``
    under ``card``), every entry: #111's ``errors`` and ``warnings`` with their location and
    exact text, every ``deferred`` item with its location and the capability it needs, and
    every ``inactive`` setting; a structured creation also lists every item it
    ``materialized`` (#149: absent from a Lite report, and under ``card``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: str | None = None
    cast: list[CastCard] = Field(default_factory=list)
    counts: CreationCounts
    unavailable: list[str] = Field(default_factory=list)
    errors: list[Entry] | None = None
    warnings: list[Entry] | None = None
    deferred: list[DeferredItem] | None = None
    inactive: list[InactiveItem] | None = None
    materialized: list[MaterializedEntry] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )


__all__ = [
    "PERSONA_REQUIRED",
    "PLAYER_ROLE_FIXED",
    "AdHocPersona",
    "AdHocSlotCharacter",
    "CastCard",
    "CastSource",
    "CatalogCastCard",
    "ClassCapabilityCounts",
    "CreationCounts",
    "CreationReport",
    "DeferredItem",
    "InactiveItem",
    "MaterializedEntry",
    "MaterializedOutcome",
    "MemberCastCard",
    "OutcomeCounts",
    "PersonaRef",
    "PlayerAssignment",
    "Projection",
    "ScenarioRef",
    "SlotChoice",
    "StoryFromScenario",
    "is_scenario_body",
    "player_role_entries",
    "resolve_personas",
]
