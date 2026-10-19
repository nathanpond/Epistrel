"""Consolidation's deterministic half: windows, scopes, inputs, coverage, the modality rule and
the cascade (FR-MEM-2,3,6,9,10, FR-STORE-13,26,27; docs/02 §4.2; #143). The model call and the
job are the Orchestrator's (``orchestrator.jobs.handlers.consolidation``); nothing here calls a
model.

**Scopes** (FR-MEM-9). One consolidation reads one viewpoint scope. ``Scope()`` is the
story-level ``shared`` scope: memories nobody holds whose visibility is ``objective`` or
``public`` (one scope class: in M4 every render-derived memory is shared knowledge), and the
canon facts of the same visibility. ``Scope(characters=(A, ...))`` reads the memories and facts
scoped to characters that include any of them. A derived record's visibility is the
**intersection** of its inputs' (``intersection``): all ``objective`` stays objective, any
``public`` makes it public, and a ``characters`` input narrows it to the characters every such
input names; an empty intersection (two characters' private memories) is refused when the job
is planned (``ScopeMismatch``, 422 ``scope_mismatch``), before any model call. Automatic
triggers consolidate the shared scope (M5's knowledge partitions add per-holder windows).

**Windows and coverage.** A window is a run of whole turns ``from_turn..to_turn``. Each
committed window is one ``consolidation`` operation positioned at ``to_turn`` on the active
chain, whose ``source_window`` is ``{"consolidation": "memories", from_turn, to_turn, scope,
level, inputs_hash, input_versions, explicit}`` (FR-STORE-27: its job's source window and the
version of every input). That operation is the coverage record, whether or not the model made
any output, and its outputs follow its membership: a truncation to N < ``to_turn`` discards
them with it, an untruncation or a swap brings them back. ``coverage`` lists the active ones
overlapping a window; ``covered_to`` is the last turn consolidated in a scope (one backward
index scan from the head). Automatic windows are contiguous: ``next_window`` starts at the
first uncovered episodic memory after ``covered_to`` and fires when
``memory.consolidation.window_memories`` (50) memories are uncovered **or** the head is
``memory.consolidation.cadence_turns`` (20) turns past ``covered_to``, whichever comes first
(the owner's decision on #143: both triggers). The window holds at most ``window_memories``
memories and snaps to whole turns. ``force`` (after a truncation, untruncation, swap or fork,
and #147's overflow) takes whatever is uncovered without waiting for a trigger. Level 2
(``next_level2``) folds every ``LEVEL2_EVERY`` (5) level-1 windows into reflections over their
outputs.

**Inputs** (``collect``). Level 1: the window's hot, active episodic memories in the scope
(and those an overflow deadline retired uncovered, #147, so a later run covers them) and
its canon facts (current versions positioned in it; the only objective-canon inputs); level 2:
the window's active level-1 outputs. Each is declared by its version (``memories:<id>``, the
memory's membership and text digest; ``operations:<id>`` for its operation), so a truncation or
an erasure between the read and the commit drops the output (FR-STORE-27, #70). A window holds
at most ``MAX_INPUTS`` inputs, cut at a whole turn. ``inputs_hash`` is the **replay key**: a
digest of the inputs' kinds, positions and text digests with the scope and level, so it is the
same in a fork (ids are not part of it) and moves only when an input's content does.

**The modality rule** (``derive``; FR-MEM-9). A statement keeps the epistemic type of what it
rests on, whatever the model proposed: it may be ``objective`` only when every supporting input
is objective canon (or, at level 2, an objective output); a ``say`` beat (or an attributed
claim) makes it an ``attributed_claim`` of that speaker, one statement per speaker, its text
"<speaker> said …" ("Bobby said the bridge is out", never "the bridge is out"); anything else
(a deed, narration, a mix) is an ``observation``. A statement that names no valid support is
dropped (``Derived.unsupported``). Its visibility is the intersection of its supports'.

**The cascade** (FR-STORE-13,26). An output's ``source_event_ids`` are every input's source
events, so ``records.memories_through(N)`` includes it exactly when every source is active at or
before N; it is positioned at its window's end, so it is in the corrected view through any
``N >= to_turn`` and never earlier. ``resolve_reactivated`` runs in a membership change's
transaction: when an untruncation or a swap reactivates an original output while a rerun of an
overlapping window is active, the original wins and the rerun's outputs are ``superseded``
(FR-MEM-10's reuse rule).

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from sqlalchemy import Text, and_, or_, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import Fact, Visibility, canon
from epistrel.core.enums import MemoryKind, OperationKind, OperationStatus, RetrievalTier
from epistrel.core.errors import ScopeMismatch
from epistrel.core.input_versions import input_key
from epistrel.core.membership import active_only, active_operation, supersede
from epistrel.core.memory.records import CONSOLIDATED_KINDS
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.tables import characters, operations, stories
from epistrel.core.tables.memories import TEXT_MAX, memories

CONSOLIDATION_MODULE = "consolidation"
"""The profile module that turns consolidation on (``memory.consolidation.enabled``)."""

MARKER = "memories"
"""``source_window.consolidation`` of a memory consolidation's operation (the Lite rolling
summary's operations, also of kind ``consolidation``, carry none)."""

