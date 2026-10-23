"""A structured turn's beats in Core: the beat and utterance models, the labels, latent routing
and the characters a render introduces (FR-TURN-1,4,5,6, FR-CAT-12; docs/02 §4.4; #150).

The Orchestrator's structured pipeline (``orchestrator.structured.pipeline``) renders, detects
and verifies; everything here is deterministic and runs on what it hands over.

**Beats** (``TurnBeat``). A render's beats are ``say``, ``think``, ``do`` and ``latent`` for a
character render, ``narration`` and ``latent`` for the narrator's; the player's input stays one
``player_input`` event at beat 1 (FR-RESP-9). ``stage_beats`` stages each as a ``beat_<kind>``
event (``core.events.types.BEAT_TYPES``) through the turn's ``Operation``, which indexes it for
erasure and runs the resurrection guard (#127): a character's beat with ``provenance = engine``
and ``authority = authoritative``, a narrator's with ``provenance = system``; the event's
``character_id`` is the actor (``None`` for narration) and its ``beat`` the beat's position.
Its payload carries the text, the propositions it asserts with their verdicts, the claims
detection found beyond them and its resolved mentions (#139, #148), for a beat the repair
ladder corrected, ``repaired_from {rung, class}`` (#151), and the beat's perception qualifiers
(``perception``, #161), so a history read re-routes it as it was routed.

**Speech acts** (FR-TURN-5,6). Every ``say`` (and the player's own ``say``) is stored as an
utterance (``write_utterances``, the ``utterances`` table) and is **never** canon by being said:
only what #144's permission check lets a speaker introduce about itself is canonized, and that
is the verifier's decision, not the utterance's. Its three labels are independent:
``objective_relation`` (``objective_relation_of``: all of its claims supported → ``true``, any
contradicted → ``false``, otherwise → ``unresolved``: undecided, novel, none at all or
verification off), ``belief_relation`` (``unknown`` until M5's partitions) and ``intent`` as the
render declared it on the ``say`` (``unspecified`` when nothing was declared; the player's input
is always ``unspecified``).

**Latent routing** (FR-TURN-4). A ``latent`` beat's propositions go to canon with its declared
visibility (``visibility_of``: ``objective`` by default, ``public``, or ``characters`` naming
who may know it) through #144's ``canonize`` (which writes #133's ``commit_fact``), whatever the
verification level (it is declared structure), unless the verifier found one contradicted. A
``latent`` that declared nothing normalizable is kept as an ``objective`` episodic memory
(``latent_unnormalized``) instead. A ``latent`` beat is never in a character viewpoint's read
(``core.viewpoint.PRIVATE_CHANNELS``).

**Introductions** (FR-CAT-12). A render that introduces a new named character creates its
``characters`` row in the turn's commit (``write_introductions``): its name, ``definition`` =
the introducing beat's text, the next ``ordinal``, the one true age (``true_age_source``
``declared`` when the render gave it, ``repaired`` when the turn's age-repair call did), its
entity (#139) and a canon ``age`` fact at the story-time of the introducing turn
(``story_time_at``), visible as objective canon, so a later claim about the age is checked
against it. A name the cast already has is a mention of that character, never a new row. The
character joins the cast from the next turn. An introduced character is modeled at creation
(#163, a salience trigger): ``weight = minor`` and ``modeled_at`` = the introducing beat.
``write_character_facts`` writes a new character's ``age`` (and, for #163's modeling, its
``located_at`` in the scene): ``core.epistemic.modeling`` writes no canon itself.

Log lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.canon.facts import OBJECTIVE, Fact, FactOperation, Visibility, commit_fact
from epistrel.core.enums import Authority, CharacterWeight, Provenance, RecordProvenance
from epistrel.core.epistemic.perception import Qualifiers
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records, reindex_story
from epistrel.core.erasure.records import character_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.events.types import (
    BEAT_TYPES,
    BeatLabels,
    BeatProposition,
    BeatVisibility,
    CharacterDefined,
    DetectedRef,
)
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, insert_derived
from epistrel.core.propositions.model import (
    Interval,
    LiteralKind,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, LOCATED_AT
from epistrel.core.resolution.context import load_time_basis
from epistrel.core.resolution.entities import add_entities, character_spec
from epistrel.core.resolution.time import story_time_at
from epistrel.core.tables import characters, events
from epistrel.core.tables.characters import TRUE_AGE_MAX, TRUE_AGE_MIN
from epistrel.core.tables.utterances import utterances

log = logging.getLogger(__name__)

BeatKind = Literal["say", "think", "do", "latent", "narration"]
Intent = Literal["sincere", "deceptive", "joking", "unspecified"]
ObjectiveRelation = Literal["true", "false", "unresolved"]
AgeSource = Literal["declared", "repaired"]

CHARACTER_KINDS: frozenset[str] = frozenset({"say", "think", "do", "latent"})
"""The beats a character render may return."""
NARRATOR_KINDS: frozenset[str] = frozenset({"narration", "latent"})
"""The beats the narrator's render may return."""
OBSERVABLE_KINDS: frozenset[str] = frozenset({"say", "do", "narration"})
"""The beats a character perceives (and an episodic memory is written for)."""
BEAT_TEXT_MAX = 2_000
"""The longest beat text a render may return."""
UNSPECIFIED: Intent = "unspecified"
UNKNOWN_BELIEF = "unknown"
LATENT_UNNORMALIZED = "latent_unnormalized"
"""Degradation: a ``latent`` beat declared nothing that normalized; it is kept as an episodic
memory with ``objective`` visibility."""
AGE = CORE_BY_NAME["age"].id

