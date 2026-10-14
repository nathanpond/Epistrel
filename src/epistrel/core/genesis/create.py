"""Creating a story from a scenario in one genesis operation (FR-SCN-6,9, FR-RATE-3,5,7,
FR-AUTH-5,6,8, FR-CAT-6,9,10; docs/02 §4.10; #119).

``create_from_scenario`` takes a ``StoryFromScenario`` request and either creates the story or
refuses with nothing written. It runs in three phases.

**1. Checks (reads only).** The scenario must exist (422 ``validation_error`` at
``body.scenario.id``), be published (``not_published``) and not archived (``entry_archived``);
its version is the one named (any published version; an unknown one is 422 at
``body.scenario.version``) or the latest. Then, building one report of #111's shape:

- **Players** (#114): ``player_role_entries`` and ``resolve_personas`` (``player_role_fixed``,
  ``persona_required``); a player role without an assignment is ``slot_required``.
- **Slots**, in the scenario's role order: a ``locked`` role keeps its character (another
  choice is ``locked_slot_reassigned``; its own ``character@version`` re-supplied verbatim is
  accepted), a ``default`` role keeps its character unless replaced, an ``open`` role takes
  the request's choice; a required role left empty is ``slot_required``, an optional one is
  left out (and not counted). A catalog choice names a character that exists (else 422
  ``validation_error`` at ``body.slots.<role>.catalog_id``), is not archived
  (``entry_archived``) and is published (``not_published``), at a version it has (else 422 at
  ``body.slots.<role>.version``; omitted: the latest published). A character the scenario
  itself names that is gone is ``not_published``.
- **Eligibility** (FR-SCN-3): #111's ``eligibility_failures`` for every catalog character
  (labels, content ratings, the age range: ``eligibility_failed``); an ad-hoc character is
  held to the age range only, and a label or rating filter on it is the warning
  ``eligibility_unverifiable``. The player's persona is never held to the filters.
- **Overrides**: each key is a dotted path under ``story_settings_defaults`` or
  ``cast.max_characters_at_start`` (another is 422 ``validation_error`` at
  ``body.overrides.<path>``) that the scenario's ``creation_rules.overridable`` names, exactly
  or by a prefix (else ``override_not_allowed``); the overridden document is validated by the
  scenario model (422 at the key).
- **The starting-cast limit** (FR-SCN-4): the filled roles, player roles included, against
  ``effective_cast_limit(scenario's, request's, deployment's)`` (``cast_limit_exceeded``).
- **Names**: the cast's names (a role's ``name_overrides.canonical`` over the catalog name)
  must be distinct ignoring case: 422 ``validation_error`` at the later slot.
- **The rating** (FR-RATE-3): the request's ``content_rating_id``, else the scenario's, else
  the deployment default; it must be assignable (422 ``rating_retired``). Every catalog
  character whose rating's guardrails lack a policy of the story's is the warning
  ``rating_looser`` (FR-RATE-5).
- **#111's report checks** re-run for what the story uses: the document check, the
  personality check (``personality_in_role`` warnings) and the checks this deployment cannot
  run, listed ``unavailable``.

The errors decide the refusal: one 422 whose code is the first of ``PRECEDENCE`` present
(``creation_conflicts`` when ``strict`` and any personality warning), with the whole report
(every entry) in ``details.report``.

**2. Model-touching checks** (outside any transaction, only once phase 1 found no error):
when the requested rating adds guardrails to the scenario's (FR-RATE-7, #118's
``classify``), the seeds, the world description and the opening narration are checked
against the requested rating's policies with #118's ``evaluate_history`` (``rating_check``,
the Engine's): a violation is ``rating_check_failed``, each listed by location and policy, and
an ``audit_log`` row (``guardrail_input_rejected``, entry point ``story_create``, policy ids
only). Then the input gate (``gate``, FR-GUARD-10) over every text the story takes in: the
personas' descriptions, the ad-hoc definitions and deflection styles, the catalog characters'
definitions and deflection styles, the seeds, the world description and the opening
narration; a violation is 422 ``guardrail_violation`` with its own audit row. Last, #109's
two stages: the story's engine profile (the request's, else the scenario's recommended one if
it can be assigned, else the deployment default, with the warning ``profile_unavailable``)
and ``creation_report``; when the profile leaves a recommended capability unmet and the
request did not set ``ignore_warnings``, the answer is ``CreationHeld`` and nothing is
written.

**3. Genesis (one transaction).** The ``stories`` row (owner: the request's, else the
requester; ``scenario_id``/``scenario_version`` and the version's whole document as
``scenario_snapshot``; the settings from ``story_settings_defaults`` with the overrides, the
default viewpoint a role id resolved to its character), one ``genesis`` operation at position
0 (``Operation.begin_in``: ``story_created`` with ``scenario {id, version}``, one
``character_defined`` per cast member with its role and catalog source), the ``characters``
rows (ordinal: role order; a catalog character's definition rendered from its snapshot by
``render_definition``, its safe fallback and deflection style copied; a player's persona
snapshot), one ``character_snapshots`` row per catalog character (its whole published
version), the scenario's policies as ``story`` policies (``write_policies``, the Engine's),
and a ``player`` relation per player principal (FR-AUTH-6). The opening narration is turn 1:
a ``turn`` operation recording one ``message_rendered`` beat (provenance ``system``, no
character, ``present_character_ids`` the characters of ``opening.roles_present``), stamped
with the guardrail set in force (#118). A scenario with no opening (a companion copied alone,
FR-CAT-10) has no turn 1. Under an idempotent call both operations bind the key (the first is
its operation). Any failure rolls the whole transaction back.

Nothing here consults an owner, a relation or a listing status (FR-AUTH-8).
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, clock, ownership
from epistrel.core.catalog import personas as catalog_personas
from epistrel.core.catalog.characters import version_card
from epistrel.core.catalog.checks import (
    document,
    fact_consistency,
    personality,
    seed_normalization,
    state_schema,
)
from epistrel.core.catalog.checks.base import Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import FilledRole
from epistrel.core.catalog.checks.character_documents import (
    name_of,
    rating_id_of,
    true_age_of,
)
from epistrel.core.catalog.checks.eligibility import eligibility_failures
from epistrel.core.catalog.models import CharacterCard
from epistrel.core.catalog.report import Entry, EntryLocation, Outcome, ValidationReport
from epistrel.core.catalog.scenario_models import (
    CastLimits,
    RoleCharacter,
    ScenarioDraft,
    ScenarioPolicy,
    ScenarioRole,
    effective_cast_limit,
)
from epistrel.core.catalog.validation import run as run_checks
from epistrel.core.config_changes import read_settings
from epistrel.core.enums import EntryStatus, OperationKind, Provenance
from epistrel.core.erasure.records import (
    character_record,
    deferred_record,
    index_rows,
    operation_record,
    snapshot_record,
    story_record,
)
from epistrel.core.errors import (
    CreationArchived,
    CreationCastLimit,
    CreationConflicts,
    CreationNotPublished,
    CreationRefused,
    EligibilityFailed,
    InvalidRequest,
    LockedSlotReassigned,
    OverrideNotAllowed,
    PersonaRequired,
    PlayerRoleFixed,
    ProfileDeleted,
    RatingCheckFailed,
    SlotRequired,
)
from epistrel.core.events.operation import Operation
from epistrel.core.events.types import (
    CastMember,
    CatalogSource,
    CharacterDefined,
    MessageRendered,
    ScenarioOrigin,
    StoryCreated,
)
from epistrel.core.genesis import merge, render
from epistrel.core.genesis.materialize import CastRole, Materialized, materialize
from epistrel.core.genesis.models import (
    PLAYER,
    AdHocSlotCharacter,
    CastCard,
    CastSource,
    CatalogCastCard,
    ClassCapabilityCounts,
    CreationCounts,
    CreationReport,
    MemberCastCard,
    PersonaRef,
    PlayerAssignment,
    StoryFromScenario,
    player_role_entries,
    resolve_personas,
)
from epistrel.core.ids import new_ulid
from epistrel.core.policies.model import Target
from epistrel.core.ports import Ports
from epistrel.core.profiles import effective
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles.fingerprint import RuntimeFacts, stamp
from epistrel.core.profiles.impact import ImpactReport, Recommendation, creation_report
from epistrel.core.profiles.schema import Purposes
from epistrel.core.ratings import load_graph
from epistrel.core.ratings.change import (
    Direction,
    HistoryRecord,
    HistoryResult,
    classify,
    live_guardrail_ids,
)
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, StorySettings
from epistrel.core.stories import (
    CharacterRef,
    CreatedStory,
    RatingRef,
    rating_ref,
    resolve_story_rating,
    split_settings,
)
from epistrel.core.tables import (
    character_catalog,
    character_snapshots,
    character_versions,
    characters,
    engine_profiles,
    genesis_deferred,
    operations,
    scenario_catalog,
    scenario_versions,
    stories,
)
from epistrel.core.tables.audit_log import AuditKind
from epistrel.core.tables.ownership import EntityType
from epistrel.core.turns import OPENING_FINISH
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

# -- codes -----------------------------------------------------------------------------------

SLOT_REQUIRED: Final = "slot_required"
LOCKED_SLOT_REASSIGNED: Final = "locked_slot_reassigned"
OVERRIDE_NOT_ALLOWED: Final = "override_not_allowed"
ELIGIBILITY_FAILED: Final = "eligibility_failed"
ELIGIBILITY_UNVERIFIABLE: Final = "eligibility_unverifiable"
CAST_LIMIT_EXCEEDED: Final = "cast_limit_exceeded"
RATING_CHECK_FAILED: Final = "rating_check_failed"
RATING_LOOSER: Final = "rating_looser"
ENTRY_ARCHIVED: Final = "entry_archived"
NOT_PUBLISHED: Final = "not_published"
CREATION_CONFLICTS: Final = "creation_conflicts"
PROFILE_UNAVAILABLE: Final = "profile_unavailable"
PERSONALITY_IN_ROLE: Final = "personality_in_role"

REFUSALS: Final[dict[str, type[CreationRefused]]] = {
    CREATION_CONFLICTS: CreationConflicts,
    SLOT_REQUIRED: SlotRequired,
    LOCKED_SLOT_REASSIGNED: LockedSlotReassigned,
    "player_role_fixed": PlayerRoleFixed,
    "persona_required": PersonaRequired,
    ENTRY_ARCHIVED: CreationArchived,
    NOT_PUBLISHED: CreationNotPublished,
    OVERRIDE_NOT_ALLOWED: OverrideNotAllowed,
    ELIGIBILITY_FAILED: EligibilityFailed,
    CAST_LIMIT_EXCEEDED: CreationCastLimit,
    RATING_CHECK_FAILED: RatingCheckFailed,
}
"""The refusal of each error code, in precedence order: several conditions at once answer the
first present (``guardrail_violation``, the input gate's own 422, comes after all of them)."""

PRECEDENCE: Final = tuple(REFUSALS)

ERROR_CODES: Final[dict[str, str]] = {
    "character_missing": NOT_PUBLISHED,
    "character_not_published": NOT_PUBLISHED,
    "character_archived": ENTRY_ARCHIVED,
    "true_age_missing": NOT_PUBLISHED,
}
"""Report codes whose refusal is another code's."""

OVERRIDE_ROOTS: Final = ("story_settings_defaults.",)
OVERRIDE_PATHS: Final = frozenset({"cast.max_characters_at_start"})
"""What a creation may override: a path under the story settings defaults, or the
starting-cast limit."""

INACTIVE_KEY: Final = "inactive"
"""Where a story's settings keep the settings for capabilities it lacks (#122):
``{name: {value, capability}}`` (``settings_map``)."""

ENTRY_POINT: Final = "story_create"
NARRATION_BEAT: Final = 1
UNKNOWN_NAME: Final = "Unnamed"

PERSONALITY_RULE: Final = "personality: character over scenario"
LOOSER_RULE: Final = (
    "every character plays under the story's rating; this one's rating lacks a guardrail the "
    "story's has (FR-RATE-3,5; kept)"
)
UNVERIFIABLE_RULE: Final = (
    "an ad-hoc character has no labels or rating to hold to the role's filter (FR-SCN-3)"
)

Gate = Callable[[Sequence[tuple[str, str]], str, Sequence[int]], Awaitable[None]]
"""The input gate over ``(locator, text)`` pairs, the story's rating and the cast's true ages
(the Engine's, which owns the Orchestrator): raises 422 ``guardrail_violation`` with its own
audit row."""

RatingCheck = Callable[[Sequence[HistoryRecord], str, Sequence[int]], Awaitable[HistoryResult]]
"""#118's ``evaluate_history`` over ``records`` against ``rating_id``'s applying policies with
the cast's true ages (the Engine's: its model check is the Orchestrator's)."""

PolicyWriter = Callable[[AsyncConnection, str, Sequence[ScenarioPolicy]], Awaitable[None]]
"""Write the scenario's policies as the story's own, inside the genesis transaction (the
Engine's, over ``orchestrator.policies.admin``: core never writes the policy store)."""


# -- results ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreationHeld:
    """A creation #109's two stages held back (FR-PROF-7): the profile leaves a capability the
    scenario recommends unmet and the request did not set ``ignore_warnings``. Nothing was
    written and the call's key is released."""

    impact: ImpactReport
    report: CreationReport
    applied: bool = False
    timeline_revision: int = 0
    replayed: bool = False


# -- the resolved cast -----------------------------------------------------------------------


@dataclass(slots=True)
class Member:
    """One filled role: who fills it and what the story keeps of them."""

    index: int
    role: ScenarioRole
    name: str
    true_age: int
    is_player: bool
    field: str
    """Where the request (or the scenario) names this member: ``slots.<role>``,
    ``players.<role>`` or the role's ``cast.roles[i].character``."""
    definition: str = ""
    safe_fallback_text: str | None = None
    deflection_style: str | None = None
    catalog_id: str | None = None
    version: Any | None = None
    """The catalog version row (``version``, ``content``, ``published_at``,
    ``content_rating_version_id``)."""
    rating_id: str | None = None
    persona: catalog_personas.PersonaSnapshot | None = None
    principal: str | None = None
    card: CharacterCard | None = None
    """A catalog character's public card, of the version the story takes (#122)."""
    id: str = dataclass_field(default_factory=new_ulid)

    @property
    def source(self) -> str | None:
        if self.catalog_id is None or self.version is None:
            return None
        return f"{self.catalog_id}@{int(self.version.version)}"


@dataclass(slots=True)
class Plan:
    """Everything phase 1 resolved, for the later phases."""

    request: StoryFromScenario
    scenario_id: str
    version: int
    content: dict[str, Any]
    draft: ScenarioDraft
    members: list[Member]
    entries: list[Entry]
    unavailable: list[str]
    rating_id: str
    scenario_rating_id: str | None
    settings: dict[str, Any]
    materialized: Materialized
    """What the creation defers and leaves inactive (#122)."""


def _entry(
    plan_or_ids: tuple[str, int],
    code: str,
    outcome: Outcome,
    *,
    field: str | None = None,
    role: str | None = None,
    text: str | None = None,
    source: str | None = None,
    rule: str | None = None,
) -> Entry:
    scenario_id, version = plan_or_ids
    return Entry(
        code=code,
        location=EntryLocation(entry=scenario_id, version=version, role=role, field=field),
        text=text,
        outcome=outcome,
        source=source,
        rule=rule,
    )


# -- rendering a catalog definition ----------------------------------------------------------


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def render_definition(name: str, content: Mapping[str, Any]) -> str:
    """The author notes a catalog character's snapshot gives the story's ``characters.definition``
    (what every Lite turn reads): its private definition as prose (``render.definition_notes``,
    #122), never its public card, labels, true age, visual identity or deflection style."""
    return render.definition_notes(name, content)


def _definition_field(content: Mapping[str, Any], key: str) -> str | None:
    found = content.get("definition")
    return _text(found.get(key)) if isinstance(found, Mapping) else None


# -- phase 1: the checks ---------------------------------------------------------------------


async def _scenario(conn: AsyncConnection, request: StoryFromScenario) -> tuple[int, Any]:
    scenario_id = request.scenario.id
    row = (
        await conn.execute(
            select(scenario_catalog.c.status, scenario_catalog.c.current_version).where(
                scenario_catalog.c.id == scenario_id
            )
        )
    ).one_or_none()
    if row is None:
        raise InvalidRequest(
            f"no scenario has the id {scenario_id}",
            {"field": "body.scenario.id", "reason": "unknown"},
        )
    if row.status == EntryStatus.ARCHIVED.value:
        raise CreationArchived(
            f"scenario {scenario_id} is archived; unarchive it to create a story from it",
            {"scenario_id": scenario_id, "field": "body.scenario.id"},
        )
    if row.current_version is None:
        raise CreationNotPublished(
            f"scenario {scenario_id} has no published version",
            {"scenario_id": scenario_id, "field": "body.scenario.id"},
        )
    version = request.scenario.version or int(row.current_version)
    found = (
        await conn.execute(
            select(scenario_versions.c.content).where(
                scenario_versions.c.catalog_id == scenario_id,
                scenario_versions.c.version == version,
            )
        )
    ).one_or_none()
    if found is None:
        raise InvalidRequest(
            f"scenario {scenario_id} has no version {version}",
            {"field": "body.scenario.version", "reason": "unknown"},
        )
    return version, found.content


@dataclass(slots=True)
class _Choice:
    """A catalog character a role is filled with, before it is looked up."""

    index: int
    role: ScenarioRole
    catalog_id: str
    version: int | None
    field: str
    requested: bool


async def _catalog_rows(
    conn: AsyncConnection, choices: Sequence[_Choice]
) -> tuple[dict[str, Any], dict[tuple[str, int], Any]]:
    ids = sorted({c.catalog_id for c in choices})
    if not ids:
        return {}, {}
    entries = {
        str(row.id): row
        for row in await conn.execute(
            select(
                character_catalog.c.id,
                character_catalog.c.status,
                character_catalog.c.current_version,
                character_catalog.c.listing_status,
            ).where(character_catalog.c.id.in_(ids))
        )
    }
    wanted: list[tuple[str, int]] = []
    for choice in choices:
        entry = entries.get(choice.catalog_id)
        number = choice.version
        if number is None and entry is not None and entry.current_version is not None:
            number = int(entry.current_version)
        if number is not None:
            wanted.append((choice.catalog_id, number))
    versions: dict[tuple[str, int], Any] = {}
    for catalog_id, number in sorted(set(wanted)):
        row = (
            await conn.execute(
                select(
                    character_versions.c.catalog_id,
                    character_versions.c.version,
                    character_versions.c.content,
                    character_versions.c.published_at,
                    character_versions.c.content_rating_version_id,
                ).where(
                    character_versions.c.catalog_id == catalog_id,
                    character_versions.c.version == number,
                )
            )
        ).one_or_none()
        if row is not None:
            versions[(catalog_id, number)] = row
    return entries, versions


def _verbatim(choice: RoleCharacter, request: Any) -> bool:
    """Whether a slot's choice re-supplies the role's own ``character@version``."""
    return (
        request.ad_hoc is None
        and request.catalog_id == choice.catalog_id
        and request.version == choice.version
    )


async def _rating_heads(conn: AsyncConnection, version_ids: Iterable[str]) -> dict[str, str]:
    from epistrel.core.tables import content_rating_versions

    wanted = sorted(set(version_ids))
    if not wanted:
        return {}
    rows = await conn.execute(
        select(content_rating_versions.c.version_id, content_rating_versions.c.rating_id).where(
            content_rating_versions.c.version_id.in_(wanted)
        )
    )
    return {str(r.version_id): str(r.rating_id) for r in rows}


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: dict[str, Any] = document
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _overridable(path: str, allowed: Iterable[str]) -> bool:
    return any(path == name or path.startswith(f"{name}.") for name in allowed)


def _apply_overrides(
    ids: tuple[str, int], content: dict[str, Any], request: StoryFromScenario, entries: list[Entry]
) -> ScenarioDraft:
    """The scenario document with the request's permitted overrides applied (validated by the
    scenario model); a refused one is an ``override_not_allowed`` entry."""
    draft = ScenarioDraft.model_validate(content)
    if not request.overrides:
        return draft
    allowed = draft.creation_rules.overridable
    changed = copy.deepcopy(content)
    for path, value in request.overrides.items():
        if not (path.startswith(OVERRIDE_ROOTS) or path in OVERRIDE_PATHS):
            raise InvalidRequest(
                "an override names a story setting default (story_settings_defaults.<name>) or "
                "the starting-cast limit (cast.max_characters_at_start)",
                {"field": f"body.overrides.{path}", "reason": "unknown"},
            )
        if not _overridable(path, allowed):
            entries.append(
                _entry(
                    ids,
                    OVERRIDE_NOT_ALLOWED,
                    Outcome.rejected,
                    field=path,
                    rule="a creation overrides only what creation_rules.overridable names",
                )
            )
            continue
        _set_path(changed, path, value)
        try:
            ScenarioDraft.model_validate(changed)
        except ValidationError as exc:
            raise InvalidRequest.from_errors(
                [{**e, "loc": ()} for e in exc.errors()], prefix=("body", "overrides", path)
            ) from None
    return ScenarioDraft.model_validate(changed)


def _players(
    ids: tuple[str, int],
    draft: ScenarioDraft,
    request: StoryFromScenario,
    saved: Mapping[str, catalog_personas.Persona],
    at: Any,
    entries: list[Entry],
) -> dict[str, Member]:
    found: dict[str, Member] = {}
    for index, role in enumerate(draft.cast.roles):
        if role.kind != PLAYER:
            continue
        assignment: PlayerAssignment | None = request.players.get(role.id)
        if assignment is None:
            if role.id not in request.slots:
                entries.append(
                    _entry(
                        ids,
                        SLOT_REQUIRED,
                        Outcome.rejected,
                        field=f"players.{role.id}",
                        role=role.id,
                        rule="the player's role needs its player and persona",
                    )
                )
            continue
        persona = assignment.persona
        if isinstance(persona, PersonaRef):
            held = saved.get(role.id)
            if held is None:  # a catalog character's id: resolve_personas reported it
                continue
            copy_ = catalog_personas.snapshot(held, at=at)
        else:
            copy_ = catalog_personas.snapshot(persona.ad_hoc, at=at)
        found[role.id] = Member(
            index=index,
            role=role,
            name=copy_.name,
            true_age=copy_.age,
            is_player=True,
            field=f"players.{role.id}",
            definition=copy_.description,
            persona=copy_,
            principal=assignment.principal,
        )
    return found


def _ad_hoc_member(
    ids: tuple[str, int],
    index: int,
    role: ScenarioRole,
    chosen: AdHocSlotCharacter,
    entries: list[Entry],
) -> Member:
    where = f"slots.{role.id}"
    eligibility = role.eligibility
    if eligibility is not None:
        if eligibility.age_range is not None:
            youngest, oldest = eligibility.age_range
            age = chosen.true_age
            if (youngest is not None and age < youngest) or (oldest is not None and age > oldest):
                entries.append(
                    _entry(
                        ids,
                        ELIGIBILITY_FAILED,
                        Outcome.rejected,
                        field=f"{where}.ad_hoc.true_age",
                        role=role.id,
                        rule="eligibility: the character's true age is within the role's age "
                        "range (inclusive)",
                    )
                )
        if eligibility.labels or eligibility.content_ratings:
            entries.append(
                _entry(
                    ids,
                    ELIGIBILITY_UNVERIFIABLE,
                    Outcome.kept,
                    field=f"{where}.ad_hoc",
                    role=role.id,
                    rule=UNVERIFIABLE_RULE,
                )
            )
    return Member(
        index=index,
        role=role,
        name=chosen.name,
        true_age=chosen.true_age,
        is_player=False,
        field=where,
        definition=chosen.definition,
        safe_fallback_text=chosen.safe_fallback_text,
        deflection_style=chosen.deflection_style,
    )


def _choices(
    ids: tuple[str, int],
    draft: ScenarioDraft,
    request: StoryFromScenario,
    entries: list[Entry],
) -> tuple[list[_Choice], dict[int, Member]]:
    """Each NPC role's catalog choice (to look up) or ad-hoc member, and the slot entries."""
    choices: list[_Choice] = []
    ad_hoc: dict[int, Member] = {}
    for index, role in enumerate(draft.cast.roles):
        if role.kind == PLAYER:
            continue
        chosen = request.slots.get(role.id)
        where = f"slots.{role.id}"
        if role.fill == "locked" and role.character is not None:
            if chosen is not None and not _verbatim(role.character, chosen):
                entries.append(
                    _entry(
                        ids,
                        LOCKED_SLOT_REASSIGNED,
                        Outcome.rejected,
                        field=where,
                        role=role.id,
                        source=role.character.catalog_id,
                        rule="a locked role keeps the character the scenario fixes (FR-SCN-3)",
                    )
                )
                continue
            choices.append(
                _Choice(
                    index,
                    role,
                    role.character.catalog_id,
                    role.character.version,
                    f"cast.roles[{index}].character",
                    requested=False,
                )
            )
            continue
        if chosen is None:
            if role.fill == "default" and role.character is not None:
                choices.append(
                    _Choice(
                        index,
                        role,
                        role.character.catalog_id,
                        role.character.version,
                        f"cast.roles[{index}].character",
                        requested=False,
                    )
                )
            elif role.required:
                entries.append(
                    _entry(
                        ids,
                        SLOT_REQUIRED,
                        Outcome.rejected,
                        field=where,
                        role=role.id,
                        rule="a required role is filled",
                    )
                )
            continue
        if chosen.ad_hoc is not None:
            ad_hoc[index] = _ad_hoc_member(ids, index, role, chosen.ad_hoc, entries)
            continue
        assert chosen.catalog_id is not None  # noqa: S101 - SlotChoice names one kind
        choices.append(
            _Choice(index, role, chosen.catalog_id, chosen.version, where, requested=True)
        )
    return choices, ad_hoc


async def _catalog_members(
    conn: AsyncConnection,
    subject: Subject,
    ids: tuple[str, int],
    choices: Sequence[_Choice],
    entries: list[Entry],
) -> dict[int, Member]:
    rows, versions = await _catalog_rows(conn, choices)
    heads = await _rating_heads(
        conn,
        (
            str(v.content_rating_version_id)
            for v in versions.values()
            if v.content_rating_version_id is not None
        ),
    )
    found: dict[int, Member] = {}
    for choice in choices:
        role = choice.role
        entry = rows.get(choice.catalog_id)
        if entry is None:
            if choice.requested:
                raise InvalidRequest(
                    f"no catalog character has the id {choice.catalog_id}",
                    {"field": f"body.{choice.field}.catalog_id", "reason": "unknown"},
                )
            entries.append(
                _entry(
                    ids,
                    "character_missing",
                    Outcome.rejected,
                    field=choice.field,
                    role=role.id,
                    source=choice.catalog_id,
                    rule="the role's catalog character exists",
                )
            )
            continue
        if entry.status == EntryStatus.ARCHIVED.value:
            entries.append(
                _entry(
                    ids,
                    ENTRY_ARCHIVED,
                    Outcome.rejected,
                    field=choice.field,
                    role=role.id,
                    source=choice.catalog_id,
                    rule="an archived catalog character cannot be used to create a story",
                )
            )
            continue
        if entry.current_version is None:
            entries.append(
                _entry(
                    ids,
                    NOT_PUBLISHED,
                    Outcome.rejected,
                    field=choice.field,
                    role=role.id,
                    source=choice.catalog_id,
                    rule="a story is created from published versions only",
                )
            )
            continue
        number = choice.version if choice.version is not None else int(entry.current_version)
        version = versions.get((choice.catalog_id, number))
        if version is None:
            if choice.requested:
                raise InvalidRequest(
                    f"catalog character {choice.catalog_id} has no version {number}",
                    {"field": f"body.{choice.field}.version", "reason": "unknown"},
                )
            entries.append(
                _entry(
                    ids,
                    "character_missing",
                    Outcome.rejected,
                    field=choice.field,
                    role=role.id,
                    source=f"{choice.catalog_id}@{number}",
                    rule="the role's pinned character version exists",
                )
            )
            continue
        content: Mapping[str, Any] = version.content if isinstance(version.content, Mapping) else {}
        stored = version.content_rating_version_id
        rating_id = heads.get(str(stored)) if stored is not None else None
        rating_id = rating_id or rating_id_of(content)
        age = true_age_of(content)
        if age is None:
            entries.append(
                _entry(
                    ids,
                    "true_age_missing",
                    Outcome.rejected,
                    field=choice.field,
                    role=role.id,
                    source=f"{choice.catalog_id}@{number}",
                    rule="every character has a true age (FR-CAT-12)",
                )
            )
            continue
        if role.eligibility is not None:
            member = FilledRole(
                index=choice.index,
                role=role,
                catalog_id=choice.catalog_id,
                pinned=choice.version,
                entry=entry,
                version=version,
                rating_version_id=None if stored is None else str(stored),
                rating_id=rating_id,
            )
            entries.extend(eligibility_failures(subject, member, role.eligibility))
        overrides = role.name_overrides
        name = (overrides.canonical if overrides is not None else None) or name_of(content)
        name = name or UNKNOWN_NAME
        found[choice.index] = Member(
            index=choice.index,
            role=role,
            name=name,
            true_age=age,
            is_player=False,
            field=choice.field,
            definition=render_definition(name, content),
            safe_fallback_text=_definition_field(content, "safe_fallback_text"),
            deflection_style=_definition_field(content, "deflection_style"),
            catalog_id=choice.catalog_id,
            version=version,
            rating_id=rating_id,
            card=await version_card(conn, entry, version),
        )
    return found


def _unique_names(members: Sequence[Member]) -> None:
    seen: dict[str, Member] = {}
    for member in members:
        key = member.name.strip().casefold()
        if key in seen:
            raise InvalidRequest(
                f"two characters of the cast are named {member.name!r}",
                {"field": f"body.{member.field}", "reason": "duplicate"},
            )
        seen[key] = member


def _settings(
    draft: ScenarioDraft, members: Sequence[Member], materialized: Materialized
) -> dict[str, Any]:
    """The story's settings: the defaults Lite uses applied, every other one kept under
    ``inactive`` with the capability it waits for (#122, ``settings_map``)."""
    defaults = draft.story_settings_defaults
    values: dict[str, Any] = {}
    if defaults.turn_concurrency is not None:
        values["turn_concurrency"] = defaults.turn_concurrency
    if defaults.audit_retention_days is not None:
        values["audit_retention_days"] = defaults.audit_retention_days
    inactive = materialized.settings()
    if inactive:
        values[INACTIVE_KEY] = inactive
    viewpoint: str | None = None
    by_role = {m.role.id: m for m in members}
    if (
        defaults.default_output_viewpoint is not None
        and defaults.default_output_viewpoint in by_role
    ):
        viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{by_role[defaults.default_output_viewpoint].id}"
    else:
        players = [m for m in members if m.is_player]
        if len(players) == 1:
            viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{players[0].id}"
    values["default_output_viewpoint"] = viewpoint
    found: dict[str, Any] = StorySettings.model_validate(values).model_dump(mode="json")
    return found


async def _looser(
    conn: AsyncConnection, ids: tuple[str, int], rating_id: str, members: Sequence[Member]
) -> list[Entry]:
    graph = await load_graph(conn)
    story = await live_guardrail_ids(conn, rating_id, graph)
    found: list[Entry] = []
    for member in members:
        if member.rating_id is None or member.rating_id == rating_id:
            continue
        if member.rating_id not in graph:
            continue
        own = await live_guardrail_ids(conn, member.rating_id, graph)
        if not story <= own:
            found.append(
                _entry(
                    ids,
                    RATING_LOOSER,
                    Outcome.kept,
                    field=member.field,
                    role=member.role.id,
                    source=member.source,
                    rule=LOOSER_RULE,
                )
            )
    return found


async def plan(db: AsyncEngine, request: StoryFromScenario, *, ports: Ports | None = None) -> Plan:
    """Phase 1 (see the module docstring): every check that reads only, in one snapshot.
    Raises the refusal when the report has an error."""
    entries: list[Entry] = []
    async with db.connect() as conn:
        conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            version, stored = await _scenario(conn, request)
            content: dict[str, Any] = dict(stored)
            scenario_id = request.scenario.id
            ids = (scenario_id, version)
            original = ScenarioDraft.model_validate(content)
            roles = {role.id: role.kind for role in original.cast.roles}
            entries.extend(
                player_role_entries(
                    scenario_id, version, roles, request.players.keys(), request.slots.keys()
                )
            )
            saved, persona_entries = await resolve_personas(
                conn, scenario_id, version, request.players
            )
            entries.extend(persona_entries)
            draft = _apply_overrides(ids, content, request, entries)
            subject = Subject(
                entity_type=EntityType.SCENARIO,
                entry_id=scenario_id,
                version=version,
                document=content,
                model=ScenarioDraft,
            )
            filled = {k: v for k, v in request.slots.items() if roles.get(k) != PLAYER}
            narrowed = request.model_copy(update={"slots": filled})
            choices, ad_hoc = _choices(ids, draft, narrowed, entries)
            catalog = await _catalog_members(conn, subject, ids, choices, entries)
            players = _players(ids, draft, request, saved, clock.now(), entries)
            members = sorted(
                [*catalog.values(), *ad_hoc.values(), *players.values()], key=lambda m: m.index
            )
            deployment = (await read_settings(conn)).as_settings()
            limit = effective_cast_limit(
                draft.cast.max_characters_at_start,
                request.max_characters_at_start,
                CastLimits.of(deployment),
            )
            filled_slots = _filled_slot_count(draft, request, members)
            if filled_slots > limit:
                entries.append(
                    _entry(
                        ids,
                        CAST_LIMIT_EXCEEDED,
                        Outcome.rejected,
                        field="cast.max_characters_at_start",
                        rule=f"at most {limit} character slots, player slots included, at the "
                        f"story's start (FR-SCN-4); this creation fills {filled_slots}",
                    )
                )
            if not any(e.error for e in entries):
                _unique_names(members)
            scenario_rating = original.metadata.content_rating_id
            rating_id = await resolve_story_rating(
                conn, request.content_rating_id or scenario_rating
            )
            entries.extend(await _looser(conn, ids, rating_id, members))
            report = await run_checks(
                subject,
                ValidationContext(conn=conn, settings=deployment, ports=ports or Ports()),
                checks=(
                    document.CHECK,
                    personality.CHECK,
                    seed_normalization.CHECK,
                    fact_consistency.CHECK,
                    state_schema.CHECK,
                ),
            )
            entries.extend(report.errors)
            entries.extend(
                merge.name_warnings(report.warnings, {m.role.id: m.name for m in members})
            )
            materialized = materialize(
                scenario_id,
                version,
                content,
                draft,
                [
                    CastRole(
                        role_id=m.role.id,
                        character_id=m.id,
                        is_player=m.is_player,
                        persona=None if m.persona is None else m.persona.model_dump(mode="json"),
                    )
                    for m in members
                ],
            )
            settings = _settings(draft, members, materialized)
    found = Plan(
        request=request,
        scenario_id=scenario_id,
        version=version,
        content=content,
        draft=draft,
        members=members,
        entries=entries,
        unavailable=list(report.unavailable),
        rating_id=rating_id,
        scenario_rating_id=scenario_rating,
        settings=settings,
        materialized=materialized,
    )
    refuse(found)
    return found


def _filled_slot_count(
    draft: ScenarioDraft, request: StoryFromScenario, members: Sequence[Member]
) -> int:
    """The character slots the creation fills, player slots included: every role that has a
    member, plus every role the request fills that a refusal kept out of ``members``."""
    filled = {m.index for m in members}
    for index, role in enumerate(draft.cast.roles):
        named = role.id in request.slots or role.id in request.players
        kept = role.fill in ("locked", "default") and role.character is not None
        if named or (kept and role.kind != PLAYER):
            filled.add(index)
    return len(filled)


# -- the report and the refusal --------------------------------------------------------------


def _card(member: Member) -> CastCard:
    if member.card is not None and member.catalog_id is not None and member.version is not None:
        return CatalogCastCard(
            **member.card.model_dump(),
            character_id=member.id,
            role_id=member.role.id,
            source=CastSource(catalog_id=member.catalog_id, version=int(member.version.version)),
        )
    return MemberCastCard(
        kind="player" if member.is_player else "ad_hoc",
        character_id=member.id,
        role_id=member.role.id,
        name=member.name,
    )


def creation_report_of(
    found: Plan, *, full: bool | None = None, story_id: str | None = None
) -> CreationReport:
    """The creation report (FR-SCN-10): the story, its cast as cards, the counts (errors, merge
    conflicts, warnings, deferred and inactive by class and capability) and the unavailable
    checks; under ``full`` (a refusal's always) every entry, deferred item and inactive
    setting."""
    report = ValidationReport.of(found.entries, found.unavailable)
    show = found.request.projection == "full" if full is None else full
    deferred = list(found.materialized.deferred)
    inactive = list(found.materialized.inactive)
    return CreationReport(
        story_id=story_id,
        cast=[_card(m) for m in found.members],
        counts=CreationCounts(
            errors=len(report.errors),
            conflicts=merge.LITE_CONFLICTS,
            warnings=len(report.warnings),
            deferred=ClassCapabilityCounts.of(deferred),
            inactive=ClassCapabilityCounts.of(inactive),
        ),
        unavailable=list(report.unavailable),
        errors=list(report.errors) if show else None,
        warnings=list(report.warnings) if show else None,
        deferred=deferred if show else None,
        inactive=inactive if show else None,
    )


def _refusal_code(found: Plan) -> str | None:
    present = {ERROR_CODES.get(e.code, e.code) for e in found.entries if e.error}
    if found.request.strict and any(e.code == PERSONALITY_IN_ROLE for e in found.entries):
        present.add(CREATION_CONFLICTS)
    for code in PRECEDENCE:
        if code in present:
            return code
    if present:  # a #111 error with no refusal of its own (the document check's)
        return NOT_PUBLISHED
    return None


def refuse(found: Plan) -> None:
    """Raise the creation's refusal when its report has an error (or, under ``strict``, a
    conflict): the first code by ``PRECEDENCE``, with the full report."""
    code = _refusal_code(found)
    if code is None:
        return
    report = creation_report_of(found, full=True)
    log.info(
        "story creation refused",
        scenario_id=found.scenario_id,
        version=found.version,
        code=code,
        errors=[e.code for e in report.errors or ()],
    )
    raise REFUSALS[code](
        "the story cannot be created from this scenario as requested; nothing was written",
        {
            "scenario_id": found.scenario_id,
            "version": found.version,
            "report": report.model_dump(mode="json"),
        },
    )


# -- phase 2: the model-touching checks ------------------------------------------------------


def genesis_texts(found: Plan) -> list[tuple[str, str]]:
    """Every text the story takes in, with its locator (#111's ``field`` shape)."""
    texts: list[tuple[str, str]] = []
    for member in found.members:
        where = member.field
        if member.persona is not None:
            texts.append((f"{where}.persona.description", member.persona.description))
            continue
        texts.append((f"{where}.definition", member.definition))
        if member.deflection_style:
            texts.append((f"{where}.deflection_style", member.deflection_style))
    texts.extend(_story_texts(found.draft))
    return texts


def _story_texts(draft: ScenarioDraft) -> list[tuple[str, str]]:
    """The seeds, the world description and the opening narration, with their fields."""
    found: list[tuple[str, str]] = []
    if draft.world.description:
        found.append(("world.description", draft.world.description))
    for loc, seed in draft.seeds():
        found.append((".".join(_dotted(loc)) + ".text", seed.text))
    narration = draft.starting_state.opening.narration
    if narration:
        found.append(("starting_state.opening.narration", narration))
    return found


def _dotted(loc: Sequence[str | int]) -> list[str]:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int) and parts:
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(str(part))
    return parts


