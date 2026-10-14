"""The Lite rolling summary in Core (FR-LITE-2, FR-STORE-26,27; #72): what is covered, what may
be compressed next, and which segments a prompt includes. Deterministic; the model calls are the
Orchestrator's ``lite_summary`` job.

- **Settings** (``read_summary_settings``, #113). The verbatim window
  (``memory.rolling_summary.window_messages``), the summary's token cap
  (``memory.rolling_summary.compress_threshold_tokens``) and whether there is a summary at all
  (``memory.rolling_summary.enabled``) come from the story's effective configuration
  (``core.profiles.effective``), read when the job (or the scheduler deciding to enqueue it)
  starts; the chunk is the story's ``lite_summary_chunk``, else the deployment's. With the
  rolling summary off there is never work: no job is enqueued and the prompt has no summary.
- **Grid.** Level-0 segments cover whole turns on a fixed grid from turn 1:
  ``lite_summary_chunk`` transcript rows at two rows per turn (default 10 rows = 5 turns:
  turns 1-5, 6-10, …), so a chunk never splits a turn and a rebuilt or forked story chunks the
  same way.
- **Inclusion** (``included_segments``, FR-STORE-26). A segment is included only if its
  operation is active (its row not ``discarded`` or ``dormant``), every source it was made from
  (``source_versions``: the turns, or a merge's child segments) is still active, and no
  included merged segment holds it as a source (``superseded``; before a regenerated turn N,
  only merged segments wholly before N count, so their children stand in). The included
  segments are the contiguous run from turn 1; ``covered_to`` is where that run ends, and every
  transcript row after it is **verbatim**: the prompt sends all of them, and never fewer than
  the latest ``window_messages`` rows (``core.turns``), so nothing between the summary and
  the window is ever dropped.
- **Work** (``summary_state``). A chunk is compressible when it is wholly older than the
  trailing ``window_messages`` verbatim rows (no row of its turns is in the window) and every
  turn in it is present. The summary is **over its cap** when its estimated tokens
  (``len(text) // 4``, no tokenizer) exceed ``compress_threshold_tokens`` with more than one
  segment; then the two oldest adjacent segments of the lowest level merge into one of the
  next level (``merge_pair``), across levels when no two adjacent segments share one.
  ``needs_work`` is either; it implies more than ``window_messages`` verbatim rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.config_changes import read_lite_defaults
from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.settings_schema import StorySettings
from epistrel.core.tables import lite_summary_segments, operations, stories, transcript_messages

ROWS_PER_TURN = 2
"""A Lite turn's transcript rows: the input and the reply."""
PROMPT_CHANNELS = ("message", "narration")
"""Transcript channels a Lite prompt carries (and the summary reads); private ones never."""
MAX_CALLS_PER_RUN = 20
"""The most ``lite_summary`` calls one job run makes (its budget)."""
TARGET_TOKENS = 400
"""The length each segment is asked for."""
RESPONSE_MAX_TOKENS = 600
"""The ``max_tokens`` of each ``lite_summary`` call."""
TEMPERATURE = 0.3

ACTIVE = Membership.ACTIVE.value


def estimate_tokens(text: str) -> int:
    """M2's token estimate: four characters a token (no tokenizer dependency)."""
    return len(text) // 4


