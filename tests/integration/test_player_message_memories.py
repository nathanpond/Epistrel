"""#958 (FR-MEM-1, FR-TURN-2): a player's message writes memories for everyone who perceives it,
whether or not the turn has a response, and keeps them when the response is discarded.

A structured story at perception ``full``: Wren (the player), Jenni and Marcus in the parlor,
Bobby in the kitchen (no adjacency: he perceives nothing of the parlor). Played on Postgres with
the stub adapter.

- ``test_ingested_player_turn__perceivers_remember``: an ingested ``player_turn`` gives Wren,
  Jenni and Marcus one memory each and Bobby none, with no model call; ingested ``narration``
  reaches the parlor the same way. With ``memory.episodic`` off nothing is written.
- ``test_player_whisper__content_to_target_only``: the player's whisper inside a generated turn
  reaches Jenni (its target) as content and Marcus as the envelope only; Bobby gets nothing.
- ``test_discarded_response__message_kept``: a regenerate keeps the message's memories (same
  ids, still active, one per perceiver) while the old response's go dormant and the new
  response's are active; a swap back and forth keeps them; truncating past the message retires
  them and untruncating restores them.
- ``test_message_memory__retrieved_after_working_memory``: 25 beats later the message has left
  Jenni's working memory and her retrieval still returns its memory.
- ``test_shared_memory__one_row_per_beat`` (#1042): under shared memory (perception ``none``)
  the message's say and do are two holder-less rows citing the message's event, and a
  regenerate keeps them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.memory import records
from epistrel.core.retrieval import pipeline
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.projection_support import swap, truncate, untruncate
from tests.respond_support import respond, respond_client
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration

WORDS = "The ledger is under the third floorboard."
RAIN = "Rain hammers the shutters."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _scene(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str]]:
    story, ids = await structured_story(client, "Wren", "Jenni", "Marcus", "Bobby")
    await ps.perceive(engine, story, "full")
    parlor = await ps.place_entity(engine, story, "parlor")
    kitchen = await ps.place_entity(engine, story, "kitchen")
    for name in ("Wren", "Jenni", "Marcus"):
        await ps.put(engine, story, ids[name], parlor)
    await ps.put(engine, story, ids["Bobby"], kitchen)
    return story, ids


async def _ingest(client: Any, story: str, *events: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        f"/stories/{story}/ingest", json={"events": list(events)}, headers=WRITE
    )
    assert response.status_code in (200, 201), response.text
    found: dict[str, Any] = response.json()
    return found


async def _message_rows(engine: AsyncEngine, story: str, turn: int = 1) -> list[Any]:
    """The player-message memories at ``turn``, in any membership."""
    return await rows(
        engine,
        "SELECT id, holder_character_id, text, fidelity, membership, operation_id "
        "FROM memories WHERE story_id = :s AND provenance = 'player' AND narrative_turn = :t "
        "ORDER BY holder_character_id, id",
        s=story,
        t=turn,
    )


async def _by_holder(engine: AsyncEngine, story: str, turn: int = 1) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}
    for row in await _message_rows(engine, story, turn):
        if row.membership == "active":
            found.setdefault(str(row.holder_character_id), []).append(row)
    return found


@pytest.mark.usefixtures("drained_embedding_jobs")
async def test_ingested_player_turn__perceivers_remember(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        try:
            story, ids = await _scene(client, engine)
            stub.reset()
            await _ingest(
                client,
                story,
                {
                    "client_event_id": "m-1",
                    "kind": "player_turn",
                    "actor_character_id": ids["Wren"],
                    "say": WORDS,
                },
            )
            assert stub.calls == []  # no model call
            held = await _by_holder(engine, story)
            line = f"Wren says: {WORDS}"
            for name in ("Wren", "Jenni", "Marcus"):
                assert [(r.text, r.fidelity) for r in held[ids[name]]] == [(line, "content")]
            # The complement: Bobby, in another room, holds nothing of it.
            assert ids["Bobby"] not in held
            assert len(await _message_rows(engine, story)) == 3

            # Ingested narration reaches every character at the scene's location.
            await _ingest(client, story, {"client_event_id": "n-1", "kind": "narration",
                                          "text": RAIN})  # fmt: skip
            narrated = await rows(
                engine,
                "SELECT holder_character_id, text, provenance FROM memories "
                "WHERE story_id = :s AND narrative_turn = 2 AND membership = 'active'",
                s=story,
            )
            assert sorted(str(r.holder_character_id) for r in narrated) == sorted(
                ids[n] for n in ("Wren", "Jenni", "Marcus")
            )
            assert {(r.text, r.provenance) for r in narrated} == {(RAIN, "director")}

            # A replay writes nothing new.
            await _ingest(
                client,
                story,
                {
                    "client_event_id": "m-1",
                    "kind": "player_turn",
                    "actor_character_id": ids["Wren"],
                    "say": WORDS,
                },
            )
            assert len(await _message_rows(engine, story)) == 3

            # Episodic memory off: the message is recorded, and nothing is remembered.
            quiet, quiet_ids = await _scene(client, engine)
            await override(
                client,
                quiet,
                {"memory": {"episodic": {"enabled": False}, "consolidation": {"enabled": False}}},
            )
            await _ingest(
                client,
                quiet,
                {
                    "client_event_id": "q-1",
                    "kind": "player_turn",
                    "actor_character_id": quiet_ids["Wren"],
                    "say": WORDS,
                },
            )
            assert await rows(engine, "SELECT id FROM memories WHERE story_id = :s", s=quiet) == []
        finally:
            await ps.forget_perception(engine)


@pytest.mark.usefixtures("drained_embedding_jobs")
async def test_player_whisper__content_to_target_only(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        try:
            story, ids = await _scene(client, engine)
            stub.reset()
            stub.script("generation", [render(beat("say", "Understood.", intent="sincere"))])
            payload = {
                "actor_character_id": ids["Wren"],
                "player_input": {"say": WORDS, "whisper_to": [ids["Jenni"]]},
                "render_character_ids": [ids["Jenni"]],
            }
            response = await respond(client, story, payload)
            assert response.status_code == 200, response.text
            held = await _by_holder(engine, story)
            assert [(r.text, r.fidelity) for r in held[ids["Jenni"]]] == [
                (f"Wren says: {WORDS}", "content")
            ]
            assert [(r.text, r.fidelity) for r in held[ids["Marcus"]]] == [
                ("Wren whispers something to Jenni", "envelope")
            ]
            assert ids["Bobby"] not in held
            content = [
                r.holder_character_id
                for r in await _message_rows(engine, story)
                if r.fidelity == "content"
            ]
            assert sorted(map(str, content)) == sorted([ids["Wren"], ids["Jenni"]])
        finally:
            await ps.forget_perception(engine)


async def _response_rows(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT m.id, m.membership, m.turn_variant FROM memories m "
        "WHERE m.story_id = :s AND m.narrative_turn = 1 AND m.provenance = 'render'",
        s=story,
    )


@pytest.mark.usefixtures("drained_embedding_jobs")
async def test_discarded_response__message_kept(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        try:
            story, ids = await _scene(client, engine)
            stub.reset()
            stub.script("generation", [render(beat("say", "First answer.", intent="sincere"))])
            payload = {
                "actor_character_id": ids["Wren"],
                "player_input": {"say": WORDS},
                "render_character_ids": [ids["Jenni"]],
            }
            response = await respond(client, story, payload)
            assert response.status_code == 200, response.text
            before = {r.id: r for r in await _message_rows(engine, story)}
            assert len(before) == 3
            assert {r.membership for r in before.values()} == {"active"}
            first = await _response_rows(engine, story)
            assert first
            assert {r.membership for r in first} == {"active"}

            # Regenerate: the response is replaced, the message's memories stay.
            stub.script("generation", [render(beat("say", "Second answer.", intent="sincere"))])
            regenerated = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
            assert regenerated.status_code == 200, regenerated.text
            after = {r.id: r for r in await _message_rows(engine, story)}
            assert set(after) == set(before)  # same ids, none written twice
            assert {r.membership for r in after.values()} == {"active"}
            held = await _by_holder(engine, story)
            assert sorted(len(v) for v in held.values()) == [1, 1, 1]
            old = {r.id for r in first}
            responses = await _response_rows(engine, story)
            assert {r.membership for r in responses if r.id in old} == {"dormant"}
            assert {r.membership for r in responses if r.id not in old} == {"active"}

            # Swap back to the first response and again to the second: the message stays.
            for _ in range(2):
                assert await swap(engine, story) is not None
                assert {r.membership for r in await _message_rows(engine, story)} == {"active"}
                assert sorted(len(v) for v in (await _by_holder(engine, story)).values()) == [
                    1,
                    1,
                    1,
                ]

            # Rewinding the message itself retires its memories; untruncate restores them.
            await _ingest(client, story, {"client_event_id": "n-1", "kind": "narration",
                                          "text": RAIN})  # fmt: skip
            assert await truncate(engine, story, 0) is not None
            assert {r.membership for r in await _message_rows(engine, story)} != {"active"}
            assert await _by_holder(engine, story) == {}
            assert await untruncate(engine, story) is not None
            restored = await _message_rows(engine, story)
            assert {r.id for r in restored if r.membership == "active"} == set(before)
        finally:
            await ps.forget_perception(engine)


@pytest.mark.usefixtures("drained_embedding_jobs")
async def test_message_memory__retrieved_after_working_memory(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, _):
        try:
            story, ids = await _scene(client, engine)
            await _ingest(
                client,
                story,
                {
                    "client_event_id": "m-1",
                    "kind": "player_turn",
                    "actor_character_id": ids["Wren"],
                    "say": WORDS,
                },
            )
            await _ingest(
                client,
                story,
                *(
                    {"client_event_id": f"n-{i}", "kind": "narration", "text": f"{RAIN} ({i})"}
                    for i in range(25)
                ),
            )
            jenni = ids["Jenni"]
            (memory,) = (await _by_holder(engine, story))[jenni]
            async with engine.begin() as conn:
                window = await records.working_memory(
                    conn, story, jenni, level="full", n=records.DEFAULT_WORKING_MEMORY_BEATS
                )
                assert all(WORDS not in b.text for b in window)
                found = await pipeline.retrieve(
                    conn,
                    story,
                    pipeline.RetrievalQuery("Where is the ledger? The floorboard?", actor=jenni),
                    viewpoint=f"character:{jenni}",
                    touch=False,
                )
            returned = [*found.episodes, *found.memories]
            assert str(memory.id) in {m.id for m in returned}, json.dumps(
                [m.text for m in returned]
            )
        finally:
            await ps.forget_perception(engine)


LIFTS = "lifts the third floorboard"


async def _shared_rows(engine: AsyncEngine, story: str) -> list[Any]:
    """The player-message memories of turn 1 with what they cite."""
    return await rows(
        engine,
        "SELECT id, holder_character_id, text, membership, source_event_ids "
        "FROM memories WHERE story_id = :s AND provenance = 'player' AND narrative_turn = 1 "
        "ORDER BY id",
        s=story,
    )


async def _message_event(engine: AsyncEngine, story: str) -> str:
    (found,) = await rows(
        engine,
        "SELECT id FROM events WHERE story_id = :s AND type = 'player_input' ORDER BY seq",
        s=story,
    )
    return str(found.id)


def _assert_shared(found: list[Any], event_id: str) -> None:
    """Two active holder-less rows, the say's and the do's, each citing the message's event."""
    assert len(found) == 2, found
    assert {r.holder_character_id for r in found} == {None}
    assert {r.membership for r in found} == {"active"}
    assert sorted(WORDS in r.text for r in found) == [False, True]
    assert sorted(LIFTS in r.text for r in found) == [False, True]
    assert all(event_id in [str(e) for e in r.source_event_ids] for r in found), found


