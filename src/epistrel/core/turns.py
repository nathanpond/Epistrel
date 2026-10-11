"""A Lite turn in Core (#57): what its prompt is assembled from, and the beats it records.

``read_turn_inputs`` runs inside ``Operation.begin``'s REPEATABLE READ snapshot, so the inputs
the prompt is built from are exactly the ones the read set hashed (FR-CONC-2,4). Every input is
the Engine's own record (FR-RESP-2): the story rating's current prompt guidance (FR-RATE-4),
the latest custom instructions and pinned memories, the active rolling-summary segments, the
cast's definitions, and the latest ``window_messages`` transcript rows (FR-LITE-1). Nothing the
consumer sends as scene context is read here (FR-RESP-4).

``stage_turn`` stages the turn's beats: the actor's ``say``/``do`` as a ``player_input`` event
at beat 1 (FR-RESP-9) and the reply as a ``message_rendered`` event at beat 2, so every input
and final message is in the transcript with its position (FR-PROF-11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import read_lite_defaults
from epistrel.core.enums import Authority, Membership, OperationStatus, Provenance
from epistrel.core.errors import StoryNotFound
from epistrel.core.events import Operation
from epistrel.core.events.types import MessageRendered, PlayerInput
from epistrel.core.ratings import story_guidance
from epistrel.core.settings_schema import GenerationSettings, StorySettings
from epistrel.core.tables import characters, events, operations, stories, transcript_messages

INPUT_BEAT = 1
REPLY_BEAT = 2
PROMPT_CHANNELS = ("message", "narration")
"""Transcript channels a Lite prompt carries; private channels never enter it."""


@dataclass(frozen=True, slots=True)
class CastEntry:
    """A character as a turn sees it. The true age is not here: it never enters a prompt."""

    id: str
    name: str
    definition: str
    is_player: bool


@dataclass(frozen=True, slots=True)
class StoryCast:
    """What a turn's prechecks read: the story's status, settings and cast in order."""

    status: str
    settings: StorySettings
    cast: tuple[CastEntry, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.cast)

    def by_id(self) -> dict[str, CastEntry]:
        return {c.id: c for c in self.cast}


@dataclass(frozen=True, slots=True)
class WindowRow:
    """One transcript row of the verbatim window, oldest first."""

    role: Literal["user", "assistant"]
    kind: str
    actor_character_id: str | None
    render_character_ids: tuple[str, ...]
    text: str
    say: str | None
    do: str | None


@dataclass(frozen=True, slots=True)
class TurnInputs:
    """Everything a Lite prompt is assembled from, read in one snapshot."""

    guidance: str
    instructions: str
    pins: tuple[str, ...]
    segments: tuple[str, ...]
    cast: tuple[CastEntry, ...]
    window: tuple[WindowRow, ...]
    deployment_generation: GenerationSettings
    """The deployment's Lite generation defaults (FR-LITE-4)."""
    story_generation: GenerationSettings | None
    """The story's override; the request's own ``prompt_params`` go on top, per request."""
    window_messages: int


def _cast_query(story_id: str) -> Any:
    return (
        select(characters.c.id, characters.c.name, characters.c.definition, characters.c.is_player)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )


def _cast(rows: Any) -> tuple[CastEntry, ...]:
    return tuple(
        CastEntry(id=r.id, name=r.name, definition=r.definition, is_player=bool(r.is_player))
        for r in rows
    )


