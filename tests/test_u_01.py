"""U-01 (M4): the commitment lifecycle (FR-COMMIT-1,2,3,5,6,7; docs/04 §3; #146). One file per
test ID and milestone; U-01's M5 part (FR-COMMIT-4,5's determined-but-unknown) joins it.

The knock fixture (``tests/commit_support``): "Someone knocks at the door" made salient by a
scripted ``salience_detection`` verdict, a mention, a dependence, a second dependence, a hidden
fact and the fact lifecycle's moves."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.canon.facts import Visibility
from epistrel.core.commit import lifecycle
from epistrel.core.enums import (
    DischargeMode,
    FactCommitState,
    OpenSlot,
    RecordProvenance,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.verify.normalize import ClaimSketch
from epistrel.core.verify.rules import VerdictKind, check_with
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.commit.salience import (
    detect_salience,
    rules_salient,
)
from epistrel.orchestrator.models.registry import (
    SALIENCE_DETECTION,
    SalienceSketch,
    SceneContext,
)
from tests import commit_support as c
from tests import structured_turn_support as structured
from tests.auth_support import auth_settings
from tests.canon_support import commit
from tests.respond_support import body, respond, respond_client
from tests.verify_support import stub_models

pytestmark = pytest.mark.integration

COMMITTED, INTRODUCED, PENDING, REVISED = (
    FactCommitState.COMMITTED,
    FactCommitState.INTRODUCED,
    FactCommitState.PENDING,
    FactCommitState.REVISED,
)

SCENE = SceneContext(present_entities=["Bobby", "Jenni"], last_beats=["Jenni sets the table."])


@pytest.mark.part("M4", "FR-COMMIT-1,2,3,5,6,7")
async def test_u_01(engine: AsyncEngine) -> None:
    """Salience triggers commitment and texture needs none (FR-COMMIT-3): the scripted verdict
    makes beat 2's knock salient and beat 3 texture, so one thunk (its knocker open: a global
    thunk with no world value, FR-COMMIT-5,6) and one ledger entry are recorded, and nothing
    for the texture. While the entry is undischarged the turn has a blocker (FR-COMMIT-7).
    Bobby's ``say`` "Who's that?" naming the mailman only mentions: the thunk stays open and
    no fact is written (complement). Jenni's ``do`` (she opens the door to the mailman) depends
    on the value: it commits at that beat (FR-COMMIT-2), ``committed``, from the thunk's
    story-minute, discharging the entry by dependence, and the turn closes with no minimal
    commit. The value is then global: every viewpoint reads one knocker (FR-COMMIT-6)."""
    k = await c.knock_story(engine)
    mal = stub_models()
    mal.stub.script(
        SALIENCE_DETECTION.name,
        [
            c.salience_answer(
                (2, "Someone knocks at the door", c.KNOCK), texture=[(3, "The kettle hums")]
            )
        ],
    )
    beats = [
        k.beat(2, "narration", "Someone knocks at the door."),
        k.beat(3, "narration", "The kettle hums softly."),
    ]
    verdict = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=4))
    assert verdict.called
    assert verdict.degradation == ()
    assert [s.span.beat for s in verdict.salient] == [2]
    assert [t.beat for t in verdict.texture] == [3]
    (call,) = [x for x in mal.stub.calls if x.purpose == SALIENCE_DETECTION.name]
    assert "Someone knocks at the door." in call.messages[-1].content

    async def first(conn: AsyncConnection, op: Operation) -> dict[str, object]:
        report = await c.salient(conn, op, k, beats[0], verdict.salient[0].proposition_sketch)
        held = await lifecycle.blockers(conn, k.story_id)
        mention = await lifecycle.depend(
            conn,
            op,
            report.recorded[0].thunk_id or "",
            c.knocker(k, k.mailman, 0),
            beat=4,
            channel="say",
        )
        mentioned = await lifecycle.read_thunk(conn, k.story_id, report.recorded[0].thunk_id or "")
        facts_after_mention = await conn.scalar(
            text("SELECT count(*) FROM facts WHERE story_id = :s AND predicate_id = :p"),
            {"s": k.story_id, "p": k.knocked_at},
        )
        dependence = await lifecycle.depend(
            conn,
            op,
            report.recorded[0].thunk_id or "",
            c.knocker(k, k.mailman, 0),
            beat=5,
            channel="do",
        )
        closed = await lifecycle.close_turn(conn, op)
        return {
            "report": report,
            "held": held,
            "mention": mention,
            "mentioned": mentioned,
            "facts_after_mention": facts_after_mention,
            "dependence": dependence,
            "closed": closed,
            "op": op,
        }

    found = await c.in_turn(engine, k.story_id, first)
    (recorded,) = found["report"].recorded
    assert recorded.outcome == "entry"
    thunks = await c.rows(engine, "thunks", k.story_id)
    ledger = await c.rows(engine, "promissory_ledger", k.story_id)
    assert len(thunks) == 1
    assert len(ledger) == 1
    assert str(thunks[0].open_slot) == OpenSlot.SUBJECT.value
    assert thunks[0].subject_id is None
    assert thunks[0].object_id == k.door
    assert thunks[0].salient is True
    assert [e.thunk_id for e in found["held"]] == [thunks[0].id]

    assert found["mention"].kind == "mention"
    assert found["mentioned"].state.value == "open"
    assert found["facts_after_mention"] == 0  # complement: a mention writes no fact

    dependence = found["dependence"]
    assert dependence.kind == "committed"
    assert dependence.discharged == (ledger[0].id,)
    fact = dependence.fact
    assert fact.commit_state is COMMITTED
    assert fact.thunk_id == thunks[0].id
    assert fact.valid_from == thunks[0].valid_time
    rows = await c.rows(engine, "facts", k.story_id)
    (fact_row,) = [r for r in rows if r.id == fact.id]
    assert fact_row.committed_at_position == {
        "turn": found["op"].narrative_turn,
        "variant": found["op"].turn_variant,
        "beat": 5,
    }
    resolved = await c.thunk(engine, k.story_id, thunks[0].id)
    assert resolved.state.value == "resolved"
    assert resolved.resolved_fact_id == fact.id
    closed = found["closed"]
    assert (closed.minimal_commits, closed.dependences, closed.ledger_discharged) == (0, 1, 1)
    assert closed.degradation == []
    assert await c.blockers(engine, k.story_id) == []
    (entry,) = await c.rows(engine, "promissory_ledger", k.story_id)
    assert str(entry.discharged_mode) == DischargeMode.DEPENDENCE.value

    # Determined globally: one knocker, whoever reads.
    async with engine.connect() as conn:
        for viewpoint in ("omniscient", "narrator", f"character:{k.bobby}", f"character:{k.jenni}"):
            seen = await canon.canon(conn, k.story_id, predicate=k.knocked_at, viewpoint=viewpoint)
            assert [f.proposition.subject_id for f in seen] == [k.mailman], viewpoint


@pytest.mark.part("M4", "FR-COMMIT-5,6")
async def test_u_01__hidden_and_undetermined(engine: AsyncEngine) -> None:
    """Hidden is not undetermined (FR-COMMIT-5): a fact committed with Bobby's ``characters``
    visibility (he knows what the parcel holds) is decided and sticky: Jenni's read does not
    see it, the omniscient read does, it has no thunk, and a salient reference to it owes no
    ledger entry because canon already settles it. An undetermined value is a thunk with no
    world value for anyone, the omniscient reader included (FR-COMMIT-6: "unknown to Jenni" is
    absence, "undetermined" is global). Complement: the undetermined knock does get an
    entry."""
    k = await c.knock_story(engine)
    hidden = await commit(
        engine,
        k.story_id,
        c.knocker(k, k.bobby, 0).model_copy(update={"object_id": k.parcel}),
        visibility=Visibility(scope="characters", characters=(k.bobby,)),
    )
    async with engine.connect() as conn:
        mine = await canon.canon(conn, k.story_id, viewpoint=f"character:{k.bobby}")
        hers = await canon.canon(conn, k.story_id, viewpoint=f"character:{k.jenni}")
        everyone = await canon.canon(conn, k.story_id)
    assert hidden.id in {f.id for f in mine}
    assert hidden.id in {f.id for f in everyone}
    assert hidden.id not in {f.id for f in hers}

    parcel = SalienceSketch(subject="Bobby", relation="knocked_at", object="the parcel")
    hidden_reference = SalienceSketch(subject="someone", relation="knocked_at", object="the parcel")

    async def step(conn: AsyncConnection, op: Operation) -> list[str]:
        out = []
        for sketch in (parcel, hidden_reference, c.KNOCK):
            report = await c.salient(conn, op, k, k.beat(2, "narration", "x"), sketch)
            out.append(report.recorded[0].outcome)
        return out

    outcomes = await c.in_turn(engine, k.story_id, step)
    # A fully stated reference is a plain claim; the hidden one is settled; the knock is owed.
    assert outcomes == ["determined", "settled", "entry"]
    thunks = await c.rows(engine, "thunks", k.story_id)
    assert [t.object_id for t in thunks] == [k.door]
    async with engine.connect() as conn:
        knocks = await canon.canon(conn, k.story_id, predicate=k.knocked_at, object=k.door)
    assert knocks == []  # the global thunk has no world value, even for omniscient


@pytest.mark.part("M4", "FR-COMMIT-1")
async def test_u_01__fact_lifecycle(engine: AsyncEngine) -> None:
    """FR-COMMIT-1's states: a fact narrated with an open qualifier is ``introduced``; it may
    be ``pending`` a dependence in its turn; a dependence commits it at its beat (``settle_fact``
    records ``committed_at_position``); a pending fact nobody leaned on is demoted back to
    ``introduced`` at the close; a committed fact can be ``revised`` (#154's revert) and
    restored. Complement: a ``say`` settles nothing, any other move is refused (422, and the
    facts guard refuses it in the database too, as it refuses any other in-place change), and
    an introduced fact is no evidence for verify."""
    k = await c.knock_story(engine)

    async def introduce(conn: AsyncConnection, op: Operation) -> tuple[str, str]:
        a = await canon.commit_fact(
            conn,
            k.story_id,
            c.knocker(k, k.mailman, 0),
            provenance=RecordProvenance.RENDER,
            operation=op,
            commit_state=INTRODUCED,
            beat=2,
        )
        b = await canon.commit_fact(
            conn,
            k.story_id,
            c.knocker(k, k.bobby, 0).model_copy(update={"object_id": k.parcel}),
            provenance=RecordProvenance.RENDER,
            operation=op,
            commit_state=PENDING,
            beat=3,
        )
        await lifecycle.transition(conn, k.story_id, a.id, PENDING)
        said = await lifecycle.settle_fact(conn, op, a.id, beat=4, channel="say")
        done = await lifecycle.settle_fact(conn, op, a.id, beat=5, channel="do")
        assert (said, done) == (False, True)
        report = await lifecycle.close_turn(conn, op)
        assert report.demoted == 1
        return a.id, b.id

    settled, demoted = await c.in_turn(engine, k.story_id, introduce)
    states = {
        r.id: (str(r.commit_state), r.committed_at_position)
        for r in await c.rows(engine, "facts", k.story_id)
    }
    assert states[settled][0] == "committed"
    assert states[settled][1]["beat"] == 5
    assert states[demoted][0] == "introduced"

    async with engine.begin() as conn:
        assert await lifecycle.transition(conn, k.story_id, settled, REVISED) is COMMITTED
        await lifecycle.transition(conn, k.story_id, settled, COMMITTED)
        with pytest.raises(InvalidRequest):
            await lifecycle.transition(conn, k.story_id, demoted, REVISED)
    for statement in (
        "UPDATE facts SET commit_state = 'revised' WHERE id = :f",  # introduced -> revised
        "UPDATE facts SET beat = 9 WHERE id = :f",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"f": demoted})

    # An introduced fact is no evidence: verify reads canon past it.
    async with engine.connect() as conn:
        lookup = KeyedLookup(conn, StructuredReadSet())
        claim = c.knocker(k, k.bobby, 0).model_copy(update={"object_id": k.parcel})
        verdict = await check_with(lookup, k.story_id, claim)
        assert verdict.kind is not VerdictKind.SUPPORTED


@pytest.mark.part("M4", "FR-COMMIT-3")
async def test_u_01__rules_and_degradation() -> None:
    """Under ``commitment.salience = rules`` no model is called, and only open-slot claims on
    ``do`` and narration beats are salient (a ``say`` never is; a fully stated claim is no
    reference to an unknown). Salience is optional work: no model, a spent budget, a failed or
    unparseable answer each degrade to all texture with the reason in ``degradation``; the
    stub's unscripted answer is all texture."""
    mal = stub_models()
    beats = [c.beat(1, "narration", "Someone knocks.")]
    assert (await detect_salience(beats, SCENE, mode="rules", models=mal)).called is False
    assert mal.stub.calls == []
    claims = [
        (
            beats[0],
            ClaimSketch(subject_text="someone", relation_text="knocked_at", object_text="door"),
        ),
        (
            c.beat(2, "say", "Someone knocked!"),
            ClaimSketch(subject_text="someone", relation_text="knocked_at", object_text="door"),
        ),
        (
            beats[0],
            ClaimSketch(subject_text="Bobby", relation_text="knocked_at", object_text="door"),
        ),
    ]
    assert [b.index for b, _ in rules_salient(claims)] == [1]

    assert (await detect_salience(beats, SCENE)).degradation == ("salience_unavailable",)
    spent = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=0))
    assert spent.salient == ()
    assert spent.degradation == ("salience_skipped",)
    mal.stub.fault(SALIENCE_DETECTION.name, "garbage", n=10)
    failed = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=9))
    assert failed.salient == ()
    assert failed.degradation == ("salience_failed",)
    mal.stub.reset()
    default = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=4))
    assert default.salient == ()
    assert default.degradation == ()


