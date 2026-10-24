"""Story lifecycle over REST (FR-ADMIN-1): create, read, list, archive, unarchive, delete.

- ``POST /stories``: a blank story with its cast (each character with a required true age),
  rating and settings; 201 with ``Location``.
- ``GET /stories`` (``?status=``, ``?owner=``, repeatable ``?relation=name:principal``) and
  ``GET /stories/{id}``; a character's ``true_age`` appears only under ``?viewpoint=omniscient``
  (FR-CAT-12). The single read also reports ``undo_available`` and ``dormant_chains`` (#65).
  Both show the story's ``owner_principal`` (FR-AUTH-5).
- ``PUT /stories/{id}/owner``, ``POST``/``GET /stories/{id}/relations`` and ``DELETE
  /stories/{id}/relations/{relation}/{principal}``: the story's owner and relations
  (``routes.ownership``, #104). Nothing gates on them (FR-AUTH-8).
- ``POST /stories/{id}/characters``: add an ad-hoc character to a running story (FR-CAT-7;
  #114): story-scoped, never a catalog entry; 201.
- ``POST /stories/{id}/characters/{cid}/promote``: a new **draft** catalog entry from one of the
  story's characters, copying only its definition fields (FR-CAT-8; #114), and seeding the
  memories ``seed_memories`` names into its companion scenario draft (#498); 201 with
  ``Location: /catalog/characters/{id}``. Tagged ``catalog``.
- ``POST /stories/{id}/characters/{cid}/weight`` and ``POST /stories/{id}/entities/{eid}/model``:
  an author's weight change and modeling directive (FR-EPI-9,10; #163), control operations.
- ``POST /stories/{id}/archive`` and ``…/unarchive``: control operations, idempotent (200).
- ``DELETE /stories/{id}``: removes every row of the story and nothing else (204).
- ``POST /stories/{id}/ingest``: record consumer events (a player-only turn, narration) without
  generating, deduplicated by ``client_event_id`` (FR-RESP-10; #58): 201 when every event is
  new, 200 when any was ingested before (``Idempotent-Replay: true`` when all were).
- ``GET /stories/{id}/settings``: the settings with defaults filled in, and the story's
  guardrails — ``effective_policies`` (scope, source, applies_to, age_condition, kind),
  ``inactive_policies`` (age-conditioned ones whose condition is false for the cast) and
  ``guardrail_calls`` — so the model-call cost of guardrails is predictable (FR-GUARD-9). A
  policy a pending catalog-scope erasure reaches is listed with a ``null`` name (#447).
- ``PATCH /stories/{id}/settings``: a JSON merge patch over the settings (#63), plus the story's
  own ``policies``; answers the read's shape with the revisions.

``POST /stories`` gates every character definition through the input gate first (#56).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Body, Path, Query, Response
from fastapi import status as http_status
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    WithJsonSchema,
    create_model,
    model_validator,
)

from epistrel.api.config_changes import ConfigChangesBody, IsoDateTime, StorySettingFilter
from epistrel.api.deps import (
    ContextDep,
    EngineDep,
    RespondAsyncDep,
    StoryCharacterId,
    StoryId,
)
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.ownership import mount_ownership_routes
from epistrel.api.routes.timeline import VariantChainBody
from epistrel.api.validation import register_body_model, register_body_selector
from epistrel.core import idempotency
from epistrel.core.catalog.promote import PromoteRequest, Promotion
from epistrel.core.catalog.report import Entry
from epistrel.core.config_changes import ConfigScope
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import InvalidRequest
from epistrel.core.genesis.create import CreationHeld
from epistrel.core.genesis.models import CreationReport, StoryFromScenario, is_scenario_body
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.ingest import IngestRequest, IngestResult
from epistrel.core.ownership import RELATION_FILTER_PATTERN, relation_filter_problem
from epistrel.core.policies.model import Policy, PolicyKind, Scope, Target, WithheldPolicy
from epistrel.core.profiles.effective import ProfileVersionRef
from epistrel.core.profiles.impact import ImpactReport
from epistrel.core.settings_schema import ConfigClass, StorySettings
from epistrel.core.stories import (
    AdHocCharacter,
    CharacterView,
    CharacterWeighted,
    CharacterWeightRequest,
    CreatedStory,
    ModelEntityRequest,
    ParticipationSet,
    ParticipationSpec,
    RatingRef,
    StatusChange,
    StoryCharacterAdded,
    StorySpec,
    StoryView,
)
from epistrel.core.story_settings import StorySettingsWrite, check_keys
from epistrel.core.tables.ownership import EntityType
from epistrel.core.timeline import UndoState
from epistrel.core.variants import Chain
from epistrel.engine import EffectiveBudget
from epistrel.orchestrator.models.metrics import ModelCall
from epistrel.orchestrator.policies.schemas import StoryPolicyItem
from epistrel.orchestrator.policies.store import StoryGuardrails

router = APIRouter(prefix="/stories", tags=["stories"])

StoryEntityId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The id of one of the story's entities (a ULID), as "
        "`GET /stories/{id}/scene` and the inspect reads list them.",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"],
    ),
]


class StoryRatingBody(BaseModel):
    """The story's rating: the rating, its current version and its name."""

    id: str
    version_id: str
    name: str

    @classmethod
    def of(cls, rating: RatingRef) -> StoryRatingBody:
        return cls(id=rating.id, version_id=rating.version_id, name=rating.name)


class CharacterRefBody(BaseModel):
    """A character of the story: its id and name."""

    id: str
    name: str


class CatalogSourceBody(BaseModel):
    """The catalog version a story character was created from (``catalog_id@version``)."""

    catalog_id: str
    version: int


class ScenarioOriginBody(BaseModel):
    """The scenario version a story was created from (#119)."""

    id: str
    version: int