async def check_rating(
    db: AsyncEngine, found: Plan, rating_check: RatingCheck, *, principal: str
) -> None:
    """The creation-time tightening check (FR-RATE-7): only when the request names a rating
    that adds guardrails to the scenario's."""
    requested = found.request.content_rating_id
    if requested is None or requested == found.scenario_rating_id:
        return
    async with db.connect() as conn:
        graph = await load_graph(conn)
        scenario = (
            frozenset()
            if found.scenario_rating_id is None or found.scenario_rating_id not in graph
            else await live_guardrail_ids(conn, found.scenario_rating_id, graph)
        )
        target = await live_guardrail_ids(conn, requested, graph)
    if classify(scenario, target) is Direction.RELAX:
        return
    records = [
        HistoryRecord(
            key=f"genesis:{locator}",
            position={"kind": "seed", "id": locator},
            target=Target.TEXT_OUTPUT,
            text=text,
        )
        for locator, text in _story_texts(found.draft)
    ]
    result = await rating_check(records, requested, [m.true_age for m in found.members])
    if result.passed:
        return
    ids = (found.scenario_id, found.version)
    for violation in result.violations:
        found.entries.append(
            _entry(
                ids,
                RATING_CHECK_FAILED,
                Outcome.rejected,
                field=str(violation.position.get("id")),
                source=violation.policy_id,
                rule=violation.policy_name,
            )
        )
    await audit.record(
        db,
        AuditKind.GUARDRAIL_INPUT_REJECTED,
        principal=principal,
        policy_ids=sorted({v.policy_id for v in result.violations if v.policy_id}),
        details={
            "entry_point": ENTRY_POINT,
            "check": "rating",
            "violations": len(result.violations),
        },
    )
    refuse(found)


