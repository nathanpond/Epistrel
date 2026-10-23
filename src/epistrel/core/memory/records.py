"""Memory records: what a character can remember, where it came from, who may know it, how
important it is now, and whether it is still on the active timeline (FR-MEM-1,2,4,5,7,
FR-STORE-9,13; docs/02 §4.2, §4.4; #135). ``core.tables.memories`` describes the table.

**Episodic memories** (FR-MEM-1). ``write_episodic(conn, operation, beats)`` runs inside the
writing operation's commit transaction (a commit step: #150's structured pipeline calls it at
its commit point, with the turn's beats) and writes one ``episodic`` row per **observable**
beat: ``say``, ``do`` and ``narration``, never ``think`` or ``latent`` (``latent`` routes to
canon, FR-TURN-4). Each row carries:

- ``text``: the beat as an attributed line (``Bobby says: …``, ``Bobby does: …``, narration
  verbatim), cut to ``TEXT_MAX`` characters;
- ``source_event_ids = [the beat's event]``, the operation's id and narrative position
  (``narrative_turn``/``turn_variant``, never ``seq``) and the beat number, and membership
  ``active`` (``core.membership.insert_derived``): a truncation, untruncation, swap or
  regenerate moves it with its operation;
- ``provenance``: ``render`` for a character's beat, ``director`` for narration, ``player`` for
  the player's input (the beat says which), and ``trust_tier`` derived from it
  (``trust_tier_of``);
- ``visibility``: ``objective`` for narration, ``public`` for a character's beat (#133's
  shape); ``holder_character_id`` ``NULL``: in M4 every render-derived memory is shared
  knowledge;
- ``proposition_ids``: the beat's propositions; ``importance`` ``NULL`` until scored;
- ``event_type`` and ``participants`` (#141): the beat's occasion and the entities it involves
  (its actor first, then ``Beat.participants``, which must be the story's entities), which
  #141's occasion step queries. #150 fills them from the beat's resolved entities.

The writer is **inert** unless the story's effective configuration has the ``episodic`` module
on (``memory.episodic.enabled``; #129's ``module_on``): no row, no hook, no call. Every text
and the actor pass the resurrection guard first (``guard.check_content``, a ``DIRECT`` write
path, #127), and every row is indexed for erasure in the same transaction (``index_records``:
its text positionally, its holder declared and its actor authored; ADR-154).
``MEMORY_HOOKS`` run last, in the same transaction, with the new ids: the importance job
(``orchestrator.jobs.handlers.importance``) enqueues itself there, so core never imports the
orchestrator. ``beats_of_operation`` reads an operation's beats from its M2/M3 beat events
(``player_input``, ``message_rendered``, ``think``, ``latent``) for callers that have no typed
beats of their own.

**Per-perceiver memories** (#161; FR-TURN-2,10, NFR-PRIV-3). Under per-character partitions with
perception at ``co_location`` or ``full``, routing replaces the shared-holder writer above for a
structured turn's beats: ``write_perceived(conn, operation, results)`` (the memory sink of
``core.epistemic.perception.PERCEPTION_SINKS``) writes **one row per perceiver** of each routed
``say``, ``do``, ``narration`` and ``think`` (a ``think`` reaches its thinker only):
``holder_character_id`` the perceiver, ``visibility`` ``characters: [perceiver]``, ``fidelity``
(``content``: the attributed line, or an eavesdropper's ``partial_text``; ``envelope``: the
deterministic envelope, ``Jenni whispers something to Marcus``, with no propositions) and
``channel`` (``self``, ``room``, ``eavesdrop``, ``remote``, ``adjacent`` or ``think``). A
non-perceiver gets nothing. The rest is the episodic writer's: the guard, the erasure index and
the hooks, inert unless ``episodic`` is on. The player's character gets its memories too (it
holds no beliefs, FR-EPI-12).

**Semantic memories and reflections** (FR-MEM-2,3; #143). ``write_consolidated(conn,
operation, rows, covered)`` is the writer the consolidation job calls in its positioned
operation's commit (``core.memory.consolidation`` builds the rows): ``semantic`` and
``reflection`` rows with provenance ``system_consolidated``, their ``level``, ``modality``,
``inputs_hash``, ``model``, ``model_version`` and ``prompt_hash``. It runs the resurrection
guard over their texts and speakers, indexes them for erasure, adds their ids to the
``covered_by`` of the inputs they compress, and runs ``MEMORY_HOOKS`` (embedding, importance).
``EPISODIC_HOOKS`` run only after an episodic write (consolidation's scheduler). An input
retired ``uncovered`` at an overflow deadline (#147) reads ``covered`` from then on.

**Author memories** (#145). ``write_author(conn, operation, fact, text)`` writes the memory of
an author fact (``core.mutation``'s ``commit_fact``): a position-less ``semantic`` row,
provenance and trust tier ``author``, wrapping the fact's proposition with its visibility, so
retrieval finds author canon. ``after_rewrite`` runs ``MEMORY_HOOKS`` for a memory whose text a
sanitize rewrote (its vector was cleared). A memory's quarantine (``quarantined_at`` and
the rest, #145) is read into ``Memory`` but written by ``core.mutation`` only.

**Seed memories** (#149). ``write_seed(conn, operation, seeds)`` writes a structured story's
knowledge seeds at genesis, in the genesis operation's commit: provenance ``seed`` (trust tier
``author``), importance ``SEED_IMPORTANCE``, ``semantic`` for a seed that normalized (its
propositions) and ``episodic`` for free text, each with the seed's holder and visibility. The
second writer of ``semantic`` rows beside consolidation; it runs the guard, the erasure index and
``MEMORY_HOOKS`` like every writer.

**Working memory** (FR-MEM-5) is a function over events, never a table:
``working_memory(conn, story_id, character_id, through_position, n)`` answers the last ``n``
beats of the active timeline at or before the position, as the character perceives them. M4
is shared knowledge: a character perceives every beat except other characters' ``think`` and
every ``latent``; ``character_id=None`` (the narrator, omniscient) perceives everything.

**Importance** (FR-MEM-7). The ``importance_scoring`` purpose scores a memory 0..1 through the
Model Access Layer (the importance job); ``set_importance`` writes a score only where none is
set, so an author-set importance is never overwritten. ``importance_at`` is the committed turn
the importance holds from (the memory's own turn). ``decayed_importance`` is pure:
``importance · 0.5^((now - importance_at) / half_life)`` over committed turns of the active
chain (``committed_turns``; a truncation shrinks the count), never stored. An unscored memory
counts as ``DEFAULT_IMPORTANCE``.

**The view through N** (FR-STORE-13). ``memories_through(conn, story_id, through_turn)``
answers the active memories whose own position is at or before N (position-less rows always
qualify, FR-STORE-26) and every one of whose source events belongs to an active operation at
or before N: an episodic memory follows its beat, and #143's semantic memories and reflections
leave the view with any of their sources.

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    and_,
    any_,
    bindparam,
    case,
    exists,
    false,
    func,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import Fact, Visibility
from epistrel.core.config_changes import read_section
from epistrel.core.enums import (
    EventCategory,
    Membership,
    MemoryKind,
    MemoryTrustTier,
    OperationKind,
    OperationStatus,
    RecordProvenance,
    RetrievalTier,
)
from epistrel.core.epistemic import modeling, perception
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position as NarrativePosition
from epistrel.core.membership import active_only, active_operation, insert_derived
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.settings_schema import MemorySettings
from epistrel.core.tables import characters, entities, events, operations
from epistrel.core.tables.base import ulid_type
from epistrel.core.tables.memories import EVENT_TYPE_MAX, TEXT_MAX, memories

EPISODIC_MODULE = "episodic"
"""The profile module that turns episodic memory on (``memory.episodic.enabled``, #106)."""

DEFAULT_IMPORTANCE = 0.5
"""What an unscored memory's importance counts as in ranking and retirement."""

DEFAULT_WORKING_MEMORY_BEATS = 20
"""How many beats working memory holds when the caller names no number."""

BeatKind = Literal["say", "do", "narration", "think", "latent"]
OBSERVABLE: frozenset[str] = frozenset({"say", "do", "narration"})
"""The beats an episodic memory is written for; ``think`` and ``latent`` never are."""

STRUCTURED_BEAT_TYPES: tuple[str, ...] = (
    "beat_say",
    "beat_think",
    "beat_do",
    "beat_latent",
    "beat_narration",
)
"""#150's typed beat events: a structured turn's beats, one event each."""
BEAT_EVENT_TYPES: tuple[str, ...] = (
    "player_input",
    "message_rendered",
    "think",
    "latent",
    *STRUCTURED_BEAT_TYPES,
)
"""The beat events ``beats_of_operation`` and ``working_memory`` read: the M2/M3 ones and #150's
typed beats. A structured turn's composed message (``message_rendered`` with ``composed``) is
derived from its beats and is never read as a beat."""
OBSERVED_TYPES: tuple[str, ...] = (
    "player_input",
    "message_rendered",
    "beat_say",
    "beat_do",
    "beat_narration",
)
"""The beat events every character perceives in M4 (shared knowledge)."""
PRIVATE_TYPES: tuple[str, ...] = ("think", "beat_think")
"""A character's own thoughts: only that character perceives them."""

_TRUST: Mapping[RecordProvenance, MemoryTrustTier] = {
    RecordProvenance.AUTHOR: MemoryTrustTier.AUTHOR,
    RecordProvenance.SEED: MemoryTrustTier.AUTHOR,
    RecordProvenance.EXTERNAL: MemoryTrustTier.EXTERNAL,
    RecordProvenance.SYSTEM_CONSOLIDATED: MemoryTrustTier.ENGINE,
    RecordProvenance.RESOLUTION: MemoryTrustTier.ENGINE,
    RecordProvenance.BACKFILL: MemoryTrustTier.ENGINE,
    RecordProvenance.IN_WORLD: MemoryTrustTier.IN_WORLD,
    RecordProvenance.RENDER: MemoryTrustTier.IN_WORLD,
    RecordProvenance.DIRECTOR: MemoryTrustTier.IN_WORLD,
    RecordProvenance.PLAYER: MemoryTrustTier.IN_WORLD,
}


def trust_tier_of(provenance: RecordProvenance | str) -> MemoryTrustTier:
    """A memory's trust tier, derived mechanically from its provenance (FR-MEM-4): the author
    and a seed are ``author``; an external authority ``external``; consolidation, a resolution
    and a backfill ``engine``; anything said or done in the story ``in_world``."""
    return _TRUST[RecordProvenance(provenance)]


# -- models -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Beat:
    """One beat of a turn as the episodic writer takes it: its kind, its text, the event that
    recorded it, its number in the turn, its actor (``None`` for narration), its provenance and
    its propositions."""

    kind: BeatKind
    text: str
    event_id: str
    beat: int | None = None
    character_id: str | None = None
    provenance: RecordProvenance = RecordProvenance.RENDER
    proposition_ids: tuple[str, ...] = ()
    event_type: str | None = None
    """The beat's occasion (#139's ``resolve_event_type``; #141's occasion step reads it)."""
    participants: tuple[str, ...] = ()
    """The entities the beat involves beyond its actor (its resolved mentions). #150 passes
    both; ``core.retrieval.pipeline.beat_occasion`` derives them from a beat's text and its
    resolved mentions."""


class Memory(BaseModel):
    """One ``memories`` row (see ``core.tables.memories`` for the columns; the vector itself is
    not carried, only whether there is one)."""

    model_config = ConfigDict(frozen=True)

    id: str
    story_id: str
    kind: MemoryKind
    text: str
    holder_character_id: str | None
    character_id: str | None
    visibility: Visibility
    provenance: RecordProvenance
    trust_tier: MemoryTrustTier
    importance: float | None
    importance_at: int | None
    operation_id: str
    source_event_ids: tuple[str, ...]
    narrative_turn: int | None
    turn_variant: int | None
    beat: int | None
    membership: Membership
    membership_tx: datetime
    proposition_ids: tuple[str, ...]
    event_type: str | None = None
    participants: tuple[str, ...] = ()
    retrieval_tier: RetrievalTier
    embedded: bool
    embedding_model_version: str | None
    created_at: datetime
    last_accessed_at: datetime | None
    covered_by: tuple[str, ...] = ()
    level: int | None = None
    modality: str | None = None
    inputs_hash: str | None = None
    model: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None
    quarantined_at: datetime | None = None
    quarantine_reason: str | None = None
    quarantined_by: str | None = None
    implication_count: int = 0
    pinned: bool = False
    pinned_by: str | None = None
    retired_reason: str | None = None
    retired_at: datetime | None = None
    retired_turn: int | None = None
    fidelity: str | None = None
    channel: str | None = None


def _memory(row: Any) -> Memory:
    return Memory(
        id=str(row.id),
        story_id=str(row.story_id),
        kind=MemoryKind(row.kind),
        text=row.text,
        holder_character_id=None
        if row.holder_character_id is None
        else str(row.holder_character_id),
        character_id=None if row.character_id is None else str(row.character_id),
        visibility=Visibility.model_validate(row.visibility),
        provenance=RecordProvenance(row.provenance),
        trust_tier=MemoryTrustTier(row.trust_tier),
        importance=None if row.importance is None else float(row.importance),
        importance_at=row.importance_at,
        operation_id=str(row.operation_id),
        source_event_ids=tuple(str(e) for e in row.source_event_ids),
        narrative_turn=row.narrative_turn,
        turn_variant=row.turn_variant,
        beat=row.beat,
        membership=Membership(row.membership),
        membership_tx=row.membership_tx,
        proposition_ids=tuple(str(p) for p in row.proposition_ids),
        event_type=row.event_type,
        participants=tuple(str(p) for p in row.participants),
        retrieval_tier=RetrievalTier(row.retrieval_tier),
        embedded=row.embedding is not None,
        embedding_model_version=row.embedding_model_version,
        created_at=row.created_at,
        last_accessed_at=row.last_accessed_at,
        covered_by=tuple(str(c) for c in row.covered_by),
        level=row.level,
        modality=row.modality,
        inputs_hash=row.inputs_hash,
        model=row.model,
        model_version=row.model_version,
        prompt_hash=row.prompt_hash,
        quarantined_at=row.quarantined_at,
        quarantine_reason=row.quarantine_reason,
        quarantined_by=row.quarantined_by,
        implication_count=int(row.implication_count or 0),
        pinned=bool(row.pinned),
        pinned_by=row.pinned_by,
        retired_reason=row.retired_reason,
        retired_at=row.retired_at,
        retired_turn=row.retired_turn,
        fidelity=row.fidelity,
        channel=row.channel,
    )


class MemoryOperation(Protocol):
    """What a memory needs of the operation writing it (an ``Operation`` inside its commit):
    its id, story, narrative position and base revision."""

    operation_id: str
    story_id: str | None
    narrative_turn: int | None
    turn_variant: int | None
    base_revision: int | None


MemoryHook = Callable[[AsyncConnection, str, tuple[str, ...]], Awaitable[None]]
"""Called in the writer's transaction with the story and the new memories' ids."""

MEMORY_HOOKS: list[MemoryHook] = []


def register_memory_hook(hook: MemoryHook) -> MemoryHook:
    """Run ``hook`` after every episodic write (once per hook)."""
    if hook not in MEMORY_HOOKS:
        MEMORY_HOOKS.append(hook)
    return hook


def remove_memory_hook(hook: MemoryHook) -> None:
    if hook in MEMORY_HOOKS:
        MEMORY_HOOKS.remove(hook)
    if hook in EPISODIC_HOOKS:
        EPISODIC_HOOKS.remove(hook)


EPISODIC_HOOKS: list[MemoryHook] = []
"""Run after ``MEMORY_HOOKS``, after an episodic write only (never a consolidation output's):
consolidation's scheduler (#143)."""


def register_episodic_hook(hook: MemoryHook) -> MemoryHook:
    """Run ``hook`` after every episodic write (once per hook)."""
    if hook not in EPISODIC_HOOKS:
        EPISODIC_HOOKS.append(hook)
    return hook


# -- the episodic writer ------------------------------------------------------------------------


def participants_of(beat: Beat) -> list[str]:
    """The ``participants`` a beat's memory carries: its actor first (a story character's
    entity id is the character's, #139), then its other participants, each once."""
    found = [beat.character_id] if beat.character_id else []
    return list(dict.fromkeys([*found, *beat.participants]))


def render_line(kind: str, name: str | None, text: str) -> str:
    """A beat as an attributed line (``Bobby says: …``, ``Bobby does: …``; narration and a beat
    without a named actor verbatim), cut to ``TEXT_MAX`` characters."""
    body = text.strip()
    if name and kind == "say":
        line = f"{name} says: {body}"
    elif name and kind == "do":
        line = f"{name} does: {body}"
    else:
        line = body
    return line[:TEXT_MAX]


async def episodic_on(
    conn: AsyncConnection, story_id: str, settings: ProfileSettings | None = None
) -> bool:
    """Whether the story's effective configuration has the ``episodic`` module on."""
    if settings is None:
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
    return module_on(settings, EPISODIC_MODULE)


async def _names(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    found = await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    )
    return {str(r.id): str(r.name) for r in found}


async def _check_beats(
    conn: AsyncConnection,
    story_id: str,
    operation_id: str,
    beats: Sequence[Beat],
    cast: Mapping[str, str],
) -> None:
    unknown = sorted({b.character_id for b in beats if b.character_id} - cast.keys())
    if unknown:
        raise InvalidRequest(
            "a beat's actor is one of the story's characters",
            {"field": "beats.character_id", "reason": "unknown_character"},
        )
    ids = sorted({b.event_id for b in beats})
    found = set(
        await conn.scalars(
            select(events.c.id).where(
                events.c.story_id == story_id,
                events.c.operation_id == operation_id,
                events.c.id.in_(ids),
            )
        )
    )
    if found != set(ids):
        raise InvalidRequest(
            "a beat's event is an event of the writing operation",
            {"field": "beats.event_id", "reason": "unknown_event"},
        )
    if any(b.event_type is not None and not 0 < len(b.event_type) <= EVENT_TYPE_MAX for b in beats):
        raise InvalidRequest(
            f"a beat's event type has 1 to {EVENT_TYPE_MAX} characters",
            {"field": "beats.event_type", "reason": "invalid_event_type"},
        )
    named = sorted({p for b in beats for p in b.participants})
    if named:
        known = {
            str(e)
            for e in await conn.scalars(
                select(entities.c.id).where(
                    entities.c.story_id == story_id, entities.c.id.in_(named)
                )
            )
        }
        if known != set(named):
            raise InvalidRequest(
                "a beat's participant is one of the story's entities",
                {"field": "beats.participants", "reason": "unknown_entity"},
            )


async def write_episodic(
    conn: AsyncConnection,
    operation: MemoryOperation,
    beats: Sequence[Beat],
    *,
    settings: ProfileSettings | None = None,
) -> list[Memory]:
    """Write one episodic memory per observable beat of ``beats`` (see the module docstring),
    inside ``operation``'s commit transaction; answers the memories written (none when the
    ``episodic`` module is off). ``settings`` is the story's effective configuration when the
    caller already holds it."""
    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("a memory belongs to a story", {"field": "operation"})
    if not await episodic_on(conn, story_id, settings):
        return []
    observable = [b for b in beats if b.kind in OBSERVABLE and b.text.strip()]
    if not observable:
        return []
    cast = await _names(conn, story_id)
    await _check_beats(conn, story_id, operation.operation_id, observable, cast)
    rows: list[dict[str, Any]] = []
    contents: list[guard.Content] = []
    for beat in observable:
        line = render_line(beat.kind, cast.get(beat.character_id or ""), beat.text)
        provenance = RecordProvenance(beat.provenance)
        visibility = Visibility(scope="objective" if beat.kind == "narration" else "public")
        contents.append(
            guard.Content(
                (line,),
                frozenset({beat.character_id}) if beat.character_id else frozenset(),
                consumer=provenance is RecordProvenance.PLAYER,
            )
        )
        rows.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "kind": MemoryKind.EPISODIC.value,
                "text": line,
                "holder_character_id": None,
                "character_id": beat.character_id,
                "visibility": visibility.document(),
                "provenance": provenance.value,
                "trust_tier": trust_tier_of(provenance).value,
                "operation_id": operation.operation_id,
                "source_event_ids": [beat.event_id],
                "narrative_turn": operation.narrative_turn,
                "turn_variant": operation.turn_variant,
                "beat": beat.beat,
                "proposition_ids": list(beat.proposition_ids),
                "event_type": beat.event_type,
                "participants": participants_of(beat),
            }
        )
    await guard.check_content(
        conn, story_id, contents, base_revision=operation.base_revision, path=__name__
    )
    await insert_derived(conn, memories, rows)
    await index_records(conn, [memory_record(SimpleNamespace(**r)) for r in rows])
    ids = tuple(str(r["id"]) for r in rows)
    for hook in [*MEMORY_HOOKS, *EPISODIC_HOOKS]:
        await hook(conn, story_id, ids)
    return await memories_by_id(conn, story_id, ids)


