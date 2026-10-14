"""The history read (FR-RESP-12, FR-ADMIN-3; #46, #59): a story's corrected history through a
turn, produced for one output viewpoint.

Reads the ``transcript_messages`` projection (joined to each beat's event for a ``player_input``'s
``say``/``do``), never a replay of the log, which keeps a projection rebuild observable
(NFR-REL-2). Only active rows are read, so this is the corrected history.

- **Viewpoint** (``core/viewpoint.py``): ``viewpoint`` when named, else ``actor_character_id``,
  else the story's default; 422 ``viewpoint_unresolved`` otherwise. A character viewpoint never
  receives ``think``/``latent`` beats; with perception ``none`` everything else is returned in
  full (``audience_filter: "none"``). Omniscient items carry the true ages of the characters
  they name (``true_ages``, FR-CAT-12); character-viewpoint items never do.
- **Items** (#59's shape): ``channel`` is ``say``/``do``/``think``/``latent``/``narration``/
  ``message``; a ``player_input`` with both ``say`` and ``do`` yields two items sharing one
  ``message_id``. ``origin`` is ``ingested`` for a consumer's ``ingest`` (#58).
- **Turns**: one summary per turn on the page with its reply (``message``), ``null`` for a
  reply-less turn such as an ingested ``player_turn`` (#58).
- **Window**: ``as_of`` (default the head; > head or < 0 is 422) bounds the read; pages hold
  whole turns, newest first, up to ``limit`` items (default 200, max 1,000; a single larger turn
  is returned whole); ``before_turn`` pages backwards and ``next_before_turn`` names the next
  page (``null`` on the last). Items are ascending within a page. Position-less rows count as
  turn 0, as does ``as_of=0`` ("genesis only").
- **Variant** (``variant``, #66): read along another chain (``core.variants``): the active
  history up to where that chain parts from it, then the chain's own rows; ``as_of`` defaults to
  the chain's tip and runs from 0 to it. 409 ``unknown_chain`` as a swap's selector.
- **Summary** (``include_summary``, #72): the rolling-summary segments a prompt includes now
  (``core.summary.included_segments``, wholly at or before ``as_of``), oldest first, as a separate
  ``summary`` array ``{level, covers_from, covers_to, text, operation_id}``; never mixed into
  ``items`` (segments are derived, not beats). Read along the active chain only.
- **Audit** (``include_audit``): ``omniscient`` only (403 ``omniscient_required`` otherwise,
  after every 422); the ``audit`` category events of the page's turns from active, committed
  operations, as items on the ``audit`` channel whose text is the payload's canonical JSON, and
  the erasure markers (#124: a position-less ``erasure_marker`` per record an erasure deleted,
  turn 0, so on the last page) as ``[erasure marker: <subject id>, <scope>]``.
- **Redaction** (#124): an item whose event an erasure rewrote carries ``redacted: true``; its
  text reads ``[erased]`` where it named the subject.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import EventCategory, Membership, OperationStatus
from epistrel.core.errors import InvalidRequest, StoryNotFound
from epistrel.core.settings_schema import StorySettings
from epistrel.core.summary import included_segments
from epistrel.core.tables import characters, events, operations, stories, transcript_messages
from epistrel.core.variants import ChainRef, ChainSelection, chain_operations
from epistrel.core.viewpoint import (
    AUDIENCE_FILTER,
    PRIVATE_CHANNELS,
    AudienceFilter,
    is_omniscient,
    require_omniscient,
    resolve_viewpoint,
    visible_to,
)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1_000
AUDIT_CHANNEL = "audit"
REPLY_KIND = "message_rendered"
ERASURE_MARKER = "erasure_marker"
INPUT_KIND = "player_input"


@dataclass(frozen=True, slots=True)
class HistoryItem:
    turn: int | None
    variant: int | None
    beat: int | None
    kind: str
    actor_character_id: str | None
    render_character_ids: list[str] | None
    channel: str
    text: str
    operation_id: str
    message_id: str
    origin: str = "generated"
    """``ingested`` when a consumer recorded the beat through ``ingest`` (#58)."""
    true_ages: dict[str, int] | None = None
    """Omniscient reads only: the true age of each character the item names (FR-CAT-12)."""
    redacted: bool = False
    """Erasure (#124) rewrote the item's event in place: its spans naming the erased subject
    read ``[erased]``."""


@dataclass(frozen=True, slots=True)
class TurnMessage:
    message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TurnSummary:
    """A turn on the page and its reply; ``message`` is ``None`` for a reply-less turn."""

    turn: int
    variant: int | None
    message: TurnMessage | None


@dataclass(frozen=True, slots=True)
class SummaryItem:
    """One included rolling-summary segment (``include_summary``, #72)."""

    level: int
    covers_from: int
    covers_to: int
    text: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class History:
    items: list[HistoryItem]
    timeline_revision: int
    output_viewpoint: str
    as_of: int
    turns: list[TurnSummary] = field(default_factory=list)
    audit: list[HistoryItem] | None = None
    next_before_turn: int | None = None
    audience_filter: AudienceFilter = AUDIENCE_FILTER
    summary: list[SummaryItem] | None = None


@dataclass(frozen=True, slots=True)
class _Story:
    timeline_revision: int
    head: int
    settings: StorySettings
    ages: dict[str, int]


async def read_history(
    db: AsyncEngine | AsyncConnection,
    story_id: str,
    *,
    viewpoint: str | None = None,
    actor_character_id: str | None = None,
    as_of: int | None = None,
    limit: int = DEFAULT_LIMIT,
    before_turn: int | None = None,
    include_audit: bool = False,
    variant: ChainRef | None = None,
    include_summary: bool = False,
) -> History:
    """The corrected history of ``story_id`` for one viewpoint; see the module docstring."""
    async with _connection(db) as conn:
        story = await _story(conn, story_id)
        chain = None if variant is None else await chain_operations(conn, story_id, variant)
        if chain is not None:
            story = _Story(
                timeline_revision=story.timeline_revision,
                head=chain.tip_turn,
                settings=story.settings,
                ages=story.ages,
            )
        through = _check_window(story, as_of=as_of, limit=limit, before_turn=before_turn)
        if actor_character_id is not None and actor_character_id not in story.ages:
            raise InvalidRequest(
                "the actor is not a character of the story",
                {"field": "query.actor_character_id", "unknown_actor": actor_character_id},
            )
        resolved = resolve_viewpoint(
            viewpoint,
            actor_character_id=actor_character_id,
            story_default=story.settings.default_output_viewpoint,
            cast_ids=story.ages,
            field="query.viewpoint",
        )
        if include_audit:
            require_omniscient(resolved, field="query.include_audit")
        upper = through if before_turn is None else min(through, before_turn - 1)
        lower, next_before = await _page(conn, story_id, resolved, upper, limit, chain)
        rows = await _rows(conn, story_id, lower=lower, upper=upper, chain=chain)
        items = _items(rows, resolved, story.ages)
        audit = (
            await _audit(conn, story_id, lower, upper, story.ages, chain) if include_audit else None
        )
        summary = (
            [
                SummaryItem(s.level, s.covers_from, s.covers_to, s.text, s.operation_id)
                for s in await included_segments(conn, story_id, before_turn=through + 1)
            ]
            if include_summary
            else None
        )
    return History(
        items=items,
        timeline_revision=story.timeline_revision,
        output_viewpoint=resolved,
        as_of=through,
        turns=_turns(items),
        audit=audit,
        next_before_turn=next_before,
        summary=summary,
    )


async def turn_beats(
    db: AsyncEngine | AsyncConnection, story_id: str, operation_id: str, viewpoint: str
) -> list[HistoryItem]:
    """One operation's beats for an already resolved ``viewpoint`` (respond's ``beats``)."""
    async with _connection(db) as conn:
        ages = await _ages(conn, story_id) if is_omniscient(viewpoint) else {}
        rows = await _rows(conn, story_id, operation_id=operation_id)
    return _items(rows, viewpoint, ages)


# -- reads ------------------------------------------------------------------------------------


async def _story(conn: AsyncConnection, story_id: str) -> _Story:
    row = (
        await conn.execute(
            select(
                stories.c.timeline_revision, stories.c.next_turn_index, stories.c.settings
            ).where(stories.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    return _Story(
        timeline_revision=int(row.timeline_revision),
        head=int(row.next_turn_index) - 1,
        settings=StorySettings.from_row(row.settings),
        ages=await _ages(conn, story_id),
    )


async def _ages(conn: AsyncConnection, story_id: str) -> dict[str, int]:
    found = await conn.execute(
        select(characters.c.id, characters.c.true_age)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    return {r.id: int(r.true_age) for r in found}


def _check_window(story: _Story, *, as_of: int | None, limit: int, before_turn: int | None) -> int:
    """The turn the read runs through (``as_of`` or the head); 422 for an impossible window."""
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequest(
            f"limit must be between 1 and {MAX_LIMIT}", {"field": "query.limit", "max": MAX_LIMIT}
        )
    if before_turn is not None and before_turn < 1:
        raise InvalidRequest("before_turn must be at least 1", {"field": "query.before_turn"})
    if as_of is None:
        return story.head
    if as_of < 0 or as_of > story.head:
        raise InvalidRequest(
            "as_of must be a turn of the story, from 0 to its head",
            {"field": "query.as_of", "head": story.head},
        )
    return as_of


def _bucket() -> Any:
    return func.coalesce(transcript_messages.c.narrative_turn, literal_column("0"))


def _on_chain(chain: ChainSelection | None) -> Any:
    """Transcript rows of the active history, or of ``chain`` (#66)."""
    t = transcript_messages
    if chain is None:
        return t.c.membership == Membership.ACTIVE.value
    return chain.visible(t.c.membership, _bucket(), t.c.operation_id)


async def _page(
    conn: AsyncConnection,
    story_id: str,
    viewpoint: str,
    upper: int,
    limit: int,
    chain: ChainSelection | None = None,
) -> tuple[int, int | None]:
    """The lowest turn of the page ending at ``upper`` and the next page's ``before_turn``."""
    t = transcript_messages
    both = and_(
        t.c.kind == INPUT_KIND,
        func.length(events.c.payload["say"].astext) > 0,
        func.length(events.c.payload["do"].astext) > 0,
    )
    query = (
        select(_bucket().label("turn"), func.sum(case((both, 2), else_=1)).label("items"))
        .join(events, events.c.id == t.c.message_id)
        .where(t.c.story_id == story_id, _on_chain(chain))
        .where(_bucket() <= upper)
        .group_by(_bucket())
        .order_by(_bucket().desc())
    )
    if not is_omniscient(viewpoint):
        query = query.where(t.c.channel.not_in(sorted(PRIVATE_CHANNELS)))
    counts = (await conn.execute(query)).all()
    taken = 0
    for index, row in enumerate(counts):
        if index and taken + int(row.items) > limit:
            return int(counts[index - 1].turn), int(counts[index - 1].turn)
        taken += int(row.items)
    return 0, None


async def _rows(
    conn: AsyncConnection,
    story_id: str,
    *,
    lower: int | None = None,
    upper: int | None = None,
    operation_id: str | None = None,
    chain: ChainSelection | None = None,
) -> Sequence[Any]:
    t = transcript_messages
    query = (
        select(t, operations.c.origin, events.c.payload)
        .join(operations, operations.c.operation_id == t.c.operation_id)
        .join(events, events.c.id == t.c.message_id)
        .where(t.c.story_id == story_id, _on_chain(chain))
        .order_by(_bucket(), t.c.turn_variant.asc().nulls_first(), t.c.seq)
    )
    if operation_id is not None:
        query = query.where(t.c.operation_id == operation_id)
    if lower is not None and upper is not None:
        query = query.where(_bucket().between(lower, upper))
    return (await conn.execute(query)).all()


async def _audit(
    conn: AsyncConnection,
    story_id: str,
    lower: int,
    upper: int,
    ages: Mapping[str, int],
    chain: ChainSelection | None = None,
) -> list[HistoryItem]:
    bucket = func.coalesce(events.c.narrative_turn, literal_column("0"))
    on_chain = (
        operations.c.membership == Membership.ACTIVE.value
        if chain is None
        else chain.visible(operations.c.membership, bucket, operations.c.operation_id)
    )
    found = await conn.execute(
        select(events, operations.c.origin)
        .join(operations, operations.c.operation_id == events.c.operation_id)
        .where(
            events.c.story_id == story_id,
            or_(
                events.c.category == EventCategory.AUDIT.value,
                events.c.type == ERASURE_MARKER,
            ),
            on_chain,
            operations.c.status == OperationStatus.COMMITTED.value,
            bucket.between(lower, upper),
        )
        .order_by(bucket, events.c.turn_variant.asc().nulls_first(), events.c.seq)
    )
    return [
        HistoryItem(
            turn=row.narrative_turn,
            variant=row.turn_variant,
            beat=row.beat,
            kind=row.type,
            actor_character_id=row.character_id,
            render_character_ids=None,
            channel=AUDIT_CHANNEL,
            text=_audit_text(row),
            operation_id=row.operation_id,
            message_id=row.id,
            origin=row.origin,
            true_ages=_named_ages(ages, row.character_id, None),
            redacted=row.redacted_at is not None,
        )
        for row in found
    ]


def _audit_text(row: Any) -> str:
    """An audit item's text: the payload's canonical JSON, or for an erasure marker (#124)
    ``[erasure marker: <subject id>, <scope>]``: identifiers and scope only."""
    if row.type == ERASURE_MARKER:
        payload = row.payload or {}
        return f"[erasure marker: {payload.get('subject_id')}, {payload.get('scope')}]"
    return json.dumps(row.payload, sort_keys=True, separators=(",", ":"))


# -- shaping ----------------------------------------------------------------------------------


def _parts(row: Any) -> list[tuple[str, str]]:
    """``(channel, text)`` of each item a transcript row yields."""
    if row.kind == INPUT_KIND:
        payload = row.payload or {}
        parts = [(name, payload.get(name)) for name in ("say", "do")]
        split = [(name, str(text)) for name, text in parts if text]
        if split:
            return split
    return [(row.channel, row.text)]


def _named_ages(
    ages: Mapping[str, int], actor: str | None, render: Collection[str] | None
) -> dict[str, int]:
    named = [actor, *(render or ())]
    return {cid: ages[cid] for cid in named if cid is not None and cid in ages}


def _items(rows: Sequence[Any], viewpoint: str, ages: Mapping[str, int]) -> list[HistoryItem]:
    omniscient = is_omniscient(viewpoint)
    items: list[HistoryItem] = []
    for row in rows:
        if not visible_to(row.channel, viewpoint):
            continue
        for channel, text in _parts(row):
            items.append(
                HistoryItem(
                    turn=row.narrative_turn,
                    variant=row.turn_variant,
                    beat=row.beat,
                    kind=row.kind,
                    actor_character_id=row.actor_character_id,
                    render_character_ids=row.render_character_ids,
                    channel=channel,
                    text=text,
                    operation_id=row.operation_id,
                    message_id=row.message_id,
                    origin=row.origin,
                    true_ages=(
                        _named_ages(ages, row.actor_character_id, row.render_character_ids)
                        if omniscient
                        else None
                    ),
                    redacted=bool(row.redacted),
                )
            )
    return items


def _turns(items: Sequence[HistoryItem]) -> list[TurnSummary]:
    turns: dict[int, TurnSummary] = {}
    for item in items:
        if item.turn is None or item.turn < 1:
            continue
        summary = turns.get(item.turn) or TurnSummary(item.turn, item.variant, None)
        if summary.message is None and item.kind == REPLY_KIND:
            summary = TurnSummary(item.turn, item.variant, TurnMessage(item.message_id, item.text))
        turns[item.turn] = summary
    return list(turns.values())


@asynccontextmanager
async def _connection(db: AsyncEngine | AsyncConnection) -> AsyncIterator[AsyncConnection]:
    if isinstance(db, AsyncConnection):
        yield db
        return
    async with db.connect() as conn:
        yield conn


__all__ = [
    "AUDIT_CHANNEL",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "History",
    "HistoryItem",
    "TurnMessage",
    "TurnSummary",
    "read_history",
    "turn_beats",
]