@pytest.mark.part("M4", "FR-COMMIT-3")
async def test_u_01__quoted_spans() -> None:
    """The model quotes each span and the engine locates the quote in the beat it names (#461;
    small models place character offsets poorly, E-05's "Mare" for "a letter before anyone can
    read it"): exactly, case-insensitively, or without the quotation marks a model adds. A quote
    not found in its beat, or naming a beat the request did not carry, is never mapped to a
    span: it is dropped into ``unlocated``, salient and texture alike."""
    mal = stub_models()
    letter = SalienceSketch(subject="Maren", relation="burned", object="a letter")
    beats = [
        c.beat(0, "narration", "The rain keeps on."),
        c.beat(1, "do", "Maren burns a letter before anyone can read it."),
    ]
    mal.stub.script(
        SALIENCE_DETECTION.name,
        [
            c.salience_answer(
                (1, "a letter before anyone can read it", letter),
                (1, '"A Letter Before Anyone Can Read It."', letter),
                texture=[(0, "rain keeps")],
            )
        ],
    )
    found = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=4))
    assert [(s.span.beat, s.span.start, s.span.end) for s in found.salient] == [
        (1, 12, 46),
        (1, 12, 47),
    ]
    assert found.salient[0].proposition_sketch == letter
    assert [(t.beat, t.start, t.end) for t in found.texture] == [(0, 4, 14)]
    assert found.unlocated == ()

    mal.stub.script(
        SALIENCE_DETECTION.name,
        [
            c.salience_answer(
                (1, "a letter nobody reads", letter),
                (0, "a letter before anyone can read it", letter),
                (7, "a letter", letter),
                texture=[(0, "the snow")],
            )
        ],
    )
    lost = await detect_salience(beats, SCENE, models=mal, budget=TurnBudget(max_calls=4))
    assert lost.called
    assert lost.degradation == ()
    assert lost.salient == ()
    assert lost.texture == ()
    assert [(q.beat, q.span) for q in lost.unlocated] == [
        (1, "a letter nobody reads"),
        (0, "a letter before anyone can read it"),
        (7, "a letter"),
        (0, "the snow"),
    ]