SHARED = "shared"
LEVEL2_EVERY = 5
"""Level-1 windows one level-2 reflection folds."""
CHUNK_RECORDS = 150
"""Records one model call carries."""
MAX_CHUNKS = 6
MAX_INPUTS = CHUNK_RECORDS * MAX_CHUNKS
"""The most inputs one window reads (900); a larger window is cut at a whole turn."""
TARGET_FACTS = 10
TARGET_REFLECTIONS = 5
"""What one call keeps at most (the template asks for no more)."""

Epistemic = Literal["objective", "claim", "observation"]


# -- scopes -----------------------------------------------------------------------------------


class Scope(BaseModel):
    """One viewpoint scope: shared (``characters`` ``None``) or the characters named."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    characters: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("characters")
    @classmethod
    def _sorted(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if value is None else tuple(sorted(set(value)))

    @property
    def key(self) -> str:
        """``shared``, or ``characters:<id>,<id>`` (the coverage key)."""
        return SHARED if self.characters is None else "characters:" + ",".join(self.characters)

    @classmethod
    def of_key(cls, key: str) -> Scope:
        if key == SHARED:
            return cls()
        return cls(characters=tuple(key.removeprefix("characters:").split(",")))


class ConsolidateRequest(BaseModel):
    """``POST /admin/stories/{id}/consolidate`` (``Engine.consolidate``): consolidate turns
    ``from_turn``..``to_turn`` of ``scope`` (the shared scope when omitted) now, rerunning a
    window already consolidated (FR-MEM-10's explicit request)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"examples": [{"from_turn": 1, "to_turn": 20}]},
    )

    from_turn: StrictInt = Field(ge=1, le=2_147_483_647, description="The window's first turn.")
    to_turn: StrictInt = Field(
        ge=1, le=2_147_483_647, description="The window's last turn (at or after `from_turn`)."
    )
    scope: Scope | None = Field(
        default=None,
        description="`{characters: [...]}` for memories private to those characters; the "
        "shared scope when omitted.",
    )

    @model_validator(mode="after")
    def _ordered(self) -> ConsolidateRequest:
        if self.to_turn < self.from_turn:
            raise ValueError("to_turn is at or after from_turn")
        return self


def intersection(visibilities: Iterable[Visibility]) -> Visibility | None:
    """The intersection of scopes (FR-MEM-9): every one ``objective`` → objective; with any
    ``public`` → public; ``characters`` inputs narrow it to the characters all of them name
    (``None`` when none is left). ``engine`` scopes never reach a consolidation."""
    found = list(visibilities)
    named = [set(v.characters or ()) for v in found if v.scope == "characters"]
    if named:
        common = set.intersection(*named)
        return Visibility(scope="characters", characters=tuple(sorted(common))) if common else None
    if found and all(v.scope == "objective" for v in found):
        return Visibility(scope="objective")
    return Visibility(scope="public")