ROUTED: frozenset[str] = frozenset({"say", "do", "narration", "think"})
"""The beats routing writes a memory for (a ``latent`` goes to canon, FR-TURN-4)."""


def perceived_line(
    beat: Beat,
    seen: perception.Perceived,
    names: Mapping[str, str],
    places: Mapping[str, str],
) -> str:
    """What ``seen``'s holder remembers of ``beat``: the attributed line at ``content`` (a
    ``think`` as ``<name> thinks: …``; an eavesdropper limited to part of the words as that
    part), the envelope at ``envelope``; cut to ``TEXT_MAX`` characters."""
    name = names.get(beat.character_id or "")
    if seen.fidelity == perception.ENVELOPE and seen.envelope is not None:
        return perception.envelope_text(seen.envelope, names, places)[:TEXT_MAX]
    if seen.partial_text and name:
        return f"{name} says (you catch only part of it): {seen.partial_text.strip()}"[:TEXT_MAX]
    if beat.kind == "think" and name:
        return f"{name} thinks: {beat.text.strip()}"[:TEXT_MAX]
    return render_line(beat.kind, name, beat.text)


async def _place_names(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    found = await conn.execute(
        select(entities.c.id, entities.c.canonical_name).where(
            entities.c.story_id == story_id, entities.c.kind == "place"
        )
    )
    return {str(r.id): f"the {r.canonical_name}" for r in found}


def _minor_holds(
    result: perception.PerceptionResult,
    beat: Beat,
    seen: perception.Perceived,
    names: Mapping[str, str],
) -> bool:
    """Whether a minor perceiver keeps its memory of ``beat`` (``core.epistemic.modeling``'s
    sparsity rule): it acted in it, was addressed by it (``addressee``, ``whisper_to``, a remote
    channel), or is a party to the interaction it states."""
    q = result.qualifiers
    addressed = [
        *([q.addressee] if q.addressee else []),
        *q.whisper_to,
        *(q.remote_channel.participants if q.remote_channel is not None else ()),
    ]
    return modeling.holds_memory(
        seen.character_id,
        actor=beat.character_id,
        addressed=addressed,
        participants=beat.participants,
        kind=beat.kind,
        text=beat.text,
        content=seen.fidelity == perception.CONTENT,
        names=names,
    )


async def write_perceived(
    conn: AsyncConnection,
    operation: MemoryOperation,
    results: Sequence[perception.PerceptionResult],
    *,
    settings: ProfileSettings | None = None,
) -> list[Memory]:
    """One episodic memory per perceiver of each routed beat of ``results`` whose ``source``
    is a ``Beat`` (see the module docstring), inside ``operation``'s commit transaction;
    answers the memories written (none when the ``episodic`` module is off)."""
    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("a memory belongs to a story", {"field": "operation"})
    routed_all = [
        (r, r.beat.source, seen)
        for r in results
        if isinstance(r.beat.source, Beat)
        and r.beat.source.kind in ROUTED
        and r.beat.source.text.strip()
        for seen in r.perceived
    ]
    if not routed_all or not await episodic_on(conn, story_id, settings):
        return []
    cast = await _names(conn, story_id)
    # #163 (FR-EPI-10): a minor's partition accrues only from the interactions it took part in.
    minors = await modeling.minor_ids(conn, story_id)
    routed = [
        (beat, seen)
        for r, beat, seen in routed_all
        if seen.character_id not in minors or _minor_holds(r, beat, seen, cast)
    ]
    if not routed:
        return []
    beats = list({id(b): b for b, _ in routed}.values())
    await _check_beats(conn, story_id, operation.operation_id, beats, cast)
    unknown = sorted({seen.character_id for _, seen in routed} - cast.keys())
    if unknown:
        raise InvalidRequest(
            "a perceiver is one of the story's characters",
            {"field": "results.perceived", "reason": "unknown_character"},
        )
    places = await _place_names(conn, story_id)
    rows: list[dict[str, Any]] = []
    contents: list[guard.Content] = []
    for beat, seen in routed:
        assert isinstance(beat, Beat)  # noqa: S101 - filtered above
        line = perceived_line(beat, seen, cast, places)
        provenance = RecordProvenance(beat.provenance)
        content = seen.fidelity == perception.CONTENT
        contents.append(
            guard.Content(
                (line,),
                frozenset({beat.character_id}) if beat.character_id else frozenset(),
                consumer=provenance is RecordProvenance.PLAYER,
            )
        )
        rows.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "kind": MemoryKind.EPISODIC.value,
                "text": line,
                "holder_character_id": seen.character_id,
                "character_id": beat.character_id,
                "visibility": Visibility(
                    scope="characters", characters=(seen.character_id,)
                ).document(),
                "provenance": provenance.value,
                "trust_tier": trust_tier_of(provenance).value,
                "operation_id": operation.operation_id,
                "source_event_ids": [beat.event_id],
                "narrative_turn": operation.narrative_turn,
                "turn_variant": operation.turn_variant,
                "beat": beat.beat,
                "proposition_ids": list(beat.proposition_ids) if content else [],
                "event_type": beat.event_type,
                "participants": participants_of(beat),
                "fidelity": seen.fidelity,
                "channel": seen.channel,
            }
        )
    await guard.check_content(
        conn, story_id, contents, base_revision=operation.base_revision, path=__name__
    )
    await insert_derived(conn, memories, rows)
    await index_records(conn, [memory_record(SimpleNamespace(**r)) for r in rows])
    ids = tuple(str(r["id"]) for r in rows)
    for hook in [*MEMORY_HOOKS, *EPISODIC_HOOKS]:
        await hook(conn, story_id, ids)
    return await memories_by_id(conn, story_id, ids)


