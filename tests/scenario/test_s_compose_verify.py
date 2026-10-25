"""S-COMPOSE-VERIFY: the polish step is verified (docs/04 §7; FR-RESP-8; #152).

Jenni answers Bobby (the player) with a thought, a line and an action. The composition's polish
is scripted by its template (``stub.script("composition.j2", …)``), so each turn's composed text
is exactly the one under test:

- (a) it adds a claim no beat makes (Jenni walks to the tavern);
- (b) it gives Jenni's line to Bobby;
- (c) it carries a phrase of Jenni's ``think``.

Each is caught by the final check, the response is the deterministic assembly of the verified
beats (``mode: deterministic``, ``degradation: ["composition_fallback"]``), the check's failure
kind is reported, and the rejected text is kept only as a ``composition_rejected`` audit
record. Complement: a clean composition (reordered with a speech cue, the same words) passes
and is returned verbatim with ``mode: polished``.

Played end to end through ``respond`` with the stub adapter.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, place, render, structured_story, turn_report

pytestmark = pytest.mark.integration

COMPOSITION = "composition.j2"
THOUGHT = "He has no idea what happened at the lake."
LINE = "Hello, Bobby. Sit with me."
ACTION = "She sits down by the fire and warms her hands over it."
DRAFT = f'Jenni: "{LINE}"\n\n*{ACTION}*'
"""The deterministic assembly of the three beats for Bobby's viewpoint (no ``think``)."""
CLEAN = f'"Hello, Bobby. Sit with me," Jenni says. {ACTION}'
ADDED = f'"Hello, Bobby. Sit with me," Jenni says. {ACTION} Jenni walks to the tavern.'
SWAPPED = f'"Hello, Bobby. Sit with me," Bobby says. {ACTION}'
LEAKED = f'"Hello, Bobby. Sit with me," Jenni says. {ACTION} He has no idea what happened.'
FALLBACK = "composition_fallback"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _jenni() -> str:
    return render(beat("think", THOUGHT), beat("say", LINE, intent="sincere"), beat("do", ACTION))


async def _rejected(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'composition_rejected' "
        "ORDER BY seq",
        s=story_id,
    )


@pytest.mark.part("M4", "FR-RESP-8")
async def test_s_compose_verify(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """(a) an added claim, (b) a swapped speaker and (c) a phrase of Jenni's thought are each
    caught and the deterministic assembly is returned; complement: a clean composition passes
    unchanged with ``mode: polished``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Bobby", "Jenni")
        await place(engine, story, "tavern")
        stub = epistrel.models.stub

        async def play(polished: str) -> tuple[dict[str, Any], dict[str, Any]]:
            stub.script("generation", [_jenni()])
            stub.script(COMPOSITION, [json.dumps({"text": polished})])
            response = await respond(client, story, body(ids["Bobby"], "Jenni, are you there?"))
            assert response.status_code == 200, response.text
            return response.json(), (await turn_report(engine, story))["composition"]

        # The complement first: a clean composition is released verbatim.
        played, report = await play(CLEAN)
        assert played["message"] == {
            "message_id": played["message"]["message_id"],
            "text": CLEAN,
            "mode": "polished",
            "narrative": CLEAN,  # #177
        }
        assert FALLBACK not in played["degradation"]
        assert report["mode"] == "polished"
        assert report["check"]["failures"] == []
        assert report["check"]["traced"] == report["check"]["claims"]
        assert await _rejected(engine, story) == []

        # Each injected composition is caught, and the verified beats are assembled instead.
        for polished, kind in (
            (ADDED, "untraced"),
            (SWAPPED, "attribution"),
            (LEAKED, "think_leak"),
        ):
            played, report = await play(polished)
            assert played["message"]["text"] == DRAFT, kind
            assert played["message"]["mode"] == "deterministic", kind
            assert FALLBACK in played["degradation"], kind
            assert {"step": FALLBACK, "cause": "composition"} in played["degradation_details"]
            assert report["mode"] == "deterministic", kind
            assert report["cause"] == "check", kind
            assert kind in {f["kind"] for f in report["rejected"]["failures"]}, report
            # The released assembly itself passes the check (layers a and b).
            assert report["check"]["failures"] == [], kind
            assert THOUGHT not in played["message"]["text"]
            assert (await _rejected(engine, story))[-1].payload["text"] == polished
        assert len(await _rejected(engine, story)) == 3