async def choose_profile(
    db: AsyncEngine, found: Plan
) -> tuple[str, int, dict[str, Any], list[Entry]]:
    """The story's engine profile (see the module docstring): its id, latest version and
    settings document, and the ``profile_unavailable`` warning when the scenario's recommended
    profile cannot be assigned."""
    warnings: list[Entry] = []
    chosen: effective.AssignableProfile | None = None
    async with db.connect() as conn, conn.begin():
        requested = found.request.engine_profile_id
        if requested is not None:
            exists = await conn.scalar(
                select(engine_profiles.c.id).where(engine_profiles.c.id == requested)
            )
            if exists is None:
                raise InvalidRequest(
                    f"no engine profile has the id {requested}",
                    {"field": "body.engine_profile_id", "reason": "unknown"},
                )
            chosen = await effective.assignable(conn, requested, field="body.engine_profile_id")
        else:
            recommended = found.draft.recommendation.engine_profile_id
            if recommended is not None:
                try:
                    chosen = await effective.assignable(conn, recommended)
                except ProfileDeleted:
                    warnings.append(
                        _entry(
                            (found.scenario_id, found.version),
                            PROFILE_UNAVAILABLE,
                            Outcome.ignored,
                            field="recommendation.engine_profile_id",
                            source=recommended,
                            rule="a recommended profile that cannot be assigned falls back "
                            "to the deployment default",
                        )
                    )
            if chosen is None:
                chosen = await effective.assignable(conn, await profiles.default_profile(conn))
    assert chosen is not None  # noqa: S101 - one of the branches above chose it
    return chosen.profile_id, chosen.version, dict(chosen.document), warnings


