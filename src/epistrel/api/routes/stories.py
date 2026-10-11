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
from pydantic import BaseModel

from epistrel.api.config_changes import ConfigChangesBody
from epistrel.api.deps import ContextDep, EngineDep, RespondAsyncDep
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.timeline import VariantChainBody
from epistrel.core import idempotency
from epistrel.core.config_changes import ConfigScope
from epistrel.core.enums import StoryStatus
from epistrel.core.ingest import IngestRequest
from epistrel.core.policies.model import Policy, PolicyKind, Scope, Target
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.stories import CharacterView, RatingRef, StorySpec, StoryView
from epistrel.engine import EffectiveBudget
from epistrel.orchestrator.policies.store import StoryGuardrails

router = APIRouter(prefix="/stories", tags=["stories"])


class RatingBody(BaseModel):
    """The story's rating: the rating, its current version and its name."""

    id: str
    version_id: str
    name: str

    @classmethod
    def of(cls, rating: RatingRef) -> RatingBody:
        return cls(id=rating.id, version_id=rating.version_id, name=rating.name)


class CharacterRefBody(BaseModel):
    id: str
    name: str


class CharacterBody(CharacterRefBody):
    is_player: bool


class OmniscientCharacterBody(CharacterBody):
    """A character as an omniscient read returns it: with its true age."""

    true_age: int


class StoryCreatedBody(MutationResponse):
    story_id: str
    characters: list[CharacterRefBody]
    content_rating: RatingBody


class StorySummaryBody(BaseModel):
    id: str
    name: str | None
    status: StoryStatus
    timeline_revision: int
    next_turn_index: int
    content_rating: RatingBody
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
            "content_rating": RatingBody.of(story.content_rating),
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
    characters: list[OmniscientCharacterBody | CharacterBody]
    settings: dict[str, Any]
    undo_available: bool
    """Whether ``POST /stories/{id}/untruncate`` has a truncation to revert (by reactivation,
    or by a swap when newer turns exist)."""
    dormant_chains: list[DormantChainBody]
    variant_chains: list[VariantChainBody]
    """Every chain of the story, as ``GET /stories/{id}/variants`` lists them (#66)."""


class StoryList(BaseModel):
    items: list[StorySummaryBody]


class StatusBody(MutationResponse):
    status: StoryStatus


class EffectivePolicyBody(BaseModel):
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
    settings: dict[str, Any]
    effective_policies: list[EffectivePolicyBody]
    inactive_policies: list[EffectivePolicyBody]
    guardrail_calls: GuardrailCallsBody
    effective_budget: EffectiveBudgetBody


class StorySettingsWriteBody(MutationResponse):
    """The settings after the write, in the read's shape, with the story's ``config_revision``
    (moved by a write that changed something) and its unchanged ``timeline_revision``."""

    settings: dict[str, Any]
    effective_policies: list[EffectivePolicyBody]
    inactive_policies: list[EffectivePolicyBody]
    guardrail_calls: GuardrailCallsBody
    effective_budget: EffectiveBudgetBody
    config_revision: int


SETTINGS_PATCH_DESCRIPTION = (
    "Change the story's settings with a JSON merge patch (RFC 7396): a key present sets that "
    "setting, `null` resets it to its default, and `generation_settings` merges the same way. "
    "`turn_concurrency` (`reject` | `queue`), `turn_queue_max` (1-50; `null`: the deployment's "
    "default, 5), `turn_deadline` (seconds), `max_model_calls_per_turn` (1-50; `null`: the "
    "deployment's default, else `9 + G`), `default_output_viewpoint` (`character:<id>` of the "
    "cast, never `omniscient`), `regenerate_retry_budget`, `generation_settings` and "
    "`lite_window_messages`. An unknown key is 422. `policies` (optional) upserts the story's "
    "own policies; an item that would relax a policy refuses the whole request with 422 "
    "`cannot_relax` before anything is written.\n\nEvery setting is prospective: it applies to "
    "requests that arrive after the write, never to a turn already running. A write that changes "
    "something moves the story's `config_revision`."
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
    "`Prefer: respond-async` answers 202 at once)."
)


class IngestedEventBody(BaseModel):
    client_event_id: str
    operation_id: str
    turn: int
    replayed: bool


class IngestResponse(MutationResponse):
    """One result per event, in request order, and the story's revision after the call."""

    results: list[IngestedEventBody]


def _character(character: CharacterView) -> OmniscientCharacterBody | CharacterBody:
    if character.true_age is not None:
        return OmniscientCharacterBody(
            id=character.id,
            name=character.name,
            is_player=character.is_player,
            true_age=character.true_age,
        )
    return CharacterBody(id=character.id, name=character.name, is_player=character.is_player)


@router.post("", status_code=http_status.HTTP_201_CREATED, response_model=StoryCreatedBody)
async def create_story(
    body: StorySpec, response: Response, ctx: ContextDep, engine: EngineDep
) -> StoryCreatedBody:
    created = await engine.create_story(ctx, body)
    response.headers["Location"] = f"/stories/{created.story_id}"
    answer = StoryCreatedBody(
        story_id=created.story_id,
        characters=[CharacterRefBody(id=c.id, name=c.name) for c in created.characters],
        timeline_revision=created.timeline_revision,
        content_rating=RatingBody.of(created.content_rating),
    )
    return answered(answer, ctx, response, replayed=created.replayed)


