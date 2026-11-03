"""S-LATE-SETTLEMENT M6: confirmations that arrive late (docs/04 §7; FR-EXT-21, FR-STATE-9,14,
FR-STORE-11,15,31, FR-CONC-4, FR-BELIEF-5,6; #217).

Cal's gold is owned by an ``asynchronous`` write-capable in-process provider (#210's ordinary
path: a purchase turn commits with its ``intended`` outbox row and closes); the sword is
story-owned inventory, granted ``conditional`` on the payment (#216). The dispatcher answers
the row later (``outbox_dispatch``): its outcome record (an ``outbox_outcomes`` row and a
position-less ``outcome_recorded`` event) enqueues one ``outcome_settlement`` job, which the
test runs in its own loop (``settlement.settle``). The docs' turn numbers are compressed: the
purchase is turn ``b`` and the arrival follows turn ``h``.

``test_s_late_settlement`` (one story through ``respond``):

1. **After turn h's reply** → the settlement is an ``inter_turn`` operation at slot ``h⁺1``;
   checkpoint ``h`` has no sword and ``h⁺1`` has it; nothing is positioned at the purchase
   turn; turn h+1's response surfaces it (``settled_effects``) and its narrator inputs carry the
   confirmation (``pending_outcomes``), no character's do.
2. **While turn h+1 is generating** (held inside its first render) → the settlement commits at
   ``h⁺1`` before it, and the turn regenerates: its read set included Cal's inventory (FR-CONC-4);
   reconstruction at ``h`` and ``h⁺1`` differs by the settlement only.
3. **Right after turn h** → ``h⁺1``, surfaced in h+1; truncating to ``h`` (the default boundary)
   drops it, keeps the outcome record and lists the effect ``confirmed_unsettled``; the
   untruncation brings it back and the listing clears.
4. **Turn h is an incomplete staged turn** (``turn_stage = awaiting_outcome``, #212) → the
   settlement waits (nothing written) and lands at ``h⁺1`` once the turn completes.

``__clock`` (5): turn h ends at offset T; slot ``h⁺1`` is a consumer clock advance to T+60; the
confirmation settles at ``h⁺2`` with valid-time T+60 (#193's ``clock_before_slot``), and the
reconstruction at ``h⁺1`` shows T+60 with no sword, at ``h⁺2`` T+60 with the sword.

``__exposure`` (6), on a partitions story with lazy resolution (``partition_support``,
``exposure_support``): slot ``h⁺1`` is a consumer State set (FR-STATE-9); the courier's delivery
settles at ``h⁺2`` while Jenni (non-salient, no partition player) is in the room with Cal →
one exposure at ``h⁺2`` of ``state_value(sword, Cal) = 1``, sourced at the slot's
``external_outcome`` event, with no belief and no memory row; the player's character is never
exposed. Later Cal sells the sword (turn h+2); a lazy resolution of Jenni's exposure at turn
h+3 reads the history through ``h⁺2`` only (its keyed reads' bounds) and believes Cal has it.
Truncating to ``h`` drops the settlement, the exposure and the resolution. Complement: Mira,
in the kitchen, is not exposed.

``__reconciliation`` (written as direct turns, the variants' shapes):

- **Discarded origin**: the dispatcher claims the purchase, the player rewinds before it and
  plays a new turn, then the confirmation arrives → nothing granted, ``confirmed_discarded_origin``.
- **Intervening action**: the merchant's last sword is granted ``conditional`` on the payment;
  a later turn sells it to someone else ("no sword left"); the confirmation arrives later →
  re-verification fails (the merchant's stock would go negative), nothing is committed,
  ``settlement_conflict`` with the confirmed outcome; views through both turns have no sword.
- **Reservation variant**: the merchant's sword is ``held`` at intent (unavailable to anyone but
  Cal: #189's availability, ``Canon.reserved``), so no later turn can sell it; the settlement
  succeeds.
- **Rewind**: truncating to a turn after the purchase and before the settlement drops the grant,
  keeps the outcome record and lists ``confirmed_unsettled``. **Fork** there: no sword, a
  non-executable outbox copy, the effect listed in the fork's response
  (``effects_pending_in_source``, ``confirmed``) and nothing listed in the fork. **Swap back**
  to the continuation with the settlement: the sword is present again and the listing clears.
- Complement: a ``failed`` outcome drops the grant and lists no settlement reason.

``__ingest_outcome`` (FR-EXT-21): a consumer-applied effect (#209) confirmed through ``ingest
{kind: outcome, effect_id}`` is recorded once (a retry with the same ``client_event_id`` replays
it, a different payload is 409) and settles at the next slot; an unknown ``effect_id`` or
``outbox_id`` is 422 ``effect_not_found`` with nothing written; outcomes mixed with turns are 422.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import fork as forking
from epistrel.core import outbox as core_outbox
from epistrel.core import timeline, world_clock
from epistrel.core.enums import StateChangeKind, StateContingency
from epistrel.core.ids import new_ulid
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions.vocabulary import STATE_VALUE
from epistrel.core.states import contingency as ct
from epistrel.core.states.api import StateSetRequest, set_state
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from tests import clock_support as clocks
from tests import effect_support as es
from tests import exposure_support as xs
from tests import late_settlement_support as ls
from tests import multi_support as ms
from tests import perception_support as perception
from tests import provider_support as ps
from tests import state_support as ss
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.projection_support import CONTROL_PRINCIPAL
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, structured_story

pytestmark = pytest.mark.integration

CONFIRMED_LINE = "(the game confirmed it)"
AWAITING = "## Awaiting the game"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _attempts(engine: AsyncEngine, operation_id: str) -> list[str]:
    (row,) = await rows(
        engine, "SELECT attempts FROM operations WHERE operation_id = :o", o=operation_id
    )
    return [a["outcome"] for a in row.attempts]


async def _recorded_events(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT e.payload, e.narrative_turn, e.category, o.kind FROM events e "
        "JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND e.type = 'outcome_recorded' ORDER BY e.seq",
        s=story,
    )


async def _slot_outcomes(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT e.id, e.narrative_turn, e.slot_k, o.kind, o.slot_k AS op_slot FROM events e "
        "JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND e.type = 'external_outcome' ORDER BY e.seq",
        s=story,
    )


@pytest.mark.part("M6", "FR-EXT-21; FR-STATE-9,14; FR-STORE-11,15,31; FR-CONC-4; FR-BELIEF-5,6")
async def test_s_late_settlement(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Arrival timings 1 to 4 (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ls.bank(engine, epistrel, story)
        # Room in the model calls for case 2's regeneration.
        await set_overrides(
            engine,
            story,
            ProfileOverridesPatch(settings={"budgets": {"model_calls": 40}}, ignore_warnings=True),
            principal="p-author",
            facts=perception.NO_FACTS,
        )
        # Cal may hold 4 swords at most (case 2 needs a change that stops fitting).
        await ls.swords(engine, story, constraints={"non_negative": True, "max": 4})

        # -- 1. After turn h's reply: slot h⁺1, valid before turn h+1, surfaced in its reply.
        bought = await ls.buy(client, stub, story, cal)
        payment = ls.payment_of(bought)
        for _ in range(2):
            last = await ls.plain(client, stub, story, cal)
        head = last["turn"]
        outcome = await ls.answer(engine, epistrel, payment)
        (recorded,) = await _recorded_events(engine, story)
        assert recorded.narrative_turn is None  # timeline-independent: no narrative position
        assert (recorded.category, recorded.kind) == ("input", "inter_turn")
        assert recorded.payload == {
            "outcome_id": outcome,
            "outbox_id": payment,
            "status": "confirmed",
            "received_via": "dispatcher",
        }
        (job,) = await ls.settle_jobs(engine, story)
        assert job.payload == {"outcome_id": outcome}
        revision = await scalar(
            engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story
        )
        done = await ls.settle(engine, epistrel, outcome)
        assert done.result == settlement.SETTLED, done
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        assert (
            await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)
            == revision + 1
        )
        assert await ls.sword_of(engine, story, cal, head) == 0
        assert await ls.sword_of(engine, story, cal, head, 1) == 1
        (granted,) = await ls.settlement_rows(engine, story)
        assert (granted.narrative_turn, granted.slot_k) == (head, 1)  # never at the purchase
        (at_slot,) = await _slot_outcomes(engine, story)
        assert (at_slot.narrative_turn, at_slot.slot_k, at_slot.kind) == (head, 1, "inter_turn")
        (row,) = [r for r in await ps.outbox_rows(engine, story) if r.id == payment]
        assert row.outcome_event_id == at_slot.id
        assert [r.settled for r in await ps.outbox_outcomes(engine, story)] == [True]
        # A second run settles nothing again.
        assert (await ls.settle(engine, epistrel, outcome)).result == settlement.SETTLED
        assert len(await ls.settlement_rows(engine, story)) == 1

        surfaced = await ls.plain(client, stub, story, cal)
        (settled,) = surfaced["settled_effects"]
        assert {k: settled[k] for k in ("state", "scope_key", "new_value", "source")} == {
            "state": "sword",
            "scope_key": cal,
            "new_value": 1,
            "source": "settlement",
        }
        (narrator_prompt,) = es.prompts(stub, "NarratorRender")
        section = narrator_prompt.split(AWAITING, 1)[1].split("\n## ", 1)[0]
        assert CONFIRMED_LINE in section
        for prompt in ms.render_prompts(stub, "Mira"):
            assert AWAITING not in prompt
        quiet = await ls.plain(client, stub, story, cal)
        assert "settled_effects" not in quiet  # surfaced once
        assert AWAITING not in es.prompts(stub, "NarratorRender")[0]

        # -- 3. Right after turn h: h⁺1, surfaced in h+1; a truncation to h drops it.
        bought = await ls.buy(client, stub, story, cal)
        payment = ls.payment_of(bought)
        head = bought["turn"]
        outcome = await ls.answer(engine, epistrel, payment)
        done = await ls.settle(engine, epistrel, outcome)
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        surfaced = await ls.plain(client, stub, story, cal)
        assert [e["state"] for e in surfaced["settled_effects"]] == ["sword"]
        assert await ls.sword_of(engine, story, cal, head) == 1
        assert await ls.sword_of(engine, story, cal, head, 1) == 2
        await timeline.truncate(
            engine,
            story,
            timeline.TruncateRequest(to_turn=head),
            principal=CONTROL_PRINCIPAL,
        )
        assert await ls.sword_of(engine, story, cal) == 1
        assert (payment, "confirmed_unsettled") in await ls.listed(engine, story)
        assert outcome in {r.id for r in await ps.outbox_outcomes(engine, story)}
        # Undone: the slot is back with the turns, and the listing clears.
        await timeline.untruncate(engine, story, principal=CONTROL_PRINCIPAL)
        assert await ls.sword_of(engine, story, cal) == 2
        assert (payment, "confirmed_unsettled") not in await ls.listed(engine, story)

        # -- 4. Turn h is an incomplete staged turn: the settlement waits, then lands at h⁺1.
        bought = await ls.buy(client, stub, story, cal)
        payment = ls.payment_of(bought)
        last = await ls.plain(client, stub, story, cal)
        head = last["turn"]
        (staged,) = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND narrative_turn = :t AND membership = 'active'",
            s=story,
            t=head,
        )
        await ls.set_stage(engine, staged.operation_id, "awaiting_outcome")
        outcome = await ls.answer(engine, epistrel, payment)
        before = len(await ls.settlement_rows(engine, story))
        assert (await ls.settle(engine, epistrel, outcome)).result == settlement.WAITING
        assert len(await ls.settlement_rows(engine, story)) == before
        await ls.set_stage(engine, staged.operation_id, "completed")
        done = await ls.settle(engine, epistrel, outcome)
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        assert await ls.sword_of(engine, story, cal, head) == 2
        assert await ls.sword_of(engine, story, cal, head, 1) == 3

        # -- 2. While turn h+1 is generating: settled at h⁺1 before it. The turn's own change
        # (Cal finds a fourth sword) read Cal's inventory and no longer fits the bound (max 4)
        # once the settlement commits: the turn regenerates (FR-CONC-4).
        bought = await ls.buy(client, stub, story, cal)
        payment = ls.payment_of(bought)
        head = bought["turn"]
        outcome = await ls.answer(engine, epistrel, payment)
        stub.reset()
        found = es.effect("sword", scope_key=cal, delta=1, reason="found in the hay")
        es.narrator(
            stub, es.render(beat("narration", "A blade glints in the hay.", effects=[found]))
        )
        ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
        latch = stub.hold("generation")
        task = asyncio.ensure_future(respond(client, story, body(cal, None, do="sits")))
        entered = asyncio.ensure_future(latch.entered.wait())
        await asyncio.wait({entered, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert entered.done(), (await task).text
        done = await ls.settle(engine, epistrel, outcome)
        latch.release()
        answered = await task
        assert answered.status_code == 200, answered.text
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        played = answered.json()
        assert played["turn"] == head + 1
        (turn_op,) = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
            "AND narrative_turn = :t AND membership = 'active'",
            s=story,
            t=head + 1,
        )
        assert await _attempts(engine, turn_op.operation_id) == ["regenerate", "commit"]
        assert [e["state"] for e in played["settled_effects"]] == ["sword"]
        assert await ls.sword_of(engine, story, cal, head) == 3
        assert await ls.sword_of(engine, story, cal, head, 1) == 4
        assert await ls.sword_of(engine, story, cal) == 4  # the found sword no longer fits

        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)


@pytest.mark.part("M6", "FR-EXT-21; FR-STATE-9,14; FR-STORE-11,15,31; FR-CONC-4; FR-BELIEF-5,6")
async def test_s_late_settlement__clock(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Case 5: an earlier slot advanced the clock (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await clocks.set_level(engine, story, "on")
        await ls.bank(engine, epistrel, story)
        await ls.swords(engine, story)
        payment = ls.payment_of(await ls.buy(client, stub, story, cal))
        head = (await ls.plain(client, stub, story, cal))["turn"]
        ended = await clocks.offset(engine, story, ss.at(head))
        moved = await clocks.advance(engine, story, 60, reason="the consumer's hour")
        assert (moved.position["turn"], moved.position["slot_k"]) == (head, 1)
        outcome = await ls.answer(engine, epistrel, payment)
        done = await ls.settle(engine, epistrel, outcome)
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 2)
        assert done.valid_time == ended + 60  # the clock before the slot: never backdated
        assert await clocks.offset(engine, story, ss.at(head, 1)) == ended + 60
        assert await ls.sword_of(engine, story, cal, head, 1) == 0
        assert await clocks.offset(engine, story, ss.at(head, 2)) == ended + 60
        assert await ls.sword_of(engine, story, cal, head, 2) == 1
        (granted,) = await ls.settlement_rows(engine, story)
        async with engine.connect() as conn:
            minute = await world_clock.story_time_at(conn, story, ss.at(head, 1))
        assert granted.valid_time_minutes == minute
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)


@pytest.mark.part("M6", "FR-EXT-21; FR-STATE-9,14; FR-STORE-11,15,31; FR-CONC-4; FR-BELIEF-5,6")
async def test_s_late_settlement__exposure(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Case 6: an exposure at a slot, resolved lazily through it only (see the module
    docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Cal", "Mira", "Jenni")
        cal, mira, jenni = ids["Cal"], ids["Mira"], ids["Jenni"]
        await ts.enable_testimony(engine, story, "defaulted")
        parlor = await perception.place_entity(engine, story, "parlor")
        kitchen = await perception.place_entity(engine, story, "kitchen")
        for who, where in ((cal, parlor), (jenni, parlor), (mira, kitchen)):
            await perception.put(engine, story, who, where)
        fake = await ls.bank(engine, epistrel, story)
        await ls.swords(engine, story, mira)
        conditional = StateContingency.CONDITIONAL
        payment, _ = await ls.intent(engine, story, fake.name, cal, [ls.Side(cal, 1, conditional)])
        head = (await ls.turn(engine, story)).narrative_turn
        assert head is not None
        # Slot h⁺1: a consumer State set (FR-STATE-9).
        await set_state(
            engine,
            story,
            "sword",
            StateSetRequest(value=1, scope_key=mira, reason="restocked"),
            principal="p-consumer",
        )
        outcome = await ls.answer(engine, epistrel, payment)
        done = await ls.settle(engine, epistrel, outcome)
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 2)
        assert done.exposures == 1
        (exposed,) = await xs.exposure_rows(engine, story)
        assert exposed.witness_character_id == jenni  # not Cal (the player), not Mira (away)
        assert (exposed.position_turn, exposed.position_slot_k) == (head, 2)
        assert (exposed.position_beat, exposed.position_step) == (0, 1)  # after the slot event
        assert exposed.state == "unresolved"
        (at_slot,) = await _slot_outcomes(engine, story)
        assert exposed.source_event_id == at_slot.id
        assert exposed.source_character_id == cal  # the holder: what Jenni saw happen to him
        (heard,) = await rows(
            engine, "SELECT * FROM propositions WHERE id = :p", p=exposed.proposition_id
        )
        assert heard.subject_id == cal
        assert heard.predicate_id == STATE_VALUE
        assert heard.literal == {"kind": "int", "value": 1}
        settled_by = done.operation_id
        for count in (
            "SELECT count(*) FROM memories WHERE operation_id = :o",
            "SELECT count(*) FROM beliefs WHERE operation_id = :o",
        ):
            assert await scalar(engine, count, o=settled_by) == 0

        # Cal sells the sword at turn h+1; Jenni's resolution at h+2 reads through h⁺2 only.
        await ss.change(engine, story, ss.delta("sword", -1, scope_key=cal, reason="sold it"))
        resolution, resolved_by = await xs.resolve(engine, story, exposed.id)
        assert resolution.reads, "the resolver read through its lookups"
        frame = (head, 2, 0, 1)  # the exposure position: h⁺2, its step 1
        for read in resolution.reads:
            assert read.through is not None, read
            assert tuple(read.through) <= frame, read
        assert resolution.belief is not None
        assert await xs.state_of(engine, story, exposed.id) == "resolved"

        # Truncating to h drops the settlement, the exposure and the resolution.
        await timeline.truncate(
            engine, story, timeline.TruncateRequest(to_turn=head), principal=CONTROL_PRINCIPAL
        )
        assert await ls.sword_of(engine, story, cal) == 0
        (dropped,) = await rows(
            engine,
            "SELECT membership::text AS member FROM exposures WHERE id = :i",
            i=exposed.id,
        )
        assert dropped.member == "discarded"
        (resolver,) = await rows(
            engine,
            "SELECT membership::text AS member FROM operations WHERE operation_id = :o",
            o=resolved_by,
        )
        assert resolver.member == "discarded"
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)
        await ts.forget(engine)


@pytest.mark.part("M6", "FR-EXT-21; FR-STATE-9,14; FR-STORE-11,15,31; FR-CONC-4; FR-BELIEF-5,6")
async def test_s_late_settlement__reconciliation(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Discarded origin, intervening action, reservation, rewind, fork, swap back, and the
    failed complement (see the module docstring)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Cal", "Mira", "Jenni")
        cal, mira = ids["Cal"], ids["Mira"]
        fake = await ls.bank(engine, epistrel, story)
        sword = await ls.swords(engine, story, mira)
        bank = fake.name
        conditional, reservation = StateContingency.CONDITIONAL, StateContingency.RESERVATION
        out, into = StateChangeKind.TRANSFER_OUT, StateChangeKind.TRANSFER_IN

        def sale(merchant: StateContingency) -> list[ls.Side]:
            return [ls.Side(mira, -1, merchant, out), ls.Side(cal, 1, conditional, into)]

        # Complement: a failed payment drops the grant and lists no settlement reason.
        fake.script_writes("gold", "failed")
        failed, _ = await ls.intent(engine, story, bank, cal, [ls.Side(cal, 1, conditional)])
        await ls.turn(engine, story)
        outcome = await ls.answer(engine, epistrel, failed)
        assert (await ls.settle(engine, epistrel, outcome)).result == settlement.SETTLED
        assert {r for o, r in await ls.listed(engine, story) if o == failed} == {"failed"}
        assert await ls.sword_of(engine, story, cal) == 0
        fake.script_writes("gold", "confirmed")

        # Discarded origin: claimed, rewound before it, a new turn, then the confirmation.
        discarded, origin = await ls.intent(engine, story, bank, cal, sale(conditional))
        assert origin.narrative_turn is not None
        claimed = await outbox_job.claim(engine, discarded, registry=epistrel.in_process_providers)
        assert isinstance(claimed, outbox_job.Claimed)
        await timeline.truncate(
            engine,
            story,
            timeline.TruncateRequest(to_turn=origin.narrative_turn - 1),
            principal=CONTROL_PRINCIPAL,
        )
        await ls.turn(engine, story, "do something else")
        assert await outbox_job.send(engine, claimed) == "confirmed"
        outcome = await ls.last_outcome(engine, discarded)
        found = await ls.settle(engine, epistrel, outcome)
        assert found.result == settlement.DISCARDED
        assert (discarded, "confirmed_discarded_origin") in await ls.listed(engine, story)
        assert await ls.sword_of(engine, story, cal) == 0
        assert await ls.sword_of(engine, story, mira) == 1

        # Intervening action: the merchant's last sword sold elsewhere before the confirmation.
        conflicted, sold_at = await ls.intent(engine, story, bank, cal, sale(conditional))
        await ls.sell(engine, story, mira)  # "no sword left"
        await ls.turn(engine, story)
        later = (await ls.turn(engine, story)).narrative_turn
        assert later is not None
        outcome = await ls.answer(engine, epistrel, conflicted)
        before = len(await ls.settlement_rows(engine, story))
        assert (await ls.settle(engine, epistrel, outcome)).result == settlement.CONFLICTED
        assert len(await ls.settlement_rows(engine, story)) == before
        (listing,) = [
            e
            for e in (await core_outbox.listings(engine, story)).listings
            if e.outbox_id == conflicted
        ]
        assert listing.reason == "settlement_conflict"
        assert listing.outcome is not None
        assert listing.outcome.kind == "confirmed"
        assert sold_at.narrative_turn is not None
        for through in range(sold_at.narrative_turn, later + 1):
            assert await ls.sword_of(engine, story, cal, through) == 0
        assert await ls.sword_of(engine, story, cal, later, 1) == 0
        assert not [
            r
            for r in await ps.outbox_outcomes(engine, story)
            if r.settled and r.outbox_id == conflicted
        ]

        # Reservation variant: the merchant's restocked sword is held for Cal at intent.
        await ss.change(engine, story, ss.set_to("sword", 1, scope_key=mira, reason="restocked"))
        reserved, reserved_at = await ls.intent(engine, story, bank, cal, sale(reservation))
        async with engine.connect() as conn:
            assert await ct.held_items(conn, story) == {sword: cal}  # no one else may take it
        middle = (await ls.turn(engine, story)).narrative_turn
        head = (await ls.turn(engine, story)).narrative_turn
        assert middle is not None
        assert head is not None
        outcome = await ls.answer(engine, epistrel, reserved)
        done = await ls.settle(engine, epistrel, outcome)
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        assert await ls.sword_of(engine, story, cal) == 1
        assert await ls.sword_of(engine, story, mira) == 0
        await ls.turn(engine, story)

        # Fork at the middle turn: no sword, a record only, listed in the fork's response.
        forked = await forking.fork(
            engine, story, forking.ForkRequest(at_turn=middle), principal=CONTROL_PRINCIPAL
        )
        assert [(e.outbox_id, e.status) for e in forked.effects_pending_in_source] == [
            (reserved, "confirmed")
        ]
        assert await ls.sword_of(engine, forked.story_id, cal) == 0
        copies = await ps.outbox_rows(engine, forked.story_id)
        assert copies
        assert not any(r.executable for r in copies)
        assert await ls.listed(engine, forked.story_id) == set()

        # Rewind to the middle turn: the grant is dropped, the outcome record remains, listed.
        await timeline.truncate(
            engine, story, timeline.TruncateRequest(to_turn=middle), principal=CONTROL_PRINCIPAL
        )
        assert await ls.sword_of(engine, story, cal) == 0
        assert (reserved, "confirmed_unsettled") in await ls.listed(engine, story)
        assert outcome in {r.id for r in await ps.outbox_outcomes(engine, story)}

        # Swap back to the continuation with the settlement: present again, the listing clears.
        await ls.turn(engine, story, "a new path")
        swapped = await timeline.untruncate(engine, story, principal=CONTROL_PRINCIPAL)
        assert swapped is not None
        assert await ls.sword_of(engine, story, cal) == 1
        assert (reserved, "confirmed_unsettled") not in await ls.listed(engine, story)
        assert reserved_at.narrative_turn is not None
        await ls.drop_jobs(engine, story)
        await ls.drop_jobs(engine, forked.story_id)
        await ss.set_level(engine, story, None)


def _outcome_event(client_event_id: str, **fields: Any) -> dict[str, Any]:
    return {"client_event_id": client_event_id, "kind": "outcome", **fields}


async def _ingest(client: Any, story: str, *events: dict[str, Any]) -> Any:
    return await client.post(
        f"/stories/{story}/ingest", json={"events": list(events)}, headers=WRITE
    )


async def _footprint(engine: AsyncEngine, story: str) -> tuple[int, ...]:
    found = []
    for sql in (
        "SELECT count(*) FROM outbox_outcomes WHERE story_id = :s",
        "SELECT count(*) FROM events WHERE story_id = :s",
        "SELECT count(*) FROM client_events WHERE story_id = :s",
        "SELECT timeline_revision FROM stories WHERE id = :s",
    ):
        found.append(int(await scalar(engine, sql, s=story)))
    return tuple(found)


@pytest.mark.part("M6", "FR-EXT-21")
async def test_s_late_settlement__ingest_outcome(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """``ingest {kind: outcome}``: dedupe, the effect it names, and its settlement (see the
    module docstring)."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        ps.ScriptedProvider(name, default=1000).attach(epistrel)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
        await ls.swords(engine, story)

        # A consumer-applied purchase (#209): the payment requested, the sword pending on it.
        played = await ls.buy(client, stub, story, cal)
        (asked,) = [e for e in played["state_effects"] if e["state"] == "gold"]
        effect_id = asked["effect_id"]
        assert asked["status"] == "pending"
        assert "outbox_id" not in asked
        (grant,) = [r for r in await ss.change_rows(engine, story) if r.reason == "the sword"]
        assert (grant.status, list(grant.prerequisite_effect_ids)) == ("pending", [effect_id])
        head = played["turn"]

        # Unknown effects and mixed batches: 422, nothing written.
        before = await _footprint(engine, story)
        for named in ({"effect_id": new_ulid()}, {"outbox_id": new_ulid()}):
            refused = await _ingest(
                client, story, _outcome_event("o-x", outcome="confirmed", **named)
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == "effect_not_found"
            assert refused.json()["error"]["details"]["locator"] == "events[0]"
        mixed = await _ingest(
            client,
            story,
            _outcome_event("o-y", outcome="confirmed", effect_id=effect_id),
            {"client_event_id": "n-1", "kind": "narration", "text": "Rain."},
        )
        assert mixed.status_code == 422, mixed.text
        assert mixed.json()["error"]["code"] == "validation_error"
        assert await _footprint(engine, story) == before

        # The consumer's confirmation: recorded position-less, no revision, a settlement queued.
        event = _outcome_event(
            "o-1", outcome="confirmed", effect_id=effect_id, resulting_values={"gold": 500}
        )
        first = await _ingest(client, story, event)
        assert first.status_code == 201, first.text
        (result,) = first.json()["results"]
        assert result["turn"] == 0
        assert first.json()["timeline_revision"] == before[3]
        (record,) = await ps.outbox_outcomes(engine, story)
        assert (record.effect_id, record.outbox_id) == (effect_id, None)
        assert (record.kind, record.received_via) == ("confirmed", "ingest")
        assert record.payload == {"resulting_values": {"gold": 500}}
        (recorded,) = await _recorded_events(engine, story)
        assert recorded.narrative_turn is None
        assert recorded.payload == {
            "outcome_id": record.id,
            "effect_id": effect_id,
            "status": "confirmed",
            "received_via": "ingest",
        }
        (job,) = await ls.settle_jobs(engine, story)
        assert job.payload == {"outcome_id": record.id}

        # A retry replays; a different payload under the same id is 409; nothing new written.
        again = await _ingest(client, story, event)
        assert again.status_code == 200, again.text
        assert again.json()["results"][0]["replayed"] is True
        changed = await _ingest(
            client, story, {**event, "outcome": "failed", "resulting_values": None}
        )
        assert changed.status_code == 409, changed.text
        assert changed.json()["error"]["code"] == "client_event_conflict"
        assert len(await ps.outbox_outcomes(engine, story)) == 1

        # Settled at the next slot, never at the requesting turn.
        done = await ls.settle(engine, epistrel, record.id)
        assert done.result == settlement.SETTLED
        assert done.position is not None
        assert (done.position.turn, done.position.slot_k) == (head, 1)
        assert await ls.sword_of(engine, story, cal, head) == 0
        assert await ls.sword_of(engine, story, cal, head, 1) == 1
        (granted,) = await ls.settlement_rows(engine, story)
        assert (granted.narrative_turn, granted.slot_k) == (head, 1)
        (settled,) = await ps.outbox_outcomes(engine, story)
        assert settled.settled is True

        # A second report of the same outcome is a duplicate: recorded, nothing settled again.
        duplicate = await _ingest(
            client, story, _outcome_event("o-2", outcome="confirmed", effect_id=effect_id)
        )
        assert duplicate.status_code == 201, duplicate.text
        kinds = [r.kind for r in await ps.outbox_outcomes(engine, story)]
        assert kinds == ["confirmed", "duplicate"]
        assert len(await ls.settle_jobs(engine, story)) == 1
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)


