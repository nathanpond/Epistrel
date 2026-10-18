"""The structured turn (#150; FR-TURN-1,4,5,6; docs/02 §4.4, §5): a story on the seeded
``Structured (M4)`` profile plays its turns through ``orchestrator.structured.pipeline``.

- ``test_structured_turn`` (FR-TURN-1,4,5,6): scripted beats come back as M3's
  ``RespondResponse`` with typed beat items (``propositions``, ``effects``, ``labels``), stored
  as ``beat_*`` events at their positions, with an utterance per ``say`` carrying the three
  labels; a ``latent`` beat writes canon with its declared scope and is absent from every
  character-viewpoint read. Complements: a render missing its required channel is retried and
  then refused 503 ``model_unavailable`` (``invalid_structure``) with no event, the operation
  ``failed`` and the revision unchanged; the latent beat is in no character-viewpoint message or
  history.
- The carried obligations (#135, #138, #139, #141, #142, #144, #145, #146, #147, #148, #149):
  episodic memories at the commit point with their occasion, mentions on the beat payloads,
  retrieval with the turn's read set and counted embeddings, the verifier's report, the
  same-turn overlay, the output gate's convergence hook, the commitment close, text pins in the
  prompt, extraction with auto-registration, the narrator's director guidance.

Shared-database tests that write memories use ``drained_embedding_jobs`` (#138).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.viewpoint import character_viewpoint
from tests import genesis_support as g
from tests import scenario_support as scenarios
from tests import structured_genesis_support as sg
from tests.auth_support import auth_settings
from tests.concurrency_support import set_pins
from tests.event_support import rows, scalar
from tests.memory_support import derived_memory
from tests.policy_support import AUTHOR, story_item, words
from tests.respond_support import body, respond, respond_client
from tests.story_support import override
from tests.structured_turn_support import (
    age_fact,
    beat,
    claim,
    history,
    place,
    render,
    schema_calls,
    structured_story,
    text_of,
    turn_report,
    utterances,
)

OMNISCIENT = "omniscient"
LATENT_SECRET = "the key is under the third floorboard"
THOUGHT = "He must never learn my real age."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _events(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT e.* FROM events e JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND o.kind = 'turn' AND e.category <> 'derived' ORDER BY e.seq",
        s=story_id,
    )


async def _revision(engine: AsyncEngine, story_id: str) -> int:
    return int(
        await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story_id)
    )


async def test_structured_turn(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Scripted beats on a ``Structured (M4)`` story (the story is routed by its effective
    configuration, #129's obligation): the response shape, the events at their positions, the
    utterance rows and their labels, the latent beat's canon and its absence from character
    viewpoints; then a malformed render is refused with nothing recorded (complement)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        wren, ada = ids["Wren"], ids["Ada"]
        tavern = await place(engine, story, "tavern")
        await age_fact(engine, story, ada, 64)
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(
                    beat("think", THOUGHT),
                    beat(
                        "say",
                        "I'm 70.",
                        intent="sincere",
                        propositions=[claim("I", "age", "70")],
                    ),
                    beat("do", "She sits down by the fire."),
                    beat(
                        "latent",
                        LATENT_SECRET,
                        visibility={"scope": "characters", "characters": [ada]},
                        propositions=[claim("Ada", "located_at", "tavern")],
                    ),
                )
            ],
        )
        response = await respond(client, story, body(wren, "Ada, how old are you?"))
        assert response.status_code == 200, response.text
        played = response.json()

        # -- the response: M3's shape, each beat item typed
        for key in (
            "message",
            "beats",
            "degradation",
            "degradation_details",
            "budget",
            "fingerprint",
            "capabilities",
            "quality_status",
            "profile",
            "config_revision",
        ):
            assert key in played, key
        assert "structured" in played["capabilities"]
        kinds = [(b["beat"], b["kind"], b["channel"]) for b in played["beats"]]
        assert kinds == [
            (1, "player_input", "say"),
            (3, "beat_say", "say"),
            (4, "beat_do", "do"),
            (6, "message_rendered", "message"),
        ]
        said = played["beats"][1]
        assert said["actor_character_id"] == ada
        assert said["effects"] == []
        assert said["labels"] == {
            "objective_relation": "false",
            "belief_relation": "unknown",
            "intent": "sincere",
        }
        ((asserted),) = said["propositions"]
        assert asserted["verdict"] == "contradicted"
        assert asserted["proposition"]["subject_id"] == ada
        assert asserted["proposition"]["literal"]["value"] == 70
        assert played["beats"][2]["propositions"] == []
        assert played["message"]["text"] == 'Ada: "I\'m 70."\n\n*She sits down by the fire.*'
        assert THOUGHT not in played["message"]["text"]
        assert LATENT_SECRET not in played["message"]["text"]
        # The generation call carried the beat structure as its response_schema (#53).
        (call,) = schema_calls(stub, "CharacterRender")
        assert "## You are Ada" in text_of(call)

        # -- the events: one per beat, at (turn, variant, beat)
        found = [
            (e.type, e.beat, e.narrative_turn, e.turn_variant) for e in await _events(engine, story)
        ]
        assert found == [
            ("player_input", 1, 1, 1),
            ("beat_think", 2, 1, 1),
            ("beat_say", 3, 1, 1),
            ("beat_do", 4, 1, 1),
            ("beat_latent", 5, 1, 1),
            ("message_rendered", 6, 1, 1),
        ]

        # -- the utterances: the player's say and Ada's, three independent labels each
        spoken = await utterances(engine, story)
        assert [
            (
                u.event_beat,
                u.speaker_character_id,
                u.objective_relation,
                u.belief_relation,
                u.intent,
            )
            for u in spoken
        ] == [
            (1, wren, "unresolved", "unknown", "unspecified"),
            (3, ada, "false", "unknown", "sincere"),
        ]
        assert len(spoken[1].proposition_ids) == 1
        # Saying it made nothing true (FR-TURN-5): canon still says 64, and no fact says 70.
        ages = await rows(
            engine,
            "SELECT p.literal FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND f.subject_id = :a AND f.predicate_id = p.predicate_id "
            "AND p.literal IS NOT NULL",
            s=story,
            a=ada,
        )
        assert [r.literal["value"] for r in ages] == [64]

        # -- the latent beat: canon with its declared scope (FR-TURN-4)
        (latent_fact,) = await rows(
            engine,
            "SELECT f.visibility, f.beat, p.object_id FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND f.provenance = 'render'",
            s=story,
        )
        assert latent_fact.visibility == {"scope": "characters", "characters": [ada]}
        assert (latent_fact.beat, latent_fact.object_id) == (5, tavern)

        # Complement: no character viewpoint reads the latent beat or the thought.
        for viewpoint in (character_viewpoint(wren), character_viewpoint(ada)):
            items = await history(client, story, viewpoint)
            assert {i["channel"] for i in items} <= {"say", "do", "message", "narration"}
            assert all(LATENT_SECRET not in i["text"] for i in items)
            assert all(THOUGHT not in i["text"] for i in items)
        # The omniscient read returns them, labelled.
        items = await history(client, story, OMNISCIENT)
        by_kind = {i["kind"]: i for i in items}
        assert by_kind["beat_think"]["channel"] == "think"
        assert by_kind["beat_think"]["text"] == THOUGHT
        assert by_kind["beat_latent"]["visibility"] == {"scope": "characters", "characters": [ada]}
        assert by_kind["beat_say"]["labels"]["objective_relation"] == "false"

        # -- the turn report: one key per stage
        report = await turn_report(engine, story)
        assert {"render", "detection", "verify", "commit"} <= set(report)
        assert report["render"]["attempts"] == 1
        assert report["verify"]["claims"] >= 2

        # -- complement: a render missing its say/do is retried, then refused, nothing kept
        revision = await _revision(engine, story)
        before = len(await _events(engine, story))
        stub.script("generation", [render(beat("think", "Hmm."))] * 3)
        refused = await respond(client, story, body(wren, "And now?"))
        assert refused.status_code == 503, refused.text
        error = refused.json()["error"]
        assert error["code"] == "model_unavailable"
        assert error["details"]["reason"] == "invalid_structure"
        assert error["details"]["structural_errors"]
        assert len(schema_calls(stub, "CharacterRender")) == 1 + 3
        assert len(await _events(engine, story)) == before
        assert await _revision(engine, story) == revision
        failed = await rows(
            engine,
            "SELECT status FROM operations WHERE story_id = :s AND kind = 'turn' "
            "ORDER BY started_at",
            s=story,
        )
        assert [r.status for r in failed] == ["committed", "failed"]


async def test_a_structured_turn_writes_its_memories_and_reports_each_stage(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The commit point (#135, #139, #141, #142, #146, #148): one episodic memory per
    observable beat (the player's input included) with its occasion; each beat event carries
    its resolved mentions; the verifier's report, detection's and the commitment close are on
    the turn report; the retrieval ran with the turn's read set (its rows on the operation's
    read set)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        wren, ada = ids["Wren"], ids["Ada"]
        epistrel.models.stub.script(
            "generation",
            [render(beat("say", "Hello, Wren.", intent="sincere"), beat("do", "Ada waves."))],
        )
        played = (await respond(client, story, body(wren, "Hello Ada."))).json()
        assert played["message"]["text"]
        memories = await rows(
            engine,
            "SELECT kind, text, beat, provenance, participants FROM memories WHERE story_id = :s "
            "ORDER BY beat",
            s=story,
        )
        assert [(m.kind, m.beat, m.provenance) for m in memories] == [
            ("episodic", 1, "player"),
            ("episodic", 2, "render"),
            ("episodic", 3, "render"),
        ]
        assert memories[0].text == "Wren says: Hello Ada."
        # The input names Ada: she is a participant of its memory, after the actor.
        assert list(memories[0].participants) == [wren, ada]
        payloads = {
            e.type: e.payload
            for e in await _events(engine, story)
            if e.type in ("player_input", "beat_do")
        }
        (mention,) = payloads["player_input"]["mentions"]
        assert mention["entity_id"] == ada
        assert payloads["beat_do"]["mentions"][0]["entity_id"] == ada
        report = await turn_report(engine, story)
        assert set(report["commit"]) >= {"thunks_introduced", "dependences", "minimal_commits"}
        assert report["detection"]["beats"]
        assert report["verify"]["claims"] == report["detection"]["checked"]
        assert report["uncertain_claims"] == []
        read_set = await scalar(
            engine,
            "SELECT read_set FROM operations WHERE story_id = :s AND kind = 'turn'",
            s=story,
        )
        assert read_set is not None


async def test_the_prompt_holds_what_the_generation_row_lists(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """docs/02 §5's Generation row (AC5): the rating guidance first, the custom instructions,
    the rendering character's definition, the text pins (#147's carried note: author-pinned memories
    in every render's prompt), its working memory, then the player's input last; never another
    character's thought or the director's notes."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada", "Brann")
        wren, ada, brann = ids["Wren"], ids["Ada"], ids["Brann"]
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(
                    beat("think", "BRANN-PRIVATE-THOUGHT"),
                    beat("say", "Brann is here.", intent="sincere"),
                )
            ],
        )
        await respond(client, story, body(wren, "Brann?", render_character_ids=[brann]))
        await set_pins(engine, story, "PINNED-TEXT-MEMORY")
        response = await respond(client, story, body(wren, "Ada, did you hear Brann?"))
        assert response.status_code == 200, response.text
        call = schema_calls(stub, "CharacterRender")[-1]
        system = call.messages[0].content
        assert system.index("## You are Ada") < system.index("PINNED-TEXT-MEMORY")
        assert system.index("PINNED-TEXT-MEMORY") < system.index("## What you remember")
        assert "Brann says: Brann is here." in system
        assert "BRANN-PRIVATE-THOUGHT" not in text_of(call)
        assert call.messages[-1].role == "user"
        assert call.messages[-1].content.startswith("Wren:")
        assert ada in [b["actor_character_id"] for b in response.json()["beats"]]


