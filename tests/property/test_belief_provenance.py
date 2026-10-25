"""BELIEF-PROVENANCE: no belief without a recorded, typed, preceding cause (docs/04 §6;
FR-EPI-7,13,14, FR-BELIEF-2; #160).

In memory (300 examples, sequences of up to 20 proposed transitions): each proposal names a
kind, an antecedent (a committed record, a staged beat or a render step reached through a fake
oracle, a reconciled resolution, or a position-less author act) and the dependent step. The
guard ``transition()`` applies (the kind-by-antecedent matrix ``KIND_ANTECEDENTS`` and the order
rule ``check_order``) accepts exactly what an independent statement of FR-EPI-13,14 admits:
every accepted transition is of a permitted kind with a permitted antecedent that strictly
precedes its step in (turn, slot, beat, step) order and is not in another variant of the same
turn, or is a resolution (FR-EPI-14 (d)). Complement: an antecedent at the same or a later step
is refused as ``SelfAttestation``.

On Postgres: FR-EPI-14's example passes and its reverse fails — Bobby says the bridge collapsed
(beat 1, a verified staged beat), Jenni believes it by testimony at beat 2; the same testimony
citing beat 2 from beat 1, or from beat 2's own verified step, is refused and writes nothing.
The provenance trigger refuses a belief with no producing transition, or one whose transition
produces another belief. The guard's refusals, each asserted by its reason and writing nothing:
``not_committed``, ``not_the_holders_own``, ``other_operation``, ``inactive`` (a truncated
belief and author act), a committed record after the dependent step (``SelfAttestation``), and
``not_reconciled`` (a resolution citing an open thunk or another operation's).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline
from epistrel.core.commit import lifecycle
from epistrel.core.enums import AntecedentKind, FactCommitState, ThunkState
from epistrel.core.epistemic.errors import AntecedentInvalid, SelfAttestation
from epistrel.core.epistemic.transitions import (
    DEFAULT_ORACLE,
    KIND_ANTECEDENTS,
    Antecedent,
    Kind,
    StagedRecord,
    check_order,
)
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from tests import canon_support as c
from tests import commit_support as cs
from tests import partition_support as ps
from tests.memory_support import use_structured

PERMITTED = {
    Kind.TESTIMONY: {AntecedentKind.RECORD, AntecedentKind.STAGED_BEAT},
    Kind.OBSERVATION: {
        AntecedentKind.RECORD,
        AntecedentKind.STAGED_BEAT,
        AntecedentKind.RENDER_STEP,
    },
    # #168: a chain of reasoning: a believe step citing an earlier verified believe step.
    Kind.INFERENCE: {AntecedentKind.RECORD, AntecedentKind.RENDER_STEP},
    Kind.DISCOVERY: {AntecedentKind.RECORD, AntecedentKind.STAGED_BEAT},
    Kind.CORRECTION: {
        AntecedentKind.RECORD,
        AntecedentKind.STAGED_BEAT,
        AntecedentKind.RENDER_STEP,
    },
    Kind.REVISION: {AntecedentKind.RECORD, AntecedentKind.STAGED_BEAT, AntecedentKind.RENDER_STEP},
    Kind.FORGETTING: {AntecedentKind.RECORD},
    Kind.AUTHOR: {AntecedentKind.RECORD},
    Kind.RESOLUTION: {AntecedentKind.RESOLUTION},
}
"""FR-BELIEF-2's acquisition operations and FR-EPI-14's antecedents, stated on their own."""


@dataclass(frozen=True)
class Proposal:
    kind: Kind
    antecedent: AntecedentKind
    cited: Position | None
    step: Position


def admits(p: Proposal) -> bool:
    """FR-EPI-13,14 stated independently: a permitted kind and antecedent, preceding strictly
    unless position-less or a resolution, never another variant of the same turn."""
    if p.antecedent not in PERMITTED[p.kind]:
        return False
    if p.antecedent is AntecedentKind.RESOLUTION or p.cited is None:
        return True
    a, d = p.cited, p.step
    if (a.turn, a.slot_k) == (d.turn, d.slot_k) and a.variant != d.variant:
        return False
    return (a.turn, a.slot_k, a.beat, a.step) < (d.turn, d.slot_k, d.beat, d.step)


class FakeOracle:
    """Staged beats and render steps at the positions the proposal cites."""

    def __init__(self, at: Position) -> None:
        self.at = at

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return StagedRecord(self.at)

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return StagedRecord(self.at)


def guard(p: Proposal) -> str:
    """The checks ``transition()`` runs before writing, without a database: ``accepted``,
    ``kind_mismatch`` or ``self_attestation`` / ``other_variant``."""
    if p.antecedent not in KIND_ANTECEDENTS[p.kind]:
        return "kind_mismatch"
    cited = p.cited
    if p.antecedent in (AntecedentKind.STAGED_BEAT, AntecedentKind.RENDER_STEP):
        assert cited is not None
        oracle = FakeOracle(cited)
        found = (
            oracle.staged_beat({})
            if p.antecedent is AntecedentKind.STAGED_BEAT
            else oracle.render_step({})
        )
        assert found is not None
        cited = found.position
    if p.antecedent is AntecedentKind.RESOLUTION:
        return "accepted"
    try:
        check_order(Antecedent(kind=p.antecedent, ref={}), cited, p.step)
    except SelfAttestation:
        return "self_attestation"
    except AntecedentInvalid:
        return "other_variant"
    return "accepted"


positions = st.builds(
    Position,
    turn=st.integers(0, 4),
    variant=st.integers(1, 2),
    slot_k=st.integers(0, 1),
    beat=st.integers(0, 4),
    step=st.integers(0, 3),
)


@st.composite
def proposals(draw: st.DrawFn) -> Proposal:
    kind = draw(st.sampled_from(list(Kind)))
    antecedent = draw(st.sampled_from(list(AntecedentKind)))
    staged = antecedent in (AntecedentKind.STAGED_BEAT, AntecedentKind.RENDER_STEP)
    cited = draw(positions if staged else st.one_of(st.none(), positions))
    return Proposal(kind, antecedent, cited, draw(positions))


@pytest.mark.part("M5", "FR-EPI-7,13,14; FR-BELIEF-2")
@settings(max_examples=300, deadline=None)
@given(st.lists(proposals(), min_size=1, max_size=20))
def test_belief_provenance(sequence: list[Proposal]) -> None:
    accepted = []
    for p in sequence:
        verdict = guard(p)
        assert (verdict == "accepted") == admits(p), (p, verdict)
        if verdict == "accepted":
            accepted.append(p)
        elif p.antecedent in PERMITTED[p.kind] and p.cited is not None:
            # Complement: a permitted antecedent refused for its order is at or after the step
            # (self-attestation), or in another variant of the turn.
            a, d = p.cited, p.step
            assert verdict in ("self_attestation", "other_variant")
            if verdict == "self_attestation":
                assert (a.turn, a.slot_k, a.beat, a.step) >= (d.turn, d.slot_k, d.beat, d.step)
    # Every accepted transition has a typed cause that precedes it (or is order-exempt).
    for p in accepted:
        assert p.kind in Kind
        assert p.antecedent in PERMITTED[p.kind]
        assert (
            p.cited is None or p.antecedent is AntecedentKind.RESOLUTION or p.cited.before(p.step)
        )


# -- Postgres -------------------------------------------------------------------------------------


class VerifiedBeats:
    """A sequential turn's verified staged beats (#174's turn stage stands in): beat ``b``
    of the operation is verified at ``(turn, variant, 0, b, step)``."""

    def __init__(self, op: Operation, verified: set[int]) -> None:
        self.op = op
        self.verified = verified

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        beat = int(ref["beat"])
        if beat not in self.verified or self.op.narrative_turn is None:
            return None
        return StagedRecord(Position(self.op.narrative_turn, self.op.turn_variant or 0, 0, beat, 1))

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return None


@pytest.mark.integration
@pytest.mark.part("M5", "FR-EPI-7,13,14; FR-BELIEF-2")
async def test_belief_provenance__postgres(engine: AsyncEngine) -> None:
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    bridge = c.at(w.people["Bobby"], w.places["garden"], 0, None, negated=True)
    stage: dict[str, VerifiedBeats] = {}

    def beat(n: int) -> Any:
        return lambda op: Antecedent.staged_beat(op.operation_id, op.turn_variant or 0, n)

    def at_beat(n: int, step: int = 2) -> Any:
        def position(op: Operation) -> Position:
            stage["turn"] = VerifiedBeats(op, {1, 2})
            assert op.narrative_turn is not None
            return Position(op.narrative_turn, op.turn_variant or 0, 0, n, step)

        return position

    class Oracle:
        def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
            return stage["turn"].staged_beat(ref)

        def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
            return stage["turn"].render_step(ref)

    # Bobby says it at beat 1 (verified); Jenni (Ann) believes him by testimony at beat 2.
    believed = await ps.transit(
        engine,
        story,
        ann,
        bridge,
        cause=Kind.TESTIMONY,
        antecedent=beat(1),
        position=at_beat(2),
        source=bobby,
        oracle=Oracle(),
    )
    assert (believed.transition.kind, believed.transition.source_character_id) == (
        Kind.TESTIMONY,
        bobby,
    )
    assert believed.belief.provenance == believed.transition.id
    before = len(await ps.belief_rows(engine, story))

    # The reverse (beat 2 cited from beat 1) and the same beat are self-attestation.
    for cited, dependent, step in ((2, 1, 2), (2, 2, 1)):
        with pytest.raises(SelfAttestation):
            await ps.transit(
                engine,
                story,
                bobby,
                bridge,
                cause=Kind.TESTIMONY,
                antecedent=beat(cited),
                position=at_beat(dependent, step),
                source=ann,
                oracle=Oracle(),
            )
    # An unverified beat, and the default oracle, are no antecedent at all.
    for oracle in (Oracle(), DEFAULT_ORACLE):
        with pytest.raises(AntecedentInvalid, match="unverified"):
            await ps.transit(
                engine,
                story,
                bobby,
                bridge,
                cause=Kind.TESTIMONY,
                antecedent=beat(3),
                position=at_beat(4),
                source=ann,
                oracle=oracle,
            )
    assert len(await ps.belief_rows(engine, story)) == before

    # The provenance trigger: no producing transition, or one producing another belief.
    held = believed.belief
    with pytest.raises(DBAPIError, match="no producing transition"):
        await ps.execute(
            engine,
            _COPY_BELIEF,
            id="01JT00000000000000000000X3",
            provenance="01JT00000000000000000000X4",
            src=held.id,
        )
    with pytest.raises(DBAPIError, match="does not produce"):
        await _steal(engine, held.provenance, held.id)
    assert len(await ps.belief_rows(engine, story)) == before


async def _steal(engine: AsyncEngine, provenance: str, src: str) -> None:
    """A copy of a transition producing another belief, then a belief citing it (one
    transaction: the deferred keys never get to check)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(_COPY_TRANSITION),
            {
                "tid": "01JT00000000000000000000X6",
                "belief": "01JT00000000000000000000X7",
                "t": provenance,
            },
        )
        await conn.execute(
            text(_COPY_BELIEF),
            {
                "id": "01JT00000000000000000000X8",
                "provenance": "01JT00000000000000000000X6",
                "src": src,
            },
        )