class ParticipationBody(BaseModel):
    """A character's participation role (FR-MULTI-7; #176): ``primary``, ``reactive`` or
    ``silent``, and whether it is ``fixed`` (a bound the engine never crosses on its own)."""

    role: Literal["primary", "reactive", "silent"]
    fixed: bool


class CharacterBody(CharacterRefBody):
    """A character as a read returns it; ``is_player`` marks a player character. A story created
    from a scenario (#119) names each character's ``role_id`` and, for a catalog character, its
    ``source``; a blank story's characters carry neither. ``participation`` (#176) is every
    non-player character's role; the player's character has none."""

    is_player: bool
    role_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    source: CatalogSourceBody | None = Field(default=None, exclude_if=lambda v: v is None)
    participation: ParticipationBody | None = Field(default=None, exclude_if=lambda v: v is None)


class ModeledAtBody(BaseModel):
    """A narrative position (#163's ``modeled_at``): the turn and its variant, the inter-turn
    slot (0: the turn itself), the beat and the step. The genesis cast is at 0."""

    turn: int
    variant: int
    slot_k: int
    beat: int
    step: int


class OmniscientCharacterBody(CharacterBody):
    """A character as an omniscient read returns it: with its true age (FR-CAT-12), the
    catalog entries promoted from it (``promoted_to``, FR-CAT-8; oldest first), its narrative
    ``weight`` (``minor``: modeled lazily by interaction or salience; ``major``: the cast, the
    player, an author-created or promoted character; FR-EPI-10) and the position it was
    modeled at (``modeled_at``, #163)."""

    true_age: int
    promoted_to: list[str]
    weight: Literal["minor", "major"] = "major"
    modeled_at: ModeledAtBody | None = None


class CharacterWeightedBody(MutationResponse):
    """A character after a weight change or a modeling directive (#163): the character as an
    omniscient read lists it, and the story's revision after."""

    character: OmniscientCharacterBody

    @classmethod
    def of(cls, done: CharacterWeighted) -> CharacterWeightedBody:
        body = _character(done.character)
        assert isinstance(body, OmniscientCharacterBody)  # noqa: S101 - an omniscient item
        return cls(character=body, timeline_revision=done.timeline_revision)


class ParticipationSetBody(MutationResponse):
    """A character after a participation change (#176): the character as a read lists it, with
    the story's ``config_revision`` (moved by a change) and its unchanged
    ``timeline_revision``."""

    character: CharacterBody
    config_revision: int

    @classmethod
    def of(cls, done: ParticipationSet) -> ParticipationSetBody:
        return cls(
            character=_character(done.character),
            config_revision=done.config_revision,
            timeline_revision=done.timeline_revision,
        )


class StoryCharacterBody(MutationResponse):
    """A character added to the story: its id, name and place in the cast (``ordinal``), with
    the story's revision after the addition."""

    id: str
    name: str
    ordinal: int

    @classmethod
    def of(cls, added: StoryCharacterAdded) -> StoryCharacterBody:
        return cls(
            id=added.id,
            name=added.name,
            ordinal=added.ordinal,
            timeline_revision=added.timeline_revision,
        )


class PromotionBody(MutationResponse):
    """A promotion's new **draft** catalog entry (``catalog_id``; read it with
    ``GET /catalog/characters/{id}?draft=true``), its draft revision, the warnings (#111's
    entries: ``definition_truncated``, an image left out as ``media_retired``), and the story's
    ``timeline_revision``, which a promotion leaves as it is."""

    catalog_id: str
    draft_revision: int
    warnings: list[Entry]

    @classmethod
    def of(cls, promotion: Promotion) -> PromotionBody:
        return cls(
            catalog_id=promotion.catalog_id,
            draft_revision=promotion.draft_revision,
            warnings=list(promotion.warnings),
            timeline_revision=promotion.timeline_revision,
        )


class CreatedCharacterBody(CharacterRefBody):
    """A character of the new story: its id and name, and the scenario role it fills (absent
    for a blank story)."""

    role_id: str | None = Field(default=None, exclude_if=lambda v: v is None)


class StoryCreatedBody(MutationResponse):
    """The new story (201): its id, its cast's ids (request order for a blank story, the
    scenario's role order for one created from a scenario), its rating and its revision. A
    story created from a scenario (#119) also names the scenario version, the engine profile
    it runs under, the creation ``report`` (counts and the unavailable checks under the
    ``card`` projection, every entry under ``full``) and #109's ``impact`` report. When the
    impact report warns and the request did not set ``ignore_warnings`` (FR-PROF-7), nothing
    is created: 200 with ``applied: false``, the reports, and no story (``story_id``,
    ``content_rating`` ``null``)."""

    applied: bool = True
    story_id: str | None
    characters: list[CreatedCharacterBody]
    content_rating: StoryRatingBody | None
    scenario: ScenarioOriginBody | None = None
    engine_profile: ProfileVersionRef | None = None
    report: CreationReport | None = None
    impact: ImpactReport | None = None

    @classmethod
    def of(cls, created: CreatedStory | CreationHeld) -> StoryCreatedBody:
        if isinstance(created, CreationHeld):
            return cls(
                applied=False,
                story_id=None,
                characters=[],
                content_rating=None,
                timeline_revision=created.timeline_revision,
                report=created.report,
                impact=created.impact,
            )
        scenario = created.scenario
        return cls(
            story_id=created.story_id,
            characters=[
                CreatedCharacterBody(id=c.id, name=c.name, role_id=c.role_id)
                for c in created.characters
            ],
            timeline_revision=created.timeline_revision,
            content_rating=StoryRatingBody.of(created.content_rating),
            scenario=None
            if scenario is None
            else ScenarioOriginBody(id=scenario.id, version=scenario.version),
            engine_profile=created.engine_profile,
            report=created.report,
            impact=created.impact,
        )


