"""A Lite turn in Core (#57): what its prompt is assembled from, and the beats it records.

``read_turn_inputs`` runs inside ``Operation.begin``'s REPEATABLE READ snapshot, so the inputs
the prompt is built from are exactly the ones the read set hashed (FR-CONC-2,4). Every input is
the Engine's own record (FR-RESP-2): the story rating's current prompt guidance (FR-RATE-4),
the custom instructions and pinned memories (the ``story_texts`` projection, #60), the
rolling-summary segments a prompt includes (``core.summary.included_segments``, #72, read
through the stored inclusion, ``read_included_segments``, #431; each as a
``Turns a-b:`` line), the cast's definitions, a scenario story's scenario text and director
notes (rendered from its snapshot on every turn, ``core.genesis.render``; #122), and every
**verbatim** transcript row: each active
row after the last turn the included segments cover, and never fewer than the latest
``window_messages`` rows (FR-LITE-1,2). The background summary job keeps the uncovered rows
near ``window_messages``; a row is never dropped from the prompt before a segment covers it.
Nothing the consumer sends as scene context is read here (FR-RESP-4).

**The configuration** (#113) is the story's effective configuration as the turn captured it at
its start (``core.profiles.effective``): ``memory.rolling_summary.window_messages`` is the
verbatim window, ``models.generation`` the generation settings (the profile's, with the story's
overrides; the request's ``prompt_params`` go on top, FR-LITE-4). With the rolling summary off
(``memory.rolling_summary.enabled``) the prompt has no summary and keeps the latest
``window_messages`` rows; with pinned memories off (``pins.enabled``) it has no pinned
memories.

``read_turn_inputs(before_turn=N)`` reads the inputs of a regenerated turn N (#66): everything
positioned at N or later (the variant being replaced) is left out, so the new variant is built
from what the old one was built from. ``read_regenerable`` reads what regenerating the head turn
reuses: its recorded ``player_input`` (actor, ``say``/``do``), render set, output viewpoint and
resolved generation settings.

``stage_turn`` stages the turn's beats: the actor's ``say``/``do`` as a ``player_input`` event
at beat 1 (FR-RESP-9) and the reply as a ``message_rendered`` event at beat 2, so every input
and final message is in the transcript with its position (FR-PROF-11).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import read_guardrail_settings
from epistrel.core.enums import Authority, Membership, OperationKind, OperationStatus, Provenance
from epistrel.core.errors import InvalidRequest, NothingToRegenerate, StoryNotFound
from epistrel.core.events import Operation
from epistrel.core.events.types import GuardrailOutputRejected, MessageRendered, PlayerInput
from epistrel.core.genesis.render import read_rendered
from epistrel.core.profiles.effective import StoryProfile, effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.ratings import story_guidance
from epistrel.core.settings_schema import (
    DEFAULT_SAFE_FALLBACK,
    GenerationSettings,
    StorySettings,
)
from epistrel.core.summary import PROMPT_CHANNELS, covered_to, read_included_segments
from epistrel.core.tables import (
    characters,
    events,
    operations,
    stories,
    story_texts,
    transcript_messages,
)

INPUT_BEAT = 1
REPLY_BEAT = 2
OPENING_FINISH = "opening"
"""``finish_reason`` of a scenario story's opening narration (#119's ``genesis.create``)."""


@dataclass(frozen=True, slots=True)
class CastEntry:
    """A character as a turn sees it. The true age is not here: it never enters a prompt."""

    id: str
    name: str
    definition: str
    is_player: bool
    safe_fallback_text: str | None = None
    """The character's own safe fallback line (#50, #68); never part of a prompt."""
    deflection_style: str | None = None
    """How the character refuses or changes the subject (FR-GUARD-7; #115): rendered with its
    author notes, so a reply regenerated after a guardrail violation stays in character."""


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
    opening: bool = False
    """The opening narration of a story created from a scenario (turn 1, stored verbatim,
    ``finish_reason: opening``; #119): the prompt labels it ``Narrator:`` (#122)."""