def _scope_clause(scope: Scope) -> Any:
    m = memories.c
    if scope.characters is None:
        return and_(
            m.holder_character_id.is_(None),
            m.visibility["scope"].astext.in_(("objective", "public")),
        )
    return or_(
        m.holder_character_id.in_(scope.characters),
        and_(
            m.visibility["scope"].astext == "characters",
            m.visibility["characters"].has_any(array(list(scope.characters), type_=Text)),
        ),
    )


def _fact_in_scope(fact: Fact, scope: Scope) -> bool:
    if scope.characters is None:
        return fact.visibility.scope in ("objective", "public")
    return fact.visibility.scope == "characters" and bool(
        set(fact.visibility.characters or ()) & set(scope.characters)
    )


# -- settings ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsolidationSettings:
    """A story's consolidation dials, from its effective configuration."""

    enabled: bool
    cadence_turns: int
    window_memories: int

    @classmethod
    def of(cls, settings: ProfileSettings) -> ConsolidationSettings:
        dials = settings.memory.consolidation
        return cls(
            enabled=module_on(settings, CONSOLIDATION_MODULE),
            cadence_turns=dials.cadence_turns,
            window_memories=dials.window_memories,
        )


async def settings_of(conn: AsyncConnection, story_id: str) -> ConsolidationSettings:
    """The story's consolidation dials as its effective configuration stands."""
    found = await effective_configuration(conn, story_id, validate=False)
    return ConsolidationSettings.of(found.settings)


async def head_turn(conn: AsyncConnection, story_id: str) -> int:
    """The story's head: its last committed turn on the active chain (0 before any)."""
    found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    return max(0, int(found or 1) - 1)


# -- windows and coverage --------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Window:
    """Whole turns ``from_turn..to_turn``."""

    from_turn: int
    to_turn: int

    @property
    def label(self) -> str:
        if self.from_turn == self.to_turn:
            return f"Turn {self.from_turn}"
        return f"Turns {self.from_turn}-{self.to_turn}"

    def overlaps(self, other: Window) -> bool:
        return self.from_turn <= other.to_turn and other.from_turn <= self.to_turn

    def document(self) -> dict[str, int]:
        return {"from_turn": self.from_turn, "to_turn": self.to_turn}


@dataclass(frozen=True, slots=True)
class Covered:
    """One active consolidation operation: the window, scope and level it covers, its replay
    key and whether an explicit request made it."""

    operation_id: str
    window: Window
    scope: str
    level: int
    inputs_hash: str | None
    explicit: bool


def source_window(
    window: Window,
    scope: Scope,
    level: int,
    inputs_hash: str,
    input_versions: Mapping[str, Any],
    *,
    explicit: bool,
) -> dict[str, Any]:
    """The ``source_window`` a window's positioned operation stores (FR-STORE-27)."""
    return {
        "consolidation": MARKER,
        **window.document(),
        "scope": scope.key,
        "level": level,
        "inputs_hash": inputs_hash,
        "input_versions": dict(input_versions),
        "explicit": explicit,
    }


def _consolidations(story_id: str, scope_key: str | None, level: int | None) -> list[Any]:
    o = operations.c
    sw = o.source_window
    where = [
        o.story_id == story_id,
        o.kind == OperationKind.CONSOLIDATION.value,
        o.status == OperationStatus.COMMITTED.value,
        active_operation(operations),
        o.narrative_turn.is_not(None),
        sw["consolidation"].astext == MARKER,
    ]
    if scope_key is not None:
        where.append(sw["scope"].astext == scope_key)
    if level is not None:
        where.append(sw["level"].astext == str(level))
    return where


