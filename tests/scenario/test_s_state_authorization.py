"""S-STATE-AUTHORIZATION: bounds are not permission (docs/04 §7; FR-STATE-10,12; #188).

Cal (the player) and Mira the smith, played end to end through ``respond`` with the stub
adapter, Mira's render scripted by name and the narrator's by its contract line. Gold is
per-character and conserved; its sources are any character, the director and the consumer;
an increase is justified only by a ``sale``, ``reward``, ``theft`` or ``loot``; one effect and
one turn may move at most 500. Cal holds 1,000.

- Mira's render declares ``gold +100000, justification: loot`` ("finds treasure"): a world gain
  is a world fact, so the effect is unpermitted (``world_gain_needs_director``), repaired (the
  deterministic rung narrates the attempt failing) and nothing changes.
- The director's narration introduces 300 gold for Cal, justified by itself (``loot``): applied.
  Above the cap (800) it is repaired down to the cap: 500 commits, the narration says 500, and
  the response marks it ``capped: true`` with the declared 800. Two 400 rewards in one turn
  under ``per_turn: 500``: the second is capped to 100.
- A source not on the State's list (Mira's render on a director-only ``favor``) is unpermitted
  (``source``).
- A purchase with both purses tracked: one ``transfer`` writes ``transfer_out`` on Cal's purse
  and ``transfer_in`` on Mira's; the deltas sum to zero. A transfer from the untracked ``till``
  writes Mira's side only and reports ``counterparty: untracked``.
- Complements: the consumer grants +100,000 through the State API (no render cap or
  justification applies); with ``conserved: false`` a transfer may be unbalanced; with
  ``consumer`` removed from the sources the same ``PUT`` is 422 ``state_unpermitted {reason:
  source}``; and ``rules`` on a State whose sources omit ``rule`` is 422 ``state_unpermitted``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.states import StateDefinition
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, turn_report

pytestmark = pytest.mark.integration

LABELS = ["sale", "reward", "theft", "loot"]
NARRATOR_BEAT = 2
"""The narrator renders first after the input (beat 1), then Mira."""
MIRA_BEAT = 3


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _gold() -> StateDefinition:
    return ss.gold(
        authorization={
            "sources": ["any_character", "director", "consumer"],
            "justifications": {"increase": LABELS},
            "caps": {"per_effect": 500, "per_turn": 500},
        },
        transfer={"conserved": True, "counterparties": ["till"]},
    )


def _favor() -> StateDefinition:
    return StateDefinition.model_validate(
        {
            "name": "favor",
            "type": "number",
            "scope": "story",
            "initial_value": 0,
            "authorization": {"sources": ["director"]},
        }
    )


async def _play(client: Any, story: str, actor: str, do: str) -> dict[str, Any]:
    answer = await respond(client, story, body(actor, None, do=do))
    assert answer.status_code == 200, answer.text
    played: dict[str, Any] = answer.json()
    return played


async def _verdict_states(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    """The State findings the turns' ``verify_verdict`` records name (codes only)."""
    found = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'verify_verdict' ORDER BY id",
        s=story,
    )
    return [v["state"] for r in found for v in r.payload["verdicts"] if v.get("state")]


def _effect(state: str, delta: int, **fields: Any) -> dict[str, Any]:
    return es.effect(state, delta=delta, **fields)