@dataclass(frozen=True, slots=True)
class TurnInputs:
    """Everything a Lite prompt is assembled from, read in one snapshot."""

    guidance: str
    instructions: str
    pins: tuple[str, ...]
    segments: tuple[str, ...]
    cast: tuple[CastEntry, ...]
    window: tuple[WindowRow, ...]
    generation: GenerationSettings
    """The story's generation settings: its effective configuration's ``models.generation``
    (FR-LITE-4); the request's own ``prompt_params`` go on top, per request."""
    window_messages: int
    default_safe_fallback: str = DEFAULT_SAFE_FALLBACK
    """The deployment's safe fallback line in force when the turn started (#68)."""
    scenario_text: str = ""
    """A story created from a scenario: its ``## Scenario`` block, rendered from the snapshot
    on every turn (``core.genesis.render``; #122). ``""`` for a blank story."""
    director_notes: str = ""
    """A story created from a scenario: its ``## Author notes (not facts)`` block (#122)."""


def _cast_query(story_id: str) -> Any:
    return (
        select(
            characters.c.id,
            characters.c.name,
            characters.c.definition,
            characters.c.is_player,
            characters.c.safe_fallback_text,
            characters.c.deflection_style,
        )
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )


def _cast(rows: Any) -> tuple[CastEntry, ...]:
    return tuple(
        CastEntry(
            id=r.id,
            name=r.name,
            definition=r.definition,
            is_player=bool(r.is_player),
            safe_fallback_text=r.safe_fallback_text,
            deflection_style=r.deflection_style,
        )
        for r in rows
    )


async def read_story_cast(db: AsyncEngine, story_id: str) -> StoryCast:
    """The story's status, settings and cast; 404 ``not_found`` for an unknown story."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(
                    stories.c.status,
                    stories.c.settings,
                    stories.c.turn_concurrency,
                    stories.c.turn_queue_max,
                ).where(stories.c.id == story_id)
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        cast = _cast((await conn.execute(_cast_query(story_id))).all())
    settings = StorySettings.from_row(
        row.settings, turn_concurrency=row.turn_concurrency, turn_queue_max=row.turn_queue_max
    )
    return StoryCast(status=row.status, settings=settings, cast=cast)


def _active_committed(story_id: str) -> Any:
    return and_(
        operations.c.story_id == story_id,
        operations.c.status == OperationStatus.COMMITTED.value,
        operations.c.membership == Membership.ACTIVE.value,
    )


def _before(column: Any, before_turn: int | None) -> Any:
    """Positioned before ``before_turn`` (or position-less); everything when ``None``."""
    if before_turn is None:
        return true()
    return or_(column.is_(None), column < before_turn)


async def transcript_rows(
    conn: AsyncConnection,
    story_id: str,
    *,
    after_turn: int = 0,
    through_turn: int | None = None,
    before_turn: int | None = None,
) -> tuple[WindowRow, ...]:
    """The story's active prompt-channel transcript rows positioned after ``after_turn`` (and
    through ``through_turn`` / before ``before_turn`` when given), oldest first: a prompt's
    verbatim window, or the rows a summary chunk is made from (#72)."""
    t = transcript_messages
    query = (
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
            _before(t.c.narrative_turn, before_turn),
        )
        .order_by(
            t.c.narrative_turn.nulls_first(),
            t.c.turn_variant.nulls_first(),
            t.c.seq,
        )
    )
    if after_turn > 0:
        query = query.where(t.c.narrative_turn > after_turn)
    if through_turn is not None:
        query = query.where(t.c.narrative_turn <= through_turn)
    found = await conn.execute(query)
    rows: list[WindowRow] = []
    for r in found.all():
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
                opening=not player and payload.get("finish_reason") == OPENING_FINISH,
            )
        )
    return tuple(rows)


