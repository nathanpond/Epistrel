"""S-SEQUENTIAL: ordered micro-events (docs/04 §7; FR-MULTI-5,6,11; #174).

The key room (``tests.fixtures.key_room``): Cal (the player), Bobby and Jenni in the study, a
structured story with per-character renders. Cal asks; the request names Bobby then Jenni
(``render_character_ids``), and the story was created without ``interaction_mode``, so the turn
is ``sequential``. Played end to end through ``respond`` with the stub adapter, each primary's
render scripted by its name in the prompt (``tests.multi_support.renders_for``):

- Bobby asks Jenni a question; Jenni renders second and her prompt's ``## Just now`` holds his
  verified beat, so her answer answers it (she saw his beat); her prompt never holds his
  ``think``;
- Bobby pockets the brass key (a ``do`` declaring ``holds(Bobby, the key)``); Jenni then tries
  to take it and lock it in the drawer (``holds(Jenni, the key)``): the staged ``holds`` fact
  (the same-turn overlay) contradicts her ``do``, so her beat is repaired alone and renders as
  a failed attempt (the regeneration rung), while Bobby's beat, verified before she rendered,
  stands unchanged: one ``render_rejected`` for her beat only, no repair of his;
- all beats commit as one turn: one operation, one ``message_rendered`` naming both in order,
  ``turn_report.multi`` (mode, order, staged, regenerated beats); regenerating the message
  regenerates both;
- order: the request's by default; ``sequence_order = director`` asks the director once
  (``director_order``) and its order is used; an answer naming someone else falls back to the
  request's order with ``director_order_skipped``;
- a story whose scenario's ``interaction_mode`` genesis kept inactive (a pre-M5 story) reads it
  from ``stories.settings.inactive``: ``simultaneous`` there renders the primaries against one
  starting state (#175's mode) until a request overrides it to ``sequential``;
- with partitions off (shared knowledge) the turn still renders both, and Jenni's context holds
  Bobby's ``say`` but not his ``think`` or ``latent``;
- under ``combined`` renders two named primaries are 422 and an override is 409 ``module_off``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, structured_story, turn_report

pytestmark = pytest.mark.integration

QUESTION = "Jenni, did you leave the lamp on in the hall?"
ANSWER = "Yes, Bobby, I left it on for Cal."
BOBBY_THINKS = "Bobby wonders whether Jenni hid the ledger."
POCKETS = "Bobby slips the brass key into his pocket."
LOCKS = "Jenni takes the brass key and locks it in the desk drawer."
ATTEMPT = "Jenni reaches for the brass key, but it is already gone."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _ask(room: KeyRoom, **fields: Any) -> dict[str, Any]:
    return body(room.cal, "What now?", render_character_ids=[room.bobby, room.jenni], **fields)


async def _play(client: Any, room: KeyRoom, **fields: Any) -> dict[str, Any]:
    answer = await respond(client, room.story_id, _ask(room, **fields))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


async def _beat_events(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT e.id, e.operation_id, e.type, e.beat, e.character_id, e.payload->>'text' AS text "
        "FROM events e JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND o.kind = 'turn' AND o.status = 'committed' "
        "AND e.type LIKE 'beat_%' ORDER BY e.seq",
        s=story,
    )


@pytest.mark.part("M5", "FR-MULTI-5,6,11")
async def test_s_sequential(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    multi_delivered: None,
) -> None:
    """See the module docstring (the question, the key, one turn, regeneration)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- Jenni sees Bobby's verified beat, never his think.
        room = await key_room(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(beat("think", BOBBY_THINKS), beat("say", QUESTION, intent="sincere")),
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("say", ANSWER, intent="sincere")))
        played = await _play(client, room)
        assert played["order"] == [room.bobby, room.jenni]
        (jenni_prompt,) = ms.render_prompts(stub, "Jenni")
        assert "## Just now" in jenni_prompt
        assert f"Bobby says: {QUESTION}" in jenni_prompt
        assert BOBBY_THINKS not in jenni_prompt
        # Bobby rendered first and saw nothing of Jenni's.
        (bobby_prompt,) = ms.render_prompts(stub, "Bobby")
        assert "## Just now" not in bobby_prompt
        assert ANSWER not in bobby_prompt
        assert ms.call_index(stub, "generation", "## You are Bobby\n") < ms.call_index(
            stub, "generation", "## You are Jenni\n"
        )
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["mode"] == "sequential"  # created without interaction_mode
        assert report["multi"]["order"] == [room.bobby, room.jenni]
        assert report["multi"]["order_source"] == "request"
        assert report["multi"]["staged"] == 2  # Bobby's think and say, staged before Jenni
        events = await _beat_events(engine, room.story_id)
        assert len({e.operation_id for e in events}) == 1  # one turn
        speakers = [(e.type, e.character_id) for e in events]
        assert speakers == [
            ("beat_think", room.bobby),
            ("beat_say", room.bobby),
            ("beat_say", room.jenni),
        ]
        assert [b for b in played["beats"] if b["kind"] == "message_rendered"]
        (message,) = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=room.story_id,
        )
        assert message.payload["render_character_ids"] == [room.bobby, room.jenni]

        # -- Bobby pockets the key; Jenni's lock attempt fails, and only her beat is redone.
        room = await key_room(client, engine)
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(beat("do", POCKETS, propositions=[claim("Bobby", "holds", "the brass key")])),
        )
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(beat("do", LOCKS, propositions=[claim("Jenni", "holds", "the brass key")])),
        )
        stub.script("regeneration", [json.dumps({"text": ATTEMPT, "propositions": []})])
        played = await _play(client, room)
        (jenni_prompt,) = ms.render_prompts(stub, "Jenni")
        assert f"Bobby does: {POCKETS}" in jenni_prompt  # the staged holds fact, perceived
        events = await _beat_events(engine, room.story_id)
        texts = {(e.character_id, e.type): e.text for e in events}
        assert texts[(room.bobby, "beat_do")] == POCKETS  # verified before Jenni rendered
        assert texts[(room.jenni, "beat_do")] == ATTEMPT  # her failed attempt
        rejected = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'render_rejected'",
            s=room.story_id,
        )
        jenni_beat = next(e.beat for e in events if e.character_id == room.jenni)
        bobby_beat = next(e.beat for e in events if e.character_id == room.bobby)
        assert {r.payload["beat"] for r in rejected} == {jenni_beat}
        assert any(r.payload["text"] == LOCKS for r in rejected)
        # One regeneration call: hers. Bobby's beat is rendered once and never repaired.
        assert sum(1 for c in stub.calls if c.purpose == "regeneration") == 1
        assert len(ms.render_prompts(stub, "Bobby")) == 1
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["regenerated_beats"] == [jenni_beat]
        assert [r["beat"] for r in report["repairs"]] == [jenni_beat]
        assert bobby_beat < jenni_beat
        first_ids = {e.character_id: e.id for e in events}

        # -- Regenerating the message regenerates both primaries.
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("do", "Bobby jingles his pockets.")))
        ms.renders_for(stub, "Jenni", ms.render(beat("say", ANSWER, intent="sincere")))
        regenerated = await client.post(
            f"/stories/{room.story_id}/regenerate", json={}, headers=WRITE
        )
        assert regenerated.status_code == 200, regenerated.text
        assert len(ms.render_prompts(stub, "Bobby")) == 1
        assert len(ms.render_prompts(stub, "Jenni")) == 1
        variant = regenerated.json()["variant"]
        again = await rows(
            engine,
            "SELECT e.id, e.character_id FROM events e "
            "JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE e.story_id = :s AND o.turn_variant = :v AND e.type LIKE 'beat_%'",
            s=room.story_id,
            v=variant,
        )
        assert {e.character_id for e in again} == {room.bobby, room.jenni}
        assert not {e.id for e in again} & set(first_ids.values())  # a new variant of both
        assert regenerated.json()["order"] == [room.bobby, room.jenni]


