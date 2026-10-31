"""S-STATE-PRECONDITIONS: funds don't make a sale (docs/04 §7; FR-STATE-11; #189).

Cal (the player) holds 1,000 gold and tries to buy Mira's 500-gold sword, played end to end
through ``respond`` with the stub adapter. Gold is per-character and conserved. Canon says Mira
holds and owns the sword. The narrator's beat declares the purchase, a ``transfer`` of 500 from
Cal's gold to Mira's with ``consideration: sword``; Mira's ``say`` beat declares (or refuses)
``agreed_to(Mira, sword)``.

- Variant 1: Mira refuses (a negated ``agreed_to``). The transfer is unpermitted
  (``precondition: consent``), the narration is repaired to the refused attempt, and nothing
  changes: sufficient gold alone never produces a transfer.
- Variant 2: Mira agrees, but the sword is ``holds(the knight)`` (sold already): unpermitted
  (``precondition: availability``), no transfer.
- Variant 3: Mira agrees and the sword is hers to sell: the transfer commits, 500 out of Cal's
  purse and 500 into Mira's.
- Complement: with consent and an available shield of 800, Cal's 500 is not enough, so the
  effect fails on the bounds (``state_constraint``, the turn's ``state_constraint_violation``:
  ``non_negative``), not on a precondition; the two reasons are distinguishable. Every
  precondition read is a query key of the turn's read set.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import AGREED_TO, HOLDS, OWNS
from epistrel.core.readset import StructuredReadSet
from epistrel.core.states import StateDefinition
from epistrel.core.states import preconditions as pc
from epistrel.core.states.authorize import Counterparty, StateEffect
from tests import canon_support as c
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, turn_report

pytestmark = pytest.mark.integration

PRICE = 500


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _gold() -> StateDefinition:
    return ss.gold(
        authorization={"sources": ["any_character", "director", "consumer"]},
        transfer={"conserved": True},
    )


def _fact(subject: str, predicate: str, item: str, *, negated: bool = False) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=item,
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
        valid_time=Interval.of(None, None),
    )


async def _author(engine: AsyncEngine, story: str, proposition: Proposition) -> Any:
    return await c.commit(
        engine, story, proposition, kind=OperationKind.CONTROL, provenance="author"
    )


async def _verdict_states(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    """The State findings the turns' ``verify_verdict`` records name (codes only)."""
    found = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'verify_verdict' ORDER BY id",
        s=story,
    )
    return [v["state"] for r in found for v in r.payload["verdicts"] if v.get("state")]