async def impact_of(
    db: AsyncEngine,
    found: Plan,
    profile_id: str,
    version: int,
    document: Mapping[str, Any],
    facts: RuntimeFacts,
    purposes: Purposes | None = None,
) -> ImpactReport:
    """#109's creation report: the new story's stamp and the recommendations it leaves unmet."""
    async with db.connect() as conn:
        defaults = await effective.profile_defaults(conn)
    settings = effective.merged_settings(defaults, document)
    del purposes
    recommendation = found.draft.recommendation
    return creation_report(
        stamp(profile_id=profile_id, version=version, settings=settings, facts=facts),
        settings,
        Recommendation(
            capabilities=tuple(recommendation.capabilities),
            recommended_by=f"{found.scenario_id}@{found.version}",
        )
        if recommendation.capabilities
        else None,
    )


# -- phase 3: genesis ------------------------------------------------------------------------


async def _locked_catalog(conn: AsyncConnection, found: Plan) -> None:
    """Under share locks: the scenario and every chosen catalog character still usable (an
    archive since phase 1 is refused, as it would have been)."""
    status = await conn.scalar(
        select(scenario_catalog.c.status)
        .where(scenario_catalog.c.id == found.scenario_id)
        .with_for_update(read=True)
    )
    if status == EntryStatus.ARCHIVED.value:
        raise CreationArchived(
            f"scenario {found.scenario_id} is archived; unarchive it to create a story from it",
            {"scenario_id": found.scenario_id, "field": "body.scenario.id"},
        )
    ids = sorted({m.catalog_id for m in found.members if m.catalog_id is not None})
    if not ids:
        return
    rows = await conn.execute(
        select(character_catalog.c.id, character_catalog.c.status)
        .where(character_catalog.c.id.in_(ids))
        .with_for_update(read=True)
    )
    for row in rows:
        if row.status == EntryStatus.ARCHIVED.value:
            raise CreationArchived(
                f"catalog character {row.id} is archived",
                {"catalog_id": str(row.id), "scenario_id": found.scenario_id},
            )


