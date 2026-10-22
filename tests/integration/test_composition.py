"""The composition of a structured turn's message (#152; FR-RESP-8, FR-VERIFY-14, NFR-PERF-6,7):
what the scenario (S-COMPOSE-VERIFY) and conformance (C-12 M4) parts leave to integration.

- A composition failure changes no beat: a turn whose polish faults stores exactly the beat
  events, utterances and memories of the same turn whose polish passes (row diff), and only
  the message differs (the deterministic assembly, ``composition_fallback``).
- The composed message's detection call is reserved once the renders are checked and released
  when the deterministic assembly is used; the turn never spends more than its budget, and a
  budget that cannot pay for the polish skips it (``polish_skipped_budget``).
- A one-beat turn, and a story whose polish pass is off, are assembled without a polish call
  and without a degradation entry.
- A Lite reply is unchanged: no ``mode``, no composition call.

Shared-database tests that write memories use ``drained_embedding_jobs`` (#138).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.models.stub import StubAdapter
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import create, override, story_body
from tests.structured_turn_support import beat, render, structured_story, turn_report

pytestmark = pytest.mark.integration

COMPOSITION = "composition.j2"
COMPOSED = "ComposedMessage"
THOUGHT = "She wonders whether he saw the letter."
LINE = "Good evening, Wren. Come in."
ACTION = "She opens the door wide and steps back."
DRAFT = f'Ada: "{LINE}"\n\n*{ACTION}*'


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _turn() -> str:
    return render(beat("think", THOUGHT), beat("say", LINE, intent="sincere"), beat("do", ACTION))


def _polish_calls(stub: StubAdapter) -> int:
    return sum(1 for c in stub.calls if c.response_schema == COMPOSED)


async def _beat_rows(engine: AsyncEngine, story_id: str) -> list[tuple[Any, ...]]:
    """The turn's committed events other than its message and the composition's audit record,
    and the derived rows it wrote, without ids (comparable across stories)."""
    events = await rows(
        engine,
        "SELECT e.type, e.beat, e.category, e.payload FROM events e "
        "JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND o.kind = 'turn' "
        "AND e.type NOT IN ('message_rendered', 'composition_rejected') ORDER BY e.seq",
        s=story_id,
    )
    found: list[tuple[Any, ...]] = [
        (
            e.type,
            e.beat,
            e.category,
            (e.payload or {}).get("text"),
            json.dumps((e.payload or {}).get("labels"), sort_keys=True),
            len((e.payload or {}).get("propositions") or ()),
        )
        for e in events
    ]
    for table, query in (
        ("utterances", "SELECT count(*) AS n FROM utterances WHERE story_id = :s"),
        ("memories", "SELECT count(*) AS n FROM memories WHERE story_id = :s"),
    ):
        (count,) = await rows(engine, query, s=story_id)
        found.append((table, count.n))
    return found


async def test_a_composition_failure_changes_no_beat(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The same turn in two stories: one polish passes, the other faults (an answer that stays
    invalid). Both turns commit; their beat events, utterances and memories are the same row
    for row; only the message differs, the faulted one the deterministic assembly with
    ``composition_fallback`` and the reserved detection released (complement: the passing one
    spends it)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        clean, clean_ids = await structured_story(client, "Wren", "Ada")
        faulted, faulted_ids = await structured_story(client, "Wren", "Ada")

        stub.script("generation", [_turn()])
        stub.script(COMPOSITION, [json.dumps({"text": f'"{LINE}" Ada says. {ACTION}'})])
        passed = await respond(client, clean, body(clean_ids["Wren"], "Hello?"))
        assert passed.status_code == 200, passed.text
        assert passed.json()["message"]["mode"] == "polished"
        report = (await turn_report(engine, clean))["composition"]
        assert (report["detection_reserved"], report["detection_released"]) == (True, False)

        stub.script("generation", [_turn()])
        stub.fault(COMPOSITION, "garbage", n=10)
        failed = await respond(client, faulted, body(faulted_ids["Wren"], "Hello?"))
        assert failed.status_code == 200, failed.text
        played = failed.json()
        assert played["message"] == {
            "message_id": played["message"]["message_id"],
            "text": DRAFT,
            "mode": "deterministic",
        }
        assert {"step": "composition_fallback", "cause": "composition"} in played[
            "degradation_details"
        ]
        report = (await turn_report(engine, faulted))["composition"]
        assert (report["cause"], report["polished"]) == ("model", True)
        assert (report["detection_reserved"], report["detection_released"]) == (True, True)
        assert played["budget"]["used"] <= played["budget"]["max_calls"]

        assert await _beat_rows(engine, faulted) == await _beat_rows(engine, clean)


async def test_one_beat_and_polish_off_skip_the_polish(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A one-beat turn is assembled with no polish call and no degradation entry; so is every
    turn of a story whose polish pass is off (``repair.polish.enabled``), and its detection
    call is never reserved. Complement: the same multi-beat turn with the pass on is polished."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")

        stub.script("generation", [render(beat("say", LINE, intent="sincere"))])
        one = await respond(client, story, body(ids["Wren"], "Hello?"))
        assert one.status_code == 200, one.text
        assert one.json()["message"] == {
            "message_id": one.json()["message"]["message_id"],
            "text": f'Ada: "{LINE}"',
            "mode": "deterministic",
        }
        assert one.json()["degradation"] == []
        assert _polish_calls(stub) == 0

        stub.script("generation", [_turn()])
        polished = await respond(client, story, body(ids["Wren"], "Hello?"))
        assert polished.json()["message"]["mode"] == "polished"
        assert _polish_calls(stub) == 1

        await override(client, story, {"repair": {"polish": {"enabled": False}}})
        stub.script("generation", [_turn()])
        off = await respond(client, story, body(ids["Wren"], "Hello?"))
        assert off.status_code == 200, off.text
        assert off.json()["message"]["text"] == DRAFT
        assert off.json()["message"]["mode"] == "deterministic"
        assert "composition_fallback" not in off.json()["degradation"]
        assert _polish_calls(stub) == 1
        report = (await turn_report(engine, story))["composition"]
        assert (report["mode"], report["detection_reserved"]) == ("deterministic", False)


async def test_a_spent_budget_skips_the_polish_first(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """With a budget that pays for the renders and their detection but not for the polish and
    its detection, the polish is skipped (NFR-PERF-7's first step, ``polish_skipped_budget``),
    the deterministic assembly is released and the turn spends no more than its budget.
    Complement: the default budget pays for the polish."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Wren", "Ada")
        stub.script("generation", [_turn()])
        full = await respond(client, story, body(ids["Wren"], "Hello?"))
        assert full.json()["message"]["mode"] == "polished"
        spent = full.json()["budget"]["used"]

        stub.script("generation", [_turn()])
        tight = await respond(
            client, story, body(ids["Wren"], "Hello?", budget={"max_calls": spent - 1})
        )
        assert tight.status_code == 200, tight.text
        played = tight.json()
        assert played["message"] == {
            "message_id": played["message"]["message_id"],
            "text": DRAFT,
            "mode": "deterministic",
        }
        assert {"step": "polish_skipped_budget", "cause": "budget"} in played["degradation_details"]
        assert played["budget"]["used"] <= spent - 1
        report = (await turn_report(engine, story))["composition"]
        assert (report["polished"], report["cause"]) == (False, None)


async def test_lite_reply_is_unchanged(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A Lite turn makes no composition call and its reply carries no ``mode``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        created = await create(client, story_body("Wren", "Ada"))
        wren = next(c["id"] for c in created["characters"] if c["name"] == "Wren")
        response = await respond(client, created["story_id"], body(wren, "Hello?"))
        assert response.status_code == 200, response.text
        assert set(response.json()["message"]) == {"message_id", "text"}
        assert _polish_calls(epistrel.models.stub) == 0