@router.get("", response_model=StoryList)
async def list_stories(
    ctx: ContextDep, engine: EngineDep, status: StoryStatus | None = None
) -> StoryList:
    found = await engine.list_stories(ctx, status=status)
    return StoryList(items=[StorySummaryBody(**StorySummaryBody.fields_of(s)) for s in found])


@router.get("/{story_id}", response_model=StoryBody)
async def get_story(story_id: str, ctx: ContextDep, engine: EngineDep) -> StoryBody:
    story = await engine.get_story(ctx, story_id)
    undo = await engine.undo_state(ctx, story_id)
    chains = await engine.variants(ctx, story_id)
    return StoryBody(
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


@router.post(
    "/{story_id}/ingest",
    status_code=http_status.HTTP_201_CREATED,
    response_model=IngestResponse,
    summary="Record consumer events without generating",
    description=INGEST_DESCRIPTION,
)
async def ingest(
    story_id: str,
    body: IngestRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    respond_async: RespondAsyncDep,
) -> IngestResponse:
    result = await engine.ingest(ctx, story_id, body, respond_async=respond_async)
    if not result.created:
        response.status_code = http_status.HTTP_200_OK
    answer = IngestResponse(
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
    )
    return answered(answer, ctx, response, replayed=result.replayed)


@router.get("/{story_id}/settings", response_model=StorySettingsBody)
async def story_settings(story_id: str, ctx: ContextDep, engine: EngineDep) -> StorySettingsBody:
    settings, guardrails = await engine.story_settings(ctx, story_id)
    return StorySettingsBody(
        settings=settings.model_dump(mode="json"),
        effective_policies=[EffectivePolicyBody.of(p, guardrails) for p in guardrails.active],
        inactive_policies=[EffectivePolicyBody.of(p, guardrails) for p in guardrails.inactive],
        guardrail_calls=GuardrailCallsBody(
            input=guardrails.calls_input, output=guardrails.calls_output
        ),
        effective_budget=EffectiveBudgetBody.of(await engine.effective_budget(ctx, story_id)),
    )


@router.get("/{story_id}/config-changes", response_model=ConfigChangesBody)
async def story_config_changes(
    story_id: str,
    ctx: ContextDep,
    engine: EngineDep,
    setting: Annotated[list[str] | None, Query()] = None,
    change_class: Annotated[ConfigClass | None, Query(alias="class")] = None,
    since: datetime | None = None,
    after: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConfigChangesBody:
    """The story's configuration changes (settings, story and character policies), newest
    first (#64); ``next`` pages on (``after``)."""
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
    summary="Change the story's settings",
    description=SETTINGS_PATCH_DESCRIPTION,
)
async def update_story_settings(
    story_id: str,
    body: Annotated[dict[str, Any], Body()],
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StorySettingsWriteBody:
    write = await engine.update_story_settings(ctx, story_id, body)
    _, guardrails = await engine.story_settings(ctx, story_id)
    answer = StorySettingsWriteBody(
        settings=write.settings,
        effective_policies=[EffectivePolicyBody.of(p, guardrails) for p in guardrails.active],
        inactive_policies=[EffectivePolicyBody.of(p, guardrails) for p in guardrails.inactive],
        guardrail_calls=GuardrailCallsBody(
            input=guardrails.calls_input, output=guardrails.calls_output
        ),
        effective_budget=EffectiveBudgetBody.of(await engine.effective_budget(ctx, story_id)),
        config_revision=write.config_revision,
        timeline_revision=write.timeline_revision,
    )
    return answered(answer, ctx, response, replayed=write.replayed)


@router.post("/{story_id}/archive", response_model=StatusBody)
async def archive_story(
    story_id: str, response: Response, ctx: ContextDep, engine: EngineDep
) -> StatusBody:
    change = await engine.archive_story(ctx, story_id)
    answer = StatusBody(status=change.status, timeline_revision=change.timeline_revision)
    return answered(answer, ctx, response, replayed=change.replayed)


@router.post("/{story_id}/unarchive", response_model=StatusBody)
async def unarchive_story(
    story_id: str, response: Response, ctx: ContextDep, engine: EngineDep
) -> StatusBody:
    change = await engine.unarchive_story(ctx, story_id)
    answer = StatusBody(status=change.status, timeline_revision=change.timeline_revision)
    return answered(answer, ctx, response, replayed=change.replayed)


@router.delete("/{story_id}", status_code=http_status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_story(story_id: str, ctx: ContextDep, engine: EngineDep) -> Response:
    """Delete the story. A retry under the same ``Idempotency-Key`` is a 204 with
    ``Idempotent-Replay: true`` (the deletion is not repeated)."""
    deleted = await engine.delete_story(ctx, story_id)
    return no_content(ctx, replayed=deleted.replayed)
