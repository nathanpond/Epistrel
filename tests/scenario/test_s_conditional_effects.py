"""S-CONDITIONAL-EFFECTS: internal effects that wait for external ones (docs/04 §7;
FR-STATE-12,14, FR-EXT-15,16,21).

Every S-CONDITIONAL-EFFECTS M6 part lives in this file: #216's intent side
(``test_s_conditional_effects``), #212's synchronous outcome stage (``__sync``, its own
docstring) and #217's late settlement (``__async``, its own docstring).

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
from collections.abc import Sequence
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
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from epistrel.orchestrator.jobs.handlers import reservations
from epistrel.orchestrator.structured import staged
from tests import canon_support as c
from tests import effect_support as es
from tests import late_settlement_support as ls
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE, override
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


# -- #212: the synchronous outcome stage ---------------------------------------------------------

REFUSES = "The smith refuses the payment."
DONE = "The smith nods: the purchase went through."
SADDLED = "Mira wraps the sword in oilcloth and sets out a saddle for Cal."
SADDLE_PRICE = 20
MODEL_CALLS = 40
"""Room for the outcome step's repair (#214 reserves the outcome render itself)."""


async def _sync_bank(
    client: Any, engine: AsyncEngine, epistrel: Any
) -> tuple[str, str, str, ps.ScriptedProvider]:
    """A story (Cal, Mira) whose gold a fresh synchronous write-capable provider owns (it reads
    1,000), with Cal's heirloom sword: its id, Cal's, Mira's and the fake."""
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name, confirmation="synchronous"))
    fake = ps.ScriptedProvider(name, default=1000).attach(epistrel)
    story, ids = await structured_story(client, "Cal", "Mira")
    await override(client, story, {"budgets": {"model_calls": MODEL_CALLS}})
    await ss.set_level(engine, story, "on")
    await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
    await _swords(engine, story, ids["Cal"])
    return story, ids["Cal"], ids["Mira"], fake


async def _saddles(engine: AsyncEngine, epistrel: Any, story: str) -> ps.ScriptedProvider:
    """``silver`` on a second synchronous provider and the story-owned ``saddle`` bought with
    it (a second external action with its own conditional grant)."""
    name = ps.unique_name("stable")
    await ps.register(engine, ps.write_capable_spec(name, confirmation="synchronous"))
    fake = ps.ScriptedProvider(name, default=100).attach(epistrel)
    await ps.bind(engine, story, name, {"player": "p-1"})
    async with engine.begin() as conn:
        saddle = await store.add_entity(conn, story, "item", "saddle")
    await ss.define_all(
        engine,
        story,
        ss.gold(
            name="silver",
            aliases=("silver coins",),
            unit="sp",
            owner="external",
            provider_id=name,
            external_value_name="gold",
            authorization=es.ANY_CHARACTER,
        ),
        StateDefinition.model_validate(
            {
                "name": "saddle",
                "type": "number",
                "scope": "character",
                "constraints": {"non_negative": True},
                "initial_value": 0,
                "entity_id": saddle,
                "transfer": {"counterparties": ["silver"]},
                "authorization": {"sources": ["any_character", "director"]},
            }
        ),
    )
    return fake


async def _swords_settled(engine: AsyncEngine, story: str) -> list[Any]:
    """The sword's turn rows and their settlement rows (``source = settlement``), in order."""
    return [r for r in await ss.change_rows(engine, story) if r.reason == "the sword"]


def _saddle(owner: str) -> dict[str, Any]:
    return {
        "kind": "transfer",
        "state": "saddle",
        "scope_key": owner,
        "delta": 1,
        "counterparty": {"state": "silver", "scope_key": owner, "delta": -SADDLE_PRICE},
        "reason": "the saddle",
    }


def _outcome_step(stub: Any, *beats: dict[str, Any]) -> None:
    """The outcome step's narrator render answers ``beats`` (only its prompt carries
    ``## Outcome step``; registered before ``es.narrator``, the first match wins)."""
    stub.when(staged.OUTCOME_STEP, es.render(*beats), purpose="generation")


