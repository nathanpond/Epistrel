"""S-EDGE-TYPES: the dependency edge types (docs/04 §7; FR-DIRECT-7; #220), the setup half (#225
adds the retcon to UCLA).

Bobby's education (``tests/fixtures/bobby_education.py``, with #220's witnesses): canon says
Bobby went to OSU. Played end to end through ``respond`` with the stub adapter, under
per-character partitions with testimony ``defaulted`` at perception ``full``:

1. Bobby lies to Marcus, "I went to UCLA." (a deceptive ``say`` declaring ``enrolled_at(Bobby,
   UCLA)``), and Marcus believes him (testimony: the say is salient);
2. Sarah comes in and sees Bobby's OSU diploma (a narration declaring ``enrolled_at(Bobby,
   OSU)``, supported by canon): she observes it;
3. Bobby thinks of his OSU friends, citing the enrollment, and decides to call his old roommate
   (a ``think`` beat citing the fact, then the ``do``);
4. the narration mentions OSU in passing ("An OSU pennant hangs by the door.").

Exactly one edge of the right type per dependency: ``asserts`` on the lie's utterance (its
target the UCLA proposition), ``derives`` with ``acquisition_kind`` ``testimony`` for Marcus and
``observation`` for Sarah, ``causes`` from the citing beat to the enrollment, ``mentions`` from
the pennant narration to OSU. Complements: the lie writes no fact and no edge of a type other
than ``asserts``; the mention is not ``presupposes``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from tests import effect_support as es
from tests import multi_support as ms
from tests import partition_support as parts
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import ENROLLED_AT, Education, bobby_education
from tests.respond_support import body, played, respond_client
from tests.structured_turn_support import beat, claim

pytestmark = pytest.mark.integration

LIE = "I went to UCLA."
DIPLOMA = "Sarah notices Bobby's OSU diploma on the wall."
THINKS = "Bobby thinks of his old friends from OSU."
DECIDES = "Bobby decides to call his old roommate."
PENNANT = "Old OSU stories fill the evening."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _edges(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT edge_type::text AS edge_type, dependent_table, dependent_id, "
        "target_kind::text AS target_kind, target_id, acquisition_kind, narrative_turn "
        "FROM dependency_edges WHERE story_id = :s AND membership = 'active' ORDER BY id",
        s=story,
    )


async def _turn(
    client: Any,
    stub: Any,
    w: Education,
    *,
    bobby: str | None = None,
    narration: str | None = None,
) -> Any:
    """Cal listens; Bobby renders ``bobby`` (a nod by default), the narrator ``narration``."""
    stub.reset()
    ms.renders_for(stub, "Bobby", bobby or ms.render(beat("do", "Bobby nods.")))
    es.narrator(stub, narration or es.render(beat("narration", "The fire crackles.")))
    return await played(
        client,
        w.story_id,
        body(w.cal, None, do="listens", render_character_ids=[w.bobby]),
    )


async def _played(client: Any, stub: Any, engine: AsyncEngine) -> tuple[Education, dict[str, str]]:
    """Steps 1-4 of the module docstring, asserting each edge; answers the fixture and the
    records: the lie's ``utterance``, Marcus's and Sarah's beliefs, the ``think`` beat, and the
    pennant narration."""
    made: dict[str, str] = {}
    w = await bobby_education(client, engine, witnesses=True)
    assert w.marcus is not None
    assert w.sarah is not None
    assert w.parlor is not None
    await ts.enable_testimony(engine, w.story_id, "defaulted", lazy=False)
    # Bobby knows where he went, and that it was not UCLA (a lie needs a contrary
    # belief, #166).
    await parts.believe(engine, w.story_id, w.bobby, w.enrollment.proposition)
    await parts.believe(
        engine,
        w.story_id,
        w.bobby,
        Proposition(
            subject_id=w.bobby,
            predicate_id=ENROLLED_AT,
            object_id=w.ucla,
            polarity=Polarity.NEGATED,
            valid_time=Interval.of(None, None),
        ),
    )
    facts_before = await rows(engine, "SELECT id FROM facts WHERE story_id = :s", s=w.story_id)

    # 1. The lie, to Marcus: asserts, and Marcus's testimony.
    stub.reset()
    ms.renders_for(
        stub,
        "Bobby",
        ms.render(
            beat(
                "say",
                LIE,
                intent="deceptive",
                propositions=[claim("Bobby", "enrolled_at", "UCLA")],
            )
        ),
    )
    stub.script("salience_detection", [ts.salient_say(2, LIE)])
    lied = await played(
        client,
        w.story_id,
        body(w.cal, "Bobby, where did you study?", render_character_ids=[w.bobby]),
    )
    facts_after = await rows(engine, "SELECT id FROM facts WHERE story_id = :s", s=w.story_id)
    assert len(facts_after) == len(facts_before)  # the lie writes no fact
    lie_turn = lied["turn"]
    first = [e for e in await _edges(engine, w.story_id) if e.narrative_turn == lie_turn]
    asserts = [e for e in first if e.edge_type == "asserts"]
    (lie_edge,) = asserts
    assert (lie_edge.dependent_table, lie_edge.target_kind) == ("utterances", "proposition")
    utterance = lie_edge.dependent_id
    made["utterance"] = utterance
    assert {e.edge_type for e in first if e.dependent_id == utterance} == {"asserts"}
    marcus_beliefs = [
        b for b in await ts.beliefs_of(engine, w.story_id, w.marcus) if b.object_id == w.ucla
    ]
    assert marcus_beliefs, "Marcus believes Bobby"
    (marcus_edge,) = [
        e for e in first if e.edge_type == "derives" and e.dependent_id == marcus_beliefs[0].id
    ]
    assert marcus_edge.acquisition_kind == "testimony"
    made["marcus_belief"] = marcus_beliefs[0].id

    # 2. Sarah comes in and sees the diploma: observation.
    await ps.put(engine, w.story_id, w.sarah, w.parlor)
    seen = await _turn(
        client,
        stub,
        w,
        narration=es.render(
            beat(
                "narration",
                DIPLOMA,
                propositions=[claim("Bobby", "enrolled_at", "OSU")],
            )
        ),
    )
    second = [e for e in await _edges(engine, w.story_id) if e.narrative_turn == seen["turn"]]
    sarah_beliefs = [
        b for b in await ts.beliefs_of(engine, w.story_id, w.sarah) if b.object_id == w.osu
    ]
    assert sarah_beliefs, "Sarah observed the diploma"
    (sarah_edge,) = [
        e for e in second if e.edge_type == "derives" and e.dependent_id == sarah_beliefs[0].id
    ]
    assert sarah_edge.acquisition_kind == "observation"
    made["sarah_belief"] = sarah_beliefs[0].id

    # 3. The decision the enrollment caused: the citing beat.
    caused = await _turn(
        client,
        stub,
        w,
        bobby=ms.render(
            beat("think", THINKS, cites=[w.enrollment.id]),
            beat("do", DECIDES),
        ),
    )
    third = [e for e in await _edges(engine, w.story_id) if e.narrative_turn == caused["turn"]]
    causes = [e for e in third if e.edge_type == "causes"]
    assert [(e.target_kind, e.target_id) for e in causes] == [("fact", w.enrollment.id)]
    made["think"] = causes[0].dependent_id

    # 4. A mention in passing: mentions, not presupposes.
    mentioned = await _turn(client, stub, w, narration=es.render(beat("narration", PENNANT)))
    fourth = [e for e in await _edges(engine, w.story_id) if e.narrative_turn == mentioned["turn"]]
    mentions = [e for e in fourth if e.edge_type == "mentions" and e.target_id == w.osu]
    (mention,) = mentions
    made["pennant"] = mention.dependent_id
    assert not [
        e for e in fourth if e.dependent_id == mention.dependent_id and e.edge_type == "presupposes"
    ]
    return w, made


@pytest.mark.part("M7", "FR-DIRECT-7")
async def test_s_edge_types__setup(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            await _played(client, epistrel.models.stub, engine)
    finally:
        await ts.forget(engine)


UCLA_REWRITE = (
    '{"changes": [{"kind": "proposition", "subject": "Bobby", "predicate": "enrolled_at", '
    '"object": "UCLA"}]}'
)
REGENERATED = "Bobby thinks of his old friends from UCLA."


@pytest.mark.part("M7", "FR-DIRECT-7,8")
async def test_s_edge_types__m7(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The retcon to UCLA (#225), applied ``best_effort``, cascades per edge type: the lie
    keeps its text and ``deceptive`` intent while its objective relation becomes ``true`` (an
    attempted lie that happened to be true: ``scene_meaning_may_change``); Marcus's testimony
    belief is unchanged (row diff); Sarah's observation belief now holds UCLA; the decision the
    enrollment caused is regenerated and flagged; the pennant's mention of OSU is fixed.
    Complement: no fact is written for the lie, and no belief changes for Marcus."""
    from tests import rewrite_support as rs

    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            stub = epistrel.models.stub
            w, made = await _played(client, stub, engine)
            assert w.ucla is not None

            async def told() -> list[Any]:
                return [
                    b
                    for b in await ts.beliefs_of(engine, w.story_id, w.marcus or "")
                    if b.id == made["marcus_belief"]
                ]

            marcus_before = await told()
            stub.reset()
            stub.when("", UCLA_REWRITE, purpose="rewrite_parse")
            stub.when("", f'{{"text": "{REGENERATED}"}}', purpose="rewrite_record")
            opened = await rs.open_session(client, w.story_id, resolution_mode="best_effort")
            assert opened.status_code == 201, opened.text
            session = opened.json()
            revision = await rs.revision(engine, w.story_id)
            applied = await rs.apply(client, w.story_id, session["id"], revision)
            assert applied.status_code == 200, applied.text
            report = applied.json()["report"]

            (lie,) = await rows(
                engine,
                "SELECT u.objective_relation, u.belief_relation, u.intent, e.payload "
                "FROM utterances u JOIN events e ON e.id = u.event_id WHERE u.id = :u",
                u=made["utterance"],
            )
            assert (lie.objective_relation, lie.intent) == ("true", "deceptive")
            assert lie.payload["text"] == LIE
            notices = [
                n for n in report["notifications"] if n["kind"] == "scene_meaning_may_change"
            ]
            assert [n["utterance_id"] for n in notices] == [made["utterance"]]

            assert await told() == marcus_before
            sarah = [
                b
                for b in await ts.beliefs_of(engine, w.story_id, w.sarah or "")
                if b.predicate_id == ENROLLED_AT and b.held_to_turn is None and b.tx_to is None
            ]
            assert w.ucla in {b.object_id for b in sarah}, (report, session["preview"])
            cascaded = {b["belief_id"]: b["cascaded"] for b in report["applied"]["beliefs"]}
            assert cascaded[made["sarah_belief"]] is True
            assert made["marcus_belief"] not in cascaded

            regenerated = [e for e in report["applied"]["cascaded"] if e.get("regenerated")]
            assert {e["record"]["id"] for e in regenerated} == {made["think"]}
            lines = {
                str(r.message_id): r.text
                for r in await rows(
                    engine,
                    "SELECT message_id, text FROM transcript_messages WHERE story_id = :s",
                    s=w.story_id,
                )
            }
            pennant = await rows(
                engine, "SELECT payload FROM events WHERE id = :e", e=made["pennant"]
            )
            assert "OSU" in pennant[0].payload["text"]  # the event is never rewritten
            fixed = lines.get(made["pennant"])
            assert fixed is None or "UCLA" in fixed
            lie_facts = await rows(
                engine,
                "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
                "WHERE f.story_id = :s AND p.object_id = :u AND f.amendment_part_id IS NULL",
                s=w.story_id,
                u=w.ucla,
            )
            assert lie_facts == []
    finally:
        await ts.forget(engine)
