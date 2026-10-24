"""S-POLLUTION: exposure-frame resolution (docs/04 §7; FR-BELIEF-5,6,7,8; #164), and
S-INTRA-TURN-FRAME (FR-BELIEF-5,6, FR-STORE-31).

Played end to end through ``respond`` with the stub adapter under per-character partitions,
perception ``full``, ``testimony = defaulted``, rule-based salience and lazy resolution, one turn
per message scaled down from the spec (msg 10 → turn 1, msg 40 → turn 2, msg 50 → turn 3):

- turn 1: Jenni says "Olivia sent the text" to the parlor. Bobby (``friend_of`` Jenni) is neither
  salient nor addressed: he gets an exposure, and no belief;
- turn 2: Jenni's ``latent`` beat establishes the truth in canon (Olivia did not send it), which
  only its renderer perceives, so Bobby does not;
- turn 3: Jenni tells Bobby (addressed) that Olivia sent the letter too: the turn needs his
  belief, so his turn-1 exposure is resolved first, in its frame. It yields his turn-1 belief (he
  trusted Jenni: ``believed``), held from the exposure position and written by turn 3, and the
  resolver's read set holds no key newer than the frame: turn 2's canon was never read.
  Complement: read now, without the frame, the claim is contradicted for Bobby.

Variant: at turn 2 Jenni shows the room the phone (a ``do``): Bobby observes the truth himself
(an eager observation dated turn 2). The turn-3 resolution still decides in the turn-1 frame
(believed), and the fold-forward closes that belief at turn 2 by a correction dated there: the
history shows belief, then correction, each decided in its own frame.

S-INTRA-TURN-FRAME: in one turn, Marcus witnesses the claim at beat 2; beat 3 (a ``latent``) puts
the truth in canon without his perceiving it.
Two turns later his belief is resolved: the exposure stores (turn, beat 2, step 1) and the
resolution excludes beat 3.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import exposure
from epistrel.core.membership import Position
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.verify.rules import Holder, VerdictKind, check_with
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, render, turn_report

pytestmark = pytest.mark.integration

NAMES = ("Wren", "Jenni", "Bobby", "Marcus")
TRUTH = "Olivia never sent it."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _says(**qualifiers: Any) -> dict[str, Any]:
    return beat(
        "say",
        ts.CLAIM_LINE,
        intent="sincere",
        propositions=[claim("Olivia", "sent", "the text")],
        **qualifiers,
    )


def _tells(whom: str) -> dict[str, Any]:
    """Jenni tells ``whom`` something more about Olivia's sending (the same subject and
    predicate, so the turn needs ``whom``'s belief about it; not the refuted claim itself, which
    canon now contradicts and the verifier would repair away)."""
    return beat(
        "say",
        "Olivia sent the letter too.",
        intent="sincere",
        propositions=[claim("Olivia", "sent", "the letter")],
        addressee=whom,
    )


def _truth() -> dict[str, Any]:
    """The truth into canon, perceived by its renderer alone (a ``latent``)."""
    return beat("latent", TRUTH, propositions=[claim("Olivia", "sent", "the text", negated=True)])


def _shown() -> dict[str, Any]:
    """The truth shown to the room (a ``do``: everyone co-located observes it)."""
    return beat(
        "do",
        "She shows them the phone: the ex sent it.",
        propositions=[claim("Olivia", "sent", "the text", negated=True)],
    )


async def _turn(client: Any, epistrel: Any, story: str, ids: dict[str, str], *beats: Any) -> None:
    stub = epistrel.models.stub
    stub.reset()
    stub.script("generation", [render(*beats)])
    response = await respond(client, story, body(ids["Wren"], "Go on."))
    assert response.status_code == 200, response.text


async def _setup(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], dict[str, str]]:
    story, ids, things = await ts.room(client, engine, *NAMES)
    async with engine.begin() as conn:
        things["the letter"] = await store.add_entity(conn, story, "item", "the letter")
    await ts.enable_testimony(engine, story, "defaulted", salience="rules")
    await ps._author(
        engine,
        story,
        Proposition(
            subject_id=ids["Bobby"],
            predicate_id=FRIEND_OF,
            object_id=ids["Jenni"],
            valid_time=Interval.of(None, None),
        ),
    )
    return story, ids, things


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[exposure.Resolution]:
    made: list[exposure.Resolution] = []
    real = exposure.resolve_exposure

    async def spied(*args: Any, **kwargs: Any) -> exposure.Resolution:
        found = await real(*args, **kwargs)
        made.append(found)
        return found

    monkeypatch.setattr(exposure, "resolve_exposure", spied)
    return made


async def _sending(
    engine: AsyncEngine, story: str, holder: str, things: dict[str, str]
) -> list[Any]:
    """``holder``'s first-order beliefs about Olivia's sending the text."""
    return await rows(
        engine,
        "SELECT b.id, b.polarity, b.confidence_level, b.state, b.held_from_turn, "
        "b.held_from_beat, b.held_from_step, b.held_to_turn, b.resolution_operation_id, "
        "b.operation_id, b.corrected_by FROM beliefs b "
        "JOIN propositions p ON p.id = b.proposition_id "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND b.kind = 'first_order' "
        "AND b.membership = 'active' AND p.predicate_id = :p AND p.object_id = :o "
        "ORDER BY b.held_from_turn, b.id",
        s=story,
        h=holder,
        p=things["sent"],
        o=things["the text"],
    )


async def _exposures(engine: AsyncEngine, story: str, witness: str, sent: str) -> list[Any]:
    """``witness``'s exposures to the sending claim (the say's detected claims are others)."""
    return await rows(
        engine,
        "SELECT x.* FROM exposures x JOIN propositions p ON p.id = x.proposition_id "
        "WHERE x.story_id = :s AND x.witness_character_id = :w AND p.predicate_id = :p "
        "AND p.polarity = 'affirmed' ORDER BY x.id",
        s=story,
        w=witness,
        p=sent,
    )


async def _turn_op(engine: AsyncEngine, story: str, turn: int) -> str:
    (found,) = await rows(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND narrative_turn = :t AND membership = 'active'",
        s=story,
        t=turn,
    )
    return str(found.operation_id)


@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8")
async def test_s_pollution(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = _spy(monkeypatch)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, things = await _setup(client, engine)
        bobby = ids["Bobby"]
        await _turn(client, epistrel, story, ids, _says())
        (exposed,) = await _exposures(engine, story, bobby, things["sent"])
        assert exposed.state == "unresolved"
        assert await _sending(engine, story, bobby, things) == []
        assert (await turn_report(engine, story))["epistemic"]["exposures_written"] >= 1

        # Turn 2: the truth enters canon, unseen by Bobby.
        await _turn(client, epistrel, story, ids, beat("do", "She looks away."), _truth())
        assert await _sending(engine, story, bobby, things) == []
        # Complement: read now, without the frame, the claim is contradicted for Bobby.
        async with engine.connect() as conn:
            (heard,) = await exposure.exposures_of(conn, story, ids=[str(exposed.id)])
            now = await check_with(
                KeyedLookup(conn, StructuredReadSet()),
                story,
                heard.proposition,
                holder=Holder.belief(bobby),
            )
        assert now.kind is VerdictKind.CONTRADICTED

        # Turn 3: Jenni tells Bobby again; his turn-1 exposure resolves first, in its frame.
        await _turn(client, epistrel, story, ids, _tells(bobby))
        resolving = await _turn_op(engine, story, 3)
        frame = Position(1, 0, 0, int(exposed.position_beat), 1)
        (resolution,) = [r for r in made if r.exposure.id == str(exposed.id)]
        assert resolution.outcome.decision == "accept"
        assert resolution.outcome.verdict == VerdictKind.NOVEL.value
        assert resolution.reads
        for read in resolution.reads:
            assert read.through is not None, read
            assert read.through <= frame.key(), read  # turn 2's canon was never read
        (held,) = await _sending(engine, story, bobby, things)
        assert (held.polarity, held.confidence_level, held.state) == (
            "affirmed",
            "believed",
            "held",
        )
        assert (held.held_from_turn, held.held_from_beat, held.held_from_step) == (1, frame.beat, 1)
        assert held.resolution_operation_id == resolving
        assert held.operation_id == resolving
        report = (await turn_report(engine, story))["epistemic"]
        # Both of his turn-1 exposures on Olivia's sending (the declared and the detected claim).
        assert report["resolutions"] == {"defaulted": len(made), "adjudicated": 0}
        assert len(made) == 2
        assert report["transitions"]["by_kind"]["testimony"] >= 2


@pytest.mark.part("M5", "FR-BELIEF-5,6,7,8")
async def test_s_pollution__observed(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Bobby observes the truth at turn 2: belief (turn-1 frame), then correction (turn 2)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, things = await _setup(client, engine)
        bobby = ids["Bobby"]
        await _turn(client, epistrel, story, ids, _says())
        await _turn(client, epistrel, story, ids, _shown())
        (seen,) = await _sending(engine, story, bobby, things)
        assert (seen.polarity, seen.confidence_level, seen.held_from_turn) == (
            "negated",
            "known",
            2,
        )
        await _turn(client, epistrel, story, ids, _tells(bobby))
        found = await _sending(engine, story, bobby, things)
        believed = [b for b in found if b.polarity == "affirmed"]
        (first,) = believed
        assert (first.held_from_turn, first.held_to_turn, first.state) == (1, 2, "corrected")
        assert first.corrected_by == seen.id
        kinds = await rows(
            engine,
            "SELECT kind, position_turn, narrative_turn FROM belief_transitions "
            "WHERE story_id = :s AND holder_character_id = :h AND membership = 'active' "
            "AND kind IN ('testimony', 'observation', 'correction') "
            "ORDER BY position_turn, position_beat, position_step, tx_from",
            s=story,
            h=bobby,
        )
        history = [(k.kind, k.position_turn) for k in kinds]
        assert ("observation", 2) in history
        assert ("correction", 2) in history
        assert history.index(("testimony", 1)) < history.index(("correction", 2))
        corrected = next(k for k in kinds if k.kind == "correction")
        assert corrected.narrative_turn == 3  # decided by the turn that needed it


@pytest.mark.part("M5", "FR-BELIEF-5,6; FR-STORE-31")
async def test_s_intra_turn_frame(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, things = await _setup(client, engine)
        marcus = ids["Marcus"]
        # Beat 2: Jenni's claim, witnessed by Marcus; beat 3: the truth, into canon unseen.
        await _turn(client, epistrel, story, ids, _says(), _truth())
        (exposed,) = await _exposures(engine, story, marcus, things["sent"])
        assert (exposed.position_turn, exposed.position_beat, exposed.position_step) == (1, 2, 1)
        (fact,) = await rows(
            engine,
            "SELECT narrative_turn, beat FROM facts WHERE story_id = :s AND predicate_id = :p "
            "AND membership = 'active'",
            s=story,
            p=things["sent"],
        )
        assert (fact.narrative_turn, fact.beat) == (1, 3)
        assert await _sending(engine, story, marcus, things) == []
        await _turn(client, epistrel, story, ids, beat("do", "She waits."))
        await _turn(client, epistrel, story, ids, _tells(marcus))
        (held,) = [
            b for b in await _sending(engine, story, marcus, things) if b.polarity == "affirmed"
        ]
        # Resolved in the (turn 1, beat 2, step 1) frame: beat 3's truth was excluded.
        assert (held.held_from_turn, held.held_from_beat, held.held_from_step) == (1, 2, 1)
        assert held.resolution_operation_id == await _turn_op(engine, story, 3)
