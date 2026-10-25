"""C-13: the request fields of a turn (FR-RESP-1, FR-AUTH-3,4) [M2] (#62).

Who acts, who is rendered, and who asked are three different fields with three meanings: the
actor may be any character of the story (who acts as whom is the consumer's decision), the
render characters are the cast members who answer, and the requesting principal is recorded for
attribution and changes nothing else. Every check runs over REST and the library (``surface``).

``test_c_13__m5`` (FR-RESP-11, NFR-PRIV-4; #170; #180 adds the authority clauses in
``test_c_13__m5_authority``: Jenni's speech as actor verified against her viewpoint, a narrated
hand-over as a request or as co-author narration): with per-character partitions at perception
``full``, Jenni renders a private ``think`` and whispers to Wren (the player's character) with Bobby
in the room. The response for the player character's viewpoint carries no NPC private content (no
``think`` beat, not the thought's words). Bobby's next context
(``core.epistemic.context.build_context``) holds neither the thought nor the whispered words, only
the whisper's envelope, and none of the consumer's ``scene_context.recent_messages`` (a planted
sentinel). Complement: Jenni's own next context holds her thought, and neither context the
consumer's message.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions import store
from epistrel.core.summary import read_included_segments
from epistrel.core.turns import read_turn_inputs
from epistrel.orchestrator.jobs.handlers import lite_summary
from epistrel.orchestrator.models.stub import StubAdapter
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.concurrency_support import set_pins
from tests.conformance.conftest import Surface
from tests.event_support import rows
from tests.respond_support import body, generation_calls, respond, respond_client, system_of
from tests.story_support import WRITE, count, story_body
from tests.structured_turn_support import beat, render, schema_calls, structured_story, text_of
from tests.summary_support import run_due_jobs

PLAYER, JENNI, BOBBY = "Wren", "Jenni", "Bobby"
INPUT, REPLY = 1, 2
ROWS = (
    "SELECT (SELECT count(*) FROM operations WHERE story_id = :s)"
    " + (SELECT count(*) FROM events WHERE story_id = :s)"
    " + (SELECT count(*) FROM transcript_messages WHERE story_id = :s)"
)


async def _story(surface: Surface) -> tuple[str, dict[str, str]]:
    created = (await surface.create_story(story_body(PLAYER, JENNI, BOBBY))).json
    return created["story_id"], {c["name"]: c["id"] for c in created["characters"]}


def _stub(surface: Surface) -> StubAdapter:
    return surface.engine.models.stub


def _beat(result: dict[str, Any], beat: int) -> dict[str, Any]:
    found: list[dict[str, Any]] = [b for b in result["beats"] if b["beat"] == beat]
    assert len(found) == 1
    return found[0]


async def _turn_events(engine: AsyncEngine, story_id: str, turn: int) -> list[Any]:
    return await rows(
        engine,
        "SELECT type, character_id, principal FROM events "
        "WHERE story_id = :s AND narrative_turn = :t ORDER BY seq",
        s=story_id,
        t=turn,
    )


@pytest.mark.integration
@pytest.mark.part("M2", "FR-RESP-1; FR-AUTH-3,4")
async def test_c_13(surface: Surface, engine: AsyncEngine) -> None:
    """The player character acting with Jenni and Bobby rendered: both answer in one message.
    Jenni acting: accepted as Jenni's own speech. A ``say`` that quotes a render character is
    still the actor's speech, and the request has no field for anyone else's words. An unknown
    render character or an actor from another story: 422 with nothing written and no model
    call. The same request under two principals: the same message, each principal recorded.

    Complement asserted: after every refusal, the same story plays a valid request, so the
    zero-rows checks are watching a story that does record turns."""
    story, ids = await _story(surface)
    player, jenni, bobby = ids[PLAYER], ids[JENNI], ids[BOBBY]
    stub = _stub(surface)

    # The player character acts; Jenni and Bobby both answer, in one combined generation.
    stub.reset()
    both = (
        await surface.respond(
            story, body(player, "Who rang the bell?", render_character_ids=[jenni, bobby])
        )
    ).json
    (call,) = generation_calls(stub)
    system = system_of(call)
    assert f"Characters you voice: {JENNI}, {BOBBY}." in system
    assert f"### Author notes: {JENNI}" in system
    assert f"### Author notes: {BOBBY}" in system
    assert [b["beat"] for b in both["beats"]] == [INPUT, REPLY]
    reply = _beat(both, REPLY)
    assert reply["render_character_ids"] == [jenni, bobby]
    assert reply["text"] == both["message"]["text"]
    assert _beat(both, INPUT)["actor_character_id"] == player

    # Jenni (a character the platform plays) acts: accepted, and the input is her speech.
    jenni_turn = (await surface.respond(story, body(jenni, "I rang it."))).json
    jenni_input = _beat(jenni_turn, INPUT)
    assert (jenni_input["actor_character_id"], jenni_input["text"]) == (jenni, "I rang it.")
    assert _beat(jenni_turn, REPLY)["render_character_ids"] == [player, bobby]
    events = await _turn_events(engine, story, jenni_turn["turn"])
    assert [(e.type, e.character_id) for e in events] == [
        ("player_input", jenni),
        ("message_rendered", None),
    ]

    # Triggering a render grants no right to supply its words (FR-AUTH-3 b).
    stub.reset()
    quoted = f'{BOBBY} says: "I took the key."'
    smuggled = (
        await surface.respond(story, body(player, quoted, render_character_ids=[bobby]))
    ).json
    smuggled_input = _beat(smuggled, INPUT)
    assert (smuggled_input["actor_character_id"], smuggled_input["text"]) == (player, quoted)
    assert [b for b in smuggled["beats"] if b["actor_character_id"] == bobby] == []
    (call,) = generation_calls(stub)
    assert call.messages[-1].content == f'{PLAYER}: "{quoted}"'
    for field in ("render_say", "speech", "character_inputs"):
        extra = await surface.respond(story, {**body(player), field: {bobby: "I took the key."}})
        assert (extra.status, extra.code) == (422, "validation_error"), field

    # Unknown render character, actor from another story, malformed id: nothing written.
    _, other_ids = await _story(surface)
    stranger = other_ids[JENNI]
    before = await count(engine, ROWS, s=story)
    stub.reset()
    unknown_render = await surface.respond(
        story, body(player, render_character_ids=[jenni, stranger])
    )
    assert (unknown_render.status, unknown_render.code) == (422, "validation_error")
    assert unknown_render.details is not None
    assert unknown_render.details["field"] == "body.render_character_ids"
    assert unknown_render.details["unknown_render_ids"] == [stranger]
    outsider = await surface.respond(story, body(stranger))
    assert (outsider.status, outsider.code) == (422, "validation_error")
    assert outsider.details is not None
    assert outsider.details["field"] == "body.actor_character_id"
    assert outsider.details["unknown_actor"] == stranger
    together = await surface.respond(story, body(stranger, render_character_ids=[stranger]))
    assert together.details is not None
    assert {k: v for k, v in together.details.items() if k not in {"field", "errors"}} == {
        "unknown_actor": stranger,
        "unknown_render_ids": [stranger],
    }
    malformed = await surface.respond(story, body("jenni"))
    assert (malformed.status, malformed.code) == (422, "validation_error")
    assert malformed.details is not None
    assert malformed.details["field"] == "body.actor_character_id"
    assert await count(engine, ROWS, s=story) == before
    assert stub.calls == []
    assert (await surface.respond(story, body(player))).status == 200

    # The requesting principal is recorded and changes nothing else (FR-AUTH-4).
    answers = []
    for principal in ("p-first", "p-second"):
        fresh, fresh_ids = await _story(surface)
        request = body(
            fresh_ids[PLAYER],
            "Good morning.",
            render_character_ids=[fresh_ids[JENNI], fresh_ids[BOBBY]],
        )
        played = (await surface.respond(fresh, request, principal=principal)).json
        recorded = await _turn_events(engine, fresh, played["turn"])
        assert {e.principal for e in recorded} == {principal}
        answers.append((played["message"]["text"], played["turn"], played["timeline_revision"]))
    assert answers[0] == answers[1]


THOUGHT = "PRIVATESENTINEL she means to burn the letter tonight"
WHISPER = "WHISPERSENTINEL the key is under the blue mat"
RECENT = "RECENTSENTINEL the consumer says Bobby saw everything"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11; NFR-PRIV-4")
async def test_c_13__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, PLAYER, JENNI, BOBBY)
        await ps.perceive(engine, story, "full")
        parlor = await ps.place_entity(engine, story, "parlor")
        for name in ids:
            await ps.put(engine, story, ids[name], parlor)
        stub = epistrel.models.stub
        wren = ids[PLAYER]
        recent = {"recent_messages": [{"character_id": ids[BOBBY], "text": RECENT}]}

        stub.script(
            "generation",
            [
                render(
                    beat("think", THOUGHT),
                    beat("say", WHISPER, intent="sincere", whisper_to=[wren], addressee=wren),
                )
            ],
        )
        first = await respond(
            client,
            story,
            body(wren, "Jenni, what is it?", render_character_ids=[ids[JENNI]]),
        )
        assert first.status_code == 200, first.text
        # The player character's viewpoint: no NPC private content.
        assert [b for b in first.json()["beats"] if "think" in b["kind"]] == []
        assert "PRIVATESENTINEL" not in first.text

        async def context_of(name: str) -> str:
            stub.calls.clear()
            stub.script("generation", [render(beat("do", f"{name} waits."))])
            answer = await respond(
                client,
                story,
                body(
                    wren,
                    "Anything else?",
                    render_character_ids=[ids[name]],
                    scene_context=recent,
                ),
            )
            assert answer.status_code == 200, answer.text
            (call,) = schema_calls(stub, "CharacterRender")
            return text_of(call)

        bobby = await context_of(BOBBY)
        assert "PRIVATESENTINEL" not in bobby
        assert "WHISPERSENTINEL" not in bobby
        assert f"{JENNI} whispers something to {PLAYER}" in bobby
        assert "RECENTSENTINEL" not in bobby
        # Complement: Jenni's own context holds her thought; still no consumer message.
        jenni = await context_of(JENNI)
        assert f"(You think) {THOUGHT}" in jenni
        assert "RECENTSENTINEL" not in jenni


PINNED = "PINSENTINEL Jenni hid the letter in the clock"


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11; NFR-PRIV-4")
async def test_c_13__m5_summary(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """C-13 M5 with the rolling summary on (#544): with per-character partitions at perception
    ``full`` and ``memory.rolling_summary`` enabled (a one-message window, two-row chunks), Jenni
    whispers to Wren with Bobby in the room and the story's text pins hold a sentinel. The
    summary job's segment carries the whispered words (its input is the requester's composed
    view), but neither the segment nor the text pin reaches Bobby's render: a partitioned
    character render carries no rolling summary and no text pins (neither is viewpoint-scoped).
    The author also pins Jenni's private ``think`` memory: retrieval's pin source applies the
    viewpoint filter, so it is not in Bobby's render either. Complement: the turn's shared
    inputs, which the narrator render reads, still hold the segment and the text pin, and
    Jenni's own render holds her pinned thought."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(
            client, PLAYER, JENNI, BOBBY, settings={"lite_summary_chunk": 2}
        )
        await set_overrides(
            engine,
            story,
            ProfileOverridesPatch(
                settings={
                    "character_knowledge": {"partitions": True},
                    "perception": {"level": "full"},
                    "memory": {"rolling_summary": {"enabled": True, "window_messages": 1}},
                },
                ignore_warnings=True,
            ),
            principal="p-author",
            facts=ps.NO_FACTS,
        )
        await set_pins(engine, story, PINNED)
        parlor = await ps.place_entity(engine, story, "parlor")
        for name in ids:
            await ps.put(engine, story, ids[name], parlor)
        stub = epistrel.models.stub
        wren = ids[PLAYER]
        stub.script(
            "generation",
            [
                render(
                    beat("think", THOUGHT),
                    beat("say", WHISPER, intent="sincere", whisper_to=[wren], addressee=wren),
                )
            ],
        )
        first = await respond(
            client, story, body(wren, "Jenni, what is it?", render_character_ids=[ids[JENNI]])
        )
        assert first.status_code == 200, first.text
        # The author pins Jenni's private thought: a pin scoped to her (FR-MEM-8).
        (thought,) = await rows(
            engine,
            "SELECT id FROM memories WHERE story_id = :s AND text LIKE '%PRIVATESENTINEL%'",
            s=story,
        )
        pinned = await client.post(f"/stories/{story}/memories/{thought.id}/pin", headers=WRITE)
        assert pinned.status_code == 200, pinned.text
        for n in range(3):
            stub.script("generation", [render(beat("do", f"Jenni waits {n}."))])
            played = await respond(
                client, story, body(wren, f"Hm {n}.", render_character_ids=[ids[JENNI]])
            )
            assert played.status_code == 200, played.text
        stub.script("lite_summary", [f"Summary: Jenni told Wren {WHISPER}."] * 10)
        await lite_summary.schedule(engine, story)
        await run_due_jobs(engine, epistrel.models, story)
        async with engine.connect() as conn:
            segments = await read_included_segments(conn, story)
            inputs = await read_turn_inputs(conn, story)
        assert any("WHISPERSENTINEL" in s.label for s in segments)
        assert inputs.segments
        assert inputs.pins == (PINNED,)

        stub.calls.clear()
        stub.script("generation", [render(beat("do", "Bobby waits."))])
        answer = await respond(
            client, story, body(wren, "Anything else?", render_character_ids=[ids[BOBBY]])
        )
        assert answer.status_code == 200, answer.text
        (call,) = schema_calls(stub, "CharacterRender")
        bobby = text_of(call)
        assert "WHISPERSENTINEL" not in bobby
        assert "PINSENTINEL" not in bobby
        assert "PRIVATESENTINEL" not in bobby
        assert "## Earlier in the story" not in bobby
        # Complement: Jenni's own render holds her pinned thought.
        stub.calls.clear()
        stub.script("generation", [render(beat("do", "Jenni waits."))])
        answer = await respond(
            client, story, body(wren, "And you?", render_character_ids=[ids[JENNI]])
        )
        assert answer.status_code == 200, answer.text
        (call,) = schema_calls(stub, "CharacterRender")
        assert "PRIVATESENTINEL" in text_of(call)


