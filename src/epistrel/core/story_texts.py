"""Pinned memories and custom instructions: story-level text in every Lite prompt (FR-LITE-3).

Both are edited by replacing the whole value: ``PUT /stories/{id}/pinned-memories`` with
``{items: [{id?, text}]}`` and ``PUT /stories/{id}/custom-instructions`` with ``{text}``.

- **Text rule.** Every text is NFC-normalised and trimmed; lengths are code points after that.
  A pinned item is non-blank and at most ``PINNED_ITEM_MAX_CHARS``; ``{text: ""}`` clears the
  instructions, and a story that never set them reads ``""``.
- **Ids.** Items get server-minted ULIDs. An item naming an ``id`` keeps it (an id the story
  does not have is 422); an item without one takes the id of a stored item with the same text
  that no other item claims, or a new id. Duplicate ids or duplicate texts in one body are 422.
- **Caps** (deployment settings, prospective): ``max_pinned_memories`` and
  ``max_custom_instructions_chars``; exceeding one is 422 ``validation_error`` with
  ``details.{cap, limit, actual}``.
- **Order of checks** (#44/#56): the body (422, before the handler) → the story (404) → ids,
  duplicates and caps (422) → archived (409) → the expected revision (409) → no change → the
  input gate over every text (``gate``, passed in by the Engine, which owns the Orchestrator)
  → one ``control`` operation.
- **The write.** An edit that changes nothing writes nothing (no operation, no gate). Otherwise
  one ``pinned_memories_set`` / ``custom_instructions_set`` event carries the full ordered value
  and the ``previous`` one; it bumps the timeline revision (FR-CONC-1) and, being a member of
  the Lite read set (#47), makes a turn generating meanwhile regenerate before it commits. It
  is not a control action, so that turn regenerates rather than abandons. Reordering is an
  edit. The ``story_texts`` projection holds the current values. Custom instructions are
  guidance in the prompt, never facts.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.concurrency import (
    Commit,
    Decision,
    ReadSet,
    Regenerate,
    RevalidationState,
    check_expected_revision,
    hash_member,
    run_with_revalidation,
)
from epistrel.core.config_changes import read_story_text_caps
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.errors import InvalidRequest, StoryNotFound
from epistrel.core.events import Operation
from epistrel.core.events.types import CustomInstructionsSet, PinnedItem, PinnedMemoriesSet
from epistrel.core.ids import new_ulid
from epistrel.core.settings_schema import PINNED_ITEM_MAX_CHARS, StoryTextCaps
from epistrel.core.stories import require_writable
from epistrel.core.tables import stories, story_texts

ITEM_ID_MAX = 64
EDIT_RETRY_BUDGET = 3
"""How often an edit re-resolves after a concurrent edit changed the stored value under it."""

Gate = Callable[[Sequence[tuple[str, str]]], Awaitable[None]]
"""The input gate over ``(locator, text)`` pairs; raises 422 ``guardrail_violation``."""


def normalise(value: str) -> str:
    """NFC-normalised and trimmed (#60's text rule, shared with ingest's)."""
    return unicodedata.normalize("NFC", value).strip()


# -- request models -------------------------------------------------------------------------


class PinnedItemIn(BaseModel):
    """One pinned memory in a request: an existing item's ``id`` to keep it, and its text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = Field(default=None, min_length=1, max_length=ITEM_ID_MAX)
    text: str

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        value = normalise(value)
        if not value:
            raise ValueError("a pinned memory must not be blank")
        if len(value) > PINNED_ITEM_MAX_CHARS:
            raise ValueError(f"a pinned memory is at most {PINNED_ITEM_MAX_CHARS} characters")
        return value


class PinnedMemoriesRequest(BaseModel):
    """The story's pinned memories, in order, replacing the current set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[PinnedItemIn]


class CustomInstructionsRequest(BaseModel):
    """The story's custom instructions, replacing the current text; ``""`` clears them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        return normalise(value)


# -- results --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PinnedMemories:
    """The stored pinned memories and the story's revision; ``operation_id`` is the edit that
    wrote them (``None`` for a read or an edit that changed nothing)."""

    items: tuple[PinnedItem, ...]
    timeline_revision: int
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class CustomInstructions:
    """The stored custom instructions and the story's revision (see ``PinnedMemories``)."""

    text: str
    timeline_revision: int
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Stored:
    pinned: tuple[PinnedItem, ...]
    instructions: str
    revision: int


async def _read_stored(conn: AsyncConnection, story_id: str) -> _Stored:
    found = (
        await conn.execute(
            select(
                stories.c.timeline_revision,
                story_texts.c.pinned,
                story_texts.c.custom_instructions,
            )
            .outerjoin(story_texts, story_texts.c.story_id == stories.c.id)
            .where(stories.c.id == story_id)
        )
    ).one_or_none()
    if found is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return _Stored(
        pinned=tuple(PinnedItem.model_validate(item) for item in found.pinned or ()),
        instructions=found.custom_instructions or "",
        revision=int(found.timeline_revision),
    )


async def _stored(db: AsyncEngine, story_id: str) -> _Stored:
    async with db.connect() as conn:
        return await _read_stored(conn, story_id)


async def read_pinned_memories(db: AsyncEngine, story_id: str) -> PinnedMemories:
    """The story's pinned memories in order, with its revision; 404 for an unknown story."""
    stored = await _stored(db, story_id)
    return PinnedMemories(stored.pinned, stored.revision)


async def read_custom_instructions(db: AsyncEngine, story_id: str) -> CustomInstructions:
    """The story's custom instructions (``""`` when never set), with its revision."""
    stored = await _stored(db, story_id)
    return CustomInstructions(stored.instructions, stored.revision)


# -- checks ---------------------------------------------------------------------------------


def _over_cap(cap: str, limit: int, actual: int, field: str) -> InvalidRequest:
    return InvalidRequest(
        f"{cap} is {limit}; the request has {actual}",
        {"field": field, "cap": cap, "limit": limit, "actual": actual},
    )


def check_pinned_cap(items: Sequence[PinnedItemIn], caps: StoryTextCaps) -> None:
    if len(items) > caps.max_pinned_memories:
        raise _over_cap("max_pinned_memories", caps.max_pinned_memories, len(items), "body.items")


def check_instructions_cap(text: str, caps: StoryTextCaps) -> None:
    if len(text) > caps.max_custom_instructions_chars:
        raise _over_cap(
            "max_custom_instructions_chars",
            caps.max_custom_instructions_chars,
            len(text),
            "body.text",
        )


def resolve_items(
    requested: Sequence[PinnedItemIn], current: Sequence[PinnedItem]
) -> tuple[PinnedItem, ...]:
    """The stored items a request asks for (see the module's id rule); 422 at
    ``body.items[n].id`` / ``.text`` for an unknown or duplicate id or a duplicate text."""
    known = {item.id for item in current}
    ids: set[str] = set()
    texts: set[str] = set()
    for n, item in enumerate(requested):
        if item.id is not None:
            if item.id in ids:
                raise InvalidRequest(
                    "an item id appears twice", {"field": f"body.items[{n}].id", "id": item.id}
                )
            if item.id not in known:
                raise InvalidRequest(
                    "the story has no pinned memory with this id",
                    {"field": f"body.items[{n}].id", "id": item.id},
                )
            ids.add(item.id)
        if item.text in texts:
            raise InvalidRequest(
                "a pinned memory appears twice", {"field": f"body.items[{n}].text"}
            )
        texts.add(item.text)
    unclaimed = {item.text: item.id for item in current if item.id not in ids}
    return tuple(
        PinnedItem(id=item.id or unclaimed.pop(item.text, None) or new_ulid(), text=item.text)
        for item in requested
    )


# -- writes ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StoredTexts:
    """The edit's read set: the stored row, ids included (``previous`` must be exact)."""

    async def hashes(self, conn: AsyncConnection, story_id: str) -> ReadSet:
        stored = await _read_stored(conn, story_id)
        return {
            "story_texts": hash_member(
                [[i.id, i.text] for i in stored.pinned] + [stored.instructions]
            )
        }


def _decide(state: RevalidationState) -> Decision:
    """Commit unless a concurrent edit changed the stored texts: then re-resolve. A turn or a
    timeline edit meanwhile changes nothing the edit read, so it commits."""
    if state.touched:
        return Regenerate(reason="read_set_touched", touched=state.touched)
    return Commit()


async def _edit(
    db: AsyncEngine,
    story_id: str,
    *,
    principal: str,
    idempotency_key: str | None,
    stage: Callable[[Operation, _Stored], None],
) -> Operation:
    op = Operation(
        db,
        story_id=story_id,
        kind=OperationKind.CONTROL,
        principal=principal,
        provenance=Provenance.CONSUMER,
        bumps_revision=True,
        idempotency_key=idempotency_key,
    )

    async def begin(conn: AsyncConnection) -> _Stored:
        return await _read_stored(conn, story_id)

    async def generate(op: Operation, stored: Any) -> None:
        stage(op, stored)

    await run_with_revalidation(
        op,
        begin=begin,
        generate=generate,
        decide=_decide,
        budget=EDIT_RETRY_BUDGET,
        read_set=_StoredTexts(),
    )
    return op


async def set_pinned_memories(
    db: AsyncEngine,
    story_id: str,
    request: PinnedMemoriesRequest,
    *,
    principal: str,
    gate: Gate,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> PinnedMemories:
    """Replace the story's pinned memories (FR-LITE-3); see the module for the rules."""
    stored = await _stored(db, story_id)
    check_pinned_cap(request.items, await read_story_text_caps(db))
    resolved = resolve_items(request.items, stored.pinned)
    await require_writable(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    if resolved == stored.pinned:
        return PinnedMemories(stored.pinned, revision)
    if resolved:
        await gate([(f"items[{n}].text", item.text) for n, item in enumerate(resolved)])
    written: list[tuple[PinnedItem, ...]] = []

    def stage(op: Operation, current: _Stored) -> None:
        items = resolve_items(request.items, current.pinned)
        written[:] = [items]
        op.append("pinned_memories_set", PinnedMemoriesSet(items=items, previous=current.pinned))

    op = await _edit(
        db, story_id, principal=principal, idempotency_key=idempotency_key, stage=stage
    )
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return PinnedMemories(written[0], op.timeline_revision, op.operation_id)


async def set_custom_instructions(
    db: AsyncEngine,
    story_id: str,
    request: CustomInstructionsRequest,
    *,
    principal: str,
    gate: Gate,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
) -> CustomInstructions:
    """Replace the story's custom instructions (FR-LITE-3); ``""`` clears them."""
    stored = await _stored(db, story_id)
    check_instructions_cap(request.text, await read_story_text_caps(db))
    await require_writable(db, story_id)
    revision = await check_expected_revision(db, story_id, expected_revision)
    if request.text == stored.instructions:
        return CustomInstructions(stored.instructions, revision)
    if request.text:
        await gate([("text", request.text)])

    def stage(op: Operation, current: _Stored) -> None:
        op.append(
            "custom_instructions_set",
            CustomInstructionsSet(text=request.text, previous=current.instructions),
        )

    op = await _edit(
        db, story_id, principal=principal, idempotency_key=idempotency_key, stage=stage
    )
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return CustomInstructions(request.text, op.timeline_revision, op.operation_id)


__all__ = [
    "EDIT_RETRY_BUDGET",
    "CustomInstructions",
    "CustomInstructionsRequest",
    "Gate",
    "PinnedItemIn",
    "PinnedMemories",
    "PinnedMemoriesRequest",
    "check_instructions_cap",
    "check_pinned_cap",
    "normalise",
    "read_custom_instructions",
    "read_pinned_memories",
    "resolve_items",
    "set_custom_instructions",
    "set_pinned_memories",
]
