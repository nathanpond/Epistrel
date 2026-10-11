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

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.responses import MutationResponse
from epistrel.core.story_texts import (
    CustomInstructions,
    CustomInstructionsRequest,
    PinnedMemories,
    PinnedMemoriesRequest,
)

router = APIRouter(prefix="/stories", tags=["stories"])

EDIT_RULES = (
    "\n\nEvery text is NFC-normalised and trimmed. The edit passes the input gate before "
    "anything is recorded, is recorded as an event, and bumps the story's timeline revision: "
    "a turn generating while it commits is regenerated against the new value before it "
    "commits. An edit that changes nothing writes nothing (`operation_id` is `null`). "
    "`Idempotency-Key` and `X-Epistrel-Expected-Revision` are optional."
)


class PinnedItemBody(BaseModel):
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
    items: list[PinnedItemBody]
    operation_id: str | None


class CustomInstructionsBody(BaseModel):
    """The story's custom instructions: standing guidance for the model, never facts."""

    text: str
    timeline_revision: int


class CustomInstructionsWritten(MutationResponse):
    text: str
    operation_id: str | None


@router.get(
    "/{story_id}/pinned-memories",
    response_model=PinnedMemoriesBody,
    summary="Read the story's pinned memories",
)
async def get_pinned_memories(
    story_id: str, ctx: ContextDep, engine: EngineDep
) -> PinnedMemoriesBody:
    return PinnedMemoriesBody.of(await engine.pinned_memories(ctx, story_id))


@router.put(
    "/{story_id}/pinned-memories",
    response_model=PinnedMemoriesWritten,
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
    story_id: str, body: PinnedMemoriesRequest, ctx: ContextDep, engine: EngineDep
) -> PinnedMemoriesWritten:
    pins = await engine.set_pinned_memories(ctx, story_id, body)
    return PinnedMemoriesWritten(
        items=[PinnedItemBody(id=i.id, text=i.text) for i in pins.items],
        operation_id=pins.operation_id,
        timeline_revision=pins.timeline_revision,
    )


@router.get(
    "/{story_id}/custom-instructions",
    response_model=CustomInstructionsBody,
    summary="Read the story's custom instructions",
)
async def get_custom_instructions(
    story_id: str, ctx: ContextDep, engine: EngineDep
) -> CustomInstructionsBody:
    found = await engine.custom_instructions(ctx, story_id)
    return CustomInstructionsBody(text=found.text, timeline_revision=found.timeline_revision)


@router.put(
    "/{story_id}/custom-instructions",
    response_model=CustomInstructionsWritten,
    summary="Replace the story's custom instructions",
    description=(
        "Custom instructions are standing guidance included in every prompt; they are never "
        'facts. `{text: ""}` clears them. Text longer than the deployment\'s '
        "`max_custom_instructions_chars` is 422." + EDIT_RULES
    ),
)
async def put_custom_instructions(
    story_id: str, body: CustomInstructionsRequest, ctx: ContextDep, engine: EngineDep
) -> CustomInstructionsWritten:
    written: CustomInstructions = await engine.set_custom_instructions(ctx, story_id, body)
    return CustomInstructionsWritten(
        text=written.text,
        operation_id=written.operation_id,
        timeline_revision=written.timeline_revision,
    )