@pytest.mark.usefixtures("drained_embedding_jobs")
async def test_shared_memory__one_row_per_beat(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """#1042 (#958; FR-MEM-1, FR-TURN-2): a shared-memory story (perception ``none``, the
    default). An ingested ``player_turn`` with a say and a do writes two holder-less episodic
    rows, one per observable beat, each citing the message's event, with no model call. A
    responded message writes the same two rows, and a regenerate (the response discarded)
    keeps them: same ids, still active."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- an ingested player turn
        story, ids = await structured_story(client, "Wren", "Jenni")
        stub.reset()
        await _ingest(
            client,
            story,
            {
                "client_event_id": "m-1",
                "kind": "player_turn",
                "actor_character_id": ids["Wren"],
                "say": WORDS,
                "do": LIFTS,
            },
        )
        assert stub.calls == []
        _assert_shared(await _shared_rows(engine, story), await _message_event(engine, story))

        # -- a responded message, then its response discarded
        story, ids = await structured_story(client, "Wren", "Jenni")
        stub.script("generation", [render(beat("say", "First answer.", intent="sincere"))])
        response = await respond(
            client,
            story,
            {
                "actor_character_id": ids["Wren"],
                "player_input": {"say": WORDS, "do": LIFTS},
                "render_character_ids": [ids["Jenni"]],
            },
        )
        assert response.status_code == 200, response.text
        event_id = await _message_event(engine, story)
        before = await _shared_rows(engine, story)
        _assert_shared(before, event_id)
        stub.script("generation", [render(beat("say", "Second answer.", intent="sincere"))])
        regenerated = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
        assert regenerated.status_code == 200, regenerated.text
        after = await _shared_rows(engine, story)
        assert [r.id for r in after] == [r.id for r in before]  # none written twice
        _assert_shared(after, event_id)
