"""Exposure records and lazy resolution: a bystander decides from what it knew then
(FR-BELIEF-5,6,7,8,12,13,14, FR-STORE-31; docs/02 §4.4; #164).

**Writing** (``record_exposures``, a commit step). A witness of a ``say`` that #162's salience
leaves to the exposure path (neither salient nor addressed) is exposed, not resolved: one
``exposures`` row per (witness, beat, proposition) it caught, with the source, the beat's event
and the **exposure position** E (the beat's step 1, the witness rule #162's transitions sit at:
``(turn, beat, 1)``; an inter-turn slot is ``(turn, slot_k)`` with beat 0). A ``partial``
exposure holds only the claims inside the eavesdropper's ``partial_text``. An envelope is no
exposure (``core.epistemic.observation`` records it as an eager ``spoke_to``/``spoke``
observation), nor is a ``do`` or a narration (eager observations), so exposures exist only for
heard ``say`` content. The player's character has no partition and is never exposed. An
exposure writes **no belief** (FR-BELIEF-1: hearing is not believing). A salient witness is
resolved in the same turn by #162 and still gets its exposures, ``resolved``, for audit.

**Resolving** (``resolve_exposure``, called only from inside a narrative turn that needs the
witness's belief, in its commit: today #162's testimony of that witness, later #166's
verification and #170's context assembly through ``resolve_needed``). The resolution is decided
**in the exposure frame**: the witness's partition and the canon it may read as the corrected
history stood through E (``through_position = E`` on every read: #159's partition and #133's
canon, a later beat or step of the same turn excluded exactly like a later turn; a belief
another pass resolved after E is excluded unless this pass made it, ``membership.including``),
and its trust in the source as of E (``testimony.trust(through=E)``). #162's deterministic
default decides (``testimony.testimony``); the result is written as the ``said`` belief and the
first-order transition the decision calls for, both dated at E (``held_from = E``) and written by
the needing operation (``beliefs.resolution_operation_id``: FR-BELIEF-12's narrative state,
committed with that turn and covered by its revision increment; FR-BELIEF-13's two anchors:
valid-from E, inclusion the needing turn). Then later **dated** transitions are folded forward
in narrative order: a belief the witness came to hold after E that contradicts the resolved one
closes it at its own position, by a ``correction`` transition dated there and citing that
belief's own antecedent (FR-BELIEF-8: belief, then correction, each in its own frame).

**The read set is the proof** (EXPOSURE-FRAME): every keyed read of a resolution is recorded in
its own ``StructuredReadSet`` with its ``through`` bound (``Resolution.reads``), and none is
newer than E. The resolver never calls semantic retrieval (an AST check in the tests).

**State** is read through the resolving operation's membership (``thunks``' pattern, by way of
each resolution's ``belief_resolved`` event, which follows its operation): an exposure whose
resolution's operation left the timeline (a truncation before it, a swap to a continuation that
never resolved it) is unresolved again and re-resolves on a later need, and a swap back to the
chain that resolved it brings that resolution back; a fork before the resolving operation copies
it unresolved. ``exposures_of(as_of_turn=N)`` reads the state as the record stood through turn N.
Read-only inspection never resolves.

**Lazy resolution off** (``character_knowledge.lazy_resolution``): the exposures are still
written and resolved at once, in the writing turn, by the default.

A needing turn resolves only the witness's unresolved exposures whose proposition shares the
claim's functional key (subject and predicate), oldest first, each later frame seeing the earlier
resolutions of the same pass, at most ``MAX_LAZY_RESOLUTIONS_PER_TURN`` a turn (the rest stay
unresolved: ``lazy_resolution_capped``). ``turn_report.epistemic`` counts ``exposures_written``
and ``resolutions = {defaulted, adjudicated}``. Log lines carry identifiers and counts only
(invariant 3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import (
    BeliefKind,
    ExposureFidelity,
    ExposureKind,
    ExposureState,
)
from epistrel.core.epistemic import partitions, testimony, transitions
from epistrel.core.epistemic.partitions import Belief, BeliefOperation
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import exposure_record
from epistrel.core.errors import InvalidRequest
from epistrel.core.events.types import BeliefResolved, ExposureRecorded
from epistrel.core.ids import new_ulid
from epistrel.core.membership import (
    Position,
    active_only,
    active_operation,
    including,
    insert_derived,
    position_key,
)
from epistrel.core.propositions.codec import proposition_from_row
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry, canonical, load
from epistrel.core.readset import KeyedLookup, QueryKey, StructuredReadSet
from epistrel.core.tables import entities, events, operations, propositions
from epistrel.core.tables.exposures import exposures

log = logging.getLogger(__name__)

EXPOSURE_RECORDED = "exposure_recorded"
BELIEF_RESOLVED = "belief_resolved"
LAZY_RESOLUTION_MODULE = "lazy_resolution"
LAZY_RESOLUTION_CAPPED = "lazy_resolution_capped"

MAX_LAZY_RESOLUTIONS_PER_TURN = 20
"""The most exposures one narrative turn resolves (``character_knowledge
.max_lazy_resolutions_per_turn`` once #181 adds the leaf); the rest wait for a later need."""

DEFAULTED = "defaulted"
ADJUDICATED = "adjudicated"


def max_lazy_resolutions(settings: Any) -> int:
    found = getattr(
        getattr(settings, "character_knowledge", None), "max_lazy_resolutions_per_turn", None
    )
    return MAX_LAZY_RESOLUTIONS_PER_TURN if not isinstance(found, int) else max(0, found)


def lazy_on(settings: Any) -> bool:
    """Whether exposures wait for a need (``character_knowledge.lazy_resolution`` delivered and
    on); off, they are resolved in the writing turn."""
    from epistrel.core.profiles.capabilities import module_on

    return settings is not None and module_on(settings, LAZY_RESOLUTION_MODULE)


def exposure_position(turn: int, variant: int, beat: int) -> Position:
    """Where a witness is exposed to a say: the beat's step 1 (#162's witness rule)."""
    return testimony.witness_position(turn, variant, beat)


def slot_position(turn: int, variant: int, slot_k: int) -> Position:
    """An inter-turn exposure position ``N⁺k`` (M6's late settlements write these)."""
    if slot_k < 1:
        raise ValueError("an inter-turn slot is k ≥ 1")
    return Position(turn, variant, slot_k, 0, 0)


# -- the pure frame ------------------------------------------------------------------------------


def in_frame(position: Position, frame: Position) -> bool:
    """Whether a record at ``position`` is in the frame through ``frame`` ((turn, slot, beat,
    step) order: a later beat or step of the same turn is outside, as a later turn is)."""
    return position.at_or_before(frame)


@dataclass(frozen=True, slots=True)
class Framed:
    """A record as the frame sees it: where it sits (``position``) and the turn whose operation
    put it in the record (``recorded_turn``; a lazy resolution's is the needing turn, later than
    its position), plus what it carries."""

    position: Position
    recorded_turn: int
    payload: Any = None
    included: bool = False
    """Made earlier in the same resolution pass (``membership.including``)."""


def frame_of(records: Iterable[Framed], frame: Position) -> list[Framed]:
    """The records the frame through ``frame`` reads: at or before it, and in the record by its
    turn (or made earlier in the same pass). Pure; ``resolve_exposure``'s SQL reads apply the
    same two bounds (``through_position`` and ``membership.included_through``)."""
    return [
        r
        for r in records
        if in_frame(r.position, frame) and (r.recorded_turn <= frame.turn or r.included)
    ]


def first_correction[T](
    resolved: Proposition,
    frame: Position,
    later: Iterable[tuple[Position, T]],
    contradicts: Callable[[Proposition, T], bool],
) -> tuple[Position, T] | None:
    """The first later dated belief (``(held_from, belief)``, held from after ``frame``) that
    contradicts ``resolved`` (``contradicts(resolved, belief)``), in narrative order: the
    fold-forward closes the resolved belief there. Pure."""
    found = sorted(
        ((p, x) for p, x in later if frame.before(p) and contradicts(resolved, x)),
        key=lambda item: item[0].key(),
    )
    return found[0] if found else None


# -- the rows ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Exposure:
    """One exposure (see ``core.tables.exposures``) with its proposition and its **effective**
    state (``resolved`` only while the resolving operation is on the timeline)."""

    id: str
    story_id: str
    witness: str
    proposition_id: str
    proposition: Proposition
    source: str | None
    source_event_id: str
    kind: ExposureKind
    fidelity: ExposureFidelity
    position: Position
    state: ExposureState
    resolved_by_transition: str | None
    resolution_operation_id: str | None
    operation_id: str

    def document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "witness_character_id": self.witness,
            "proposition_id": self.proposition_id,
            "source_character_id": self.source,
            "source_event_id": self.source_event_id,
            "kind": self.kind.value,
            "fidelity": self.fidelity.value,
            "position": self.position.document(),
            "state": self.state.value,
            "resolved_by_transition": self.resolved_by_transition,
            "resolution_operation_id": self.resolution_operation_id,
        }


