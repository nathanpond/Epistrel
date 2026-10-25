"""EXPOSURE-FRAME: no backward pollution (docs/04 §6; FR-BELIEF-5,6,7,8, FR-STORE-31; #164).

In memory (300 examples): a frame E (a turn position ``(turn, beat, step)`` or an inter-turn slot
``(turn, slot_k)``), a generated history of records about the exposed claim (canon facts and the
witness's beliefs, each at a position and recorded by a turn's operation: a lazy resolution's
recording turn is later than its position) and later *dated* transitions. The resolved decision
(#162's ``decide`` over the verdict the frame's records give) is a function only of the records
``core.epistemic.exposure.frame_of`` admits, which are exactly those an independent statement of
FR-BELIEF-6 admits: at or before E in (turn, slot, beat, step) order and in the record by E's turn
(or made earlier in the same pass). Adding any record after E (a later turn, a later beat or step
of E's own turn, or a resolution recorded after E) changes nothing. Complement: a later dated
transition that contradicts the resolved belief *does* change the folded-forward result (the fold
closes it at that transition's position), proving the fold runs.

On Postgres: an exposure at (turn 2, beat 2, step 1), canon at a later beat of the same turn and
in a later turn that contradicts the claim, resolved in turn 4: the witness believes the claim as
of turn 2 (novel, from a trusted speaker), and every keyed read the resolver made is bounded at
or before E (the read set is the proof). Complement: read now, without the frame, the same claim
is contradicted for the witness. With a later dated observation of the contradiction in the
witness's own partition, the resolved belief is closed by a correction dated at that observation.
The resolver never imports semantic retrieval.

Trust in the frame (#542): a ``friend_of`` committed at a later beat of E's own turn leaves Ann's
trust in Bobby neutral (the claim only ``suspected``), committed before E it seeds it high
(``believed``); each trust read is a key of the resolver's read set through E; with
``lazy_resolution`` off (resolved in the writing turn) a friendship at an earlier beat of that turn
counts and one at a later beat does not. And the property again, through the real resolver on
Postgres (``PG_EXAMPLES``): a contradicting canon fact and a friendship placed before E, at an
earlier or later beat or step of E's turn, in a later turn, or nowhere; the resolution is what
only the records at or before E decide, and every keyed read is bounded at E.
"""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from importlib import resources
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon_facts
from epistrel.core.db import make_engine
from epistrel.core.enums import (
    BeliefConfidenceLevel,
    OperationKind,
    Provenance,
    RecordProvenance,
)
from epistrel.core.epistemic import exposure, partitions, testimony, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position, beat_steps
from epistrel.core.propositions import registry as registry_
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.registry import load
from epistrel.core.propositions.vocabulary import FRIEND_OF
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.verify.rules import Holder, VerdictKind, check_with
from tests import canon_support as c
from tests import exposure_support as xs
from tests import partition_support as parts
from tests.auth_support import auth_settings
from tests.memory_support import use_structured

CLAIM = Proposition(
    subject_id="01JSVBJECT0000000000000000",
    predicate_id="01JPREDZCATE00000000000000",
    object_id="01J0BJECT00000000000000000",
    valid_time=Interval.of(None, None),
)
NEGATION = CLAIM.negation()


def key(p: Position) -> tuple[int, int, int, int]:
    return (p.turn, p.slot_k, p.beat, p.step)


turn_positions = st.builds(
    lambda turn, beat, step: Position(turn, 0, 0, beat, step),
    turn=st.integers(1, 6),
    beat=st.integers(0, 4),
    step=st.integers(0, 3),
)
slot_positions = st.builds(
    lambda turn, slot: Position(turn, 0, slot, 0, 0),
    turn=st.integers(1, 6),
    slot=st.integers(1, 2),
)
positions = st.one_of(turn_positions, slot_positions)


@st.composite
def framed(draw: st.DrawFn) -> exposure.Framed:
    position = draw(positions)
    late = draw(st.integers(0, 3))
    return exposure.Framed(
        position=position,
        recorded_turn=position.turn + late,
        payload=draw(st.sampled_from([CLAIM, NEGATION])),
        included=draw(st.booleans()) and late > 0,
    )


