"""S-CONDITIONAL-EFFECTS: internal effects that wait for external ones (docs/04 §7;
FR-STATE-12,14, FR-EXT-15,16,21).

Every S-CONDITIONAL-EFFECTS M6 part lives in this file: #216's intent side
(``test_s_conditional_effects``), #212's synchronous outcome stage (``__sync``) and #217's late
settlement (``__async``).

#216's part, played end to end through ``respond`` with the stub adapter. Cal's gold is owned
by a write-capable, asynchronous in-process provider (``bank``; #210's ordinary path: the turn
commits with its outbox row and closes). The sword is story-owned inventory: the per-character
``sword`` State tracks the sword entity, and its transfers name the external gold as their
counterparty.

1. **Buying** the sword with an external payment (Mira's render declares ``sword +1`` for Cal
   against ``gold -500``): at intent the grant is a ``pending``, ``conditional`` row waiting on
   the payment's outbox row (``prerequisite_effect_ids``), noted ``counterparty: external``, and
   the sword is not in Cal's inventory. Her beat asserting possession ("Cal now holds the
   sword.") is #209's ``external_outcome_unconfirmed``, repaired to the attempt (wrapping the
   sword). Complement: the narrator's unconditional change in the same turn
   commits at intent.
2. **Selling** it for an external payout (``sword -1`` against ``gold +500``): the sword is
   ``held`` at intent (a ``reservation``), still counted in the value but unavailable: a later
   turn in which Mira takes it from Cal fails #189's availability check
   (``precondition:availability``), and nothing changes.
3. **Timeout / ``unknown``**: both outbox rows answered ``unknown`` leave the grant pending and
   the reservation held; a settlement of ``unknown`` outcomes changes nothing. With
   ``reservation_expiry_minutes`` set, the sale scheduled a ``reservation_expiry`` timer; at
   expiry the hold is released and listed ``reservation_expired``.
4. **Within one ``sequential`` turn** (Bobby and Jenni, primaries): Bobby holds his sword for a
   sale; Jenni's later beat taking it is checked against the turn's staged reservation (#174's
   ``TurnStage``), not only committed rows, and is refused (``precondition:availability``).
5. **In ``simultaneous`` mode** the same two effects on the one held sword are a FR-MULTI-8
   conflict (``state:sword``) settled by the story's tie-break (request order: Bobby).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core import outbox as core_outbox
from epistrel.core.enums import OperationKind, StateChangeSource
from epistrel.core.propositions import store
from epistrel.core.states import StateDefinition
from epistrel.core.states import contingency as ct
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from epistrel.orchestrator.jobs.handlers import reservations
from tests import canon_support as c
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

PRICE = 500
EXPIRY = 120
HOLDS = "Cal now holds the sword."
WRAPS = "Mira wraps the sword in oilcloth."
POSTS = "Cal sends the sword off to the buyer in the city."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _sword(entity: str) -> StateDefinition:
    return StateDefinition.model_validate(
        {
            "name": "sword",
            "type": "number",
            "scope": "character",
            "constraints": {"non_negative": True},
            "initial_value": 0,
            "aliases": ("blade",),
            "entity_id": entity,
            "transfer": {"counterparties": ["gold"]},
            "authorization": {"sources": ["any_character", "director"]},
            "reservation_expiry_minutes": EXPIRY,
        }
    )


async def _bank(engine: AsyncEngine, epistrel: Any, story: str) -> tuple[str, ps.ScriptedProvider]:
    """``story``'s gold, owned by a fresh write-capable asynchronous in-process provider that
    reads 1,000; answers the provider's name and the fake."""
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name))
    fake = ps.ScriptedProvider(name, default=1000).attach(epistrel)
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    return name, fake


async def _swords(engine: AsyncEngine, story: str, *holders: str) -> str:
    """The sword entity and the ``sword`` State (and the hidden ``suspicion``); each of
    ``holders`` holds one sword."""
    async with engine.begin() as conn:
        sword = await store.add_entity(conn, story, "item", "sword")
    await ss.define_all(engine, story, _sword(sword), es.suspicion())
    for holder in holders:
        await ss.change(
            engine,
            story,
            ss.set_to(
                "sword",
                1,
                scope_key=holder,
                reason="an heirloom",
                source=StateChangeSource.CONSUMER,
            ),
        )
    return sword


def _trade(owner: str, delta: int, *, to: str | None = None) -> dict[str, Any]:
    """``owner``'s sword by ``delta`` against ``owner``'s external gold (a purchase or a sale),
    or (``to``) handed to ``to``."""
    other: dict[str, Any] = (
        {"state": "sword", "scope_key": to}
        if to is not None
        else {"state": "gold", "scope_key": owner, "delta": -delta * PRICE}
    )
    return {
        "kind": "transfer",
        "state": "sword",
        "scope_key": owner,
        "delta": delta,
        "counterparty": other,
        "reason": "the sword",
    }