class StorySummaryBody(BaseModel):
    """A story as the list returns it: its owner, status, revision, next turn index and
    rating."""

    id: str
    name: str | None
    owner_principal: str
    status: StoryStatus
    timeline_revision: int
    next_turn_index: int
    content_rating: StoryRatingBody
    created_at: datetime
    archived_at: datetime | None

    @classmethod
    def fields_of(cls, story: StoryView) -> dict[str, Any]:
        return {
            "id": story.id,
            "name": story.name,
            "owner_principal": story.owner_principal,
            "status": story.status,
            "timeline_revision": story.timeline_revision,
            "next_turn_index": story.next_turn_index,
            "content_rating": StoryRatingBody.of(story.content_rating),
            "created_at": story.created_at,
            "archived_at": story.archived_at,
        }


class DormantChainBody(BaseModel):
    """A dormant continuation (#66): turns ``from_turn``..``through_turn`` of the chain
    ``{from_turn, variant}``; ``POST /stories/{id}/swap`` makes it active.
    ``truncate_operation_id`` names the truncation whose tail it is, when ``untruncate`` would
    undo that truncation."""

    from_turn: int
    variant: int
    through_turn: int
    turns: int
    truncate_operation_id: str | None


class StoryPauseProgressBody(BaseModel):
    """The progress of the check that paused the story: records checked, of how many."""

    done: int
    total: int


class StoryPauseBody(BaseModel):
    """What pauses the story (#118): the check's job (``GET
    /stories/{id}/content-rating/checks/{job_id}``), its ``cause`` (``rating_check``) and its
    progress."""

    job_id: str | None
    cause: str
    progress: StoryPauseProgressBody


class StoryBody(StorySummaryBody):
    """One story: the summary, its cast (true ages only for an omniscient read), its settings,
    its engine profile and the version it runs on (#113; ``GET /stories/{id}/effective-config``
    has the whole configuration), whether a truncation can be undone, its dormant continuations
    and every variant chain."""

    characters: list[OmniscientCharacterBody | CharacterBody]
    settings: dict[str, Any]
    engine_profile: ProfileVersionRef
    undo_available: bool
    """Whether ``POST /stories/{id}/untruncate`` has a truncation to revert (by reactivation,
    or by a swap when newer turns exist)."""
    dormant_chains: list[DormantChainBody]
    variant_chains: list[VariantChainBody]
    """Every chain of the story, as ``GET /stories/{id}/variants`` lists them (#66)."""
    pause: StoryPauseBody | None = None
    """While ``status`` is ``paused`` (#118): the check that paused it; ``null`` otherwise."""
    scenario: ScenarioOriginBody | None = None
    """The scenario version the story was created from (#119); ``null`` for a blank story."""

    @classmethod
    def of(cls, story: StoryView, undo: UndoState, chains: list[Chain]) -> StoryBody:
        assert story.engine_profile is not None  # noqa: S101 - get_story reads it
        return cls(
            **StorySummaryBody.fields_of(story),
            characters=[_character(c) for c in story.characters],
            settings=story.settings,
            engine_profile=story.engine_profile,
            undo_available=undo.undo_available,
            dormant_chains=[
                DormantChainBody(
                    from_turn=c.from_turn,
                    variant=c.variant,
                    through_turn=c.through_turn,
                    turns=c.turns,
                    truncate_operation_id=c.truncate_operation_id,
                )
                for c in undo.dormant_chains
            ],
            variant_chains=[VariantChainBody.of(c) for c in chains],
            pause=StoryPauseBody.model_validate(story.pause) if story.pause else None,
            scenario=None
            if story.scenario is None
            else ScenarioOriginBody(id=story.scenario.id, version=story.scenario.version),
        )


class StoryList(BaseModel):
    """The stories, newest first."""

    items: list[StorySummaryBody]

    @classmethod
    def of(cls, found: list[StoryView]) -> StoryList:
        return cls(items=[StorySummaryBody(**StorySummaryBody.fields_of(s)) for s in found])


class StatusBody(MutationResponse):
    """The story's status after an archive or unarchive, with its revision."""

    status: StoryStatus

    @classmethod
    def of(cls, change: StatusChange) -> StatusBody:
        return cls(status=change.status, timeline_revision=change.timeline_revision)


class EffectivePolicyBody(BaseModel):
    """A policy that applies to the story (or would, once its age condition holds), with its
    source and kind: a ``model_assisted`` policy costs a model call per check (FR-GUARD-9)."""

    id: str
    name: str | None
    """``null`` while a pending catalog-scope erasure reaches the policy's text (it is still
    enforced; #447, ADR-161)."""
    scope: Scope
    source: str
    """``global``, ``rating:<id>`` (the rating that contributes it), ``story`` or
    ``character:<id>``."""
    applies_to: list[Target]
    age_condition: str | None
    kind: PolicyKind

    @classmethod
    def of(
        cls, policy: Policy | WithheldPolicy, guardrails: StoryGuardrails
    ) -> EffectivePolicyBody:
        return cls(
            id=policy.id,
            name=policy.name,
            scope=policy.scope,
            source=guardrails.source(policy),
            applies_to=list(policy.applies_to),
            age_condition=policy.age_condition,
            kind=policy.kind,
        )


class GuardrailCallsBody(BaseModel):
    """Model calls the guardrails add: ``input`` per text checked (a character-scoped input
    policy adds one when its character acts), ``output`` per output checkpoint."""

    input: int
    output: int


class EffectiveBudgetBody(BaseModel):
    """The budget the story's next turn plans with (#69), from its effective configuration's
    `budgets` (#113): `max_model_calls_per_turn` and its `source` (`story`: the story's
    overrides, `profile`: its profile's version, `deployment`: the deployment's
    `profile_defaults`, or `formula`: `9 + G` when the cap is `null`), `turn_deadline` (seconds:
    `budgets.deadline_ms` / 1000), the guardrail calls G it reserves (`guardrail_input`,
    `guardrail_output`) and `minimum_required` (`1 + G`)."""

    max_model_calls_per_turn: int
    source: str
    turn_deadline: float
    reserved: dict[str, int]
    minimum_required: int

    @classmethod
    def of(cls, budget: EffectiveBudget) -> EffectiveBudgetBody:
        return cls(
            max_model_calls_per_turn=budget.max_model_calls_per_turn,
            source=budget.source,
            turn_deadline=budget.turn_deadline,
            reserved=dict(budget.reserved),
            minimum_required=budget.minimum_required,
        )


