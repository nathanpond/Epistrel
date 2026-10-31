"""C-28: story inspection (FR-ADMIN-5, FR-RESP-12, FR-AUTH-1) [M5] (#178), [M6] (#207).

The inspection reads (``/stories/{id}/inspect/beliefs``, ``…/divergences``, ``…/exposures``,
``…/ledger``) return a character's belief set, the story's divergences and exposures, and the
promissory ledger, each labelled ``output_viewpoint: "omniscient"``. A character viewpoint is
refused (403 ``omniscient_required``), no viewpoint is refused (422 ``viewpoint_unresolved``: an
inspection never infers one), and service authentication is the only other check. None of
their content appears in a character-viewpoint ``respond`` or history read: canary texts and the
inspected records' ids are searched in every character-viewpoint body, and an AST check shows no
inspection serializer is reachable from the respond and history routes. Run over REST and the
library (the ``surface`` fixture).

M6 (``test_c_28__m6``, #207): the same holds for the snapshotted external facts
(``…/inspect/external-facts``: each ``external_read`` with the facts citing it), which keeps no
partitions gate.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import canon_support as c
from tests import effect_support as effects
from tests import erasure_support as e
from tests import multi_support as multi
from tests import partition_support as ps
from tests import perception_support as perception
from tests import provider_support as pv
from tests import state_support as states
from tests.conformance.conftest import Surface
from tests.conftest import REPO_ROOT
from tests.event_support import rows
from tests.respond_support import body
from tests.story_support import story_body
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration

KINDS = ("beliefs", "divergences", "exposures", "ledger")
CANARY = "C28-CANARY-LOCKET"
SERIALIZERS = {
    "BeliefsBody",
    "DivergencesBody",
    "ExposuresBody",
    "LedgerBody",
    "BeliefItemBody",
    "ExternalFactsBody",
    "ExternalReadItemBody",
}


def _params(kind: str, ann: str) -> dict[str, Any]:
    return {"character": ann} if kind == "beliefs" else {}


async def _ids(engine: AsyncEngine, story: str) -> set[str]:
    """The ids of every belief, exposure and divergence of the story (what inspection shows)."""
    found: set[str] = set()
    for table in ("beliefs", "exposures", "divergences"):
        found |= {
            str(r.id)
            for r in await rows(engine, f"SELECT id FROM {table} WHERE story_id = :s", s=story)  # noqa: S608
        }
    return found


@pytest.mark.part("M5", "FR-ADMIN-5; FR-RESP-12; FR-AUTH-1")
async def test_c_28(surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    """Every inspection answers ``omniscient`` alone, labelled so, and refuses the rest; the
    belief read keeps its rules (the player's character: an empty page; ``as_of`` before a
    belief: without it); a Lite story is 409 ``module_off``; two principals read the same
    bodies; a character's history read holds no inspected record (complement: the omniscient
    inspection does)."""
    k = await e.knowing_story(engine)
    story = k.story_id
    for kind in KINDS:
        params = _params(kind, k.ann)
        seen = await surface.inspect(kind, story, viewpoint="omniscient", **params)
        assert seen.status == 200, (kind, seen.code)
        assert seen.json["output_viewpoint"] == "omniscient"
        other = await surface.inspect(
            kind, story, viewpoint="omniscient", principal="p-stranger", **params
        )
        assert other.json == seen.json, kind  # authentication is the only check
        refused = await surface.inspect(kind, story, viewpoint=f"character:{k.ann}", **params)
        assert (refused.status, refused.code) == (403, "omniscient_required"), kind
        unnamed = await surface.inspect(kind, story, **params)
        assert (unnamed.status, unnamed.code) == (422, "viewpoint_unresolved"), kind

    beliefs = (
        await surface.inspect("beliefs", story, viewpoint="omniscient", character=k.ann)
    ).json
    assert k.kept_belief in {b["belief_id"] for b in beliefs["beliefs"]}
    player = (await surface.inspect("beliefs", story, viewpoint="omniscient", character=k.cal)).json
    assert (player["beliefs"], player["identities"]) == ([], [])
    before = (
        await surface.inspect("beliefs", story, viewpoint="omniscient", character=k.ann, as_of=0)
    ).json
    assert before["beliefs"] == []
    exposures = (await surface.inspect("exposures", story, viewpoint="omniscient")).json
    assert {x["witness"] for x in exposures["items"]} == {k.ann}
    assert {x["state"] for x in exposures["items"]} == {"unresolved"}
    (lie,) = (await surface.inspect("divergences", story, viewpoint="omniscient")).json["items"]
    assert lie["speaker"] == k.bobby
    only = await surface.inspect("divergences", story, viewpoint="omniscient", speaker=k.ann)
    assert only.json["items"] == []
    resolved = await surface.inspect(
        "exposures", story, viewpoint="omniscient", state="resolved", character=k.ann
    )
    assert resolved.json["items"] == []

    inspected = await _ids(engine, story)
    assert inspected
    for viewer in (k.ann, k.bobby, k.cal):
        read = await surface.history(story, viewpoint=f"character:{viewer}")
        assert read.status == 200, read.code
        text = json.dumps(read.json)
        assert not [i for i in inspected if i in text], viewer
    everything = json.dumps((await surface.history(story, viewpoint="omniscient")).json)
    assert not [i for i in inspected if i in everything]  # history is no inspection either

    lite = (await surface.create_story(story_body("Wren", "Ada"))).json
    ada = lite["characters"][1]["id"]
    for kind in ("beliefs", "divergences", "exposures"):
        off = await surface.inspect(
            kind, lite["story_id"], viewpoint="omniscient", **_params(kind, ada)
        )
        assert (off.status, off.code) == (409, "module_off"), kind


@pytest.mark.part("M5", "FR-ADMIN-5; FR-RESP-12; FR-AUTH-1")
async def test_c_28__respond(
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A character-viewpoint respond carries no inspection content: Jenni believes the canary
    locket is in the parlor (a belief about an entity named ``CANARY``); responses produced for
    Jenni and for Bobby hold neither the canary nor the belief's id, while the inspection of
    Jenni's beliefs holds the belief."""
    stub = surface.engine.models.stub
    story, ids = await structured_story(surface.client, "Wren", "Jenni", "Bobby")
    await perception.perceive(engine, story, "full")
    locket = await perception.place_entity(engine, story, CANARY)
    parlor = await perception.place_entity(engine, story, "parlor")
    for name in ("Wren", "Jenni", "Bobby"):
        await perception.put(engine, story, ids[name], parlor)
    held = await ps.believe(
        engine,
        story,
        ids["Jenni"],
        c.at(locket, parlor, 0, None),
    )
    for viewer in ("Jenni", "Bobby"):
        stub.script("generation", [render(beat("say", "Good evening.", intent="sincere"))])
        found = await surface.respond(
            story, body(ids["Wren"], "Evening."), viewpoint=f"character:{ids[viewer]}"
        )
        assert found.status == 200, found.code
        text = json.dumps(found.json)
        read = await surface.history(story, viewpoint=f"character:{ids[viewer]}")
        for found_text in (text, json.dumps(read.json)):
            assert CANARY not in found_text, viewer
            assert held.id not in found_text, viewer
    seen = await surface.inspect("beliefs", story, viewpoint="omniscient", character=ids["Jenni"])
    assert held.id in {b["belief_id"] for b in seen.json["beliefs"]}


