"""S-WHISPER: presence is not perception (docs/04 §7; FR-TURN-2,9,10,13, NFR-PRIV-3; #161;
the player-output half and the working-memory clause, FR-RESP-4,8,12,13, FR-MULTI-4, FR-MEM-11;
#177).

The partition and routing half: with ``full`` perception
routing, Jenni (the rendered character) whispers to Marcus with Bobby co-located in the parlor.
Played end to end through ``respond`` with the stub adapter; the routed memories (one per
perceiver, ``core.memory.records.write_perceived``) are the partitions' episodic content.

- Bobby's partition holds the envelope at full fidelity ("Jenni whispers something to Marcus",
  and her visible action) and none of the words; Marcus's holds the words.
- A foreign language: Bobby, who understands only English, holds the envelope; Marcus the words.
- An aside with no addressee: the room holds "Jenni says something, under her breath".
- A closed door: Bobby in the hall gets no write at all, except of a sound ("You hear a knock
  from the parlor").
- An explicit eavesdrop through the closed door: Bobby perceives, in full or, when the render
  limits it, only the words he caught. With no plausible path (the study adjoins nothing) the
  qualifier is rejected and repaired: no write, a ``render_rejected`` audit record, and the
  repair on ``turn_report.perception``.
- A remote phone call: Clara, in another city, on the call, perceives the words; the room gets
  "Jenni speaks into the phone".
- Security: a roster (``scene_context.characters_present``) falsely listing the absent Clara
  and omitting the present Bobby grants Clara nothing, and Bobby still perceives.
- Co-location only: at ``co_location`` the same whisper writes the words to Bobby too.

``test_s_whisper__inference`` (FR-BELIEF-11; #165) plays the whisper under per-character
partitions with testimony at ``defaulted``: Bobby's envelope is an observation that Jenni spoke to
Marcus and no path to the words (``core.epistemic.replay.could_have_known`` finds ``NoPath``),
while Marcus, addressed, believes them by testimony (``Known``). The envelope observation is a
valid antecedent for Bobby's low-confidence inference that they are hiding something (written
through #160), which still licenses nothing about the words: when Bobby then claims the words,
the claim is caught as a hallucination and repaired (#166). Complement: once Marcus tells Bobby
the words, the same claim has a path (the exposure, dry-run; then the testimony it resolves
to).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import BeliefConfidenceLevel
from epistrel.core.epistemic import replay, transitions
from epistrel.core.events.types import BeatSay, RenderRejected
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import SPOKE_TO
from epistrel.core.views import audit_view
from tests import exposure_support as xs
from tests import partition_support as parts
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.canon_support import auto_predicate
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    beat,
    claim,
    history,
    render,
    renders_of,
    structured_story,
    text_of,
    turn_report,
)

pytestmark = pytest.mark.integration

WORDS = "The key is under the blue mat."
LEAN = "She leans in close and lowers her voice."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _room(
    client: Any, engine: AsyncEngine, level: str = "full"
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Wren (the player), Jenni, Marcus, Bobby and Clara; the parlor, the hall (through a
    closed door), the study (adjoining nothing) and another city. Everyone but Clara is in the
    parlor; Clara is in the city."""
    story, ids = await structured_story(client, "Wren", "Jenni", "Marcus", "Bobby", "Clara")
    await ps.perceive(engine, story, level)
    places = {
        name: await ps.place_entity(engine, story, name)
        for name in ("parlor", "hall", "study", "city")
    }
    await ps.adjoin(engine, story, places["parlor"], places["hall"], "door_closed")
    for name in ("Wren", "Jenni", "Marcus", "Bobby"):
        await ps.put(engine, story, ids[name], places["parlor"])
    await ps.put(engine, story, ids["Clara"], places["city"])
    return story, ids, places


def _whisper(ids: dict[str, str], **qualifiers: Any) -> str:
    fields = {"whisper_to": [ids["Marcus"]], "addressee": ids["Marcus"], **qualifiers}
    return render(beat("say", WORDS, intent="sincere", **fields), beat("do", LEAN))


async def _play(
    client: Any, epistrel: Any, story: str, ids: dict[str, str], answer: str, **kw: Any
) -> None:
    epistrel.models.stub.script("generation", [answer])
    response = await respond(client, story, body(ids["Wren"], "What's going on?", **kw))
    assert response.status_code == 200, response.text


