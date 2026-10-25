"""Testimony's commit writer and trust (``core.epistemic.testimony``, #162) on Postgres: a trusted
speaker's correction of a suspected belief, a supported claim's one-level revision, the speech-act
row (one per speaker and claim, its replay answered in place, never in ``belief_state``), and the
trust nudges a witness's own history makes."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel as Level
from epistrel.core.enums import OperationKind
from epistrel.core.epistemic import partitions, testimony
from epistrel.core.events import Operation
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from tests import canon_support as c
from tests import partition_support as parts
from tests.event_support import rows

pytestmark = pytest.mark.integration


async def _setup(engine: AsyncEngine) -> tuple[c.World, dict[str, str]]:
    w = await c.world(engine)
    async with engine.begin() as conn:
        things = {
            "Olivia": await store.add_entity(conn, w.story_id, "person", "Olivia"),
            "text": await store.add_entity(conn, w.story_id, "item", "the text"),
        }
    things["sent"] = await c.auto_predicate(engine, w.story_id, "sent")
    return w, things


def _sent(things: dict[str, str], polarity: Polarity = Polarity.AFFIRMED) -> Proposition:
    return Proposition(
        subject_id=things["Olivia"],
        predicate_id=things["sent"],
        object_id=things["text"],
        polarity=polarity,
        valid_time=Interval.of(None, None),
    )


async def _hear(
    engine: AsyncEngine, w: c.World, witness: str, speaker: str, claim: Proposition
) -> list[testimony.TestimonyOutcome]:
    """``witness`` hears ``speaker`` claim ``claim`` (in an author operation citing its own
    event); answers the outcomes written."""
    found: dict[str, Any] = {}
    settings = await parts.partitioned(engine, w.story_id)
    async with Operation(
        engine,
        story_id=w.story_id,
        kind=OperationKind.CONTROL,
        principal=c.PRINCIPAL,
        bumps_revision=True,
    ) as op:
        op.append("belief_authored", {"holder_character_id": witness})

        async def step(conn: AsyncConnection) -> None:
            act = await conn.scalar(
                text("SELECT id FROM events WHERE operation_id = :o AND type = 'belief_authored'"),
                {"o": op.operation_id},
            )
            lookup = KeyedLookup(conn, StructuredReadSet())
            said = testimony.SpeechAct(speaker, 0, (claim,), event_id=str(act))
            consistency = await testimony.consistency_of(lookup, w.story_id, witness, [claim])
            trust = await testimony.trust(conn, w.story_id, witness, speaker)
            outcomes = testimony.testimony(
                witness, said, "sincere", consistency=consistency, trust=trust
            )
            await testimony.commit_testimony(conn, op, said, outcomes, settings=settings)
            found["outcomes"] = list(outcomes)
            found["trust"] = trust

        op.add_commit_step(step)
        await op.commit()
    outcomes: list[testimony.TestimonyOutcome] = found["outcomes"]
    return outcomes


async def _trust(engine: AsyncEngine, w: c.World, witness: str, speaker: str) -> float:
    async with engine.connect() as conn:
        return await testimony.trust(conn, w.story_id, witness, speaker)


async def test_a_trusted_friend_corrects_a_suspicion_and_a_claim_raises_a_belief(
    engine: AsyncEngine,
) -> None:
    w, things = await _setup(engine)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    await c.commit(
        engine,
        w.story_id,
        Proposition(
            subject_id=ann,
            predicate_id=FRIEND_OF,
            object_id=bobby,
            valid_time=Interval.of(None, None),
        ),
        kind=OperationKind.CONTROL,
        provenance="author",
    )
    assert await _trust(engine, w, ann, bobby) == testimony.TRUST_HIGH
    prior = await parts.believe(
        engine,
        w.story_id,
        ann,
        _sent(things, Polarity.NEGATED),
        level=Level.SUSPECTED,
        kind=OperationKind.CONTROL,
    )
    (outcome,) = await _hear(engine, w, ann, bobby, _sent(things))
    assert (outcome.decision, outcome.transition) == ("accept", testimony.Kind.CORRECTION)
    closed = await parts.rows_of(
        engine, "SELECT state, corrected_by FROM beliefs WHERE id = :b", b=prior.id
    )
    assert [r.state for r in closed] == ["corrected"]
    async with engine.connect() as conn:
        (held,) = await partitions.partition(conn, w.story_id, ann)
        said = await partitions.speech_acts(conn, w.story_id, ann)
    assert (held.polarity, held.confidence_level) == ("affirmed", Level.BELIEVED)
    assert [(a.speaker_id, a.outcome) for a in said] == [(bobby, "accept")]
    assert said[0].belief.confidence_level is Level.KNOWN

    # The same claim again: supported by her own belief, already believed: the speech act is the
    # same row (in place), and her belief is not raised past believed.
    (again,) = await _hear(engine, w, ann, bobby, _sent(things))
    assert (again.decision, again.transition, again.confidence) == ("accept", None, Level.BELIEVED)
    async with engine.connect() as conn:
        assert len(await partitions.speech_acts(conn, w.story_id, ann)) == 1
    # The speech act never enters belief_state, which keeps its fold.
    assert await parts.state_rows(engine, w.story_id) == await parts.folded_rows(engine, w.story_id)
    states = await rows(
        engine,
        "SELECT b.kind FROM belief_state s JOIN beliefs b ON b.id = s.belief_id "
        "WHERE s.story_id = :s",
        s=w.story_id,
    )
    assert {r.kind for r in states} == {"first_order"}


async def test_a_rejected_claim_lowers_trust_and_a_suspicion_is_raised(engine: AsyncEngine) -> None:
    w, things = await _setup(engine)
    ann, bobby, cal = (w.characters[n] for n in ("Ann", "Bobby", "Cal"))
    await parts.believe(
        engine,
        w.story_id,
        ann,
        _sent(things, Polarity.NEGATED),
        level=Level.KNOWN,
        kind=OperationKind.CONTROL,
    )
    (rejected,) = await _hear(engine, w, ann, bobby, _sent(things))
    assert rejected.decision == "reject"
    assert await _trust(engine, w, ann, bobby) == pytest.approx(testimony.TRUST_NEUTRAL - 0.1)
    # Another witness, another speaker: a suspicion supported is raised one level (revision).
    await parts.believe(
        engine, w.story_id, bobby, _sent(things), level=Level.SUSPECTED, kind=OperationKind.CONTROL
    )
    (raised,) = await _hear(engine, w, bobby, cal, _sent(things))
    assert (raised.transition, raised.confidence) == (testimony.Kind.REVISION, Level.BELIEVED)
    async with engine.connect() as conn:
        (held,) = await partitions.partition(conn, w.story_id, bobby)
    assert held.confidence_level is Level.BELIEVED
