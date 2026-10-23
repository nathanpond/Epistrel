"""A character's generation context, built structurally from what it could know (NFR-PRIV-1,4,
FR-MEM-11, FR-TURN-8, FR-RET-7, FR-RESP-4, FR-CAT-12; docs/02 §11; #170).

**Privacy is structural first.** A character's prompt holds only what that character could
know. Context assembly is the primary privacy control, and output scanning is a secondary,
measured one: the scan catches some disclosures, and it is never proof that none occurred.
``build_context(conn, character_id, state)`` is the only producer of a character render's
knowledge sections (``CharacterContext``). ``orchestrator.structured.prompt.character_inputs``
takes nothing else, and ``python -m tools.gates check-character-context`` checks that
``structured_generation.j2`` is rendered only from inputs built that way. Its sections:

- **Beliefs** (FR-TURN-8): the character's own partition (#159), first-order beliefs held at
  ``suspected`` or above (never ``unknown``, never a closed row), established false beliefs
  included: they are the character's truth (FR-EPI-3). There are at most ``BELIEFS_MAX`` lines.
  Beliefs about what the retrieval query names come first, then by confidence, then the most
  recent. Each reads "you suspect / believe / know <statement>", with no scalar, provenance or
  date. The rest of the partition reaches the render through retrieval's ``beliefs`` arm
  (FR-TURN-8's "supplied" is met by the cap plus retrieval).
- **Nested beliefs and speech acts** (#167): "you think Bobby believes …" and "you heard Bobby
  say …" (``nested.describe`` over ``nested.held_nested`` and ``partitions.speech_acts``).
- **Working memory** (FR-MEM-11): ``core.memory.records.working_memory`` at the story's
  perception level, re-routed through the same routing as memory writes. Content the character
  perceived reads in full, an envelope as its envelope, and an unperceived beat is absent. No
  ``latent`` appears, and no other character's ``think``. This turn's earlier beats (the
  player's input, the narrator's narration) are routed over the turn's scene the same way
  (``PendingBeat``). It never includes the request's ``scene_context.recent_messages``
  (FR-RESP-4): this function's signature has no way to receive them.
- **Just now** (#174): in a ``sequential`` turn, the beats the earlier primaries rendered and the
  engine verified (``TurnState.stage``, ``core.epistemic.stage.TurnStage``), routed the same way
  (``working_memory(…, stage=)``) and kept apart from the working memory: they count against
  the prompt cap, not against ``render.working_memory_beats``, and their perceived text joins
  the retrieval query.
- **Retrieved context and canon** (#141, #133): retrieval for ``character:<id>``, whose
  viewpoint filter (#159) composes into every arm. The assembler takes only that already
  filtered set (``core.retrieval.filtered.ViewpointFiltered``, U-31). Every ``age`` canon fact
  about a character is left out: true ages travel only as below.
- **Your private memories** (#172, FR-VERIFY-9, FR-EPI-11; per-character partitions only): the
  character's own ``think`` memories, at most ``PRIVATE_MAX``, those retrieval ranked first, then
  the most recent, each "you thought: …". They sit in U-31's pins tier (never cut by the prompt
  cap), so a later render can resurface a recorded state (a grounded callback,
  ``core.epistemic.resurface``) rather than invent one. Omitted when there are none.
- **Ages** (FR-CAT-12, FR-RESP-12, S-AGE-ASSIGNMENT M5; per-character partitions only): the
  character's own true age ("You are 34."). Every other present character on the ``## Scene``
  line gets only an apparent band (``age_band``: "early twenties"), or the exact age when the
  character holds a belief about it.

**Lazy resolution at the need site** (#164's FR-BELIEF-5,6,9). With ``lazy_resolution`` on, the
character's unresolved exposures whose proposition names an entity the retrieval query resolved
are needed now: they are decided in their own frames (``exposure.decide``, a dry run that writes
nothing) for the context, at most ``exposure.max_lazy_resolutions`` of them. When the turn may
adjudicate testimony (``TurnState.adjudicate``, the orchestrator's hook: ``planner.adjudicates``
and the pool affords it), the model adjudicates them, the rendering character being the turn's
primary witness; otherwise the default decides. ``CharacterContext.needed`` carries them to the
turn's commit, which writes them through ``exposure.resolve_needed`` with the adjudicated
decisions.

**Shared knowledge** (``character_knowledge = shared``, every M4 profile): the same function
with perception ``none`` and an unfiltered retrieval, so there is one path. It writes no
beliefs and no ages.

A player's character has no generation context: it is never rendered (FR-EPI-12), so
``build_context`` raises ``InvalidRequest`` (``player_has_no_context``).

**The pure half** (``compose``): the sections from already-filtered inputs. ``build_context``
does the reads and calls it, and the PRIV property (``tests/property/test_priv.py``) drives it
over in-memory stories. Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import BeliefConfidenceLevel, ExposureState
from epistrel.core.epistemic import exposure as exposures
from epistrel.core.epistemic import nested, partitions, perception, testimony
from epistrel.core.epistemic import scene as scenes
from epistrel.core.errors import InvalidRequest
from epistrel.core.memory import records
from epistrel.core.memory.records import WorkingBeat
from epistrel.core.profiles.capabilities import module_on
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.propositions.vocabulary import AGE
from epistrel.core.retrieval.filtered import ViewpointFiltered, _seal
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX
from epistrel.core.tables import characters, entities

if TYPE_CHECKING:
    from epistrel.core.readset import StructuredReadSet
    from epistrel.core.retrieval.lexical import LexicalSearch
    from epistrel.core.retrieval.pipeline import BatchEmbedder, RetrievalResult
    from epistrel.core.retrieval.search import Embedder

PARTITIONS_MODULE = "partitions"

BELIEFS_MAX = 40
"""The most first-order belief lines one context carries (the rest reach it through retrieval)."""
NESTED_MAX = 20
"""The most nested-belief and speech-act lines one context carries, each."""
PRIVATE_MAX = 8
"""The most of a character's own ``think`` memories its context carries (#172)."""
QUERY_BEATS = 3
"""How many of the character's own latest perceived beats join the retrieval query."""
TEXT_MAX = 1_000