@pytest.mark.part("M6", "FR-ADMIN-5")
async def test_c_28__m6(
    surface: Surface,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#207: the snapshotted external facts (``inspect/external-facts``). Cal's gold is owned by
    a registered provider; two turns read it (300, then 250), and the narrator's "300" in the
    first commits an external fact citing its read. ``omniscient`` returns both reads, oldest
    first, each with its provider, value name, scope key, value, read time, turn, variant and
    source, the first with the fact that cites it; labelled ``omniscient``, the same under any
    principal; paged by id; ``as_of`` keeps the reads through a turn. A character viewpoint is
    refused (403 ``omniscient_required``), no viewpoint 422 ``viewpoint_unresolved``. No
    partitions gate: a story with no reads answers an empty page. None of it reaches a
    character-viewpoint respond or history read (the reads' and the fact's ids are searched)."""
    name = pv.unique_name("ledger")
    await pv.register(engine, pv.in_process_spec(name, {"gold": "number"}))
    fake = pv.ScriptedProvider(name).attach(surface.engine)
    fake.script("gold", 300, 250)
    stub = surface.engine.models.stub
    story, ids = await structured_story(surface.client, "Cal", "Mira")
    cal, mira = ids["Cal"], ids["Mira"]
    await states.set_level(engine, story, "on")
    gold = await pv.external_gold(engine, story, name)
    turns: list[int] = []
    for narration in ("Rain taps. Cal has 300 gold in all.", "Wind rattles the door."):
        stub.reset()
        effects.narrator(stub, effects.render(beat("narration", narration)))
        multi.renders_for(stub, "Mira", effects.render(beat("say", "Mind the edge.")))
        played = await surface.respond(
            story, body(cal, None, do="checks his purse"), viewpoint=f"character:{mira}"
        )
        assert played.status == 200, played.code
        assert played.json is not None
        turns.append(int(played.json["turn"]))
        for found in (played, await surface.history(story, viewpoint=f"character:{mira}")):
            text = json.dumps(found.json)
            reads = [str(r.id) for r in await pv.external_reads(engine, story)]
            cited = [str(f.id) for f in await pv.external_facts(engine, story)]
            assert reads
            assert not [i for i in (*reads, *cited) if i in text]

    seen = await surface.inspect("external-facts", story, viewpoint="omniscient")
    assert seen.status == 200, seen.code
    assert seen.json["output_viewpoint"] == "omniscient"
    assert seen.json["next_after_id"] is None
    first, second = seen.json["items"]
    (fact,) = await pv.external_facts(engine, story)
    assert {k: first[k] for k in ("provider", "value_name", "state_id", "scope_key")} == {
        "provider": name,
        "value_name": "gold",
        "state_id": gold.id,
        "scope_key": cal,
    }
    recorded = await pv.external_reads(engine, story)
    assert [(i["event_id"], i["variant"]) for i in (first, second)] == [
        (str(r.id), r.turn_variant) for r in recorded
    ]
    assert [(i["value"], i["turn"], i["source"]) for i in (first, second)] == [
        (300, turns[0], "live"),
        (250, turns[1], "live"),
    ]
    assert first["read_at"]
    assert [(f["fact_id"], f["provenance"], f["scope"]) for f in first["facts"]] == [
        (str(fact.id), "external", "engine")
    ]
    assert second["facts"] == []
    other = await surface.inspect(
        "external-facts", story, viewpoint="omniscient", principal="p-stranger"
    )
    assert other.json == seen.json  # authentication is the only check
    refused = await surface.inspect("external-facts", story, viewpoint=f"character:{mira}")
    assert (refused.status, refused.code) == (403, "omniscient_required")
    unnamed = await surface.inspect("external-facts", story)
    assert (unnamed.status, unnamed.code) == (422, "viewpoint_unresolved")

    page = await surface.inspect("external-facts", story, viewpoint="omniscient", limit=1)
    assert [i["event_id"] for i in page.json["items"]] == [first["event_id"]]
    assert page.json["next_after_id"] == first["event_id"]
    rest = await surface.inspect(
        "external-facts", story, viewpoint="omniscient", after_id=first["event_id"]
    )
    assert [i["event_id"] for i in rest.json["items"]] == [second["event_id"]]
    through = await surface.inspect("external-facts", story, viewpoint="omniscient", as_of=turns[0])
    assert [i["event_id"] for i in through.json["items"]] == [first["event_id"]]

    plain = (await surface.create_story(story_body("Wren", "Ada"))).json
    empty = await surface.inspect("external-facts", plain["story_id"], viewpoint="omniscient")
    assert (empty.status, empty.json["items"], empty.json["next_after_id"]) == (200, [], None)


def _references(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in SERIALIZERS:
                found.add(name)
    return found


@pytest.mark.part("M5", "FR-ADMIN-5; FR-RESP-12; FR-AUTH-1")
def test_c_28__serializers() -> None:
    """No inspection serializer is reachable from the respond and history routes: neither
    module (nor any API module it reaches) names ``epistrel.api.routes.inspect`` or an
    inspection body. Complement: the inspect routes module does."""
    api = REPO_ROOT / "src" / "epistrel" / "api"
    for route in ("respond.py", "history.py"):
        seen = {api / "routes" / route}
        frontier = list(seen)
        while frontier:
            path = frontier.pop()
            for module in _references(path):
                assert not module.startswith("epistrel.api.routes.inspect"), (path, module)
                assert module not in SERIALIZERS, (path, module)
                if module.startswith("epistrel.api."):
                    nxt = api.joinpath(*module.removeprefix("epistrel.api.").split("."))
                    candidate = nxt.with_suffix(".py")
                    if candidate.exists() and candidate not in seen:
                        seen.add(candidate)
                        frontier.append(candidate)
    assert SERIALIZERS & _references(api / "routes" / "inspect.py")