async def read_story_cast(db: AsyncEngine, story_id: str) -> StoryCast:
    """The story's status, settings and cast; 404 ``not_found`` for an unknown story."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(stories.c.status, stories.c.settings).where(stories.c.id == story_id)
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        cast = _cast((await conn.execute(_cast_query(story_id))).all())
    return StoryCast(status=row.status, settings=StorySettings.from_row(row.settings), cast=cast)


def _active_committed(story_id: str) -> Any:
    return and_(
        operations.c.story_id == story_id,
        operations.c.status == OperationStatus.COMMITTED.value,
        operations.c.membership == Membership.ACTIVE.value,
    )


async def _payloads(conn: AsyncConnection, story_id: str, event_type: str) -> list[Any]:
    found = await conn.scalars(
        select(events.c.payload)
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id, events.c.type == event_type, _active_committed(story_id)
        )
        .order_by(events.c.seq)
    )
    return list(found)


async def _window(conn: AsyncConnection, story_id: str, limit: int) -> tuple[WindowRow, ...]:
    t = transcript_messages
    found = await conn.execute(
        select(
            t.c.kind,
            t.c.actor_character_id,
            t.c.render_character_ids,
            t.c.text,
            events.c.payload,
        )
        .join(events, events.c.id == t.c.message_id)
        .where(
            t.c.story_id == story_id,
            t.c.membership == Membership.ACTIVE.value,
            t.c.channel.in_(PROMPT_CHANNELS),
        )
        .order_by(
            t.c.narrative_turn.desc().nulls_last(),
            t.c.turn_variant.desc().nulls_last(),
            t.c.seq.desc(),
        )
        .limit(limit)
    )
    rows: list[WindowRow] = []
    for r in reversed(found.all()):
        payload = r.payload or {}
        player = r.kind == "player_input"
        rows.append(
            WindowRow(
                role="user" if player else "assistant",
                kind=r.kind,
                actor_character_id=r.actor_character_id,
                render_character_ids=tuple(r.render_character_ids or ()),
                text=r.text,
                say=payload.get("say") if player else None,
                do=payload.get("do") if player else None,
            )
        )
    return tuple(rows)


async def read_turn_inputs(conn: AsyncConnection, story_id: str) -> TurnInputs:
    """The prompt inputs of the next turn of ``story_id`` (call inside the read snapshot)."""
    defaults = await read_lite_defaults(conn)
    raw_settings = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    settings = StorySettings.from_row(raw_settings)
    window_messages = settings.lite_window_messages or defaults.lite_window_messages
    guidance = await story_guidance(conn, story_id)
    instructions = await _payloads(conn, story_id, "custom_instructions_set")
    pins = await _payloads(conn, story_id, "pinned_memories_set")
    segments = await _payloads(conn, story_id, "lite_summary_segment")
    cast = _cast((await conn.execute(_cast_query(story_id))).all())
    return TurnInputs(
        guidance=guidance.prompt_guidance or "",
        instructions=str(instructions[-1].get("text", "")) if instructions else "",
        pins=tuple(str(m) for m in pins[-1].get("memories", [])) if pins else (),
        segments=tuple(str(s.get("text", "")) for s in segments),
        cast=cast,
        window=await _window(conn, story_id, window_messages),
        deployment_generation=defaults.lite_generation_defaults,
        story_generation=settings.generation_settings,
        window_messages=window_messages,
    )


def joined_input(say: str | None, do: str | None) -> str:
    """``say`` and ``do`` as one text, joined with a newline: what the gate checks and the
    transcript shows."""
    return "\n".join(part for part in (say, do) if part)


def stage_turn(
    op: Operation,
    *,
    actor_character_id: str,
    say: str | None,
    do: str | None,
    reply: str,
    render_character_ids: list[str],
    model: str | None,
    prompt_hash: str,
    finish_reason: str | None,
) -> None:
    """Stage the input beat (1) and the reply beat (2) on the turn operation."""
    op.append(
        "player_input",
        PlayerInput(text=joined_input(say, do), say=say, do=do),
        provenance=Provenance.PLAYER,
        character_id=actor_character_id,
        beat=INPUT_BEAT,
    )
    op.append(
        "message_rendered",
        MessageRendered(
            text=reply,
            render_character_ids=tuple(render_character_ids),
            model=model,
            prompt_hash=prompt_hash,
            finish_reason=finish_reason,
        ),
        provenance=Provenance.ENGINE,
        authority=Authority.AUTHORITATIVE,
        character_id=render_character_ids[0] if len(render_character_ids) == 1 else None,
        beat=REPLY_BEAT,
    )


__all__ = [
    "INPUT_BEAT",
    "REPLY_BEAT",
    "CastEntry",
    "StoryCast",
    "TurnInputs",
    "WindowRow",
    "joined_input",
    "read_story_cast",
    "read_turn_inputs",
    "stage_turn",
]