LEVEL_WORDS: Mapping[BeliefConfidenceLevel, str] = {
    BeliefConfidenceLevel.SUSPECTED: "suspect",
    BeliefConfidenceLevel.BELIEVED: "believe",
    BeliefConfidenceLevel.KNOWN: "know",
}


# -- ages ----------------------------------------------------------------------------------------

_DECADES = {
    2: "twenties",
    3: "thirties",
    4: "forties",
    5: "fifties",
    6: "sixties",
}


def age_band(true_age: int | None) -> str | None:
    """The apparent age band another character perceives: ``child`` under 13, ``teens`` to 19,
    each decade split into thirds by its last digit (0-3 ``early``, 4-6 ``mid``, 7-9 ``late``:
    "early twenties"), ``elderly`` from 70; ``None`` when there is no true age."""
    if true_age is None:
        return None
    if true_age < 13:
        return "child"
    if true_age < 20:
        return "teens"
    if true_age >= 70:
        return "elderly"
    digit = true_age % 10
    third = "early" if digit <= 3 else "mid" if digit <= 6 else "late"
    return f"{third} {_DECADES[true_age // 10]}"


# -- the context ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Needed:
    """One exposure the context needed (#164): its default's outcome as the frame decides it, and
    the model's adjudication when one was made (``decision``, ``confidence``)."""

    exposure: exposures.Exposure
    outcome: testimony.TestimonyOutcome
    decision: testimony.Decision | None = None
    confidence: float = 0.0

    @property
    def adjudicated(self) -> tuple[testimony.Decision, float] | None:
        return None if self.decision is None else (self.decision, self.confidence)