@pytest.mark.part("M6", "FR-STATE-11")
async def test_s_state_preconditions(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, _gold(), gold_of_player=1000)
        cal, mira = ids["Cal"], ids["Mira"]
        async with engine.begin() as conn:
            sword = await store.add_entity(conn, story, "item", "sword")
            shield = await store.add_entity(conn, story, "item", "shield")
            knight = await store.add_entity(conn, story, "person", "the knight")
        held = await _author(engine, story, _fact(mira, HOLDS, sword))
        await _author(engine, story, _fact(mira, OWNS, sword))
        await _author(engine, story, _fact(mira, HOLDS, shield))
        await _author(engine, story, _fact(mira, OWNS, shield))

        def purchase(item: str, price: int = PRICE) -> dict[str, Any]:
            return {
                "kind": "transfer",
                "state": "gold",
                "scope_key": cal,
                "delta": -price,
                "counterparty": {"state": "gold", "scope_key": mira},
                "reason": f"the {item}",
                "consideration": item,
            }

        def sale(item: str, price: int = PRICE) -> dict[str, Any]:
            return beat(
                "narration",
                f"The {item} is priced at {price} gold.",
                effects=[purchase(item, price)],
            )

        async def turn(mira_beat: dict[str, Any], narration: dict[str, Any]) -> dict[str, Any]:
            stub.reset()
            ms.renders_for(stub, "Mira", es.render(mira_beat))
            es.narrator(stub, es.render(narration))
            # A regeneration that declares the same purchase again: the deterministic rung
            # narrates the refusal.
            stub.script("regeneration", [json.dumps(narration)] * 4)
            answer = await respond(client, story, body(cal, None, do="buys the sword"))
            assert answer.status_code == 200, answer.text
            played: dict[str, Any] = answer.json()
            return played

        def agrees(item: str, *, negated: bool = False) -> dict[str, Any]:
            return beat(
                "say",
                "No. It is not for sale." if negated else "Done. It is yours.",
                intent="sincere",
                propositions=[claim("Mira", "agreed_to", item, negated=negated)],
            )

        # -- Variant 1: the merchant refuses; 1,000 gold buys nothing.
        played = await turn(agrees("sword", negated=True), sale("sword"))
        assert "state_effects" not in played
        assert await es.gold_of(engine, story, cal) == 1000
        assert await es.gold_of(engine, story, mira) == 0
        assert not await es.turn_changes(engine, story)
        report = await turn_report(engine, story)
        assert report["state_effects"]["repaired"] == {"state_unpermitted": 1}
        (verdict,) = await _verdict_states(engine, story)
        assert (verdict["tag"], verdict["constraint"]) == (
            "state_unpermitted",
            "precondition:consent",
        )
        text = " ".join(b["text"] for b in played["beats"])
        assert "has not agreed" in text
        assert "priced at" not in text

        # -- Variant 2: Mira agrees, but the sword is gone: the knight holds it now.
        released = await c.supersede(
            engine,
            story,
            held.id,
            _fact(mira, HOLDS, sword, negated=True),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
        knights = await _author(engine, story, _fact(knight, HOLDS, sword))
        played = await turn(agrees("sword"), sale("sword"))
        assert "state_effects" not in played
        assert await es.gold_of(engine, story, cal) == 1000
        assert not await es.turn_changes(engine, story)
        assert (await _verdict_states(engine, story))[-1]["constraint"] == (
            "precondition:availability"
        )

        # -- Variant 3: consent and the sword available again: the transfer commits.
        await c.supersede(
            engine,
            story,
            knights.id,
            _fact(knight, HOLDS, sword, negated=True),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
        await c.supersede(
            engine,
            story,
            released.id,
            _fact(mira, HOLDS, sword),
            kind=OperationKind.CONTROL,
            provenance="author",
        )
        played = await turn(agrees("sword"), sale("sword"))
        sides = played["state_effects"]
        assert [(e["scope_key"], e["kind"]) for e in sides] == [
            (cal, "transfer_out"),
            (mira, "transfer_in"),
        ]
        assert [e["new_value"] - e["old_value"] for e in sides] == [-PRICE, PRICE]
        assert (await es.gold_of(engine, story, cal), await es.gold_of(engine, story, mira)) == (
            500,
            500,
        )
        report = await turn_report(engine, story)
        assert not report["state_effects"].get("repaired")
        verdicts = len(await _verdict_states(engine, story))

        # -- Complement: consent and the shield available, but 800 is more than Cal has:
        # the bounds refuse it, distinguishably from a precondition.
        played = await turn(agrees("shield"), sale("shield", 800))
        assert "state_effects" not in played
        assert (await es.gold_of(engine, story, cal), await es.gold_of(engine, story, mira)) == (
            500,
            500,
        )
        found = (await _verdict_states(engine, story))[verdicts:]
        assert [(v["tag"], v["constraint"]) for v in found] == [
            ("state_constraint", "non_negative")
        ]
        assert not any(str(v["constraint"]).startswith("precondition:") for v in found)

        # -- Every precondition read is a query key of the turn's read set (#136).
        read_set = StructuredReadSet()
        read_set.start_attempt()
        effect = StateEffect(
            "gold",
            cal,
            kind="transfer",
            delta=Decimal(-PRICE),
            counterparty=Counterparty("gold", mira),
            consideration="sword",
            beat=2,
        )
        async with engine.connect() as conn:
            read = await pc.read(conn, read_set, story, 9, [effect], [])
        assert set(read.holds) == set(read.owns) == {sword}
        assert set(read.agreed) == {mira}
        keys = {(k.predicate_id, k.object_id, k.subjects) for k in read_set.query_keys}
        assert {(HOLDS, sword, None), (OWNS, sword, None), (AGREED_TO, None, (mira,))} <= keys