async def insert_cast(conn: AsyncConnection, story_id: str, found: Plan) -> None:
    """The ``characters`` rows and the catalog characters' ``character_snapshots`` (one
    multi-row ``VALUES``, so ``age_recorded_at`` is the transaction's time)."""
    rows: list[dict[str, Any]] = []
    for ordinal, member in enumerate(found.members):
        row: dict[str, Any] = {
            "id": member.id,
            "story_id": story_id,
            "ordinal": ordinal,
            "name": member.name,
            "definition": member.definition,
            "true_age": member.true_age,
            "age_recorded_at": func.transaction_timestamp(),
            "is_player": member.is_player,
            "safe_fallback_text": member.safe_fallback_text,
            "deflection_style": member.deflection_style,
            "role_id": member.role.id,
            "source_catalog_id": member.catalog_id,
            "source_catalog_version": None
            if member.version is None
            else int(member.version.version),
            "persona_id": None,
            "persona_snapshot": null(),
        }
        if member.persona is not None:
            row.update(catalog_personas.player_character(member.persona))
            row["name"] = member.name
        rows.append(row)
    await conn.execute(insert(characters).values(rows))
    snapshots = [
        {
            "story_id": story_id,
            "character_id": member.id,
            "source_catalog_id": member.catalog_id,
            "version": int(member.version.version),
            "content": member.version.content,
            "published_at": member.version.published_at,
            "content_rating_version_id": member.version.content_rating_version_id,
        }
        for member in found.members
        if member.catalog_id is not None and member.version is not None
    ]
    if snapshots:
        await conn.execute(insert(character_snapshots), snapshots)


