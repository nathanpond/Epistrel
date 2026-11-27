"""#1060: an actor's own beats at the place it stands, played end to end through ``respond`` on
the stub adapter. Maren is the actor and the render character (as a consumer's host is):

- ``test_a_shared_place_name_is_the_scene_place``: "the tribal council" names the day 1 and the
  day 3 council; Maren stands at the day 3 one, so ``located_at`` resolves to it and is
  supported, with no regeneration. Complement: placed at the camp, the name stays ambiguous
  and is repaired as before.
- ``test_a_move_between_turns_is_seen``: everyone is moved from the camp to the council between
  two turns (clock off, and on with a tick before the move). Maren's ``located_at`` the council
  and the detector's ``present_at`` of Flynn are supported, and nothing is denied. Complement:
  Flynn left at the camp, "Maren nods to Flynn" is still denied.
- ``test_a_greeting_toward_a_present_character_commits``: "greets"/"welcomes" Flynn (present)
  are texture and commit. Complement: Carlos is elsewhere, so the greeting toward him is denied,
  and the regeneration's constraint names Carlos, not Maren herself.
- ``test_a_failed_do_falls_back_to_a_say``: a ``do`` beat whose regenerations keep failing
  commits the safe fallback as a ``say``, never a ``do``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions.model import Interval, Proposition
from tests import clock_support as cs
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.detection_support import claims_answer
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    LOCATED_AT,
    beat,
    claim,
    render,
    structured_story,
    turn_report,
)

pytestmark = pytest.mark.integration

SAY = "It's time to vote."
DO = "Maren stands by the fire."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _extracted(span: str, subject: str, predicate: str, obj: str | None) -> dict[str, Any]:
    return {
        "beat": 0,
        "span": span,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "time": None,
        "polarity": "affirmed",
        "confidence": 1.0,
    }


def _regenerations(stub: Any) -> list[str]:
    return [
        "\n".join(m.content for m in c.messages) for c in stub.calls if c.purpose == "regeneration"
    ]


def _verdicts(report: dict[str, Any]) -> list[str]:
    return [v["kind"] for v in report.get("verify", {}).get("verdicts", [])]


@pytest.mark.parametrize("placed", ["day 3", "camp"])
async def test_a_shared_place_name_is_the_scene_place(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None, placed: str
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Flynn")
        await ps.place_entity(engine, story, "the day 1 tribal council")
        council = await ps.place_entity(engine, story, "the day 3 tribal council")
        camp = await ps.place_entity(engine, story, "the camp")
        for n in ("Wren", "Maren", "Flynn"):
            await ps.put(engine, story, ids[n], council if placed == "day 3" else camp)
        stub = epistrel.models.stub
        stub.reset()
        located = claim("Maren", "located_at", "the tribal council")
        stub.script("generation", [render(beat("do", DO, propositions=[located]))])
        stub.script("regeneration", [json.dumps({"text": DO})] * 4)
        r = await respond(
            client, story, body(ids["Maren"], SAY, do=DO, render_character_ids=[ids["Maren"]])
        )
        assert r.status_code == 200, r.text
        report = await turn_report(engine, story)
    if placed == "day 3":
        assert _verdicts(report) == ["supported"], report.get("verify")
        assert _regenerations(stub) == []
    else:
        assert _verdicts(report) == ["uncertain"], report.get("verify")
        assert _regenerations(stub), "the ambiguous name is repaired as before"


@pytest.mark.parametrize("flynn_moves", [True, False])
@pytest.mark.parametrize("clock", ["off", "on"])
async def test_a_move_between_turns_is_seen(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
    clock: str,
    flynn_moves: bool,
) -> None:
    if clock == "on":
        cs.deliver(monkeypatch)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Flynn")
        if clock == "on":
            await cs.set_clock(engine, story, "on", stops=False)
        camp = await ps.place_entity(engine, story, "the camp")
        council = await ps.place_entity(engine, story, "the day 3 tribal council")
        start = 0
        if clock == "on":
            await cs.advance(engine, story, 60)
            start = await cs.offset(engine, story)
        until = None if clock == "off" else start + 1
        for n in ("Wren", "Maren", "Flynn"):
            at_camp = Interval.of(None, None if (n == "Flynn" and not flynn_moves) else until)
            await ps._author(
                engine,
                story,
                Proposition(
                    subject_id=ids[n], predicate_id=LOCATED_AT, object_id=camp, valid_time=at_camp
                ),
            )
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("say", "Morning."))])
        r = await respond(
            client, story, body(ids["Maren"], "Morning.", render_character_ids=[ids["Maren"]])
        )
        assert r.status_code == 200, r.text
        # The consumer's move between the turns: a tick, then the council from now on.
        now = None
        if clock == "on":
            await cs.advance(engine, story, 1)
            now = await cs.offset(engine, story)
        movers = ("Wren", "Maren", "Flynn") if flynn_moves else ("Wren", "Maren")
        for n in movers:
            await ps._author(
                engine,
                story,
                Proposition(
                    subject_id=ids[n],
                    predicate_id=LOCATED_AT,
                    object_id=council,
                    valid_time=Interval.of(now, None),
                ),
            )
        stub.reset()
        located = claim("Maren", "located_at", "the day 3 tribal council")
        stub.script(
            "generation",
            [render(beat("do", DO, propositions=[located]), beat("do", "Maren nods to Flynn."))],
        )
        stub.script("regeneration", [json.dumps({"text": DO})] * 4)
        r = await respond(
            client, story, body(ids["Maren"], SAY, do=DO, render_character_ids=[ids["Maren"]])
        )
        assert r.status_code == 200, r.text
        report = await turn_report(engine, story)
    denied = report.get("verify", {}).get("denied", [])
    if flynn_moves:
        assert denied == [], denied
        assert _verdicts(report) == ["supported", "supported"], report.get("verify")
    else:
        assert [d["reason"] for d in denied] == ["other_character_secret"], denied


@pytest.mark.parametrize("relation", ["greets", "welcomes"])
@pytest.mark.parametrize("target", ["Flynn", "Carlos"])
async def test_a_greeting_toward_a_present_character_commits(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    relation: str,
    target: str,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren", "Flynn", "Carlos")
        council = await ps.place_entity(engine, story, "the day 3 tribal council")
        camp = await ps.place_entity(engine, story, "the camp")
        for n in ("Wren", "Maren", "Flynn"):
            await ps.put(engine, story, ids[n], council)
        await ps.put(engine, story, ids["Carlos"], camp)
        stub = epistrel.models.stub
        stub.reset()
        line = f"{target}. Welcome to tribal council."
        stub.script("generation", [render(beat("say", line, intent="sincere"))])
        greeting = claims_answer(_extracted("Welcome to tribal council", "Maren", relation, target))
        stub.script("extraction_backstop", [greeting] * 4)
        stub.script("regeneration", [json.dumps({"text": line})] * 4)
        r = await respond(
            client, story, body(ids["Maren"], "Welcome.", render_character_ids=[ids["Maren"]])
        )
        assert r.status_code == 200, r.text
        report = await turn_report(engine, story)
    denied = report.get("verify", {}).get("denied", [])
    if target == "Flynn":
        assert denied == [], denied
        assert _regenerations(stub) == []
        assert line in r.json()["message"]["text"]
    else:
        assert [d["reason"] for d in denied] == ["other_character_secret"], denied
        (first, *_) = _regenerations(stub)
        assert "Do not introduce facts about Carlos" in first
        assert "Do not introduce facts about Maren" not in first


async def test_a_failed_do_falls_back_to_a_say(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("do", DO))])
        bad = claims_answer(_extracted("stands by the fire", "the_stable", "holds", "straw"))
        stub.script("extraction_backstop", [bad] * 6)
        stub.script("regeneration", [json.dumps({"text": DO})] * 6)
        r = await respond(
            client, story, body(ids["Maren"], SAY, do=DO, render_character_ids=[ids["Maren"]])
        )
        assert r.status_code == 200, r.text
    rendered = [(b["kind"], b["text"]) for b in r.json()["beats"] if b["beat"] >= 2]
    kinds = [k for k, _ in rendered if k.startswith("beat_")]
    assert kinds == ["beat_say"], rendered
    assert "beat_do" not in kinds
    assert "changes the subject" in rendered[0][1]