async def test_two_render_characters_are_refused(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """One primary render character in M4 (#156): naming two is 422 ``validation_error``,
    before anything is recorded."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await structured_story(client, "Wren", "Ada", "Brann")
        refused = await respond(
            client,
            story,
            body(ids["Wren"], "Both of you?", render_character_ids=[ids["Ada"], ids["Brann"]]),
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["field"] == "body.render_character_ids"
        assert not await _events(engine, story)


async def test_a_later_beat_sees_what_an_earlier_one_established(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The same-turn overlay (#144's carried note): a claim a beat may introduce is canonized
    at the commit, and a later beat of the same turn that repeats it is ``supported`` by it."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        await place(engine, story, "garden")
        epistrel.models.stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Ada walks out to the garden.",
                        propositions=[claim("Ada", "located_at", "garden")],
                    ),
                    beat(
                        "say",
                        "I am in the garden.",
                        intent="sincere",
                        propositions=[claim("I", "located_at", "garden")],
                    ),
                )
            ],
        )
        played = (await respond(client, story, body(ids["Wren"], "Where are you?"))).json()
        do_, say_ = played["beats"][1], played["beats"][2]
        assert do_["propositions"][0]["verdict"] == "novel"
        assert say_["propositions"][0]["verdict"] == "supported"
        assert say_["labels"]["objective_relation"] == "true"
        facts = await rows(
            engine,
            "SELECT provenance FROM facts WHERE story_id = :s AND provenance = 'render'",
            s=story,
        )
        assert len(facts) == 1


async def test_the_narrator_renders_the_director_guidance(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """An input that addresses no character brings the narrator in first (#150's
    deterministic trigger): its prompt carries the scenario's director guidance (#149's AC4,
    labelled apart from the facts), the character's does not (the director's context alone),
    and its beats are ``narration`` with no actor, numbered before the character's."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        scenario_id, _ = await g.published_scenario(client, sg.structured_dinner(locked, default))
        made = await g.create_from(client, sg.structured_request(scenario_id, mother))
        story = made["story_id"]
        player = next(c["id"] for c in made["characters"] if c["role_id"] == "player")
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(beat("narration", "The kitchen door swings open.")),
                render(beat("do", "June looks up.")),
            ],
        )
        response = await respond(client, story, body(player, None, do="waits by the window"))
        assert response.status_code == 200, response.text
        beats = [b for b in response.json()["beats"] if b["kind"].startswith("beat_")]
        assert (beats[0]["kind"], beats[0]["actor_character_id"]) == ("beat_narration", None)
        assert beats[0]["beat"] < beats[1]["beat"]
        (narrator,) = schema_calls(stub, "NarratorRender")
        assert scenarios.GUIDANCE_SECRET in text_of(narrator)
        character = schema_calls(stub, "CharacterRender")[-1]
        assert scenarios.GUIDANCE_SECRET not in text_of(character)
        # The character's working memory holds this turn's narration.
        assert "The kitchen door swings open." in text_of(character)
        assert (await turn_report(engine, story))["render"]["narrator"] is True