async def insert_deferred(
    conn: AsyncConnection, story_id: str, operation_id: str, found: Plan
) -> None:
    """One ``genesis_deferred`` row per item the story cannot use yet (#122)."""
    rows = [
        {
            "id": new_ulid(),
            "story_id": story_id,
            "operation_id": operation_id,
            "field_ref": item.field_ref,
            "field_class": item.field_class.value,
            "location": item.location.model_dump(mode="json"),
            "capability_needed": item.capability_needed,
            "payload": item.payload,
        }
        for item in found.materialized.deferred
    ]
    if rows:
        await conn.execute(insert(genesis_deferred), rows)


async def _index_genesis(conn: AsyncConnection, story_id: str, operation_id: str) -> None:
    """The subject-reference rows of what genesis wrote beside its events (FR-ADMIN-6; #123):
    the story row with its scenario snapshot, the cast, the catalog characters' snapshots, the
    deferred items and the creation report."""
    await index_rows(conn, stories, stories.c.id == story_id, story_record)
    await index_rows(conn, characters, characters.c.story_id == story_id, character_record)
    await index_rows(
        conn, character_snapshots, character_snapshots.c.story_id == story_id, snapshot_record
    )
    await index_rows(
        conn, genesis_deferred, genesis_deferred.c.story_id == story_id, deferred_record
    )
    await index_rows(conn, operations, operations.c.operation_id == operation_id, operation_record)