async def memory_sink(
    conn: AsyncConnection,
    operation: perception.SinkOperation,
    results: Sequence[perception.PerceptionResult],
) -> None:
    """The memory sink of ``core.epistemic.perception.PERCEPTION_SINKS`` (``write_perceived``)."""
    await write_perceived(conn, operation, results)


perception.register_perception_sink(memory_sink)


CONSOLIDATED_KINDS: frozenset[MemoryKind] = frozenset({MemoryKind.SEMANTIC, MemoryKind.REFLECTION})

_CONSOLIDATED_FIELDS: tuple[str, ...] = (
    "id",
    "kind",
    "text",
    "holder_character_id",
    "character_id",
    "visibility",
    "source_event_ids",
    "proposition_ids",
    "participants",
    "importance",
    "level",
    "modality",
    "inputs_hash",
    "model",
    "model_version",
    "prompt_hash",
)
"""What a consolidation output row names (``write_consolidated`` adds the rest)."""


async def write_consolidated(
    conn: AsyncConnection,
    operation: MemoryOperation,
    rows: Sequence[Mapping[str, Any]],
    covered: Sequence[str] = (),
) -> list[Memory]:
    """Write consolidation outputs (#143; see the module docstring) inside ``operation``'s
    commit transaction: each row names ``_CONSOLIDATED_FIELDS``; the row is positioned at the
    operation's narrative position, ``system_consolidated`` and engine-trusted. ``covered`` are
    the input memories whose ``covered_by`` gains the new ids. Answers the memories written."""
    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("a memory belongs to a story", {"field": "operation"})
    if not rows:
        return []
    provenance = RecordProvenance.SYSTEM_CONSOLIDATED
    written: list[dict[str, Any]] = []
    contents: list[guard.Content] = []
    for row in rows:
        unknown = set(row) - set(_CONSOLIDATED_FIELDS)
        if unknown:
            raise ValueError(f"a consolidation output names no column(s) {sorted(unknown)}")
        if MemoryKind(row["kind"]) not in CONSOLIDATED_KINDS:
            raise ValueError("a consolidation output is a semantic memory or a reflection")
        importance = row.get("importance")
        line = str(row["text"])[:TEXT_MAX]
        speaker = None if row.get("character_id") is None else str(row["character_id"])
        values: dict[str, Any] = {
            **{k: row.get(k) for k in _CONSOLIDATED_FIELDS},
            "story_id": story_id,
            "text": line,
            "provenance": provenance.value,
            "trust_tier": trust_tier_of(provenance).value,
            "operation_id": operation.operation_id,
            "narrative_turn": operation.narrative_turn,
            "turn_variant": operation.turn_variant,
            "beat": None,
            "importance_at": operation.narrative_turn if importance is not None else None,
            "source_event_ids": list(row.get("source_event_ids") or ()),
            "proposition_ids": list(row.get("proposition_ids") or ()),
            "participants": list(row.get("participants") or ()),
        }
        contents.append(guard.Content((line,), frozenset({speaker}) if speaker else frozenset()))
        written.append(values)
    await guard.check_content(
        conn, story_id, contents, base_revision=operation.base_revision, path=__name__
    )
    await insert_derived(conn, memories, written)
    await index_records(conn, [memory_record(SimpleNamespace(**r)) for r in written])
    ids = tuple(str(r["id"]) for r in written)
    inputs = sorted(set(covered))
    if inputs:
        await conn.execute(
            update(memories)
            .where(memories.c.story_id == story_id, memories.c.id.in_(inputs))
            .values(
                covered_by=func.array_cat(
                    memories.c.covered_by,
                    bindparam("covering", list(ids), type_=ARRAY(ulid_type())),
                ),
                # A record retired uncovered at an overflow deadline is covered now (#147).
                retired_reason=case(
                    (memories.c.retired_reason == "uncovered", "covered"),
                    else_=memories.c.retired_reason,
                ),
            )
        )
    for hook in list(MEMORY_HOOKS):
        await hook(conn, story_id, ids)
    return await memories_by_id(conn, story_id, ids)