def independent(records: list[exposure.Framed], frame: Position) -> list[exposure.Framed]:
    """FR-BELIEF-6 stated directly: through E's full position, as the record stood by E's
    turn (a resolution made later by another pass is not yet in it)."""
    return [
        r
        for r in records
        if key(r.position) <= key(frame) and (r.recorded_turn <= frame.turn or r.included)
    ]


def verdict(records: list[exposure.Framed]) -> VerdictKind:
    """The frame's verdict on the claim: a negation contradicts it, an affirmation supports it."""
    said = {r.payload.polarity for r in records}
    if Polarity.NEGATED in said:
        return VerdictKind.CONTRADICTED
    if Polarity.AFFIRMED in said:
        return VerdictKind.SUPPORTED
    return VerdictKind.NOVEL


def resolved(records: list[exposure.Framed], frame: Position, trust: float) -> Any:
    seen = exposure.frame_of(records, frame)
    return testimony.decide("w", testimony.Consistency(CLAIM, verdict(seen)), trust, "sincere")


def contradicts(resolved_claim: Proposition, other: Proposition) -> bool:
    return other.normalized_key() == resolved_claim.normalized_key() and (
        other.polarity is not resolved_claim.polarity
    )


@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
@settings(max_examples=300, deadline=None)
@given(
    frame=positions,
    history=st.lists(framed(), max_size=12),
    after=st.lists(framed(), max_size=6),
    dated=st.lists(st.tuples(positions, st.sampled_from([CLAIM, NEGATION])), max_size=4),
    trust=st.floats(0, 1),
)
def test_exposure_frame(
    frame: Position,
    history: list[exposure.Framed],
    after: list[exposure.Framed],
    dated: list[tuple[Position, Proposition]],
    trust: float,
) -> None:
    # The frame reads exactly what FR-BELIEF-6 admits.
    assert exposure.frame_of(history, frame) == independent(history, frame)
    # Nothing after E changes the resolution: a later turn or slot, a later beat or step of
    # E's own turn, or a record put in the record after E's turn by another pass.
    polluted = [
        *history,
        *(
            r
            for r in after
            if key(frame) < key(r.position) or (r.recorded_turn > frame.turn and not r.included)
        ),
        exposure.Framed(
            Position(frame.turn, 0, frame.slot_k, frame.beat + 1, 0), frame.turn, NEGATION
        )
        if frame.slot_k == 0
        else exposure.Framed(Position(frame.turn, 0, frame.slot_k + 1, 0, 0), frame.turn, NEGATION),
        exposure.Framed(frame, frame.turn + 1, NEGATION),
    ]
    assert exposure.frame_of(polluted, frame) == exposure.frame_of(history, frame)
    base = resolved(history, frame, trust)
    assert resolved(polluted, frame, trust) == base
    # The fold-forward: the first later dated contradiction of an accepted belief closes it.
    if base.transition is not None and base.decision == testimony.ACCEPT:
        later = [(p, x) for p, x in dated if exposure.in_frame(p, frame) is False]
        found = exposure.first_correction(CLAIM, frame, later, contradicts)
        expected = sorted(
            ((p, x) for p, x in later if x.polarity is Polarity.NEGATED), key=lambda i: key(i[0])
        )
        assert found == (expected[0] if expected else None)
        # Complement: a contradiction dated after E does change the folded result.
        after_e = Position(frame.turn + 1, 0, 0, 1, 1)
        closer = exposure.first_correction(CLAIM, frame, [(after_e, NEGATION)], contradicts)
        assert closer == (after_e, NEGATION)


def test_exposure_frame__no_retrieval() -> None:
    """The resolver's read set is its proof: it may not call semantic retrieval."""
    source = resources.files("epistrel.core.epistemic").joinpath("exposure.py").read_text()
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    assert not [m for m in imported if m.startswith("epistrel.core.retrieval")], imported


# -- Postgres -------------------------------------------------------------------------------------