async def _sword_rows(engine: AsyncEngine, story: str) -> list[Any]:
    return [
        r
        for r in await ss.change_rows(engine, story)
        if r.source == "turn" and r.reason == "the sword"
    ]


async def _verdict_states(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    found = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'verify_verdict' ORDER BY id",
        s=story,
    )
    return [v["state"] for r in found for v in r.payload["verdicts"] if v.get("state")]


def _tags(report: dict[str, Any]) -> list[tuple[str, int]]:
    return [(r["tag"], r["rung"]) for r in report.get("repairs", [])]


@pytest.mark.part("M6", "FR-STATE-12,14")
async def test_s_conditional_effects(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (the numbered cases in order)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal, mira = ids["Cal"], ids["Mira"]
        _, fake = await _bank(engine, epistrel, story)
        sword = await _swords(engine, story, cal)

        # -- 1. Buying a sword with an external payment: the grant waits on the payment.
        stub.reset()
        ms.renders_for(
            stub,
            "Mira",
            es.render(beat("do", f"{WRAPS} {HOLDS}", effects=[_trade(cal, 1)])),
        )
        nudge = es.effect("suspicion", delta=1, reason="a stranger watches")
        es.narrator(stub, es.render(beat("narration", "The forge glows.", effects=[nudge])))
        stub.script("regeneration", [json.dumps({"text": WRAPS, "effects": []})])
        answer = await respond(client, story, body(cal, None, do="buys the sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        texts = [b["text"] for b in played["beats"]]
        assert WRAPS in texts
        assert not any("holds the sword" in t for t in texts)
        assert "holds the sword" not in played["message"]["text"]
        assert ("external_outcome_unconfirmed", 3) in _tags(await turn_report(engine, story))
        (grant,) = [e for e in played["state_effects"] if e["state"] == "sword"]
        (payment,) = [e for e in played["state_effects"] if e["state"] == "gold"]
        assert {k: grant[k] for k in ("contingency", "status", "counterparty", "kind")} == {
            "contingency": "conditional",
            "status": "pending",
            "counterparty": "external",
            "kind": "transfer_in",
        }
        assert grant["old_value"] == grant["new_value"] == 1  # no value impact
        assert (payment["status"], payment["delta"]) == ("pending", -PRICE)
        (row,) = await _sword_rows(engine, story)
        assert (row.status, row.contingency) == ("pending", "conditional")
        assert list(row.prerequisite_effect_ids) == [payment["outbox_id"]]
        assert await ss.value(engine, story, "sword", cal) == 1  # the heirloom only
        # Complement: the narrator's unconditional change commits at intent.
        (nudged,) = [r for r in await es.turn_changes(engine, story) if r.reason != "the sword"]
        assert (nudged.status, nudged.contingency) == ("committed", "unconditional")
        assert await ss.value(engine, story, "suspicion") == 1

        # -- 2. Selling it for an external payout: the sword is held, not handed over.
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", "I know a buyer.", intent="sincere")))
        es.narrator(stub, es.render(beat("narration", POSTS, effects=[_trade(cal, -1)])))
        answer = await respond(client, story, body(cal, None, do="sells the sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        (hold,) = [e for e in played["state_effects"] if e["state"] == "sword"]
        (payout,) = [e for e in played["state_effects"] if e["state"] == "gold"]
        assert (hold["contingency"], hold["status"], hold["counterparty"]) == (
            "reservation",
            "held",
            "external",
        )
        held = (await _sword_rows(engine, story))[-1]
        assert (held.status, list(held.prerequisite_effect_ids)) == ("held", [payout["outbox_id"]])
        assert await ss.value(engine, story, "sword", cal) == 1  # held: not transferred yet
        async with engine.connect() as conn:
            assert await ct.held_items(conn, story) == {sword: ct.NOBODY}

        # A later turn giving the held sword away fails #189's availability check.
        before = len(await ss.change_rows(engine, story))
        stub.reset()
        takes = beat(
            "do", "Mira lifts the sword from the rack.", effects=[_trade(cal, -1, to=mira)]
        )
        ms.renders_for(stub, "Mira", es.render(takes))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        stub.script(
            "regeneration", [json.dumps({"text": takes["text"], "effects": takes["effects"]})] * 4
        )
        answer = await respond(client, story, body(cal, None, do="watches Mira"))
        assert answer.status_code == 200, answer.text
        assert len(await ss.change_rows(engine, story)) == before
        assert (await _verdict_states(engine, story))[-1]["constraint"] == (
            "precondition:availability"
        )
        assert await ss.value(engine, story, "sword", cal) == 1
        assert await ss.value(engine, story, "sword", mira) == 0

        # -- 3. Timeout / unknown: the grant stays pending and the reservation held.
        fake.script_writes("gold", "unknown")
        for outbox_id in (payment["outbox_id"], payout["outbox_id"]):
            assert (
                await outbox_job.dispatch(engine, outbox_id, registry=epistrel.in_process_providers)
                == "unknown"
            )
        unknown = {payment["outbox_id"]: "unknown", payout["outbox_id"]: "unknown"}

        async def settle_unknown(conn: Any, op: Any) -> Any:
            return await ct.settle(conn, op, unknown, ss.at(played["turn"], 1))

        _, settled = await c.run(
            engine, story, settle_unknown, kind=OperationKind.CONTROL, turn=played["turn"]
        )
        assert settled == []
        statuses = [r.status for r in await _sword_rows(engine, story)]
        assert statuses == ["pending", "held"]
        listed = (await core_outbox.listings(engine, story)).listings
        assert {(e.outbox_id, e.reason) for e in listed} == {
            (payment["outbox_id"], "unknown"),
            (payout["outbox_id"], "unknown"),
        }

        # With reservation_expiry_minutes set, the hold is released at expiry and listed.
        (timer,) = await rows(
            engine,
            "SELECT due_at, story_id FROM timers WHERE kind = :k AND target = :t",
            k=reservations.RESERVATION_EXPIRY,
            t=held.id,
        )
        assert timer.story_id == story
        lapse = timer.due_at - clock.now()
        assert timedelta(minutes=EXPIRY - 5) < lapse <= timedelta(minutes=EXPIRY)
        assert await reservations.expire(engine, held.id) == reservations.RELEASED
        assert await reservations.expire(engine, held.id) == reservations.SKIPPED
        assert [r.status for r in await _sword_rows(engine, story)] == ["pending", "released"]
        listed = (await core_outbox.listings(engine, story)).listings
        assert {(e.outbox_id, e.reason) for e in listed} == {
            (payment["outbox_id"], "unknown"),
            (payout["outbox_id"], "reservation_expired"),
        }
        async with engine.connect() as conn:
            assert await ct.held_items(conn, story) == {}
        await ps.drop_outbox_jobs(engine, story)
        await ss.set_level(engine, story, None)


async def _room(client: Any, engine: AsyncEngine, epistrel: Any) -> tuple[KeyRoom, str]:
    room = await key_room(client, engine)
    await _bank(engine, epistrel, room.story_id)
    sword = await _swords(engine, room.story_id, room.bobby)
    return room, sword


def _script_pair(stub: Any, room: KeyRoom) -> None:
    stub.reset()
    sells = beat("do", "Bobby wraps his sword for the courier.", effects=[_trade(room.bobby, -1)])
    takes = beat(
        "do",
        "Jenni lifts the sword from the table.",
        effects=[_trade(room.bobby, -1, to=room.jenni)],
    )
    ms.renders_for(stub, "Bobby", ms.render(sells))
    ms.renders_for(stub, "Jenni", ms.render(takes))
    stub.script(
        "regeneration", [json.dumps({"text": takes["text"], "effects": takes["effects"]})] * 4
    )


@pytest.mark.part("M6", "FR-STATE-12,14")
async def test_s_conditional_effects__same_turn(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Cases 4 and 5 of the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- 4. Sequential: Jenni's later beat is checked against Bobby's staged hold.
        room, _ = await _room(client, engine, epistrel)
        _script_pair(stub, room)
        ask = body(
            room.cal,
            "Sell it, Bobby!",
            render_character_ids=[room.bobby, room.jenni],
            interaction_mode="sequential",
        )
        answer = await respond(client, room.story_id, ask)
        assert answer.status_code == 200, answer.text
        sides = [r for r in await _sword_rows(engine, room.story_id)]
        assert [(r.scope_key, r.status) for r in sides] == [(room.bobby, "held")]
        assert (await _verdict_states(engine, room.story_id))[-1]["constraint"] == (
            "precondition:availability"
        )
        assert await ss.value(engine, room.story_id, "sword", room.jenni) == 0

        # -- 5. Simultaneous: the two effects on the one held sword are a conflict.
        room, _ = await _room(client, engine, epistrel)
        _script_pair(stub, room)
        ask = body(
            room.cal,
            "Sell it, Bobby!",
            render_character_ids=[room.bobby, room.jenni],
            interaction_mode="simultaneous",
        )
        answer = await respond(client, room.story_id, ask)
        assert answer.status_code == 200, answer.text
        played = answer.json()
        assert played["adjudications"] == [
            {
                "object": f"state:sword:{room.bobby}",
                "winner": room.bobby,
                "loser": room.jenni,
                "rule": "request_order",
            }
        ]
        sides = await _sword_rows(engine, room.story_id)
        assert [(r.scope_key, r.status) for r in sides] == [(room.bobby, "held")]
        assert await ss.value(engine, room.story_id, "sword", room.jenni) == 0
