"""I-32's M9 parts: perception while benched (FR-MULTI-14; #832).

A character benched ``present`` (``POST …/bench`` ``{mode: "present"}``, or ``PATCH …/bench`` to
switch a benched character's mode) stays in the scene: it perceives what happens where it is
under the normal presence and location rules, exactly as an unbenched ``silent`` character there
would, and the other characters' scene line and the scene read name it. It still never speaks:
no render, no role, no cast definition, no beat of its own, a turn naming it is 409
``character_benched``, and the narrator's and each primary's prompt carries "Present but not
taking part: <names>; never give them dialogue". Verification permits the narrator's or a
primary's description of its non-speech actions, and nothing more. It does not count toward
``max_story_cast``. On a Lite story the prompt line is its only effect.

The perception tests run at perception ``co_location``. Every I-32 M9 part lives in this file
(the traceability gate takes one file per test ID and milestone).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import NotBenched
from epistrel.core.ids import new_ulid
from epistrel.core.stories import BenchModeRequest, BenchRequest
from epistrel.core.verify import permission as pm
from epistrel.core.verify.permission import (
    Asserter,
    Basis,
    Denied,
    PermissionFacts,
    Permitted,
    Reason,
)
from epistrel.engine import Engine
from tests import multi_support as ms
from tests import reactive_support as rs
from tests import testimony_support as ts
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.integration.test_i_31_m9 import (
    bench,
    bench_rows,
    char_of,
    exposures_of,
    heard_by,
    reactivate,
    refusal,
    revisions,
    story_read,
)
from tests.respond_support import body, new_story, respond, respond_client
from tests.scenario.test_s_novelty_permission import (
    BOBBY,
    CAL,
    CAMP,
    ENTITIES,
    JENNI,
    KITCHEN,
    decide,
)
from tests.story_support import WRITE, character, create
from tests.structured_turn_support import beat, claim, render, schema_calls, turn_report

pytestmark = pytest.mark.integration

OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}
LIBRARY = RequestContext(principal="p-author")
NOTHING_SALIENT = json.dumps({"salient": [], "texture": []})
SetLimit = Callable[[int], Awaitable[None]]
NOT_TAKING_PART = "Present but not taking part: Bobby; never give them dialogue."
HEARD = "you heard Jenni say Olivia sent the text"


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
    test (``testimony_support.forget``)."""
    try:
        yield
    finally:
        await ts.forget(engine)


# -- helpers ----------------------------------------------------------------------------------


async def switch(
    client: httpx.AsyncClient, story: str, character_id: str, mode: Any, **headers: str
) -> httpx.Response:
    return await client.patch(
        f"/stories/{story}/characters/{character_id}/bench",
        json={"mode": mode},
        headers={**WRITE, **headers},
    )


async def told_room(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    lib: Engine,
    *,
    mode: str | None,
    away: tuple[str, ...] = (),
    silent: bool = False,
) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any]]:
    """Wren (the player), Jenni and Bobby in the parlor (``away`` in the kitchen), partitions
    with testimony ``defaulted`` at perception ``co_location`` and lazy resolution off, under
    per-character renders. Bobby is benched in ``mode`` (none: not benched; ``silent``: his
    participation is a fixed ``silent`` instead). Jenni then tells Wren that Olivia sent the
    text. Answers the story, the ids, the room's things and the turn's response."""
    story, ids, things = await ts.room(client, engine, "Wren", "Jenni", "Bobby", away=away)
    await ts.enable_testimony(
        engine, story, "defaulted", level="co_location", salience="rules", lazy=False
    )
    await ms.per_character(engine, story)
    bobby = ids["Bobby"]
    if silent:
        await rs.set_role(client, story, bobby, "silent", fixed=True)
    if mode is not None:
        benched = await bench(client, story, bobby, {"mode": mode})
        assert benched.status_code == 200, benched.text
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
    return story, ids, things, told.json()


