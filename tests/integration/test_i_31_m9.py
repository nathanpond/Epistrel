"""I-31's M9 parts: benching and reactivating a story character (FR-MULTI-13; #831).

``POST /stories/{id}/characters/{cid}/bench`` (``mode: sequestered``, the default) and
``…/reactivate``. A benched character keeps its partition and memories, takes no part in any
turn (no render, no role, no place in another character's prompt; a turn request naming it is
409 ``character_benched``) and does not count toward ``max_story_cast``. Sequestered, it perceives
nothing: no exposure, belief or perceived memory is written for it while it is benched. A purpose
call from its viewpoint (``store_as_memory`` included) and the operator's belief writes still
reach it. The bench is positional: an inter-turn slot operation that bumps the timeline
revision, so truncation, untruncation and forks carry it like any slot write. On a Lite story
benching is cast exclusion only: a reactivated character's prompt includes the transcript from
its time away (documented behaviour).

Every I-31 M9 part lives in this file (the traceability gate takes one file per test ID and
milestone).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import turn_gate
from epistrel.core.context import RequestContext
from epistrel.core.errors import AlreadyBenched, CharacterBenched, InvalidRequest, NotBenched
from epistrel.core.ids import new_ulid
from epistrel.core.propositions import store
from epistrel.core.stories import BenchRequest, CharacterWeightRequest
from epistrel.engine import Engine
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings, bearer
from tests.canon_support import auto_predicate
from tests.event_support import rows
from tests.integration.test_i_29_m9 import (
    MEMORY_SCHEMA,
    MEMORY_TEMPLATE,
    REMEMBERED,
    partitioned_story,
    prompt_of,
    purpose_body,
    remembered,
    store_body,
    story_call,
    unique,
)
from tests.respond_support import body, new_story, respond, respond_client
from tests.story_support import WRITE, character, create
from tests.structured_turn_support import beat, claim, render, schema_calls, structured_story

pytestmark = pytest.mark.integration

OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}
LIBRARY = RequestContext(principal="p-author")
NOTHING_SALIENT = json.dumps({"salient": [], "texture": []})
SetLimit = Callable[[int], Awaitable[None]]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def served(
    stub_settings: Settings, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    async with respond_client(stub_settings, engine) as found:
        yield found


@pytest.fixture
async def cast_limit(
    served: tuple[httpx.AsyncClient, Engine],
) -> AsyncIterator[SetLimit]:
    """Sets ``max_story_cast`` through ``PUT /admin/settings``; the value it found is put back
    afterwards (I-28's fixture)."""
    client, _ = served
    start = (await client.get("/admin/settings", headers=bearer())).json()["max_story_cast"]

    async def set_limit(value: int) -> None:
        response = await client.put(
            "/admin/settings", json={"max_story_cast": value}, headers=OPERATOR
        )
        assert response.status_code == 200, response.text

    try:
        yield set_limit
    finally:
        await set_limit(start)


@pytest.fixture
async def forgetting(engine: AsyncEngine) -> AsyncIterator[None]:
    """Takes back every story override of ``character_knowledge`` and ``perception`` after the
    test (``testimony_support.forget``), as the testimony tests do."""
    try:
        yield
    finally:
        await ts.forget(engine)


# -- helpers ----------------------------------------------------------------------------------


async def bench(
    client: httpx.AsyncClient, story: str, character_id: str, payload: Any = None, **headers: str
) -> httpx.Response:
    path = f"/stories/{story}/characters/{character_id}/bench"
    if payload is None:
        return await client.post(path, headers={**WRITE, **headers})
    return await client.post(path, json=payload, headers={**WRITE, **headers})


async def reactivate(
    client: httpx.AsyncClient, story: str, character_id: str, **headers: str
) -> httpx.Response:
    return await client.post(
        f"/stories/{story}/characters/{character_id}/reactivate", headers={**WRITE, **headers}
    )


def refusal(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    details = {k: v for k, v in error.get("details", {}).items() if k != "errors"}
    return response.status_code, error["code"], details


async def story_read(client: httpx.AsyncClient, story: str) -> dict[str, Any]:
    found = await client.get(f"/stories/{story}", headers=WRITE)
    assert found.status_code == 200, found.text
    result: dict[str, Any] = found.json()
    return result


def char_of(read: dict[str, Any], character_id: str) -> dict[str, Any]:
    return next(c for c in read["characters"] if c["id"] == character_id)


async def revisions(engine: AsyncEngine, story: str) -> tuple[int, int]:
    (found,) = await rows(
        engine,
        "SELECT timeline_revision, config_revision FROM stories WHERE id = :s",
        s=story,
    )
    return int(found.timeline_revision), int(found.config_revision)


async def bench_rows(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT character_id, mode, benched, previous_role, previous_fixed, narrative_turn, "
        "slot_k, membership::text AS membership, operation_id, source_event_ids "
        "FROM character_bench WHERE story_id = :s ORDER BY narrative_turn, slot_k, id",
        s=story,
    )


async def exposures_of(engine: AsyncEngine, story: str, witness: str) -> int:
    (found,) = await rows(
        engine,
        "SELECT count(*) AS n FROM exposures WHERE story_id = :s AND witness_character_id = :w",
        s=story,
        w=witness,
    )
    return int(found.n)


async def heard_by(engine: AsyncEngine, story: str, holder: str, words: str) -> list[Any]:
    """``holder``'s active routed memories whose text holds ``words``."""
    return [m for m in await ps.memories_of(engine, story, holder) if words in (m.text or "")]


# -- the round trip ---------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_round_trip(
    served: tuple[httpx.AsyncClient, Engine], engine: AsyncEngine
) -> None:
    """Bench Ada (fixed ``primary``) with no body: 200 with the character, its ``bench``
    ``{mode: sequestered, since_turn: 0}`` and the timeline revision one higher; the config
    revision does not move and no ``config_changes`` row is written. The bench is an
    ``inter_turn`` slot operation at ``0⁺1`` recording ``character_benched`` (previous role
    ``primary``, fixed) and one active ``character_bench`` version citing it. The story read
    shows the bench and ``cast {active: 2, benched: 1}``. Reactivation answers ``bench: null``,
    restores the role, records ``character_reactivated`` at ``0⁺2`` and the read no longer
    lists a bench (complement)."""
    client, _ = served
    story, ids = await structured_story(client, "Wren", "Ada", "Bo")
    ada = ids["Ada"]
    role = await client.patch(
        f"/stories/{story}/characters/{ada}/participation",
        json={"role": "primary", "fixed": True},
        headers=WRITE,
    )
    assert role.status_code == 200, role.text
    timeline, config = await revisions(engine, story)

    benched = await bench(client, story, ada)
    assert benched.status_code == 200, benched.text
    answer = benched.json()
    assert answer["bench"] == {"mode": "sequestered", "since_turn": 0}
    assert answer["character"]["bench"] == {"mode": "sequestered", "since_turn": 0}
    assert answer["character"]["id"] == ada
    assert answer["timeline_revision"] == timeline + 1
    assert await revisions(engine, story) == (timeline + 1, config)
    (version,) = await bench_rows(engine, story)
    assert (version.character_id, version.mode, version.benched) == (ada, "sequestered", True)
    assert (version.previous_role, version.previous_fixed) == ("primary", True)
    assert (version.narrative_turn, version.slot_k, version.membership) == (0, 1, "active")
    (op,) = await rows(
        engine,
        "SELECT kind::text AS kind, narrative_turn, slot_k FROM operations WHERE operation_id = :o",
        o=version.operation_id,
    )
    assert (op.kind, op.narrative_turn, op.slot_k) == ("inter_turn", 0, 1)
    (event,) = await rows(
        engine,
        "SELECT id, type, category::text AS category, payload FROM events WHERE operation_id = :o",
        o=version.operation_id,
    )
    assert (event.type, event.category) == ("character_benched", "control")
    assert event.payload == {
        "character_id": ada,
        "mode": "sequestered",
        "previous_role": "primary",
        "previous_fixed": True,
    }
    assert version.source_event_ids == [event.id]
    changes = await rows(
        engine,
        "SELECT setting FROM config_changes WHERE scope_id = :s AND setting LIKE 'participation:%'",
        s=story,
    )
    assert len(changes) == 1  # the role set above; the bench writes none

    read = await story_read(client, story)
    assert char_of(read, ada)["bench"] == {"mode": "sequestered", "since_turn": 0}
    assert "bench" not in char_of(read, ids["Bo"])
    assert read["cast"] == {"active": 2, "benched": 1}

    back = await reactivate(client, story, ada)
    assert back.status_code == 200, back.text
    assert back.json()["bench"] is None
    assert "bench" not in back.json()["character"]
    assert back.json()["character"]["participation"] == {"role": "primary", "fixed": True}
    assert back.json()["timeline_revision"] == timeline + 2
    _, second = await bench_rows(engine, story)
    assert (second.benched, second.mode, second.slot_k) == (False, "sequestered", 2)
    (event,) = await rows(
        engine,
        "SELECT type, payload FROM events WHERE operation_id = :o",
        o=second.operation_id,
    )
    assert event.type == "character_reactivated"
    assert event.payload == {
        "character_id": ada,
        "mode": "sequestered",
        "restored_role": "primary",
        "restored_fixed": True,
    }
    read = await story_read(client, story)
    assert "bench" not in char_of(read, ada)
    assert read["cast"] == {"active": 3, "benched": 0}

    again = await bench(client, story, ada, {"mode": "sequestered"})
    assert again.status_code == 200, again.text
    assert again.json()["bench"] == {"mode": "sequestered", "since_turn": 0}


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_refusals(
    served: tuple[httpx.AsyncClient, Engine], engine: AsyncEngine
) -> None:
    """Each refusal and its body, nothing written by any: 404 for an unknown story or
    character; 422 ``player_not_benchable`` (``field: character_id``); 409 ``already_benched``
    ``{character_id, mode, since_turn}``; 409 ``not_benched``; 409 ``character_benched`` for a
    participation or weight write on a benched character; 423 ``story_busy`` while a turn holds
    the story's turn slot; 409 ``story_archived`` before the 422 on an archived story (the
    precedence)."""
    client, _ = served
    story, ids = await structured_story(client, "Wren", "Ada", "Bo")
    wren, ada, bo = ids["Wren"], ids["Ada"], ids["Bo"]

    missing_story = await bench(client, new_ulid(), ada)
    assert missing_story.status_code == 404, missing_story.text
    missing = await bench(client, story, new_ulid())
    assert missing.status_code == 404, missing.text
    assert refusal(await reactivate(client, story, new_ulid()))[0] == 404

    before = await revisions(engine, story)
    assert refusal(await bench(client, story, wren)) == (
        422,
        "validation_error",
        {"field": "character_id", "reason": "player_not_benchable"},
    )
    assert refusal(await reactivate(client, story, bo)) == (
        409,
        "not_benched",
        {"character_id": bo},
    )
    assert await revisions(engine, story) == before
    assert await bench_rows(engine, story) == []

    assert (await bench(client, story, ada)).status_code == 200
    assert refusal(await bench(client, story, ada)) == (
        409,
        "already_benched",
        {"character_id": ada, "mode": "sequestered", "since_turn": 0},
    )
    weight = await client.post(
        f"/stories/{story}/characters/{ada}/weight", json={"weight": "minor"}, headers=WRITE
    )
    assert refusal(weight) == (409, "character_benched", {"character_id": ada})
    role = await client.patch(
        f"/stories/{story}/characters/{ada}/participation",
        json={"role": "silent", "fixed": False},
        headers=WRITE,
    )
    assert refusal(role) == (409, "character_benched", {"character_id": ada})
    assert len(await bench_rows(engine, story)) == 1

    holder = new_ulid()
    held = await turn_gate.acquire(engine, story, holder, lease_s=60)
    assert held is None
    try:
        busy = await bench(client, story, bo)
        assert busy.status_code == 423, busy.text
        assert busy.json()["error"]["code"] == "story_busy"
        late = await reactivate(client, story, ada)
        assert late.status_code == 423, late.text
    finally:
        await turn_gate.release_now(engine, story, holder)
    assert len(await bench_rows(engine, story)) == 1

    archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
    assert archived.status_code == 200, archived.text
    try:
        assert refusal(await bench(client, story, wren))[:2] == (409, "story_archived")
        assert refusal(await reactivate(client, story, ada))[:2] == (409, "story_archived")
    finally:
        unarchived = await client.post(f"/stories/{story}/unarchive", headers=WRITE)
        assert unarchived.status_code == 200, unarchived.text


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_cast_count(
    served: tuple[httpx.AsyncClient, Engine], cast_limit: SetLimit
) -> None:
    """The cast cap counts unbenched full characters only: at a limit of 3 with Ada benched, a
    4th character (Dale) is added; reactivating Ada is then 422 ``cast_max`` (``limit`` 3,
    ``actual`` 4) and leaves her benched. Demoting Bo to ``minor`` frees the place, and the
    reactivation succeeds. A benched ``minor`` is reactivated whatever the count."""
    client, _ = served
    story = await create(
        client,
        {"characters": [character("Wren", is_player=True), character("Ada"), character("Bo")]},
    )
    sid = story["story_id"]
    ids = {c["name"]: c["id"] for c in story["characters"]}
    await cast_limit(3)
    assert (await bench(client, sid, ids["Ada"])).status_code == 200
    added = await client.post(
        f"/stories/{sid}/characters",
        json={"name": "Dale", "definition": "A driver.", "true_age": 52},
        headers=WRITE,
    )
    assert added.status_code == 201, added.text
    assert (await story_read(client, sid))["cast"] == {"active": 3, "benched": 1}
    assert refusal(await reactivate(client, sid, ids["Ada"])) == (
        422,
        "validation_error",
        {"field": "body", "cap": "cast_max", "limit": 3, "actual": 4},
    )
    assert char_of(await story_read(client, sid), ids["Ada"])["bench"]["mode"] == "sequestered"

    demoted = await client.post(
        f"/stories/{sid}/characters/{ids['Bo']}/weight", json={"weight": "minor"}, headers=WRITE
    )
    assert demoted.status_code == 200, demoted.text
    assert (await bench(client, sid, ids["Bo"])).status_code == 200
    back = await reactivate(client, sid, ids["Ada"])
    assert back.status_code == 200, back.text
    assert (await story_read(client, sid))["cast"] == {"active": 3, "benched": 1}
    minor = await reactivate(client, sid, ids["Bo"])
    assert minor.status_code == 200, minor.text


# -- turns ------------------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_out_of_turns(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """In a structured story with Wren, Ada and Bo in one room and Ada benched, a turn with no
    render ids renders Bo, not Ada (the default selection skips her): no render call is hers,
    and no captured prompt carries her definition or her id (no ``## Others present`` line).
    Complement: before the bench, the same turn renders Ada. Every field that names her is 409
    ``character_benched`` ``{character_id}``: the actor, ``render_character_ids``, a
    ``character:<id>`` output viewpoint, ``whisper_to``, ``characters_present`` and a role
    override; nothing is written."""
    client, lib = served
    stub = lib.models.stub
    story, ids, _ = await ts.room(client, engine, "Wren", "Ada", "Bo")
    wren, ada = ids["Wren"], ids["Ada"]
    definition = "Ada, as the cast sheet describes them."

    stub.reset()
    stub.script("generation", [render(beat("do", "Someone nods."))])
    first = await respond(client, story, body(wren, "Hello?"))
    assert first.status_code == 200, first.text
    (rendered,) = schema_calls(stub, "CharacterRender")
    assert "## You are Ada" in "\n".join(m.content for m in rendered.messages)

    assert (await bench(client, story, ada)).status_code == 200
    stub.reset()
    stub.script("generation", [render(beat("do", "Bo nods."))])
    second = await respond(client, story, body(wren, "Anyone?"))
    assert second.status_code == 200, second.text
    assert len(schema_calls(stub, "CharacterRender")) == 1
    sent = "\n".join(m.content for call in stub.calls for m in call.messages)
    assert definition not in sent
    assert ada not in sent
    assert "## You are Bo" in sent

    before = await revisions(engine, story)
    named: list[dict[str, Any]] = [
        body(ada, "I am back."),
        body(wren, "Ada?", render_character_ids=[ada]),
        body(wren, "Ada?", output_viewpoint=f"character:{ada}"),
        body(wren, "psst", player_input={"say": "psst", "whisper_to": [ada]}),
        body(wren, "Hi.", scene_context={"characters_present": [ada]}),
        body(wren, "Hi.", roles={ada: "primary"}),
    ]
    for payload in named:
        refused = await respond(client, story, payload)
        assert refusal(refused) == (409, "character_benched", {"character_id": ada}), payload
    assert await revisions(engine, story) == before


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_sequestered(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """Bobby is in the room, benched ``sequestered``, when Jenni tells it that Olivia sent the
    text. No exposure row, no belief and no routed memory is written for him (the absence of
    the rows, not only of a prompt line). After his reactivation, a render of Bobby asked about
    it holds nothing of it. Complement: the same scenario without the bench leaves Bobby an
    exposure and a routed memory, and his later context says he heard Jenni say it."""
    client, lib = served

    async def scene(benched: bool) -> tuple[str, dict[str, str], str, str]:
        story, ids, things = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
        await ts.enable_testimony(engine, story, "defaulted", salience="rules")
        bobby = ids["Bobby"]
        if benched:
            assert (await bench(client, story, bobby)).status_code == 200
        stub = lib.models.stub
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        ts.CLAIM_LINE,
                        intent="sincere",
                        propositions=[claim("Olivia", "sent", "the text")],
                    )
                )
            ],
        )
        stub.script("salience_detection", [NOTHING_SALIENT])
        told = await respond(
            client, story, body(ids["Wren"], "What happened?", render_character_ids=[ids["Jenni"]])
        )
        assert told.status_code == 200, told.text
        if benched:
            assert await exposures_of(engine, story, bobby) == 0
            assert await heard_by(engine, story, bobby, "Olivia") == []
            assert [
                b
                for b in await ts.beliefs_of(engine, story, bobby)
                if b.predicate_id == things["sent"]
            ] == []
            assert (await reactivate(client, story, bobby)).status_code == 200
        stub.reset()
        stub.script("generation", [render(beat("do", "Bobby shrugs."))])
        stub.script("salience_detection", [NOTHING_SALIENT])
        asked = await respond(
            client,
            story,
            body(ids["Wren"], "Bobby, did Olivia send the text?", render_character_ids=[bobby]),
        )
        assert asked.status_code == 200, asked.text
        (call,) = schema_calls(stub, "CharacterRender")
        prompt = "\n".join(m.content for m in call.messages)
        return story, ids, things["sent"], prompt

    _, _, _, benched_prompt = await scene(benched=True)
    assert "you heard Jenni say Olivia sent the text" not in benched_prompt
    assert ts.CLAIM_LINE not in benched_prompt

    story, ids, _, prompt = await scene(benched=False)
    assert await exposures_of(engine, story, ids["Bobby"]) >= 1
    assert await heard_by(engine, story, ids["Bobby"], "Olivia") != []
    assert "you heard Jenni say Olivia sent the text" in prompt


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_history(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """Bobby hears Jenni in turn 1, is benched at ``1⁺1``, and misses what she says in turn 2.
    A fork at turn 2 keeps him benched; a fork at turn 1 (before the bench) does not. Truncating
    to turn 1 discards the bench in that turn's slot (he is unbenched), and untruncating
    re-applies it. After his reactivation he still holds his turn 1 memory and nothing of turn
    2."""
    client, lib = served
    stub = lib.models.stub
    story, ids, _ = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
    await ps.perceive(engine, story, "full")
    wren, jenni, bobby = ids["Wren"], ids["Jenni"], ids["Bobby"]

    async def jenni_says(line: str) -> None:
        stub.reset()
        stub.script("generation", [render(beat("say", line))])
        stub.script("salience_detection", [NOTHING_SALIENT])
        said = await respond(client, story, body(wren, "Well?", render_character_ids=[jenni]))
        assert said.status_code == 200, said.text

    await jenni_says("The kettle is MAGENTA-ONE.")
    assert await heard_by(engine, story, bobby, "MAGENTA-ONE") != []
    assert (await bench(client, story, bobby)).json()["bench"]["since_turn"] == 1
    await jenni_says("The garden is MAGENTA-TWO.")
    assert await heard_by(engine, story, bobby, "MAGENTA-TWO") == []

    async def fork_bench(at_turn: int) -> Any:
        forked = await client.post(
            f"/stories/{story}/fork", json={"at_turn": at_turn}, headers=WRITE
        )
        assert forked.status_code == 201, forked.text
        read = await story_read(client, forked.json()["story_id"])
        found = next(c for c in read["characters"] if c["name"] == "Bobby")
        return found.get("bench")

    assert await fork_bench(2) == {"mode": "sequestered", "since_turn": 1}
    assert await fork_bench(1) is None

    cut = await client.post(f"/stories/{story}/truncate", json={"to_turn": 1}, headers=WRITE)
    assert cut.status_code == 200, cut.text
    assert "bench" not in char_of(await story_read(client, story), bobby)
    assert [r.membership for r in await bench_rows(engine, story)] == ["discarded"]
    restored = await client.post(f"/stories/{story}/untruncate", headers=WRITE)
    assert restored.status_code == 200, restored.text
    assert char_of(await story_read(client, story), bobby)["bench"]["since_turn"] == 1

    assert (await reactivate(client, story, bobby)).status_code == 200
    assert await heard_by(engine, story, bobby, "MAGENTA-ONE") != []
    assert await heard_by(engine, story, bobby, "MAGENTA-TWO") == []


# -- deliberate writes ------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_deliberate_writes(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """Bench Briar ``sequestered``. A purpose call from her viewpoint answers 200 and stores her
    memory (``store_as_memory``), and the operator's belief write for her lands (201). After
    her reactivation, a viewpoint call's prompt for Briar (a template showing only
    ``viewpoint.text``) holds both: the memory and the belief that Olivia sent the text."""
    client, lib = served
    name, look = unique("juror"), unique("juror_view")
    for purpose, template in ((name, MEMORY_TEMPLATE), (look, "{{ viewpoint.text }}")):
        made = await client.post(
            "/admin/purposes",
            json=purpose_body(purpose, template=template, output_schema=MEMORY_SCHEMA),
            headers=OPERATOR,
        )
        assert made.status_code == 201, made.text
    try:
        story, ids = await partitioned_story(client)
        briar = ids["Briar"]
        async with engine.begin() as conn:
            olivia = await store.add_entity(conn, story, "person", "Olivia")
            item = await store.add_entity(conn, story, "item", "the text")
        sent = await auto_predicate(engine, story, "sent")
        assert (await bench(client, story, briar)).status_code == 200

        lib.models.stub.script(name, [remembered()])
        stored = await story_call(client, story, name, store_body(briar))
        assert stored.status_code == 200, stored.text
        assert stored.json()["memory_id"] is not None
        proposition = ts.sent({"Olivia": olivia, "item": item, "sent": sent})
        believed = await client.post(
            f"/admin/stories/{story}/beliefs",
            json={"holder": briar, "proposition": proposition.model_dump(mode="json")},
            headers=OPERATOR,
        )
        assert believed.status_code == 201, believed.text

        assert (await reactivate(client, story, briar)).status_code == 200
        lib.models.stub.script(look, [remembered("Again.")])
        question = {
            "inputs": {"target": f"the {REMEMBERED} pact and the text"},
            "viewpoint_character_id": briar,
        }
        again = await story_call(client, story, look, question)
        assert again.status_code == 200, again.text
        prompt = prompt_of(lib, look)
        assert f"{REMEMBERED} pact." in prompt
        assert "Olivia sent the text" in prompt
    finally:
        for purpose in (name, look):
            archived = await client.post(f"/admin/purposes/{purpose}/archive", headers=OPERATOR)
            assert archived.status_code == 200, archived.text


# -- Lite -------------------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_lite(served: tuple[httpx.AsyncClient, Engine]) -> None:
    """On a Lite story, benching is cast exclusion only. With Ada benched, a turn renders Bo
    alone: no author notes for Ada and her id in no request. Naming her is 409. After her
    reactivation, a turn rendering Ada carries the reply from her time away in its transcript
    window: Lite has no per-character perception, so she knows the transcript (documented
    behaviour)."""
    client, lib = served
    stub = lib.models.stub
    story, ids = await new_story(client, "Wren", "Ada", "Bo")
    wren, ada = ids["Wren"], ids["Ada"]
    assert (await bench(client, story, ada)).status_code == 200

    stub.reset()
    stub.script("generation", ["Bo: The road is AMBER-AWAY."])
    away = await respond(client, story, body(wren, "Where to?"))
    assert away.status_code == 200, away.text
    sent = "\n".join(m.content for call in stub.calls for m in call.messages)
    assert "### Author notes: Ada" not in sent
    assert "### Author notes: Bo" in sent
    assert ada not in sent
    named = await respond(client, story, body(wren, "Ada?", render_character_ids=[ada]))
    assert refusal(named) == (409, "character_benched", {"character_id": ada})

    assert (await reactivate(client, story, ada)).status_code == 200
    stub.reset()
    stub.script("generation", ["Ada: I am back."])
    back = await respond(client, story, body(wren, "Ada?", render_character_ids=[ada]))
    assert back.status_code == 200, back.text
    sent = "\n".join(m.content for call in stub.calls for m in call.messages)
    assert "### Author notes: Ada" in sent
    assert "AMBER-AWAY" in sent


# -- the library ------------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-13")
async def test_i_31__m9_library(served: tuple[httpx.AsyncClient, Engine]) -> None:
    """``Engine.bench_character`` and ``Engine.reactivate_character`` do what the routes do,
    and raise the same errors: ``AlreadyBenched``, ``NotBenched``, ``InvalidRequest``
    (``player_not_benchable``) and ``CharacterBenched`` for a weight write."""
    client, lib = served
    story, ids = await new_story(client, "Wren", "Ada", "Bo")
    ada = ids["Ada"]
    done = await lib.bench_character(LIBRARY, story, ada)
    assert done.character.bench == {"mode": "sequestered", "since_turn": 0}
    with pytest.raises(AlreadyBenched):
        await lib.bench_character(LIBRARY, story, ada, BenchRequest(mode="sequestered"))
    with pytest.raises(InvalidRequest) as player:
        await lib.bench_character(LIBRARY, story, ids["Wren"])
    assert player.value.details["reason"] == "player_not_benchable"
    with pytest.raises(CharacterBenched):
        await lib.set_character_weight(LIBRARY, story, ada, CharacterWeightRequest(weight="minor"))
    back = await lib.reactivate_character(LIBRARY, story, ada)
    assert back.character.bench is None
    assert back.timeline_revision == done.timeline_revision + 1
    with pytest.raises(NotBenched):
        await lib.reactivate_character(LIBRARY, story, ada)