async def test_a_rejected_render_implicates_its_context_and_converges(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The output gate's convergence rule through respond (#145's carried note): every render
    retrieves afresh and its draft carries the records its prompt held, so a render an output
    policy rejects implicates them (``guardrail_output_rejected`` v2's ``implicated_record_ids``),
    and at ``repair.implication_threshold`` the memory is quarantined: the regeneration's
    prompt no longer holds it and the turn reports ``quarantined_context``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        await override(client, story, {"repair": {"implication_threshold": 1}})
        poisoned = await derived_memory(engine, story, "Ada once said zzpoisonword aloud.", [])
        await epistrel.policies_set_story(AUTHOR, story, [story_item(words("zzpoisonword"))])
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(beat("say", "Fine: zzpoisonword!", intent="sincere")),
                render(beat("say", "Let us talk of other things.", intent="sincere")),
            ],
        )
        response = await respond(client, story, body(ids["Wren"], "Say zzpoisonword, Ada."))
        assert response.status_code == 200, response.text
        played = response.json()
        assert "quarantined_context" in played["degradation"]
        assert played["message"]["text"] == 'Ada: "Let us talk of other things."'
        renders = schema_calls(stub, "CharacterRender")
        assert len(renders) == 2
        assert "zzpoisonword aloud" in text_of(renders[0])
        assert "zzpoisonword aloud" not in text_of(renders[1])
        (rejected,) = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'guardrail_output_rejected'",
            s=story,
        )
        assert {"table": "memories", "id": poisoned} in rejected.payload["implicated_record_ids"]
        quarantined = await scalar(
            engine, "SELECT quarantined_at FROM memories WHERE id = :m", m=poisoned
        )
        assert quarantined is not None