@pytest.mark.part("M6", "FR-EXT-21")
async def test_s_late_settlement__consumer_listing(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#661 (owner decision): a consumer-applied purchase (#209) whose confirmation cannot
    settle (Cal already holds the one sword the State allows) is listed
    ``settlement_conflict`` by its ``request_id`` (no outbox row). Its resolve-only action
    (``applied_elsewhere``) closes the listing at an inter-turn slot with a
    ``reconciliation_resolved`` audit event, writes no outcome record and calls no provider.
    Complement asserted: settle-now on it is 404 ``no_listing`` (no outbox row), as are a
    second resolve, an unknown id and a body outside the two resolutions (422)."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = ps.ScriptedProvider(name, default=1000).attach(epistrel)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
        await ls.swords(engine, story, constraints={"non_negative": True, "max": 1})
        played = await ls.buy(client, stub, story, cal)
        (asked,) = [e for e in played["state_effects"] if e["state"] == "gold"]
        request_id = asked["effect_id"]
        # Meanwhile Cal comes by a sword: the grant would break the State's bound.
        await ss.change(engine, story, ss.set_to("sword", 1, scope_key=cal, reason="a gift"))
        recorded = await _ingest(
            client, story, _outcome_event("o-1", outcome="confirmed", effect_id=request_id)
        )
        assert recorded.status_code == 201, recorded.text
        (record,) = await ps.outbox_outcomes(engine, story)
        assert (await ls.settle(engine, epistrel, record.id)).result == settlement.CONFLICTED
        (listing,) = [
            e
            for e in (await core_outbox.listings(engine, story)).listings
            if e.request_id == request_id
        ]
        assert listing.outbox_id is None
        assert (listing.reason, listing.status, listing.provider) == (
            "settlement_conflict",
            "confirmed",
            name,
        )
        assert listing.effect is not None
        assert listing.effect["state"] == "gold"
        seen = await client.get(f"/stories/{story}/reconciliation", headers=WRITE)
        assert seen.status_code == 200, seen.text
        assert request_id in {e.get("request_id") for e in seen.json()["listings"]}

        base = f"/stories/{story}/reconciliation"
        settle_now = await client.post(f"{base}/{request_id}/settle-now", headers=WRITE)
        assert settle_now.status_code == 404, settle_now.text
        assert settle_now.json()["error"]["details"]["reason"] == "no_listing"
        bad = await client.post(
            f"{base}/{request_id}/resolve", json={"resolution": "settled"}, headers=WRITE
        )
        assert bad.status_code == 422, bad.text

        calls, outcomes = fake.calls, len(await ps.outbox_outcomes(engine, story))
        resolved = await client.post(
            f"{base}/{request_id}/resolve", json={"resolution": "applied_elsewhere"}, headers=WRITE
        )
        assert resolved.status_code == 200, resolved.text
        done = resolved.json()
        assert (done["request_id"], done["resolution"], done["reasons"]) == (
            request_id,
            "applied_elsewhere",
            ["settlement_conflict"],
        )
        assert done["slot"]["slot_k"] >= 1
        assert (fake.calls, len(await ps.outbox_outcomes(engine, story))) == (calls, outcomes)
        audit = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'reconciliation_resolved'",
            s=story,
        )
        assert [r.payload["request_id"] for r in audit] == [request_id]
        listed = (await core_outbox.listings(engine, story)).listings
        assert request_id not in {e.request_id for e in listed}
        for again in (request_id, new_ulid()):
            refused = await client.post(
                f"{base}/{again}/resolve", json={"resolution": "abandoned"}, headers=WRITE
            )
            assert refused.status_code == 404, refused.text
            assert refused.json()["error"]["details"]["reason"] == "no_listing"
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)