class StorySettingsBody(BaseModel):
    """The story's settings with defaults filled in, its guardrails (effective and inactive
    policies, the model calls they add) and the turn budget its next turn plans with."""

    settings: dict[str, Any]
    effective_policies: list[EffectivePolicyBody]
    inactive_policies: list[EffectivePolicyBody]
    guardrail_calls: GuardrailCallsBody
    effective_budget: EffectiveBudgetBody

    @classmethod
    def of(
        cls, settings: StorySettings, guardrails: StoryGuardrails, budget: EffectiveBudget
    ) -> StorySettingsBody:
        return cls(
            settings=settings.model_dump(mode="json"), **_guardrail_fields(guardrails, budget)
        )


class StorySettingsWriteBody(MutationResponse):
    """The settings after the write, in the read's shape, with the story's ``config_revision``
    (moved by a write that changed something) and its unchanged ``timeline_revision``."""

    settings: dict[str, Any]
    effective_policies: list[EffectivePolicyBody]
    inactive_policies: list[EffectivePolicyBody]
    guardrail_calls: GuardrailCallsBody
    effective_budget: EffectiveBudgetBody
    config_revision: int

    @classmethod
    def of(
        cls, write: StorySettingsWrite, guardrails: StoryGuardrails, budget: EffectiveBudget
    ) -> StorySettingsWriteBody:
        return cls(
            settings=write.settings,
            **_guardrail_fields(guardrails, budget),
            config_revision=write.config_revision,
            timeline_revision=write.timeline_revision,
        )


def _guardrail_fields(guardrails: StoryGuardrails, budget: EffectiveBudget) -> dict[str, Any]:
    return {
        "effective_policies": [EffectivePolicyBody.of(p, guardrails) for p in guardrails.active],
        "inactive_policies": [EffectivePolicyBody.of(p, guardrails) for p in guardrails.inactive],
        "guardrail_calls": GuardrailCallsBody(
            input=guardrails.calls_input, output=guardrails.calls_output
        ),
        "effective_budget": EffectiveBudgetBody.of(budget),
    }


class _SettingsPatchBase(BaseModel):
    """What ``StorySettingsPatch`` adds to the story's settings: its schema's description and
    example, and the refusal of a key that is no story setting."""

    model_config = ConfigDict(
        # Extra keys are refused by ``_known_keys`` (with the replacement of a moved one); the
        # published schema allows none.
        extra="allow",
        title="StorySettingsPatch",
        json_schema_extra={
            "description": "A JSON merge patch (RFC 7396) over the story's settings.",
            "examples": [{"turn_concurrency": "queue", "turn_queue_max": 3}],
            "additionalProperties": False,
        },
    )

    @model_validator(mode="after")
    def _known_keys(self) -> Self:
        """A key that is no story setting is 422; one of the engine dials that moved to the
        story's profile (#113) is 422 naming its replacement. An after-validator over the extra
        keys, so the strict JSON check of the fields' own types stays JSON's
        (``api.validation``)."""
        for key in self.model_extra or {}:
            check_keys({key: None})
        return self


def _settings_patch_model() -> type[BaseModel]:
    """``PATCH /stories/{id}/settings``'s documented body: every story setting, optional and
    nullable (``null`` resets it), plus ``policies``. The handler validates the patch itself
    (``Engine.update_story_settings``); this model is the schema consumers generate from."""
    fields: dict[str, Any] = {}
    for name, info in StorySettings.model_fields.items():
        annotation: Any = info.annotation | None  # type: ignore[operator]
        if info.metadata:
            annotation = Annotated[annotation, *info.metadata]
        fields[name] = (
            annotation,
            Field(default=None, description=info.description),
        )
    fields["policies"] = (
        list[StoryPolicyItem] | None,
        Field(default=None, description="Upserts the story's own policies (never relaxing)."),
    )
    return create_model("StorySettingsPatch", __base__=_SettingsPatchBase, **fields)


StorySettingsPatch = _settings_patch_model()
register_body_model("update_story_settings", StorySettingsPatch)


SETTINGS_PATCH_DESCRIPTION = (
    "Change the story's settings with a JSON merge patch (RFC 7396): a key present sets that "
    "setting and `null` resets it to its default. A story's settings describe the story: "
    "`turn_concurrency` (`reject` | `queue`), `turn_queue_max` (1-50; `null`: the deployment's "
    "default, 5), `default_output_viewpoint` (`character:<id>` of the cast, never "
    "`omniscient`), `regenerate_retry_budget` (0-10), `lite_summary_chunk` (2-200 rows per "
    "summary segment), `audit_retention_days` (1-3650; `null`: the deployment's) and "
    "`allow_new_arrivals` (whether a director pass, or a render as scene texture, may introduce "
    "new arrivals; default `true`), `interaction_mode` (`sequential` | `simultaneous`: how a "
    "turn with several primaries renders them; `null`: the scenario's, else `sequential`), "
    "`sequence_order` (`request` | `director`: who orders a sequential turn's primaries; "
    "`null`: `request`) and `simultaneous_tie_break` (`request_order` | `director` | "
    "`state:<name>`: how a simultaneous turn settles conflicting actions; `null`: "
    "`request_order`; `state:<name>` is 422 `not_yet` until M6), `reactive_interjections` "
    "(whether reactive narration may give a reactive character a brief interjection; default "
    "`false`) and `player_view` (`character` | `story`: whether a character viewpoint's output "
    "holds only what that character perceived, the audience filter, or every beat; default "
    "`character`; a no-op where perception is `none`). An "
    "unknown key is 422. The engine's dials are the story's engine profile's, overridden per "
    "story with `PATCH /stories/{id}/profile-overrides` (FR-PROF-5): a key that moved there "
    "(`generation_settings`, `lite_window_messages`, `lite_summary_max_tokens`, "
    "`max_model_calls_per_turn`, `turn_deadline`, `guardrail_retry_budget`) is 422 with "
    "`details.reason: superseded` and the profile setting that replaced it in "
    "`details.replacement`. `policies` (optional) upserts the story's own policies; an item "
    "that would relax a policy refuses the whole request with 422 `cannot_relax` before "
    "anything is written."
    "\n\nEvery setting but `default_output_viewpoint` and `allow_new_arrivals` is prospective: "
    "it applies to requests that arrive after the write, never to a turn already running; "
    "those two are interpretive (a turn in flight is checked against them when it commits). A "
    "write that changes something moves the story's `config_revision`. The answer carries the "
    "budget the story's next turn plans with (`effective_budget`)."
)