@pytest.mark.part("M5", "FR-MULTI-5,6,11")
async def test_s_sequential__director(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    multi_delivered: None,
) -> None:
    """``sequence_order = director``: the director's order is used (its call attributed by its
    budget kind); a request override does the same; an answer naming someone else falls back to
    the request's order, listing ``director_order_skipped``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        patched = await client.patch(
            f"/stories/{room.story_id}/settings",
            json={"sequence_order": "director"},
            headers=WRITE,
        )
        assert patched.status_code == 200, patched.text
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("say", QUESTION, intent="sincere")))
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "Who is asking?", intent="sincere")))
        stub.script("director_order", [ms.director(room.jenni, room.bobby)])
        played = await _play(client, room)
        assert played["order"] == [room.jenni, room.bobby]
        assert ms.call_index(stub, "generation", "## You are Jenni\n") < ms.call_index(
            stub, "generation", "## You are Bobby\n"
        )
        (bobby_prompt,) = ms.render_prompts(stub, "Bobby")
        assert "Jenni says: Who is asking?" in bobby_prompt
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["order_source"] == "director"
        assert report["budget"]["used_by_kind"]["director_order"] == 1
        assert "director_order_skipped" not in played["degradation"]

        # A request override, and a director naming someone else: request order stands.
        room = await key_room(client, engine)
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("say", QUESTION, intent="sincere")))
        ms.renders_for(stub, "Jenni", ms.render(beat("say", ANSWER, intent="sincere")))
        stub.script("director_order", [ms.director(room.cal, room.jenni)])
        played = await _play(client, room, sequence_order="director")
        assert played["order"] == [room.bobby, room.jenni]
        assert "director_order_skipped" in played["degradation"]
        report = await turn_report(engine, room.story_id)
        assert report["multi"]["order_source"] == "director_fallback"


@pytest.mark.part("M5", "FR-MULTI-5,6,11")
async def test_s_sequential__modes(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    multi_delivered: None,
) -> None:
    """A pre-M5 story's inactive ``interaction_mode`` is read; under shared knowledge
    (partitions off) a sequential turn still renders, its later context filtered of the other's
    ``think`` and ``latent`` only; ``combined`` profiles keep one primary and refuse the
    overrides (409 ``module_off``)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE stories SET settings = jsonb_set(settings, '{inactive}', "
                    '\'{"interaction_mode": {"value": "simultaneous", '
                    '"capability": "multi_character"}}\'::jsonb) WHERE id = :s'
                ),
                {"s": room.story_id},
            )
        # #175: the inactive simultaneous mode is read and the turn runs in it.
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("say", QUESTION, intent="sincere")))
        ms.renders_for(stub, "Jenni", ms.render(beat("say", ANSWER, intent="sincere")))
        played = await _play(client, room)
        assert played["interaction_mode"] == "simultaneous"
        (jenni_prompt,) = ms.render_prompts(stub, "Jenni")
        assert QUESTION not in jenni_prompt
        stub.reset()
        ms.renders_for(stub, "Bobby", ms.render(beat("say", QUESTION, intent="sincere")))
        ms.renders_for(stub, "Jenni", ms.render(beat("say", ANSWER, intent="sincere")))
        played = await _play(client, room, interaction_mode="sequential")
        assert played["order"] == [room.bobby, room.jenni]
        assert played["interaction_mode"] == "sequential"

        # Shared knowledge (partitions off): multi-render still works; the later primary's
        # context is not viewpoint-filtered beyond the other's think and latent.
        story, ids = await structured_story(client, "Cal", "Bobby", "Jenni")
        await ms.per_character(engine, story)
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(
                beat("think", BOBBY_THINKS),
                beat("say", QUESTION, intent="sincere"),
                beat(
                    "latent",
                    "The ledger is under the floorboard.",
                    visibility={"scope": "characters", "characters": [ids["Bobby"]]},
                ),
            ),
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("say", ANSWER, intent="sincere")))
        shared = await respond(
            client,
            story,
            body(ids["Cal"], "What now?", render_character_ids=[ids["Bobby"], ids["Jenni"]]),
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["order"] == [ids["Bobby"], ids["Jenni"]]
        (jenni_prompt,) = ms.render_prompts(stub, "Jenni")
        assert f"Bobby says: {QUESTION}" in jenni_prompt
        assert BOBBY_THINKS not in jenni_prompt
        assert "floorboard" not in jenni_prompt
        report = await turn_report(engine, story)
        assert report["multi"]["accepted"] == 0  # no partitions: no beliefs, no acceptances
        assert not await rows(
            engine, "SELECT id FROM belief_transitions WHERE story_id = :s", s=story
        )

        # Under combined renders: two named are 422, an override is 409 module_off.
        room = await key_room(client, engine)
        await ms.per_character(engine, room.story_id, multi_character={"renders": "combined"})
        refused = await respond(client, room.story_id, _ask(room))
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["reason"] == "one_render_character"
        refused = await respond(
            client,
            room.story_id,
            body(
                room.cal, "What now?", render_character_ids=[room.bobby], sequence_order="director"
            ),
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "module_off"
