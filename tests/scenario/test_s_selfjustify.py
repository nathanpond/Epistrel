"""S-SELFJUSTIFY: ``think`` cannot launder an error (docs/04 §7; FR-VERIFY-10, FR-EPI-7,13,
FR-TURN-8; #166).

Canon is the Bobby education fixture (``tests.fixtures.bobby_education``: ``bobby_ba`` was
awarded by OSU). The story keeps per-character partitions (testimony ``defaulted``), so Bobby's
first-person ``say`` is checked against his beliefs as well as canon and classified
(``core.verify.speech_acts``). Played end to end through ``respond`` with the stub adapter.

- (a) Bobby holds his OSU degree (an author seed: the fixture's ``self`` belief, this variant
  only). The render's ``say`` "I got my degree at Florida" with ``think`` "I remember graduating
  from Florida" has no antecedent: the claim is a hallucination, repaired (rung 2 restores OSU),
  and no belief about Florida is written.
- (b) ``think`` "I'll lie about Florida" (a ``deceptive`` say) while Bobby holds **no** belief
  about his schooling: a declared lie with no underlying belief is a hallucination, repaired
  whatever the declared intent; no divergence.
- (c) a prior recorded message had Cal misinform Bobby that he went to Florida. Bobby states it:
  a legitimate false belief (``sincere_error``), written with that provenance (testimony from
  Cal, citing the misinforming say), with or without a ``think`` citation (the engine finds the
  antecedent). Both shapes of the hearing run: the salient witness (eager: Bobby holds that Cal
  *said* it, a ``said`` row, his default having rejected it against canon) and the lazy
  exposure (#164: resolved at this need site, in its frame).

The belief set never changes without an antecedent-backed cause: (a) and (b) leave the counts
of ``beliefs``, ``belief_transitions`` and ``exposures`` as they were and write no
``divergences`` row.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import BeliefKind, OperationKind, Provenance
from epistrel.core.epistemic import exposure, partitions, testimony, transitions
from epistrel.core.events import Operation
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.propositions.registry import load
from tests import canon_support as c
from tests import partition_support as parts
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.fixtures.bobby_education import DEGREE_AWARDED, GRADUATION, Education, bobby_education
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, history, render, turn_report

pytestmark = pytest.mark.integration

FLORIDA_LINE = "I got my degree at Florida."
REMEMBER = "I remember graduating from Florida."
LIE_THINK = "I'll lie about Florida."
MISINFORMED = "You got your degree at Florida, Bobby."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _degree(school: str) -> dict[str, Any]:
    return claim("I", "degree_awarded", school, qualifiers={"degree_id": "bobby_ba"})


def _degree_of(edu: Education, school: str) -> Proposition:
    """``degree_awarded(Bobby, {school}, degree_id = bobby_ba)``: what the say normalizes to."""
    return Proposition(
        subject_id=edu.bobby,
        predicate_id=DEGREE_AWARDED,
        object_id=school,
        qualifiers={"degree_id": "bobby_ba"},
        valid_time=Instant.of(GRADUATION, Granularity.MINUTE),
    )


async def _story(client: Any, engine: AsyncEngine) -> Education:
    edu = await bobby_education(client, engine)
    await ts.enable_testimony(engine, edu.story_id, "defaulted", level="co_location")
    return edu


async def _counts(engine: AsyncEngine, story_id: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for table in ("beliefs", "belief_transitions", "exposures", "divergences"):
        value = await scalar(
            engine,
            f"SELECT count(*) FROM {table} WHERE story_id = :s",  # noqa: S608 - fixed names
            s=story_id,
        )
        found[table] = int(value or 0)
    return found


async def _play(client: Any, edu: Education, *beats: dict[str, Any], stub: Any) -> dict[str, Any]:
    stub.script("generation", [render(*beats)])
    response = await respond(client, edu.story_id, body(edu.cal, "Where did you study?"))
    assert response.status_code == 200, response.text
    played: dict[str, Any] = response.json()
    return played


async def _speech(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    (verdict,) = [
        v for v in (await turn_report(engine, story_id))["verify"]["verdicts"] if "speech" in v
    ]
    found: dict[str, Any] = verdict["speech"]
    return found


async def _misinform(engine: AsyncEngine, edu: Education, *, eager: bool) -> str:
    """A turn in which Cal tells Bobby he got his degree at Florida: Bobby hears it as a salient
    witness (``eager``: his ``said`` row, the default having rejected it against canon he may
    read) or is exposed to it (#164's lazy path). Answers the say event's id."""
    told = _degree_of(edu, edu.florida)
    found: dict[str, Any] = {}
    settings = await parts.partitioned(engine, edu.story_id)
    async with Operation(
        engine, story_id=edu.story_id, kind=OperationKind.TURN, principal=c.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": MISINFORMED}, provenance=Provenance.PLAYER, beat=1)
        op.append(
            "beat_say",
            {"text": MISINFORMED, "intent": "sincere"},
            character_id=edu.cal,
            beat=2,
        )
        op.append("message_rendered", {"text": MISINFORMED})

        async def step(conn: AsyncConnection) -> None:
            event_id = str(
                await conn.scalar(
                    text("SELECT id FROM events WHERE operation_id = :o AND type = 'beat_say'"),
                    {"o": op.operation_id},
                )
            )
            found["event"] = event_id
            at = testimony.witness_position(op.narrative_turn or 0, op.turn_variant or 0, 2)
            if eager:
                await transitions.transition(
                    conn,
                    op,
                    holder=edu.bobby,
                    kind=transitions.Kind.TESTIMONY,
                    proposition=told,
                    antecedent=transitions.Antecedent.record("events", event_id),
                    position=at,
                    confidence=transitions.Confidence(testimony.Level.KNOWN),
                    source=edu.cal,
                    outcome=testimony.REJECT,
                    belief_kind=BeliefKind.SPEECH_ACT,
                    settings=settings,
                )
                return
            registry = await load(conn, edu.story_id)
            stored = await store.write_proposition(conn, edu.story_id, told, registry)
            await exposure.record_exposures(
                conn,
                op,
                [
                    exposure.Exposed(
                        witness=edu.bobby,
                        proposition_id=stored,
                        proposition=told,
                        source=edu.cal,
                        source_event_id=event_id,
                        position=at,
                    )
                ],
            )

        op.add_commit_step(step)
        await op.commit()
    return str(found["event"])


async def _florida_beliefs(engine: AsyncEngine, edu: Education) -> list[Any]:
    """Bobby's first-order beliefs that name Florida, with their producing transition."""
    return await rows(
        engine,
        "SELECT b.id, b.confidence_level, b.held_from_turn, t.kind, t.source_character_id, "
        "t.outcome, t.antecedent_ref FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
        "JOIN belief_transitions t ON t.id = b.provenance WHERE b.story_id = :s "
        "AND b.holder_character_id = :h AND b.kind = 'first_order' "
        "AND p.object_id = :f AND b.tx_to IS NULL",
        s=edu.story_id,
        h=edu.bobby,
        f=edu.florida,
    )


@pytest.mark.part("M5", "FR-VERIFY-10; FR-EPI-7,13; FR-TURN-8")
async def test_s_selfjustify(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Variants (a) and (b) (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- (a) a remembered degree with no antecedent
        edu = await _story(client, engine)
        await parts.believe(
            engine, edu.story_id, edu.bobby, _degree_of(edu, edu.osu), kind=OperationKind.CONTROL
        )
        before = await _counts(engine, edu.story_id)
        played = await _play(
            client,
            edu,
            beat("think", REMEMBER),
            beat("say", FLORIDA_LINE, intent="sincere", propositions=[_degree("Florida")]),
            stub=stub,
        )
        found = await _speech(engine, edu.story_id)
        assert (found["classification"], found["belief_relation"]) == (
            "hallucination_repaired",
            "inconsistent",
        )
        assert found["contrary_belief_ids"]
        assert found["licence"] is None
        (repaired,) = (await turn_report(engine, edu.story_id))["repairs"]
        assert repaired["rung"] == 2
        assert "Florida" not in played["message"]["text"]
        assert await _counts(engine, edu.story_id) == before
        assert await _florida_beliefs(engine, edu) == []

        # -- (b) a declared lie with no underlying belief about his schooling
        edu = await _story(client, engine)
        before = await _counts(engine, edu.story_id)
        stub.reset()
        played = await _play(
            client,
            edu,
            beat("think", LIE_THINK),
            beat("say", FLORIDA_LINE, intent="deceptive", propositions=[_degree("Florida")]),
            stub=stub,
        )
        found = await _speech(engine, edu.story_id)
        assert (found["declared_intent"], found["classification"]) == (
            "deceptive",
            "hallucination_repaired",
        )
        assert found["contrary_belief_ids"] == []
        (repaired,) = (await turn_report(engine, edu.story_id))["repairs"]
        assert repaired["accepted"]
        assert "Florida" not in played["message"]["text"]
        assert await _counts(engine, edu.story_id) == before
        assert await _florida_beliefs(engine, edu) == []


@pytest.mark.part("M5", "FR-VERIFY-10; FR-EPI-7,13; FR-TURN-8")
async def test_s_selfjustify__misinformed(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Variant (c) (see the module docstring): the salient witness and the lazy exposure, each
    with and without a ``think`` citing the misinforming say."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for eager in (True, False):
            for cited in (False, True):
                edu = await _story(client, engine)
                said = await _misinform(engine, edu, eager=eager)
                stub.reset()
                played = await _play(
                    client,
                    edu,
                    beat("think", REMEMBER, **({"cites": [said]} if cited else {})),
                    beat("say", FLORIDA_LINE, intent="sincere", propositions=[_degree("Florida")]),
                    stub=stub,
                )
                case = (eager, cited)
                found = await _speech(engine, edu.story_id)
                assert (found["classification"], found["belief_relation"]) == (
                    "sincere_error",
                    "consistent",
                ), case
                licence = found["licence"]
                assert licence["path"] == ("said" if eager else "exposure"), case
                assert (licence["kind"], licence["antecedent"]["id"]) == ("testimony", said), case
                assert licence["cited"] is cited, case
                report = await turn_report(engine, edu.story_id)
                assert "repairs" not in report, case
                # #177: Cal is unplaced (absent at co_location), so his own read perceives none
                # of Bobby's beats; the omniscient read carries the released line.
                seen = await history(client, edu.story_id, "omniscient")
                assert any("Florida" in i["text"] for i in seen if i["channel"] == "say"), case
                assert played["message"]["text"] == "", case
                (belief,) = await _florida_beliefs(engine, edu)
                assert (
                    belief.kind,
                    belief.source_character_id,
                    belief.outcome,
                    belief.confidence_level,
                    belief.antecedent_ref["id"],
                    belief.held_from_turn,
                ) == ("testimony", edu.cal, "accept", "believed", said, 1), case
                (utterance,) = await rows(
                    engine,
                    "SELECT classification, belief_relation FROM utterances WHERE story_id = :s "
                    "AND speaker_character_id = :b",
                    s=edu.story_id,
                    b=edu.bobby,
                )
                assert (utterance.classification, utterance.belief_relation) == (
                    "sincere_error",
                    "consistent",
                ), case
                assert (await _counts(engine, edu.story_id))["divergences"] == 0, case
                if not eager:
                    async with engine.connect() as conn:
                        (heard,) = await exposure.exposures_of(conn, edu.story_id, edu.bobby)
                    assert heard.state.value == "resolved", case
                    assert report["epistemic"]["speech"]["resolutions"] == 1, case
                assert report["epistemic"]["speech"]["licensed"] == 1, case
                assert report["epistemic"]["transitions"]["by_kind"].get("testimony", 0) >= 1
                async with engine.connect() as conn:
                    held = await partitions.partition(conn, edu.story_id, edu.bobby)
                assert any(b.id == belief.id for b in held), case