async def write_author(
    conn: AsyncConnection,
    operation: MemoryOperation,
    fact: Fact,
    text: str,
    *,
    settings: ProfileSettings | None = None,
) -> Memory | None:
    """The author memory of an author fact (#145; FR-GUARD-4's canonize): a ``semantic`` row,
    provenance ``author`` (trust tier ``author``), position-less like the fact, wrapping the
    fact's proposition with the fact's visibility and its subject and object as participants,
    sourced from the fact's own event; ``text`` is the fact's context line. Inert (``None``)
    unless the story keeps episodic memory. Runs the resurrection guard, the erasure index and
    ``MEMORY_HOOKS`` (embedding, importance) like every writer."""
    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("a memory belongs to a story", {"field": "operation"})
    if not await episodic_on(conn, story_id, settings):
        return None
    line = text.strip()[:TEXT_MAX]
    if not line:
        return None
    p = fact.proposition
    named = [i for i in (p.subject_id, p.object_id, *(p.object_set or ())) if i is not None]
    known = {
        str(e)
        for e in await conn.scalars(
            select(entities.c.id).where(entities.c.story_id == story_id, entities.c.id.in_(named))
        )
    }
    provenance = RecordProvenance.AUTHOR
    row: dict[str, Any] = {
        "id": new_ulid(),
        "story_id": story_id,
        "kind": MemoryKind.SEMANTIC.value,
        "text": line,
        "holder_character_id": None,
        "character_id": None,
        "visibility": fact.visibility.document(),
        "provenance": provenance.value,
        "trust_tier": trust_tier_of(provenance).value,
        "operation_id": operation.operation_id,
        "source_event_ids": list(fact.source_event_ids),
        "narrative_turn": None,
        "turn_variant": None,
        "beat": None,
        "proposition_ids": [fact.proposition_id],
        "participants": list(dict.fromkeys(i for i in named if i in known)),
    }
    await guard.check_content(
        conn,
        story_id,
        [guard.Content((line,), frozenset(fact.visibility.characters or ()))],
        base_revision=operation.base_revision,
        path=__name__,
    )
    await insert_derived(conn, memories, [row])
    await index_records(conn, [memory_record(SimpleNamespace(**row))])
    ids = (str(row["id"]),)
    for hook in list(MEMORY_HOOKS):
        await hook(conn, story_id, ids)
    return (await memories_by_id(conn, story_id, ids))[0]


