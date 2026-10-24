"""S-PLAYER-AUTHORITY: own_character vs co_author (FR-RESP-11; docs/04 §7; #180).

A structured story (Cal, the player; Jenni; Bobby) and a key. The player's input: "Jenni smiles
and hands me the key."

- A story created without the setting reads ``player_authority = own_character`` (``GET
  /stories/{id}`` echoes it). The input is a **request** Jenni's render resolves: it reaches her
  render (and no one else's) under ``## The player asks of you``; she refuses ("Not a chance.")
  and the report says ``refused``; no Jenni ``do`` is recorded from the input (the ``utterances``
  table holds none, the complement), the span is stored tagged ``request`` on the
  ``player_input`` event and is not part of the player's own text, and nothing about the key is
  canon. Asked to drop the key, she complies with a ``do`` of her own render (``complied``).
- Under ``co_author`` (``PATCH /stories/{id}/settings``), the same input is recorded as Jenni's
  observable action: her ``beat_do`` at beat 2 (``narrated_by = player``, first person rewritten
  to "Cal"), checked against her viewpoint (her own view, the co-author asserter: the hand-over is
  permitted, basis ``co_author``, and canonized with provenance ``player``), her render after it.
  "Jenni thinks about betraying me." is rejected ``player_cannot_write_think``: no ``think`` row
  is ever written from the input (the complement), and the rest of the input stands. Placed in the
  kitchen while Cal is in the parlor, Jenni's narrated action is dropped (``not_present``) and
  nothing of hers is recorded.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import store
from tests import perception_support as ps
from tests import structured_turn_support as st
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, WRITE

HANDS = "Jenni smiles and hands me the key."
SKETCH = "Jenni smiles and hands Cal the key."
THINKS = "Jenni thinks about betraying me."
DROPS = "Jenni drops the key on the table."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _authority(client: Any, story: str) -> str:
    answer = await client.get(f"/stories/{story}", headers=READ)
    assert answer.status_code == 200, answer.text
    return str(answer.json()["settings"]["player_authority"])


async def _input_events(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'player_input' ORDER BY seq",
        s=story,
    )


async def _key_facts(engine: AsyncEngine, story: str, key: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.provenance::text AS provenance FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND (p.subject_id = :k OR p.object_id = :k)",
        s=story,
        k=key,
    )


async def _thinks(engine: AsyncEngine, story: str, character: str) -> int:
    found = await rows(
        engine,
        "SELECT count(*) AS n FROM events WHERE story_id = :s AND character_id = :c "
        "AND type = 'beat_think'",
        s=story,
        c=character,
    )
    return int(found[0].n)


@pytest.mark.integration
@pytest.mark.part("M5", "FR-RESP-11")
async def test_s_player_authority(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await st.structured_story(client, "Cal", "Jenni", "Bobby")
        async with engine.begin() as conn:
            key = await store.add_entity(conn, story, "item", "the key")
        stub = epistrel.models.stub
        cal, jenni = ids["Cal"], ids["Jenni"]
        assert await _authority(client, story) == "own_character"

        # own_character: a request Jenni's render resolves; she refuses.
        stub.script("generation", [st.render(st.beat("say", "Not a chance."))])
        answer = await respond(client, story, body(cal, "Well?", do=HANDS))
        assert answer.status_code == 200, answer.text
        (call,) = st.schema_calls(stub, "CharacterRender")
        prompt = st.text_of(call)
        assert "## You are Jenni" in prompt
        assert "## The player asks of you" in prompt
        assert SKETCH in prompt
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["mode"] == "own_character"
        assert report["requests"] == [
            {"text": HANDS, "character_id": jenni, "kind": "do", "outcome": "refused"}
        ]
        assert not [d for d in (await st.turn_report(engine, story))["verify"]["denied"]]
        (event,) = await _input_events(engine, story)
        assert "hands" not in event.payload["text"]
        assert event.payload.get("do") is None
        (request,) = event.payload["requests"]
        assert (request["tag"], request["character_id"], request["text"]) == (
            "request",
            jenni,
            HANDS,
        )
        # The complement: no Jenni ``do`` recorded from the input, nothing about the key.
        assert [
            (u.speaker_character_id, u.event_type) for u in await st.utterances(engine, story)
        ] == [(cal, "player_input"), (jenni, "beat_say")]
        assert await _key_facts(engine, story, key) == []

        # Asked something else, she complies: the action is her own render's.
        stub.reset()
        stub.script("generation", [st.render(st.beat("do", "Jenni drops the key on the table."))])
        answer = await respond(client, story, body(cal, None, do=DROPS))
        assert answer.status_code == 200, answer.text
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["requests"][0]["outcome"] == "complied"
        narrated = await rows(
            engine,
            "SELECT payload->>'narrated_by' AS by FROM events WHERE story_id = :s "
            "AND type = 'beat_do' AND character_id = :c",
            s=story,
            c=jenni,
        )
        assert [n.by for n in narrated] == [None]

        # co_author: recorded as Jenni's observable action, never her ``think``.
        patched = await client.patch(
            f"/stories/{story}/settings", json={"player_authority": "co_author"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        assert await _authority(client, story) == "co_author"
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "There. Happy?"))])
        answer = await respond(
            client, story, body(cal, "Thanks.", do=f"I hold out my hand. {HANDS} {THINKS}")
        )
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        assert "## The player asks of you" not in st.text_of(
            st.schema_calls(stub, "CharacterRender")[0]
        )
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["narrated"] == [{"beat": 2, "character_id": jenni, "kind": "do"}]
        assert report["rejected"] == [
            {"text": THINKS, "character_id": jenni, "code": "player_cannot_write_think"}
        ]
        found = await rows(
            engine,
            "SELECT beat, payload FROM events WHERE story_id = :s AND type = 'beat_do' "
            "AND character_id = :c AND payload ? 'narrated_by' ORDER BY seq",
            s=story,
            c=jenni,
        )
        assert [(f.beat, f.payload["narrated_by"], f.payload["text"]) for f in found] == [
            (2, "player", SKETCH)
        ]
        assert [b["beat"] for b in turn["beats"] if b["kind"] == "beat_say"][-1] == 3
        verdicts = (await st.turn_report(engine, story))["verify"]
        assert not [d for d in verdicts["denied"] if d["beat"] == 2]
        assert "permitted:co_author" in {v.get("permission") for v in verdicts["verdicts"]}
        assert {f.provenance for f in await _key_facts(engine, story, key)} == {"player"}
        (*_, event) = await _input_events(engine, story)
        assert event.payload["do"] == "I hold out my hand."
        assert "requests" not in event.payload
        # The complement: player input never writes her ``think``.
        assert await _thinks(engine, story, jenni) == 0

        # Not in the room: her narrated action is dropped, nothing of hers recorded.
        parlor = await ps.place_entity(engine, story, "parlor")
        kitchen = await ps.place_entity(engine, story, "kitchen")
        await ps.put(engine, story, cal, parlor)
        await ps.put(engine, story, ids["Bobby"], parlor)
        await ps.put(engine, story, jenni, kitchen)
        before = len(await st.utterances(engine, story))
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm?"))])
        answer = await respond(
            client,
            story,
            body(cal, None, do=HANDS, render_character_ids=[ids["Bobby"]]),
        )
        assert answer.status_code == 200, answer.text
        report = (await st.turn_report(engine, story))["player_authority"]
        assert report["narrated"] == []
        assert report["rejected"] == [{"text": HANDS, "character_id": jenni, "code": "not_present"}]
        made = (await st.utterances(engine, story))[before:]
        assert jenni not in {u.speaker_character_id for u in made}
