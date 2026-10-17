"""I-18: the Lite pipeline (docs/04), the M2 parts #57 delivers: prompt order on the captured
prompt, one generation call per turn, input and reply stored with positions, several render
characters in one combined generation, and generation settings by precedence (FR-LITE-1,4,5,
FR-PROF-11); #60's slice edits pinned memories and custom instructions mid-turn (FR-LITE-3).
#64's slice (``test_i_18__no_bypass``) shows no story setting turns guardrails off
(FR-PROF-14). #68's slice (``test_i_18__call_count``) counts the calls guardrails add
(FR-LITE-7, FR-GUARD-9). #72's slices cover the rolling summary: ``test_i_18__summary``
(segments, merging, truncate/regenerate/fork, FR-LITE-2), ``test_i_18__summary_policy`` (the
deterministic output check on segments) and ``test_i_18__summary_budget`` (off the turn,
FR-LITE-7).

The M3 part #113 delivers, ``test_i_18__profile_resolution``, resolves the generation settings
from the story's engine profile, then its overrides, then the request (FR-LITE-4, FR-PROF-5).
#122's I-18 (M3) slice, ``test_i_18__scenario_prompt``, places a scenario story's scenario text
and director notes in the prompt order (FR-LITE-1) and joins this file: traceability counts a
test ID per milestone in one file."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, get_args, get_origin

import annotated_types
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic.fields import FieldInfo
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind
from epistrel.core.errors import BudgetBelowSafetyMinimum, GuardrailViolation
from epistrel.core.fork import Forked, ForkRequest
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import story_guidance
from epistrel.core.settings_schema import StorySettings
from epistrel.core.stories import StorySpec
from epistrel.core.story_settings import update_story_settings
from epistrel.core.summary import included_segments
from epistrel.core.timeline import TruncateRequest
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.lite.pipeline import INPUT_LOCATOR
from epistrel.orchestrator.lite.schemas import RegenerateRequest, RespondRequest
from epistrel.orchestrator.policies.input_gate import EntryPoint, GateText
from epistrel.orchestrator.policies.rebase import output_passes
from epistrel.orchestrator.policies.store import guard_context
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings
from tests.catalog_support import publish as catalog_publish
from tests.catalog_support import save as catalog_save
from tests.concurrency_support import set_instructions, set_pins, summarise
from tests.event_support import execute_in_tx, rows, scalar
from tests.gate_support import input_policies
from tests.policy_support import AUTHOR, OPERATOR, global_policy, story_item, words
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
    system_of,
)
from tests.story_support import READ, WRITE
from tests.story_support import override as override_dials
from tests.summary_support import run_due_jobs, segment_rows, summary_jobs

DERIVED_KIND = OperationKind.CONSOLIDATION.value
SEGMENT_EVENTS = "SELECT count(*) FROM events WHERE story_id = :s AND type = 'lite_summary_segment'"
REJECTED_EVENTS = (
    "SELECT count(*) FROM events WHERE story_id = :s AND type = 'guardrail_output_rejected'"
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
    render characters share one reply; settings resolve deployment → story → request. Since
    #113 the deployment's Lite defaults are the Lite preset's (the deployment's default
    profile), and the story's layer is its overrides of them.

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

        # FR-LITE-4: deployment defaults (Lite's: 0.8, 0.95, medium) → story → request.
        override = {"models": {"generation": {"temperature": 0.6}}}
        tuned, tuned_ids = await new_story(client, PLAYER, FIRST, overrides=override)
        plain, plain_ids = await new_story(client, PLAYER, FIRST)
        cases: list[tuple[str, dict[str, str], dict[str, Any], tuple[Any, ...]]] = [
            (plain, plain_ids, {}, (0.8, 0.95, 400)),
            (tuned, tuned_ids, {}, (0.6, 0.95, 400)),
            (tuned, tuned_ids, {"top_p": 0.7}, (0.6, 0.7, 400)),
            (tuned, tuned_ids, {"length": "long", "temperature": 1.1}, (1.1, 0.95, 800)),
            (tuned, tuned_ids, {"length": 321}, (0.6, 0.95, 321)),
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
            "top_p": 0.95,
            "length": 321,
            "max_tokens": 321,
        }


@pytest.mark.part("M3", "FR-LITE-4; FR-PROF-5")
async def test_i_18__profile_resolution(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Generation settings come from the story's engine profile, then the story's overrides,
    then the request, per parameter (#113). Two stories run under a profile whose generation
    defaults are 0.3, 0.5 and ``short``; one overrides the temperature (0.6). Each turn's
    generation request carries the settings so resolved, the turn records them with the profile,
    its version and the overrides' revision, and the effective configuration states the same
    profile and overrides.

    Complement asserted: a parameter neither the overrides nor the request name falls back to the
    profile's, and removing the override (``null``) brings the profile's temperature back."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        generation = {"temperature": 0.3, "top_p": 0.5, "response_length": "short"}
        created = await client.post(
            "/engine-profiles",
            json={"name": "Quiet", "settings": {"models": {"generation": generation}}},
            headers=WRITE,
        )
        assert created.status_code == 201, created.text
        profile = created.json()["id"]
        override = {"models": {"generation": {"temperature": 0.6}}}
        plain, plain_ids = await new_story(client, PLAYER, FIRST)
        tuned, tuned_ids = await new_story(client, PLAYER, FIRST)
        for story in (plain, tuned):
            assigned = await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": profile, "ignore_warnings": True},
                headers=WRITE,
            )
            assert assigned.status_code == 200, assigned.text
        await override_dials(client, tuned, override)
        cases: list[tuple[str, dict[str, str], dict[str, Any], tuple[Any, ...]]] = [
            (plain, plain_ids, {}, (0.3, 0.5, 150)),
            (tuned, tuned_ids, {}, (0.6, 0.5, 150)),
            (tuned, tuned_ids, {"top_p": 0.7}, (0.6, 0.7, 150)),
            (tuned, tuned_ids, {"length": "long", "temperature": 1.1}, (1.1, 0.5, 800)),
            (tuned, tuned_ids, {"length": 321}, (0.6, 0.5, 321)),
        ]
        for target, who, params, expected in cases:
            stub.reset()
            turn = await played(client, target, body(who[PLAYER], prompt_params=params))
            (sent,) = generation_calls(stub)
            assert (sent.params.temperature, sent.params.top_p, sent.params.max_tokens) == (
                expected
            )
            assert turn["profile"] == {"id": profile, "version": 1}
        recorded = await rows(
            engine,
            "SELECT config_revisions FROM operations WHERE story_id = :s AND kind = 'turn' "
            "ORDER BY started_at DESC LIMIT 1",
            s=tuned,
        )
        configuration = recorded[0].config_revisions
        assert configuration["generation_settings"] == {
            "temperature": 0.6,
            "top_p": 0.5,
            "length": 321,
            "max_tokens": 321,
        }
        assert (
            configuration["profile"],
            configuration["profile_version"],
            configuration["overrides_revision"],
        ) == (profile, 1, 1)
        read = await client.get(f"/stories/{tuned}/effective-config", headers=READ)
        assert read.json()["profile"]["id"] == profile
        assert read.json()["overrides"] == {"settings": override, "revision": 1}

        await override_dials(client, tuned, {"models": {"generation": {"temperature": None}}})
        stub.reset()
        await played(client, tuned, body(tuned_ids[PLAYER]))
        (sent,) = generation_calls(stub)
        assert (sent.params.temperature, sent.params.top_p, sent.params.max_tokens) == (
            0.3,
            0.5,
            150,
        )


@pytest.mark.part("M3", "FR-LITE-1")
async def test_i_18__scenario_prompt(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A story created from a scenario (#122): the captured prompt's ``system`` block holds, in
    order, the rating guidance, the custom instructions, the scenario text (closed by the
    instruction that its facts prevail), the characters' author notes, the director notes,
    the pinned memories and the summary, then the preamble; the opening narration is the
    window's first message, labelled ``Narrator:``. Both scenario blocks are read-set members.

    Complement asserted: after the story's creation, editing and republishing its catalog
    character and its scenario changes nothing in its next prompt's scenario text, author
    notes or director notes."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        document = lg.sunday_dinner(locked, default)
        scenario_id, _ = await g.published_scenario(client, document)
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
            ),
        )
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        await set_instructions(engine, story, "INSTRUCTIONS: keep it at the table.")
        await set_pins(engine, story, "PIN: the gravy boat is chipped.")
        await summarise(engine, story, 1)
        payload = body(by_role["player"], "Smells good.", render_character_ids=[by_role["mother"]])

        stub.reset()
        await played(client, story, payload)
        (call,) = generation_calls(stub)
        system = system_of(call)
        guidance = (await story_guidance(engine, story)).prompt_guidance
        positions = _order(
            system,
            guidance,
            "INSTRUCTIONS: keep it at the table.",
            lg.SCENARIO_HEADING,
            lg.PREVAIL,
            "### Author notes: Sam",
            "### Author notes: June Harrow",
            lg.DIRECTOR_HEADING,
            "PIN: the gravy boat is chipped.",
            "summary through 1",
            "never speak or act for Sam",
        )
        assert positions == sorted(positions)
        assert call.messages[1].role == "assistant"
        assert call.messages[1].content == f"Narrator: {g.OPENING}"
        read_set = (
            await rows(
                engine,
                "SELECT read_set FROM operations WHERE story_id = :s AND kind = 'turn' "
                "AND narrative_turn = 2",
                s=story,
            )
        )[0].read_set
        assert {"scenario_text", "director_notes"} <= set(read_set)

        def blocks(text: str) -> tuple[str, str, str]:
            return (
                lg.section(text, lg.SCENARIO_HEADING, "### Author notes:"),
                lg.section(text, "### Author notes: Sam", lg.DIRECTOR_HEADING),
                lg.section(text, lg.DIRECTOR_HEADING, "## Pinned memories"),
            )

        kept = blocks(system)
        edited = await catalog_save(
            client, mother, 2, definition={"personality": "Wholly changed after the story began."}
        )
        assert edited.status_code == 200, edited.text
        assert (await catalog_publish(client, mother, 3)).status_code == 201
        draft = (await scenarios.read(client, scenario_id, draft=True, projection="full")).json()
        changed = await scenarios.save(
            client,
            scenario_id,
            draft["draft_revision"],
            world={"description": "A different farm entirely."},
            drivers={"director_guidance": "A different plan entirely."},
        )
        assert changed.status_code == 200, changed.text
        republished = await scenarios.publish(client, scenario_id, changed.json()["draft_revision"])
        assert republished.status_code == 201, republished.text
        stub.reset()
        await played(client, story, payload)
        (again,) = generation_calls(stub)
        assert blocks(system_of(again)) == kept


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

        # The instructions path passes the same gate (#60 AC2, #56 AC1; #288).
        instructions = (await client.get(instructions_url, headers=READ)).json()
        op_count = await scalar(
            engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story
        )
        refused = await client.put(
            instructions_url, json={"text": "Praise zarquon always."}, headers=WRITE
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "guardrail_violation"
        assert error["details"]["violations"][0]["locator"] == "text"
        assert (await client.get(instructions_url, headers=READ)).json() == instructions
        assert instructions["text"] == "Speak in rhyme."
        assert await _revision(engine, story) == stored["timeline_revision"]
        assert (
            await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story)
            == event_count
        )
        assert (
            await scalar(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story)
            == op_count
        )

        # No change → nothing written.
        same = await client.put(pins_url, json={"items": [{"text": "PIN B"}]}, headers=WRITE)
        assert same.status_code == 200
        assert same.json()["operation_id"] is None
        assert same.json()["timeline_revision"] == stored["timeline_revision"]


# -- FR-PROF-14: no story setting turns guardrails off (#64) ----------------------------------


def _bound(info: FieldInfo, kind: type, attr: str) -> float | None:
    found = next((m for m in info.metadata if isinstance(m, kind)), None)
    return None if found is None else float(getattr(found, attr))


def _bounded(name: str, info: FieldInfo) -> st.SearchStrategy[Any]:
    """A strategy for a bounded ``int``/``float`` (optionally ``None``) setting, from its
    ``ge``/``gt``/``le`` metadata; any other shape fails, naming the field."""
    args = get_args(info.annotation)
    optional = type(None) in args
    base = (
        next((a for a in args if a is not type(None)), info.annotation) if args else info.annotation
    )
    if get_origin(base) is Annotated:  # ``StrictInt | None`` keeps its ``Strict`` inside (#322)
        base = get_args(base)[0]
    low = _bound(info, annotated_types.Ge, "ge")
    above = _bound(info, annotated_types.Gt, "gt")
    high = _bound(info, annotated_types.Le, "le")
    if base is int and low is not None and high is not None:
        drawn: st.SearchStrategy[Any] = st.integers(int(low), int(high))
    elif base is float and (low is not None or above is not None) and high is not None:
        floor = low if low is not None else (above or 0.0) + 0.5
        drawn = st.floats(floor, high)
    else:
        raise AssertionError(f"StorySettings.{name}: add a strategy to _story_settings")
    return st.none() | drawn if optional else drawn


def _story_settings(cast: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    """Every ``StorySettings`` field at random allowed values (``None`` resets to default),
    derived from the model so a setting added later is covered too (#292)."""
    explicit: dict[str, st.SearchStrategy[Any]] = {
        "turn_concurrency": st.sampled_from(["reject", "queue"]),
        "allow_new_arrivals": st.none() | st.booleans(),
        "default_output_viewpoint": st.none() | st.sampled_from([f"character:{c}" for c in cast]),
    }
    return st.fixed_dictionaries(
        {
            name: explicit[name] if name in explicit else _bounded(name, info)
            for name, info in StorySettings.model_fields.items()
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
    assert all(set(p) == set(StorySettings.model_fields) for p in seen)

    async def retire() -> None:
        engine = make_engine(auth_settings(pg_url))
        try:
            await Engine(auth_settings(pg_url), db=engine).policies_retire(OPERATOR, policy_id)
        finally:
            await engine.dispose()

    asyncio.run(retire())
    asyncio.run(_guardrails_hold(pg_url, story, cast[0], seen[-1], word, expect_caught=False))


OWLS = {"type": "model_assisted", "prompt": "Is this text about OWLS?", "on_uncertain": "pass"}
FLAGGED = '{"violation": true, "confidence": 0.9}'


@pytest.mark.part("M2", "FR-LITE-7; FR-GUARD-9")
async def test_i_18__call_count(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Call accounting with guardrails (#68): with only deterministic policies every turn
    makes exactly one model call (the generation) and reserves none beyond it; one
    model-assisted output policy adds exactly one ``guardrail_check`` (and reserves it); one
    on both input and output adds two; a regeneration adds one ``generation`` and its
    re-check. Every call is in the turn's ``model_calls``.

    Complement asserted: with no story policy defined, no ``guardrail_check`` is made and none
    is reserved."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client, PLAYER, FIRST)
        actor, renders = ids[PLAYER], [ids[FIRST]]

        async def purposes(say: str) -> list[str]:
            stub.reset()
            turn = await played(client, story, body(actor, say))
            assert [c.purpose for c in stub.calls] == [c["purpose"] for c in turn["model_calls"]]
            return [c["purpose"] for c in turn["model_calls"]]

        async def reserves(max_calls: int) -> int | None:
            """The gate with ``max_calls``: ``None`` when it fits, else ``minimum_required``."""
            try:
                await lib.gate_input(
                    AUTHOR,
                    story,
                    EntryPoint.RESPOND,
                    [GateText(INPUT_LOCATOR, "Hello.")],
                    actor=actor,
                    budget=TurnBudget(max_calls=max_calls),
                    reserved=1,
                    reserve_output_for=renders,
                )
            except BudgetBelowSafetyMinimum as refused:
                return int(refused.details["minimum_required"])
            return None

        # No story policy: one generation, nothing reserved beyond it.
        assert await purposes("Hello.") == ["generation"]
        assert await reserves(1) is None

        # Deterministic policies on input and output: still exactly one call, none reserved.
        await lib.policies_set_story(
            AUTHOR,
            story,
            [
                story_item(words("zzinputword"), applies_to=["text_input"]),
                story_item(words("zzoutputword")),
            ],
        )
        assert await purposes("Hello.") == ["generation"]
        assert await reserves(1) is None

        # One model-assisted output policy: +1 check per turn, reserved up front.
        (owls,) = (await lib.policies_set_story(AUTHOR, story, [story_item(OWLS)])).policies
        assert await purposes("Hello.") == ["generation", "guardrail_check"]
        assert await reserves(1) == 2

        # A regeneration: + one generation and its re-check.
        stub.reset()
        stub.when(("OWL DRAFT", "OWLS?"), FLAGGED, purpose="guardrail_check")
        stub.script("generation", ["An OWL DRAFT hoots.", "A quiet night."])
        turn = await played(client, story, body(actor, "Listen."))
        assert turn["message"]["text"] == "A quiet night."
        assert [c["purpose"] for c in turn["model_calls"]] == [
            "generation",
            "guardrail_check",
            "generation",
            "guardrail_check",
        ]

        # The same policy on input and output: +2 per turn.
        both = story_item(
            OWLS, id=owls.id, name=owls.name, applies_to=["text_input", "text_output"]
        )
        await lib.policies_set_story(AUTHOR, story, [both])
        assert sorted(await purposes("Hello.")) == ["generation", *["guardrail_check"] * 2]
        assert await reserves(2) == 3


SUMMARY_SETTINGS = {"lite_summary_chunk": 2}
SUMMARY_OVERRIDES = {
    "memory": {"rolling_summary": {"window_messages": 4, "compress_threshold_tokens": 100}}
}
"""Four verbatim rows (two turns), one turn per level-0 segment, a 100-token cap (the window
and the cap are the story's overrides of its profile since #113)."""
LONG_ONE = "Wren met Ada by the old well and promised to return the brass key. " * 4
LONG_TWO = "Ada showed Wren the hidden door behind the mill and asked for silence. " * 4
MERGED = "Wren met Ada at the well, promised the brass key, and learned of the mill door."


def _shape(found: list[Any]) -> list[tuple[int, int, int, str]]:
    return [(r.covers_from, r.covers_to, r.level, r.membership) for r in found]


@pytest.mark.part("M2", "FR-LITE-2")
async def test_i_18__summary(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The rolling summary (#72): once a whole chunk is older than the verbatim window a job
    is queued (one per story, coalesced); it writes one level-0 segment per chunk, and over the
    token cap the two oldest adjacent segments merge into a level-1 segment that supersedes
    them. The next prompt carries the segment under "Earlier in the story" and the rows after
    it verbatim. Truncating discards the segments past the cut and makes the merged segment's
    surviving child active again; undoing it restores them; regenerating the head turn makes
    the segments positioned there dormant; a fork copies the segments through its turn and
    queues its own job for the turns after them.

    Complements asserted: before the job runs no row leaves the prompt; an old segment is
    discarded or dormant, never deleted (its event and row stay); the source story is untouched
    by its fork's job; and a segment one of whose sources is no longer active is left out of the
    prompt."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(
            client, PLAYER, FIRST, settings=SUMMARY_SETTINGS, overrides=SUMMARY_OVERRIDES
        )
        actor = ids[PLAYER]

        async def turn(say: str) -> dict[str, Any]:
            return await played(client, story, body(actor, say))

        for n in (1, 2):
            await turn(f"line {n}")
        assert await summary_jobs(engine, story) == []  # four rows: nothing is older yet
        await turn("line 3")
        (queued,) = await summary_jobs(engine, story)
        assert (queued.status, queued.coalesce_key) == ("queued", f"lite_summary:{story}")
        await turn("line 4")
        assert [j.id for j in await summary_jobs(engine, story)] == [queued.id]  # coalesced
        assert await segment_rows(engine, story) == []

        stub.reset()
        stub.script("lite_summary", [LONG_ONE, LONG_TWO, MERGED])
        assert await run_due_jobs(engine, lib.models, story) == [queue.CommitOutcome.COMMITTED]
        modes = [c.messages[-1].content.split("\n", 1)[0] for c in stub.calls]
        assert [c.purpose for c in stub.calls] == ["lite_summary"] * 3
        assert modes[0].startswith("Summarise these messages from turns 1:")
        assert modes[2].startswith("Combine these consecutive summaries of turns 1-2")
        found = await segment_rows(engine, story)
        assert _shape(found) == [
            (1, 1, 0, "superseded"),
            (1, 2, 1, "active"),
            (2, 2, 0, "superseded"),
        ]
        one, merged, two = found
        assert [s["operation_id"] for s in merged.source_versions] == [
            one.operation_id,
            two.operation_id,
        ]
        (done,) = await summary_jobs(engine, story)
        assert done.status == "done"
        assert {k: done.result[k] for k in ("segments", "merged", "rejected", "calls")} == {
            "segments": 3,
            "merged": 1,
            "rejected": 0,
            "calls": 3,
        }
        windows = await rows(
            engine,
            "SELECT o.source_window, o.narrative_turn FROM operations o "
            "JOIN lite_summary_segments g ON g.operation_id = o.operation_id "
            "WHERE g.story_id = :s ORDER BY g.covers_from, g.covers_to, g.level",
            s=story,
        )
        assert [(w.source_window, w.narrative_turn) for w in windows] == [
            ({"from_turn": 1, "to_turn": 1}, 1),
            ({"from_turn": 1, "to_turn": 2}, 2),
            ({"from_turn": 2, "to_turn": 2}, 2),
        ]

        stub.reset()
        await turn("line 5")
        (call,) = generation_calls(stub)
        system = system_of(call)
        assert f"## Earlier in the story\nTurns 1-2: {MERGED}" in system
        assert LONG_ONE.strip() not in system
        assert call.messages[1].content == f'{PLAYER}: "line 3"'  # verbatim after turn 2
        shown = await client.get(
            f"/stories/{story}/history",
            params={"include_summary": "1", "viewpoint": "omniscient"},
            headers=READ,
        )
        assert shown.status_code == 200, shown.text
        assert shown.json()["summary"] == [
            {
                "level": 1,
                "covers_from": 1,
                "covers_to": 2,
                "text": MERGED,
                "operation_id": merged.operation_id,
            }
        ]
        assert all(item["kind"] != "lite_summary_segment" for item in shown.json()["items"])

        # A fork copies the segments through its turn and queues its own job for 3 and 4.
        await turn("line 6")
        forked = await lib.fork(AUTHOR, story, ForkRequest(at_turn=6))
        assert isinstance(forked, Forked)
        copy = forked.story_id
        assert _shape(await segment_rows(engine, copy)) == _shape(found)
        assert {r.segment_id for r in await segment_rows(engine, copy)}.isdisjoint(
            r.segment_id for r in found
        )
        assert [j.status for j in await summary_jobs(engine, copy)] == ["queued"]
        stub.script("lite_summary", ["Turn three, briefly.", "Turn four, briefly."])
        await run_due_jobs(engine, lib.models, copy)
        assert _shape(await segment_rows(engine, copy))[-2:] == [
            (3, 3, 0, "active"),
            (4, 4, 0, "active"),
        ]
        assert _shape(await segment_rows(engine, story)) == _shape(found)

        # Truncating to turn 1 discards what covers turn 2; the surviving child is active again.
        await lib.truncate(AUTHOR, story, TruncateRequest(to_turn=1))
        assert _shape(await segment_rows(engine, story)) == [
            (1, 1, 0, "active"),
            (1, 2, 1, "discarded"),
            (2, 2, 0, "discarded"),
        ]
        assert [r.segment_id for r in await segment_rows(engine, story)] == [
            r.segment_id for r in found
        ]
        assert await scalar(engine, SEGMENT_EVENTS, s=story) == 3
        await lib.untruncate(AUTHOR, story)
        assert _shape(await segment_rows(engine, story)) == _shape(found)

        # Regenerating the head turn makes the segments positioned at it dormant.
        await lib.truncate(AUTHOR, story, TruncateRequest(to_turn=2))
        stub.reset()
        await lib.regenerate(AUTHOR, story, RegenerateRequest())
        assert _shape(await segment_rows(engine, story)) == [
            (1, 1, 0, "active"),
            (1, 2, 1, "dormant"),
            (2, 2, 0, "dormant"),
        ]
        (call,) = generation_calls(stub)
        assert f"Turn 1: {LONG_ONE.strip()}" in system_of(call)
        assert MERGED not in system_of(call)
        assert await scalar(engine, SEGMENT_EVENTS, s=story) == 3

        # A segment whose source is no longer active is left out of the prompt.
        async with engine.connect() as conn:
            assert [s.covers_to for s in await included_segments(conn, story)] == [1]
        await execute_in_tx(
            engine,
            (
                "UPDATE operations SET membership = 'dormant' WHERE operation_id = :o",
                {"o": one.source_versions[0]["operation_id"]},
            ),
        )
        async with engine.connect() as conn:
            assert await included_segments(conn, story) == ()


@pytest.mark.part("M2", "FR-LITE-2")
async def test_i_18__summary_policy(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Each new segment passes the story's deterministic output policies before it is
    written (#72): a violating summary is asked for once more; a second violation writes no
    segment, leaves those rows in the prompt verbatim, holds back the later chunks, and keeps
    both texts as ``guardrail_output_rejected`` audit records.

    Complement asserted: when the second attempt is clean the segment is written (with the one
    rejection kept), and the held-back chunk follows."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(
            client, PLAYER, FIRST, settings=SUMMARY_SETTINGS, overrides=SUMMARY_OVERRIDES
        )
        await lib.policies_set_story(AUTHOR, story, [story_item(words("zzbanned"))])
        for n in range(1, 5):
            await played(client, story, body(ids[PLAYER], f"line {n}"))

        stub.script("lite_summary", ["A zzbanned summary.", "Still zzbanned."])
        await run_due_jobs(engine, lib.models, story)
        assert await segment_rows(engine, story) == []
        (job,) = await summary_jobs(engine, story)
        assert (job.status, job.result["segments"], job.result["rejected"]) == ("done", 0, 2)
        rejected = await rows(
            engine,
            "SELECT e.payload, o.kind::text AS kind, e.narrative_turn FROM events e "
            "JOIN operations o ON o.operation_id = e.operation_id "
            "WHERE e.story_id = :s AND e.type = 'guardrail_output_rejected' ORDER BY e.seq",
            s=story,
        )
        assert [(r.payload["text"], r.payload["attempt"]) for r in rejected] == [
            ("A zzbanned summary.", 0),
            ("Still zzbanned.", 1),
        ]
        assert {r.kind for r in rejected} == {DERIVED_KIND}
        stub.reset()
        await played(client, story, body(ids[PLAYER], "line 5"))
        (call,) = generation_calls(stub)
        assert "Earlier in the story" not in system_of(call)
        assert call.messages[1].content == f'{PLAYER}: "line 1"'  # still verbatim

        stub.script("lite_summary", ["Also zzbanned.", "Wren said line one.", "Line two."])
        await run_due_jobs(engine, lib.models, story)
        assert [(r.covers_from, r.text) for r in await segment_rows(engine, story)][:2] == [
            (1, "Wren said line one."),
            (2, "Line two."),
        ]
        assert await scalar(engine, REJECTED_EVENTS, s=story) == 3


@pytest.mark.part("M2", "FR-LITE-7")
async def test_i_18__summary_budget(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Summary compression runs off the turn (FR-LITE-7, #72): a turn played while the
    background job is calling the model reports exactly one model call, its generation, and its
    budget spends one; the ``lite_summary`` calls are made and recorded on no turn.

    Complement asserted: the job's own calls happen (the stub saw them) under its own budget."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(
            client, PLAYER, FIRST, settings=SUMMARY_SETTINGS, overrides=SUMMARY_OVERRIDES
        )
        for n in range(1, 5):
            await played(client, story, body(ids[PLAYER], f"line {n}"))
        stub.reset()
        latch = stub.hold("lite_summary")
        job = asyncio.create_task(run_due_jobs(engine, lib.models, story))
        await asyncio.wait_for(latch.entered.wait(), 10)
        during = await played(client, story, body(ids[PLAYER], "line 5"))
        latch.release()
        outcomes = await asyncio.wait_for(job, 30)
        assert outcomes[0] is queue.CommitOutcome.COMMITTED  # line 5 queued one more after it

        assert [c["purpose"] for c in during["model_calls"]] == ["generation"]
        assert during["call_counts"] == {"generation": 1}
        assert during["budget"]["used"] == 1
        assert sum(c.purpose == "lite_summary" for c in stub.calls) >= 2
        reports = await rows(
            engine,
            "SELECT turn_report FROM operations WHERE story_id = :s AND turn_report IS NOT NULL",
            s=story,
        )
        assert reports
        assert all(
            c["purpose"] != "lite_summary" for r in reports for c in r.turn_report["model_calls"]
        )