async def write_resolution(
    conn: AsyncConnection,
    operation: MemoryOperation,
    *,
    holder: str,
    act: str,
    at: NarrativePosition,
    source_event_ids: Sequence[str] = (),
    settings: ProfileSettings | None = None,
) -> Memory | None:
    """The bound character's backdated memory of the act a thunk resolution names (#165,
    FR-COMMIT-14): an ``episodic`` row of ``holder`` alone (``characters: [holder]``),
    provenance ``resolution``, its text made from the thunk's sketch without a model call
    ("Bobby knocked at the porch."), anchored at the thunk's introduction (``at``: its turn,
    variant and beat) and written by the resolving operation, so it follows that operation's
    membership; importance takes the default. Inert (``None``) unless the story keeps episodic
    memory. Runs the resurrection guard, the erasure index and ``MEMORY_HOOKS`` like every
    writer."""
    from epistrel.core.visibility import private_to

    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("a memory belongs to a story", {"field": "operation"})
    if not await episodic_on(conn, story_id, settings):
        return None
    name = await conn.scalar(
        select(characters.c.name).where(
            characters.c.story_id == story_id, characters.c.id == holder
        )
    )
    line = f"{name or 'You'} {act.strip()}.".strip()[:TEXT_MAX]
    provenance = RecordProvenance.RESOLUTION
    row: dict[str, Any] = {
        "id": new_ulid(),
        "story_id": story_id,
        "kind": MemoryKind.EPISODIC.value,
        "text": line,
        "holder_character_id": holder,
        "character_id": holder,
        "visibility": private_to(holder).document(),
        "provenance": provenance.value,
        "trust_tier": trust_tier_of(provenance).value,
        "operation_id": operation.operation_id,
        "source_event_ids": list(source_event_ids),
        "narrative_turn": at.turn,
        "turn_variant": at.variant,
        "beat": at.beat,
        "proposition_ids": [],
        "participants": [holder],
    }
    await guard.check_content(
        conn,
        story_id,
        [guard.Content((line,), frozenset({holder}))],
        base_revision=operation.base_revision,
        path=__name__,
    )
    await insert_derived(conn, memories, [row])
    await index_records(conn, [memory_record(SimpleNamespace(**row))])
    ids = (str(row["id"]),)
    for hook in list(MEMORY_HOOKS):
        await hook(conn, story_id, ids)
    return (await memories_by_id(conn, story_id, ids))[0]