@dataclass(frozen=True, slots=True)
class CharacterContext:
    """One character render's knowledge sections (see the module docstring), rendered by
    ``structured_generation.j2``. Built only here (``build_context``, ``compose``)."""

    character_id: str
    beliefs: tuple[str, ...] = ()
    """"you suspect / believe / know …" lines (FR-TURN-8)."""
    nested: tuple[str, ...] = ()
    """"you think B believes …" lines (#167)."""
    speech_acts: tuple[str, ...] = ()
    """"you heard B say …" lines (#167)."""
    working_memory: tuple[str, ...] = ()
    just_now: tuple[str, ...] = ()
    """#174: ``## Just now``, this turn's staged beats of earlier primaries as the character
    perceived them (a ``sequential`` turn; never another character's ``think`` or a ``latent``)."""
    retrieved: tuple[str, ...] = ()
    canon: tuple[str, ...] = ()
    scene: str = ""
    own_age: str = ""
    """"You are N." (per-character partitions only)."""
    private_memories: tuple[str, ...] = ()
    """#172: "you thought: …" lines, the character's own ``think`` memories."""
    player_requests: tuple[str, ...] = ()
    """#180's section (empty until it lands)."""
    time_belief: None = None
    """M6's own time belief (FR-CLOCK-11)."""
    needed: tuple[Needed, ...] = field(default=(), compare=False)
    """The exposures this context needed, which the turn's commit resolves."""
    retrieval: RetrievalResult | None = field(default=None, compare=False)
    """The retrieval the sections came from (the turn's draft context, #145)."""
    report: Mapping[str, Any] = field(default_factory=dict, compare=False)
    """``turn_report.context``: ``context_ms``, ``perceived``, ``envelope``, ``dropped``
    (the items the assembler left out) and ``beliefs`` (lines)."""

    @property
    def belief_lines(self) -> tuple[str, ...]:
        """Every belief-state line, in prompt order."""
        return (*self.beliefs, *self.nested, *self.speech_acts)


@dataclass(frozen=True, slots=True)
class PendingBeat:
    """One of this turn's beats before the character renders (the player's input, the narrator's
    narration), routed over the turn's scene like history."""

    kind: Literal["say", "do", "narration"]
    actor: str | None
    text: str
    qualifiers: perception.Qualifiers = field(default_factory=perception.Qualifiers)


Adjudicator = Callable[
    [AsyncConnection, exposures.Exposure, testimony.TestimonyOutcome],
    Awaitable[tuple[testimony.Decision, float] | None],
]
"""The orchestrator's adjudication hook: the model's decision and confidence for one needed
exposure, or ``None`` (the default decides: adjudication off, capped or unaffordable)."""


@dataclass(frozen=True, slots=True)
class TurnState:
    """What the turn hands ``build_context``: the story, the turn, the effective settings, the
    player's input, this turn's earlier beats, the authoritative scene (its location's name and
    the entities present, as the turn's read set read them; ``scene_text`` the shared line), and
    the retrieval's ports. ``build_context`` does its own reads. There is no ``scene_context``
    field: consumer-supplied recent messages never reach a character context (FR-RESP-4)."""

    story_id: str
    turn: int
    settings: ProfileSettings
    say: str | None = None
    do: str | None = None
    pending: tuple[PendingBeat, ...] = ()
    location_name: str | None = None
    present: tuple[str, ...] = ()
    scene_text: str = ""
    read_set: StructuredReadSet | None = None
    embed: Embedder | None = None
    embed_batch: BatchEmbedder | None = None
    lexical: LexicalSearch | None = None
    adjudicate: Adjudicator | None = None
    scene_loader: records.SceneLoader | None = None
    stage: records.StagedWindow | None = None
    """#174: the turn's staged beats (``core.epistemic.stage.TurnStage``), read through
    ``working_memory(…, stage=)`` into ``## Just now``."""


# -- rendering -------------------------------------------------------------------------------------


def statement(p: Proposition, predicates: Mapping[str, str], names: Mapping[str, str]) -> str:
    """A proposition as a context line's statement: ``<subject> [not] <predicate> <object>``."""
    words = [names.get(p.subject_id, p.subject_id)]
    if p.polarity.value == "negated":
        words.append("not")
    words.append(predicates.get(p.predicate_id, p.predicate_id))
    if p.object_id is not None:
        words.append(names.get(p.object_id, p.object_id))
    if p.object_set:
        words.append(", ".join(names.get(o, o) for o in p.object_set))
    if p.literal is not None:
        unit = f" {p.literal.unit}" if p.literal.unit else ""
        words.append(f"{p.literal.value}{unit}")
    return " ".join(str(w) for w in words)[:TEXT_MAX]


DISCLOSURE_MARKS: Mapping[str, str] = {"secret": "(secret)", "guarded": "(guarded)"}
"""How a seeded belief's disclosure marks its context line (``beliefs.disclosure``; #173)."""


def belief_line(level: BeliefConfidenceLevel, text: str) -> str | None:
    """ "you suspect / believe / know <text>"; ``None`` at ``unknown``."""
    word = LEVEL_WORDS.get(BeliefConfidenceLevel(level))
    return None if word is None else f"you {word} {text}"