_COPY_BELIEF = (
    "INSERT INTO beliefs (id, story_id, holder_character_id, proposition_id, polarity, "
    "confidence_level, valid_span, valid_from, held_from_turn, tx_from, visibility, "
    "operation_id, membership_tx, provenance, lineage_id, kind, speaker_character_id, depth) "
    "SELECT :id, story_id, holder_character_id, proposition_id, polarity, confidence_level, "
    "valid_span, valid_from, held_from_turn + 1, tx_from, visibility, operation_id, "
    "membership_tx, :provenance, :id, 'speech_act', holder_character_id, 2 "
    "FROM beliefs WHERE id = :src"
)
_COPY_TRANSITION = (
    "INSERT INTO belief_transitions (id, story_id, holder_character_id, belief_id, kind, "
    "antecedent_kind, antecedent_ref, confidence_level, position_turn, tx_from, operation_id, "
    "membership_tx) SELECT :tid, story_id, holder_character_id, :belief, kind, antecedent_kind, "
    "antecedent_ref, confidence_level, position_turn, tx_from, operation_id, membership_tx "
    "FROM belief_transitions WHERE id = :t"
)


INTRODUCED = FactCommitState.INTRODUCED
RESOLVED = ThunkState.RESOLVED


def _refused(info: pytest.ExceptionInfo[AntecedentInvalid], reason: str) -> None:
    assert info.value.details["reason"] == reason


