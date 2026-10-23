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
"""

from __future__ import annotations

import ast
from importlib import resources
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import exposure, partitions, testimony, transitions
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.verify.rules import Holder, VerdictKind, check_with
from tests import canon_support as c
from tests import exposure_support as xs
from tests import partition_support as parts
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
async def test_exposure_frame__postgres(engine: AsyncEngine, testimony_delivered: None) -> None:
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
async def test_exposure_frame__fold_forward(engine: AsyncEngine, testimony_delivered: None) -> None:
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
