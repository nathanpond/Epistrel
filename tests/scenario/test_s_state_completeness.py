"""S-STATE-COMPLETENESS: both directions (docs/04 §7; FR-STATE-13; #190).

Cal (the player) holds 1,000 gold, a player-visible State any render may change, at Mira's
forge. Played end to end through ``respond`` with the stub adapter: Mira's render scripted by
name, the narrator's by its contract line, and the repair ladder's regeneration by purpose.

- **Narrated, not declared.** The narrator renders "You count out 500 gold and hand it over"
  with no effect: caught (``state_undeclared``, the permission check's ``denied:state_change``
  and ``completeness.diff``'s ``undeclared`` one finding, so one repair) and repaired:
  - the regeneration declares ``gold -500``, which is validated and committed (1,000 → 500);
  - in a second turn the regeneration declares nothing, and the deterministic rung removes the
    sentence: nothing changes.
  The committed value is the start plus the turn's committed changes either way.
- **Declared, not narrated.** The narrator declares ``gold -50`` ("paid the toll") on a beat
  that never mentions it: flagged (``state_unnarrated``) and repaired:
  - the regeneration narrates it, and the effect commits;
  - when the regeneration still leaves it unnarrated, the deterministic rung appends one line
    built from the effect's reason, and the effect commits.
- **Silent.** The same unnarrated change on a ``silent`` State, and a ``silent`` effect on
  gold, commit with no repair.

Complements: a narrated change of an untracked quantity ("three loaves") is not flagged.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import StateChangeSource
from epistrel.core.states import StateDefinition
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests import structured_turn_support as st
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client

pytestmark = pytest.mark.integration

COUNTED = "You count out 500 gold and hand it over."
LAYS = "Mira lays the sword on the counter."
GATE = "The forge's back gate swings open."
TOLL = "paid the toll"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _tab() -> StateDefinition:
    """A silent story State: the forge's running tab, never narrated."""
    return StateDefinition.model_validate(
        {"name": "tab", "type": "number", "scope": "story", "initial_value": 0, "silent": True}
    )


async def _verdicts(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    """The latest turn's ``verify_verdict`` entries."""
    found = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'verify_verdict' "
        "ORDER BY id DESC LIMIT 1",
        s=story,
    )
    return [v for r in found for v in r.payload["verdicts"]]


@pytest.mark.part("M6", "FR-STATE-13")
async def test_s_state_completeness(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, ss.gold(), _tab())
        cal = ids["Cal"]
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold", 1000, scope_key=cal, reason="a purse", source=StateChangeSource.CONSUMER
            ),
        )

        async def turn(narration: dict[str, Any], *regenerated: dict[str, Any]) -> dict[str, Any]:
            stub.reset()
            ms.renders_for(stub, "Mira", es.render(st.beat("say", "Good steel, this.")))
            es.narrator(stub, es.render(narration))
            stub.script("regeneration", [json.dumps(r) for r in regenerated])
            answer = await respond(client, story, body(cal, None, do="looks over the sword"))
            assert answer.status_code == 200, answer.text
            played: dict[str, Any] = answer.json()
            return played

        async def consistent(start: int, played: dict[str, Any]) -> int:
            """The committed gold is ``start`` plus the turn's committed changes."""
            now = await es.gold_of(engine, story, cal)
            moved = sum(
                (Decimal(str(e["new_value"])) - Decimal(str(e["old_value"])))
                for e in played.get("state_effects", [])
                if e["state"] == "gold"
            )
            assert now == start + moved, (now, start, moved)
            return int(now)

        # -- Narrated, not declared: the regeneration declares the effect, which commits.
        played = await turn(
            st.beat("narration", f"{LAYS} {COUNTED}"),
            {
                "text": f"{LAYS} {COUNTED}",
                "effects": [es.effect("gold", scope_key=cal, delta=-500, reason="the sword")],
            },
        )
        report = await st.turn_report(engine, story)
        (entry,) = report["repairs"]  # one claim, one repair
        assert (entry["tag"], entry["rung"], entry["accepted"]) == ("state_undeclared", 3, True)
        assert "This beat changes gold: declare the change" in es.regenerations(stub)[0]
        state = [v for v in await _verdicts(engine, story) if "state" in v]
        assert [(v["cause"], v["permission"]) for v in state] == [
            ("state_undeclared", "denied:state_change")
        ]
        assert [(e["state"], e["old_value"], e["new_value"]) for e in played["state_effects"]] == [
            ("gold", 1000, 500)
        ]
        assert COUNTED in played["message"]["text"]
        assert await consistent(1000, played) == 500

        # -- The deterministic rung: the regeneration declares nothing; the sentence goes.
        played = await turn(
            st.beat("narration", f"{LAYS} {COUNTED}"), {"text": f"{LAYS} {COUNTED}"}
        )
        (entry,) = (await st.turn_report(engine, story))["repairs"]
        assert (entry["tag"], entry["rung"]) == ("state_undeclared", 4)
        assert COUNTED not in played["message"]["text"]
        assert LAYS in played["message"]["text"]
        assert "state_effects" not in played
        assert await consistent(500, played) == 500

        # -- Declared, not narrated: the regeneration narrates it, and it commits.
        toll = es.effect("gold", scope_key=cal, delta=-50, reason=TOLL)
        played = await turn(
            st.beat("narration", GATE, effects=[toll]),
            {"text": f"{GATE} You pay the 50 gold toll on the way through."},
        )
        (entry,) = (await st.turn_report(engine, story))["repairs"]
        assert (entry["tag"], entry["rung"], entry["accepted"]) == ("state_unnarrated", 3, True)
        assert (
            "This beat changes gold, but nothing in the turn says so" in (es.regenerations(stub)[0])
        )
        assert "50 gold toll" in played["message"]["text"]
        assert [(e["old_value"], e["new_value"]) for e in played["state_effects"]] == [(500, 450)]
        assert await consistent(500, played) == 450

        # -- Still unnarrated after the regeneration: the deterministic rung appends a line.
        played = await turn(st.beat("narration", GATE, effects=[toll]), {"text": GATE})
        (entry,) = (await st.turn_report(engine, story))["repairs"]
        assert (entry["tag"], entry["rung"]) == ("state_unnarrated", 4)
        assert "Paid the toll (gold -50)." in played["message"]["text"]
        assert [(e["old_value"], e["new_value"]) for e in played["state_effects"]] == [(450, 400)]
        assert await consistent(450, played) == 400

        # -- Silent: a silent State, and a silent effect, commit with no narration and no repair.
        played = await turn(
            st.beat(
                "narration",
                GATE,
                effects=[
                    es.effect("tab", delta=50, reason="the smith's tab"),
                    es.effect("gold", scope_key=cal, delta=-5, reason="a tip", silent=True),
                ],
            )
        )
        assert not (await st.turn_report(engine, story)).get("repairs")
        assert sorted((e["state"], e["new_value"]) for e in played["state_effects"]) == [
            ("gold", 395),
            ("tab", 50),
        ]
        assert await ss.value(engine, story, "tab") == 50

        # -- Complement: an untracked quantity is no State's change.
        await turn(st.beat("narration", "Mira eats three loaves at the anvil."))
        report = await st.turn_report(engine, story)
        assert not [r for r in report.get("repairs", []) if str(r["tag"]).startswith("state_")]
        assert not report["state_effects"]["repaired"]
        assert await es.gold_of(engine, story, cal) == 395