IDEMPOTENT_REPLAY_HEADER = idempotency.IDEMPOTENT_REPLAY_HEADER

INGEST_DESCRIPTION = (
    "Record events into the story's authoritative history without generating a reply: a "
    "`player_turn` (an actor's `say`/`do`) or consumer-authored `narration` (`text`, no actor)."
    " Each event is its own narrative turn at the next turn index and advances the timeline "
    "revision; up to 50 events are recorded all or nothing, each through the input gate.\n\n"
    "Every event carries a `client_event_id` unique within the story. Re-ingesting an ID with "
    "the same payload returns the original result and writes nothing; the same ID with a "
    "different payload is 409 `client_event_conflict`. The response is 201 when every event is "
    "new and 200 when any was replayed (`Idempotent-Replay: true` when all were). "
    "`outcome` and `state_change` events arrive in M6 (422 `unsupported_event_kind`)."
    "\n\nThe batch is one turn for turn concurrency: while a generated turn runs, a story with "
    "`turn_concurrency: reject` answers 423 `story_busy`, and one with `queue` queues the batch "
    "(waiting for it up to the story's turn deadline, its `budgets.deadline_ms`, then 202 "
    "`{operation_id, position}`; "
    "`Prefer: respond-async` answers 202 at once).\n\nThe response reports what the input gate "
    "spent: `model_calls` (one `guardrail_check` per model-assisted input policy per text) and "
    "`elapsed_ms`."
)


class IngestedEventBody(BaseModel):
    """What one event became: its operation and turn, and whether it was ingested before."""

    client_event_id: str
    operation_id: str
    turn: int
    replayed: bool


class IngestResponse(MutationResponse):
    """One result per event, in request order, the story's revision after the call, and what
    the input gate spent: its ``model_calls`` (as a turn reports them) and ``elapsed_ms``."""

    results: list[IngestedEventBody]
    model_calls: list[ModelCall]
    elapsed_ms: int = Field(ge=0)

    @classmethod
    def of(cls, result: IngestResult) -> IngestResponse:
        return cls(
            results=[
                IngestedEventBody(
                    client_event_id=r.client_event_id,
                    operation_id=r.operation_id,
                    turn=r.turn,
                    replayed=r.replayed,
                )
                for r in result.results
            ],
            timeline_revision=result.timeline_revision,
            model_calls=[ModelCall.model_validate(call) for call in result.model_calls],
            elapsed_ms=result.elapsed_ms,
        )


def _character(character: CharacterView) -> OmniscientCharacterBody | CharacterBody:
    source = (
        None
        if character.source is None
        else CatalogSourceBody(
            catalog_id=character.source.catalog_id, version=character.source.version
        )
    )
    if character.true_age is not None:
        return OmniscientCharacterBody(
            id=character.id,
            name=character.name,
            is_player=character.is_player,
            role_id=character.role_id,
            source=source,
            true_age=character.true_age,
            promoted_to=list(character.promoted_to or ()),
            weight="minor" if character.weight == "minor" else "major",
            participation=_participation(character),
            modeled_at=None
            if character.modeled_at is None
            else ModeledAtBody(
                turn=character.modeled_at.turn,
                variant=character.modeled_at.variant,
                slot_k=character.modeled_at.slot_k,
                beat=character.modeled_at.beat,
                step=character.modeled_at.step,
            ),
        )
    return CharacterBody(
        id=character.id,
        name=character.name,
        is_player=character.is_player,
        role_id=character.role_id,
        source=source,
        participation=_participation(character),
    )


def _participation(character: CharacterView) -> ParticipationBody | None:
    found = character.participation
    if found is None:
        return None
    return ParticipationBody(role=found["role"], fixed=bool(found["fixed"]))


def _create_body_model(document: Any) -> type[BaseModel]:
    return StoryFromScenario if is_scenario_body(document) else StorySpec


register_body_selector("create_story", _create_body_model)


def parse_create_body(body: dict[str, Any]) -> StorySpec | StoryFromScenario:
    """The ``POST /stories`` body as its model (422 ``validation_error`` at ``body.<field>``)."""
    try:
        if is_scenario_body(body):
            return StoryFromScenario.model_validate(body)
        return StorySpec.model_validate(body)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body",)) from None


