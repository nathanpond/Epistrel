"""C-28: story inspection (FR-ADMIN-5, FR-RESP-12, FR-AUTH-1) [M5] (#178).

The inspection reads (``/stories/{id}/inspect/beliefs``, ``…/divergences``, ``…/exposures``,
``…/ledger``) return a character's belief set, the story's divergences and exposures, and the
promissory ledger, each labelled ``output_viewpoint: "omniscient"``. A character viewpoint is
refused (403 ``omniscient_required``), no viewpoint is refused (422 ``viewpoint_unresolved``: an
inspection never infers one), and service authentication is the only other check. None of
their content appears in a character-viewpoint ``respond`` or history read: canary texts and the
inspected records' ids are searched in every character-viewpoint body, and an AST check shows no
inspection serializer is reachable from the respond and history routes. Run over REST and the
library (the ``surface`` fixture).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import canon_support as c
from tests import erasure_support as e
from tests import partition_support as ps
from tests import perception_support as perception
from tests.conformance.conftest import Surface
from tests.conftest import REPO_ROOT
from tests.event_support import rows
from tests.respond_support import body
from tests.story_support import story_body
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration

KINDS = ("beliefs", "divergences", "exposures", "ledger")
CANARY = "C28-CANARY-LOCKET"
SERIALIZERS = {"BeliefsBody", "DivergencesBody", "ExposuresBody", "LedgerBody", "BeliefItemBody"}


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
async def test_c_28(
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None, perception_delivered: None
) -> None:
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
    surface: Surface, engine: AsyncEngine, drained_embedding_jobs: None, perception_delivered: None
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