async def asked_prompt(
    client: httpx.AsyncClient, lib: Engine, story: str, ids: dict[str, str]
) -> str:
    """Bobby's render prompt when Wren asks him about the text (after his reactivation)."""
    stub = lib.models.stub
    stub.reset()
    stub.script("generation", [render(beat("do", "Bobby shrugs."))])
    stub.script("salience_detection", [NOTHING_SALIENT])
    asked = await respond(
        client,
        story,
        body(ids["Wren"], "Bobby, did Olivia send the text?", render_character_ids=[ids["Bobby"]]),
    )
    assert asked.status_code == 200, asked.text
    (call,) = schema_calls(stub, "CharacterRender")
    return "\n".join(m.content for m in call.messages)


def scene_line(prompt: str) -> str:
    """The prompt's ``## Scene`` line (``Location: … Present: …``)."""
    return next(line for line in prompt.splitlines() if line.startswith("Location:"))


async def beats_by(engine: AsyncEngine, story: str, character_id: str) -> int:
    return int(
        await scalar(
            engine,
            "SELECT count(*) FROM events WHERE story_id = :s AND type LIKE 'beat_%' "
            "AND character_id = :c",
            s=story,
            c=character_id,
        )
        or 0
    )


async def named(engine: AsyncEngine, story: str) -> dict[str, str]:
    """Every id of the story that a belief, exposure or memory may name → a stable label: an
    entity's canonical name, a character's name, a predicate's name."""
    found: dict[str, str] = {}
    for r in await rows(
        engine, "SELECT id, canonical_name FROM entities WHERE story_id = :s", s=story
    ):
        found[str(r.id)] = str(r.canonical_name)
    for r in await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story):
        found[str(r.id)] = str(r.name)
    for r in await rows(
        engine, "SELECT id, name FROM predicates WHERE story_id = :s OR story_id IS NULL", s=story
    ):
        found[str(r.id)] = str(r.name)
    return found


def _label(value: Any, names: dict[str, str]) -> Any:
    if value is None:
        return None
    text = str(value)
    return names.get(text, text)


async def partition(engine: AsyncEngine, story: str, holder: str) -> dict[str, list[Any]]:
    """``holder``'s beliefs, exposures and perceived memories, compared by proposition (or
    content), source and position: ids and timestamps left out (the planner's equality)."""
    names = await named(engine, story)
    beliefs = sorted(
        (
            _label(b.subject_id, names),
            _label(b.predicate_id, names),
            _label(b.object_id, names),
            str(b.polarity),
            str(b.kind),
            str(b.confidence_level),
            str(b.state),
            str(b.cause),
            _label(b.source_character_id, names),
            str(b.outcome),
            b.position_beat,
            b.position_step,
        )
        for b in await ts.beliefs_of(engine, story, holder)
    )
    exposures = sorted(
        (
            _label(r.subject_id, names),
            _label(r.predicate_id, names),
            _label(r.object_id, names),
            _label(r.source_character_id, names),
            str(r.source_type),
            str(r.kind),
            str(r.fidelity),
            bool(r.inferred),
            r.position_turn,
            r.position_slot_k,
            r.position_beat,
            r.position_step,
            str(r.state),
        )
        for r in await rows(
            engine,
            "SELECT p.subject_id, p.predicate_id, p.object_id, x.source_character_id, "
            "e.type AS source_type, x.kind, x.fidelity, x.inferred, x.position_turn, "
            "x.position_slot_k, x.position_beat, x.position_step, x.state "
            "FROM exposures x JOIN propositions p ON p.id = x.proposition_id "
            "LEFT JOIN events e ON e.id = x.source_event_id "
            "WHERE x.story_id = :s AND x.witness_character_id = :w AND x.membership = 'active'",
            s=story,
            w=holder,
        )
    )
    memories = sorted(
        (str(m.text), str(m.fidelity), str(m.channel), m.beat, m.narrative_turn)
        for m in await rows(
            engine,
            "SELECT text, fidelity, channel, beat, narrative_turn FROM memories "
            "WHERE story_id = :s AND holder_character_id = :h AND membership = 'active'",
            s=story,
            h=holder,
        )
    )
    return {"beliefs": beliefs, "exposures": exposures, "memories": memories}