SEED_IMPORTANCE = 0.8
"""The importance a seed memory starts with: the author's knowledge (#149)."""


@dataclass(frozen=True, slots=True)
class SeedMemory:
    """One memory structured genesis writes for a knowledge seed (#149): ``semantic`` when the
    seed normalized to canon (its ``proposition_ids``), ``episodic`` for free text; the seed's
    holder (``None`` for the world's), its visibility, its participants and the events that
    caused it."""

    kind: MemoryKind
    text: str
    visibility: Visibility
    source_event_ids: tuple[str, ...]
    holder_character_id: str | None = None
    proposition_ids: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()


async def write_seed(
    conn: AsyncConnection, operation: MemoryOperation, seeds: Sequence[SeedMemory]
) -> list[Memory]:
    """The seed memories of a structured story's genesis (#149), in the genesis operation's
    commit: provenance ``seed`` (trust tier ``author``), importance ``SEED_IMPORTANCE`` from the
    genesis position, the operation's narrative position. The caller has checked that the story
    keeps episodic memory. Runs the resurrection guard, the erasure index and ``MEMORY_HOOKS``
    (embedding, importance) like every writer."""
    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("a memory belongs to a story", {"field": "operation"})
    provenance = RecordProvenance.SEED
    rows: list[dict[str, Any]] = []
    contents: list[guard.Content] = []
    for seed in seeds:
        line = seed.text.strip()[:TEXT_MAX]
        if not line:
            continue
        if seed.kind not in (MemoryKind.EPISODIC, MemoryKind.SEMANTIC):
            raise ValueError("a seed memory is episodic or semantic")
        holder = seed.holder_character_id
        contents.append(guard.Content((line,), frozenset({holder}) if holder else frozenset()))
        rows.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "kind": seed.kind.value,
                "text": line,
                "holder_character_id": holder,
                "character_id": None,
                "visibility": seed.visibility.document(),
                "provenance": provenance.value,
                "trust_tier": trust_tier_of(provenance).value,
                "importance": SEED_IMPORTANCE,
                "importance_at": operation.narrative_turn,
                "operation_id": operation.operation_id,
                "source_event_ids": list(seed.source_event_ids),
                "narrative_turn": operation.narrative_turn,
                "turn_variant": operation.turn_variant,
                "beat": None,
                "proposition_ids": list(seed.proposition_ids),
                "participants": list(dict.fromkeys(seed.participants)),
            }
        )
    if not rows:
        return []
    await guard.check_content(
        conn, story_id, contents, base_revision=operation.base_revision, path=__name__
    )
    await insert_derived(conn, memories, rows)
    await index_records(conn, [memory_record(SimpleNamespace(**r)) for r in rows])
    ids = tuple(str(r["id"]) for r in rows)
    for hook in list(MEMORY_HOOKS):
        await hook(conn, story_id, ids)
    return await memories_by_id(conn, story_id, ids)


async def after_rewrite(conn: AsyncConnection, story_id: str, ids: tuple[str, ...]) -> None:
    """Run ``MEMORY_HOOKS`` for memories whose text was rewritten (#145's sanitize): their
    vectors were cleared, so the embed job re-embeds them."""
    for hook in list(MEMORY_HOOKS):
        await hook(conn, story_id, ids)


def _beat_parts(
    type_: str, actor: str | None, payload: Mapping[str, Any]
) -> list[tuple[BeatKind, str | None, str]]:
    """``(kind, actor, text)`` of each beat an M2/M3 beat event records: a player input's
    ``say`` and ``do`` (its ``text`` when it has neither); a reply voiced by one character as
    that character's ``say``, any other reply as narration; ``think`` and ``latent`` as
    themselves."""
    if type_ == "player_input":
        parts: list[tuple[BeatKind, str | None, str]] = []
        for part in ("say", "do"):
            value = payload.get(part)
            if isinstance(value, str) and value.strip():
                parts.append(("say" if part == "say" else "do", actor, value))
        return parts or [("say", actor, str(payload.get("text") or ""))]
    if type_ == "message_rendered":
        voiced = [str(c) for c in payload.get("render_character_ids") or ()]
        text = str(payload.get("text") or "")
        if len(voiced) == 1:
            return [("say", voiced[0], text)]
        return [("narration", None, text)]
    if type_.startswith("beat_"):
        structured = cast(BeatKind, type_.removeprefix("beat_"))
        return [(structured, None if structured == "narration" else actor, str(payload["text"]))]
    kind: BeatKind = "think" if type_ == "think" else "latent"
    return [(kind, actor, str(payload.get("text") or ""))]