def grid_end(start: int, chunk_turns: int) -> int:
    """The last turn of the grid cell (from turn 1, ``chunk_turns`` wide) holding ``start``."""
    return ((start - 1) // chunk_turns + 1) * chunk_turns


@dataclass(frozen=True, slots=True)
class SummarySettings:
    """The story's effective summary settings (see the module docstring; all prospective: read
    when a job or a turn starts)."""

    window_messages: int
    chunk_rows: int
    max_tokens: int
    enabled: bool = True
    """Whether the story has a rolling summary (``memory.rolling_summary.enabled``)."""

    @property
    def chunk_turns(self) -> int:
        return max(1, self.chunk_rows // ROWS_PER_TURN)


async def read_summary_settings(conn: AsyncConnection, story_id: str) -> SummarySettings:
    """The story's summary settings (see the module docstring)."""
    defaults = await read_lite_defaults(conn)
    raw = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    story = StorySettings.from_row(raw)
    summary = (await effective_configuration(conn, story_id)).settings.memory.rolling_summary
    return SummarySettings(
        window_messages=summary.window_messages,
        chunk_rows=story.lite_summary_chunk or defaults.lite_summary_chunk,
        max_tokens=summary.compress_threshold_tokens,
        enabled=summary.enabled,
    )


@dataclass(frozen=True, slots=True)
class Segment:
    """A summary segment as the prompt and the job see it."""

    segment_id: str
    operation_id: str
    covers_from: int
    covers_to: int
    level: int
    text: str
    turn_variant: int | None = None
    sources: tuple[str, ...] = ()
    """The operation ids it was made from (turns, or child segments for a merge)."""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def label(self) -> str:
        """``Turns a-b: <text>``, the prompt's line for it."""
        span = (
            str(self.covers_from)
            if self.covers_from == self.covers_to
            else f"{self.covers_from}-{self.covers_to}"
        )
        word = "Turn" if self.covers_from == self.covers_to else "Turns"
        return f"{word} {span}: {self.text}"


def _segment(row: Any) -> Segment:
    return Segment(
        segment_id=row.segment_id,
        operation_id=row.operation_id,
        covers_from=int(row.covers_from),
        covers_to=int(row.covers_to),
        level=int(row.level),
        text=row.text,
        turn_variant=row.turn_variant,
        sources=tuple(str(s["operation_id"]) for s in row.source_versions or ()),
    )


async def _memberships(conn: AsyncConnection, operation_ids: set[str]) -> dict[str, str]:
    if not operation_ids:
        return {}
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.membership).where(
            operations.c.operation_id.in_(sorted(operation_ids)),
            operations.c.status == OperationStatus.COMMITTED.value,
        )
    )
    return {r.operation_id: str(r.membership) for r in found}


async def included_segments(
    conn: AsyncConnection, story_id: str, *, before_turn: int | None = None
) -> tuple[Segment, ...]:
    """The segments a prompt of ``story_id`` includes, oldest first (see the module docstring);
    ``before_turn`` keeps only segments wholly before it (a regenerated turn's inputs)."""
    t = lite_summary_segments
    query = select(t).where(
        t.c.story_id == story_id, t.c.membership.in_((ACTIVE, Membership.SUPERSEDED.value))
    )
    if before_turn is not None:
        query = query.where(t.c.covers_to < before_turn)
    rows = [_segment(r) for r in (await conn.execute(query)).all()]
    memberships = await _memberships(
        conn, {s.operation_id for s in rows} | {x for s in rows for x in s.sources}
    )
    eligible = [
        s
        for s in rows
        if memberships.get(s.operation_id) == ACTIVE
        and all(memberships.get(x) == ACTIVE for x in s.sources)
    ]
    # Superseded is relative to the merged segments in scope: before a regenerated turn, a
    # child whose parent covers that turn is the one the prompt uses.
    held = {x for s in eligible if s.level > 0 for x in s.sources}
    eligible = [s for s in eligible if s.operation_id not in held]
    included: list[Segment] = []
    covered = 0
    for segment in sorted(eligible, key=lambda s: (s.covers_from, -s.level, s.covers_to)):
        if segment.covers_from == covered + 1:
            included.append(segment)
            covered = segment.covers_to
    return tuple(included)


def covered_to(segments: Sequence[Segment]) -> int:
    """The last turn the included segments cover (0: none)."""
    return segments[-1].covers_to if segments else 0


@dataclass(frozen=True, slots=True)
class SourceTurn:
    """A turn a level-0 segment is made from: its active operation and position."""

    operation_id: str
    turn: int
    variant: int


