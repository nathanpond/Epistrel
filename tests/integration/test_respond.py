"""``POST /stories/{id}/respond`` (#57) beyond the marked parts: the request's refusals and
their order, the viewpoint, the input gate, empty and failed generations, the window size, a
read-set edit during generation, and the library surface."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import lite_defaults, read_settings
from epistrel.core.context import RequestContext
from epistrel.core.deployment import read_lite_defaults, update_lite_defaults
from epistrel.core.settings_schema import GenerationSettings, LiteDefaults
from epistrel.orchestrator.lite.schemas import PlayerInputBody, RespondRequest
from tests.auth_support import auth_settings
from tests.concurrency_support import set_pins
from tests.event_support import rows
from tests.gate_support import audit_rows, input_policies
from tests.respond_support import (
    FIRST,
    PLAYER,
    SECOND,
    body,
    generation_calls,
    new_story,
    played,
    respond,
    respond_client,
    story_events,
)
from tests.story_support import WRITE, count


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _turn_ops(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT status, attempts, viewpoint FROM operations WHERE story_id = :s AND kind = 'turn' "
        "ORDER BY started_at",
        s=story,
    )


def _error(response: Any) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


async def test_unknown_characters_are_refused_with_nothing_written(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """An unknown actor, render id or present character is 422 naming each, with no operation
    and no model call; an explicit empty render list and a blank input are 422 too.
    Complement asserted: the same story then plays a valid request."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        stub = lib.models.stub
        stub.reset()
        bad = body(
            "0" * 26,
            render_character_ids=[ids[FIRST], "1" * 26],
            scene_context={"characters_present": ["2" * 26]},
        )
        refused = await respond(client, story, bad)
        assert refused.status_code == 422
        details = _error(refused)["details"]
        assert details == {
            "unknown_actor": "0" * 26,
            "unknown_render_ids": ["1" * 26],
            "unknown_present_ids": ["2" * 26],
        }
        for payload in (
            body(ids[PLAYER], render_character_ids=[]),
            body(ids[PLAYER], say="   "),
            body(ids[PLAYER], say=None),
            {**body(ids[PLAYER]), "prompt_params": {"temperature": 3}},
            {**body(ids[PLAYER]), "prompt_params": {"length": "huge"}},
            {**body(ids[PLAYER]), "scene_context": {"recent_messages": [{"text": "x"}] * 21}},
        ):
            response = await respond(client, story, payload)
            assert response.status_code == 422, payload
        assert stub.calls == []
        assert await _turn_ops(engine, story) == []
        missing = await respond(client, "9" * 26, body(ids[PLAYER]))
        assert missing.status_code == 404
        await played(client, story, body(ids[PLAYER]))