async def _world(engine: AsyncEngine) -> tuple[c.World, Proposition]:
    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    claim = c.at(w.people["Cal"], w.places["garden"], None, None)
    return w, claim


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
async def test_exposure_frame__postgres(engine: AsyncEngine) -> None:
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    await xs.plain_turn(engine, story)
    # Turn 2: Bobby says Cal is in the garden (beat 2) and Ann is exposed; beat 3 then commits
    # the truth (he is not) into canon. Turn 3 confirms it again.
    exposure_id, _ = await xs.expose(
        engine, story, ann, claim, source=bobby, then=[claim.negation()]
    )
    await c.commit(engine, story, claim.negation())
    (row,) = await xs.exposure_rows(engine, story)
    assert (row.position_turn, row.position_beat, row.position_step) == (2, xs.SAY_BEAT, 1)
    assert row.state == "unresolved"
    assert await parts.belief_rows(engine, story) == []  # an exposure is not a belief

    # Complement: read now, the claim is contradicted for Ann (the canon she may read).
    async with engine.connect() as conn:
        now = await check_with(
            KeyedLookup(conn, StructuredReadSet()), story, claim, holder=Holder.belief(ann)
        )
    assert now.kind is VerdictKind.CONTRADICTED

    resolution, resolving_op = await xs.resolve(engine, story, exposure_id)
    frame = Position(2, 0, 0, xs.SAY_BEAT, 1)
    assert resolution.outcome.verdict == VerdictKind.NOVEL.value
    assert resolution.outcome.decision == testimony.ACCEPT
    assert resolution.reads, "the resolver read through its lookups"
    for read in resolution.reads:
        assert read.through is not None, read
        assert read.through <= frame.key(), read
    belief = resolution.belief
    assert belief is not None
    assert belief.held_from.key() == frame.key()
    assert belief.resolution_operation_id == resolving_op
    assert belief.operation_id == resolving_op
    assert await xs.state_of(engine, story, exposure_id) == "resolved"


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
async def test_exposure_frame__fold_forward(engine: AsyncEngine) -> None:
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    exposure_id, _ = await xs.expose(engine, story, ann, claim, source=bobby)
    # A later dated transition in Ann's own partition (turn 2): she sees Cal is not in the
    # garden.
    seen = await parts.transit(
        engine,
        story,
        ann,
        claim.negation(),
        cause=transitions.Kind.OBSERVATION,
        confidence=transitions.Confidence(BeliefConfidenceLevel.KNOWN),
    )
    assert seen.belief.held_from.turn == 2
    resolution, _ = await xs.resolve(engine, story, exposure_id)
    assert resolution.outcome.decision == testimony.ACCEPT  # in its frame, she had no reason
    assert resolution.corrected_by == seen.belief.id
    async with engine.connect() as conn:
        history = await transitions.transitions_of(conn, story, holder=ann)
    kinds = [(t.kind, t.position.turn) for t in history]
    assert (transitions.Kind.CORRECTION, seen.transition.position.turn) in kinds
    assert resolution.belief is not None
    assert resolution.belief.held_to == seen.belief.held_from
    assert resolution.belief.state.value == "corrected"
    # The observation's own belief stands, known.
    async with engine.connect() as conn:
        held = await partitions.read_belief(conn, story, seen.belief.id)
    assert held is not None
    assert held.confidence_level is BeliefConfidenceLevel.KNOWN


# -- trust in the frame (#542) ----------------------------------------------------------------

FRAME = Position(2, 0, 0, xs.SAY_BEAT, 1)
"""Where ``xs.expose`` exposes the witness in the story's second turn."""