SUPPORTED = "supported"
CONTRADICTED = "contradicted"


# -- the models ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssertedClaim:
    """One proposition a beat asserts (a declared one, or one detection found) and the verdict
    it ended with (``None``: verification off), with its claim id (``<operation>:<n>``)."""

    proposition: Proposition | None
    verdict: str | None = None
    claim_id: str | None = None
    declared: bool = True
    span: tuple[int, int] | None = None
    category: str = "declared"
    proposition_id: str | None = None

    def payload(self) -> BeatProposition:
        assert self.proposition is not None  # noqa: S101 - only normalized claims are listed
        return BeatProposition(
            proposition=self.proposition.model_dump(mode="json"),
            verdict=self.verdict,
            claim_id=self.claim_id,
        )


@dataclass(frozen=True, slots=True)
class TurnBeat:
    """One beat of a structured turn: its kind, position, text and actor (``None`` for
    narration), a ``say``'s declared intent, a ``latent``'s visibility, the claims it asserts,
    its resolved mentions and the occasion #141's retrieval reads (``event_type``,
    ``participants``)."""

    kind: BeatKind
    beat: int
    text: str
    character_id: str | None
    intent: Intent = UNSPECIFIED
    visibility: Visibility = OBJECTIVE
    visibility_declared: str = "objective"
    claims: tuple[AssertedClaim, ...] = ()
    mentions: tuple[dict[str, Any], ...] = ()
    event_type: str | None = None
    participants: tuple[str, ...] = ()
    narrator: bool = False
    repaired_from: tuple[int, str] | None = None
    """``(rung, class)`` when the repair ladder corrected the beat (#151)."""
    qualifiers: Qualifiers = field(default_factory=Qualifiers)
    """The beat's explicit perception qualifiers (#161; persisted on its event)."""

    @property
    def declared(self) -> tuple[AssertedClaim, ...]:
        return tuple(c for c in self.claims if c.declared and c.proposition is not None)

    @property
    def detected(self) -> tuple[AssertedClaim, ...]:
        return tuple(c for c in self.claims if not c.declared)

    def labels(self) -> BeatLabels | None:
        if self.kind != "say":
            return None
        return BeatLabels(
            objective_relation=objective_relation_of(self.claims),
            belief_relation=UNKNOWN_BELIEF,
            intent=self.intent,
        )