# -- the mode -------------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_switch(
    served: tuple[httpx.AsyncClient, Engine], engine: AsyncEngine
) -> None:
    """``bench`` accepts ``{mode: "present"}``. ``PATCH …/bench`` switches a benched
    character's mode: 200 with the character and its ``bench`` in the new mode (``since_turn``
    kept), one more timeline revision, an ``inter_turn`` slot operation recording
    ``character_bench_mode_changed`` ``{character_id, mode, previous_mode}`` (category
    ``control``) and one more ``benched`` version row in the new mode citing it, the bench's
    previous role carried. The mode it has already is a 200 no-op: no event, no row, no
    revision. A character that is not benched is 409 ``not_benched``; an unknown character
    404; an unknown mode 422. Reactivation after a switch restores the role it had."""
    client, _ = served
    story, ids = await new_story(client, "Wren", "Ada", "Bo")
    ada, bo = ids["Ada"], ids["Bo"]
    role = await client.patch(
        f"/stories/{story}/characters/{ada}/participation",
        json={"role": "silent", "fixed": True},
        headers=WRITE,
    )
    assert role.status_code == 200, role.text

    present = await bench(client, story, bo, {"mode": "present"})
    assert present.status_code == 200, present.text
    assert present.json()["bench"] == {"mode": "present", "since_turn": 0}
    assert char_of(await story_read(client, story), bo)["bench"]["mode"] == "present"

    assert (await bench(client, story, ada)).status_code == 200
    timeline, config = await revisions(engine, story)
    switched = await switch(client, story, ada, "present")
    assert switched.status_code == 200, switched.text
    assert switched.json()["bench"] == {"mode": "present", "since_turn": 0}
    assert switched.json()["character"]["bench"] == {"mode": "present", "since_turn": 0}
    assert switched.json()["timeline_revision"] == timeline + 1
    assert await revisions(engine, story) == (timeline + 1, config)
    versions = [v for v in await bench_rows(engine, story) if v.character_id == ada]
    assert [(v.mode, v.benched) for v in versions] == [("sequestered", True), ("present", True)]
    assert (versions[1].previous_role, versions[1].previous_fixed) == ("silent", True)
    (op,) = await rows(
        engine,
        "SELECT kind::text AS kind FROM operations WHERE operation_id = :o",
        o=versions[1].operation_id,
    )
    assert op.kind == "inter_turn"
    (event,) = await rows(
        engine,
        "SELECT id, type, category::text AS category, payload FROM events WHERE operation_id = :o",
        o=versions[1].operation_id,
    )
    assert (event.type, event.category) == ("character_bench_mode_changed", "control")
    assert event.payload == {"character_id": ada, "mode": "present", "previous_mode": "sequestered"}
    assert versions[1].source_event_ids == [event.id]

    same = await switch(client, story, ada, "present")
    assert same.status_code == 200, same.text
    assert same.json()["bench"] == {"mode": "present", "since_turn": 0}
    assert same.json()["timeline_revision"] == timeline + 1
    assert await revisions(engine, story) == (timeline + 1, config)
    assert len([v for v in await bench_rows(engine, story) if v.character_id == ada]) == 2

    back_again = await switch(client, story, ada, "sequestered")
    assert back_again.status_code == 200, back_again.text
    assert back_again.json()["bench"]["mode"] == "sequestered"

    assert refusal(await switch(client, story, ids["Wren"], "present")) == (
        409,
        "not_benched",
        {"character_id": ids["Wren"]},
    )
    assert (await switch(client, story, new_ulid(), "present")).status_code == 404
    unknown = await switch(client, story, ada, "elsewhere")
    assert unknown.status_code == 422, unknown.text

    restored = await reactivate(client, story, ada)
    assert restored.status_code == 200, restored.text
    assert restored.json()["character"]["participation"] == {"role": "silent", "fixed": True}
    assert refusal(await switch(client, story, ada, "present"))[:2] == (409, "not_benched")


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_cast_count(
    served: tuple[httpx.AsyncClient, Engine], cast_limit: SetLimit
) -> None:
    """A ``present`` bench doesn't count either: at a limit of 3 with Ada benched ``present``,
    a 4th character is added, and reactivating Ada is then 422 ``cast_max``."""
    client, _ = served
    story = await create(
        client,
        {"characters": [character("Wren", is_player=True), character("Ada"), character("Bo")]},
    )
    sid = story["story_id"]
    ada = next(c["id"] for c in story["characters"] if c["name"] == "Ada")
    await cast_limit(3)
    assert (await bench(client, sid, ada, {"mode": "present"})).status_code == 200
    added = await client.post(
        f"/stories/{sid}/characters",
        json={"name": "Dale", "definition": "A driver.", "true_age": 52},
        headers=WRITE,
    )
    assert added.status_code == 201, added.text
    assert (await story_read(client, sid))["cast"] == {"active": 3, "benched": 1}
    assert refusal(await reactivate(client, sid, ada)) == (
        422,
        "validation_error",
        {"field": "body", "cap": "cast_max", "limit": 3, "actual": 4},
    )