def _covered(row: Any) -> Covered:
    sw = dict(row.source_window or {})
    return Covered(
        operation_id=str(row.operation_id),
        window=Window(int(sw["from_turn"]), int(sw["to_turn"])),
        scope=str(sw.get("scope", SHARED)),
        level=int(sw.get("level", 1)),
        inputs_hash=sw.get("inputs_hash"),
        explicit=bool(sw.get("explicit", False)),
    )


async def coverage(
    conn: AsyncConnection,
    story_id: str,
    scope_key: str | None,
    level: int | None,
    window: Window | None = None,
) -> list[Covered]:
    """The active consolidation operations of ``scope_key`` and ``level`` (each when given)
    overlapping ``window`` (every one when ``None``), by window."""
    where = _consolidations(story_id, scope_key, level)
    if window is not None:
        where.append(operations.c.narrative_turn >= window.from_turn)
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.source_window)
        .where(*where)
        .order_by(operations.c.narrative_turn, operations.c.operation_id)
    )
    covered = [_covered(r) for r in found]
    if window is None:
        return covered
    return [c for c in covered if c.window.overlaps(window)]


async def covered_to(conn: AsyncConnection, story_id: str, scope_key: str, level: int) -> int:
    """The last turn ``scope_key`` is consolidated through at ``level`` (0 when none is): the
    newest active window's end, found scanning back from the head."""
    found = await conn.scalar(
        select(operations.c.narrative_turn)
        .where(*_consolidations(story_id, scope_key, level))
        .order_by(operations.c.narrative_turn.desc())
        .limit(1)
    )
    return int(found or 0)


async def next_window(
    conn: AsyncConnection,
    story_id: str,
    scope: Scope,
    dials: ConsolidationSettings,
    *,
    floor: int = 0,
    force: bool = False,
    head: int | None = None,
) -> Window | None:
    """The oldest uncovered window of ``scope`` that is due (see the module docstring), or
    ``None``. ``floor`` is a window this transaction is committing (not visible yet); ``head``
    a turn it is committing (the story's head is read otherwise)."""
    after = max(floor, await covered_to(conn, story_id, scope.key, 1))
    head = max(head or 0, await head_turn(conn, story_id))
    if head <= after:
        return None
    m = memories.c
    turns = list(
        await conn.scalars(
            select(m.narrative_turn)
            .where(
                m.story_id == story_id,
                m.kind == MemoryKind.EPISODIC.value,
                m.narrative_turn > after,
                m.narrative_turn <= head,
                active_only(memories),
                _scope_clause(scope),
            )
            .order_by(m.narrative_turn)
            .limit(dials.window_memories)
        )
    )
    if not turns:
        return None
    start = int(turns[0])
    if len(turns) >= dials.window_memories:
        return Window(start, int(turns[-1]))
    if force or head - after >= dials.cadence_turns:
        return Window(start, head)
    return None


async def next_level2(
    conn: AsyncConnection, story_id: str, scope: Scope, *, extra: Window | None = None
) -> Window | None:
    """The level-2 window due over ``LEVEL2_EVERY`` level-1 windows not yet folded (``extra`` is
    a level-1 window this transaction is committing), or ``None``."""
    after = await covered_to(conn, story_id, scope.key, 2)
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.source_window)
        .where(*_consolidations(story_id, scope.key, 1), operations.c.narrative_turn > after)
        .order_by(operations.c.narrative_turn)
        .limit(LEVEL2_EVERY)
    )
    windows = sorted(_covered(r).window for r in found)
    if extra is not None and extra.from_turn > after and extra not in windows:
        windows = sorted([*windows, extra])
    windows = windows[:LEVEL2_EVERY]
    if len(windows) < LEVEL2_EVERY:
        return None
    return Window(windows[0].from_turn, windows[-1].to_turn)