_P = "p_"


def _stored_resolution(as_of_turn: int | None) -> Any:
    """The resolution the row stores (a salient witness's, made by the writing turn) is on the
    timeline (and in the record by ``as_of_turn``)."""
    o = operations.alias("resolver")
    where = [
        exposures.c.state == ExposureState.RESOLVED.value,
        o.c.operation_id == exposures.c.resolution_operation_id,
        active_operation(o),
    ]
    if as_of_turn is not None:
        where.append(o.c.narrative_turn <= as_of_turn)
    return exists().where(*where)


def _resolution_event(as_of_turn: int | None) -> Any:
    """The active ``belief_resolved`` events naming the exposure: one per resolution made, each
    following its operation's membership, so a swap back to a chain that resolved it brings its
    resolution back while another chain's leaves with its operation."""
    e = events.alias("resolved_event")
    o = operations.alias("resolved_by")
    where = [
        e.c.story_id == exposures.c.story_id,
        e.c.type == BELIEF_RESOLVED,
        e.c.payload["exposure_id"].astext == exposures.c.id,
        o.c.operation_id == e.c.operation_id,
        active_operation(o),
    ]
    if as_of_turn is not None:
        where.append(o.c.narrative_turn <= as_of_turn)
    return e, o, where


def _resolved(as_of_turn: int | None = None) -> Any:
    _, _, where = _resolution_event(as_of_turn)
    return or_(_stored_resolution(as_of_turn), exists().where(*where))