SOLD_OUT = "Mira shrugs: the swords are sold out."


@pytest.mark.part("M7", "FR-BELIEF-6; FR-STORE-20; FR-DIRECT-3")
async def test_s_late_settlement__m7(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#697 (S-LATE-SETTLEMENT M7, #242's backdating clause): Cal's purchase settles (the gold is
    the game's, the sword the story's); a later line, "the swords are sold out", presupposes the
    story-owned sword State (#191's owned reading: a ``presupposes`` edge on the State). An
    author amendment backdating Cal's possession to the story's start goes through the normal
    blast radius: the line is a conflict (``state_presupposed``) settled under the session's
    mode, and the externally owned payment (its outbox row and outcome record) is
    byte-identical before and after."""
    from epistrel.core import edges
    from epistrel.core.enums import (
        DependencyEdgeType,
        DependencyTarget,
        OperationKind,
        Provenance,
    )
    from epistrel.core.events import Operation
    from epistrel.core.membership import Position
    from tests import rewrite_support as rs

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal, mira = ids["Cal"], ids["Mira"]
        fake = await ls.bank(engine, epistrel, story)
        await ls.swords(engine, story, mira, transfer={})
        conditional = StateContingency.CONDITIONAL
        payment, _ = await ls.intent(engine, story, fake.name, cal, [ls.Side(cal, 1, conditional)])
        await ls.turn(engine, story)
        done = await ls.settle(engine, epistrel, await ls.answer(engine, epistrel, payment))
        assert done.result == settlement.SETTLED
        (sword_state,) = await rows(
            engine,
            "SELECT id FROM state_definitions WHERE story_id = :s AND name = 'sword'",
            s=story,
        )
        line: dict[str, str] = {}
        async with Operation(
            engine, story_id=story, kind=OperationKind.TURN, principal="p-test"
        ) as op:
            op.append("player_input", {"text": "Another sword?"}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": SOLD_OUT})

            async def step(conn: Any) -> None:
                from sqlalchemy import text

                found = await conn.scalar(
                    text(
                        "SELECT id FROM events WHERE operation_id = :o "
                        "AND type = 'message_rendered'"
                    ),
                    {"o": op.operation_id},
                )
                line["id"] = str(found)
                await edges.record_edges(
                    conn,
                    op,
                    [
                        edges.EdgeSpec(
                            DependencyEdgeType.PRESUPPOSES,
                            "events",
                            str(found),
                            DependencyTarget.STATE,
                            str(sword_state.id),
                            target_subject_id=mira,
                            position=Position(op.narrative_turn or 0, 0, 0, 1, 0),
                        )
                    ],
                )

            op.add_commit_step(step)
            await op.commit()

        async def payment_rows() -> list[Any]:
            outbox = [dict(r._mapping) for r in await ps.outbox_rows(engine, story)]
            outcomes = [dict(r._mapping) for r in await ps.outbox_outcomes(engine, story)]
            return [*outbox, *outcomes]

        before = await payment_rows()
        stub.reset()
        stub.when(
            "",
            '{"changes": [{"kind": "state", "state": "sword", "scope": "Cal", "value": 1, '
            '"valid": {"from": "0"}}]}',
            purpose="rewrite_parse",
        )
        stub.when(
            "", '{"text": "Mira shrugs: she has no more swords to sell."}', purpose="rewrite_record"
        )
        opened = await rs.open_session(client, story, resolution_mode="best_effort")
        assert opened.status_code == 201, opened.text
        session = opened.json()
        conflicts = {
            c["record"]["id"]: c["reason"]
            for c in session["preview"]["blast_radius"]["irreducible"]
        }
        assert conflicts.get(line["id"]) == "state_presupposed", session["preview"]
        applied = await rs.apply(client, story, session["id"], await rs.revision(engine, story))
        assert applied.status_code == 200, applied.text
        resolved = {
            r["records"][0]["id"]: r["action"] for r in applied.json()["report"]["resolutions"]
        }
        assert resolved[line["id"]] in ("rewrite", "drop")
        assert await payment_rows() == before
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)


@pytest.mark.part("M7", "FR-BELIEF-6; FR-STORE-20; FR-DIRECT-3")
@pytest.mark.parametrize("corrects", ["earlier", "later"])
async def test_s_late_settlement__m7_lazy(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    corrects: str,
) -> None:
    """#697 (#242's case 6): case 6's story (an exposure at ``h⁺2`` that Jenni resolves lazily,
    after Cal sold the sword) with an author amendment of Cal's sword applied before the
    resolution. One correcting history before ``h⁺2`` (valid from the story's start) is in the
    corrected history the resolution reads through ``h⁺2`` (#221's per-part eligibility at the
    frame); one correcting only later history (valid from after ``h⁺2``'s story-time) is not.
    The resolution reads through the frame only, either way."""
    from epistrel.core import world_clock
    from epistrel.core.membership import Position
    from tests import rewrite_support as rs

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira", "Jenni")
        cal, mira, jenni = ids["Cal"], ids["Mira"], ids["Jenni"]
        await ts.enable_testimony(engine, story, "defaulted")
        parlor = await perception.place_entity(engine, story, "parlor")
        kitchen = await perception.place_entity(engine, story, "kitchen")
        for who, where in ((cal, parlor), (jenni, parlor), (mira, kitchen)):
            await perception.put(engine, story, who, where)
        fake = await ls.bank(engine, epistrel, story)
        await ls.swords(engine, story, mira, transfer={})
        conditional = StateContingency.CONDITIONAL
        payment, _ = await ls.intent(engine, story, fake.name, cal, [ls.Side(cal, 1, conditional)])
        head = (await ls.turn(engine, story)).narrative_turn
        assert head is not None
        await set_state(
            engine,
            story,
            "sword",
            StateSetRequest(value=1, scope_key=mira, reason="restocked"),
            principal="p-consumer",
        )
        done = await ls.settle(engine, epistrel, await ls.answer(engine, epistrel, payment))
        assert done.result == settlement.SETTLED
        (exposed,) = await xs.exposure_rows(engine, story)
        assert exposed.witness_character_id == jenni
        at_frame = await ls.sword_of(engine, story, cal, head, 2)
        async with engine.connect() as conn:
            frame_minute = await world_clock.story_time_at(conn, story, Position(head, 0, 2, 0, 1))
        await ss.change(engine, story, ss.delta("sword", -1, scope_key=cal, reason="sold it"))
        await ls.turn(engine, story)

        valid_from = 0 if corrects == "earlier" else frame_minute + 600
        stub.reset()
        stub.when(
            "",
            '{"changes": [{"kind": "state", "state": "sword", "scope": "Cal", "value": 7, '
            f'"valid": {{"from": "{valid_from}"}}}}]}}',
            purpose="rewrite_parse",
        )
        amended = await rs.open_session(
            client, story, resolution_mode="best_effort", preview="apply_directly"
        )
        assert amended.status_code == 201, amended.text
        assert amended.json()["state"] == "applied", amended.json()

        resolution, _ = await xs.resolve(engine, story, exposed.id)
        frame = (head, 2, 0, 1)
        assert resolution.reads
        assert all(r.through is not None and tuple(r.through) <= frame for r in resolution.reads)
        through_frame = await ls.sword_of(engine, story, cal, head, 2)
        if corrects == "earlier":
            assert through_frame != at_frame  # the correction is in the history read there
        else:
            assert through_frame == at_frame  # a later correction is not
        await ls.drop_jobs(engine, story)
        await ss.set_level(engine, story, None)
        await ts.forget(engine)
