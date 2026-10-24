"""A character's context carries its own belief state, and needs its exposures (#170;
FR-TURN-8, FR-BELIEF-5,6,9,15, FR-EPI-2,7; docs/02 §4.4). These are implementation checks of
``core.epistemic.context`` with no docs/04 part: PRIV, C-13, C-25 and S-AGE-ASSIGNMENT carry the
story's parts. Each turn is played end to end through ``respond`` with the stub adapter, and each
check reads the character render's prompt.

- **Belief state** (FR-TURN-8; #166's and #167's context clauses): in the knock world, Bobby holds
  a false belief that the delivery driver knocked (canon says George did). His context reads "you
  believe the knock knocker the delivery driver". Jenni overheard Bobby's joke, so her context
  reads "you think Bobby believes the knock knocker Marcus" and "you heard Bobby say …".
  Complement: neither line is in the other's context.
- **Lazy resolution at the need site** (#164 AC2): Jenni tells the room that Olivia sent the text.
  Bobby is neither salient nor addressed, so he gets an exposure. When he is rendered next and
  asked about Olivia and the text, his context needs the exposure. It reads "you heard Jenni say
  Olivia sent the text" and the belief the default decides. The turn's commit resolves it
  (``exposure.resolve_needed``: resolved by that turn's operation, counted in
  ``turn_report.epistemic.context_resolutions``).
- **Adjudicated** (``testimony = model`` with model salience): the same need is adjudicated. The
  model rejects, so the context and the commit hold the speech act only, with no belief that
  Olivia sent it, and the ``belief_resolved`` event says ``adjudicated``.
- **Own backstory** (#166's Discretion): Jenni sincerely says she grew up in Leeds, which canon
  left open and #144 permits as her own. She then knows it: an ``observation`` belief at
  ``known`` citing the say's beat event (``turn_report.epistemic.speech.observed``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import BeliefConfidenceLevel, OperationKind
from epistrel.core.epistemic import nested, testimony, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from tests import canon_support as c
from tests import nested_support as ns
from tests import partition_support as parts
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    beat,
    claim,
    render,
    schema_calls,
    text_of,
    turn_report,
)

pytestmark = pytest.mark.integration

NOTHING_SALIENT = json.dumps({"salient": [], "texture": []})


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _context_of(
    client: Any, epistrel: Any, story: str, player: str, who: str, say: str
) -> str:
    stub = epistrel.models.stub
    stub.calls.clear()
    stub.script("generation", [render(beat("do", "A pause."))])
    stub.script("salience_detection", [NOTHING_SALIENT])
    answer = await respond(client, story, body(player, say, render_character_ids=[who]))
    assert answer.status_code == 200, answer.text
    (call,) = schema_calls(stub, "CharacterRender")
    return text_of(call)


def _at(op: Operation) -> Position:
    return testimony.witness_position(op.narrative_turn or 0, op.turn_variant or 0, 2)


async def test_belief_state_in_the_context(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (belief state)."""
    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            w = await ns.knock_world(client, engine, "Cal", "Bobby", "Jenni")
            story = w.story_id
            cal, bobby, jenni = (w.ids[n] for n in ("Cal", "Bobby", "Jenni"))
            await ns.enable_nested(engine, story)
            settings = await ns.nested_settings(engine, story)
            await c.commit(
                engine, story, w.knocked(w.george), kind=OperationKind.CONTROL, provenance="author"
            )
            await parts.believe(engine, story, bobby, w.knocked(w.driver))

            async def joke(conn: AsyncConnection, op: Operation, event_id: str) -> Any:
                cited = transitions.Antecedent.record("events", event_id)
                await nested.write_speech_act(
                    conn,
                    op,
                    holder=jenni,
                    speaker=bobby,
                    proposition=w.knocked(w.marcus),
                    antecedent=cited,
                    position=_at(op),
                    perceived_intent="joking",
                    settings=settings,
                )
                return await nested.write_nested(
                    conn,
                    op,
                    holder=jenni,
                    attributed_holder=bobby,
                    proposition=w.knocked(w.marcus),
                    cause=nested.Cause(transitions.Kind.INFERENCE, cited),
                    position=_at(op),
                    confidence=transitions.Confidence(BeliefConfidenceLevel.SUSPECTED),
                    settings=settings,
                )

            await ns.in_operation(
                engine, story, joke, say=(bobby, "Probably Marcus again.", "joking")
            )
            asked = "Who knocked at the door, the knock just now?"
            his = await _context_of(client, epistrel, story, cal, bobby, asked)
            hers = await _context_of(client, epistrel, story, cal, jenni, asked)
    finally:
        await ts.forget(engine)
    believed = "you believe the knock knocker the delivery driver"
    thinks = "you think Bobby believes the knock knocker Marcus"
    heard = "you heard Bobby say the knock knocker Marcus"
    assert "## What you believe" in his
    assert believed in his
    assert thinks in hers
    assert heard in hers
    # Complement: each holds its own partition only.
    assert believed not in hers
    assert thinks not in his
    assert heard not in his


async def _olivia(
    client: Any, engine: AsyncEngine, testify: str
) -> tuple[str, dict[str, str], str]:
    """The room, the testimony dial, Bobby a friend of Jenni's; answers the story, the ids and
    the story predicate ``sent``."""
    story, ids, things = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
    await ts.enable_testimony(
        engine, story, testify, salience="rules" if testify != "model" else None
    )
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
    return story, ids, things["sent"]