@dataclass(frozen=True, slots=True)
class Chunk:
    """Turns ``covers_from``..``covers_to``, compressible into one level-0 segment."""

    covers_from: int
    covers_to: int
    turns: tuple[SourceTurn, ...]


@dataclass(frozen=True, slots=True)
class SummaryState:
    """Where a story's summary stands: its settings, the included segments, the verbatim row
    count after them, and the chunks compressible now (oldest first)."""

    settings: SummarySettings
    segments: tuple[Segment, ...]
    verbatim: int
    chunks: tuple[Chunk, ...] = field(default_factory=tuple)

    @property
    def covered(self) -> int:
        return covered_to(self.segments)

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.segments)

    @property
    def over_cap(self) -> bool:
        return len(self.segments) > 1 and self.tokens > self.settings.max_tokens

    @property
    def needs_work(self) -> bool:
        return self.settings.enabled and (bool(self.chunks) or self.over_cap)


async def summary_state(conn: AsyncConnection, story_id: str) -> SummaryState:
    """The story's summary state, read on ``conn`` (one snapshot when the caller holds one)."""
    settings = await read_summary_settings(conn, story_id)
    segments = await included_segments(conn, story_id)
    covered = covered_to(segments)
    t = transcript_messages
    turns_of_rows = [
        int(turn)
        for turn in await conn.scalars(
            select(t.c.narrative_turn)
            .where(
                t.c.story_id == story_id,
                t.c.membership == ACTIVE,
                t.c.channel.in_(PROMPT_CHANNELS),
                t.c.narrative_turn > covered,
            )
            .order_by(t.c.narrative_turn, t.c.turn_variant, t.c.seq)
        )
    ]
    verbatim = len(turns_of_rows)
    if verbatim <= settings.window_messages:
        return SummaryState(settings, segments, verbatim)
    window_from = turns_of_rows[verbatim - settings.window_messages]
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.narrative_turn, operations.c.turn_variant)
        .where(
            operations.c.story_id == story_id,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
            operations.c.membership == ACTIVE,
            operations.c.narrative_turn > covered,
            operations.c.narrative_turn < window_from,
        )
        .order_by(operations.c.narrative_turn)
    )
    by_turn = {
        int(r.narrative_turn): SourceTurn(
            r.operation_id, int(r.narrative_turn), int(r.turn_variant)
        )
        for r in found
    }
    chunks: list[Chunk] = []
    start = covered + 1
    while True:
        end = grid_end(start, settings.chunk_turns)
        if end >= window_from or any(turn not in by_turn for turn in range(start, end + 1)):
            break
        chunks.append(Chunk(start, end, tuple(by_turn[turn] for turn in range(start, end + 1))))
        start = end + 1
    return SummaryState(settings, segments, verbatim, tuple(chunks))


async def needs_work(conn: AsyncConnection, story_id: str) -> bool:
    """Whether the story's summary job has anything to do now."""
    return (await summary_state(conn, story_id)).needs_work


def merge_pair(segments: Sequence[Segment]) -> int | None:
    """The index ``i`` of the pair ``(i, i + 1)`` to merge next: the two oldest adjacent
    segments of the lowest level that has an adjacent pair, else (no two adjacent segments share
    a level) the two oldest adjacent segments; ``None`` with fewer than two."""
    if len(segments) < 2:
        return None
    for level in sorted({s.level for s in segments}):
        for i in range(len(segments) - 1):
            if segments[i].level == level and segments[i + 1].level == level:
                return i
    return 0


__all__ = [
    "MAX_CALLS_PER_RUN",
    "PROMPT_CHANNELS",
    "RESPONSE_MAX_TOKENS",
    "ROWS_PER_TURN",
    "TARGET_TOKENS",
    "TEMPERATURE",
    "Chunk",
    "Segment",
    "SourceTurn",
    "SummarySettings",
    "SummaryState",
    "covered_to",
    "estimate_tokens",
    "grid_end",
    "included_segments",
    "merge_pair",
    "needs_work",
    "read_summary_settings",
    "summary_state",
]
