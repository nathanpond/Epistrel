"""S-NESTED-BELIEF: what Jenni thinks Bobby thinks (docs/04 §7; FR-BELIEF-15, FR-EPI-2,
FR-TURN-6, NFR-PRIV-1; #167).

The knock world (``tests.nested_support``): Cal (the player), Bobby and Jenni in the parlor, the
story keeping partitions, testimony ``defaulted`` and nested beliefs at depth 2. Played end to end
through ``respond`` with the stub adapter and scripted attribution claims.

- Objective: George knocked (the author's canon, ``engine``-scoped: no character reads it).
- Bobby was told, wrongly, that it was the delivery driver, and believes it (a testimony from
  Cal).
- Bobby jokes to Jenni "Probably Marcus again." She keeps that he *said* it (a ``speech_act``
  row, the joke perceived) and infers ``believes(Jenni, believes(Bobby, knocker = Marcus))``: a
  nested row in her partition, Bobby its attributed holder, an inference citing the overheard
  beat, with its three times. All three differ, each stored in its place.
- Jenni says "Bobby thinks it was Marcus": belief-consistent for her, sincere; its objective
  relation (Bobby does not believe that) is labelled on the utterance and never reaches her
  context (no Jenni prompt names Bobby's actual belief or the label).
- Jenni says "Bobby thinks it was the delivery guy": no acquisition path, belief-inconsistent: a
  hallucination, repaired, even though it is true of Bobby.
- Bobby tells Jenni "I figured it was the delivery guy": her nested belief is revised by
  testimony, the Marcus attribution closed and kept in its history.
- Jenni says "Bobby *said* it was Marcus": checked against her ``said`` row (consistent; Bobby
  did say it), stored apart from what she thinks he believes.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import BeliefKind, OperationKind
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.visibility import Visibility
from tests import canon_support as c
from tests import nested_support as ns
from tests import partition_support as parts
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, render, renders_of, text_of, turn_report

pytestmark = pytest.mark.integration

JOKE = "Probably Marcus again."
THINKS_MARCUS = "Bobby thinks it was Marcus."
THINKS_DRIVER = "Bobby thinks it was the delivery guy."
FIGURED = "I figured it was the delivery guy."
SAID_MARCUS = "Bobby said it was Marcus."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _knocked(who: str, **fields: Any) -> dict[str, Any]:
    return claim("the knock", "knocker", who, **fields)


async def _play(
    client: Any, stub: Any, w: ns.Knock, speaker: str, *beats: dict[str, Any]
) -> dict[str, Any]:
    stub.script("generation", [render(*beats)])
    response = await respond(
        client,
        w.story_id,
        body(w.ids["Cal"], "Go on.", render_character_ids=[w.ids[speaker]]),
    )
    assert response.status_code == 200, response.text
    played: dict[str, Any] = response.json()
    return played


def _speech(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [v for v in report["verify"]["verdicts"] if "speech" in v]


async def _utterance(engine: AsyncEngine, story_id: str, speaker: str) -> Any:
    found = await rows(
        engine,
        "SELECT * FROM utterances WHERE story_id = :s AND speaker_character_id = :c "
        "AND membership = 'active' ORDER BY narrative_turn DESC LIMIT 1",
        s=story_id,
        c=speaker,
    )
    return found[0]


@pytest.mark.part("M5", "FR-BELIEF-15; FR-EPI-2; FR-TURN-6; NFR-PRIV-1")
async def test_s_nested_belief(
    stub_settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    ns.deliver_nested(monkeypatch)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        w = await ns.knock_world(client, engine, "Cal", "Bobby", "Jenni")
        story = w.story_id
        cal, bobby, jenni = w.ids["Cal"], w.ids["Bobby"], w.ids["Jenni"]
        await ns.enable_nested(engine, story)
        # Objective: George knocked; no character reads it.
        await c.commit(
            engine,
            story,
            w.knocked(w.george),
            kind=OperationKind.CONTROL,
            provenance="author",
            visibility=Visibility(scope="engine"),
        )
        # Bobby was told, wrongly, that it was the delivery driver, and believes it.
        with ns.delivered():
            told = await parts.transit(
                engine,
                story,
                bobby,
                w.knocked(w.driver),
                cause=transitions.Kind.TESTIMONY,
                kind=OperationKind.CONTROL,
                source=cal,
                outcome="accept",
            )
        assert told.belief.kind is BeliefKind.FIRST_ORDER

        # -- Bobby jokes; Jenni overhears (addressed): said row + nested inference
        await _play(
            client,
            stub,
            w,
            "Bobby",
            beat(
                "say",
                JOKE,
                intent="joking",
                addressee=jenni,
                propositions=[_knocked("Marcus")],
            ),
        )
        joke_event = await scalar(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'beat_say' "
            "ORDER BY seq DESC LIMIT 1",
            s=story,
        )
        found = await ns.nested_rows(engine, story, jenni)
        (said,) = [r for r in found if r.kind == "speech_act"]
        (outer,) = [r for r in found if r.kind == "nested"]
        (inner,) = [r for r in found if r.kind == "attributed"]
        joke_utterance = await _utterance(engine, story, bobby)
        assert (said.speaker_character_id, said.perceived_intent, said.object_id) == (
            bobby,
            "joking",
            w.marcus,
        )
        assert said.utterance_id == joke_utterance.id
        assert (outer.attributed_holder_id, outer.inner_belief_id, outer.depth) == (
            bobby,
            inner.id,
            2,
        )
        assert outer.object_id == w.marcus
        assert outer.confidence_level == "suspected"  # a joke suggests, no more
        assert (outer.cause, outer.antecedent_ref) == (
            "inference",
            {"table": "events", "id": joke_event},
        )
        assert outer.held_from_turn == joke_utterance.narrative_turn
        assert outer.attributed_held_from is not None
        # All three differ, each in its place: canon (George), Bobby's (driver), Jenni's of his.
        canon = await rows(
            engine,
            "SELECT p.object_id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND p.predicate_id = :k",
            s=story,
            k=w.knocker,
        )
        assert [r.object_id for r in canon] == [w.george]
        async with engine.connect() as conn:
            bobbys = await partitions.partition(conn, story, bobby, predicate=w.knocker)
            jennis = await partitions.partition(conn, story, jenni, predicate=w.knocker)
        assert [b.proposition.object_id for b in bobbys] == [w.driver]
        assert jennis == []  # her nested belief is no first-order belief

        # -- "Bobby thinks it was Marcus": consistent for Jenni, sincere; objective false
        stub.reset()
        played = await _play(
            client,
            stub,
            w,
            "Jenni",
            beat(
                "say",
                THINKS_MARCUS,
                intent="sincere",
                propositions=[_knocked("Marcus", attribution={"holder": "Bobby"})],
            ),
        )
        report = await turn_report(engine, story)
        (verdict,) = _speech(report)
        assert verdict["speech"]["belief_relation"] == "consistent"
        assert verdict["speech"]["classification"] == "sincere"
        assert verdict["attribution"]["attributed_holder"] == bobby
        assert verdict["attribution"]["belief_ids"] == [outer.id]
        assert verdict["attribution"]["objective_relation"] == "false"
        assert "repairs" not in report
        assert "Marcus" in played["message"]["text"]
        utterance = await _utterance(engine, story, jenni)
        assert (
            utterance.belief_relation,
            utterance.classification,
            utterance.attribution_objective_relation,
        ) == ("consistent", "sincere", "false")
        assert utterance.proposition_ids == []  # she asserted no knocker of her own
        # NFR-PRIV-1: Bobby's actual belief, and the label, never reach Jenni's context.
        prompts = [text_of(call) for call in renders_of(stub)]
        assert prompts
        for prompt in prompts:
            assert "delivery" not in prompt
            assert "objective" not in prompt.casefold()
        assert "delivery" not in played["message"]["text"]

        # -- "Bobby thinks it was the delivery guy": no path: a hallucination, repaired
        before = await rows(
            engine,
            "SELECT count(*) AS n FROM beliefs WHERE story_id = :s AND holder_character_id = :h",
            s=story,
            h=jenni,
        )
        stub.reset()
        played = await _play(
            client,
            stub,
            w,
            "Jenni",
            beat(
                "say",
                THINKS_DRIVER,
                intent="sincere",
                propositions=[_knocked("the delivery guy", attribution={"holder": "Bobby"})],
            ),
        )
        report = await turn_report(engine, story)
        (verdict,) = _speech(report)
        assert verdict["speech"]["belief_relation"] == "inconsistent"
        assert verdict["speech"]["classification"] == "hallucination_repaired"
        assert verdict["speech"]["contrary_belief_ids"] == [outer.id]
        assert verdict["attribution"]["objective_relation"] == "true"  # true of Bobby, still
        (repaired,) = report["repairs"]
        assert repaired["accepted"]
        assert "delivery" not in played["message"]["text"]
        after = await rows(
            engine,
            "SELECT count(*) AS n FROM beliefs WHERE story_id = :s AND holder_character_id = :h",
            s=story,
            h=jenni,
        )
        assert after[0].n == before[0].n  # her belief set did not change

        # -- Bobby tells Jenni "I figured it was the delivery guy": revised by testimony
        stub.reset()
        await _play(
            client,
            stub,
            w,
            "Bobby",
            beat(
                "say",
                FIGURED,
                intent="sincere",
                addressee=jenni,
                propositions=[_knocked("the delivery guy", attribution={"holder": "I"})],
            ),
        )
        report = await turn_report(engine, story)
        (verdict,) = _speech(report)
        assert verdict["speech"]["classification"] == "sincere"  # his own belief, reported
        assert verdict["speech"]["belief_relation"] == "consistent"
        assert report["epistemic"]["testimony"]["nested_written"] >= 1
        found = await ns.nested_rows(engine, story, jenni)
        held = [r for r in found if r.kind == "nested" and r.tx_to is None]
        (revised,) = held
        assert (revised.object_id, revised.cause, revised.source_character_id) == (
            w.driver,
            "testimony",
            bobby,
        )
        async with engine.connect() as conn:
            old = await partitions.read_belief(conn, story, outer.id)
            history = await transitions.history(conn, story, revised.id)
        assert old is not None
        assert old.state.value == "corrected"
        assert old.corrected_by == revised.id
        assert [e.belief.id for e in history] == [outer.id, revised.id]

        # -- "Bobby *said* it was Marcus": her said row, apart from what she thinks he believes
        stub.reset()
        await _play(
            client,
            stub,
            w,
            "Jenni",
            beat(
                "say",
                SAID_MARCUS,
                intent="sincere",
                propositions=[_knocked("Marcus", attribution={"holder": "Bobby", "mode": "said"})],
            ),
        )
        report = await turn_report(engine, story)
        (verdict,) = _speech(report)
        assert verdict["attribution"]["mode"] == "said"
        assert verdict["attribution"]["belief_ids"] == [said.id]
        assert verdict["speech"]["belief_relation"] == "consistent"
        assert verdict["attribution"]["objective_relation"] == "true"  # he did say it
        assert "repairs" not in report
        utterance = await _utterance(engine, story, jenni)
        assert utterance.attribution_objective_relation == "true"
        kinds = {r.kind for r in await ns.nested_rows(engine, story, jenni)}
        assert {"speech_act", "nested", "attributed"} <= kinds