def memory_line(beat: WorkingBeat, names: Mapping[str, str], own: str) -> str:
    """One working-memory beat as a prompt line: ``Bobby says: …``, ``Bobby does: …``, a
    narration verbatim, an envelope as itself, an eavesdropper's part as ``Bobby says (you catch
    only part of it): …`` and the character's own thought as ``(You think) …``."""
    body = beat.text.strip()
    name = names.get(beat.character_id or "")
    if beat.fidelity == "envelope":
        return body
    if beat.fidelity == "partial" and name:
        return f"{name} says (you catch only part of it): {body}"
    if beat.kind == "say" and name:
        return f"{name} says: {body}"
    if beat.kind == "do" and name:
        return f"{name} does: {body}"
    if beat.kind == "think" and beat.character_id == own:
        return f"(You think) {body}"
    return body


# -- the pure half ---------------------------------------------------------------------------------


def compose(
    character_id: str,
    *,
    working: ViewpointFiltered[WorkingBeat],
    names: Mapping[str, str],
    just_now: ViewpointFiltered[WorkingBeat] | None = None,
    retrieved: ViewpointFiltered[str] | None = None,
    canon: ViewpointFiltered[str] | None = None,
    beliefs: Sequence[str] = (),
    nested_lines: Sequence[str] = (),
    speech_lines: Sequence[str] = (),
    scene: str = "",
    own_age: str = "",
    private_memories: Sequence[str] = (),
    needed: Sequence[Needed] = (),
    retrieval: RetrievalResult | None = None,
    report: Mapping[str, Any] | None = None,
) -> CharacterContext:
    """The context's sections from already-filtered inputs (see the module docstring). Pure."""
    if working.viewpoint != f"{CHARACTER_VIEWPOINT_PREFIX}{character_id}":
        raise ValueError("a character's working memory is filtered for that character")
    if just_now is not None and just_now.viewpoint != working.viewpoint:
        raise ValueError("a character's staged beats are filtered for that character")
    return CharacterContext(
        character_id=character_id,
        beliefs=tuple(beliefs[:BELIEFS_MAX]),
        nested=tuple(nested_lines[:NESTED_MAX]),
        speech_acts=tuple(speech_lines[:NESTED_MAX]),
        working_memory=tuple(memory_line(b, names, character_id) for b in working),
        just_now=tuple(memory_line(b, names, character_id) for b in (just_now or ())),
        retrieved=tuple(retrieved or ()),
        canon=tuple(canon or ()),
        scene=scene,
        own_age=own_age,
        private_memories=tuple(private_memories[:PRIVATE_MAX]),
        needed=tuple(needed),
        retrieval=retrieval,
        report=dict(report or {}),
    )


# -- the reads ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cast:
    names: dict[str, str]
    ages: dict[str, int | None]
    players: frozenset[str]
    entity_of: dict[str, str]
    character_of: dict[str, str]
    entity_names: dict[str, str]