def friends(w: c.World) -> Proposition:
    """Ann and Bobby are friends (``friend_of``, symmetric: seeds trust ``TRUST_HIGH``)."""
    return c.at(w.people["Ann"], w.people["Bobby"], None, None, predicate=FRIEND_OF)


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
@pytest.mark.parametrize("when", ["later_beat", "earlier_turn"])
async def test_exposure_frame__trust(engine: AsyncEngine, when: str) -> None:
    """Trust in the frame is bounded at E's full position: a relationship committed at a later
    beat of E's own turn is outside it exactly like a later turn (the novel claim from Bobby is
    only ``suspected``), and the same relationship committed before E seeds Ann's trust
    (``believed``). Every trust read is a key of the resolver's read set, bounded at E."""
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    if when == "earlier_turn":
        await c.commit(engine, story, friends(w))
        exposure_id, _ = await xs.expose(engine, story, ann, claim, source=bobby)
    else:
        await xs.plain_turn(engine, story)
        exposure_id, _ = await xs.expose(engine, story, ann, claim, source=bobby, then=[friends(w)])
    async with engine.connect() as conn:
        now = await testimony.trust(conn, story, ann, bobby)
        framed_trust = await testimony.trust(conn, story, ann, bobby, through=FRAME)
    assert now == testimony.TRUST_HIGH  # the friendship is canon now either way
    resolution, _ = await xs.resolve(engine, story, exposure_id)
    assert resolution.outcome.decision == testimony.ACCEPT
    if when == "earlier_turn":
        assert framed_trust == testimony.TRUST_HIGH
        assert resolution.outcome.confidence is BeliefConfidenceLevel.BELIEVED
    else:
        # Beat 3 of E's own turn is after E: Ann did not yet count Bobby a friend.
        assert framed_trust == testimony.TRUST_NEUTRAL
        assert resolution.outcome.confidence is BeliefConfidenceLevel.SUSPECTED
    trust_keys = [r for r in resolution.reads if r.predicate_id == FRIEND_OF]
    assert trust_keys, "the frame's trust read is in its read set"
    for read in trust_keys:
        assert read.through == FRAME.key(), read


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
@pytest.mark.parametrize(
    ("beat_no", "trust", "level"),
    [
        (3, testimony.TRUST_NEUTRAL, BeliefConfidenceLevel.SUSPECTED),
        (1, testimony.TRUST_HIGH, BeliefConfidenceLevel.BELIEVED),
    ],
)
async def test_exposure_frame__trust_lazy_off(
    engine: AsyncEngine, beat_no: int, trust: float, level: BeliefConfidenceLevel
) -> None:
    """``lazy_resolution`` off resolves the exposure in its own turn, while that turn is not yet
    the story's head and after its later beats are in the record: a friendship committed at
    beat 3 is outside E's frame, one committed at beat 1 of the same turn is inside it."""
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    await xs.plain_turn(engine, story)
    eager: list[exposure.Resolution] = []
    await _expose_with(
        engine, story, ann, claim, bobby, [(friends(w), (2, beat_no, 0))], eager=eager
    )
    (resolution,) = eager
    assert resolution.outcome.decision == testimony.ACCEPT
    assert resolution.outcome.trust == trust
    assert resolution.outcome.confidence is level
    for read in resolution.reads:
        assert read.through is not None, read
        assert read.through <= FRAME.key(), read


# -- the real resolver over generated histories (#542) -----------------------------------------

PG_EXAMPLES = 30

PLACES: dict[str, tuple[int, int, int] | None] = {
    "none": None,
    "earlier_turn": (1, 1, 0),
    "beat_1": (2, 1, 0),
    "say_beat_step_0": (2, xs.SAY_BEAT, 0),
    "say_beat_step_2": (2, xs.SAY_BEAT, 2),
    "beat_3": (2, 3, 0),
    "beat_4_step_1": (2, 4, 1),
    "later_turn": (3, 1, 0),
}
"""Where a generated record is committed, as ``(turn, beat, step)`` (``None``: not at all);
E is ``(2, SAY_BEAT, 1)``. A record in E's own turn at a later beat or a later step of the say
beat is outside the frame exactly like a record of a later turn."""

Placed = list[tuple[Proposition, tuple[int, int, int]]]


def in_e(place: tuple[int, int, int] | None) -> bool:
    """FR-BELIEF-6 stated directly: at or before E in (turn, beat, step) order."""
    return place is not None and place <= (FRAME.turn, FRAME.beat, FRAME.step)