@pytest.mark.integration
@pytest.mark.part("M4", "FR-COMMIT-7")
async def test_u_01__follow_up(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A turn cannot close with a salient reference it made still open (FR-COMMIT-7; #146's AC,
    #150's pipeline). Bobby's ``do`` "Someone knocks at the door." is made salient (who
    knocked is open); the turn's budget can pay for one more render, so the narrator renders
    once more, told to settle it, and its narration names the mailman: the settling fact is
    committed, its beat is the turn's next beat, and no minimal commit is made. Complements:
    with the budget spent before the follow-up, the minimal commit settles the reference and
    the turn reports ``follow_up_skipped_budget``; a follow-up that settles one of two open
    references leaves the minimal commit for the other alone."""
    settings = auth_settings(pg_url, model_adapter="stub")
    knock_text = "Someone knocks at the door."
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        async def play(
            k: c.Knock, spans: list[Any], settle: bool = True, **fields: Any
        ) -> dict[str, Any]:
            stub.reset()
            renders = [structured.render(structured.beat("do", knock_text))]
            if settle:
                renders.append(
                    structured.render(
                        structured.beat(
                            "narration",
                            "The mailman knocked at the door.",
                            propositions=[structured.claim("mailman", "knocked_at", "door")],
                        )
                    )
                )
            stub.script("generation", renders)
            stub.script("salience_detection", [c.salience_answer(*spans)])
            response = await respond(
                client,
                k.story_id,
                body(k.cal, "Did you hear that?", render_character_ids=[k.bobby], **fields),
            )
            assert response.status_code == 200, response.text
            played: dict[str, Any] = response.json()
            return played

        async def knockers(k: c.Knock) -> list[Any]:
            async with engine.connect() as conn:
                found = await canon.canon(
                    conn, k.story_id, predicate=k.knocked_at, viewpoint="omniscient"
                )
            return [f.proposition.subject_id for f in found]

        # -- the follow-up settles the reference
        k = await c.knock_story(engine)
        played = await play(k, [(2, knock_text, c.KNOCK)])
        commit_report = (await structured.turn_report(engine, k.story_id))["commit"]
        assert commit_report["follow_up"]["path"] == "follow_up"
        assert commit_report["follow_up"]["settled"] == 1
        assert commit_report["minimal_commits"] == 0
        follow = [b for b in played["beats"] if b["kind"] == "beat_narration"]
        assert [(b["beat"], b["actor_character_id"]) for b in follow] == [(3, None)]
        assert k.mailman in await knockers(k)
        assert "follow_up_skipped_budget" not in played["degradation"]
        narrator = [cl for cl in stub.calls if cl.response_schema == "NarratorRender"]
        assert len(narrator) == 1
        assert "## Settle now" in narrator[0].messages[0].content
        # The calls the turn made before its follow-up render (the second ``generation``), plus
        # the composed message's detection C, which #156's planner reserves from the start and
        # no optional work may spend (NFR-PERF-6): under that cap salience still fits and the
        # follow-up (a render plus its extraction) does not.
        purposes = [call["purpose"] for call in played["model_calls"]]
        before = [n for n, p in enumerate(purposes) if p == "generation"][1]
        reservation = (await structured.turn_report(engine, k.story_id))["budget"]["reservation"]

        # -- complement: no budget left for the follow-up, so the minimal commit settles it
        k = await c.knock_story(engine)
        played = await play(
            k,
            [(2, knock_text, c.KNOCK)],
            settle=False,
            budget={"max_calls": before + reservation["composed"]},
        )
        assert "salience_skipped" not in played["degradation"]
        assert "follow_up_skipped_budget" in played["degradation"]
        commit_report = (await structured.turn_report(engine, k.story_id))["commit"]
        assert commit_report["follow_up"]["skipped"] == "budget"
        assert commit_report["follow_up"]["path"] == "minimal"
        assert commit_report["minimal_commits"] == 1
        assert not [cl for cl in stub.calls if cl.response_schema == "NarratorRender"]

        # -- the follow-up settles one of two: the minimal commit is for the other only
        k = await c.knock_story(engine)
        await play(k, [(2, knock_text, c.KNOCK), (2, knock_text, c.WHY)])
        commit_report = (await structured.turn_report(engine, k.story_id))["commit"]
        assert commit_report["follow_up"]["path"] == "follow_up_and_minimal"
        assert (commit_report["follow_up"]["settled"], commit_report["follow_up"]["open"]) == (1, 1)
        assert commit_report["minimal_commits"] == 1
        assert k.mailman in await knockers(k)