CREATE_DESCRIPTION = (
    "Create a story, blank or from a scenario, in one operation; answers 201 with `Location`."
    "\n\n**Blank** (`StorySpec`): its cast (each character with a required `true_age`), its "
    "content rating (default: the deployment's) and its settings. Every character "
    "`definition` (and `deflection_style`, how it refuses or changes the subject: FR-GUARD-7) "
    "passes the input gate first: a violation is 422 `guardrail_violation` and no story exists."
    "\n\n**From a scenario** (`StoryFromScenario`, the body naming `scenario`; FR-SCN-6,9): "
    "the scenario (and a published version; default the latest), who fills each NPC role "
    "(`slots`: a catalog `character@version` or an `ad_hoc` definition; locked roles and "
    "default roles kept as pre-filled need none), each player's principal and persona "
    "(`players`, saved or ad hoc, in the scenario's player role), permitted `overrides`, an "
    "optional rating, engine profile and `max_characters_at_start`, and `strict`. The story is "
    "created in one transaction: one genesis operation, full snapshots of the scenario and "
    "of each catalog character (`catalog_id@version`), story settings and policies, a "
    "`player` relation per player principal, and the opening narration as turn 1. A refusal "
    "writes nothing: one 422 whose code is the first present of `creation_conflicts` (under "
    "`strict`), `slot_required`, `locked_slot_reassigned`, `player_role_fixed`, "
    "`persona_required`, `entry_archived`, `not_published`, `override_not_allowed`, "
    "`eligibility_failed`, `cast_limit_exceeded`, `rating_check_failed`, then the input gate's "
    "`guardrail_violation`, then, for a story whose profile runs the `structured` pipeline, "
    "`seed_unnormalizable` (a canon seed naming an entity the cast and world lack), with the "
    "full creation report in `details.report`. A structured story's knowledge seeds, story "
    "rules, places and entities are materialized in the same transaction (#149): the report "
    "lists each item's outcome under `materialized` and the merge conflicts it dropped. A scenario "
    "rating mismatch is a warning (FR-RATE-5). Owners and listing statuses are never checked "
    "(FR-AUTH-8). When the engine profile leaves a capability the scenario recommends unmet, "
    "the answer is 200 with `applied: false` and the reports, unless the request sets "
    "`ignore_warnings` (FR-PROF-7)."
)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=StoryCreatedBody,
    operation_id="create_story",
    summary="Create a story",
    description=CREATE_DESCRIPTION,
    responses={
        200: {
            "description": "A creation from a scenario held back by its impact report "
            "(FR-PROF-7): nothing was created; resend with `ignore_warnings`.",
        }
    },
)
async def create_story(
    body: Annotated[dict[str, Any], Body()], response: Response, ctx: ContextDep, engine: EngineDep
) -> StoryCreatedBody:
    request = parse_create_body(body)
    if isinstance(request, StoryFromScenario):
        result = await engine.create_story(ctx, request)
    else:
        result = await engine.create_story(ctx, request)
    if isinstance(result, CreationHeld):
        response.status_code = http_status.HTTP_200_OK
    else:
        response.headers["Location"] = f"/stories/{result.story_id}"
    return answered(StoryCreatedBody.of(result), ctx, response, replayed=result.replayed)


def _relation_filter(value: str) -> str:
    problem = relation_filter_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


RelationFilterParam = Annotated[
    str, StringConstraints(pattern=RELATION_FILTER_PATTERN), AfterValidator(_relation_filter)
]
"""A ``relation`` filter: ``name:principal``, or ``name:`` for any principal."""

OWNER_FILTER_SCHEMA = {"type": "string", "pattern": PRINCIPAL_PATTERN}
RELATION_FILTERS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "pattern": RELATION_FILTER_PATTERN},
}
"""The filters' documented schemas. A query parameter that is absent is simply not sent, so the
schemas carry no ``null`` alternative: a value a client could send for "null" (``true``, ``0``,
an object's pairs) would read as a principal over the wire."""


@router.get(
    "",
    response_model=StoryList,
    operation_id="list_stories",
    summary="List stories",
    description=(
        "Every story, newest first, unless the request filters: by `status`, by `owner` (the "
        "owner principal, `system` included), or by `relation` (`name:principal`, or `name:` "
        "for any principal; repeat it and every one must hold). Owners and relations narrow a "
        "list only when a request asks: they gate nothing (FR-AUTH-8). A story held "
        "`blocked_by_erasure` after a restore is listed with a `null` `name` until its erasure "
        "is reapplied (FR-ADMIN-7)."
    ),
)
async def list_stories(
    ctx: ContextDep,
    engine: EngineDep,
    status: StoryStatus | None = None,
    owner: Annotated[
        str | None,
        Query(pattern=PRINCIPAL_PATTERN, description="Only the stories this principal owns."),
        WithJsonSchema(OWNER_FILTER_SCHEMA),
    ] = None,
    relation: Annotated[
        list[RelationFilterParam] | None,
        Query(
            description="Only the stories with this relation: `name:principal`, or `name:` for "
            "any principal (repeatable; all must hold)."
        ),
        WithJsonSchema(RELATION_FILTERS_SCHEMA),
    ] = None,
) -> StoryList:
    found = await engine.list_stories(ctx, status=status, owner=owner, relation=relation or ())
    return StoryList.of(found)