@pytest.mark.integration
@pytest.mark.part("M5", "FR-EPI-7,13,14; FR-BELIEF-2")
async def test_belief_provenance__refusals(engine: AsyncEngine) -> None:
    """The antecedent guard's refusals on Postgres (#160 AC2), each writing nothing: a fact
    that is only introduced is ``not_committed``; another holder's belief as an inference
    premise is ``not_the_holders_own``; a staged beat of another operation is
    ``other_operation``; a committed fact positioned after the dependent step is
    ``SelfAttestation`` (a record is no exception to the order rule); a belief, and an author act,
    whose turn was truncated away are ``inactive``."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    garden = c.at(w.people["Bobby"], w.places["garden"], 0, None)
    school = c.at(w.people["Cal"], w.places["school"], 0, None)

    introduced = await c.commit(engine, story, garden, commit_state=INTRODUCED)
    committed = await c.commit(engine, story, school)
    bobbys = await ps.believe(engine, story, bobby, garden)
    before = len(await ps.belief_rows(engine, story))

    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine,
            story,
            ann,
            garden,
            cause=Kind.DISCOVERY,
            antecedent=Antecedent.record("facts", introduced.id),
        )
    _refused(info, "not_committed")
    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine,
            story,
            ann,
            garden,
            cause=Kind.INFERENCE,
            antecedent=Antecedent.record("beliefs", bobbys.id),
        )
    _refused(info, "not_the_holders_own")
    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine,
            story,
            ann,
            garden,
            cause=Kind.TESTIMONY,
            antecedent=Antecedent.staged_beat(bobbys.operation_id, 0, 1),
            position=lambda op: Position(op.narrative_turn or 0, op.turn_variant or 0, 0, 2, 1),
            source=bobby,
            oracle=VerifiedBeatsAnyOperation(),
        )
    _refused(info, "other_operation")
    # A committed record at turn N cited from a step before it: self-attestation.
    assert committed.narrative_turn is not None
    with pytest.raises(SelfAttestation) as attested:
        await ps.transit(
            engine,
            story,
            ann,
            school,
            cause=Kind.DISCOVERY,
            antecedent=Antecedent.record("facts", committed.id),
            position=Position(committed.narrative_turn - 1),
        )
    assert attested.value.details["antecedent"]["ref"]["id"] == committed.id
    assert len(await ps.belief_rows(engine, story)) == before

    # Ann's belief (and the author act that caused it) in a turn later truncated away.
    held = await ps.believe(engine, story, ann, school)
    (act,) = await ps.rows_of(
        engine,
        "SELECT id FROM events WHERE operation_id = :o AND type = 'belief_authored'",
        o=held.operation_id,
    )
    await timeline.truncate(
        engine,
        story,
        timeline.TruncateRequest(to_turn=held.held_from.turn - 1),
        principal=c.PRINCIPAL,
    )
    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine,
            story,
            ann,
            school,
            cause=Kind.INFERENCE,
            antecedent=Antecedent.record("beliefs", held.id),
        )
    _refused(info, "inactive")
    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine, story, ann, school, antecedent=Antecedent.record("events", str(act.id))
        )
    _refused(info, "inactive")
    assert len(await ps.belief_rows(engine, story)) == before + 1  # only the truncated one


class VerifiedBeatsAnyOperation:
    """An oracle that would verify any beat of any operation: the ``other_operation`` refusal
    precedes it."""

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        return StagedRecord(Position(1, 0, 0, int(ref["beat"]), 1))

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        return StagedRecord(Position(1, 0, 0, int(ref["beat"]), int(ref["step"])))


@pytest.mark.integration
@pytest.mark.part("M5", "FR-EPI-7,13,14; FR-BELIEF-2")
async def test_belief_provenance__resolution_refusals(engine: AsyncEngine) -> None:
    """A ``resolution`` transition needs a thunk this operation resolved (a passed
    reconciliation, FR-EPI-14 (d)): citing a thunk still open, or one another operation
    resolved, is ``not_reconciled``, and nothing is written."""
    k = await cs.knock_story(engine)
    knocked = cs.knocker(k, k.mailman, 0)

    async def open_knock(conn: AsyncConnection, op: Operation) -> Any:
        return await lifecycle.introduce(conn, op, cs.knock_sketch(k), valid_time=0, beat=2)

    thunk = await cs.in_turn(engine, k.story_id, open_knock)
    assert thunk is not None
    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine,
            k.story_id,
            k.bobby,
            knocked,
            cause=Kind.RESOLUTION,
            antecedent=Antecedent.resolution(thunk.id),
        )
    _refused(info, "not_reconciled")

    async def resolve(conn: AsyncConnection, op: Operation) -> Any:
        return await lifecycle.depend(conn, op, thunk.id, knocked, beat=3, channel="do")

    dependence = await cs.in_turn(engine, k.story_id, resolve)
    assert dependence.kind == "committed"
    assert (await cs.thunk(engine, k.story_id, thunk.id)).state == RESOLVED
    before = len(await ps.belief_rows(engine, k.story_id))
    with pytest.raises(AntecedentInvalid) as info:
        await ps.transit(
            engine,
            k.story_id,
            k.bobby,
            knocked,
            cause=Kind.RESOLUTION,
            antecedent=Antecedent.resolution(thunk.id),
        )
    _refused(info, "not_reconciled")
    assert len(await ps.belief_rows(engine, k.story_id)) == before
