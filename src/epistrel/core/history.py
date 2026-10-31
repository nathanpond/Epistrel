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
- **Quarantine** (#145, #497): a quarantine leaves history alone. Under ``omniscient`` an item
  whose event is a source of a quarantined memory carries ``quarantined: true``; a character
  viewpoint never sees the flag.
- **Composed messages** (#152): a structured turn stores one composed message, for the
  viewpoint its turn was played for (``operations.viewpoint``). A read for any other viewpoint
  gets the deterministic assembly of the beats it receives instead
  (``core.turns.assembly``), so an omniscient message never reaches a character read and a
  character's message never stands in for an omniscient one. So does a character read under
  another perception level or player view than the message was composed under (#177).
- **The audience filter** (FR-RESP-13; #177, ``core.epistemic.audience``): with the story's
  perception at ``co_location`` or ``full`` now, a character read re-routes each structured
  turn's beats (the player's input included) from their stored qualifiers over the scene at the
  turn's start and its committed moves (``core.epistemic.scene.load_scene``): a beat the
  character perceived is returned in full, one it perceived only the envelope of as that
  envelope (``fidelity: envelope``), any other omitted, and the turn's message is recomposed over
  what is returned. A turn whose message records no perception level (one committed before
  perception was on) is read as perceived by all, and so is an ingested turn. ``player_view``
  (the request's, else the story's) ``story`` returns every beat in full instead, each with the
  fidelity the character would have perceived it at (``audience_filter:
  "bypassed_by_player_view"``). Every structured beat carries ``fidelity``.
- **Beat metadata** (#177): a character read's structured beats keep each proposition's text
  with ``verdict: null`` and carry no ``detected`` or ``effects`` (a ``contradicted`` verdict on a
  lie discloses the truth); ``omniscient`` reads keep them all, each beat's ``effects`` being the
  State changes it committed (#187).
- **State effects** (#187, FR-RESP-7): each turn on the page carries ``state_effects``, the State
  changes its operation committed (``core.states.turn_effects``: ``{state, scope_key,
  old_value, new_value, reason, source, contingency, status}``), a ``hidden`` State's only for
  ``omniscient``: visibility only, no audience filter (owner decision 2026-10-28).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, bindparam, case, func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import EventCategory, Membership, OperationStatus
from epistrel.core.epistemic import audience as audience_rules
from epistrel.core.epistemic import perception
from epistrel.core.errors import InvalidRequest, StoryNotFound
from epistrel.core.settings_schema import PlayerView, StorySettings
from epistrel.core.summary import read_included_segments
from epistrel.core.tables import characters, events, operations, stories, transcript_messages
from epistrel.core.tables.base import ulid_type
from epistrel.core.tables.memories import memories
from epistrel.core.turns.assembly import AssemblyBeat, deterministic_assembly
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
    quarantined: bool = False
    """Omniscient reads only (#145): the item's event is a source of a quarantined memory."""
    propositions: list[dict[str, Any]] | None = None
    """A structured turn's beat (#150): the propositions it asserts, each
    ``{proposition, verdict}``; ``None`` for a Lite item."""
    effects: list[dict[str, Any]] | None = None
    """A structured turn's beat (#150; omniscient reads only): the State changes it committed
    (#187)."""
    labels: dict[str, Any] | None = None
    """A ``say`` (#150): the utterance's three labels (FR-TURN-6)."""
    visibility: dict[str, Any] | None = None
    """A ``latent`` beat (#150, omniscient reads only): its declared scope (FR-TURN-4)."""
    detected: list[dict[str, Any]] | None = None
    """A structured turn's beat (#150): the claims detection found that it did not declare."""
    narrated_by: str | None = None
    """A reactive character's reaction (#176): the character whose render narrated it."""
    fidelity: str | None = None
    """A structured turn's beat (#177): the fidelity the viewing character perceived it at,
    ``content`` or ``envelope`` (its text is then the envelope), and under ``player_view =
    story`` also ``none``; always ``content`` for ``omniscient`` and perception ``none``."""


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
    state_effects: list[dict[str, Any]] = field(default_factory=list)
    """#187 (FR-RESP-7): the State changes the turn committed (see the module docstring)."""


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
    player_view: PlayerView = "character"
    """The player view the read was produced under (#177): the request's, else the story's."""


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
    player_view: PlayerView | None = None,
) -> History:
    """The corrected history of ``story_id`` for one viewpoint; see the module docstring.
    ``player_view``: the read's own over the story's (#177)."""
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
        view = player_view or story.settings.player_view
        heard = await _audience(conn, story_id, rows, resolved, view)
        changed = await _state_effects(conn, story_id, rows, resolved)
        items = _items(
            rows,
            resolved,
            story.ages,
            await _names(conn, story_id, rows, resolved, heard),
            await _quarantined(conn, story_id, rows, resolved),
            heard,
            changed,
        )
        audit = (
            await _audit(conn, story_id, lower, upper, story.ages, chain) if include_audit else None
        )
        summary = (
            [
                SummaryItem(s.level, s.covers_from, s.covers_to, s.text, s.operation_id)
                for s in await read_included_segments(conn, story_id, before_turn=through + 1)
            ]
            if include_summary
            else None
        )
    return History(
        items=items,
        timeline_revision=story.timeline_revision,
        output_viewpoint=resolved,
        as_of=through,
        turns=_turns(items, changed),
        audit=audit,
        next_before_turn=next_before,
        summary=summary,
        audience_filter=heard.stated,
        player_view=view,
    )


@dataclass(frozen=True, slots=True)
class TurnBeats:
    """One operation's beats for a viewpoint and what the result states about its audience
    filtering (``nothing_perceived`` when the viewpoint perceived none of them)."""

    items: list[HistoryItem]
    audience_filter: AudienceFilter


async def turn_beats(
    db: AsyncEngine | AsyncConnection,
    story_id: str,
    operation_id: str,
    viewpoint: str,
    *,
    player_view: PlayerView = "character",
) -> list[HistoryItem]:
    """One operation's beats for an already resolved ``viewpoint`` (respond's ``beats``)."""
    return (
        await filtered_turn_beats(db, story_id, operation_id, viewpoint, player_view=player_view)
    ).items


async def filtered_turn_beats(
    db: AsyncEngine | AsyncConnection,
    story_id: str,
    operation_id: str,
    viewpoint: str,
    *,
    player_view: PlayerView = "character",
) -> TurnBeats:
    """``turn_beats`` with the audience filter's statement (#177)."""
    async with _connection(db) as conn:
        ages = await _ages(conn, story_id) if is_omniscient(viewpoint) else {}
        rows = await _rows(conn, story_id, operation_id=operation_id)
        heard = await _audience(conn, story_id, rows, viewpoint, player_view)
        names = await _names(conn, story_id, rows, viewpoint, heard)
        quarantined = await _quarantined(conn, story_id, rows, viewpoint)
        changed = await _state_effects(conn, story_id, rows, viewpoint)
    items = _items(rows, viewpoint, ages, names, quarantined, heard, changed)
    stated = heard.stated
    beats = [i for i in items if i.kind != REPLY_KIND]
    if heard.routed and not beats and stated not in ("none", audience_rules.BYPASSED):
        stated = audience_rules.NOTHING_PERCEIVED
    return TurnBeats(items, stated)


async def story_player_view(db: AsyncEngine | AsyncConnection, story_id: str) -> PlayerView:
    """The story's ``player_view`` setting (#177; ``character`` for a story with none)."""
    async with _connection(db) as conn:
        found = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    return StorySettings.from_row(found).player_view


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
        select(
            t, operations.c.origin, operations.c.viewpoint.label("op_viewpoint"), events.c.payload
        )
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


STRUCTURED_PREFIX = "beat_"


def _labels(labels: Mapping[str, Any], omniscient: bool) -> dict[str, Any] | None:
    """A ``say``'s labels as a read returns them (FR-TURN-6): M4's three; under partitions
    (#166) a classified utterance's labels, ``classification`` and ``intent_corrected``
    included, on omniscient reads only (its belief relation is the speaker's private state)."""
    found = dict(labels)
    classification = found.pop("classification", None)
    corrected = bool(found.pop("intent_corrected", False))
    if classification is None:
        return found
    if not omniscient:
        return None
    found["classification"] = classification
    if corrected:
        found["intent_corrected"] = True
    return found


def _structured(row: Any, channel: str, omniscient: bool = True) -> dict[str, Any]:
    """A structured turn's fields of one item (#150): a ``beat_*`` event's propositions,
    effects, labels, visibility and detected claims; a structured player input's (one that
    carries labels) for its ``say`` and ``do`` parts. Empty for every Lite item."""
    payload = row.payload or {}
    structured = str(row.kind).startswith(STRUCTURED_PREFIX)
    if not structured and not (row.kind == INPUT_KIND and "labels" in payload):
        return {}
    found: dict[str, Any] = {
        "propositions": [
            {
                "proposition": p.get("proposition"),
                "verdict": p.get("verdict") if omniscient else None,
            }
            for p in payload.get("propositions") or ()
        ],
    }
    if omniscient:
        # #177: a character read gets no effects and no detected claims (a ``latent`` effect, a
        # ``contradicted`` verdict on a lie, disclose truth the viewer may not know).
        found["effects"] = []
    labels = payload.get("labels")
    if labels is not None and channel == "say":
        shown = _labels(labels, omniscient)
        if shown is not None:
            found["labels"] = shown
    if row.kind == "beat_latent":
        found["visibility"] = dict(payload.get("visibility") or {"scope": "objective"})
    if payload.get("detected") and omniscient:
        found["detected"] = [dict(d) for d in payload["detected"]]
    if payload.get("narrated_by"):
        found["narrated_by"] = str(payload["narrated_by"])
    return found


def _recomposed(row: Any, viewpoint: str, heard: _Audience | None = None) -> bool:
    """Whether ``row`` is a structured turn's composed message stored for a viewpoint other
    than ``viewpoint`` (#152), or composed under another audience filtering than the read's
    (#177): the read recomposes it."""
    payload = row.payload or {}
    stored = getattr(row, "op_viewpoint", None)
    if row.kind != REPLY_KIND or not payload.get("composed") or stored is None:
        return False
    if stored != viewpoint:
        return True
    if heard is None or is_omniscient(viewpoint):
        return False
    return _stored_audience(payload) != heard.key(row.operation_id)


def _stored_audience(payload: Mapping[str, Any]) -> tuple[str, str]:
    """The perception level and player view a composed message was filtered under (#177)."""
    level = payload.get("perception_level")
    if not level:
        return (perception.NONE, audience_rules.CHARACTER_VIEW)
    return (str(level), str(payload.get("player_view") or audience_rules.CHARACTER_VIEW))


async def _names(
    conn: AsyncConnection,
    story_id: str,
    rows: Sequence[Any],
    viewpoint: str,
    heard: _Audience | None = None,
) -> dict[str, str]:
    """The cast's names, when a composed message on the page is recomposed (#152)."""
    if not any(_recomposed(r, viewpoint, heard) for r in rows):
        return {}
    found = await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    )
    return {r.id: r.name for r in found}


@dataclass(frozen=True, slots=True)
class _Audience:
    """A read's audience filtering (#177): what it states, the level and player view, each
    routed operation's beats as the viewpoint receives them (by ``(message_id, channel)``), and
    the operations that were routed (a beat of one that is not in ``seen`` is omitted)."""

    stated: AudienceFilter
    level: perception.Level
    player_view: PlayerView
    seen: Mapping[tuple[str, str], audience_rules.Seen] = field(default_factory=dict)
    routed: frozenset[str] = frozenset()

    def key(self, operation_id: str) -> tuple[str, str]:
        if operation_id not in self.routed:
            return (perception.NONE, audience_rules.CHARACTER_VIEW)
        return (self.level, self.player_view)

    def of(self, row: Any, channel: str) -> audience_rules.Seen | bool | None:
        """The beat as the viewpoint receives it; ``False`` when the filter omitted it; ``None``
        when the row was not filtered."""
        if row.operation_id not in self.routed or not _is_beat(row):
            return None
        return self.seen.get((str(row.message_id), channel), False)


def _is_beat(row: Any) -> bool:
    """A beat the audience filter routes: a structured beat or a turn's player input."""
    return str(row.kind).startswith(STRUCTURED_PREFIX) or row.kind == INPUT_KIND


async def _audience(
    conn: AsyncConnection,
    story_id: str,
    rows: Sequence[Any],
    viewpoint: str,
    player_view: PlayerView,
) -> _Audience:
    """The read's audience filtering (see the module docstring): every routed operation on the
    page filtered for ``viewpoint`` (``core.epistemic.audience.filter_for_viewpoint``) over
    the scene at its turn's start and its committed moves."""
    if is_omniscient(viewpoint):
        return _Audience("none", perception.NONE, player_view)
    from epistrel.core.epistemic import scene as scenes
    from epistrel.core.memory import records

    level = await scenes.current_level(conn, story_id)
    stated = audience_rules.audience_filter_of(viewpoint, level, player_view)
    if level == perception.NONE:
        return _Audience(stated, level, player_view)
    messages: dict[str, Mapping[str, Any]] = {}
    beats: dict[str, list[Any]] = {}
    for row in rows:
        if row.kind == REPLY_KIND:
            messages[str(row.operation_id)] = row.payload or {}
        elif _is_beat(row) and row.narrative_turn is not None:
            beats.setdefault(str(row.operation_id), []).append(row)
    wanted = {
        op: found for op, found in beats.items() if (messages.get(op) or {}).get("perception_level")
    }
    if not wanted:
        return _Audience(stated, level, player_view)
    names, places = await records.places_and_names(conn, story_id)
    seen: dict[tuple[str, str], audience_rules.Seen] = {}
    for op, found in wanted.items():
        turn = int(found[0].narrative_turn)
        scene, moves = await scenes.load_scene(conn, story_id, turn)
        heard: list[audience_rules.AudienceBeat] = []
        for row in found:
            stored = perception.Qualifiers.of((row.payload or {}).get("perception"))
            for channel, part in _parts(row):
                heard.append(
                    audience_rules.AudienceBeat(
                        kind=channel,
                        beat=int(row.beat or 0),
                        character_id=row.actor_character_id,
                        text=part,
                        qualifiers=stored
                        if channel == "say" or row.kind != INPUT_KIND
                        else (perception.Qualifiers()),
                        source=(str(row.message_id), channel),
                    )
                )
        heard = _with_moves(heard, moves)
        mode = str((messages.get(op) or {}).get("interaction_mode") or perception.SEQUENTIAL)
        filtered = audience_rules.filter_for_viewpoint(
            heard,
            viewpoint,
            scene=scene,
            level=level,
            names=names,
            places=places,
            player_view=player_view,
            mode=mode,
        )
        for item in filtered.seen:
            source = item.beat.source
            assert isinstance(source, tuple)  # noqa: S101 - built above
            seen[source] = item
    return _Audience(stated, level, player_view, seen, frozenset(wanted))


def _with_moves(
    beats: Sequence[audience_rules.AudienceBeat],
    moves: Mapping[int, tuple[tuple[str, str | None], ...]],
) -> list[audience_rules.AudienceBeat]:
    """``beats`` with each beat number's committed moves on its last beat."""
    found = sorted(beats, key=lambda b: b.beat)
    for index, item in enumerate(found):
        last = index == len(found) - 1 or found[index + 1].beat != item.beat
        if last and moves.get(item.beat):
            found[index] = dataclasses.replace(item, moves=tuple(moves[item.beat]))
    return found


async def _quarantined(
    conn: AsyncConnection, story_id: str, rows: Sequence[Any], viewpoint: str
) -> set[str]:
    """The page's events that are a source of a quarantined memory (#145): omniscient reads
    only, an empty set for a character viewpoint."""
    page = sorted({str(r.message_id) for r in rows})
    if not page or not is_omniscient(viewpoint):
        return set()
    m = memories.c
    found = await conn.scalars(
        select(func.unnest(m.source_event_ids)).where(
            m.story_id == story_id,
            m.quarantined_at.is_not(None),
            m.source_event_ids.overlap(
                bindparam("history_page_events", page, type_=ARRAY(ulid_type()))
            ),
        )
    )
    return {str(i) for i in found} & set(page)


def _assemblies(
    rows: Sequence[Any],
    viewpoint: str,
    names: Mapping[str, str],
    heard: _Audience | None = None,
) -> dict[str, str]:
    """Each recomposed message's text (``_recomposed``), by its ``message_id``: the
    deterministic assembly of its operation's beats as ``viewpoint`` receives them (#177: an
    envelope as itself, an unperceived beat left out)."""
    wanted = {r.operation_id: r.message_id for r in rows if _recomposed(r, viewpoint, heard)}
    if not wanted:
        return {}
    beats: dict[str, list[AssemblyBeat]] = {}
    for row in rows:
        if row.operation_id in wanted and str(row.kind).startswith(STRUCTURED_PREFIX):
            seen = None if heard is None else heard.of(row, row.channel)
            if seen is False:
                continue
            text = row.text if not isinstance(seen, audience_rules.Seen) else seen.text
            fidelity = (
                audience_rules.CONTENT
                if not isinstance(seen, audience_rules.Seen)
                or heard is None
                or heard.player_view == audience_rules.STORY_VIEW
                else seen.fidelity
            )
            beats.setdefault(row.operation_id, []).append(
                AssemblyBeat(
                    row.channel, text, row.actor_character_id, int(row.beat or 0), fidelity
                )
            )
    return {
        message_id: deterministic_assembly(beats.get(op, []), names, viewpoint)
        for op, message_id in wanted.items()
    }


async def _state_effects(
    conn: AsyncConnection, story_id: str, rows: Sequence[Any], viewpoint: str
) -> dict[str, list[dict[str, Any]]]:
    """#187: the State changes each structured turn among ``rows`` committed, by operation."""
    from epistrel.core.states import turn_effects

    ops = sorted({str(r.operation_id) for r in rows if str(r.kind).startswith(STRUCTURED_PREFIX)})
    return await turn_effects(conn, story_id, ops, omniscient=is_omniscient(viewpoint))


def _turn_effects(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "beat"} for e in found]


def _items(
    rows: Sequence[Any],
    viewpoint: str,
    ages: Mapping[str, int],
    names: Mapping[str, str] | None = None,
    quarantined: Collection[str] = (),
    heard: _Audience | None = None,
    changed: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> list[HistoryItem]:
    omniscient = is_omniscient(viewpoint)
    recomposed = _assemblies(rows, viewpoint, names or {}, heard)
    items: list[HistoryItem] = []
    for row in rows:
        if not visible_to(row.channel, viewpoint):
            continue
        for channel, part in _parts(row):
            text = recomposed.get(row.message_id, part)
            structured = _structured(row, channel, omniscient)
            seen = None if heard is None else heard.of(row, channel)
            if seen is False:
                continue  # #177: the viewpoint did not perceive the beat
            if structured:
                structured["fidelity"] = audience_rules.CONTENT
            if "effects" in structured and changed:
                # #187: an omniscient read's beat carries the State changes it committed.
                structured["effects"] = _turn_effects(
                    [e for e in changed.get(str(row.operation_id), ()) if e["beat"] == row.beat]
                )
            if isinstance(seen, audience_rules.Seen):
                structured["fidelity"] = seen.fidelity
                if seen.text != part:
                    text = seen.text
                if seen.envelope:
                    # Only that it was said: none of what it asserts or how it was labelled.
                    structured["propositions"] = []
                    structured.pop("labels", None)
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
                    quarantined=omniscient and row.message_id in quarantined,
                    **structured,
                )
            )
    return items


def _turns(
    items: Sequence[HistoryItem],
    changed: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> list[TurnSummary]:
    turns: dict[int, TurnSummary] = {}
    for item in items:
        if item.turn is None or item.turn < 1:
            continue
        summary = turns.get(item.turn) or TurnSummary(
            item.turn,
            item.variant,
            None,
            _turn_effects((changed or {}).get(item.operation_id, ())),
        )
        if summary.message is None and item.kind == REPLY_KIND:
            summary = dataclasses.replace(summary, message=TurnMessage(item.message_id, item.text))
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
    "TurnBeats",
    "TurnMessage",
    "TurnSummary",
    "filtered_turn_beats",
    "read_history",
    "story_player_view",
    "turn_beats",
]