def not_composed() -> Any:
    """Not a structured turn's composed message (#150): its beats are read instead."""
    return or_(
        events.c.type != "message_rendered",
        not_(events.c.payload.has_key("composed")),
    )


async def beats_of_operation(conn: AsyncConnection, operation_id: str) -> list[Beat]:
    """The beats operation ``operation_id`` recorded in its M2/M3 beat events, in ``seq``
    order: the player input's ``say`` and ``do`` (provenance ``player``); a reply voiced by one
    character as that character's ``say`` (``render``), any other reply as ``narration``
    (``director``); and ``think``/``latent`` as themselves."""
    found = await conn.execute(
        select(events.c.id, events.c.type, events.c.beat, events.c.character_id, events.c.payload)
        .where(
            events.c.operation_id == operation_id,
            events.c.type.in_(BEAT_EVENT_TYPES),
            not_composed(),
        )
        .order_by(events.c.seq)
    )
    beats: list[Beat] = []
    for row in found:
        payload = dict(row.payload or {})
        actor = None if row.character_id is None else str(row.character_id)
        for kind, character, text in _beat_parts(row.type, actor, payload):
            if row.type == "player_input":
                provenance = RecordProvenance.PLAYER
            elif kind == "narration":
                provenance = RecordProvenance.DIRECTOR
            else:
                provenance = RecordProvenance.RENDER
            beats.append(
                Beat(
                    kind,
                    text,
                    str(row.id),
                    beat=row.beat,
                    character_id=character,
                    provenance=provenance,
                )
            )
    return beats


# -- working memory -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkingBeat:
    """One beat of working memory: where it is, what kind, whose and what it says."""

    event_id: str
    narrative_turn: int
    turn_variant: int | None
    beat: int | None
    kind: BeatKind
    character_id: str | None
    text: str


Position = int | tuple[int, int] | NarrativePosition
"""A narrative position: a turn, ``(turn, beat)``, or the shared ``core.membership.Position``
(its turn and beat bound the beats; #164)."""


def _working(row: Any) -> list[WorkingBeat]:
    actor = None if row.character_id is None else str(row.character_id)
    return [
        WorkingBeat(
            event_id=str(row.id),
            narrative_turn=int(row.narrative_turn),
            turn_variant=row.turn_variant,
            beat=row.beat,
            kind=kind,
            character_id=character,
            text=text,
        )
        for kind, character, text in _beat_parts(row.type, actor, dict(row.payload or {}))
    ]


async def working_memory(
    conn: AsyncConnection,
    story_id: str,
    character_id: str | None,
    through_position: Position | None = None,
    n: int = DEFAULT_WORKING_MEMORY_BEATS,
) -> list[WorkingBeat]:
    """The last ``n`` beat events of the active timeline at or before ``through_position`` (the
    head when ``None``), oldest first, as ``character_id`` perceives them (see the module
    docstring). Read from ``events``; nothing is stored."""
    if n < 1:
        return []
    e, o = events, operations
    where = [
        e.c.story_id == story_id,
        e.c.type.in_(BEAT_EVENT_TYPES),
        e.c.category.in_([EventCategory.NARRATIVE_STATE.value, EventCategory.INPUT.value]),
        e.c.narrative_turn.is_not(None),
        e.c.slot_k.is_(None),
        o.c.status == OperationStatus.COMMITTED.value,
        active_operation(o),
    ]
    if through_position is not None:
        turn, beat = (
            (through_position, None)
            if isinstance(through_position, int)
            else (through_position.turn, through_position.beat)
            if isinstance(through_position, NarrativePosition)
            else through_position
        )
        if beat is None:
            where.append(e.c.narrative_turn <= turn)
        else:
            where.append(
                or_(
                    e.c.narrative_turn < turn,
                    and_(e.c.narrative_turn == turn, or_(e.c.beat.is_(None), e.c.beat <= beat)),
                )
            )
    where.append(not_composed())
    if character_id is not None:
        where.append(
            or_(
                e.c.type.in_(OBSERVED_TYPES),
                and_(e.c.type.in_(PRIVATE_TYPES), e.c.character_id == character_id),
            )
        )
    found = await conn.execute(
        select(
            e.c.id,
            e.c.type,
            e.c.narrative_turn,
            e.c.turn_variant,
            e.c.beat,
            e.c.character_id,
            e.c.payload,
        )
        .join(o, o.c.operation_id == e.c.operation_id)
        .where(*where)
        .order_by(e.c.narrative_turn.desc(), e.c.seq.desc())
        .limit(n)
    )
    beats = [b for row in reversed(found.all()) for b in _working(row)]
    return beats[-n:]


# -- importance ----------------------------------------------------------------------------------


def decayed_importance(
    importance: float | None, importance_at: int | None, now: int, half_life: float
) -> float:
    """``importance`` decayed from committed turn ``importance_at`` to ``now`` with
    ``half_life`` turns: ``importance · 0.5^((now - importance_at) / half_life)``. An unscored
    memory counts as ``DEFAULT_IMPORTANCE``; no age (``importance_at`` unknown, or ``now``
    earlier, as after a truncation) is no decay. Pure; never stored."""
    if half_life <= 0:
        raise ValueError("an importance half-life is a positive number of turns")
    value = DEFAULT_IMPORTANCE if importance is None else float(importance)
    age = 0 if importance_at is None else max(0, now - importance_at)
    decayed: float = value * 0.5 ** (age / half_life)
    return decayed


async def half_life_turns(conn: AsyncConnection) -> int:
    """The deployment's ``memory_importance_half_life_turns``."""
    return (await read_section(conn, MemorySettings)).memory_importance_half_life_turns


async def committed_turns(conn: AsyncConnection, story_id: str) -> int:
    """How many committed turns the story's active chain holds (the clock importance decays
    by: a truncation shrinks it, an untruncation restores it)."""
    found = await conn.scalar(
        select(func.count(func.distinct(operations.c.narrative_turn))).where(
            operations.c.story_id == story_id,
            operations.c.kind == OperationKind.TURN.value,
            operations.c.status == OperationStatus.COMMITTED.value,
            active_operation(operations),
        )
    )
    return int(found or 0)