async def _tell(client: Any, epistrel: Any, story: str, ids: dict[str, str]) -> None:
    stub = epistrel.models.stub
    stub.reset()
    stub.script(
        "generation",
        [
            render(
                beat(
                    "say",
                    ts.CLAIM_LINE,
                    intent="sincere",
                    propositions=[claim("Olivia", "sent", "the text")],
                )
            )
        ],
    )
    stub.script("salience_detection", [NOTHING_SALIENT])
    response = await respond(
        client, story, body(ids["Wren"], "What happened?", render_character_ids=[ids["Jenni"]])
    )
    assert response.status_code == 200, response.text


async def _exposure_of(engine: AsyncEngine, story: str, witness: str, sent: str) -> Any:
    """``witness``'s exposure to the sending claim (the say's detected claims are others)."""
    (found,) = await rows(
        engine,
        "SELECT x.state, x.resolution_operation_id FROM exposures x "
        "JOIN propositions p ON p.id = x.proposition_id "
        "WHERE x.story_id = :s AND x.witness_character_id = :w AND p.predicate_id = :p",
        s=story,
        w=witness,
        p=sent,
    )
    return found


def _about(found: list[Any], sent: str) -> list[Any]:
    return [b for b in found if b.predicate_id == sent]


async def _head_op(engine: AsyncEngine, story: str) -> str:
    (found,) = await rows(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND membership = 'active' ORDER BY narrative_turn DESC LIMIT 1",
        s=story,
    )
    return str(found.operation_id)


ASK = "Bobby, did Olivia send the text?"


async def test_lazy_resolution_at_the_context(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (lazy resolution at the need site)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, sent = await _olivia(client, engine, "defaulted")
        bobby = ids["Bobby"]
        await _tell(client, epistrel, story, ids)
        assert (await _exposure_of(engine, story, bobby, sent)).state == "unresolved"
        assert _about(ts.first_order(await ts.beliefs_of(engine, story, bobby)), sent) == []

        prompt = await _context_of(client, epistrel, story, ids["Wren"], bobby, ASK)
        assert "you heard Jenni say Olivia sent the text" in prompt
        assert any(
            f"- you {word} Olivia sent the text" in prompt for word in ("suspect", "believe")
        ), prompt
        resolved = await _exposure_of(engine, story, bobby, sent)
        assert resolved.state == "resolved"
        assert str(resolved.resolution_operation_id) == await _head_op(engine, story)
        report = await turn_report(engine, story)
        # Every exposure the say left on the key (its declared claim and any detected one).
        resolutions = report["epistemic"]["context_resolutions"]
        assert resolutions["defaulted"] >= 1
        assert resolutions["adjudicated"] == 0
        assert report["context"]["beliefs"] >= 2
        (held,) = _about(ts.first_order(await ts.beliefs_of(engine, story, bobby)), sent)
        assert held.cause == "testimony"


async def test_adjudicated_resolution_at_the_context(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (adjudicated)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, sent = await _olivia(client, engine, "model")
        bobby = ids["Bobby"]
        await _tell(client, epistrel, story, ids)
        assert (await _exposure_of(engine, story, bobby, sent)).state == "unresolved"
        stub = epistrel.models.stub
        stub.reset()
        stub.when("Bobby (trust", ts.adjudicated("reject", 0.95), purpose="belief_adjudication")
        prompt = await _context_of(client, epistrel, story, ids["Wren"], bobby, ASK)
        calls = [x for x in stub.calls if x.purpose == "belief_adjudication"]
        assert calls
        assert all("Bobby (trust in Jenni" in x.messages[0].content for x in calls)
        assert "you heard Jenni say Olivia sent the text" in prompt
        assert "you believe Olivia sent the text" not in prompt
        assert "you suspect Olivia sent the text" not in prompt
        assert (await _exposure_of(engine, story, bobby, sent)).state == "resolved"
        how = await rows(
            engine,
            "SELECT payload->>'resolution' AS how FROM events "
            "WHERE story_id = :s AND type = 'belief_resolved'",
            s=story,
        )
        assert how
        assert {r.how for r in how} == {"adjudicated"}
        found = _about(await ts.beliefs_of(engine, story, bobby), sent)
        assert ts.first_order(found) == []
        assert [b.outcome for b in ts.said(found)] == ["reject"]
        report = await turn_report(engine, story)
        resolutions = report["epistemic"]["context_resolutions"]
        assert resolutions["adjudicated"] >= 1
        assert resolutions["defaulted"] == 0


async def test_own_backstory_is_observed(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (own backstory)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _ = await ts.room(client, engine, "Wren", "Jenni")
        await ts.enable_testimony(engine, story, "defaulted", salience="rules")
        async with engine.begin() as conn:
            await store.add_entity(conn, story, "place", "Leeds")
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "I grew up in Leeds, you know.",
                        intent="sincere",
                        propositions=[claim("I", "grew up in", "Leeds")],
                    )
                )
            ],
        )
        response = await respond(client, story, body(ids["Wren"], "Where are you from?"))
        assert response.status_code == 200, response.text
        report = await turn_report(engine, story)
        assert report["epistemic"]["speech"]["observed"] == 1
        (known,) = ts.first_order(await ts.beliefs_of(engine, story, ids["Jenni"]))
        assert (known.cause, known.confidence_level) == ("observation", "known")
        (say_event,) = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'beat_say'",
            s=story,
        )
        assert known.antecedent_ref == {"table": "events", "id": str(say_event.id)}
        assert known.position_step >= 1