async def _buy(
    client: Any,
    stub: Any,
    story: str,
    cal: str,
    outcome: str,
    *,
    regenerations: Sequence[str] = (),
    more: Sequence[dict[str, Any]] = (),
    text: str = f"{WRAPS} {HOLDS}",
) -> dict[str, Any]:
    """A turn in which Mira's beat buys the sword for Cal (its external payment staged), the
    narrator nudges the story-owned ``suspicion`` (unconditional), and the outcome step
    answers ``outcome``."""
    stub.reset()
    _outcome_step(stub, beat("narration", outcome))
    ms.renders_for(stub, "Mira", es.render(beat("do", text, effects=[_trade(cal, 1), *more])))
    nudge = es.effect("suspicion", delta=1, reason="a stranger watches")
    es.narrator(stub, es.render(beat("narration", "The forge glows.", effects=[nudge])))
    stub.script("regeneration", [json.dumps({"text": t, "effects": []}) for t in regenerations])
    answer = await respond(client, story, body(cal, None, do="buys the sword"))
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


@pytest.mark.part("M6", "FR-STATE-12,14; FR-EXT-15,16")
async def test_s_conditional_effects__sync(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#212's synchronous clauses (moved here from #216; the outcome stage is #212's): Cal's
    gold is owned by a ``synchronous`` provider, so each turn is staged.

    1. **Buying** the sword: at intent the grant is ``pending`` and Mira's possession sentence
       ("Cal now holds the sword.") is repaired to the attempt (#216's "Mira wraps the sword in
       oilcloth."); the payment confirms and the grant is committed **at the outcome stage**:
       a new ``committed`` settlement row at the outcome step's first position, under the
       turn's own operation (``settles_change_id`` the intent row, now ``converted``); the
       outcome step's "the purchase went through" stands. Complement: the narrator's
       unconditional ``suspicion`` change committed at intent.
    2. **The payment fails**: the grant is dropped, never granted, and the outcome step's "the
       purchase went through" is repaired to the refusal ("The smith refuses the payment.").
       Complement: the unconditional change still committed at intent.
    3. **Selling** the sword for a synchronous payout: held at intent, handed over (converted,
       a new ``-1`` row) at the outcome stage.
    4. **Partial success**: the sword paid in gold, the saddle in silver (a second synchronous
       provider), each grant conditional on its own payment; the gold confirms and the silver
       fails: only the sword is granted, the confirmed payment is not compensated (no new
       outbox row after the turn's two), and one outcome step narrates both."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- 1. Buying: granted only at the outcome stage, after the confirmation.
        story, cal, _, fake = await _sync_bank(client, engine, epistrel)
        fake.script_writes("gold", "confirmed")
        played = await _buy(client, stub, story, cal, DONE, regenerations=[WRAPS])
        texts = [b["text"] for b in played["beats"]]
        assert texts.index(WRAPS) < texts.index(DONE)
        assert HOLDS not in texts
        report = await turn_report(engine, story)
        assert ("external_outcome_unconfirmed", 3) in _tags(report)  # at intent
        assert report["staged"]["outcome_step"]["repairs"] == []
        intent, settled = await _swords_settled(engine, story)
        assert (intent.status, intent.contingency) == ("converted", "conditional")
        assert (settled.status, settled.source, settled.settles_change_id) == (
            "committed",
            "settlement",
            intent.id,
        )
        assert settled.operation_id == intent.operation_id  # the turn's own stage
        assert settled.beat == report["staged"]["outcome_step"]["beats"][0]
        assert settled.beat > intent.beat
        assert await ss.value(engine, story, "sword", cal) == 2
        (nudged,) = [r for r in await es.turn_changes(engine, story) if r.reason != "the sword"]
        assert (nudged.status, nudged.contingency) == ("committed", "unconditional")
        await ps.drop_outbox_jobs(engine, story)

        # -- 2. The payment fails: never granted; the outcome step narrates the refusal.
        story, cal, _, fake = await _sync_bank(client, engine, epistrel)
        fake.script_writes("gold", "failed")
        played = await _buy(client, stub, story, cal, DONE, regenerations=[WRAPS, REFUSES])
        texts = [b["text"] for b in played["beats"]]
        assert REFUSES in texts
        assert DONE not in texts
        assert "went through" not in played["message"]["text"]
        outcome_step = (await turn_report(engine, story))["staged"]["outcome_step"]
        assert [r["tag"] for r in outcome_step["repairs"]] == ["external_outcome_unconfirmed"]
        (intent,) = await _swords_settled(engine, story)
        assert intent.status == "dropped"
        assert await ss.value(engine, story, "sword", cal) == 1
        (nudged,) = [r for r in await es.turn_changes(engine, story) if r.reason != "the sword"]
        assert nudged.status == "committed"  # the unconditional change stands
        assert await ss.value(engine, story, "suspicion") == 1
        await ps.drop_outbox_jobs(engine, story)

        # -- 3. Selling for a synchronous payout: held at intent, handed over at the outcome.
        story, cal, _, fake = await _sync_bank(client, engine, epistrel)
        fake.script_writes("gold", "confirmed")
        stub.reset()
        _outcome_step(stub, beat("narration", "The buyer's courier rides off with the sword."))
        ms.renders_for(stub, "Mira", es.render(beat("say", "I know a buyer.", intent="sincere")))
        es.narrator(stub, es.render(beat("narration", POSTS, effects=[_trade(cal, -1)])))
        answer = await respond(client, story, body(cal, None, do="sells the sword"))
        assert answer.status_code == 200, answer.text
        intent, handed = await _swords_settled(engine, story)
        assert (intent.status, intent.contingency) == ("converted", "reservation")
        assert (handed.status, handed.source, handed.delta) == ("committed", "settlement", -1)
        assert await ss.value(engine, story, "sword", cal) == 0
        await ps.drop_outbox_jobs(engine, story)

        # -- 4. Partial success: the gold confirms, the silver fails.
        story, cal, _, fake = await _sync_bank(client, engine, epistrel)
        silver = await _saddles(engine, epistrel, story)
        fake.script_writes("gold", "confirmed")
        silver.script_writes("gold", "failed")
        played = await _buy(
            client,
            stub,
            story,
            cal,
            "The sword is Cal's; the stable keeps its saddle.",
            more=[_saddle(cal)],
            text=SADDLED,
        )
        prompts = [p for p in es.prompts(stub, "NarratorRender") if staged.OUTCOME_STEP in p]
        (prompt,) = prompts  # one outcome step narrates both
        assert "the sword: confirmed" in prompt
        assert "the saddle: refused" in prompt
        assert await ss.value(engine, story, "sword", cal) == 2
        assert await ss.value(engine, story, "saddle", cal) == 0
        saddle = [r for r in await ss.change_rows(engine, story) if r.reason == "the saddle"]
        assert [r.status for r in saddle] == ["dropped"]
        rows_after = await ps.outbox_rows(engine, story)
        assert sorted(r.status for r in rows_after) == ["confirmed", "failed"]  # no compensation
        await ps.drop_outbox_jobs(engine, story)
        await ss.set_level(engine, story, None)


@pytest.mark.part("M6", "FR-STATE-14; FR-EXT-21")
async def test_s_conditional_effects__async(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#217's part (FR-EXT-21): an ``asynchronous`` provider's outcome and a consumer-applied
    effect's outcome reported later through ``ingest`` settle the conditional effects and the
    reservations at the next inter-turn slot after the last completed turn, never at the
    requesting turn.

    1. **Asynchronous** (``bank``): Cal buys a sword (the grant ``pending``) and sells his
       heirloom (``held``); both payments are confirmed by the dispatcher after two more turns:
       the grant commits at ``h⁺1`` and the sale hands the sword over at ``h⁺2`` (new committed
       rows at the slots; the intent rows ``converted``); checkpoint ``h`` has neither.
    2. **Released on failure**: a second sale's payout fails: the hold is released at the next
       slot, the sword stays Cal's, nothing converts.
    3. **Consumer-applied** (a read-only provider, #209): a purchase's payment is a request of
       the consumer; its confirmation through ``ingest {kind: outcome, effect_id}`` settles the
       grant at the next slot."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        _, fake = await _bank(engine, epistrel, story)
        await _swords(engine, story, cal)

        # -- 1. A purchase and a sale, confirmed late.
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("do", WRAPS, effects=[_trade(cal, 1)])))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        bought = await respond(client, story, body(cal, None, do="buys a sword"))
        assert bought.status_code == 200, bought.text
        requested = bought.json()["turn"]
        (payment,) = [e for e in bought.json()["state_effects"] if e["state"] == "gold"]
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", "I know a buyer.", intent="sincere")))
        es.narrator(stub, es.render(beat("narration", POSTS, effects=[_trade(cal, -1)])))
        sold = await respond(client, story, body(cal, None, do="sells his heirloom"))
        assert sold.status_code == 200, sold.text
        (payout,) = [e for e in sold.json()["state_effects"] if e["state"] == "gold"]
        last = await ls.plain(client, stub, story, cal)
        head = last["turn"]
        fake.script_writes("gold", "confirmed")
        grant = await ls.answer(engine, epistrel, payment["outbox_id"])
        handed = await ls.answer(engine, epistrel, payout["outbox_id"])
        first = await ls.settle(engine, epistrel, grant)
        second = await ls.settle(engine, epistrel, handed)
        assert first.position is not None
        assert second.position is not None
        assert (first.position.turn, first.position.slot_k) == (head, 1)
        assert (second.position.turn, second.position.slot_k) == (head, 2)
        rows_now = await _sword_rows(engine, story)
        assert [r.status for r in rows_now] == ["converted", "converted"]
        settled = await ls.settlement_rows(engine, story)
        assert sorted((r.narrative_turn, r.slot_k, int(r.delta)) for r in settled) == [
            (head, 1, 1),
            (head, 2, -1),
        ]
        assert all(r.narrative_turn != requested for r in settled)
        assert await ls.sword_of(engine, story, cal, head) == 1  # the heirloom, held
        assert await ls.sword_of(engine, story, cal, head, 1) == 2  # the grant
        assert await ls.sword_of(engine, story, cal, head, 2) == 1  # the heirloom handed over

        # -- 2. A second sale whose payout fails: the hold is released, nothing converts.
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", "Another buyer?", intent="sincere")))
        es.narrator(stub, es.render(beat("narration", POSTS, effects=[_trade(cal, -1)])))
        again = await respond(client, story, body(cal, None, do="sells the new sword"))
        assert again.status_code == 200, again.text
        (refused,) = [e for e in again.json()["state_effects"] if e["state"] == "gold"]
        fake.script_writes("gold", "failed")
        failed = await ls.answer(engine, epistrel, refused["outbox_id"])
        assert (await ls.settle(engine, epistrel, failed)).result == settlement.SETTLED
        assert [r.status for r in await _sword_rows(engine, story)][-1] == "released"
        assert await ls.sword_of(engine, story, cal) == 1
        assert len(await ls.settlement_rows(engine, story)) == 2
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)

    # -- 3. A consumer-applied effect, confirmed through ingest.
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        ps.ScriptedProvider(name, default=1000).attach(epistrel)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
        await _swords(engine, story)
        played = await ls.buy(client, stub, story, cal)
        (asked,) = [e for e in played["state_effects"] if e["state"] == "gold"]
        head = (await ls.plain(client, stub, story, cal))["turn"]
        reported = await client.post(
            f"/stories/{story}/ingest",
            json={
                "events": [
                    {
                        "client_event_id": "paid-1",
                        "kind": "outcome",
                        "effect_id": asked["effect_id"],
                        "outcome": "confirmed",
                    }
                ]
            },
            headers=WRITE,
        )
        assert reported.status_code == 201, reported.text
        (record,) = await ps.outbox_outcomes(engine, story)
        done = await ls.settle(engine, epistrel, record.id)
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        assert await ls.sword_of(engine, story, cal, played["turn"]) == 0
        assert await ls.sword_of(engine, story, cal, head) == 0
        assert await ls.sword_of(engine, story, cal, head, 1) == 1
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)