def objective_relation_of(claims: Iterable[AssertedClaim]) -> ObjectiveRelation:
    """An utterance's objective relation (FR-TURN-6): over its claims' verdicts, any
    ``contradicted`` → ``false``; all ``supported`` (at least one) → ``true``; otherwise →
    ``unresolved`` (undecided, novel, denied, none at all, or verification off)."""
    verdicts = [c.verdict for c in claims]
    if CONTRADICTED in verdicts:
        return "false"
    if verdicts and all(v == SUPPORTED for v in verdicts):
        return "true"
    return "unresolved"


def visibility_of(scope: str, characters_: Sequence[str] = ()) -> Visibility:
    """A ``latent`` beat's declared scope as canon visibility (#133): ``objective`` (the
    default), ``public``, or ``characters`` with the characters named."""
    if scope == "characters":
        return Visibility(scope="characters", characters=tuple(characters_))
    if scope == "public":
        return Visibility(scope="public")
    return OBJECTIVE


def event_type_of(kind: str) -> str:
    return f"beat_{kind}"


# -- staging -------------------------------------------------------------------------------------


def beat_payload(beat: TurnBeat) -> dict[str, Any]:
    """A beat's event payload (``core.events.types.StructuredBeat`` and its kind's fields)."""
    payload: dict[str, Any] = {
        "text": beat.text,
        "propositions": [c.payload() for c in beat.declared],
    }
    detected = [
        DetectedRef(start=c.span[0], end=c.span[1], category=c.category, verdict=c.verdict)
        for c in beat.detected
        if c.span is not None
    ]
    if detected:
        payload["detected"] = detected
    if beat.mentions:
        payload["mentions"] = list(beat.mentions)
    if beat.repaired_from is not None:
        rung, repair_class = beat.repaired_from
        payload["repaired_from"] = {"rung": rung, "class": repair_class}
    if not beat.qualifiers.empty:
        payload["perception"] = beat.qualifiers.payload()
    if beat.kind == "say":
        payload["intent"] = beat.intent
        payload["labels"] = beat.labels()
    if beat.kind == "latent":
        characters_ = (
            beat.visibility.characters or () if beat.visibility.scope == "characters" else ()
        )
        payload["visibility"] = BeatVisibility(
            scope=beat.visibility_declared, characters=tuple(characters_)
        )
    return payload


def stage_beats(op: Operation, beats: Sequence[TurnBeat]) -> None:
    """Stage each beat as its ``beat_<kind>`` event on the turn's operation (see the module
    docstring)."""
    for beat in beats:
        model = BEAT_TYPES[beat.kind]
        op.append(
            event_type_of(beat.kind),
            model.model_validate(beat_payload(beat)),
            provenance=Provenance.SYSTEM if beat.narrator else Provenance.ENGINE,
            authority=Authority.AUTHORITATIVE,
            character_id=None if beat.kind == "narration" else beat.character_id,
            beat=beat.beat,
        )


async def beat_event_ids(conn: AsyncConnection, operation_id: str) -> dict[int, str]:
    """The ids of the turn's beat events by beat number (call in a commit step, after the
    events are written): the ``player_input`` and every ``beat_*`` event."""
    found = await conn.execute(
        select(events.c.id, events.c.beat, events.c.type).where(
            events.c.operation_id == operation_id,
            events.c.beat.is_not(None),
            events.c.type.in_(["player_input", *(event_type_of(k) for k in BEAT_TYPES)]),
        )
    )
    return {int(r.beat): str(r.id) for r in found}


# -- utterances ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Utterance:
    """One ``say`` as a speech act: its beat, speaker, asserted propositions and labels."""

    beat: int
    speaker_character_id: str
    proposition_ids: tuple[str, ...]
    objective_relation: ObjectiveRelation
    intent: Intent = UNSPECIFIED
    belief_relation: str = UNKNOWN_BELIEF