def _texts(found: list[Any]) -> list[tuple[str, str, str]]:
    return [(m.text, m.fidelity, m.channel) for m in found if m.narrative_turn is not None]


@pytest.mark.part("M5", "FR-TURN-2,9,10,13; NFR-PRIV-3")
async def test_s_whisper__routing(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    perception_delivered: None,
) -> None:
    """See the module docstring. Complement throughout: the words never reach a partition
    routing did not grant them to."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        # The whisper: Bobby holds the envelope and Jenni's visible action, never the words.
        story, ids, places = await _room(client, engine)
        await _play(client, epistrel, story, ids, _whisper(ids))
        bobby = _texts(await ps.memories_of(engine, story, ids["Bobby"]))
        assert ("Jenni whispers something to Marcus", "envelope", "room") in bobby
        assert (f"Jenni does: {LEAN}", "content", "room") in bobby
        assert not [t for t, *_ in bobby if "blue mat" in t]
        marcus = _texts(await ps.memories_of(engine, story, ids["Marcus"]))
        assert (f"Jenni says: {WORDS}", "content", "room") in marcus
        jenni = _texts(await ps.memories_of(engine, story, ids["Jenni"]))
        assert (f"Jenni says: {WORDS}", "content", "self") in jenni
        # Each routed memory is its holder's alone; Clara, elsewhere, got nothing.
        for m in await ps.memories_of(engine, story, ids["Bobby"]):
            assert m.visibility == {"scope": "characters", "characters": [ids["Bobby"]]}
        assert _texts(await ps.memories_of(engine, story, ids["Clara"])) == []
        # The player's character perceives the room like anyone (memories, never beliefs).
        assert ("Jenni whispers something to Marcus", "envelope", "room") in _texts(
            await ps.memories_of(engine, story, ids["Wren"])
        )

        # A foreign language: the envelope for Bobby, the words for Marcus.
        story, ids, places = await _room(client, engine)
        await ps.speaks(engine, ids["Bobby"], "en")
        answer = render(
            beat("say", WORDS, intent="sincere", addressee=ids["Marcus"], language="fr")
        )
        await _play(client, epistrel, story, ids, answer)
        bobby = _texts(await ps.memories_of(engine, story, ids["Bobby"]))
        assert (
            "Jenni says something to Marcus in a language you don't understand",
            "envelope",
            "room",
        ) in bobby
        assert not [t for t, *_ in bobby if "blue mat" in t]
        assert (f"Jenni says: {WORDS}", "content", "room") in _texts(
            await ps.memories_of(engine, story, ids["Marcus"])
        )

        # An aside with no addressee: the room gets "<Speaker> says something, <manner>".
        story, ids, places = await _room(client, engine)
        answer = render(beat("say", WORDS, intent="sincere", aside=True, manner="under her breath"))
        await _play(client, epistrel, story, ids, answer)
        for name in ("Bobby", "Marcus"):
            assert ("Jenni says something, under her breath", "envelope", "room") in _texts(
                await ps.memories_of(engine, story, ids[name])
            )

        # A closed door: Bobby in the hall gets no write at all.
        story, ids, places = await _room(client, engine)
        await ps.put(engine, story, ids["Bobby"], places["hall"])
        await _play(client, epistrel, story, ids, _whisper(ids))
        assert _texts(await ps.memories_of(engine, story, ids["Bobby"])) == []

        # ... except a sound: a knock crosses the closed door as an envelope.
        story, ids, places = await _room(client, engine)
        await ps.put(engine, story, ids["Bobby"], places["hall"])
        await _play(client, epistrel, story, ids, render(beat("do", "She knocks twice.")))
        assert ("You hear a knock from the parlor", "envelope", "adjacent") in _texts(
            await ps.memories_of(engine, story, ids["Bobby"])
        )

        # An explicit eavesdrop through the closed door: Bobby catches the words.
        story, ids, places = await _room(client, engine)
        await ps.put(engine, story, ids["Bobby"], places["hall"])
        await _play(client, epistrel, story, ids, _whisper(ids, eavesdrop_by=[ids["Bobby"]]))
        assert _texts(await ps.memories_of(engine, story, ids["Bobby"])) == [
            (f"Jenni says: {WORDS}", "content", "eavesdrop")
        ]
        # ... or only the part the render says he caught.
        story, ids, places = await _room(client, engine)
        await ps.put(engine, story, ids["Bobby"], places["hall"])
        answer = _whisper(ids, eavesdrop_by=[ids["Bobby"]], partial_text="under the blue")
        await _play(client, epistrel, story, ids, answer)
        assert _texts(await ps.memories_of(engine, story, ids["Bobby"])) == [
            ("Jenni says (you catch only part of it): under the blue", "content", "eavesdrop")
        ]

        # No plausible path: rejected and repaired (rung 1), the beat routed as a whisper.
        story, ids, places = await _room(client, engine)
        await ps.put(engine, story, ids["Bobby"], places["study"])
        await _play(client, epistrel, story, ids, _whisper(ids, eavesdrop_by=[ids["Bobby"]]))
        assert _texts(await ps.memories_of(engine, story, ids["Bobby"])) == []
        report = await turn_report(engine, story)
        assert report["perception"]["level"] == "full"
        assert report["perception"]["qualifiers_repaired"] == [
            {"beat": 2, "kind": "eavesdrop_by", "reason": "no_path", "character_id": ids["Bobby"]}
        ]
        audit = await audit_view(engine, story)
        rejected = [e.payload for e in audit if isinstance(e.payload, RenderRejected)]
        assert [(r.rung, r.text) for r in rejected] == [(1, WORDS)]
        assert [c["kind"] for r in rejected for c in r.constraints] == ["perception_qualifier"]
        assert [c["qualifier"] for r in rejected for c in r.constraints] == ["eavesdrop_by"]
        (said,) = [e.payload for e in audit if isinstance(e.payload, BeatSay)]
        assert said.repaired_from is not None
        assert said.perception is not None
        assert "eavesdrop_by" not in said.perception
        assert said.perception["whisper_to"] == [ids["Marcus"]]

        # A remote call: Clara, in the city and on the call, hears the words; the room gets
        # the phone envelope.
        story, ids, places = await _room(client, engine)
        for name in ("Jenni", "Clara"):
            await ps.tune_in(engine, story, ids[name], "call-1")
        answer = render(
            beat("say", WORDS, intent="sincere", remote_channel={"channel_id": "call-1"})
        )
        await _play(client, epistrel, story, ids, answer)
        assert (f"Jenni says: {WORDS}", "content", "remote") in _texts(
            await ps.memories_of(engine, story, ids["Clara"])
        )
        assert ("Jenni speaks into the phone", "envelope", "room") in _texts(
            await ps.memories_of(engine, story, ids["Bobby"])
        )

        # A call Jenni places in this very turn: her ``do`` declares it, and her next beat
        # speaks on it (the declaration is honored before it is canon).
        story, ids, places = await _room(client, engine)
        await ps.tune_in(engine, story, ids["Clara"], "call-2")
        answer = render(
            beat(
                "do",
                "She dials a number and lifts the phone.",
                propositions=[claim("Jenni", "on_channel", "call-2")],
            ),
            beat("say", WORDS, intent="sincere", remote_channel={"channel_id": "call-2"}),
        )
        await _play(client, epistrel, story, ids, answer)
        report = await turn_report(engine, story)
        assert report["perception"]["qualifiers_repaired"] == []
        assert (f"Jenni says: {WORDS}", "content", "remote") in _texts(
            await ps.memories_of(engine, story, ids["Clara"])
        )

        # Security: a roster naming the absent Clara and omitting the present Bobby.
        story, ids, places = await _room(client, engine)
        roster = {"characters_present": [ids["Wren"], ids["Jenni"], ids["Marcus"], ids["Clara"]]}
        answer = render(beat("say", WORDS, intent="sincere"))
        await _play(client, epistrel, story, ids, answer, scene_context=roster)
        assert _texts(await ps.memories_of(engine, story, ids["Clara"])) == []
        assert (f"Jenni says: {WORDS}", "content", "room") in _texts(
            await ps.memories_of(engine, story, ids["Bobby"])
        )

        # Co-location only: the whisper reaches the whole room, words included.
        story, ids, places = await _room(client, engine, "co_location")
        await _play(client, epistrel, story, ids, _whisper(ids))
        bobby = _texts(await ps.memories_of(engine, story, ids["Bobby"]))
        assert (f"Jenni says: {WORDS}", "content", "room") in bobby
        assert not [f for _, f, _ in bobby if f == "envelope"]


async def _knows(
    engine: AsyncEngine, story: str, who: str, p: Proposition, at: Position
) -> replay.Answer:
    async with engine.connect() as conn:
        return await replay.could_have_known(conn, story, who, p, at)


@pytest.mark.part("M5", "FR-BELIEF-11")
async def test_s_whisper__inference(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """See the module docstring (the inference clause)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, things = await ts.room(client, engine, "Wren", "Jenni", "Marcus", "Bobby")
        await ts.enable_testimony(engine, story, "defaulted")
        words = ts.sent(things)
        answer = render(
            beat(
                "say",
                ts.CLAIM_LINE,
                intent="sincere",
                whisper_to=[ids["Marcus"]],
                addressee=ids["Marcus"],
                propositions=[claim("Olivia", "sent", "the text")],
            )
        )
        await _play(client, epistrel, story, ids, answer)
        report = await turn_report(engine, story)
        assert report["epistemic"]["could_have_known"] == {"checks": 0, "no_path": 0}
        now = Position.end_of(1)

        # Bobby observed the envelope (Jenni spoke to Marcus), never the words.
        (envelope,) = await rows(
            engine,
            "SELECT b.id, t.kind FROM beliefs b JOIN propositions p ON p.id = b.proposition_id "
            "JOIN belief_transitions t ON t.belief_id = b.id WHERE b.story_id = :s "
            "AND b.holder_character_id = :h AND p.predicate_id = :q",
            s=story,
            h=ids["Bobby"],
            q=SPOKE_TO,
        )
        assert str(envelope.kind) == "observation"
        assert await _knows(engine, story, ids["Bobby"], words, now) == replay.NoPath()
        heard = await _knows(engine, story, ids["Marcus"], words, now)
        assert isinstance(heard, replay.Known)
        assert heard.path.transition_kind == "testimony"

        # The envelope seeds a low-confidence inference: they're hiding something.
        hiding = Proposition(
            subject_id=ids["Jenni"],
            predicate_id=await auto_predicate(engine, story, "hiding_something_from"),
            object_id=ids["Bobby"],
            valid_time=Interval.of(None, None),
        )
        inferred = await parts.transit(
            engine,
            story,
            ids["Bobby"],
            hiding,
            cause=transitions.Kind.INFERENCE,
            antecedent=transitions.Antecedent.record("beliefs", envelope.id),
            confidence=transitions.Confidence(BeliefConfidenceLevel.SUSPECTED),
        )
        assert inferred.belief.confidence_level is BeliefConfidenceLevel.SUSPECTED
        later = Position.end_of(inferred.belief.held_from.turn)
        assert isinstance(await _knows(engine, story, ids["Bobby"], hiding, later), replay.Known)
        assert await _knows(engine, story, ids["Bobby"], words, later) == replay.NoPath()

        # Bobby claims to know the words: no path, so the claim is a hallucination, repaired
        # (#166's rule), and he still holds nothing about them.
        blurt = render(
            beat(
                "say",
                ts.CLAIM_LINE,
                intent="sincere",
                propositions=[claim("Olivia", "sent", "the text")],
            )
        )
        await _play(client, epistrel, story, ids, blurt, render_character_ids=[ids["Bobby"]])
        report = await turn_report(engine, story)
        caught = [v["speech"] for v in report["verify"]["verdicts"] if "speech" in v]
        assert caught  # the declared claim, and the detector's reading of "sent"
        for found in caught:
            assert (found["classification"], found["licence"]) == ("hallucination_repaired", None)
            assert found["hidden"] == "supported"  # Marcus holds the words: withheld content
        assert {r["tag"] for r in report["repairs"]} == {"hallucination"}
        assert await _knows(engine, story, ids["Bobby"], words, Position.end_of(3)) == (
            replay.NoPath()
        )

        # Complement: Marcus tells Bobby the words. The hearing is a path, and so is the
        # testimony it resolves to.
        exposure_id, _ = await xs.expose(engine, story, ids["Bobby"], words, source=ids["Marcus"])
        (told,) = await rows(engine, "SELECT * FROM exposures WHERE id = :e", e=exposure_id)
        after = Position.end_of(told.position_turn)
        found = await _knows(engine, story, ids["Bobby"], words, after)
        assert isinstance(found, replay.Known)
        assert (found.path.kind, found.path.ref_id) == ("exposure", exposure_id)
        await xs.resolve(engine, story, exposure_id)
        found = await _knows(engine, story, ids["Bobby"], words, Position.end_of(after.turn + 1))
        assert isinstance(found, replay.Known)
        assert found.path.transition_kind == "testimony"
        # ... and only from then: at the whisper, still none.
        assert isinstance(await _knows(engine, story, ids["Bobby"], words, now), replay.NoPath)


