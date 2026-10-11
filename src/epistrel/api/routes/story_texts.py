"""Story-level text over REST (FR-LITE-3; #60): pinned memories and custom instructions.

- ``GET /stories/{id}/pinned-memories`` → ``{items: [{id, text}], timeline_revision}``.
- ``PUT /stories/{id}/pinned-memories`` with ``{items: [{id?, text}]}`` replaces the set.
- ``GET /stories/{id}/custom-instructions`` → ``{text, timeline_revision}`` (``""`` if never
  set).
- ``PUT /stories/{id}/custom-instructions`` with ``{text}`` replaces it; ``""`` clears it.

A PUT echoes the stored value, the edit's ``operation_id`` (``null`` when nothing changed) and
the story's revision. Thin handlers over ``Engine``; the rules are in
``epistrel.core.story_texts``. Reads are viewpoint-free.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.story_texts import (
    CustomInstructions,
    CustomInstructionsRequest,
    PinnedMemories,
    PinnedMemoriesRequest,
)
from epistrel.orchestrator.models.metrics import ModelCall

router = APIRouter(prefix="/stories", tags=["stories"])

EDIT_RULES = (
    "\n\nEvery text is NFC-normalised and trimmed. The edit passes the input gate before "
    "anything is recorded, is recorded as an event, and bumps the story's timeline revision: "
    "a turn generating while it commits is regenerated against the new value before it "
    "commits. An edit that changes nothing writes nothing (`operation_id` is `null`). "
    "The answer reports what the input gate spent: `model_calls` and `elapsed_ms`. "
    "`Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional."
)


class PinnedItemBody(BaseModel):
    """One pinned memory: its server-minted id and its text."""

    id: str
    text: str


class PinnedMemoriesBody(BaseModel):
    """The story's pinned memories, in prompt order."""

    items: list[PinnedItemBody]
    timeline_revision: int

    @classmethod
    def of(cls, pins: PinnedMemories) -> PinnedMemoriesBody:
        return cls(
            items=[PinnedItemBody(id=i.id, text=i.text) for i in pins.items],
            timeline_revision=pins.timeline_revision,
        )


class PinnedMemoriesWritten(MutationResponse):
    """The stored pinned memories after the edit, the edit's operation (``null`` when nothing
    changed), the story's revision, and what the input gate spent (``model_calls``,
    ``elapsed_ms``)."""

    items: list[PinnedItemBody]
    operation_id: str | None
    model_calls: list[ModelCall]
    elapsed_ms: int = Field(ge=0)

    @classmethod
    def of(cls, pins: PinnedMemories) -> PinnedMemoriesWritten:
        return cls(
            items=[PinnedItemBody(id=i.id, text=i.text) for i in pins.items],
            operation_id=pins.operation_id,
            timeline_revision=pins.timeline_revision,
            model_calls=[ModelCall.model_validate(call) for call in pins.model_calls],
            elapsed_ms=pins.elapsed_ms,
        )


class CustomInstructionsBody(BaseModel):
    """The story's custom instructions: standing guidance for the model, never facts."""

    text: str
    timeline_revision: int

    @classmethod
    def of(cls, found: CustomInstructions) -> CustomInstructionsBody:
        return cls(text=found.text, timeline_revision=found.timeline_revision)


class CustomInstructionsWritten(MutationResponse):
    """The stored custom instructions after the edit (see ``PinnedMemoriesWritten``)."""

    text: str
    operation_id: str | None
    model_calls: list[ModelCall]
    elapsed_ms: int = Field(ge=0)

    @classmethod
    def of(cls, written: CustomInstructions) -> CustomInstructionsWritten:
        return cls(
            text=written.text,
            operation_id=written.operation_id,
            timeline_revision=written.timeline_revision,
            model_calls=[ModelCall.model_validate(call) for call in written.model_calls],
            elapsed_ms=written.elapsed_ms,
        )


@router.get(
    "/{story_id}/pinned-memories",
    response_model=PinnedMemoriesBody,
    operation_id="pinned_memories",
    summary="Read the story's pinned memories",
)
async def get_pinned_memories(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> PinnedMemoriesBody:
    return PinnedMemoriesBody.of(await engine.pinned_memories(ctx, story_id))


@router.put(
    "/{story_id}/pinned-memories",
    response_model=PinnedMemoriesWritten,
    operation_id="set_pinned_memories",
    summary="Replace the story's pinned memories",
    description=(
        "Pinned memories are story-level text included in every prompt, in order. Items keep "
        "their server-minted `id` when the request names it (an unknown id is 422); an item "
        "without one keeps the id of a stored item with the same text, or gets a new one. "
        "Duplicate ids or texts, a blank item, an item over 2,000 characters, or more items "
        "than the deployment's `max_pinned_memories` are 422." + EDIT_RULES
    ),
)
async def put_pinned_memories(
    story_id: StoryId,
    body: PinnedMemoriesRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> PinnedMemoriesWritten:
    pins = await engine.set_pinned_memories(ctx, story_id, body)
    return answered(PinnedMemoriesWritten.of(pins), ctx, response, replayed=pins.replayed)


@router.get(
    "/{story_id}/custom-instructions",
    response_model=CustomInstructionsBody,
    operation_id="custom_instructions",
    summary="Read the story's custom instructions",
)
async def get_custom_instructions(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> CustomInstructionsBody:
    return CustomInstructionsBody.of(await engine.custom_instructions(ctx, story_id))


@router.put(
    "/{story_id}/custom-instructions",
    response_model=CustomInstructionsWritten,
    operation_id="set_custom_instructions",
    summary="Replace the story's custom instructions",
    description=(
        "Custom instructions are standing guidance included in every prompt; they are never "
        'facts. `{text: ""}` clears them. Text longer than the deployment\'s '
        "`max_custom_instructions_chars` is 422." + EDIT_RULES
    ),
)
async def put_custom_instructions(
    story_id: StoryId,
    body: CustomInstructionsRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CustomInstructionsWritten:
    written = await engine.set_custom_instructions(ctx, story_id, body)
    answer = CustomInstructionsWritten.of(written)
    return answered(answer, ctx, response, replayed=written.replayed)