async def _commit_each(conn: AsyncConnection, story: str, op: Operation, records: Placed) -> None:
    """Commit each record at its beat, the beat declared at its step."""
    for proposition, (_, beat_no, step_no) in records:
        with beat_steps(op.operation_id, {beat_no: step_no}):
            await canon_facts.commit_fact(
                conn,
                story,
                proposition,
                provenance=RecordProvenance.DIRECTOR,
                operation=op,
                beat=beat_no,
            )


async def _commit_at(engine: AsyncEngine, story: str, records: Placed) -> None:
    """A turn committing each of ``records`` at its beat and step."""

    async def write(conn: AsyncConnection, op: Operation) -> None:
        await _commit_each(conn, story, op, records)

    await c.run(engine, story, write)


async def _expose_with(
    engine: AsyncEngine,
    story: str,
    witness: str,
    claim: Proposition,
    source: str,
    records: Placed,
    *,
    eager: list[exposure.Resolution] | None = None,
) -> str:
    """Turn 2: ``source`` says ``claim`` at the say beat, ``witness`` is exposed at E, and each
    of ``records`` is committed at its own beat and step of the same turn. ``eager``: resolve
    the exposure at once in the same turn, as ``lazy_resolution`` off does (``resolve_written``),
    and collect the resolution into it."""
    found: dict[str, Any] = {}
    profile = await parts.partitioned(engine, story) if eager is not None else None
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=c.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER, beat=1)
        op.append(
            "beat_say",
            {"text": "It was said.", "intent": "sincere"},
            character_id=source,
            beat=xs.SAY_BEAT,
        )
        op.append("message_rendered", {"text": "It was said."})

        async def step(conn: AsyncConnection) -> None:
            event_id = await conn.scalar(
                text("SELECT id FROM events WHERE operation_id = :o AND type = 'beat_say'"),
                {"o": op.operation_id},
            )
            stored = await store.write_proposition(conn, story, claim, await load(conn, story))
            (found["id"],) = await exposure.record_exposures(
                conn,
                op,
                [
                    exposure.Exposed(
                        witness=witness,
                        proposition_id=stored,
                        proposition=claim,
                        source=source,
                        source_event_id=str(event_id),
                        position=exposure.exposure_position(
                            op.narrative_turn or 0, op.turn_variant or 0, xs.SAY_BEAT
                        ),
                    )
                ],
            )
            await _commit_each(conn, story, op, records)
            if eager is not None:
                eager.extend(
                    await exposure.resolve_written(
                        conn, op, [found["id"]], budget=exposure.Pass(), settings=profile
                    )
                )

        op.add_commit_step(step)
        await op.commit()
    return str(found["id"])


@dataclass
class Tally:
    examples: int = 0
    excluded_same_turn: int = 0
    contradicted: int = 0
    trusted: int = 0