def _select(as_of_turn: int | None = None) -> Any:
    e, _o, where = _resolution_event(as_of_turn)
    latest = (
        select(e.c.payload["transition_id"].astext, e.c.operation_id)
        .where(*where)
        .order_by(e.c.seq.desc())
        .limit(1)
    )
    return select(
        exposures,
        _resolved(as_of_turn).label("resolved_now"),
        latest.with_only_columns(e.c.payload["transition_id"].astext)
        .scalar_subquery()
        .label("resolved_transition"),
        latest.with_only_columns(e.c.operation_id).scalar_subquery().label("resolved_operation"),
        *(c.label(f"{_P}{c.name}") for c in propositions.c),
    ).select_from(exposures.join(propositions, propositions.c.id == exposures.c.proposition_id))


def _exposure_of(row: Any) -> Exposure:
    m = row._mapping
    proposition = proposition_from_row(
        {c.name: m[f"{_P}{c.name}"] for c in propositions.c if c.name not in ("id", "story_id")}
    )
    position = Position.of_columns(m, "position")
    assert position is not None  # noqa: S101 - NOT NULL
    resolved = bool(m["resolved_now"])
    by_event = m["resolved_operation"] is not None
    transition = m["resolved_transition"] if by_event else m["resolved_by_transition"]
    operation = m["resolved_operation"] if by_event else m["resolution_operation_id"]
    return Exposure(
        id=str(m["id"]),
        story_id=str(m["story_id"]),
        witness=str(m["witness_character_id"]),
        proposition_id=str(m["proposition_id"]),
        proposition=proposition,
        source=None if m["source_character_id"] is None else str(m["source_character_id"]),
        source_event_id=str(m["source_event_id"]),
        kind=ExposureKind(m["kind"]),
        fidelity=ExposureFidelity(m["fidelity"]),
        position=position,
        state=ExposureState.RESOLVED if resolved else ExposureState.UNRESOLVED,
        resolved_by_transition=None if not resolved or transition is None else str(transition),
        resolution_operation_id=None if not resolved or operation is None else str(operation),
        operation_id=str(m["operation_id"]),
    )


