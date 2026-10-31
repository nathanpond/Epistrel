"""S-STATE-SPEND and S-STATE-INSUFFICIENT: declared State effects (docs/04 §7; FR-STATE-1,4,5,6,
FR-TURN-11, FR-RESP-7; #187).

Cal (the player) buys from Mira the smith, played end to end through ``respond`` with the stub
adapter, Mira's render scripted by name and the narrator's by its contract line.

- ``test_s_state_spend``: Cal holds 1,000 gold. "I buy a new sword for 500 gold": Mira's render
  declares ``gold -500`` with a reason, the change commits with the turn (its operation, its
  beat), and the response returns 1000 → 500 with the reason. Vague narration ("Cal spends most
  of his gold") is ``effect_unresolved``: the ladder asks for a concrete amount, and the
  repaired render's effect commits. Complements: the narrator's effect on a hidden State is in
  the omniscient history and absent from a character-viewpoint response and history; an
  induced failure after the effect and before the beat's records are written leaves no change
  and no beat; the captured prompts show Mira's ``## Tracked values`` with no value and the
  narrator's with every value; with ``states.level`` at ``prompt_only`` neither section
  appears and an effect is ``invalid_structure``, named by module.
- ``test_s_state_insufficient``: Cal holds 300 gold; "I buy the 500-gold sword": the effect
  breaks ``non_negative``, so nothing changes and the beat is repaired in character (the
  regeneration names the rule, never the value); a repair that still overspends falls to the
  deterministic rung (the attempt narrated as refused, the effect dropped). The value is never
  negative, on ``state_values`` and on the fold. Complement: with 600 gold the same purchase
  commits.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import StateChangeSource
from epistrel.orchestrator.structured import pipeline
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, history, turn_report

pytestmark = pytest.mark.integration

BUY = "buys a new sword for 500 gold"
SOLD = "Mira takes the coins and hands Cal the sword."
REASON = "bought a sword"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _play(client: Any, story: str, actor: str, do: str, **fields: Any) -> dict[str, Any]:
    """A turn whose input is an action naming no one: the narrator renders, then Mira."""
    answer = await respond(client, story, body(actor, None, do=do, **fields))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


@pytest.mark.part("M6", "FR-STATE-1,4,5; FR-TURN-11; FR-RESP-7")
async def test_s_state_spend(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, es.gold(), es.suspicion(), gold_of_player=1000)
        cal = ids["Cal"]

        # -- The purchase: gold -500 declared, committed with the turn, returned 1000 → 500.
        stub.reset()
        ms.renders_for(
            stub,
            "Mira",
            es.render(
                beat(
                    "do",
                    SOLD,
                    effects=[es.effect("gold", scope_key=cal, delta=-500, reason=REASON)],
                ),
                beat("say", "Mind the edge.", intent="sincere"),
            ),
        )
        es.narrator(
            stub,
            es.render(
                beat(
                    "narration",
                    "The forge glows behind the counter.",
                    effects=[es.effect("suspicion", delta=2, reason="a stranger with coin")],
                )
            ),
        )
        turn = await _play(client, story, cal, BUY)
        assert turn["state_effects"] == [
            {
                "state": "gold",
                "scope_key": cal,
                "old_value": 1000,
                "new_value": 500,
                "reason": REASON,
                "source": "turn",
                "contingency": "unconditional",
                "status": "committed",
            }
        ]
        assert await es.gold_of(engine, story, cal) == 500
        (change,) = [r for r in await es.turn_changes(engine, story) if r.reason == REASON]
        op = await scalar(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND narrative_turn = :t AND status = 'committed'",
            s=story,
            t=turn["turn"],
        )
        assert str(change.operation_id) == str(op)
        do_beat = next(b for b in turn["beats"] if b["text"] == SOLD)
        assert (change.narrative_turn, change.beat) == (turn["turn"], do_beat["beat"])
        assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)

        # The hidden State changed too, seen only under omniscient (history and respond).
        assert await ss.value(engine, story, "suspicion") == 2
        mine = await _turn_effects(client, story, f"character:{cal}", turn["turn"])
        assert [e["state"] for e in mine] == ["gold"]
        everyone = await _turn_effects(client, story, "omniscient", turn["turn"])
        assert {e["state"] for e in everyone} == {"gold", "suspicion"}
        omniscient_beats = await history(client, story, "omniscient")
        assert any(i.get("effects") for i in omniscient_beats if i["turn"] == turn["turn"])
        assert not any("effects" in i for i in await history(client, story, f"character:{cal}"))

        # The prompts: Mira's lists gold without a value and never the hidden State; the
        # narrator's lists every State with its value.
        (mira_prompt,) = ms.render_prompts(stub, "Mira")
        assert "## Tracked values" in mira_prompt
        assert "gold (number; one per character; unit gp; also called coins)" in mira_prompt
        assert "suspicion" not in mira_prompt
        assert "unease" not in mira_prompt
        es.no_value(mira_prompt, 1000, "1000")
        (narrator_prompt,) = es.prompts(stub, "NarratorRender")
        assert "## Tracked values" in narrator_prompt
        assert f"Cal (id {cal}) 1000 gp" in narrator_prompt
        assert "suspicion (number; the story's; also called unease; hidden" in narrator_prompt

        # -- Vague narration resolves to a concrete effect before commit.
        stub.reset()
        ms.renders_for(
            stub,
            "Mira",
            es.render(beat("do", "Mira wraps the cloak and takes most of his gold for it.")),
        )
        es.narrator(stub, es.render(beat("narration", "Rain taps the shutters.")))
        stub.script(
            "regeneration",
            [
                '{"text": "Mira wraps the cloak and takes 300 gold for it.", '
                f'"effects": [{{"kind": "change", "state": "gold", "scope_key": "{cal}", '
                '"delta": -300, "reason": "a cloak"}]}'
            ],
        )
        turn = await _play(client, story, cal, "takes the cloak too")
        (asked,) = es.regenerations(stub)
        assert "Say exactly how much gold changes" in asked
        assert [(e["old_value"], e["new_value"]) for e in turn["state_effects"]] == [(500, 200)]
        report = await turn_report(engine, story)
        assert report["state_effects"]["repaired"] == {"effect_unresolved": 1}
        assert [r["tag"] for r in report["repairs"]] == ["effect_unresolved"]
        assert await es.gold_of(engine, story, cal) == 200

        # -- Complement: a failure after the effect, before the beat's records: nothing stays.
        before_changes = len(await ss.change_rows(engine, story))
        before_beats = await scalar(
            engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story
        )

        async def induced(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("induced failure before the beats' records")

        monkeypatch.setattr(pipeline, "write_introductions", induced)
        stub.reset()
        ms.renders_for(
            stub,
            "Mira",
            es.render(
                beat(
                    "do",
                    "Mira takes ten coins for the whetstone.",
                    effects=[es.effect("gold", scope_key=cal, delta=-10, reason="a whetstone")],
                )
            ),
        )
        es.narrator(stub, es.render(beat("narration", "Sparks fly.")))
        with pytest.raises(RuntimeError, match="induced"):
            await respond(client, story, body(cal, None, do="asks for a whetstone"))
        monkeypatch.undo()
        ss.deliver(monkeypatch)
        assert len(await ss.change_rows(engine, story)) == before_changes
        assert (
            await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story)
            == before_beats
        )
        assert await es.gold_of(engine, story, cal) == 200

        # -- Complement: at prompt_only no section appears and an effect is invalid.
        await ss.set_level(engine, story, "prompt_only")
        stub.reset()
        ms.renders_for(
            stub,
            "Mira",
            es.render(
                beat(
                    "do",
                    SOLD,
                    effects=[es.effect("gold", scope_key=cal, delta=-5, reason="a nail")],
                )
            ),
        )
        es.narrator(stub, es.render(beat("narration", "Smoke curls.")))
        refused = await respond(client, story, body(cal, None, do="asks for a nail"))
        assert refused.status_code == 503, refused.text
        details = refused.json()["error"]["details"]
        assert details["reason"] == "invalid_structure"
        assert any("module states is prompt_only" in e for e in details["structural_errors"])
        assert all("## Tracked values" not in p for p in ms.render_prompts(stub, "Mira"))
        assert all("## Tracked values" not in p for p in es.prompts(stub, "NarratorRender"))
        assert await es.gold_of(engine, story, cal) == 200


async def _turn_effects(client: Any, story: str, viewpoint: str, turn: int) -> list[Any]:
    response = await client.get(
        f"/stories/{story}/history", params={"viewpoint": viewpoint}, headers=ss_read()
    )
    assert response.status_code == 200, response.text
    (found,) = [t for t in response.json()["turns"] if t["turn"] == turn]
    effects: list[Any] = found.get("state_effects", [])
    return effects


def ss_read() -> dict[str, str]:
    from tests.story_support import READ

    return READ


@pytest.mark.part("M6", "FR-STATE-6")
async def test_s_state_insufficient(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, es.gold(), gold_of_player=300)
        cal = ids["Cal"]
        overspend = es.render(
            beat(
                "do",
                SOLD,
                effects=[es.effect("gold", scope_key=cal, delta=-500, reason=REASON)],
            )
        )

        # -- Repaired in character: the regeneration names the rule, the purchase fails.
        stub.reset()
        ms.renders_for(stub, "Mira", overspend)
        es.narrator(stub, es.render(beat("narration", "The forge hisses.")))
        refusal = "Mira shakes her head: you're short for this blade."
        stub.script("regeneration", [f'{{"text": "{refusal}", "effects": []}}'])
        turn = await _play(client, story, cal, "buys the 500-gold sword")
        (asked,) = es.regenerations(stub)
        assert "there is not enough gold" in asked
        assert "300" not in asked.split("## Repair", 1)[1]
        assert "state_effects" not in turn
        assert any(b["text"] == refusal for b in turn["beats"])
        assert await es.gold_of(engine, story, cal) == 300
        assert not await es.turn_changes(engine, story)
        report = await turn_report(engine, story)
        assert [r["tag"] for r in report["repairs"]] == ["state_constraint"]

        # -- A repair that still overspends: the deterministic rung narrates the refusal.
        stub.reset()
        ms.renders_for(stub, "Mira", overspend)
        es.narrator(stub, es.render(beat("narration", "The forge hisses.")))
        stub.script(
            "regeneration",
            [
                '{"text": "Fine, take it.", "effects": [{"kind": "change", "state": "gold", '
                f'"scope_key": "{cal}", "delta": -400, "reason": "a sword, haggled"}}]}}'
            ],
        )
        turn = await _play(client, story, cal, "buys the 500-gold sword anyway")
        refused = [b for b in turn["beats"] if b["text"].startswith("Mira tries")]
        assert refused
        assert "there is not enough gold" in refused[0]["text"]
        assert "state_effects" not in turn
        report = await turn_report(engine, story)
        assert [(r["tag"], r["rung"]) for r in report["repairs"]] == [("state_constraint", 4)]

        # Never negative: on the projection and on the fold.
        assert await es.gold_of(engine, story, cal) == 300
        assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)
        assert all(
            float(r.value) >= 0
            for r in await rows(
                engine, "SELECT value FROM state_values WHERE story_id = :s", s=story
            )
        )
        assert not await es.turn_changes(engine, story)

        # -- Complement: with 600 gold the same purchase commits.
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold",
                600,
                scope_key=cal,
                reason="a windfall",
                source=StateChangeSource.CONSUMER,
            ),
        )
        stub.reset()
        ms.renders_for(stub, "Mira", overspend)
        es.narrator(stub, es.render(beat("narration", "The forge hisses.")))
        turn = await _play(client, story, cal, "buys the 500-gold sword at last")
        assert [(e["old_value"], e["new_value"]) for e in turn["state_effects"]] == [(600, 100)]
        assert not es.regenerations(stub)
        assert await es.gold_of(engine, story, cal) == 100
