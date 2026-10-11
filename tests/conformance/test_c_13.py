"""C-13: the request fields of a turn (FR-RESP-1, FR-AUTH-3,4) [M2] (#62).

Who acts, who is rendered, and who asked are three different fields with three meanings: the
actor may be any character of the story (who acts as whom is the consumer's decision), the
render characters are the cast members who answer, and the requesting principal is recorded for
attribution and changes nothing else. Every check runs over REST and the library (``surface``).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.orchestrator.models.stub import StubAdapter
from tests.conformance.conftest import Surface
from tests.event_support import rows
from tests.respond_support import body, generation_calls, system_of
from tests.story_support import count, story_body

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