async def genesis(
    db: AsyncEngine,
    found: Plan,
    *,
    principal: str,
    profile: tuple[str, int],
    write_policies: PolicyWriter,
    story_id: str | None = None,
) -> tuple[str, int, RatingRef]:
    """Phase 3 (see the module docstring): the story id, its revision and its rating."""
    story_id = story_id or new_ulid()
    request = found.request
    document, columns = split_settings(found.settings)
    name = request.name or found.draft.public_card.name
    present = set(found.draft.starting_state.opening.roles_present)
    narration = found.draft.starting_state.opening.narration
    started: list[Operation] = []
    error: BaseException | None = None
    try:
        async with db.begin() as conn:
            await _locked_catalog(conn, found)
            rating = await rating_ref(conn, await resolve_story_rating(conn, found.rating_id))
            await profiles.require_assignable(conn, profile[0], field="body.engine_profile_id")
            await conn.execute(
                insert(stories).values(
                    id=story_id,
                    name=name,
                    owner_principal=request.owner or principal,
                    settings=document,
                    content_rating_id=rating.id,
                    engine_profile_id=profile[0],
                    scenario_id=found.scenario_id,
                    scenario_version=found.version,
                    scenario_snapshot=found.content,
                    **columns,
                )
            )
            op = Operation(
                db,
                story_id=story_id,
                kind=OperationKind.GENESIS,
                principal=principal,
                provenance=Provenance.CONSUMER,
            )
            await op.begin_in(conn)
            started.append(op)
            await insert_cast(conn, story_id, found)
            await write_policies(conn, story_id, found.draft.story_settings_defaults.policies)
            for player in sorted({m.principal for m in found.members if m.principal}):
                await ownership.relate_in(
                    conn, EntityType.STORY, story_id, player, PLAYER, principal=principal
                )
            op.append(
                "story_created",
                StoryCreated(
                    name=name,
                    characters=[
                        CastMember(id=m.id, name=m.name, is_player=m.is_player)
                        for m in found.members
                    ],
                    settings=found.settings,
                    content_rating_id=rating.id,
                    rating_version_id=rating.version_id,
                    scenario=ScenarioOrigin(id=found.scenario_id, version=found.version),
                ),
            )
            for member in found.members:
                op.append(
                    "character_defined",
                    CharacterDefined(
                        character_id=member.id,
                        name=member.name,
                        is_player=member.is_player,
                        role_id=member.role.id,
                        source=None
                        if member.catalog_id is None or member.version is None
                        else CatalogSource(
                            catalog_id=member.catalog_id, version=int(member.version.version)
                        ),
                    ),
                )
            await insert_deferred(conn, story_id, op.operation_id, found)
            await op.write_in(conn)
            await conn.execute(
                update(operations)
                .where(operations.c.operation_id == op.operation_id)
                .values(
                    turn_report={
                        "creation_report": creation_report_of(
                            found, full=True, story_id=story_id
                        ).model_dump(mode="json")
                    }
                )
            )
            await _index_genesis(conn, story_id, op.operation_id)
            revision = 1
            if narration:
                turn = Operation(
                    db,
                    story_id=story_id,
                    kind=OperationKind.TURN,
                    principal=principal,
                    provenance=Provenance.SYSTEM,
                    origin="ingested",
                )
                await turn.begin_in(conn)
                started.append(turn)
                turn.append(
                    "message_rendered",
                    MessageRendered(
                        text=narration,
                        finish_reason=OPENING_FINISH,
                        present_character_ids=tuple(
                            m.id for m in found.members if m.role.id in present
                        ),
                    ),
                    beat=NARRATION_BEAT,
                )
                await turn.write_in(conn)
                revision = 2
    except BaseException as exc:
        error = exc
        raise
    finally:
        for begun in started:
            await begun.settle(error)
    return story_id, revision, rating