# -- the M5 authority clauses (#180) ---------------------------------------------------------------

HANDS = "Jenni smiles and hands me the key."


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11")
async def test_c_13__m5_authority(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """C-13 M5's authority clauses (FR-RESP-11, FR-AUTH-3; #180), with per-character partitions
    at perception ``full`` and everyone in the parlor. Jenni as actor: her input is her own
    speech and action, checked in her view like her render (#57's actor rule): her ``do`` naming
    Bobby's hand-over is not player authority (no ``player_authority`` report) but her render's
    rule, denied ``other_character_secret`` to her (asserter ``character:<Jenni>``). The player's
    input narrating Jenni handing over the key: under ``own_character`` a request in Jenni's
    render, never fact (nothing about the key canonized); under ``co_author`` accepted narration,
    Jenni's ``beat_do`` asserted ``co_author`` and checked in her own view (permitted
    ``co_author``, canonized with provenance ``player``). The player character's viewpoint
    receives no NPC private content (no ``think`` beat, not the thought's words)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, PLAYER, JENNI, BOBBY)
        await ps.perceive(engine, story, "full")
        parlor = await ps.place_entity(engine, story, "parlor")
        for name in ids:
            await ps.put(engine, story, ids[name], parlor)
        async with engine.begin() as conn:
            key = await store.add_entity(conn, story, "item", "the key")
        stub = epistrel.models.stub
        wren, jenni, bobby = ids[PLAYER], ids[JENNI], ids[BOBBY]
        key_facts = (
            "SELECT f.provenance::text AS provenance FROM facts f JOIN propositions p "
            "ON p.id = f.proposition_id WHERE f.story_id = :s "
            "AND (p.subject_id = :k OR p.object_id = :k)"
        )

        async def report() -> dict[str, Any]:
            found = await rows(
                engine,
                "SELECT turn_report FROM operations WHERE story_id = :s AND kind = 'turn' "
                "AND status = 'committed' ORDER BY narrative_turn DESC LIMIT 1",
                s=story,
            )
            value: dict[str, Any] = found[0].turn_report
            return value

        # Jenni as actor: her own speech and action, checked like her render.
        stub.script("generation", [render(beat("say", "Thanks, Jenni.", intent="sincere"))])
        answer = await respond(
            client,
            story,
            body(jenni, "Here.", do="Bobby hands me the key.", render_character_ids=[bobby]),
        )
        assert answer.status_code == 200, answer.text
        found = await report()
        assert "player_authority" not in found
        assert {(d["asserter"], d["reason"]) for d in found["verify"]["denied"]} == {
            (f"character:{jenni}", "other_character_secret")
        }

        # own_character: a request in Jenni's render, never fact.
        stub.reset()
        stub.script("generation", [render(beat("say", "Maybe later."))])
        answer = await respond(client, story, body(wren, None, do=HANDS))
        assert answer.status_code == 200, answer.text
        assert [b for b in answer.json()["beats"] if "think" in b["kind"]] == []
        (call,) = schema_calls(stub, "CharacterRender")
        assert "Jenni smiles and hands Wren the key." in text_of(call)
        found = await report()
        assert found["player_authority"]["requests"][0]["outcome"] == "refused"
        assert await rows(engine, key_facts, s=story, k=key) == []

        # co_author: accepted narration, checked in Jenni's own view.
        patched = await client.patch(
            f"/stories/{story}/settings",
            json={"player_authority": "co_author"},
            headers=WRITE,
        )
        assert patched.status_code == 200, patched.text
        stub.reset()
        stub.script("generation", [render(beat("think", THOUGHT), beat("say", "There."))])
        answer = await respond(client, story, body(wren, None, do=HANDS))
        assert answer.status_code == 200, answer.text
        assert [b for b in answer.json()["beats"] if "think" in b["kind"]] == []
        assert "PRIVATESENTINEL" not in answer.text
        found = await report()
        assert found["player_authority"]["narrated"] == [
            {"beat": 2, "character_id": jenni, "kind": "do"}
        ]
        assert "permitted:co_author" in {v["permission"] for v in found["verify"]["verdicts"]}
        assert {r.provenance for r in await rows(engine, key_facts, s=story, k=key)} == {"player"}