def utterance_of(beat: TurnBeat, proposition_ids: Sequence[str]) -> Utterance:
    """The utterance a ``say`` beat stores."""
    if beat.kind != "say" or beat.character_id is None:
        raise InvalidRequest("only a character's say is an utterance", {"field": "beat"})
    return Utterance(
        beat=beat.beat,
        speaker_character_id=beat.character_id,
        proposition_ids=tuple(proposition_ids),
        objective_relation=objective_relation_of(beat.claims),
        intent=beat.intent,
    )


async def write_utterances(
    conn: AsyncConnection,
    operation: FactOperation,
    rows: Sequence[Utterance],
    *,
    event_ids: Mapping[int, str],
) -> list[str]:
    """Write ``rows`` in ``operation``'s commit transaction (``core.membership.insert_derived``:
    they follow the turn's operation); answers their ids. Ids and labels only, no text."""
    if not rows:
        return []
    story_id = operation.story_id
    if story_id is None:
        raise InvalidRequest("an utterance belongs to a story", {"field": "operation"})
    values: list[dict[str, Any]] = []
    for row in rows:
        event_id = event_ids.get(row.beat)
        if event_id is None:
            raise InvalidRequest(
                "an utterance names one of the turn's beat events", {"field": "beat"}
            )
        values.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "operation_id": operation.operation_id,
                "source_event_ids": [event_id],
                "narrative_turn": operation.narrative_turn,
                "turn_variant": operation.turn_variant,
                "event_id": event_id,
                "speaker_character_id": row.speaker_character_id,
                "proposition_ids": list(row.proposition_ids),
                "objective_relation": row.objective_relation,
                "belief_relation": row.belief_relation,
                "intent": row.intent,
            }
        )
    await insert_derived(conn, utterances, values)
    log.debug("utterances written: %d", len(values))
    return [str(v["id"]) for v in values]


# -- introductions -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Introduction:
    """A character a render introduces: its name, its one true age and where that came from,
    the introducing beat (its text is the definition) and the id the turn minted for it."""

    name: str
    age: int
    source: AgeSource
    beat: int
    definition: str
    character_id: str = field(default_factory=new_ulid)

    def __post_init__(self) -> None:
        if not TRUE_AGE_MIN <= self.age <= TRUE_AGE_MAX:
            raise InvalidRequest(
                f"a true age is {TRUE_AGE_MIN} to {TRUE_AGE_MAX}", {"field": "age"}
            )


def stage_introductions(op: Operation, intros: Sequence[Introduction]) -> None:
    """Stage the ``character_defined`` control event of each introduction (never its age or
    definition, which live only in ``characters``: FR-CAT-12)."""
    for intro in intros:
        op.append(
            "character_defined",
            CharacterDefined(character_id=intro.character_id, name=intro.name, is_player=False),
            provenance=Provenance.SYSTEM,
        )


async def taken_names(conn: AsyncConnection, story_id: str) -> dict[str, str]:
    """The story's character names, case-folded, to their ids."""
    found = await conn.execute(
        select(characters.c.id, characters.c.name).where(characters.c.story_id == story_id)
    )
    return {str(r.name).casefold(): str(r.id) for r in found}