async def exposures_of(
    conn: AsyncConnection,
    story_id: str,
    witness: str | None = None,
    *,
    state: ExposureState | None = None,
    ids: Sequence[str] | None = None,
    as_of_turn: int | None = None,
) -> list[Exposure]:
    """The story's active exposures (of ``witness``; in effective ``state``), oldest position
    first; ``as_of_turn`` reads the state as the record stood through that turn (a resolution
    made by a later turn is not yet one: FR-BELIEF-14). A raw read: it never resolves anything
    (inspection reads through here)."""
    x = exposures.c
    where: list[Any] = [x.story_id == story_id, active_only(exposures)]
    if witness is not None:
        where.append(x.witness_character_id == witness)
    if ids is not None:
        where.append(x.id.in_(list(ids)))
    if state is ExposureState.RESOLVED:
        where.append(_resolved(as_of_turn))
    elif state is ExposureState.UNRESOLVED:
        where.append(~_resolved(as_of_turn))
    rows = await conn.execute(
        _select(as_of_turn)
        .where(*where)
        .order_by(*position_key(exposures, "position").clauses, x.id)
    )
    return [_exposure_of(r) for r in rows]


@dataclass(frozen=True, slots=True)
class Exposed:
    """One proposition a witness was exposed to, to write: the witness, the stored proposition
    id and the proposition, the source, the beat's event, the fidelity and the position (and,
    for a salient witness #162 resolved at once, the transition that resolved it)."""

    witness: str
    proposition_id: str
    proposition: Proposition
    source: str | None
    source_event_id: str
    position: Position
    fidelity: ExposureFidelity = ExposureFidelity.FULL
    resolved_by_transition: str | None = None


async def _subject_characters(
    conn: AsyncConnection, story_id: str, proposition: Proposition
) -> tuple[str | None, str | None]:
    wanted = [i for i in (proposition.subject_id, proposition.object_id) if i is not None]
    if not wanted:
        return None, None
    found = {
        str(r.id): None if r.character_id is None else str(r.character_id)
        for r in await conn.execute(
            select(entities.c.id, entities.c.character_id).where(
                entities.c.story_id == story_id, entities.c.id.in_(wanted)
            )
        )
    }
    return found.get(proposition.subject_id), (
        None if proposition.object_id is None else found.get(proposition.object_id)
    )