THOUGHT = "S-WHISPER-THOUGHT he must not find out."
ENVELOPE = "Jenni whispers something to Marcus"
RECENT = "S-WHISPER-RECENT Jenni whispered: the key is under the blue mat."


def _vp(character_id: str) -> str:
    return f"character:{character_id}"


async def _seen(
    client: Any, epistrel: Any, story: str, ids: dict[str, str], viewer: str, **kw: Any
) -> dict[str, Any]:
    """The whisper (with Jenni's thought) played for ``viewer``'s viewpoint."""
    epistrel.models.stub.script(
        "generation",
        [
            render(
                beat("think", THOUGHT),
                beat("say", WORDS, intent="sincere", whisper_to=[ids["Marcus"]]),
                beat("do", LEAN),
            )
        ],
    )
    response = await respond(
        client, story, body(ids["Wren"], "What's going on?", output_viewpoint=viewer, **kw)
    )
    assert response.status_code == 200, response.text
    played: dict[str, Any] = response.json()
    return played


def _beats(played: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    return [(b["channel"], b["text"], b.get("fidelity")) for b in played["beats"]]


@pytest.mark.part("M5", "FR-RESP-4,8,12,13; FR-MULTI-4; FR-MEM-11")
async def test_s_whisper__output(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    perception_delivered: None,
) -> None:
    """See the module docstring (the player-output half and the working-memory clause).
    Complement throughout: the words reach exactly the viewpoints that perceived them."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        # Bobby's viewpoint: the envelope in both the narrative and the beat list.
        story, ids, places = await _room(client, engine)
        played = await _seen(client, epistrel, story, ids, _vp(ids["Bobby"]))
        assert played["audience_filter"] == "full"
        assert played["player_view"] == "character"
        assert (_vp(ids["Bobby"])) == played["output_viewpoint"]
        assert ("say", ENVELOPE, "envelope") in _beats(played)
        assert ("do", LEAN, "content") in _beats(played)
        assert ENVELOPE in played["message"]["text"]
        assert played["message"]["narrative"] == played["message"]["text"]
        assert "blue mat" not in str(played)
        assert "S-WHISPER-THOUGHT" not in str(played)
        (whisper,) = [b for b in played["beats"] if b["text"] == ENVELOPE]
        assert whisper["propositions"] == []
        assert "labels" not in whisper
        report = await turn_report(engine, story)
        assert report["audience"]["audience_filter"] == "full"
        assert report["audience"]["withheld"] == [3]
        # ... and so does Bobby's history read, the message recomposed for nobody else.
        items = await history(client, story, _vp(ids["Bobby"]))
        assert ("say", ENVELOPE) in [(i["channel"], i["text"]) for i in items]
        assert "blue mat" not in str(items)
        # Marcus's viewpoint: the words.
        marcus = await history(client, story, _vp(ids["Marcus"]))
        assert ("say", WORDS, "content") in [
            (i["channel"], i["text"], i.get("fidelity")) for i in marcus
        ]
        (message,) = [i["text"] for i in marcus if i["channel"] == "message"]
        assert WORDS in message
        assert "S-WHISPER-THOUGHT" not in str(marcus)

        # ``omniscient``, named: every beat in full, the thought labelled.
        story, ids, places = await _room(client, engine)
        seen = await _seen(client, epistrel, story, ids, "omniscient")
        assert seen["audience_filter"] == "none"
        assert ("say", WORDS, "content") in _beats(seen)
        assert ("think", THOUGHT, "content") in _beats(seen)
        assert f"[Jenni thinks] {THOUGHT}" in seen["message"]["text"]
        # ... and Bobby's read of the same turn never.
        bobby = await history(client, story, _vp(ids["Bobby"]))
        assert "blue mat" not in str(bobby)
        assert "S-WHISPER-THOUGHT" not in str(bobby)
        # No resolvable viewpoint is refused.
        refused = await respond(
            client, story, body(ids["Wren"], "Hm?", output_viewpoint="character:nobody")
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "viewpoint_unresolved"

        # The player's character in the hall: the beat is absent (only the own input remains).
        story, ids, places = await _room(client, engine)
        await ps.put(engine, story, ids["Wren"], places["hall"])
        away = await _seen(client, epistrel, story, ids, _vp(ids["Wren"]))
        assert [b for b in away["beats"] if b["actor_character_id"] == ids["Jenni"]] == []
        assert "blue mat" not in str(away)
        assert away["message"]["text"] == ""
        # A viewpoint that perceived nothing at all: an empty narrative, said so.
        clara = await history(client, story, _vp(ids["Clara"]))
        assert [i for i in clara if i["channel"] != "message"] == []
        played = await _seen(client, epistrel, story, ids, _vp(ids["Clara"]))
        assert played["audience_filter"] == "nothing_perceived"
        assert played["message"]["text"] == ""
        assert [b["channel"] for b in played["beats"]] == ["message"]

        # Co-location only: Bobby's viewpoint shows the words.
        story, ids, places = await _room(client, engine, "co_location")
        close = await _seen(client, epistrel, story, ids, _vp(ids["Bobby"]))
        assert close["audience_filter"] == "co_location"
        assert ("say", WORDS, "content") in _beats(close)
        assert WORDS in close["message"]["text"]

        # The player's own whisper (#161's ``whisper_to``): Marcus hears it, Bobby gets the
        # envelope; Clara, not in the room, is dropped silently.
        story, ids, places = await _room(client, engine)
        epistrel.models.stub.script("generation", [render(beat("do", LEAN))])
        whispered = await respond(
            client,
            story,
            {
                "actor_character_id": ids["Wren"],
                "player_input": {
                    "say": WORDS,
                    "whisper_to": [ids["Marcus"], ids["Clara"]],
                },
                "output_viewpoint": _vp(ids["Bobby"]),
            },
        )
        assert whispered.status_code == 200, whispered.text
        assert ("say", "Wren whispers something to Marcus", "envelope") in _beats(whispered.json())
        report = await turn_report(engine, story)
        assert {"beat": 1, "kind": "whisper_to", "reason": "target_absent"} | {
            "character_id": ids["Clara"]
        } in report["perception"]["qualifiers_repaired"]
        assert (f"Wren says: {WORDS}", "content", "room") in _texts(
            await ps.memories_of(engine, story, ids["Marcus"])
        )
        assert ("Wren whispers something to Marcus", "envelope", "room") in _texts(
            await ps.memories_of(engine, story, ids["Bobby"])
        )

        # Working memory: the consumer's recent messages carry the words, yet Bobby's next
        # context holds only the envelope and none of them; Marcus's holds the words.
        story, ids, places = await _room(client, engine)
        await _seen(client, epistrel, story, ids, _vp(ids["Wren"]))
        recent = {"recent_messages": [{"character_id": ids["Jenni"], "text": RECENT}]}
        for who in ("Bobby", "Marcus"):
            stub = epistrel.models.stub
            before = len(renders_of(stub))
            stub.script("generation", [render(beat("say", "Hm.", intent="sincere"))])
            response = await respond(
                client,
                story,
                body(
                    ids["Wren"],
                    "Anything to add?",
                    render_character_ids=[ids[who]],
                    scene_context=recent,
                ),
            )
            assert response.status_code == 200, response.text
            (call,) = renders_of(stub)[before:]
            prompt = text_of(call)
            assert "S-WHISPER-RECENT" not in prompt
            if who == "Bobby":
                assert ENVELOPE in prompt
                assert "blue mat" not in prompt
            else:
                assert WORDS in prompt