# -- inputs -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Input:
    """One record a consolidation reads: its input key (FR-STORE-27), what it is, its text and
    position, and the labels the modality rule and the scope rule read."""

    key: str
    record_id: str
    kind: Literal["episodic", "semantic", "reflection", "fact"]
    text: str
    narrative_turn: int
    beat: int | None
    operation_id: str
    source_event_ids: tuple[str, ...]
    visibility: Visibility
    epistemic: Epistemic
    speaker: str | None = None
    """The character a ``say`` beat (or an attributed claim) quotes."""
    holder: str | None = None
    participants: tuple[str, ...] = ()
    importance: float | None = None
    digest: str = ""

    @property
    def is_memory(self) -> bool:
        return self.kind != "fact"

    @property
    def replay_term(self) -> str:
        return f"{self.kind}|{self.narrative_turn}|{self.beat}|{self.digest}"


def _digest(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def _beat_epistemic(text: str, character_id: str | None, names: Mapping[str, str]) -> Epistemic:
    """An episodic memory's type, from its attributed line (``records.render_line``): a
    narration (no actor) or ``<name> does: …`` is an observation; ``<name> says: …`` (or any
    other line by an actor) is a claim of that actor."""
    if character_id is None:
        return "observation"
    name = names.get(character_id)
    if name and text.startswith(f"{name} does: "):
        return "observation"
    return "claim"


def _output_epistemic(modality: str | None) -> Epistemic:
    if modality == "objective":
        return "objective"
    if modality == "attributed_claim":
        return "claim"
    return "observation"


async def character_names(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    found = await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    )
    return {str(r.id): str(r.name) for r in found}


def _memory_input(row: Any, names: Mapping[str, str], level: int) -> Input:
    visibility = Visibility.model_validate(row.visibility)
    character_id = None if row.character_id is None else str(row.character_id)
    if level == 1:
        epistemic = _beat_epistemic(row.text, character_id, names)
    else:
        epistemic = _output_epistemic(row.modality)
    return Input(
        key=input_key(memories.name, str(row.id)),
        record_id=str(row.id),
        kind=MemoryKind(row.kind).value,
        text=row.text,
        narrative_turn=int(row.narrative_turn),
        beat=row.beat,
        operation_id=str(row.operation_id),
        source_event_ids=tuple(str(e) for e in row.source_event_ids),
        visibility=visibility,
        epistemic=epistemic,
        speaker=character_id if epistemic == "claim" else None,
        holder=None if row.holder_character_id is None else str(row.holder_character_id),
        participants=tuple(str(p) for p in row.participants),
        importance=None if row.importance is None else float(row.importance),
        digest=_digest(row.text),
    )


async def _fact_inputs(
    conn: AsyncConnection, story_id: str, window: Window, scope: Scope
) -> list[Input]:
    from epistrel.core.propositions.registry import load as load_registry
    from epistrel.core.resolution import context as resolution
    from epistrel.core.retrieval.pipeline import canon_text

    found = [
        f
        for f in await canon(conn, story_id, through_position=(window.to_turn, 0))
        if f.narrative_turn is not None
        and window.from_turn <= f.narrative_turn <= window.to_turn
        and _fact_in_scope(f, scope)
    ]
    if not found:
        return []
    index = await resolution.load_index(conn, story_id)
    registry = await load_registry(conn, story_id)
    predicates = {pid: p.name for pid, p in registry.predicates.items()}
    inputs = []
    for fact in found:
        text = canon_text(fact, index, predicates)[:TEXT_MAX]
        inputs.append(
            Input(
                key=input_key(operations.name, fact.operation_id),
                record_id=fact.id,
                kind="fact",
                text=text,
                narrative_turn=int(fact.narrative_turn or 0),
                beat=fact.beat,
                operation_id=fact.operation_id,
                source_event_ids=fact.source_event_ids,
                visibility=fact.visibility,
                epistemic="objective",
                participants=tuple(
                    p for p in (fact.subject_id, fact.proposition.object_id) if isinstance(p, str)
                ),
                digest=_digest(text),
            )
        )
    return inputs


async def collect(
    conn: AsyncConnection, story_id: str, window: Window, scope: Scope, level: int
) -> tuple[list[Input], bool]:
    """The inputs of ``window`` in ``scope`` at ``level`` (see the module docstring), oldest
    first, at most ``MAX_INPUTS``, and whether the window had to be cut (at a whole turn)."""
    names = await character_names(conn, story_id)
    m = memories.c
    kinds = (
        [MemoryKind.EPISODIC.value] if level == 1 else [k.value for k in sorted(CONSOLIDATED_KINDS)]
    )
    where = [
        m.story_id == story_id,
        m.kind.in_(kinds),
        m.narrative_turn >= window.from_turn,
        m.narrative_turn <= window.to_turn,
        active_only(memories),
        # Hot records, and those an overflow deadline retired uncovered: consolidation is how
        # they come to be covered later (FR-MEM-8, #147).
        or_(m.retrieval_tier == RetrievalTier.HOT.value, m.retired_reason == "uncovered"),
        # A quarantined memory is in no input window (#145, FR-GUARD-4).
        m.quarantined_at.is_(None),
        _scope_clause(scope),
    ]
    if level == 2:
        where.append(m.level == 1)
    rows = await conn.execute(
        select(memories)
        .where(*where)
        .order_by(m.narrative_turn, m.beat.asc().nulls_first(), m.created_at, m.id)
        .limit(MAX_INPUTS + 1)
    )
    found = [_memory_input(r, names, level) for r in rows]
    if level == 1:
        found.extend(await _fact_inputs(conn, story_id, window, scope))
        found.sort(key=lambda i: (i.narrative_turn, i.beat or 0, i.kind == "fact", i.record_id))
    if len(found) <= MAX_INPUTS:
        return found, False
    cut = found[MAX_INPUTS].narrative_turn
    kept = [i for i in found if i.narrative_turn < cut]
    return kept or [i for i in found if i.narrative_turn == cut][:MAX_INPUTS], True


def inputs_hash(inputs: Sequence[Input], scope: Scope, level: int) -> str:
    """The replay key (see the module docstring): the same inputs, by content and position,
    in the same scope at the same level, give the same key in every story and fork."""
    terms = sorted(i.replay_term for i in inputs)
    body = "\n".join([f"scope={scope.key}", f"level={level}", *terms])
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Plan:
    """What a consolidation of ``window`` reads: its inputs (oldest first; the window ends at
    the last input's turn when ``collect`` cut it), their replay key and visibility."""

    story_id: str
    window: Window
    scope: Scope
    level: int
    inputs: tuple[Input, ...]
    inputs_hash: str
    visibility: Visibility

    def chunks(self) -> list[tuple[Input, ...]]:
        """The inputs, ``CHUNK_RECORDS`` to a model call."""
        return [
            self.inputs[i : i + CHUNK_RECORDS] for i in range(0, len(self.inputs), CHUNK_RECORDS)
        ]


async def plan(
    conn: AsyncConnection, story_id: str, window: Window, scope: Scope, level: int = 1
) -> Plan | None:
    """The plan of ``window`` (``None`` when it holds no input). ``ScopeMismatch`` when the
    inputs' scopes have an empty intersection (FR-MEM-9: refused before any model call)."""
    inputs, cut = await collect(conn, story_id, window, scope, level)
    if not inputs:
        return None
    visibility = intersection(i.visibility for i in inputs)
    if visibility is None:
        scopes = sorted({",".join(i.visibility.characters or ()) for i in inputs})
        raise ScopeMismatch(
            "a consolidation's inputs span viewpoint scopes no output may combine",
            {"story_id": story_id, "scopes": scopes},
        )
    last = max(i.narrative_turn for i in inputs)
    return Plan(
        story_id=story_id,
        window=Window(window.from_turn, last) if cut else window,
        scope=scope,
        level=level,
        inputs=tuple(inputs),
        inputs_hash=inputs_hash(inputs, scope, level),
        visibility=visibility,
    )


# -- the modality rule ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Proposed:
    """One statement the model proposed: its kind (``semantic`` for a standing fact,
    ``reflection``), text, modality and supports (indexes into its call's records)."""

    kind: MemoryKind
    text: str
    modality: str
    supports: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Output:
    """One statement as the engine keeps it: its type and speaker recomputed from its supports
    (FR-MEM-9), its visibility their intersection."""

    kind: MemoryKind
    text: str
    modality: Literal["objective", "attributed_claim", "observation"]
    speaker: str | None
    supports: tuple[Input, ...]
    visibility: Visibility


@dataclass
class Derived:
    """What ``derive`` kept and what it dropped."""

    outputs: list[Output] = field(default_factory=list)
    unsupported: int = 0
    downgraded: int = 0
    duplicates: int = 0


_LOWERED = frozenset(
    [
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "there",
        "they",
        "he",
        "she",
        "we",
        "you",
        "his",
        "her",
        "their",
        "our",
        "my",
        "some",
        "all",
        "no",
        "every",
        "each",
        "one",
    ]
)
"""Sentence-initial words that read lower-case after "<name> said" (a name or another proper
noun keeps its capital)."""


def attributed(name: str, text: str) -> str:
    """``text`` as ``name``'s claim: "<name> said …" unless it already names them first."""
    body = text.strip()
    if body.casefold().startswith(name.casefold()):
        return body[:TEXT_MAX]
    first = body.split(" ", 1)[0]
    if first.casefold() in _LOWERED:
        body = first.casefold() + body[len(first) :]
    return f"{name} said {body}"[:TEXT_MAX]


def derive(
    proposed: Sequence[Proposed],
    records: Sequence[Input],
    names: Mapping[str, str],
    *,
    level: int,
    seen: set[str] | None = None,
) -> Derived:
    """The outputs ``proposed`` statements make over ``records`` (one call's), by the modality
    rule (see the module docstring). ``seen`` holds the case-folded texts already kept by this
    window's earlier calls (duplicates are dropped)."""
    seen = set() if seen is None else seen
    derived = Derived()
    kept = {MemoryKind.SEMANTIC: 0, MemoryKind.REFLECTION: 0}
    targets = {MemoryKind.SEMANTIC: TARGET_FACTS, MemoryKind.REFLECTION: TARGET_REFLECTIONS}
    for statement in proposed:
        kind = MemoryKind.REFLECTION if level == 2 else statement.kind
        indexes = sorted({i for i in statement.supports if 0 <= i < len(records)})
        supports = [records[i] for i in indexes]
        text = statement.text.strip()
        if not supports or not text:
            derived.unsupported += 1
            continue
        speakers = sorted({s.speaker for s in supports if s.epistemic == "claim" and s.speaker})
        made: list[tuple[str, str, str | None, list[Input]]] = []
        modality: str
        if speakers:
            others = [s for s in supports if s.epistemic != "claim"]
            for speaker in speakers:
                own = [s for s in supports if s.speaker == speaker and s.epistemic == "claim"]
                name = names.get(speaker, speaker)
                made.append((attributed(name, text), "attributed_claim", speaker, own + others))
            if statement.modality != "attributed_claim":
                derived.downgraded += 1
        elif all(s.epistemic == "objective" for s in supports):
            modality = "objective" if statement.modality == "objective" else "observation"
            made.append((text[:TEXT_MAX], modality, None, supports))
        else:
            if statement.modality == "objective":
                derived.downgraded += 1
            made.append((text[:TEXT_MAX], "observation", None, supports))
        for out_text, modality, said_by, rest in made:
            folded = out_text.casefold()
            if folded in seen:
                derived.duplicates += 1
                continue
            if kept[kind] >= targets[kind]:
                continue
            visibility = intersection(s.visibility for s in rest)
            if visibility is None:
                derived.unsupported += 1
                continue
            seen.add(folded)
            kept[kind] += 1
            derived.outputs.append(
                Output(
                    kind=kind,
                    text=out_text,
                    modality=modality,  # type: ignore[arg-type]
                    speaker=said_by,
                    supports=tuple(sorted(rest, key=lambda s: (s.narrative_turn, s.beat or 0))),
                    visibility=visibility,
                )
            )
    return derived


def output_row(
    output: Output,
    plan_: Plan,
    *,
    memory_id: str,
    model: str | None,
    model_version: str | None,
    prompt_hash: str | None,
) -> dict[str, Any]:
    """The ``records.write_consolidated`` row of ``output``: every input's source events (the
    cascade's only input), its supports' participants, importance the highest of theirs."""
    events = list(dict.fromkeys(e for i in plan_.inputs for e in i.source_event_ids))
    participants = list(dict.fromkeys(p for s in output.supports for p in s.participants))
    scored = [s.importance for s in output.supports if s.importance is not None]
    holder = None
    if output.visibility.scope == "characters" and len(output.visibility.characters or ()) == 1:
        holder = (output.visibility.characters or (None,))[0]
    return {
        "id": memory_id,
        "kind": output.kind.value,
        "text": output.text,
        "holder_character_id": holder,
        "character_id": output.speaker,
        "visibility": output.visibility.document(),
        "source_event_ids": events,
        "proposition_ids": [],
        "participants": participants,
        "importance": max(scored) if scored else None,
        "level": plan_.level,
        "modality": output.modality,
        "inputs_hash": plan_.inputs_hash,
        "model": model,
        "model_version": model_version,
        "prompt_hash": prompt_hash,
    }


# -- outputs and the cascade ---------------------------------------------------------------------


async def outputs_of(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str], *, active: bool = True
) -> list[str]:
    """The memories the consolidation operations ``operation_ids`` wrote (active ones only,
    unless ``active`` is false), by id."""
    if not operation_ids:
        return []
    where = [memories.c.story_id == story_id, memories.c.operation_id.in_(list(operation_ids))]
    if active:
        where.append(active_only(memories))
    return [
        str(i)
        for i in await conn.scalars(select(memories.c.id).where(*where).order_by(memories.c.id))
    ]