async def _verbatim(
    conn: AsyncConnection,
    story_id: str,
    covered: int,
    window_messages: int,
    before_turn: int | None,
    *,
    summarised: bool = True,
) -> tuple[WindowRow, ...]:
    """Every row after the summary's ``covered`` turn, and never fewer than the latest
    ``window_messages`` rows (FR-LITE-1: after a rewind leaves few uncovered rows, the latest N
    are still verbatim, even where a segment also covers them). Without a rolling summary
    (``summarised`` false, #113) the latest ``window_messages`` rows only: nothing summarises
    the older ones, and the prompt drops them."""
    if not summarised:
        every = await transcript_rows(conn, story_id, before_turn=before_turn)
        return every[-window_messages:]
    uncovered = await transcript_rows(conn, story_id, after_turn=covered, before_turn=before_turn)
    if covered == 0 or len(uncovered) >= window_messages:
        return uncovered
    every = await transcript_rows(conn, story_id, before_turn=before_turn)
    return every[-window_messages:]


GENERATION_PURPOSE = "generation"
"""The model purpose a Lite turn's generation calls (``models.purposes`` keys it)."""


def generation_of(settings: ProfileSettings) -> GenerationSettings:
    """A configuration's generation settings as a layer of ``resolve_generation`` (FR-LITE-4):
    ``models.generation``, with ``response_length`` as the turn's ``length``, under the
    ``generation`` purpose's routed params (#325): its ``temperature`` and ``top_p``, and its
    ``max_tokens`` as a token-cap ``length``, win where set. A request's ``prompt_params`` still
    win over both."""
    generation = settings.models.generation
    route = settings.models.purposes.get(GENERATION_PURPOSE)
    params = route.params if route is not None else None

    def pick[T](routed: T | None, default: T) -> T:
        return routed if routed is not None else default

    return GenerationSettings(
        temperature=pick(params.temperature if params else None, generation.temperature),
        top_p=pick(params.top_p if params else None, generation.top_p),
        length=pick(params.max_tokens if params else None, generation.response_length),
    )


async def read_turn_inputs(
    conn: AsyncConnection,
    story_id: str,
    *,
    before_turn: int | None = None,
    configuration: StoryProfile | None = None,
) -> TurnInputs:
    """The prompt inputs of the next turn of ``story_id`` (call inside the read snapshot), or
    of a regenerated turn ``before_turn`` (only what is positioned before it), under
    ``configuration`` (the one the turn captured at its start; read now when ``None``)."""
    if configuration is None:
        configuration = await effective_configuration(conn, story_id)
    settings = configuration.settings
    guardrails = await read_guardrail_settings(conn)
    window_messages = settings.memory.rolling_summary.window_messages
    guidance = await story_guidance(conn, story_id)
    texts = (
        await conn.execute(
            select(story_texts.c.pinned, story_texts.c.custom_instructions).where(
                story_texts.c.story_id == story_id
            )
        )
    ).one_or_none()
    segments = (
        await read_included_segments(conn, story_id, before_turn=before_turn)
        if settings.memory.rolling_summary.enabled
        else ()
    )
    pins = (
        tuple(str(item["text"]) for item in texts.pinned)
        if texts is not None and settings.pins.enabled
        else ()
    )
    cast = _cast((await conn.execute(_cast_query(story_id))).all())
    scenario = await read_rendered(conn, story_id)
    return TurnInputs(
        guidance=guidance.prompt_guidance or "",
        instructions=texts.custom_instructions if texts is not None else "",
        pins=pins,
        segments=tuple(segment.label for segment in segments),
        cast=cast,
        window=await _verbatim(
            conn,
            story_id,
            covered_to(segments),
            window_messages,
            before_turn,
            summarised=settings.memory.rolling_summary.enabled,
        ),
        generation=generation_of(settings),
        window_messages=window_messages,
        default_safe_fallback=guardrails.default_safe_fallback,
        scenario_text=scenario.scenario_text,
        director_notes=scenario.director_notes,
    )


@dataclass(frozen=True, slots=True)
class RecordedTurn:
    """What regenerating the head turn reuses (#66): its position, its recorded input and the
    resolved values it ran with."""

    turn: int
    variant: int
    operation_id: str
    actor_character_id: str
    say: str | None
    do: str | None
    render_character_ids: tuple[str, ...]
    viewpoint: str | None
    generation_settings: dict[str, Any] | None