async def record_exposures(
    conn: AsyncConnection, op: BeliefOperation, exposed: Sequence[Exposed]
) -> list[str]:
    """Write ``exposed`` in ``op``'s commit transaction (see the module docstring): the erasure
    guard over the witnesses and sources, the rows (``resolved`` when a transition is named),
    their erasure index rows and one ``exposure_recorded`` event per beat. Answers the ids."""
    story_id = op.story_id
    if story_id is None:
        raise InvalidRequest("an exposure belongs to a story", {"field": "operation"})
    if not exposed:
        return []
    await guard.check_content(
        conn,
        story_id,
        [
            guard.Content((), frozenset(c for c in (e.witness, e.source) if c is not None))
            for e in exposed
        ],
        base_revision=None,
        path=__name__,
    )
    rows: list[dict[str, Any]] = []
    records = []
    by_beat: dict[int, list[str]] = {}
    for item in exposed:
        exposure_id = new_ulid()
        by_beat.setdefault(item.position.beat, []).append(exposure_id)
        resolved = item.resolved_by_transition is not None
        rows.append(
            {
                "id": exposure_id,
                "story_id": story_id,
                "witness_character_id": item.witness,
                "proposition_id": item.proposition_id,
                "source_character_id": item.source,
                "source_event_id": item.source_event_id,
                "kind": ExposureKind.CONTENT.value,
                "fidelity": ExposureFidelity(item.fidelity).value,
                **item.position.columns("position"),
                "state": (ExposureState.RESOLVED if resolved else ExposureState.UNRESOLVED).value,
                "resolved_by_transition": item.resolved_by_transition,
                "resolution_operation_id": op.operation_id if resolved else None,
                "operation_id": op.operation_id,
                "source_event_ids": [item.source_event_id],
                "narrative_turn": op.narrative_turn,
                "turn_variant": op.turn_variant,
            }
        )
        subject, obj = await _subject_characters(conn, story_id, item.proposition)
        records.append(
            exposure_record(
                SimpleNamespace(
                    id=exposure_id,
                    story_id=story_id,
                    witness_character_id=item.witness,
                    source_character_id=item.source,
                    subject_character_id=subject,
                    object_character_id=obj,
                )
            )
        )
    await insert_derived(conn, exposures, rows)
    await index_records(conn, records)
    for beat, ids in sorted(by_beat.items()):
        await partitions._append_event(
            conn,
            story_id,
            op,
            ExposureRecorded(exposure_ids=tuple(ids)),
            beat or None,
            EXPOSURE_RECORDED,
        )
    return [str(r["id"]) for r in rows]