async def write_introductions(
    conn: AsyncConnection,
    operation: FactOperation,
    intros: Sequence[Introduction],
    *,
    base_revision: int | None = None,
) -> list[Fact]:
    """Create each introduced character in ``operation``'s commit transaction (see the module
    docstring): its row, its entity, and its canon ``age`` fact; answers the age facts. A name
    the story already has (another turn may have added it since the render) is skipped."""
    story_id = operation.story_id
    if story_id is None or not intros:
        return []
    taken = await taken_names(conn, story_id)
    fresh = [i for i in intros if i.name.casefold() not in taken]
    if not fresh:
        return []
    await guard.check_content(
        conn,
        story_id,
        [guard.Content((i.name, i.definition), frozenset()) for i in fresh],
        base_revision=base_revision,
        path=__name__,
    )
    last = await conn.scalar(
        select(func.max(characters.c.ordinal)).where(characters.c.story_id == story_id)
    )
    ordinal = int(last if last is not None else -1) + 1
    rows: list[dict[str, Any]] = []
    for offset, intro in enumerate(fresh):
        rows.append(
            {
                "id": intro.character_id,
                "story_id": story_id,
                "ordinal": ordinal + offset,
                "name": intro.name,
                "definition": intro.definition,
                "true_age": intro.age,
                "true_age_source": intro.source,
                "age_recorded_at": func.transaction_timestamp(),
                "is_player": False,
                # #163: an introduced character is modeled at creation, a minor.
                "weight": CharacterWeight.MINOR.value,
                **Position(
                    operation.narrative_turn or 0, operation.turn_variant or 0, 0, intro.beat, 0
                ).columns("modeled"),
            }
        )
    await conn.execute(insert(characters).values(rows))
    await add_entities(
        conn,
        story_id,
        [character_spec(i.character_id, i.name) for i in fresh],
        operation_id=operation.operation_id,
    )
    await index_records(
        conn,
        [
            character_record(
                SimpleNamespace(
                    id=r["id"],
                    story_id=story_id,
                    name=r["name"],
                    definition=r["definition"],
                    safe_fallback_text=None,
                    deflection_style=None,
                    persona_snapshot=None,
                )
            )
            for r in rows
        ],
    )
    # The new names may already be in the story's records (FR-ADMIN-6; #123).
    await reindex_story(conn, story_id)
    written = await write_character_facts(
        conn, operation, [(i.character_id, i.age, i.beat) for i in fresh]
    )
    log.debug("introductions written: %d", len(fresh))
    return written


async def write_character_facts(
    conn: AsyncConnection,
    operation: FactOperation,
    joined: Sequence[tuple[str, int | None, int]],
    *,
    location: str | None = None,
) -> list[Fact]:
    """The canon a new character needs, in ``operation``'s commit transaction: for each
    ``(character_id, age, beat)`` its ``age`` fact at the story-time of the turn (objective, so a
    later claim about the age is checked against it; none for a ``None`` age, a character whose
    age is already canon) and, with a ``location``, its
    ``located_at`` there (#163: a modeled character joins the scene it was modeled in). Answers
    the facts written. Canon's writer for ``core.epistemic.modeling``, which never writes canon
    itself (#160's gate)."""
    story_id = operation.story_id
    if story_id is None or not joined:
        return []
    basis = await load_time_basis(conn, story_id)
    since = story_time_at(operation.narrative_turn or 0, basis)
    written: list[Fact] = []
    for character_id, age, beat in joined:
        if age is not None:
            written.append(
                await commit_fact(
                    conn,
                    story_id,
                    Proposition(
                        subject_id=character_id,
                        predicate_id=AGE,
                        literal=TypedLiteral(kind=LiteralKind.INT, value=age),
                        valid_time=Interval.of(since, None),
                    ),
                    provenance=RecordProvenance.RENDER,
                    operation=operation,
                    beat=beat,
                )
            )
        if location is not None:
            written.append(
                await commit_fact(
                    conn,
                    story_id,
                    Proposition(
                        subject_id=character_id,
                        predicate_id=LOCATED_AT,
                        object_id=location,
                        valid_time=Interval.of(since, None),
                    ),
                    provenance=RecordProvenance.RENDER,
                    operation=operation,
                    beat=beat,
                )
            )
    return written


__all__ = [
    "AGE",
    "BEAT_TEXT_MAX",
    "CHARACTER_KINDS",
    "LATENT_UNNORMALIZED",
    "NARRATOR_KINDS",
    "OBSERVABLE_KINDS",
    "UNSPECIFIED",
    "AgeSource",
    "AssertedClaim",
    "BeatKind",
    "Intent",
    "Introduction",
    "ObjectiveRelation",
    "TurnBeat",
    "Utterance",
    "beat_event_ids",
    "beat_payload",
    "event_type_of",
    "objective_relation_of",
    "stage_beats",
    "stage_introductions",
    "taken_names",
    "utterance_of",
    "visibility_of",
    "write_character_facts",
    "write_introductions",
    "write_utterances",
]
