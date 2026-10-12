"""Story lifecycle over REST (FR-ADMIN-1): create, read, list, archive, unarchive, delete.

- ``POST /stories``: a blank story with its cast (each character with a required true age),
  rating and settings; 201 with ``Location``.
- ``GET /stories`` (``?status=``) and ``GET /stories/{id}``; a character's ``true_age`` appears
  only under ``?viewpoint=omniscient`` (FR-CAT-12). The single read also reports
  ``undo_available`` and ``dormant_chains`` (#65).
- ``POST /stories/{id}/archive`` and ``…/unarchive``: control operations, idempotent (200).
- ``DELETE /stories/{id}``: removes every row of the story and nothing else (204).
- ``POST /stories/{id}/ingest``: record consumer events (a player-only turn, narration) without
  generating, deduplicated by ``client_event_id`` (FR-RESP-10; #58): 201 when every event is
  new, 200 when any was ingested before (``Idempotent-Replay: true`` when all were).
- ``GET /stories/{id}/settings``: the settings with defaults filled in, and the story's
  guardrails — ``effective_policies`` (scope, source, applies_to, age_condition, kind),
  ``inactive_policies`` (age-conditioned ones whose condition is false for the cast) and
  ``guardrail_calls`` — so the model-call cost of guardrails is predictable (FR-GUARD-9).
- ``PATCH /stories/{id}/settings``: a JSON merge patch over the settings (#63), plus the story's
  own ``policies``; answers the read's shape with the revisions.

``POST /stories`` gates every character definition through the input gate first (#56).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, create_model

from epistrel.api.config_changes import ConfigChangesBody, IsoDateTime, StorySettingFilter
from epistrel.api.deps import ContextDep, EngineDep, RespondAsyncDep, StoryId
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.timeline import VariantChainBody
from epistrel.api.validation import register_body_model
from epistrel.core import idempotency
from epistrel.core.config_changes import ConfigScope
from epistrel.core.enums import StoryStatus
from epistrel.core.ingest import IngestRequest, IngestResult
from epistrel.core.policies.model import Policy, PolicyKind, Scope, Target
from epistrel.core.settings_schema import ConfigClass, StorySettings
from epistrel.core.stories import (
    CharacterView,
    CreatedStory,
    RatingRef,
    StatusChange,
    StorySpec,
    StoryView,
)
from epistrel.core.story_settings import StorySettingsWrite
from epistrel.core.timeline import UndoState
from epistrel.core.variants import Chain
from epistrel.engine import EffectiveBudget
from epistrel.orchestrator.models.metrics import ModelCall
from epistrel.orchestrator.policies.schemas import StoryPolicyItem
from epistrel.orchestrator.policies.store import StoryGuardrails

router = APIRouter(prefix="/stories", tags=["stories"])


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


class CharacterBody(CharacterRefBody):
    """A character as a read returns it; ``is_player`` marks a player character."""

    is_player: bool


class OmniscientCharacterBody(CharacterBody):
    """A character as an omniscient read returns it: with its true age."""

    true_age: int


class StoryCreatedBody(MutationResponse):
    """The new story: its id, its cast's ids in request order, its rating and its revision."""

    story_id: str
    characters: list[CharacterRefBody]
    content_rating: StoryRatingBody

    @classmethod
    def of(cls, created: CreatedStory) -> StoryCreatedBody:
        return cls(
            story_id=created.story_id,
            characters=[CharacterRefBody(id=c.id, name=c.name) for c in created.characters],
            timeline_revision=created.timeline_revision,
            content_rating=StoryRatingBody.of(created.content_rating),
        )


class StorySummaryBody(BaseModel):
    """A story as the list returns it: status, revision, next turn index and rating."""

    id: str
    name: str | None
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


class StoryBody(StorySummaryBody):
    """One story: the summary, its cast (true ages only for an omniscient read), its settings,
    whether a truncation can be undone, its dormant continuations and every variant chain."""

    characters: list[OmniscientCharacterBody | CharacterBody]
    settings: dict[str, Any]
    undo_available: bool
    """Whether ``POST /stories/{id}/untruncate`` has a truncation to revert (by reactivation,
    or by a swap when newer turns exist)."""
    dormant_chains: list[DormantChainBody]
    variant_chains: list[VariantChainBody]
    """Every chain of the story, as ``GET /stories/{id}/variants`` lists them (#66)."""

    @classmethod
    def of(cls, story: StoryView, undo: UndoState, chains: list[Chain]) -> StoryBody:
        return cls(
            **StorySummaryBody.fields_of(story),
            characters=[_character(c) for c in story.characters],
            settings=story.settings,
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
        )