async def unscored(conn: AsyncConnection, story_id: str, limit: int) -> list[Memory]:
    """The story's active hot memories that have no importance yet, oldest first."""
    found = await conn.execute(
        select(memories)
        .where(
            memories.c.story_id == story_id,
            active_only(memories),
            memories.c.retrieval_tier == RetrievalTier.HOT.value,
            memories.c.importance.is_(None),
        )
        .order_by(memories.c.created_at, memories.c.id)
        .limit(limit)
    )
    return [_memory(r) for r in found]


async def stories_with_unscored(conn: AsyncConnection) -> list[str]:
    """Every story with an active hot memory that has no importance yet."""
    found = await conn.scalars(
        select(memories.c.story_id)
        .where(
            active_only(memories),
            memories.c.retrieval_tier == RetrievalTier.HOT.value,
            memories.c.importance.is_(None),
        )
        .group_by(memories.c.story_id)
        .order_by(memories.c.story_id)
    )
    return [str(s) for s in found]


async def set_importance(conn: AsyncConnection, story_id: str, scores: Mapping[str, float]) -> int:
    """Write each scored memory's importance (clamped to 0..1), with ``importance_at`` its own
    turn (or, position-less, the story's committed turns now), only where no importance is set
    yet: an author's or an earlier score is never overwritten. Answers the rows written."""
    if not scores:
        return 0
    now = await committed_turns(conn, story_id)
    written = 0
    for memory_id, score in sorted(scores.items()):
        result = await conn.execute(
            update(memories)
            .where(
                memories.c.story_id == story_id,
                memories.c.id == memory_id,
                memories.c.importance.is_(None),
            )
            .values(
                importance=min(1.0, max(0.0, float(score))),
                importance_at=func.coalesce(memories.c.narrative_turn, now),
            )
        )
        written += int(result.rowcount)
    return written


# -- reads ---------------------------------------------------------------------------------------


async def memories_by_id(conn: AsyncConnection, story_id: str, ids: Iterable[str]) -> list[Memory]:
    """The story's memories ``ids`` (any membership), in the order given."""
    wanted = list(ids)
    found = await conn.execute(
        select(memories).where(memories.c.story_id == story_id, memories.c.id.in_(wanted))
    )
    by_id = {str(r.id): _memory(r) for r in found}
    return [by_id[i] for i in wanted if i in by_id]


async def memories_linked(
    conn: AsyncConnection,
    story_id: str,
    *,
    proposition_ids: Sequence[str] = (),
    source_event_ids: Sequence[str] = (),
    limit: int | None = None,
) -> list[Memory]:
    """The story's active memories that wrap one of ``proposition_ids`` or come from one of
    ``source_event_ids`` (#142's ``fact_neighborhood``): first those linked by proposition,
    then those linked only by source event, each by position then id, up to ``limit``."""
    props, sources = list(dict.fromkeys(proposition_ids)), list(dict.fromkeys(source_event_ids))
    if (not props and not sources) or limit == 0:
        return []
    m = memories
    by_proposition = (
        m.c.proposition_ids.overlap(bindparam("linked_props", props, type_=ARRAY(ulid_type())))
        if props
        else false()
    )
    by_source = (
        m.c.source_event_ids.overlap(bindparam("linked_sources", sources, type_=ARRAY(ulid_type())))
        if sources
        else false()
    )
    query = (
        select(m)
        .where(m.c.story_id == story_id, active_only(m), or_(by_proposition, by_source))
        .order_by(
            case((by_proposition, 0), else_=1),
            m.c.narrative_turn.asc().nulls_first(),
            m.c.beat.asc().nulls_first(),
            m.c.id,
        )
    )
    if limit is not None:
        query = query.limit(limit)
    return [_memory(r) for r in await conn.execute(query)]


async def memories_through(
    conn: AsyncConnection, story_id: str, through_turn: int | None = None
) -> list[Memory]:
    """The story's memories in the corrected view through ``through_turn`` (the head when
    ``None``): active, at or before it (position-less rows always), and every source event of
    an active committed operation at or before it (FR-STORE-13). Ordered by position."""
    m, e, o = memories, events, operations
    source_out = and_(
        e.c.id == any_(m.c.source_event_ids),
        or_(
            not_(active_operation(o)),
            o.c.status != OperationStatus.COMMITTED.value,
            *(
                ()
                if through_turn is None
                else (and_(e.c.narrative_turn.is_not(None), e.c.narrative_turn > through_turn),)
            ),
        ),
    )
    where = [
        m.c.story_id == story_id,
        active_only(m),
        not_(
            exists(select(e.c.id).join(o, o.c.operation_id == e.c.operation_id).where(source_out))
        ),
    ]
    if through_turn is not None:
        where.append(or_(m.c.narrative_turn.is_(None), m.c.narrative_turn <= through_turn))
    found = await conn.execute(
        select(m)
        .where(*where)
        .order_by(
            m.c.narrative_turn.asc().nulls_first(),
            m.c.turn_variant.asc().nulls_first(),
            m.c.beat.asc().nulls_first(),
            m.c.created_at,
            m.c.id,
        )
    )
    return [_memory(r) for r in found]


__all__ = [
    "BEAT_EVENT_TYPES",
    "CONSOLIDATED_KINDS",
    "DEFAULT_IMPORTANCE",
    "DEFAULT_WORKING_MEMORY_BEATS",
    "EPISODIC_HOOKS",
    "EPISODIC_MODULE",
    "MEMORY_HOOKS",
    "OBSERVABLE",
    "ROUTED",
    "SEED_IMPORTANCE",
    "Beat",
    "BeatKind",
    "Memory",
    "MemoryHook",
    "MemoryOperation",
    "Position",
    "SeedMemory",
    "WorkingBeat",
    "after_rewrite",
    "beats_of_operation",
    "committed_turns",
    "decayed_importance",
    "episodic_on",
    "half_life_turns",
    "memories_by_id",
    "memories_linked",
    "memories_through",
    "memory_sink",
    "participants_of",
    "perceived_line",
    "register_episodic_hook",
    "register_memory_hook",
    "remove_memory_hook",
    "render_line",
    "set_importance",
    "stories_with_unscored",
    "trust_tier_of",
    "unscored",
    "working_memory",
    "write_author",
    "write_consolidated",
    "write_perceived",
    "write_resolution",
    "write_seed",
]

# #147's hot-set counters register their hooks (after everything above is defined, so the
# import cycle is harmless).
from epistrel.core.memory import retirement as _retirement  # noqa: E402, F401 - registers hooks
