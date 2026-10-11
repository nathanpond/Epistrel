"""I-18: the Lite pipeline (docs/04), the M2 parts #57 delivers: prompt order on the captured
prompt, one generation call per turn, input and reply stored with positions, several render
characters in one combined generation, and generation settings by precedence (FR-LITE-1,4,5,
FR-PROF-11); #60's slice edits pinned memories and custom instructions mid-turn (FR-LITE-3).
#64's slice (``test_i_18__no_bypass``) shows no story setting turns guardrails off
(FR-PROF-14). #68, #69 and #72 add their slices here."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.errors import GuardrailViolation
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import story_guidance
from epistrel.core.settings_schema import GenerationSettings
from epistrel.core.stories import StorySpec
from epistrel.core.story_settings import update_story_settings
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.lite.schemas import RespondRequest
from epistrel.orchestrator.policies.rebase import output_passes
from epistrel.orchestrator.policies.store import guard_context
from tests.auth_support import auth_settings
from tests.concurrency_support import set_instructions, set_pins, summarise
from tests.event_support import rows, scalar
from tests.gate_support import input_policies
from tests.policy_support import OPERATOR, global_policy, words
from tests.respond_support import (
    FIRST,
    PLAYER,
    SECOND,
    body,
    generation_calls,
    lite_defaults,
    new_story,
    played,
    respond,
    respond_client,
    system_of,
)
from tests.story_support import READ, WRITE


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
            f"### Author notes: {SECOND}",
            f"### Author notes: {FIRST}",
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


async def _revision(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story))


@pytest.mark.part("M2", "FR-LITE-3")
async def test_i_18__pins(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Pinned memories and custom instructions are story-level text in every prompt: an edit
    is an event that moves the revision by exactly one, and one committed while a turn is
    generating makes that turn generate again (two calls) with the new pin before it commits.

    Complements asserted: a violating edit is refused with the stored value, the revision and
    the event log unchanged; an edit that changes nothing moves nothing."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client)
        pins_url = f"/stories/{story}/pinned-memories"
        instructions_url = f"/stories/{story}/custom-instructions"

        # Edit → revision +1, recorded as an event with the full value.
        before = await _revision(engine, story)
        put = await client.put(pins_url, json={"items": [{"text": "PIN A"}]}, headers=WRITE)
        assert put.status_code == 200, put.text
        assert put.json()["timeline_revision"] == before + 1
        put = await client.put(instructions_url, json={"text": "Speak in rhyme."}, headers=WRITE)
        assert put.json()["timeline_revision"] == before + 2
        recorded = await rows(
            engine,
            "SELECT type, payload FROM events WHERE story_id = :s AND type IN "
            "('pinned_memories_set', 'custom_instructions_set') ORDER BY seq",
            s=story,
        )
        assert [r.type for r in recorded] == ["pinned_memories_set", "custom_instructions_set"]
        assert [i["text"] for i in recorded[0].payload["items"]] == ["PIN A"]
        assert recorded[1].payload == {"text": "Speak in rhyme.", "previous": ""}

        # Mid-generation edit → the turn regenerates with the new pin before it commits.
        stub.reset()
        latch = stub.hold("generation")
        task = asyncio.ensure_future(respond(client, story, body(ids[PLAYER])))
        await asyncio.wait_for(latch.entered.wait(), 5)
        edited = await client.put(pins_url, json={"items": [{"text": "PIN B"}]}, headers=WRITE)
        assert edited.status_code == 200, edited.text
        latch.release()
        turn = await task
        assert turn.status_code == 200, turn.text
        calls = generation_calls(stub)
        assert len(calls) == 2
        assert "PIN A" in system_of(calls[0])
        final = system_of(calls[1])
        assert "PIN B" in final
        assert "PIN A" not in final
        assert "Speak in rhyme." in final
        assert turn.json()["timeline_revision"] == edited.json()["timeline_revision"] + 1

        # Violation → refused, nothing changed.
        await input_policies(lib, story, {"rule": {"type": "wordlist", "words": ["zarquon"]}})
        stored = (await client.get(pins_url, headers=READ)).json()
        event_count = await scalar(
            engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story
        )
        refused = await client.put(
            pins_url, json={"items": [{"text": "PIN B"}, {"text": "Hail zarquon"}]}, headers=WRITE
        )
        assert refused.status_code == 422
        error = refused.json()["error"]
        assert error["code"] == "guardrail_violation"
        assert error["details"]["violations"][0]["locator"] == "items[1].text"
        assert (await client.get(pins_url, headers=READ)).json() == stored
        assert (
            await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story)
            == event_count
        )

        # No change → nothing written.
        same = await client.put(pins_url, json={"items": [{"text": "PIN B"}]}, headers=WRITE)
        assert same.status_code == 200
        assert same.json()["operation_id"] is None
        assert same.json()["timeline_revision"] == stored["timeline_revision"]


# -- FR-PROF-14: no story setting turns guardrails off (#64) ----------------------------------

GENERATION_LAYERS = st.builds(
    lambda t, p, n: {"temperature": t, "top_p": p, "length": n},
    st.none() | st.floats(0.0, 2.0),
    st.none() | st.floats(0.0, 1.0),
    st.none() | st.sampled_from(["short", "medium", "long"]) | st.integers(1, 4_000),
)


def _story_settings(cast: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    """Every ``StorySettings`` field at random allowed values (``None`` resets to default)."""
    return st.fixed_dictionaries(
        {
            "regenerate_retry_budget": st.integers(0, 10),
            "turn_concurrency": st.sampled_from(["reject", "queue"]),
            "turn_queue_max": st.none() | st.integers(1, 50),
            "turn_deadline": st.floats(0.5, 600.0),
            "default_output_viewpoint": st.none()
            | st.sampled_from([f"character:{c}" for c in cast]),
            "generation_settings": st.none() | GENERATION_LAYERS,
            "lite_window_messages": st.none() | st.integers(1, 500),
        }
    )


async def _no_bypass_setup(url: str, word: str) -> tuple[str, list[str], str]:
    engine = make_engine(auth_settings(url))
    lib = Engine(auth_settings(url, model_adapter="stub"), db=engine)
    try:
        created = await lib.create_story(
            OPERATOR,
            StorySpec.model_validate(
                {
                    "name": "no bypass",
                    "characters": [
                        {"name": PLAYER, "definition": "The player.", "true_age": 30},
                        {"name": FIRST, "definition": "A guide.", "true_age": 40},
                    ],
                }
            ),
        )
        policy = await global_policy(lib, words(word))
        return created.story_id, [c.id for c in created.characters], policy.id
    finally:
        await engine.dispose()


async def _guardrails_hold(
    url: str, story: str, actor: str, patch: dict[str, Any], word: str, *, expect_caught: bool
) -> None:
    """Apply ``patch`` to the story's settings, then: the output re-check and the input gate
    both catch ``word`` (or, with the policy retired, both let it through)."""
    engine = make_engine(auth_settings(url))
    lib = Engine(auth_settings(url, model_adapter="stub"), db=engine)
    try:
        await update_story_settings(engine, story, patch, principal="p-author")
        text = f"The {word} is here."
        context = await guard_context(engine, story)
        passes = await output_passes(lib.models, [text], context, budget=TurnBudget())
        assert passes is not expect_caught
        request = RespondRequest.model_validate(
            {"actor_character_id": actor, "player_input": {"say": text}}
        )
        try:
            await lib.respond(OPERATOR, story, request)
        except GuardrailViolation:
            assert expect_caught
        else:
            assert not expect_caught
    finally:
        await lib.stop_workers()
        await engine.dispose()


@pytest.mark.part("M2", "FR-PROF-14")
def test_i_18__no_bypass(pg_url: str) -> None:
    """With a guardrail policy defined, no story setting disables guardrail evaluation: for
    every ``StorySettings`` field at random allowed values, a violating reply is still caught
    by the output check and the same text as input is refused 422 ``guardrail_violation``.

    Complement asserted: with the policy retired, the same reply passes and the same input
    plays."""
    word = f"zq{new_ulid().lower()[-8:]}"
    story, cast, policy_id = asyncio.run(_no_bypass_setup(pg_url, word))
    seen: list[dict[str, Any]] = []

    @settings(
        max_examples=25,
        deadline=None,
        derandomize=True,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(patch=_story_settings(cast))
    def every_setting(patch: dict[str, Any]) -> None:
        seen.append(patch)
        asyncio.run(_guardrails_hold(pg_url, story, cast[0], patch, word, expect_caught=True))

    every_setting()
    assert {p["turn_concurrency"] for p in seen} == {"reject", "queue"}

    async def retire() -> None:
        engine = make_engine(auth_settings(pg_url))
        try:
            await Engine(auth_settings(pg_url), db=engine).policies_retire(OPERATOR, policy_id)
        finally:
            await engine.dispose()

    asyncio.run(retire())
    asyncio.run(_guardrails_hold(pg_url, story, cast[0], seen[-1], word, expect_caught=False))