class StoryList(BaseModel):
    """The stories, oldest first."""

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
    name: str
    scope: Scope
    source: str
    """``global``, ``rating:<id>`` (the rating that contributes it), ``story`` or
    ``character:<id>``."""
    applies_to: list[Target]
    age_condition: str | None
    kind: PolicyKind

    @classmethod
    def of(cls, policy: Policy, guardrails: StoryGuardrails) -> EffectivePolicyBody:
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
    """The budget the story's next turn plans with (#69): `max_model_calls_per_turn` and its
    `source` (`story`, `deployment` or `formula`: `9 + G`), `turn_deadline`, the guardrail calls
    G it reserves (`guardrail_input`, `guardrail_output`) and `minimum_required` (`1 + G`)."""

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
    return create_model(
        "StorySettingsPatch",
        __config__=ConfigDict(
            extra="forbid",
            title="StorySettingsPatch",
            json_schema_extra={
                "description": "A JSON merge patch (RFC 7396) over the story's settings.",
                "examples": [
                    {"turn_concurrency": "queue", "turn_deadline": 30, "guardrail_retry_budget": 1}
                ],
            },
        ),
        **fields,
    )


StorySettingsPatch = _settings_patch_model()
register_body_model("update_story_settings", StorySettingsPatch)


SETTINGS_PATCH_DESCRIPTION = (
    "Change the story's settings with a JSON merge patch (RFC 7396): a key present sets that "
    "setting, `null` resets it to its default, and `generation_settings` merges the same way. "
    "`turn_concurrency` (`reject` | `queue`), `turn_queue_max` (1-50; `null`: the deployment's "
    "default, 5), `turn_deadline` (seconds), `max_model_calls_per_turn` (1-50; `null`: the "
    "deployment's default, else `9 + G`), `default_output_viewpoint` (`character:<id>` of the "
    "cast, never `omniscient`), `regenerate_retry_budget` (0-10), `guardrail_retry_budget` "
    "(0-5: regenerations after a reply breaks an output policy, before the safe fallback), "
    "`generation_settings`, `lite_window_messages`, `lite_summary_chunk` (2-200 rows per "
    "summary segment), `lite_summary_max_tokens` (the rolling summary's token cap) and "
    "`audit_retention_days` (1-3650; `null`: the deployment's). An unknown key is 422. "
    "`policies` (optional) upserts the story's own policies; an item that would relax a "
    "policy refuses the whole request with 422 `cannot_relax` before anything is written."
    "\n\nEvery setting but `default_output_viewpoint` is prospective: it applies to requests "
    "that arrive after the write, never to a turn already running; `default_output_viewpoint` "
    "is interpretive (a turn in flight is checked against it when it commits). A write that "
    "changes something moves the story's `config_revision`."
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
    "(waiting for it up to the story's `turn_deadline`, then 202 `{operation_id, position}`; "
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
    if character.true_age is not None:
        return OmniscientCharacterBody(
            id=character.id,
            name=character.name,
            is_player=character.is_player,
            true_age=character.true_age,
        )
    return CharacterBody(id=character.id, name=character.name, is_player=character.is_player)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=StoryCreatedBody,
    operation_id="create_story",
    summary="Create a story",
    description=(
        "Create a blank story with its cast (each character with a required `true_age`), its "
        "content rating (default: the deployment's) and its settings, in one operation. Every "
        "character `definition` passes the input gate first: a violation is 422 "
        "`guardrail_violation` and no story exists. Answers 201 with `Location`."
    ),
)
async def create_story(
    body: StorySpec, response: Response, ctx: ContextDep, engine: EngineDep
) -> StoryCreatedBody:
    created = await engine.create_story(ctx, body)
    response.headers["Location"] = f"/stories/{created.story_id}"
    return answered(StoryCreatedBody.of(created), ctx, response, replayed=created.replayed)


@router.get(
    "",
    response_model=StoryList,
    operation_id="list_stories",
    summary="List stories",
    description="Every story, optionally only those with `status`.",
)
async def list_stories(
    ctx: ContextDep, engine: EngineDep, status: StoryStatus | None = None
) -> StoryList:
    return StoryList.of(await engine.list_stories(ctx, status=status))


@router.get(
    "/{story_id}",
    response_model=StoryBody,
    operation_id="get_story",
    summary="Read a story",
    description=(
        "The story, its cast and settings, `undo_available`, its `dormant_chains` and its "
        "`variant_chains`. A character's `true_age` is returned only under "
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
        "and `effective_budget`, the budget the next turn plans with."
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
