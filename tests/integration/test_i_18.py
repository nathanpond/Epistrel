"""I-18: the Lite pipeline (docs/04), the M2 parts #57 delivers: prompt order on the captured
prompt, one generation call per turn, input and reply stored with positions, several render
characters in one combined generation, and generation settings by precedence (FR-LITE-1,4,5,
FR-PROF-11). #60, #68, #69 and #72 add their slices here."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ratings import story_guidance
from epistrel.core.settings_schema import GenerationSettings
from tests.auth_support import auth_settings
from tests.concurrency_support import set_instructions, set_pins, summarise
from tests.event_support import rows
from tests.respond_support import (
    FIRST,
    PLAYER,
    SECOND,
    body,
    generation_calls,
    lite_defaults,
    new_story,
    played,
    respond_client,
    system_of,
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _order(system: str, *sections: str) -> list[int]:
    return [system.index(section) for section in sections]


@pytest.mark.part("M2", "FR-LITE-1,4,5; FR-PROF-11")
async def test_i_18(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The captured prompt is one ``system`` message in the planned order, the window as
    alternating ``user``/``assistant`` messages, and the input last; each turn makes exactly one
    ``generation`` call; the input and reply are stored as beats 1 and 2 of their turn; two
    render characters share one reply; settings resolve deployment → story → request.

    Complement asserted: a character neither acting nor rendered has no author notes, and an
    omitted request parameter falls back to the layer below."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST, SECOND, "Cass")
        await set_instructions(engine, story, "INSTRUCTIONS: stay in the village.")
        await set_pins(engine, story, "PIN: the bell rings at noon.")
        first = await played(client, story, body(ids[PLAYER], "Good morning.", do="waves"))
        await summarise(engine, story, 1)

        # FR-LITE-1 and FR-LITE-5: the order of the one prompt, with two characters rendered.
        stub.reset()
        payload = body(
            ids[PLAYER], "Who rang the bell?", render_character_ids=[ids[SECOND], ids[FIRST]]
        )
        second = await played(client, story, payload)
        (call,) = generation_calls(stub)
        assert [c.purpose for c in stub.calls] == ["generation"]
        assert [m.role for m in call.messages] == ["system", "user", "assistant", "user"]
        system = system_of(call)
        guidance = (await story_guidance(engine, story)).prompt_guidance
        assert system.startswith("## Content rating guidance\n" + guidance)
        positions = _order(
            system,
            guidance,
            "INSTRUCTIONS: stay in the village.",
            f"### Author notes: {PLAYER}",
            f"### Author notes: {FIRST}",
            f"### Author notes: {SECOND}",
            "PIN: the bell rings at noon.",
            "summary through 1",
            f"never speak or act for {PLAYER}",
        )
        assert positions == sorted(positions)
        assert "### Author notes: Cass" not in system
        assert f"Characters you voice: {SECOND}, {FIRST}." in system
        assert call.messages[1].content == f'{PLAYER}: "Good morning." *waves*'
        assert call.messages[2].content == first["message"]["text"]
        assert call.messages[3].content == f'{PLAYER}: "Who rang the bell?"'
        assert call.params.stop == (f"\n{PLAYER}:",)
        reply = next(b for b in second["beats"] if b["beat"] == 2)
        assert reply["render_character_ids"] == [ids[SECOND], ids[FIRST]]
        assert reply["actor_character_id"] is None

        # FR-PROF-11: every input and reply is an event with its position.
        events = await rows(
            engine,
            "SELECT type, narrative_turn, turn_variant, beat, character_id, provenance, payload "
            "FROM events WHERE story_id = :s AND type IN ('player_input', 'message_rendered') "
            "ORDER BY seq",
            s=story,
        )
        assert [(e.type, e.narrative_turn, e.turn_variant, e.beat) for e in events] == [
            ("player_input", 1, 1, 1),
            ("message_rendered", 1, 1, 2),
            ("player_input", 2, 1, 1),
            ("message_rendered", 2, 1, 2),
        ]
        assert events[0].character_id == ids[PLAYER]
        assert events[0].payload == {
            "text": "Good morning.\nwaves",
            "say": "Good morning.",
            "do": "waves",
        }
        assert events[3].payload["render_character_ids"] == [ids[SECOND], ids[FIRST]]
        assert events[3].payload["text"] == second["message"]["text"]
        assert len(events[3].payload["prompt_hash"]) == 64

        # FR-LITE-4: deployment defaults → story override → request, per parameter.
        deployment = GenerationSettings(temperature=0.3, top_p=0.5, length="short")
        async with lite_defaults(engine, generation=deployment):
            override = {"generation_settings": {"temperature": 0.6}}
            tuned, tuned_ids = await new_story(client, PLAYER, FIRST, settings=override)
            plain, plain_ids = await new_story(client, PLAYER, FIRST)
            cases: list[tuple[str, dict[str, str], dict[str, Any], tuple[Any, ...]]] = [
                (plain, plain_ids, {}, (0.3, 0.5, 150)),
                (tuned, tuned_ids, {}, (0.6, 0.5, 150)),
                (tuned, tuned_ids, {"top_p": 0.7}, (0.6, 0.7, 150)),
                (tuned, tuned_ids, {"length": "long", "temperature": 1.1}, (1.1, 0.5, 800)),
                (tuned, tuned_ids, {"length": 321}, (0.6, 0.5, 321)),
            ]
            for target, who, params, expected in cases:
                stub.reset()
                await played(client, target, body(who[PLAYER], prompt_params=params))
                (sent,) = generation_calls(stub)
                assert (sent.params.temperature, sent.params.top_p, sent.params.max_tokens) == (
                    expected
                )
            recorded = await rows(
                engine,
                "SELECT config_revisions FROM operations WHERE story_id = :s AND kind = 'turn' "
                "ORDER BY started_at DESC LIMIT 1",
                s=tuned,
            )
            assert recorded[0].config_revisions["generation_settings"] == {
                "temperature": 0.6,
                "top_p": 0.5,
                "length": 321,
                "max_tokens": 321,
            }