async def read_regenerable(db: AsyncEngine, story_id: str, turn: int | None = None) -> RecordedTurn:
    """The head turn of ``story_id`` as a regenerate reuses it. ``turn`` other than the head is
    422; a story with no turn, or a head turn without a reply or without an actor's input (an
    ingested player turn, a narration turn), is 422 ``nothing_to_regenerate``."""
    async with db.connect() as conn:
        head = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
        if head is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        latest = int(head) - 1
        if turn is not None and turn != latest:
            raise InvalidRequest(
                f"only the latest turn ({latest}) can be regenerated",
                {"field": "body.turn", "head": latest},
            )
        op = (
            await conn.execute(
                select(
                    operations.c.operation_id,
                    operations.c.turn_variant,
                    operations.c.viewpoint,
                    operations.c.config_revisions,
                ).where(
                    _active_committed(story_id),
                    operations.c.kind == OperationKind.TURN.value,
                    operations.c.narrative_turn == latest,
                )
            )
        ).one_or_none()
        found = (
            []
            if op is None
            else (
                await conn.execute(
                    select(events.c.type, events.c.payload, events.c.character_id)
                    .where(
                        events.c.operation_id == op.operation_id,
                        events.c.type.in_(("player_input", "message_rendered")),
                    )
                    .order_by(events.c.seq)
                )
            ).all()
        )
    inputs = [r for r in found if r.type == "player_input" and r.character_id is not None]
    replies = [r for r in found if r.type == "message_rendered"]
    if op is None or not inputs or not replies:
        raise NothingToRegenerate(
            "the latest turn has no reply to regenerate"
            if op is not None
            else "the story has no turn to regenerate",
            {"story_id": story_id, "turn": latest},
        )
    said = inputs[0].payload
    say, do = said.get("say"), said.get("do")
    if not (say or do):
        say = said.get("text")
    recorded = (op.config_revisions or {}).get("generation_settings")
    return RecordedTurn(
        turn=latest,
        variant=int(op.turn_variant),
        operation_id=op.operation_id,
        actor_character_id=inputs[0].character_id,
        say=say,
        do=do,
        render_character_ids=tuple(replies[-1].payload.get("render_character_ids") or ()),
        viewpoint=op.viewpoint,
        generation_settings=recorded if isinstance(recorded, dict) else None,
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
    prompt_hash: str | None,
    finish_reason: str | None,
    fallback_used: bool = False,
    rejected: Sequence[GuardrailOutputRejected] = (),
) -> None:
    """Stage the input beat (1) and the reply beat (2) on the turn operation; the texts an
    output policy rejected on the way (#68) go between them, as audit records of beat 2."""
    op.append(
        "player_input",
        PlayerInput(text=joined_input(say, do), say=say, do=do),
        provenance=Provenance.PLAYER,
        character_id=actor_character_id,
        beat=INPUT_BEAT,
    )
    for record in rejected:
        op.append(
            "guardrail_output_rejected",
            record,
            provenance=Provenance.ENGINE,
            character_id=None,
            beat=REPLY_BEAT,
        )
    op.append(
        "message_rendered",
        MessageRendered(
            text=reply,
            render_character_ids=tuple(render_character_ids),
            model=model,
            prompt_hash=prompt_hash,
            finish_reason=finish_reason,
            fallback_used=fallback_used,
        ),
        provenance=Provenance.ENGINE,
        authority=Authority.AUTHORITATIVE,
        # One combined reply, even for a single render character: who it voices is
        # ``render_character_ids``, and no one character owns it (#62).
        character_id=None,
        beat=REPLY_BEAT,
    )


__all__ = [
    "INPUT_BEAT",
    "REPLY_BEAT",
    "CastEntry",
    "RecordedTurn",
    "StoryCast",
    "TurnInputs",
    "WindowRow",
    "generation_of",
    "joined_input",
    "read_regenerable",
    "read_story_cast",
    "read_turn_inputs",
    "stage_turn",
    "transcript_rows",
]