@router.get(
    "/{story_id}",
    response_model=StoryBody,
    operation_id="get_story",
    summary="Read a story",
    description=(
        "The story, its cast and settings, its `engine_profile` (the profile and the version it "
        "runs on; `GET /stories/{id}/effective-config` has the whole configuration), "
        "`undo_available`, its `dormant_chains` and its `variant_chains`. While a check of a "
        "rating change pauses it (`status: paused`), `pause` names the check `{job_id, cause, "
        "progress}`. A character's "
        "`true_age` is returned only under "
        "`?viewpoint=omniscient` (FR-CAT-12); a viewpoint that is neither `omniscient` nor "
        "`character:<id>` of the cast is 422 `viewpoint_unresolved`."
    ),
)
async def get_story(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> StoryBody:
    story = await engine.get_story(ctx, story_id)
    undo = await engine.undo_state(ctx, story_id)
    chains = await engine.variants(ctx, story_id)
    return StoryBody.of(story, undo, chains)


@router.post(
    "/{story_id}/ingest",
    status_code=http_status.HTTP_201_CREATED,
    response_model=IngestResponse,
    operation_id="ingest",
    summary="Record consumer events without generating",
    description=INGEST_DESCRIPTION,
)
async def ingest(
    story_id: StoryId,
    body: IngestRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    respond_async: RespondAsyncDep,
) -> IngestResponse:
    result = await engine.ingest(ctx, story_id, body, respond_async=respond_async)
    if not result.created:
        response.status_code = http_status.HTTP_200_OK
    return answered(IngestResponse.of(result), ctx, response, replayed=result.replayed)


@router.get(
    "/{story_id}/settings",
    response_model=StorySettingsBody,
    operation_id="story_settings",
    summary="Read the story's settings and guardrails",
    description=(
        "The settings with defaults filled in, and the story's guardrails: "
        "`effective_policies` (scope, source, applies_to, age_condition, kind), "
        "`inactive_policies` (age-conditioned ones whose condition is false for the cast) and "
        "`guardrail_calls`, so the model-call cost of guardrails is predictable (FR-GUARD-9); "
        "and `effective_budget`, the budget the next turn plans with. A policy whose text a "
        "pending catalog-scope erasure reaches is listed with a `null` `name` until the "
        "erasure is reapplied; it is still enforced (ADR-161)."
    ),
)
async def story_settings(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> StorySettingsBody:
    settings, guardrails = await engine.story_settings(ctx, story_id)
    budget = await engine.effective_budget(ctx, story_id)
    return StorySettingsBody.of(settings, guardrails, budget)


@router.get(
    "/{story_id}/config-changes",
    response_model=ConfigChangesBody,
    operation_id="config_changes_story",
    summary="List the story's configuration changes",
    description=(
        "The story's configuration changes (settings, story and character policies), newest "
        "first (#64); filter by `setting` (repeatable), `class` and `since`; `next` pages on "
        "(`after`)."
    ),
)
async def story_config_changes(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    setting: Annotated[list[StorySettingFilter] | None, Query()] = None,
    change_class: Annotated[ConfigClass | None, Query(alias="class")] = None,
    since: IsoDateTime | None = None,
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConfigChangesBody:
    page = await engine.config_changes(
        ctx,
        scope=ConfigScope.STORY,
        story_id=story_id,
        settings=setting or (),
        change_class=change_class,
        since=since,
        after=after,
        limit=limit,
    )
    return ConfigChangesBody.of(page)


@router.patch(
    "/{story_id}/settings",
    response_model=StorySettingsWriteBody,
    operation_id="update_story_settings",
    summary="Change the story's settings",
    description=SETTINGS_PATCH_DESCRIPTION,
)
async def update_story_settings(
    story_id: StoryId,
    body: Annotated[dict[str, Any], Body()],
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StorySettingsWriteBody:
    write = await engine.update_story_settings(ctx, story_id, body)
    _, guardrails = await engine.story_settings(ctx, story_id)
    budget = await engine.effective_budget(ctx, story_id)
    answer = StorySettingsWriteBody.of(write, guardrails, budget)
    return answered(answer, ctx, response, replayed=write.replayed)


@router.post(
    "/{story_id}/characters",
    status_code=http_status.HTTP_201_CREATED,
    response_model=StoryCharacterBody,
    operation_id="add_story_character",
    summary="Add a character to a running story",
    description=(
        "Add an ad-hoc character to the story's cast (FR-CAT-7): its `name`, its free-text "
        "`definition`, its required `true_age` (FR-CAT-12) and optionally its "
        "`safe_fallback_text`, its `deflection_style` (FR-GUARD-7) and `images` (media asset ids "
        "it shows, each a live image, by reference). It is story-scoped and never a catalog entry "
        "or a player character; `POST /stories/{id}/characters/{cid}/promote` makes a catalog "
        "draft from it. A full cast (20 characters), a name the story has (ignoring case) or an "
        "image that is not a live image asset is 422. The definition and the deflection style pass "
        "the input gate, with the new character's age in the cast: a violation is 422 "
        "`guardrail_violation`. 409 `story_archived` on an archived story, 423 `story_busy` while "
        "a turn is in flight. The addition bumps the story's revision (the cast is part of every "
        "prompt), records a `character_defined` event and is kept by a truncation and copied by "
        "every fork, as a story-text edit is. Answers 201 with the new character's id and place in "
        "the cast."
    ),
)
async def add_story_character(
    story_id: StoryId,
    body: AdHocCharacter,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StoryCharacterBody:
    added = await engine.add_story_character(ctx, story_id, body)
    return answered(StoryCharacterBody.of(added), ctx, response, replayed=added.replayed)


@router.post(
    "/{story_id}/characters/{character_id}/promote",
    status_code=http_status.HTTP_201_CREATED,
    response_model=PromotionBody,
    operation_id="promote_character",
    summary="Promote a story character to a catalog draft",
    description=(
        "Make a new **draft** catalog character from one of the story's characters (FR-CAT-8), "
        "owned by `owner` (default: the requesting principal) and rated `content_rating_id` "
        "(default: the story's rating). Only definition fields are copied: the name (or the "
        "request's), the true age as of the story's time, the free-text definition (as "
        "`definition.background_flavor`, cut at 4,000 characters with the warning "
        "`definition_truncated`), the safe fallback line, and the images by reference (the "
        "first as the primary image, the rest as the gallery; one no longer live is left out "
        "with the warning `media_retired`). No story event, pinned memory, summary or fact is "
        "copied into the character. `seed_memories` names memories of the story to seed into "
        "the new character's companion scenario draft instead (`starting_state.seeded_memories`, "
        "held by the `npc` role, audience `self`, disclosure `open`), each an active, "
        "unquarantined memory the character holds or can know; any other is 422 "
        "`seed_memory_invalid` (`details.reason`: `not_in_story`, `inactive`, `quarantined` or "
        "`not_known`) and nothing is written. The story character keeps a link to the entry "
        "(`promoted_to`, in an "
        "omniscient `GET /stories/{id}`) and nothing else of the story changes; its revision "
        "does not move, so a promotion is allowed on an archived story and while a turn is in "
        "flight. Promoting again makes another draft. A player character plays a persona and "
        "is 422 (`reason: player_character`). Answers 201 with `Location: "
        "/catalog/characters/{id}`."
    ),
)
async def promote_character(
    story_id: StoryId,
    character_id: StoryCharacterId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    body: Annotated[PromoteRequest | None, Body()] = None,
) -> PromotionBody:
    promotion = await engine.promote_character(ctx, story_id, character_id, body)
    response.headers["Location"] = f"/catalog/characters/{promotion.catalog_id}"
    return answered(PromotionBody.of(promotion), ctx, response, replayed=promotion.replayed)


@router.post(
    "/{story_id}/characters/{character_id}/weight",
    response_model=CharacterWeightedBody,
    operation_id="set_character_weight",
    summary="Set a story character's narrative weight",
    description=(
        "Set a character's narrative `weight` (FR-EPI-10): `minor` (a character modeled lazily "
        "by interaction or salience: a sparse partition that accrues only from the "
        "interactions it takes part in, a smaller consolidation window and hot budget, never "
        "rendered unless a request names it) or `major`. Prospective: what the character "
        "already holds stays as it is, and nothing promotes automatically. The player's "
        "character is always `major` (422 `is_player`). A control operation: it bumps the "
        "story's revision and records `character_weight_set`; 409 `story_archived`, 423 "
        "`story_busy` while a turn is in flight. Answers the character as an omniscient read "
        "lists it."
    ),
)
async def set_character_weight(
    story_id: StoryId,
    character_id: StoryCharacterId,
    body: CharacterWeightRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CharacterWeightedBody:
    done = await engine.set_character_weight(ctx, story_id, character_id, body)
    return answered(CharacterWeightedBody.of(done), ctx, response, replayed=done.replayed)


@router.patch(
    "/{story_id}/characters/{character_id}/participation",
    response_model=ParticipationSetBody,
    operation_id="set_participation",
    summary="Set a story character's participation role",
    description=(
        "Set a character's participation role (FR-MULTI-7): `role` is `primary` (it renders "
        "when a turn names it), `reactive` (another render may narrate its brief observable "
        "reaction, FR-MULTI-9; the default) or `silent` (nothing is narrated for it), and "
        "`fixed` makes the role a bound the engine never crosses on its own: escalation never "
        "raises a fixed `reactive` or `silent` character to a primary render (FR-MULTI-10), and "
        "budget planning never lowers a fixed `primary` one to reactive narration (it defers "
        "it instead). Only a request changes a fixed role, for that request, by naming the "
        "character or overriding its role (`roles`). A cast lead may be set `primary`. A "
        "prospective story configuration write: it moves the story's `config_revision` and "
        "records a `config_changes` row (`participation:<character id>`); a turn already "
        "running keeps its roles. The player's character has no role (422 `is_player`); 404 "
        "for a character the story does not have; 409 `story_archived`. Setting the role it has "
        "changes nothing. Answers the character as a read lists it."
    ),
)
async def set_participation(
    story_id: StoryId,
    character_id: StoryCharacterId,
    body: ParticipationSpec,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ParticipationSetBody:
    done = await engine.set_participation(ctx, story_id, character_id, body)
    return answered(ParticipationSetBody.of(done), ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/entities/{entity_id}/model",
    status_code=http_status.HTTP_201_CREATED,
    response_model=CharacterWeightedBody,
    operation_id="model_entity",
    summary="Model a story entity as a character",
    description=(
        "The author's directive to model one of the story's entities (a `person`, or an "
        "`other` entity) as a character (FR-EPI-9's explicit salience): a `minor` character "
        "with the entity's name, the request's required `true_age` (FR-CAT-12) and its "
        "`definition` (default: the entity's name). The entity's id becomes the character's, so "
        "what canon says of the entity is said of the character. Story-scoped (FR-CAT-7) and "
        "exempt from the cast cap. 404 for an entity the story does not have; 422 "
        "`already_modeled`, `not_a_person` or `allow_new_arrivals` (`details.reason`); 409 "
        "`story_archived`; 423 `story_busy` while a turn is in flight. A control operation: it "
        "bumps the revision and records `character_defined` and `character_modeled`. Answers "
        "201 with the new character as an omniscient read lists it."
    ),
)
async def model_entity(
    story_id: StoryId,
    entity_id: StoryEntityId,
    body: ModelEntityRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CharacterWeightedBody:
    done = await engine.model_entity(ctx, story_id, entity_id, body)
    return answered(CharacterWeightedBody.of(done), ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/archive",
    response_model=StatusBody,
    operation_id="archive_story",
    summary="Archive a story",
    description=(
        "Make the story read-only: truth-changing calls are refused with 409 `story_archived` "
        "until it is unarchived. Idempotent; 423 `story_busy` while a turn is in flight."
    ),
)
async def archive_story(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> StatusBody:
    change = await engine.archive_story(ctx, story_id)
    return answered(StatusBody.of(change), ctx, response, replayed=change.replayed)


@router.post(
    "/{story_id}/unarchive",
    response_model=StatusBody,
    operation_id="unarchive_story",
    summary="Unarchive a story",
    description="Make an archived story active again. Idempotent; 423 while a turn is in flight.",
)
async def unarchive_story(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> StatusBody:
    change = await engine.unarchive_story(ctx, story_id)
    return answered(StatusBody.of(change), ctx, response, replayed=change.replayed)


@router.delete(
    "/{story_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_story",
    summary="Delete a story",
    description=(
        "Remove every row of the story and nothing else (204). 423 `story_busy` while a turn "
        "runs (`reason: in_flight`) or a background job of the story runs (`reason: "
        "job_running`, `details.job_id`). A retry under the same `Idempotency-Key` is a 204 with "
        "`Idempotent-Replay: true` (the deletion is not repeated)."
    ),
)
async def delete_story(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> Response:
    deleted = await engine.delete_story(ctx, story_id)
    return no_content(ctx, replayed=deleted.replayed)


mount_ownership_routes(router, EntityType.STORY)