# -- perception -----------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_perceives(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """The three-way perception test at ``co_location``. Bobby benched ``present`` in the room
    when Jenni says Olivia sent the text: he has an exposure and a routed memory of it, and
    after his reactivation his context says he heard her say it. Benched ``present`` but in the
    kitchen: none of it. Benched ``sequestered`` in the room: none of it."""
    client, lib = served

    story, ids, _, _ = await told_room(client, engine, lib, mode="present")
    bobby = ids["Bobby"]
    assert await exposures_of(engine, story, bobby) >= 1
    assert await heard_by(engine, story, bobby, "Olivia") != []
    assert (await reactivate(client, story, bobby)).status_code == 200
    assert HEARD in await asked_prompt(client, lib, story, ids)

    for mode, away in (("present", ("Bobby",)), ("sequestered", ())):
        story, ids, _, _ = await told_room(client, engine, lib, mode=mode, away=away)
        bobby = ids["Bobby"]
        assert await exposures_of(engine, story, bobby) == 0, mode
        assert await heard_by(engine, story, bobby, "Olivia") == [], mode
        assert (await reactivate(client, story, bobby)).status_code == 200
        prompt = await asked_prompt(client, lib, story, ids)
        assert HEARD not in prompt, mode
        assert ts.CLAIM_LINE not in prompt, mode


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_no_widening(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """A ``present`` benched character's partition is exactly an unbenched ``silent``
    co-located character's for the same turn: twin stories, identical scripted output under
    per-character renders with lazy resolution off, and Bobby's beliefs, exposures and
    perceived memories compared by proposition (or content), source and position. The
    sets are not empty, so the comparison compares something."""
    client, lib = served
    benched, ids, _, _ = await told_room(client, engine, lib, mode="present")
    twin, twin_ids, _, _ = await told_room(client, engine, lib, mode=None, silent=True)
    mine = await partition(engine, benched, ids["Bobby"])
    theirs = await partition(engine, twin, twin_ids["Bobby"])
    assert mine["exposures"] != []
    assert mine["memories"] != []
    assert mine["beliefs"] != []
    assert mine == theirs


# -- visible, never speaking ------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_visible(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """Bobby benched ``present`` in the room. Jenni's render prompt names him in its scene line
    and carries "Present but not taking part: Bobby; never give them dialogue", but not his
    definition, his ``## You are`` header or his id; the turn has no render call of his and no
    beat with him as actor. Jenni's prose describing what he does ("Bobby shifts in his
    seat.") stays her beat. The scene read lists him with ``bench {mode: present}``, and
    ``turn_report.bench.present`` lists him. A turn naming him is 409 ``character_benched``.
    Complement: benched ``sequestered``, the scene line and the scene read lack him, and the
    prompt has no such line."""
    client, lib = served
    stub = lib.models.stub
    definition = "Bobby, as the cast sheet describes them."

    async def scene(mode: str) -> tuple[str, dict[str, str], str]:
        story, ids, _ = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
        await ts.enable_testimony(engine, story, "defaulted", level="co_location")
        await ms.per_character(engine, story)
        benched = await bench(client, story, ids["Bobby"], {"mode": mode})
        assert benched.status_code == 200, benched.text
        stub.reset()
        stub.script(
            "generation",
            [render(beat("do", "Jenni pours the tea. Bobby shifts in his seat."))],
        )
        stub.script("salience_detection", [NOTHING_SALIENT])
        played = await respond(
            client, story, body(ids["Wren"], "Tea?", render_character_ids=[ids["Jenni"]])
        )
        assert played.status_code == 200, played.text
        (call,) = schema_calls(stub, "CharacterRender")
        return story, ids, "\n".join(m.content for m in call.messages)

    story, ids, prompt = await scene("present")
    bobby = ids["Bobby"]
    assert "Bobby" in scene_line(prompt)
    assert NOT_TAKING_PART in prompt
    assert "## You are Jenni" in prompt
    assert "## You are Bobby" not in prompt
    assert definition not in prompt
    assert bobby not in prompt
    assert len(schema_calls(stub, "CharacterRender")) == 1
    assert await beats_by(engine, story, bobby) == 0
    (said,) = await rows(
        engine,
        "SELECT character_id, payload->>'text' AS text FROM events "
        "WHERE story_id = :s AND type = 'beat_do'",
        s=story,
    )
    assert said.character_id == ids["Jenni"]
    assert "Bobby shifts in his seat." in said.text
    assert (await turn_report(engine, story))["bench"] == {"present": [bobby]}
    read = await client.get(f"/stories/{story}/scene", headers=WRITE)
    assert read.status_code == 200, read.text
    listed = {e["name"]: e for e in read.json()["present"]}
    assert listed["Bobby"]["bench"] == {"mode": "present"}
    assert "bench" not in listed["Jenni"]
    for payload in (
        body(ids["Wren"], "Bobby?", render_character_ids=[bobby]),
        body(ids["Wren"], "Hi.", roles={bobby: "primary"}),
        body(ids["Wren"], "psst", player_input={"say": "psst", "whisper_to": [bobby]}),
    ):
        named_ = await respond(client, story, payload)
        assert refusal(named_) == (409, "character_benched", {"character_id": bobby}), payload

    story, ids, prompt = await scene("sequestered")
    assert "Bobby" not in scene_line(prompt)
    assert "Present but not taking part" not in prompt
    assert "bench" not in (await turn_report(engine, story))
    read = await client.get(f"/stories/{story}/scene", headers=WRITE)
    assert "Bobby" not in {e["name"] for e in read.json()["present"]}


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_never_renders(
    served: tuple[httpx.AsyncClient, Engine],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    forgetting: None,
) -> None:
    """The bench story's no-render and no-prompt-cast assertions hold in ``present`` mode: with
    Ada benched ``present``, a turn with no render ids renders Bo, not Ada; no captured prompt
    carries her definition or her id, and the one line names her. Naming her is 409."""
    client, lib = served
    stub = lib.models.stub
    story, ids, _ = await ts.room(client, engine, "Wren", "Ada", "Bo")
    ada = ids["Ada"]
    assert (await bench(client, story, ada, {"mode": "present"})).status_code == 200
    stub.reset()
    stub.script("generation", [render(beat("do", "Bo nods."))])
    played = await respond(client, story, body(ids["Wren"], "Anyone?"))
    assert played.status_code == 200, played.text
    assert len(schema_calls(stub, "CharacterRender")) == 1
    sent = "\n".join(m.content for call in stub.calls for m in call.messages)
    assert "Ada, as the cast sheet describes them." not in sent
    assert ada not in sent
    assert "## You are Bo" in sent
    assert "Present but not taking part: Ada; never give them dialogue." in sent
    assert await beats_by(engine, story, ada) == 0
    refused = await respond(client, story, body(ada, "I object."))
    assert refusal(refused) == (409, "character_benched", {"character_id": ada})


# -- verification -----------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-14")
def test_i_32__m9_verify_exception() -> None:
    """The present-bench exception to #144's other-agent denial, both ways. With Bobby
    benched ``present``, the narrator (a director pass) and Jenni's render may establish where
    he is and what he holds (basis ``bench_present``); that he spoke, to anyone, and his
    backstory stay ``other_character_secret``, and so does a claim also naming an unbenched
    character, or the player's character. With Bobby unbenched or benched ``sequestered``
    (not in ``bench_present``), the same placement claim is denied as before."""
    present = frozenset({BOBBY})
    for asserter in (Asserter("director"), Asserter("character", JENNI)):
        assert decide(BOBBY, "located_at", KITCHEN, asserter, bench_present=present) == (
            Permitted(Basis.BENCH_PRESENT)
        )
        assert decide(BOBBY, "spoke", None, asserter, bench_present=present) == Denied(
            Reason.OTHER_CHARACTER_SECRET
        )
        assert decide(BOBBY, "spoke_to", JENNI, asserter, bench_present=present) == Denied(
            Reason.OTHER_CHARACTER_SECRET
        )
        assert decide(BOBBY, "located_at", KITCHEN, asserter) == Denied(
            Reason.OTHER_CHARACTER_SECRET
        )
        assert decide(
            BOBBY, "located_at", KITCHEN, asserter, bench_present=present, protected=True
        ) == Denied(Reason.PROTECTED_CONTENT)
    jenni_says = Asserter("character", JENNI)
    assert decide(BOBBY, "attended_program", CAMP, jenni_says, bench_present=present) == (
        Denied(Reason.OTHER_CHARACTER_SECRET)
    )
    assert decide(BOBBY, "spoke_to", CAL, jenni_says, bench_present=present) == Denied(
        Reason.PLAYER_CHARACTER
    )
    facts = PermissionFacts(entities=ENTITIES, bench_present=present)
    assert frozenset({"holds", "located_at", "present_at"}) == pm.BENCH_PRESENT_PREDICATES
    assert facts.bench_present == present


# -- Lite -------------------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_lite(served: tuple[httpx.AsyncClient, Engine]) -> None:
    """A Lite story accepts ``present``; its only effect is the one prompt line. With Ada
    benched ``present``, a turn renders Bo alone, with no author notes for Ada and the line
    "Present but not taking part: Ada; never give them dialogue". Benched ``sequestered``, the
    line is absent."""
    client, lib = served
    stub = lib.models.stub
    story, ids = await new_story(client, "Wren", "Ada", "Bo")
    ada = ids["Ada"]
    assert (await bench(client, story, ada, {"mode": "present"})).status_code == 200
    stub.reset()
    stub.script("generation", ["Bo: Over here."])
    played = await respond(client, story, body(ids["Wren"], "Where to?"))
    assert played.status_code == 200, played.text
    sent = "\n".join(m.content for call in stub.calls for m in call.messages)
    assert "### Author notes: Ada" not in sent
    assert "Present but not taking part: Ada; never give them dialogue." in sent

    assert (await switch(client, story, ada, "sequestered")).status_code == 200
    stub.reset()
    stub.script("generation", ["Bo: Still here."])
    again = await respond(client, story, body(ids["Wren"], "And now?"))
    assert again.status_code == 200, again.text
    sent = "\n".join(m.content for call in stub.calls for m in call.messages)
    assert "Present but not taking part" not in sent


# -- the library ------------------------------------------------------------------------------


@pytest.mark.part("M9", "FR-MULTI-14")
async def test_i_32__m9_library(served: tuple[httpx.AsyncClient, Engine]) -> None:
    """``Engine.set_bench_mode`` does what the route does: a switch, a no-op to the same mode
    (the revision unchanged) and ``NotBenched`` for an unbenched character."""
    client, lib = served
    story, ids = await new_story(client, "Wren", "Ada", "Bo")
    ada = ids["Ada"]
    benched = await lib.bench_character(LIBRARY, story, ada, BenchRequest(mode="present"))
    assert benched.character.bench == {"mode": "present", "since_turn": 0}
    switched = await lib.set_bench_mode(LIBRARY, story, ada, BenchModeRequest(mode="sequestered"))
    assert switched.character.bench == {"mode": "sequestered", "since_turn": 0}
    assert switched.timeline_revision == benched.timeline_revision + 1
    same = await lib.set_bench_mode(LIBRARY, story, ada, BenchModeRequest(mode="sequestered"))
    assert same.timeline_revision == switched.timeline_revision
    with pytest.raises(NotBenched):
        await lib.set_bench_mode(LIBRARY, story, ids["Bo"], BenchModeRequest(mode="present"))