async def test_render_defaults_and_duplicates(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Omitted render ids are every other cast member in cast order, or the actor when the cast
    is the player alone (#62); duplicates are dropped and an actor who is rendered loses the
    stop sequence."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        stub = lib.models.stub
        stub.reset()
        result = await played(client, story, body(ids[FIRST]))
        assert result["beats"][1]["render_character_ids"] == [ids[PLAYER], ids[SECOND]]
        stub.reset()
        payload = body(ids[PLAYER], render_character_ids=[ids[SECOND], ids[SECOND], ids[PLAYER]])
        result = await played(client, story, payload)
        assert result["beats"][1]["render_character_ids"] == [ids[SECOND], ids[PLAYER]]
        (call,) = generation_calls(stub)
        assert call.params.stop is None
        assert "never speak or act for" not in call.messages[0].content

        solo, solo_ids = await new_story(client, PLAYER)
        stub.reset()
        result = await played(client, solo, body(solo_ids[PLAYER]))
        assert result["beats"][1]["render_character_ids"] == [solo_ids[PLAYER]]
        (call,) = generation_calls(stub)
        assert call.params.stop is None
        assert f"Characters you voice: {PLAYER}." in call.messages[0].content


async def test_archived_stale_and_viewpoint_refusals(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A stale expected revision is 409 ``revision_conflict``; an unresolvable viewpoint is 422
    ``viewpoint_unresolved``; an archived story is 409 ``story_archived``; none writes anything.
    Complement asserted: ``omniscient`` named explicitly is echoed and stored on the turn."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        stale = await respond(
            client, story, body(ids[PLAYER]), headers={"X-Epistrel-Expected-Revision": "99"}
        )
        assert stale.status_code == 409
        assert _error(stale)["code"] == "revision_conflict"
        for viewpoint in ("character:nobody", "everyone"):
            unresolved = await respond(client, story, body(ids[PLAYER], output_viewpoint=viewpoint))
            assert unresolved.status_code == 422
            assert _error(unresolved)["code"] == "viewpoint_unresolved"
        assert await _turn_ops(engine, story) == []

        result = await played(client, story, body(ids[PLAYER], output_viewpoint="omniscient"))
        assert result["output_viewpoint"] == "omniscient"
        assert [op.viewpoint for op in await _turn_ops(engine, story)] == ["omniscient"]
        result = await played(
            client, story, body(ids[PLAYER], output_viewpoint=f"character:{ids[FIRST]}")
        )
        assert result["output_viewpoint"] == f"character:{ids[FIRST]}"

        archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
        assert archived.status_code == 200
        refused = await respond(client, story, body(ids[PLAYER]))
        assert refused.status_code == 409
        assert _error(refused)["code"] == "story_archived"
        assert len(await _turn_ops(engine, story)) == 2


async def test_input_gate_runs_first(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A violating input is 422 ``guardrail_violation`` with only the audit row written: no
    operation, no event and no generation call. Complement asserted: a clean input plays."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        await input_policies(lib, story, {"rule": {"type": "wordlist", "words": ["zarquon"]}})
        lib.models.stub.reset()
        refused = await respond(client, story, body(ids[PLAYER], "Hail zarquon!"))
        assert refused.status_code == 422
        assert _error(refused)["code"] == "guardrail_violation"
        assert _error(refused)["details"]["violations"][0]["locator"] == "player_input"
        assert generation_calls(lib.models.stub) == []
        assert await _turn_ops(engine, story) == []
        assert [r.details["entry_point"] for r in await audit_rows(engine, story_id=story)] == [
            "respond"
        ]
        await played(client, story, body(ids[PLAYER], "Hail friend!"))


async def test_empty_reply_is_asked_again_once(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """One empty reply is regenerated (two generation calls, one turn); two are 503
    ``model_unavailable`` ``reason=empty`` with the operation failed and no event. A provider
    fault is 503 with nothing stored too."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        stub = lib.models.stub
        stub.reset()
        stub.fault("generation", "empty", 1)
        result = await played(client, story, body(ids[PLAYER]))
        assert [c["outcome"] for c in result["model_calls"]] == ["invalid_output", "ok"]
        events_before = len(await story_events(engine, story))

        stub.fault("generation", "empty", 2)
        failed = await respond(client, story, body(ids[PLAYER]))
        assert failed.status_code == 503
        assert _error(failed)["details"]["reason"] == "empty"
        stub.fault("generation", "timeout", 1)
        timed_out = await respond(client, story, body(ids[PLAYER]))
        assert timed_out.status_code == 503
        assert [op.status for op in await _turn_ops(engine, story)] == [
            "committed",
            "failed",
            "failed",
        ]
        assert len(await story_events(engine, story)) == events_before


async def test_window_size_counts_transcript_rows(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A story's ``lite_window_messages`` bounds the verbatim window to its latest rows."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client, settings={"lite_window_messages": 3})
        for said in ("one", "two", "three"):
            await played(client, story, body(ids[PLAYER], said))
        stub = lib.models.stub
        stub.reset()
        await played(client, story, body(ids[PLAYER], "four"))
        (call,) = generation_calls(stub)
        window = [m for m in call.messages[1:-1]]
        assert [m.role for m in window] == ["assistant", "user", "assistant"]
        assert window[1].content == f'{PLAYER}: "three"'


async def test_pins_edit_during_generation_regenerates(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A pinned-memory edit committed while the turn generates touches the read set: the same
    operation generates again with the new pins and then commits."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        stub = lib.models.stub
        stub.reset()
        latch = stub.hold("generation")
        task = asyncio.ensure_future(respond(client, story, body(ids[PLAYER])))
        await asyncio.wait_for(latch.entered.wait(), 5)
        await set_pins(engine, story, "NEW PIN")
        latch.release()
        response = await task
        assert response.status_code == 200, response.text
        calls = generation_calls(stub)
        assert len(calls) == 2
        assert "NEW PIN" not in calls[0].messages[0].content
        assert "NEW PIN" in calls[1].messages[0].content
        (op,) = await _turn_ops(engine, story)
        assert [a["outcome"] for a in op.attempts] == ["regenerate", "commit"]
        assert op.attempts[0]["touched"] == ["pins"]


async def test_library_respond_needs_no_credential(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``Engine.respond`` plays the same turn in process with only a principal."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        request = RespondRequest(
            actor_character_id=ids[PLAYER], player_input=PlayerInputBody(say="Hi.")
        )
        result = await lib.respond(RequestContext(principal="p-lib"), story, request)
        assert (result.turn, result.audience_filter) == (1, "none")
        assert [b.beat for b in result.beats] == [1, 2]
        principal = await count(
            engine,
            "SELECT count(*) FROM events WHERE story_id = :s AND principal = 'p-lib'",
            s=story,
        )
        assert principal == 2


async def test_openapi_calls_the_message_finished_never_verified(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """FR-RESP-1's last sentence: no description in the API schema calls the message
    "verified"; the respond operation calls it finished."""
    async with respond_client(stub_settings, engine) as (client, _):
        schema = (await client.get("/openapi.json")).json()
    descriptions: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("description", "summary") and isinstance(value, str):
                    descriptions.append(value)
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(schema)
    assert descriptions
    assert not [d for d in descriptions if "verified" in d.lower()]
    operation = schema["paths"]["/stories/{story_id}/respond"]["post"]
    assert "finished" in operation["description"]


async def test_lite_defaults_write_is_recorded(engine: AsyncEngine) -> None:
    """The deployment's Lite defaults are a recorded configuration write: one revision and one
    prospective ``config_changes`` row per changed key; the same values again write nothing; a
    corrupt stored document reads as the defaults."""
    before = await read_lite_defaults(engine)
    start = (await read_settings(engine)).config_revision
    try:
        await update_lite_defaults(
            engine,
            principal="p-operator",
            generation=GenerationSettings(temperature=0.1, length="short"),
            window_messages=7,
        )
        assert (await read_settings(engine)).config_revision == start + 1
        changes = await rows(
            engine,
            "SELECT setting, class AS change_class, actor FROM config_changes "
            "WHERE scope = 'deployment' AND config_revision = :r ORDER BY setting",
            r=start + 1,
        )
        assert [(c.setting, c.change_class, c.actor) for c in changes] == [
            ("lite_generation_defaults", "prospective", "p-operator"),
            ("lite_window_messages", "prospective", "p-operator"),
        ]
        again = await update_lite_defaults(engine, principal="p-operator", window_messages=7)
        assert again.lite_window_messages == 7
        assert (await read_settings(engine)).config_revision == start + 1
    finally:
        await update_lite_defaults(
            engine,
            principal="p-operator",
            generation=before.lite_generation_defaults,
            window_messages=before.lite_window_messages,
        )
    assert lite_defaults({"lite_window_messages": "lots"}) == LiteDefaults()
    assert lite_defaults(None) == LiteDefaults()
