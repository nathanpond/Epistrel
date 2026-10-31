"""S-EFFECT-PROSE: prose never runs ahead of an effect's outcome (docs/04 §7; FR-EXT-15,
FR-RESP-8).

Every S-EFFECT-PROSE M6 part lives in this file: #209's consumer-applied part
(``__consumer_applied``), #210's and #212's parts, and #217's outcome tails.

#209's part, played end to end through ``respond`` with the stub adapter. Cal's gold is owned by
a read-only provider (it reads 300); Mira's render declares ``gold -50`` for a sword:

1. Her beat says "The purchase went through.": no outcome exists, so the sentence is
   ``external_outcome_unconfirmed``; the repair asks her render for the attempt only (the
   regeneration's constraint), the repaired beat ("Cal hands over the coins.") keeps the
   declared effect, and the response marks it ``pending``.
2. Her beat says "Your balance is now 250." and the repair still asserts it: the deterministic
   rung removes the sentence and keeps the effect (the pinned value is still 300).
3. A polished composition that adds "The purchase went through." fails #152's check
   (``external_outcome``) and the deterministic assembly is released.
4. The next turn's narrator inputs carry ``pending_outcomes`` (``## Awaiting the game``, the
   requests of the last three turns with no outcome) and no character render's prompt does
   (a character learns an outcome only by perception).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

REASON = "bought a sword"
TAKES = "Mira takes the coins."
WENT = "The purchase went through."
BALANCE = "Your balance is now 250."
ATTEMPT = "Cal hands over the coins."
COMPOSITION = "composition.j2"
AWAITING = "## Awaiting the game"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _mira(cal: str, text: str) -> str:
    return es.render(
        beat("do", text, effects=[es.effect("gold", scope_key=cal, delta=-50, reason=REASON)]),
        beat("say", "Mind the edge.", intent="sincere"),
    )


def _tags(report: dict[str, Any]) -> list[tuple[str, int]]:
    return [(r["tag"], r["rung"]) for r in report.get("repairs", [])]


def _pending(played: dict[str, Any]) -> dict[str, Any]:
    (effect,) = played["state_effects"]
    assert (effect["owner"], effect["status"], effect["delta"]) == ("external", "pending", -50)
    assert effect["effect_id"]
    return dict(effect)


@pytest.mark.part("M6", "FR-EXT-15; FR-RESP-8")
async def test_s_effect_prose__consumer_applied(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (each numbered case in order)."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        ps.ScriptedProvider(name, default=300).attach(epistrel)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)

        # 1. The outcome asserted: repaired to the attempt, the effect kept and pending.
        stub.reset()
        ms.renders_for(stub, "Mira", _mira(cal, f"{TAKES} {WENT}"))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        stub.script("regeneration", [json.dumps({"text": ATTEMPT, "effects": []})])
        answer = await respond(client, story, body(cal, None, do="buys the sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        (asked,) = es.regenerations(stub)
        assert "has not said yet: narrate only the attempt" in asked
        assert "300" not in asked.split("## Repair", 1)[1]
        texts = [b["text"] for b in played["beats"]]
        assert ATTEMPT in texts
        assert not any("went through" in t for t in texts)
        assert "went through" not in played["message"]["text"]
        first = _pending(played)
        report = await turn_report(engine, story)
        assert _tags(report) == [("external_outcome_unconfirmed", 3)]
        assert await es.turn_changes(engine, story) == []

        # 2. A repair that still asserts it: the deterministic rung drops the sentence and
        # keeps the effect; the pinned value is still 300.
        stub.reset()
        ms.renders_for(stub, "Mira", _mira(cal, f"{TAKES} {BALANCE}"))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        stub.script("regeneration", [json.dumps({"text": BALANCE, "effects": []})])
        answer = await respond(client, story, body(cal, None, do="buys another sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        texts = [b["text"] for b in played["beats"]]
        assert TAKES in texts
        assert not any("250" in t for t in texts)
        second = _pending(played)
        assert second["old_value"] == 300
        assert second["effect_id"] != first["effect_id"]
        assert ("external_outcome_unconfirmed", 4) in _tags(await turn_report(engine, story))

        # 3. A polished composition adding the outcome fails #152's check.
        stub.reset()
        ms.renders_for(stub, "Mira", _mira(cal, TAKES))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        polished = f"The forge glows. {TAKES} {WENT} Mira says, Mind the edge."
        stub.script(COMPOSITION, [json.dumps({"text": polished})])
        answer = await respond(client, story, body(cal, None, do="buys a third sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert played["message"]["mode"] == "deterministic"
        assert "went through" not in played["message"]["text"]
        composition = (await turn_report(engine, story))["composition"]
        assert "external_outcome" in [f["kind"] for f in composition["rejected"]["failures"]]
        _pending(played)

        # 4. The next turn: pending_outcomes in the narrator's inputs, in no character's.
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Rain taps.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Anything else?")))
        answer = await respond(client, story, body(cal, None, do="waits"))
        assert answer.status_code == 200, answer.text
        (narrator_prompt,) = es.prompts(stub, "NarratorRender")
        section = narrator_prompt.split(AWAITING, 1)[1].split("\n## ", 1)[0]
        assert section.count(f"gold -50 (Cal's): {REASON}") == 3
        for prompt in ms.render_prompts(stub, "Mira"):
            assert AWAITING not in prompt
            assert REASON not in prompt
        await ss.set_level(engine, story, None)
