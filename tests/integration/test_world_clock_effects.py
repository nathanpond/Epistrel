"""#996 (FR-CLOCK-12, FR-STATE-5): a model's effect that changes ``world_clock`` never escapes a
structured turn as 422 ``InvalidRequest``.

Cal (the player) buys from Mira the smith, played end to end through ``respond`` with the stub
adapter. Mira's vague narration ("most of his gold") is ``effect_unresolved``, so the repair
ladder asks the ``regeneration`` purpose (``RegeneratedBeat``) for a concrete amount:

- ``test_regeneration_clock_change__corrected``: the first regeneration answer also sets
  ``world_clock``. It gets one structured retry whose correction names the clock; the
  corrected answer commits its gold change and the turn is 200.
- ``test_regeneration_clock_change__dropped``: both answers set ``world_clock``. The clock
  change is dropped (``clock_effect_dropped``), the gold change and the beats commit, the turn
  is 200, and the clock did not move (the complement: no ``world_clock`` change row).
- ``test_generation_clock_change__dropped``: the same rule on the ``generation`` render: the
  render's clock change is corrected once, then dropped, and the turn commits its other effect.
- ``test_client_clock_set__still_reserved``: a client's own set of ``world_clock`` through the
  States API is still 422 ``reserved_state`` (that path is unchanged).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import clock_support as cs
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, override
from tests.structured_turn_support import beat

pytestmark = pytest.mark.integration

VAGUE = "Mira wraps the cloak and takes most of his gold for it."
CONCRETE = "Mira wraps the cloak and takes 300 gold for it."
CLOCK_CORRECTION = (
    "Do not change the world clock in a turn's effects; it moves only through the clock."
)
"""``render.CLOCK_CORRECTION`` (spelled out, so the test runs against the pre-fix code)."""
CLOCK_EFFECT_DROPPED = "clock_effect_dropped"
MODEL_CALLS = 20
CLOCK = {
    "kind": "change",
    "state": "world_clock",
    "delta": 30,
    "reason": "evening falls",
    "silent": True,
}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _gold(cal: str) -> dict[str, Any]:
    return {"kind": "change", "state": "gold", "scope_key": cal, "delta": -300, "reason": "a cloak"}


def _regenerated(*effects: dict[str, Any]) -> str:
    return json.dumps({"text": CONCRETE, "effects": list(effects)})


def _calls(stub: Any, purpose: str) -> list[str]:
    return ["\n".join(m.content for m in c.messages) for c in stub.calls if c.purpose == purpose]


async def _shop(client: Any, engine: AsyncEngine) -> tuple[str, str]:
    """The shop with Cal's 1,000 gold and the world clock on (its State is then in the turn's
    book, as on the eval case that found #996), with room in the turn's budget for the
    regeneration's structured retry."""
    story, ids = await es.shop(client, engine, es.gold(), gold_of_player=1000)
    await override(client, story, {"budgets": {"model_calls": MODEL_CALLS}})
    await ss.set_level(engine, story, "on")
    await cs.set_level(engine, story, "on")
    return story, ids["Cal"]


async def _turn_clock_changes(engine: AsyncEngine, story: str) -> list[Any]:
    """The clock's changes from a turn's effects (the clock's own pacing advance, source
    ``pacing``, is not one)."""
    return list(
        await rows(
            engine,
            "SELECT c.id FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
            "WHERE c.story_id = :s AND d.name = 'world_clock' AND c.source = 'turn'",
            s=story,
        )
    )


def _effect_states(played: dict[str, Any]) -> list[str]:
    """The response's State effects from the turn's declarations (the pacing advance aside)."""
    return [e["state"] for e in played.get("state_effects", []) if e["source"] == "turn"]


async def _vague_turn(
    client: Any, engine: AsyncEngine, stub: Any, *answers: str
) -> tuple[str, str, Any]:
    story, cal = await _shop(client, engine)
    stub.reset()
    ms.renders_for(stub, "Mira", es.render(beat("do", VAGUE)))
    es.narrator(stub, es.render(beat("narration", "Rain taps the shutters.")))
    stub.script("regeneration", [a.replace("CAL", cal) for a in answers])
    answer = await respond(client, story, body(cal, None, do="takes the cloak"))
    return story, cal, answer


async def test_regeneration_clock_change__corrected(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal, answer = await _vague_turn(
            client,
            engine,
            stub,
            _regenerated(_gold("CAL"), CLOCK),
            _regenerated(_gold("CAL")),
        )
        assert answer.status_code == 200, answer.text
        played = answer.json()
        first, retry = _calls(stub, "regeneration")
        assert CLOCK_CORRECTION not in first
        assert CLOCK_CORRECTION in retry
        assert [
            (e["state"], e["old_value"], e["new_value"])
            for e in played["state_effects"]
            if e["source"] == "turn"
        ] == [("gold", 1000, 700)]
        assert CONCRETE in [b["text"] for b in played["beats"]]
        assert CLOCK_EFFECT_DROPPED not in played["degradation"]
        assert await es.gold_of(engine, story, cal) == 700
        assert await _turn_clock_changes(engine, story) == []


async def test_regeneration_clock_change__dropped(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        offending = _regenerated(_gold("CAL"), CLOCK)
        story, cal, answer = await _vague_turn(client, engine, stub, offending, offending)
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert len(_calls(stub, "regeneration")) == 2
        assert CONCRETE in [b["text"] for b in played["beats"]]
        assert _effect_states(played) == ["gold"]
        assert CLOCK_EFFECT_DROPPED in played["degradation"]
        (detail,) = [d for d in played["degradation_details"] if d["step"] == CLOCK_EFFECT_DROPPED]
        assert detail["cause"] == "state"
        assert await es.gold_of(engine, story, cal) == 700
        # The complement: the clock did not move.
        assert await _turn_clock_changes(engine, story) == []


async def test_generation_clock_change__dropped(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, cal = await _shop(client, engine)
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("do", CONCRETE, effects=[_gold(cal), CLOCK])))
        es.narrator(stub, es.render(beat("narration", "Rain taps the shutters.")))
        answer = await respond(client, story, body(cal, None, do="takes the cloak"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        mira = ms.render_prompts(stub, "Mira")
        assert len(mira) == 2
        assert CLOCK_CORRECTION in mira[1]
        assert _effect_states(played) == ["gold"]
        assert CLOCK_EFFECT_DROPPED in played["degradation"]
        assert await es.gold_of(engine, story, cal) == 700
        assert await _turn_clock_changes(engine, story) == []


async def test_client_clock_set__still_reserved(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ids = await es.shop(client, engine, es.gold())
        refused = await client.put(
            f"/stories/{story}/states/world_clock",
            json={"value": 600, "reason": "evening falls"},
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["reason"] == "reserved_state"
        assert await _turn_clock_changes(engine, story) == []
