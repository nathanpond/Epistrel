"""Story lifecycle over REST (FR-ADMIN-1): create, read, list, archive, unarchive, delete.

- ``POST /stories``: a blank story with its cast (each character with a required true age),
  rating and settings; 201 with ``Location``.
- ``GET /stories`` (``?status=``) and ``GET /stories/{id}``; a character's ``true_age`` appears
  only under ``?viewpoint=omniscient`` (FR-CAT-12).
- ``POST /stories/{id}/archive`` and ``…/unarchive``: control operations, idempotent (200).
- ``DELETE /stories/{id}``: removes every row of the story and nothing else (204).
- ``GET /stories/{id}/settings``: the settings with defaults filled in, and the story's
  guardrails — ``effective_policies`` (scope, source, applies_to, age_condition, kind),
  ``inactive_policies`` (age-conditioned ones whose condition is false for the cast) and
  ``guardrail_calls`` — so the model-call cost of guardrails is predictable (FR-GUARD-9). #63
  adds ``PATCH``.

``POST /stories`` gates every character definition through the input gate first (#56).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.responses import MutationResponse
from epistrel.core.enums import StoryStatus
from epistrel.core.policies.model import Policy, PolicyKind, Scope, Target
from epistrel.core.stories import CharacterView, RatingRef, StorySpec, StoryView
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


class StoryBody(StorySummaryBody):
    characters: list[OmniscientCharacterBody | CharacterBody]
    settings: dict[str, Any]


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


class StorySettingsBody(BaseModel):
    settings: dict[str, Any]
    effective_policies: list[EffectivePolicyBody]
    inactive_policies: list[EffectivePolicyBody]
    guardrail_calls: GuardrailCallsBody


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
    return StoryCreatedBody(
        story_id=created.story_id,
        characters=[CharacterRefBody(id=c.id, name=c.name) for c in created.characters],
        timeline_revision=created.timeline_revision,
        content_rating=RatingBody.of(created.content_rating),
    )


@router.get("", response_model=StoryList)
async def list_stories(
    ctx: ContextDep, engine: EngineDep, status: StoryStatus | None = None
) -> StoryList:
    found = await engine.list_stories(ctx, status=status)
    return StoryList(items=[StorySummaryBody(**StorySummaryBody.fields_of(s)) for s in found])


@router.get("/{story_id}", response_model=StoryBody)
async def get_story(story_id: str, ctx: ContextDep, engine: EngineDep) -> StoryBody:
    story = await engine.get_story(ctx, story_id)
    return StoryBody(
        **StorySummaryBody.fields_of(story),
        characters=[_character(c) for c in story.characters],
        settings=story.settings,
    )


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
    )


@router.post("/{story_id}/archive", response_model=StatusBody)
async def archive_story(story_id: str, ctx: ContextDep, engine: EngineDep) -> StatusBody:
    change = await engine.archive_story(ctx, story_id)
    return StatusBody(status=change.status, timeline_revision=change.timeline_revision)


@router.post("/{story_id}/unarchive", response_model=StatusBody)
async def unarchive_story(story_id: str, ctx: ContextDep, engine: EngineDep) -> StatusBody:
    change = await engine.unarchive_story(ctx, story_id)
    return StatusBody(status=change.status, timeline_revision=change.timeline_revision)


@router.delete("/{story_id}", status_code=http_status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_story(story_id: str, ctx: ContextDep, engine: EngineDep) -> Response:
    await engine.delete_story(ctx, story_id)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