async def _resolver_example(url: str, contradiction: str, friendship: str, tally: Tally) -> None:
    engine = make_engine(auth_settings(url))
    try:
        w, claim = await _world(engine)
        story = w.story_id
        ann, bobby = w.characters["Ann"], w.characters["Bobby"]
        placed = [(claim.negation(), PLACES[contradiction]), (friends(w), PLACES[friendship])]
        by_turn: dict[int, Placed] = {1: [], 2: [], 3: []}
        for proposition, place in placed:
            if place is not None:
                by_turn[place[0]].append((proposition, place))
        await _commit_at(engine, story, by_turn[1])
        exposure_id = await _expose_with(engine, story, ann, claim, bobby, by_turn[2])
        await _commit_at(engine, story, by_turn[3])
        resolution, _ = await xs.resolve(engine, story, exposure_id)

        # The resolution is a function only of the records at or before E.
        if in_e(PLACES[contradiction]):
            assert resolution.outcome.decision == testimony.REJECT
            tally.contradicted += 1
        else:
            assert resolution.outcome.decision == testimony.ACCEPT
            trusted = in_e(PLACES[friendship])
            expected = (
                BeliefConfidenceLevel.BELIEVED if trusted else BeliefConfidenceLevel.SUSPECTED
            )
            assert resolution.outcome.confidence is expected
            tally.trusted += trusted
        tally.excluded_same_turn += any(
            p is not None and p[0] == FRAME.turn and not in_e(p) for _, p in placed
        )
        # The read set is the proof: every keyed read is bounded at or before E.
        assert resolution.reads
        for read in resolution.reads:
            assert read.through is not None, read
            assert read.through <= FRAME.key(), read
        tally.examples += 1
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
def test_exposure_frame__resolver(pg_url: str) -> None:
    """EXPOSURE-FRAME through the real resolver (``resolve_exposure`` on Postgres): a
    contradicting canon fact and a trust-seeding relationship, each placed before E, in E's own
    turn at an earlier or later beat or step, in a later turn, or nowhere; the resolved decision
    and level are those only the records at or before E give, and every keyed read the resolver
    made is bounded at E."""
    tally = Tally()
    names = st.sampled_from(sorted(PLACES))

    @settings(
        max_examples=PG_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(contradiction=names, friendship=names)
    def frame(contradiction: str, friendship: str) -> None:
        asyncio.run(_resolver_example(pg_url, contradiction, friendship, tally))

    frame()
    # Complement: the runs exercised what the property is about.
    assert tally.examples == PG_EXAMPLES
    assert tally.excluded_same_turn > 0
    assert tally.contradicted > 0
    assert tally.trusted > 0


# -- a relationship to a set that holds the speaker (#542) -----------------------------------

SWORN_TRUST = 0.9


async def _sworn(engine: AsyncEngine, w: c.World) -> Proposition:
    """Ann is sworn to Bobby and Cal (an author relationship whose object is a set, with a
    ``trust`` qualifier of ``SWORN_TRUST``)."""
    async with engine.begin() as conn:
        reg = await registry_.register_author(
            conn,
            w.story_id,
            registry_.ScenarioRules.parse(
                {
                    "predicates": [
                        {
                            "name": "sworn_to",
                            "subject_type": "person",
                            "object_type": "person",
                            "set_object": True,
                            "qualifiers": {"trust": "float"},
                            "projection": "relationships",
                            "docs": "Bound by oath to a band of people.",
                        }
                    ]
                }
            ),
        )
    sworn = reg.named("sworn_to")
    assert sworn is not None
    return Proposition(
        subject_id=w.people["Ann"],
        predicate_id=sworn.id,
        object_set=(w.people["Bobby"], w.people["Cal"]),
        qualifiers={"trust": SWORN_TRUST},
        valid_time=Interval.of(None, None),
    )


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8; FR-STORE-31")
@pytest.mark.parametrize(("beat_no", "trust"), [(1, SWORN_TRUST), (3, testimony.TRUST_NEUTRAL)])
async def test_exposure_frame__trust_set_object(
    engine: AsyncEngine, beat_no: int, trust: float
) -> None:
    """A relationship whose object is a set holding the speaker seeds trust in the frame, as
    the projection reads it (a row per member): Ann sworn to {Bobby, Cal} at beat 1 of E's
    turn trusts Bobby at its ``trust`` qualifier (the claim ``believed``); the same oath at
    beat 3, after E, is outside the frame (neutral, ``suspected``). Its read is keyed through E
    on the speaker, so a set object holding him touches it."""
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    oath = await _sworn(engine, w)
    await xs.plain_turn(engine, story)
    exposure_id = await _expose_with(engine, story, ann, claim, bobby, [(oath, (2, beat_no, 0))])
    async with engine.connect() as conn:
        assert await testimony.trust(conn, story, ann, bobby) == SWORN_TRUST  # canon now
        assert await testimony.trust(conn, story, ann, bobby, through=FRAME) == trust
    resolution, _ = await xs.resolve(engine, story, exposure_id)
    assert resolution.outcome.trust == trust
    level = BeliefConfidenceLevel.BELIEVED if beat_no == 1 else BeliefConfidenceLevel.SUSPECTED
    assert resolution.outcome.confidence is level
    keys = {
        (r.subjects, r.object_id, r.through)
        for r in resolution.reads
        if r.predicate_id == oath.predicate_id
    }
    assert keys == {((ann,), bobby, FRAME.key()), ((bobby,), ann, FRAME.key())}