async def create_from_scenario(
    db: AsyncEngine,
    request: StoryFromScenario,
    *,
    principal: str,
    gate: Gate,
    rating_check: RatingCheck,
    write_policies: PolicyWriter,
    facts: RuntimeFacts,
    ports: Ports | None = None,
    story_id: str | None = None,
) -> CreatedStory | CreationHeld:
    """Create a story from ``request`` (see the module docstring)."""
    found = await plan(db, request, ports=ports)
    await check_rating(db, found, rating_check, principal=principal)
    await gate(genesis_texts(found), found.rating_id, [m.true_age for m in found.members])
    profile_id, version, document_, warnings = await choose_profile(db, found)
    found.entries.extend(warnings)
    impact = await impact_of(db, found, profile_id, version, document_, facts)
    if impact.warnings and not request.ignore_warnings:
        return CreationHeld(impact=impact, report=creation_report_of(found))
    created_id, revision, rating = await genesis(
        db,
        found,
        principal=principal,
        profile=(profile_id, version),
        write_policies=write_policies,
        story_id=story_id,
    )
    report = creation_report_of(found, story_id=created_id)
    return CreatedStory(
        story_id=created_id,
        characters=tuple(
            CharacterRef(id=m.id, name=m.name, role_id=m.role.id) for m in found.members
        ),
        timeline_revision=revision,
        content_rating=rating,
        scenario=ScenarioOrigin(id=found.scenario_id, version=found.version),
        engine_profile=effective.ProfileVersionRef(id=profile_id, version=version),
        report=report,
        impact=impact,
    )


__all__ = [
    "PRECEDENCE",
    "REFUSALS",
    "CreationHeld",
    "Gate",
    "Plan",
    "PolicyWriter",
    "RatingCheck",
    "create_from_scenario",
    "creation_report_of",
    "genesis",
    "genesis_texts",
    "insert_cast",
    "insert_deferred",
    "plan",
    "refuse",
    "render_definition",
]