async def _cast(conn: AsyncConnection, story_id: str) -> _Cast:
    found = await conn.execute(
        select(characters.c.id, characters.c.name, characters.c.is_player, characters.c.true_age)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    names: dict[str, str] = {}
    ages: dict[str, int | None] = {}
    players: set[str] = set()
    for row in found:
        names[str(row.id)] = str(row.name)
        ages[str(row.id)] = None if row.true_age is None else int(row.true_age)
        if row.is_player:
            players.add(str(row.id))
    entity_of: dict[str, str] = {}
    character_of: dict[str, str] = {}
    entity_names: dict[str, str] = {}
    for entity in await conn.execute(
        select(entities.c.id, entities.c.character_id, entities.c.canonical_name).where(
            entities.c.story_id == story_id
        )
    ):
        entity_names[str(entity.id)] = str(entity.canonical_name)
        if entity.character_id is not None:
            entity_of[str(entity.character_id)] = str(entity.id)
            character_of[str(entity.id)] = str(entity.character_id)
    return _Cast(names, ages, frozenset(players), entity_of, character_of, entity_names)


async def _pending(
    conn: AsyncConnection,
    state: TurnState,
    character_id: str,
    level: perception.Level,
) -> list[WorkingBeat]:
    """This turn's earlier beats as ``character_id`` perceives them (routed over the turn's
    scene at ``level``; everything at ``none``)."""
    beats = [
        WorkingBeat(
            event_id="",
            narrative_turn=state.turn,
            turn_variant=None,
            beat=1 if p.kind != "narration" else 2 + n,
            kind=p.kind,
            character_id=None if p.kind == "narration" else p.actor,
            text=p.text,
        )
        for n, p in enumerate(state.pending)
        if p.text.strip()
    ]
    if not beats or level == perception.NONE:
        return beats
    loader = state.scene_loader or scenes.load_scene
    scene, _ = await loader(conn, state.story_id, state.turn)
    qualifiers = [p.qualifiers for p in state.pending if p.text.strip()]
    found = await records.places_and_names(conn, state.story_id)
    return records.perceived_window(
        [
            (
                scene,
                [records.HistoryBeat(b, q) for b, q in zip(beats, qualifiers, strict=True)],
            )
        ],
        character_id,
        level,
        found[0],
        found[1],
    )


def _hidden_ages(result: RetrievalResult, character_entities: frozenset[str]) -> set[str]:
    return {
        f.id
        for f in result.canon
        if f.proposition.predicate_id == AGE and f.proposition.subject_id in character_entities
    }


async def _needed(
    conn: AsyncConnection,
    state: TurnState,
    character_id: str,
    about: Sequence[str],
    registry: Registry,
) -> list[Needed]:
    """The character's unresolved exposures on what the retrieval query named, decided in their
    frames (and adjudicated through ``state.adjudicate`` when it answers), oldest first, within
    the turn's lazy-resolution cap."""
    if not exposures.lazy_on(state.settings) or not about:
        return []
    named = set(about)
    waiting = [
        e
        for e in await exposures.exposures_of(
            conn, state.story_id, character_id, state=ExposureState.UNRESOLVED
        )
        if e.proposition.subject_id in named or e.proposition.object_id in named
    ][: exposures.max_lazy_resolutions(state.settings)]
    found: list[Needed] = []
    for item in waiting:
        outcome = await exposures.decide(conn, item, registry=registry, read_set=state.read_set)
        decision: tuple[testimony.Decision, float] | None = None
        if state.adjudicate is not None:
            decision = await state.adjudicate(conn, item, outcome)
        if decision is not None:
            intent = testimony.perceived(await _intent(conn, item.source_event_id))
            taken = testimony.apply_adjudication(outcome, decision[0], decision[1], intent)
            if taken is not None:
                found.append(Needed(item, taken, decision[0], decision[1]))
                continue
        found.append(Needed(item, outcome))
    return found


async def _intent(conn: AsyncConnection, event_id: str) -> str | None:
    from epistrel.core.tables import events

    payload = await conn.scalar(select(events.c.payload).where(events.c.id == event_id))
    return payload.get("intent") if isinstance(payload, dict) else None


def _needed_lines(
    needed: Sequence[Needed],
    predicates: Mapping[str, str],
    names: Mapping[str, str],
    source_names: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    """The belief and "you heard" lines the needed exposures resolve to."""
    beliefs: list[str] = []
    heard: list[str] = []
    for n in needed:
        p = n.exposure.proposition
        speaker = source_names.get(n.exposure.source or "", "someone")
        heard.append(f"you heard {speaker} say {statement(p, predicates, names)}")
        o = n.outcome
        if o.transition is None or o.confidence is None or o.proposition is None:
            continue
        held = o.proposition.negation() if o.decision == testimony.REJECT else o.proposition
        line = belief_line(o.confidence, statement(held, predicates, names))
        if line is not None:
            beliefs.append(line)
    return beliefs, heard


async def _belief_lines(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    relevant: frozenset[str],
    predicates: Mapping[str, str],
    names: Mapping[str, str],
) -> list[str]:
    held = await partitions.partition(
        conn,
        story_id,
        character_id,
        levels=partitions.HELD_LEVELS,
        order="confidence",
        limit=BELIEFS_MAX * 5,
    )
    ranked = sorted(enumerate(held), key=lambda item: (item[1].id not in relevant, item[0]))
    found: list[str] = []
    for _, belief in ranked:
        if belief.held_to is not None or belief.tx_to is not None:
            continue
        said = statement(belief.proposition, predicates, names)
        line = belief_line(belief.confidence_level, said)
        if line is not None:
            # #173: a seeded secret or guarded belief says so; how the character keeps it
            # follows its own honesty disposition (the definition block), never the role.
            mark = DISCLOSURE_MARKS.get(belief.disclosure or "")
            found.append(line if mark is None else f"{line} {mark}")
        if len(found) >= BELIEFS_MAX:
            break
    return found


async def _nested_lines(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    predicates: Mapping[str, str],
    names: Mapping[str, str],
    character_names: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    held = await nested.held_nested(conn, story_id, character_id)
    said = await partitions.speech_acts(conn, story_id, character_id)

    def line(found: partitions.NestedBelief | partitions.Belief) -> str:
        inner = found.belief if isinstance(found, partitions.NestedBelief) else found
        text = statement(inner.proposition, predicates, names)
        return nested.describe(found, character_names, text)

    return [line(n) for n in held[:NESTED_MAX]], [line(s.belief) for s in said[-NESTED_MAX:]]


async def _private_lines(
    conn: AsyncConnection, story_id: str, character_id: str, ranked: Sequence[str]
) -> list[str]:
    """#172: the character's own ``think`` memories as "you thought: …" lines, those retrieval
    ranked (``ranked``, memory ids in order) first, then the most recent."""
    from epistrel.core.membership import active_only
    from epistrel.core.tables.memories import memories

    m = memories.c
    rows = list(
        await conn.execute(
            select(m.id, m.text)
            .where(
                m.story_id == story_id,
                m.holder_character_id == character_id,
                m.channel == "think",
                m.quarantined_at.is_(None),
                active_only(memories),
            )
            .order_by(m.narrative_turn.desc(), m.beat.desc(), m.id.desc())
            .limit(PRIVATE_MAX * 8)
        )
    )
    order = {memory_id: i for i, memory_id in enumerate(ranked)}
    rows.sort(key=lambda r: order.get(str(r.id), len(order)))
    lines = []
    for row in rows[:PRIVATE_MAX]:
        body = str(row.text)
        body = body.split(" thinks: ", 1)[1] if " thinks: " in body else body
        lines.append(f"you thought: {body.strip()}"[:TEXT_MAX])
    return lines


async def _apparent(
    conn: AsyncConnection,
    story_id: str,
    character_id: str,
    other: str,
    cast: _Cast,
) -> str | None:
    """How old ``other`` looks to ``character_id``: the age it believes, else the band."""
    entity = cast.entity_of.get(other)
    if entity is not None:
        believed = await partitions.partition(
            conn,
            story_id,
            character_id,
            subjects=[entity],
            predicate=AGE,
            levels=partitions.HELD_LEVELS,
            order="confidence",
            limit=1,
        )
        for b in believed:
            literal = b.proposition.literal
            if literal is not None and b.proposition.polarity.value == "affirmed":
                return str(literal.value)
    return age_band(cast.ages.get(other))


async def _scene_line(
    conn: AsyncConnection, state: TurnState, character_id: str, cast: _Cast
) -> str:
    """The ``## Scene`` line with each other present character's apparent age."""
    present = [cast.character_of.get(e) for e in state.present]
    cards: list[str] = []
    others: list[str] = []
    for entity, member in zip(state.present, present, strict=True):
        if member is None:
            others.append(cast.entity_names.get(entity, entity))
            continue
        name = cast.names.get(member, cast.entity_names.get(entity, entity))
        if member == character_id:
            cards.append(name)
            continue
        seen = await _apparent(conn, state.story_id, character_id, member, cast)
        cards.append(name if seen is None else f"{name} ({seen})")
    listed = sorted([*cards, *others])
    if state.location_name is None and not listed:
        return ""
    where = state.location_name or "an unnamed place"
    return f"Location: {where}." + (f" Present: {', '.join(listed)}." if listed else "")


async def build_context(
    conn: AsyncConnection, character_id: str, state: TurnState
) -> CharacterContext:
    """``character_id``'s generation context for this turn (see the module docstring)."""
    from epistrel.core.retrieval.pipeline import RetrievalQuery, retrieve

    started = time.perf_counter()
    story_id = state.story_id
    cast = await _cast(conn, story_id)
    if character_id in cast.players:
        raise InvalidRequest(
            "the player's character has no generation context",
            {"field": "character_id", "reason": "player_has_no_context"},
        )
    settings = state.settings
    partitioned = module_on(settings, PARTITIONS_MODULE)
    level = scenes.level_of(settings)
    viewpoint = f"{CHARACTER_VIEWPOINT_PREFIX}{character_id}"
    window = await records.working_memory(
        conn,
        story_id,
        character_id,
        None,
        settings.render.working_memory_beats,
        level=level,
        scene_loader=state.scene_loader,
        stage=state.stage,
    )
    staged = [b for b in window if b.staged]
    window = [b for b in window if not b.staged]
    window += await _pending(conn, state, character_id, level)
    own = [b.text for b in window if b.character_id == character_id and b.fidelity == "content"]
    joined = " ".join(x for x in (state.say, state.do) if x)
    # #174: what the earlier primaries just did, as perceived, joins the retrieval query.
    heard = [b.text for b in staged if b.fidelity != "envelope"]
    query = " ".join([joined, *heard, *own[-QUERY_BEATS:]])
    retrieval = await retrieve(
        conn,
        story_id,
        RetrievalQuery(text=query[:2_000] or ".", actor=character_id),
        viewpoint=viewpoint,
        embed=state.embed,
        embed_batch=state.embed_batch,
        read_set=state.read_set,
        lexical=state.lexical,
        settings=settings,
    )
    hidden = _hidden_ages(retrieval, frozenset(cast.character_of))
    retrieved: list[str] = []
    canon: list[str] = []
    for item in retrieval.context.items:
        if item.kind == "canon":
            if item.item_id not in hidden:
                canon.append(item.text)
        else:
            retrieved.append(item.text)
    beliefs: list[str] = []
    nested_lines: list[str] = []
    speech_lines: list[str] = []
    needed: list[Needed] = []
    own_age = ""
    private_memories: list[str] = []
    scene = state.scene_text
    if partitioned:
        registry = await load(conn, story_id)
        predicates = {pid: p.name for pid, p in registry.predicates.items()}
        names = {**cast.entity_names, **cast.names}
        about = list(dict.fromkeys((*retrieval.resolution.entities, *retrieval.participants)))
        needed = await _needed(conn, state, character_id, about, registry)
        pending_beliefs, pending_heard = _needed_lines(needed, predicates, names, cast.names)
        beliefs = pending_beliefs + await _belief_lines(
            conn,
            story_id,
            character_id,
            frozenset(b.id for b in retrieval.beliefs),
            predicates,
            names,
        )
        nested_lines, speech_lines = await _nested_lines(
            conn, story_id, character_id, predicates, names, cast.names
        )
        speech_lines = pending_heard + speech_lines
        age = cast.ages.get(character_id)
        own_age = "" if age is None else f"You are {age}."
        ranked = [i.item_id for i in retrieval.context.items if i.kind != "canon"]
        private_memories = await _private_lines(conn, story_id, character_id, ranked)
        scene = await _scene_line(conn, state, character_id, cast)
    report = {
        "context_ms": round((time.perf_counter() - started) * 1000, 3),
        "perceived": sum(1 for b in window if b.fidelity != "envelope"),
        "envelope": sum(1 for b in window if b.fidelity == "envelope"),
        **({"just_now": len(staged)} if staged else {}),
        "dropped": len(retrieval.context.dropped),
        "beliefs": min(len(beliefs), BELIEFS_MAX)
        + min(len(nested_lines), NESTED_MAX)
        + min(len(speech_lines), NESTED_MAX),
    }
    return compose(
        character_id,
        working=_seal(window, viewpoint),
        names=cast.names,
        just_now=_seal(staged, viewpoint),
        retrieved=_seal(retrieved, viewpoint),
        canon=_seal(canon, viewpoint),
        beliefs=beliefs,
        nested_lines=nested_lines,
        speech_lines=speech_lines,
        scene=scene,
        own_age=own_age,
        private_memories=private_memories,
        needed=needed,
        retrieval=retrieval,
        report=report,
    )


__all__ = [
    "BELIEFS_MAX",
    "NESTED_MAX",
    "PRIVATE_MAX",
    "QUERY_BEATS",
    "Adjudicator",
    "CharacterContext",
    "Needed",
    "PendingBeat",
    "TurnState",
    "ViewpointFiltered",
    "age_band",
    "belief_line",
    "build_context",
    "compose",
    "memory_line",
    "statement",
]