@pytest.mark.part("M6", "FR-STATE-10,12")
async def test_s_state_authorization(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, _gold(), _favor(), gold_of_player=1000)
        cal, mira = ids["Cal"], ids["Mira"]

        async def turn(
            mira_beat: dict[str, Any],
            narration: dict[str, Any],
            do: str,
            regeneration: str | None = None,
        ) -> dict[str, Any]:
            stub.reset()
            ms.renders_for(stub, "Mira", es.render(mira_beat))
            es.narrator(stub, es.render(narration))
            if regeneration is not None:
                stub.script("regeneration", [regeneration])
            return await _play(client, story, cal, do)

        quiet = beat("narration", "The forge glows.")

        # -- A character's world gain: unpermitted, repaired, nothing changes.
        treasure = "Mira pries up a floorboard and finds 100000 gold."
        loot = _effect(
            "gold",
            100_000,
            scope_key=mira,
            reason="found treasure",
            justification="loot",
            justified_by=MIRA_BEAT,
        )
        played = await turn(
            beat("do", treasure, effects=[loot]),
            quiet,
            "watches Mira work",
            regeneration=json.dumps({"text": treasure, "effects": [loot]}),
        )
        assert "state_effects" not in played
        assert await es.gold_of(engine, story, mira) == 0
        assert not await es.turn_changes(engine, story)
        report = await turn_report(engine, story)
        assert [r["tag"] for r in report["repairs"]] == ["state_unpermitted"]
        assert report["state_effects"]["repaired"] == {"state_unpermitted": 1}
        (verdict,) = await _verdict_states(engine, story)
        assert (verdict["tag"], verdict["constraint"]) == (
            "state_unpermitted",
            "world_gain_needs_director",
        )
        assert treasure not in played["message"]["text"]

        # -- The director introduces treasure within the cap: applied.
        played = await turn(
            beat("say", "Mind the sparks.", intent="sincere"),
            beat(
                "narration",
                "A traveller's purse bursts open and 300 gold rolls to Cal's feet.",
                effects=[
                    _effect(
                        "gold",
                        300,
                        scope_key=cal,
                        reason="a dropped purse",
                        justification="loot",
                        justified_by=NARRATOR_BEAT,
                    )
                ],
            ),
            "looks around",
        )
        assert [(e["old_value"], e["new_value"]) for e in played["state_effects"]] == [(1000, 1300)]
        assert "capped" not in played["state_effects"][0]

        # -- Above the cap: repaired down to it; the response keeps the declared amount.
        played = await turn(
            beat("say", "Lucky you.", intent="sincere"),
            beat(
                "narration",
                "A chest cracks open and 800 gold spills toward Cal.",
                effects=[
                    _effect(
                        "gold",
                        800,
                        scope_key=cal,
                        reason="a chest",
                        justification="loot",
                        justified_by=NARRATOR_BEAT,
                    )
                ],
            ),
            "opens the chest",
        )
        (capped,) = played["state_effects"]
        assert (capped["old_value"], capped["new_value"]) == (1300, 1800)
        assert (capped["capped"], capped["declared"]) == (True, 800)
        assert any("500 gold spills" in b["text"] for b in played["beats"])
        assert not any("800 gold" in b["text"] for b in played["beats"])
        report = await turn_report(engine, story)
        assert report["state_effects"]["repaired"] == {"state_capped": 1}

        # -- Per-turn caps: two 400 rewards under per_turn 500, the second capped to 100.
        played = await turn(
            beat("say", "Well earned.", intent="sincere"),
            beat(
                "narration",
                "The guild pays Cal a reward, and the mayor adds another.",
                effects=[
                    _effect(
                        "gold",
                        400,
                        scope_key=cal,
                        reason="the guild's reward",
                        justification="reward",
                        justified_by=NARRATOR_BEAT,
                    ),
                    _effect(
                        "gold",
                        400,
                        scope_key=cal,
                        reason="the mayor's reward",
                        justification="reward",
                        justified_by=NARRATOR_BEAT,
                    ),
                ],
            ),
            "accepts the rewards",
        )
        assert [(e["old_value"], e["new_value"]) for e in played["state_effects"]] == [
            (1800, 2200),
            (2200, 2300),
        ]
        assert [e.get("declared") for e in played["state_effects"]] == [None, 400]

        # -- A source not on the State's list: unpermitted.
        played = await turn(
            beat(
                "do",
                "Mira raises a cup to Cal's favor.",
                effects=[_effect("favor", 1, reason="a toast")],
            ),
            quiet,
            "raises a cup",
            regeneration='{"text": "Mira raises a cup.", "effects": []}',
        )
        assert "state_effects" not in played
        assert await ss.value(engine, story, "favor") == 0
        constraints = [v["constraint"] for v in await _verdict_states(engine, story)]
        assert constraints[-1] == "source"

        # -- A purchase with both purses tracked: the transfer balances.
        sold = "Mira takes the coins and hands Cal the sword."
        played = await turn(
            beat(
                "do",
                sold,
                effects=[
                    {
                        "kind": "transfer",
                        "state": "gold",
                        "scope_key": cal,
                        "delta": -500,
                        "counterparty": {"state": "gold", "scope_key": mira},
                        "reason": "a sword",
                    }
                ],
            ),
            quiet,
            "buys a sword for 500 gold",
        )
        sides = played["state_effects"]
        assert [(e["scope_key"], e["kind"]) for e in sides] == [
            (cal, "transfer_out"),
            (mira, "transfer_in"),
        ]
        assert sum(e["new_value"] - e["old_value"] for e in sides) == 0
        assert (await es.gold_of(engine, story, cal), await es.gold_of(engine, story, mira)) == (
            1800,
            500,
        )
        assert {r.kind for r in await es.turn_changes(engine, story) if r.reason == "a sword"} == {
            "transfer_out",
            "transfer_in",
        }

        # -- An untracked counterparty: only the tracked side is written.
        till = "Mira sells a dagger and pockets 20 gold from the till."
        played = await turn(
            beat(
                "do",
                till,
                effects=[
                    {
                        "kind": "transfer",
                        "state": "gold",
                        "scope_key": mira,
                        "delta": 20,
                        "counterparty": {"state": "till"},
                        "reason": "a dagger",
                        "justification": "sale",
                        "justified_by": MIRA_BEAT,
                    }
                ],
            ),
            quiet,
            "browses the daggers",
        )
        (side,) = played["state_effects"]
        assert (side["scope_key"], side["kind"], side["counterparty"]) == (
            mira,
            "transfer_in",
            "untracked",
        )
        assert await es.gold_of(engine, story, mira) == 520

        # -- Complement: the consumer grants more through the State API, uncapped.
        granted = await client.put(
            f"/stories/{story}/states/gold",
            json={"scope_key": cal, "delta": 100_000, "reason": "the author's gift"},
            headers=WRITE,
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["new_value"] == 101_800

        # -- Complement: under conserved: false a transfer may be unbalanced.
        loosened = await client.patch(
            f"/stories/{story}/states/gold/definition",
            json={"transfer": {"conserved": False, "counterparties": ["till"]}},
            headers=WRITE,
        )
        assert loosened.status_code == 200, loosened.text
        played = await turn(
            beat(
                "do",
                "Mira takes 100 gold and hands Cal a whetstone worth 50.",
                effects=[
                    {
                        "kind": "transfer",
                        "state": "gold",
                        "scope_key": cal,
                        "delta": -100,
                        "counterparty": {"state": "gold", "scope_key": mira, "delta": 50},
                        "reason": "a whetstone, overpriced",
                    }
                ],
            ),
            quiet,
            "buys a whetstone",
        )
        assert [e["new_value"] - e["old_value"] for e in played["state_effects"]] == [-100, 50]

        # -- With consumer removed from the sources, the same PUT is state_unpermitted.
        closed = await client.patch(
            f"/stories/{story}/states/gold/definition",
            json={"authorization": {"sources": ["any_character", "director"]}},
            headers=WRITE,
        )
        assert closed.status_code == 200, closed.text
        refused = await client.put(
            f"/stories/{story}/states/gold",
            json={"scope_key": cal, "delta": 100_000, "reason": "the author's gift"},
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["details"]["reason"]) == ("state_unpermitted", "source")

        # -- Rules on a State whose sources omit rule: refused at update. ``favor`` is a
        # story-scoped number State with an initial value, so the elapsed rule is otherwise valid.
        ruled = await client.patch(
            f"/stories/{story}/states/favor/definition",
            json={
                "rules": {"kind": "elapsed", "per_minutes": 60, "delta": 1, "bounds_clamp": True}
            },
            headers=WRITE,
        )
        assert ruled.status_code == 422, ruled.text
        error = ruled.json()["error"]
        assert (error["code"], error["details"]["reason"]) == ("state_unpermitted", "source")