async def supersede_outputs(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> int:
    """Mark the active outputs of ``operation_ids`` ``superseded`` (a rerun replaced them)."""
    ids = await outputs_of(conn, story_id, operation_ids)
    return await supersede(conn, memories, ids, story_id=story_id)


async def resolve_reactivated(
    conn: AsyncConnection, story_id: str, operation_ids: Sequence[str]
) -> int:
    """In the transaction of a change that made ``operation_ids`` active: an original window
    reactivated while a rerun of an overlapping window of its scope and level is active wins;
    the rerun's outputs are superseded (FR-MEM-10). Returns the outputs superseded."""
    if not operation_ids:
        return 0
    ids = set(operation_ids)
    found = await conn.execute(
        select(operations.c.operation_id, operations.c.source_window).where(
            operations.c.operation_id.in_(sorted(ids)),
            operations.c.source_window["consolidation"].astext == MARKER,
        )
    )
    restored = [_covered(r) for r in found]
    losing: set[str] = set()
    for original in restored:
        for rival in await coverage(
            conn, story_id, original.scope, original.level, original.window
        ):
            if rival.operation_id not in ids:
                losing.add(rival.operation_id)
    return await supersede_outputs(conn, story_id, sorted(losing))


__all__ = [
    "CHUNK_RECORDS",
    "CONSOLIDATION_MODULE",
    "LEVEL2_EVERY",
    "MARKER",
    "MAX_CHUNKS",
    "MAX_INPUTS",
    "SHARED",
    "TARGET_FACTS",
    "TARGET_REFLECTIONS",
    "ConsolidateRequest",
    "ConsolidationSettings",
    "Covered",
    "Derived",
    "Input",
    "Output",
    "Plan",
    "Proposed",
    "Scope",
    "Window",
    "attributed",
    "character_names",
    "collect",
    "coverage",
    "covered_to",
    "derive",
    "head_turn",
    "inputs_hash",
    "intersection",
    "next_level2",
    "next_window",
    "output_row",
    "outputs_of",
    "plan",
    "resolve_reactivated",
    "settings_of",
    "source_window",
    "supersede_outputs",
]