# -- resolving ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """What one resolution did: the exposure, the default's outcome, the transitions written
    (the said belief's, the first-order one's, a fold-forward correction's), the belief it left
    (``None`` for the speech act alone), the correction that closed it, how it was decided and
    the keyed reads it made (the frame proof)."""

    exposure: Exposure
    outcome: testimony.TestimonyOutcome
    transitions: tuple[transitions.TransitionResult, ...]
    belief: Belief | None
    corrected_by: str | None
    resolution: str
    reads: tuple[QueryKey, ...]


async def _intent(conn: AsyncConnection, event_id: str) -> str | None:
    payload = await conn.scalar(select(events.c.payload).where(events.c.id == event_id))
    return payload.get("intent") if isinstance(payload, dict) else None


async def decide(
    conn: AsyncConnection,
    exposure: Exposure,
    *,
    registry: Registry | None = None,
    read_set: StructuredReadSet | None = None,
) -> testimony.TestimonyOutcome:
    """#162's deterministic default for ``exposure``, decided in its frame (the witness's
    partition and canon through the exposure position, its trust in the source as of then).
    It only reads, so ``resolve_exposure`` writes what it decides, and #165's could-have-known
    replay dry-runs an unresolved exposure through it with nothing persisted."""
    story_id = exposure.story_id
    frame = exposure.position
    found = registry or await load(conn, story_id)
    witness = exposure.witness
    lookup = KeyedLookup(conn, read_set if read_set is not None else StructuredReadSet())
    consistency = await testimony.consistency_of(
        lookup, story_id, witness, [exposure.proposition], registry=found, through=frame
    )
    trust = (
        testimony.TRUST_NEUTRAL
        if exposure.source is None
        else await testimony.trust(conn, story_id, witness, exposure.source, through=frame)
    )
    intent = testimony.perceived(await _intent(conn, exposure.source_event_id))
    act = testimony.SpeechAct(
        speaker=exposure.source or witness,
        beat=frame.beat,
        propositions=(exposure.proposition,),
        event_id=exposure.source_event_id,
    )
    (outcome,) = testimony.testimony(witness, act, intent, consistency=consistency, trust=trust)
    return outcome


async def resolve_exposure(
    conn: AsyncConnection,
    exposure: Exposure,
    needing_operation: BeliefOperation,
    *,
    settings: Any = None,
    registry: Registry | None = None,
    adjudicated: tuple[testimony.Decision, float] | None = None,
) -> Resolution:
    """Resolve ``exposure`` inside ``needing_operation``'s commit (see the module docstring):
    the default decided in the frame through the exposure position (``adjudicated``: the
    model's decision and confidence, applied where #162's guards allow, ``apply_adjudication``;
    #170's context assembly adjudicates the rendering character's exposures when the turn's
    pool affords it), the said and first-order beliefs written there by the needing operation,
    later dated contradicting beliefs folded forward as corrections, the exposure marked
    resolved and ``belief_resolved`` appended."""
    op = needing_operation
    story_id = exposure.story_id
    if op.story_id != story_id:
        raise InvalidRequest(
            "an exposure is resolved by an operation of its own story", {"field": "operation"}
        )
    frame = exposure.position
    found = registry or await load(conn, story_id)
    reads = StructuredReadSet()
    witness = exposure.witness
    written: list[transitions.TransitionResult] = []
    with including([op.operation_id]):
        outcome = await decide(conn, exposure, registry=found, read_set=reads)
        how = DEFAULTED
        if adjudicated is not None:
            intent = testimony.perceived(await _intent(conn, exposure.source_event_id))
            taken = testimony.apply_adjudication(outcome, adjudicated[0], adjudicated[1], intent)
            if taken is not None:
                outcome, how = taken, ADJUDICATED
        antecedent = transitions.Antecedent.record("events", exposure.source_event_id)
        common: dict[str, Any] = {
            "holder": witness,
            "antecedent": antecedent,
            "position": frame,
            "held_from": frame,
            "frame": frame,
            "resolution": True,
            "settings": settings,
            "registry": found,
        }
        said = None
        if exposure.source is not None:
            said = await transitions.transition(
                conn,
                op,
                kind=transitions.Kind.TESTIMONY,
                proposition=exposure.proposition,
                confidence=transitions.Confidence(testimony.Level.KNOWN),
                source=exposure.source,
                outcome=outcome.decision,
                belief_kind=BeliefKind.SPEECH_ACT,
                **common,
            )
            written.append(said)
            # #167: the same exposure yields the witness's nested belief that the source
            # believes what it said (a joke: suspected; a detected deception: none).
            await _nested(conn, op, exposure, outcome.decision, frame, settings, found)
        belief: Belief | None = None
        if outcome.transition is not None and outcome.confidence is not None:
            confidence = transitions.Confidence(outcome.confidence)
            try:
                if outcome.transition is transitions.Kind.TESTIMONY:
                    first = await transitions.transition(
                        conn,
                        op,
                        kind=transitions.Kind.TESTIMONY,
                        proposition=exposure.proposition,
                        confidence=confidence,
                        source=exposure.source,
                        outcome=testimony.ACCEPT,
                        **common,
                    )
                else:
                    first = await transitions.transition(
                        conn,
                        op,
                        kind=outcome.transition,
                        proposition=exposure.proposition,
                        confidence=confidence,
                        **common,
                    )
                written.append(first)
                belief = first.belief
            except InvalidRequest as refused:
                # Nothing left to correct in the frame, or the witness already holds it from a
                # later position (a later observation confirmed it first): the said belief stands.
                if (refused.details or {}).get("reason") not in (
                    "nothing_to_correct",
                    "held_already",
                ):
                    raise
                transitions.refused()
    corrected_by = None
    if belief is not None and belief.held_to is None and belief.tx_to is None:
        corrected_by = await _fold_forward(conn, op, belief, frame, found, settings, written)
        refreshed = await partitions.read_belief(conn, story_id, belief.id)
        belief = refreshed or belief
    resolving = written[1] if len(written) > 1 else (written[0] if written else None)
    if resolving is not None:
        await conn.execute(
            update(exposures)
            .where(exposures.c.id == exposure.id)
            .values(
                state=ExposureState.RESOLVED.value,
                resolved_by_transition=resolving.transition.id,
                resolution_operation_id=op.operation_id,
            )
        )
        await partitions._append_event(
            conn,
            story_id,
            op,
            BeliefResolved(
                exposure_id=exposure.id,
                transition_id=resolving.transition.id,
                resolution=how,
            ),
            None,
            BELIEF_RESOLVED,
        )
    log.debug("exposure %s resolved by %s", exposure.id, op.operation_id)
    return Resolution(
        exposure,
        outcome,
        tuple(written),
        belief,
        corrected_by,
        how,
        tuple(reads.query_keys),
    )


async def _nested(
    conn: AsyncConnection,
    op: BeliefOperation,
    exposure: Exposure,
    decision: str,
    frame: Position,
    settings: Any,
    registry: Registry,
) -> None:
    from epistrel.core.epistemic import nested
    from epistrel.core.epistemic.errors import EpistemicError
    from epistrel.core.profiles.effective import effective_configuration

    if exposure.source is None or exposure.source == exposure.witness:
        return
    current = settings
    if current is None:
        current = (await effective_configuration(conn, exposure.story_id, validate=False)).settings
    if not nested.nested_on(current):
        return
    intent = await _intent(conn, exposure.source_event_id)
    level = nested.INTENT_LEVELS.get(testimony.perceived_of(intent, decision))
    if level is None:
        return
    try:
        await nested.write_nested(
            conn,
            op,
            holder=exposure.witness,
            attributed_holder=exposure.source,
            proposition=exposure.proposition,
            cause=nested.Cause(
                transitions.Kind.INFERENCE,
                transitions.Antecedent.record("events", exposure.source_event_id),
            ),
            position=frame,
            confidence=transitions.Confidence(level),
            times=nested.Times(held_from=frame),
            settings=current,
            registry=registry,
            frame=frame,
            resolution=True,
        )
    except EpistemicError, InvalidRequest:
        transitions.refused()


async def _fold_forward(
    conn: AsyncConnection,
    op: BeliefOperation,
    belief: Belief,
    frame: Position,
    registry: Registry,
    settings: Any,
    written: list[transitions.TransitionResult],
) -> str | None:
    """Close ``belief`` at the first later dated belief of its holder that contradicts it (a
    ``correction`` dated at that belief's own position, citing its own antecedent); answers that
    belief's id, or ``None``."""
    story_id = belief.story_id
    stored = canonical(belief.proposition, registry)
    held = await partitions.partition(
        conn,
        story_id,
        belief.holder_character_id,
        subjects=[stored.subject_id],
        predicate=stored.predicate_id,
    )
    first = first_correction(
        stored,
        frame,
        ((b.held_from, b) for b in held if b.id != belief.id),
        lambda resolved, other: transitions.conflicts(resolved, other, registry),
    )
    if first is None:
        return None
    position, closer = first
    produced = [
        t
        for t in await transitions.transitions_of(conn, story_id, belief_ids=[closer.id])
        if t.id == closer.provenance
    ]
    if not produced:
        return None
    cause = produced[0]
    try:
        written.append(
            await transitions.transition(
                conn,
                op,
                holder=belief.holder_character_id,
                kind=transitions.Kind.CORRECTION,
                proposition=closer.proposition,
                antecedent=cause.antecedent,
                position=cause.position,
                held_from=position,
                confidence=transitions.Confidence(
                    closer.confidence_level, closer.confidence_scalar
                ),
                settings=settings,
                registry=registry,
            )
        )
    except InvalidRequest:
        transitions.refused()
        return None
    return closer.id


def _key(proposition: Proposition, registry: Registry) -> tuple[str, str]:
    stored = canonical(proposition, registry)
    return (stored.subject_id, stored.predicate_id)


@dataclass(slots=True)
class Pass:
    """One narrative turn's lazy resolutions: how many it made (by how), and whether the cap
    left any (``capped``)."""

    cap: int = MAX_LAZY_RESOLUTIONS_PER_TURN
    made: list[Resolution] = field(default_factory=list)
    capped: bool = False

    def report(self) -> dict[str, int]:
        found = {DEFAULTED: 0, ADJUDICATED: 0}
        for r in self.made:
            found[r.resolution] = found.get(r.resolution, 0) + 1
        return found


async def resolve_needed(
    conn: AsyncConnection,
    op: BeliefOperation,
    witness: str,
    claims: Sequence[Proposition],
    *,
    budget: Pass,
    settings: Any = None,
    registry: Registry | None = None,
    decisions: Mapping[str, tuple[testimony.Decision, float]] | None = None,
) -> list[Resolution]:
    """Resolve ``witness``'s unresolved exposures whose proposition shares a functional key
    (subject and predicate) with one of ``claims``, oldest first, inside ``op``'s commit (see the
    module docstring), within ``budget``'s cap; ``decisions``: a model's adjudication of some of
    them, by exposure id (#170). Answers the resolutions made."""
    story_id = op.story_id
    if story_id is None or not claims:
        return []
    found = registry or await load(conn, story_id)
    keys = {_key(c, found) for c in claims}
    waiting = [
        e
        for e in await exposures_of(conn, story_id, witness, state=ExposureState.UNRESOLVED)
        if _key(e.proposition, found) in keys
    ]
    made: list[Resolution] = []
    for exposure in waiting:
        if len(budget.made) >= budget.cap:
            budget.capped = True
            break
        resolution = await resolve_exposure(
            conn,
            exposure,
            op,
            settings=settings,
            registry=found,
            adjudicated=(decisions or {}).get(exposure.id),
        )
        made.append(resolution)
        budget.made.append(resolution)
    return made


async def resolve_written(
    conn: AsyncConnection,
    op: BeliefOperation,
    ids: Sequence[str],
    *,
    budget: Pass,
    settings: Any = None,
    registry: Registry | None = None,
) -> list[Resolution]:
    """``lazy_resolution`` off: resolve the exposures just written (``ids``) at once, in the
    writing turn, by the default (never capped: they are the turn's own)."""
    story_id = op.story_id
    if story_id is None or not ids:
        return []
    found = registry or await load(conn, story_id)
    made = []
    for exposure in await exposures_of(conn, story_id, ids=ids):
        resolution = await resolve_exposure(conn, exposure, op, settings=settings, registry=found)
        made.append(resolution)
        budget.made.append(resolution)
    return made


def report(written: int, budget: Pass) -> dict[str, Any]:
    """``turn_report.epistemic``'s exposure counts."""
    return {"exposures_written": written, "resolutions": budget.report()}


__all__ = [
    "ADJUDICATED",
    "BELIEF_RESOLVED",
    "DEFAULTED",
    "EXPOSURE_RECORDED",
    "LAZY_RESOLUTION_CAPPED",
    "MAX_LAZY_RESOLUTIONS_PER_TURN",
    "Exposed",
    "Exposure",
    "Framed",
    "Pass",
    "Resolution",
    "decide",
    "exposure_position",
    "exposures_of",
    "first_correction",
    "frame_of",
    "in_frame",
    "lazy_on",
    "max_lazy_resolutions",
    "record_exposures",
    "report",
    "resolve_exposure",
    "resolve_needed",
    "resolve_written",
    "slot_position",
]
