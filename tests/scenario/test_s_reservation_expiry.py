"""S-RESERVATION-EXPIRY M6: expired holds, duplicates and conflicting outcomes, and the explicit
reconciliation actions (docs/04 §7; FR-EXT-22, FR-STATE-14; #218).

Cal's gold is owned by an ``asynchronous`` write-capable in-process provider (``bank``); the
sword is story-owned inventory whose State sets ``reservation_expiry_minutes = 120``. A sale is
written as a turn's intent (``_sale``): the payout's outbox row (Cal's gold ``+500``) and Cal's
sword held for it (``-1``, a ``reservation``), with its ``reservation_expiry`` timer scheduled in
the same transaction, as the commit path schedules it (#216). The dispatcher's and the
consumer's answers go through #217's intake (``outbox_dispatch``, ``ingest {kind: outcome}``);
the settlement runs in the test's own loop. The wall clock is a ``MovableClock``.

``test_s_reservation_expiry``:

- **Expiry → reuse → late success.** The payout is ``unknown`` at the 2-hour expiry → the hold
  is released and listed ``reservation_expired``; a later turn sells the sword to Mira, a
  merchant; the payout confirms → the sword is not taken from Mira, nothing in the story
  changes (no operation, no revision), and the effect is listed ``late_success_after_expiry``
  with its re-verification failed (``precondition:availability``: Cal no longer has it).
  Settling now is refused (409 ``settle_refused``). The author compensates → a refund row
  (``compensates``) is written at a slot and dispatched; the listing shows ``compensated`` until
  the refund is confirmed, then clears.
- **Not reused.** The same expiry with the sword still Cal's → the listing shows re-verification
  passing; settle-now applies the transfer at the next inter-turn slot (a ``settlement`` row,
  the released hold left as it is) and clears the listing.
- **Duplicates.** The same confirmation through the dispatcher and then the consumer's ingest →
  one settled record, the second ``duplicate``, no second job, one transfer.
- **Conflicts.** ``failed`` then ``confirmed`` → the failure stays settled, the confirmation is
  recorded ``conflict`` and listed ``outcome_conflict``, the released hold is not re-taken.
  ``confirmed`` then ``failed`` → the settlement stands and the failure is listed. ``unknown``
  then ``confirmed`` → settles normally.
- Complement: a confirmation before the expiry converts the hold normally.

``__actions``: both actions are idempotent under ``Idempotency-Key``; ``provider_read_only``;
settle-now and compensate on ``confirmed_discarded_origin`` (S-LATE-SETTLEMENT's discarded
origin: the purchase claimed, the player rewinds and plays a new turn, the confirmation arrives
→ nothing granted; settle-now re-verifies and grants it at the next slot, compensate refunds)
and on ``settlement_conflict`` (settle-now refused while the merchant has no sword, applied
once restocked; compensate refunds); an effect not listed for an action is 404.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock, states, timeline
from epistrel.core import outbox as core_outbox
from epistrel.core.enums import (
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
    StateContingency,
)
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs import timers as timer_api
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from epistrel.orchestrator.jobs.handlers import reservations
from tests import canon_support as c
from tests import late_settlement_support as ls
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, rows, scalar
from tests.projection_support import CONTROL_PRINCIPAL
from tests.respond_support import respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import structured_story
from tests.wall_clock_support import MovableClock, moving

pytestmark = pytest.mark.integration

EXPIRY_MINUTES = 120
PAYOUT = 500
REFUND = {"state": "gold", "delta": -PAYOUT, "reason": "the payout returned"}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _world(
    client: Any, engine: AsyncEngine, epistrel: Any
) -> tuple[str, str, str, ps.ScriptedProvider]:
    """The story (Cal, Mira the merchant, Jenni), the bank and the sword State (a two-hour
    reservation expiry); Cal holds one sword. Answers the story, Cal, Mira and the bank."""
    story, ids = await structured_story(client, "Cal", "Mira", "Jenni")
    fake = await ls.bank(engine, epistrel, story)
    await ls.swords(engine, story, ids["Cal"], reservation_expiry_minutes=EXPIRY_MINUTES)
    return story, ids["Cal"], ids["Mira"], fake


async def _sale(engine: AsyncEngine, story: str, provider: str, seller: str) -> tuple[str, str]:
    """A turn's intent: ``seller``'s sword sold for an external payout (the payout's outbox row,
    the sword held for it, the hold's expiry timer). Answers the outbox id and the hold's id."""
    outbox_id = new_ulid()
    held: list[str] = []

    async def write(conn: AsyncConnection, op: Operation) -> None:
        assert op.narrative_turn is not None
        await core_outbox.write_intended(
            conn,
            op,
            [
                core_outbox.Intended(
                    id=outbox_id,
                    provider_name=provider,
                    value_name="gold",
                    scope_key=seller,
                    effect={"kind": "change", "state": "gold", "delta": PAYOUT, "reason": "pay"},
                    beat=2,
                )
            ],
        )
        found = await states.apply_change(
            conn,
            op,
            states.StateChange(
                state="sword",
                reason="the sword sold",
                source=StateChangeSource.TURN,
                kind=StateChangeKind.TRANSFER_OUT,
                delta=Decimal(-1),
                scope_key=seller,
                position=Position(op.narrative_turn, op.turn_variant or 0, 0, 2),
                contingency=StateContingency.RESERVATION,
                status=StateChangeStatus.HELD,
                prerequisite_effect_ids=(outbox_id,),
            ),
        )
        assert isinstance(found, states.Applied), found
        held.append(found.change_id)
        due = clock.now() + timedelta(minutes=EXPIRY_MINUTES)
        await timer_api.schedule(
            conn, reservations.RESERVATION_EXPIRY, found.change_id, due, story_id=story
        )

    await c.run(engine, story, write, said="sell the sword")
    return outbox_id, held[0]


async def _expire(engine: AsyncEngine, moved: MovableClock, hold: str) -> None:
    """The two hours pass: the hold's timer is due, and its job releases the hold."""
    (timer,) = await rows(engine, "SELECT due_at FROM timers WHERE target = :t", t=hold)
    assert timer.due_at > clock.now()
    moved.advance(timedelta(minutes=EXPIRY_MINUTES + 1).total_seconds())
    assert timer.due_at <= clock.now()
    assert await reservations.expire(engine, hold) == reservations.RELEASED
    await execute_in_tx(engine, ("DELETE FROM timers WHERE target = :t", {"t": hold}))


async def _status(engine: AsyncEngine, change_id: str) -> str:
    return str(await scalar(engine, "SELECT status FROM state_changes WHERE id = :i", i=change_id))


async def _ingest(client: Any, story: str, outbox_id: str, outcome: str, key: str) -> Any:
    answer = await client.post(
        f"/stories/{story}/ingest",
        json={
            "events": [
                {
                    "client_event_id": key,
                    "kind": "outcome",
                    "outbox_id": outbox_id,
                    "outcome": outcome,
                }
            ]
        },
        headers=WRITE,
    )
    assert answer.status_code == 201, answer.text
    return answer


async def _kinds(engine: AsyncEngine, story: str, outbox_id: str) -> list[str]:
    return [r.kind for r in await ps.outbox_outcomes(engine, story) if r.outbox_id == outbox_id]


def _listing(found: core_outbox.Reconciliation, outbox_id: str) -> core_outbox.Listing:
    (listing,) = [e for e in found.listings if e.outbox_id == outbox_id]
    return listing


async def _revision(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story))


async def _settle_now(client: Any, story: str, outbox_id: str, key: str | None = None) -> Any:
    headers = WRITE if key is None else {**WRITE, "Idempotency-Key": key}
    return await client.post(
        f"/stories/{story}/reconciliation/{outbox_id}/settle-now", headers=headers
    )


async def _compensate(
    client: Any, story: str, outbox_id: str, effect: dict[str, Any], key: str | None = None
) -> Any:
    headers = WRITE if key is None else {**WRITE, "Idempotency-Key": key}
    return await client.post(
        f"/stories/{story}/reconciliation/{outbox_id}/compensate",
        json={"effect": effect},
        headers=headers,
    )


async def _settled(engine: AsyncEngine, epistrel: Any, story: str, outbox_id: str) -> Any:
    """Run the settlement the latest outcome record of ``outbox_id`` enqueued."""
    return await ls.settle(engine, epistrel, await ls.last_outcome(engine, outbox_id))


@pytest.mark.part("M6", "FR-EXT-22; FR-STATE-14")
async def test_s_reservation_expiry(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Expiry, reuse and the late success; the not-reused variant; duplicates; conflicting
    outcomes; and the complement (see the module docstring)."""
    with moving() as moved:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            story, cal, mira, fake = await _world(client, engine, epistrel)
            bank = fake.name

            # Complement: confirmed before the expiry → the hold converts as usual.
            fake.script_writes("gold", "confirmed")
            early, early_hold = await _sale(engine, story, bank, cal)
            assert (await _settled_after(engine, epistrel, early)).result == settlement.SETTLED
            assert await _status(engine, early_hold) == "converted"
            assert await ls.sword_of(engine, story, cal) == 0
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=cal, reason="again"))

            # Expiry → reuse → late success.
            fake.script_writes("gold", "unknown")
            sold, hold = await _sale(engine, story, bank, cal)
            await ls.answer(engine, epistrel, sold)
            assert await _kinds(engine, story, sold) == ["unknown"]
            await _expire(engine, moved, hold)
            assert await _status(engine, hold) == "released"
            assert (sold, "reservation_expired") in await ls.listed(engine, story)
            await ss.change(
                engine,
                story,
                ss.delta("sword", -1, scope_key=cal, reason="sold to Mira"),
                ss.delta("sword", 1, scope_key=mira, reason="bought from Cal"),
            )
            before = (await _revision(engine, story), len(await ss.change_rows(engine, story)))
            await _ingest(client, story, sold, "confirmed", "late-1")
            late = await _settled(engine, epistrel, story, sold)
            assert late.result == settlement.LATE
            assert late.operation_id is None
            assert await ls.sword_of(engine, story, mira) == 1
            assert await ls.sword_of(engine, story, cal) == 0
            assert (await _revision(engine, story), len(await ss.change_rows(engine, story))) == (
                before
            )
            assert await _status(engine, hold) == "released"
            listing = _listing(await core_outbox.listings(engine, story), sold)
            assert listing.reason == "late_success_after_expiry"
            assert listing.reverification is not None
            assert listing.reverification.reverification == "failed"
            assert settlement.AVAILABILITY in listing.reverification.reasons
            outcome = await ls.last_outcome(engine, sold)
            assert not [
                r for r in await ps.outbox_outcomes(engine, story) if r.settled and r.id == outcome
            ]

            # Settling now after the sword was reused is refused; nothing changes.
            refused = await _settle_now(client, story, sold)
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "settle_refused"
            assert settlement.AVAILABILITY in refused.json()["error"]["details"]["reasons"]
            assert await _revision(engine, story) == before[0]
            assert (sold, "late_success_after_expiry") in await ls.listed(engine, story)

            # The author compensates: a refund row at a slot, dispatched; listed until confirmed.
            made = await _compensate(client, story, sold, {**REFUND, "scope_key": cal})
            assert made.status_code == 201, made.text
            refund = made.json()["outbox_id"]
            assert made.json()["compensates"] == sold
            assert made.json()["slot"]["slot_k"] >= 1
            (row,) = [r for r in await ps.outbox_rows(engine, story) if r.id == refund]
            assert (row.status, row.compensates, row.effect["delta"]) == ("intended", sold, -PAYOUT)
            assert refund in {j.payload["outbox_id"] for j in await ps.outbox_jobs(engine, story)}
            listing = _listing(await core_outbox.listings(engine, story), sold)
            assert (listing.status, listing.compensated_by) == ("compensated", refund)
            assert listing.reason == "late_success_after_expiry"
            fake.script_writes("gold", "confirmed")
            await ls.answer(engine, epistrel, refund)
            assert sold not in {o for o, _ in await ls.listed(engine, story)}
            assert refund not in {o for o, _ in await ls.listed(engine, story)}
            resolved = await rows(
                engine,
                "SELECT resolved_by FROM reconciliation_marks WHERE outbox_id = :o",
                o=sold,
            )
            assert [r.resolved_by for r in resolved] == [core_outbox.RESOLVED_COMPENSATED]
            assert await ls.sword_of(engine, story, mira) == 1

            # Not reused: re-verification passes, and settle-now applies it at the next slot.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=cal, reason="found"))
            fake.script_writes("gold", "unknown")
            kept, kept_hold = await _sale(engine, story, bank, cal)
            await ls.answer(engine, epistrel, kept)
            await _expire(engine, moved, kept_hold)
            await _ingest(client, story, kept, "confirmed", "late-2")
            assert (await _settled(engine, epistrel, story, kept)).result == settlement.LATE
            listing = _listing(await core_outbox.listings(engine, story), kept)
            assert listing.reverification == core_outbox.Reverification(
                reverification="passed", reasons=[]
            )
            assert await ls.sword_of(engine, story, cal) == 1
            head = int(
                await scalar(
                    engine, "SELECT next_turn_index - 1 FROM stories WHERE id = :s", s=story
                )
            )
            done = await _settle_now(client, story, kept)
            assert done.status_code == 200, done.text
            assert done.json()["result"] == "settled"
            assert (done.json()["slot"]["turn"], done.json()["reverification"]) == (head, "passed")
            assert done.json()["slot"]["slot_k"] >= 1
            assert await ls.sword_of(engine, story, cal) == 0
            assert await ls.sword_of(engine, story, cal, head) == 1
            (settled_row,) = [
                r
                for r in await ls.settlement_rows(engine, story)
                if r.settles_change_id == kept_hold
            ]
            assert (settled_row.narrative_turn, settled_row.slot_k) == (
                head,
                done.json()["slot"]["slot_k"],
            )
            assert await _status(engine, kept_hold) == "released"
            assert kept not in {o for o, _ in await ls.listed(engine, story)}

            # Duplicates: the dispatcher's confirmation, then the consumer's → one transfer.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=cal, reason="again"))
            fake.script_writes("gold", "confirmed")
            twice, twice_hold = await _sale(engine, story, bank, cal)
            await ls.answer(engine, epistrel, twice)
            jobs = len(await ls.settle_jobs(engine, story))
            await _ingest(client, story, twice, "confirmed", "dup-1")
            assert await _kinds(engine, story, twice) == ["confirmed", "duplicate"]
            assert len(await ls.settle_jobs(engine, story)) == jobs
            (first,) = [
                r
                for r in await ps.outbox_outcomes(engine, story)
                if r.outbox_id == twice and r.kind == "confirmed"
            ]
            assert (await ls.settle(engine, epistrel, first.id)).result == settlement.SETTLED
            duplicate = await ls.last_outcome(engine, twice)
            assert (await ls.settle(engine, epistrel, duplicate)).result == settlement.NOTHING
            assert [r.settles_change_id for r in await ls.settlement_rows(engine, story)].count(
                twice_hold
            ) == 1
            assert await ls.sword_of(engine, story, cal) == 0
            assert twice not in {o for o, _ in await ls.listed(engine, story)}

            # Conflicts: failed, then confirmed → the failure stands, the hold is not re-taken.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=cal, reason="again"))
            fake.script_writes("gold", "failed")
            failed, failed_hold = await _sale(engine, story, bank, cal)
            await _settled_after(engine, epistrel, failed)
            assert await _status(engine, failed_hold) == "released"
            jobs = len(await ls.settle_jobs(engine, story))
            await _ingest(client, story, failed, "confirmed", "conflict-1")
            assert await _kinds(engine, story, failed) == ["failed", "conflict"]
            assert len(await ls.settle_jobs(engine, story)) == jobs
            assert (failed, "outcome_conflict") in await ls.listed(engine, story)
            assert await _status(engine, failed_hold) == "released"
            assert await ls.sword_of(engine, story, cal) == 1
            (row,) = [r for r in await ps.outbox_rows(engine, story) if r.id == failed]
            assert row.status == "failed"

            # Confirmed, then failed → the settlement stands, the failure is listed.
            fake.script_writes("gold", "confirmed")
            confirmed, confirmed_hold = await _sale(engine, story, bank, cal)
            assert (await _settled_after(engine, epistrel, confirmed)).result == (
                settlement.SETTLED
            )
            await _ingest(client, story, confirmed, "failed", "conflict-2")
            assert await _kinds(engine, story, confirmed) == ["confirmed", "conflict"]
            assert (confirmed, "outcome_conflict") in await ls.listed(engine, story)
            assert await _status(engine, confirmed_hold) == "converted"
            assert await ls.sword_of(engine, story, cal) == 0

            # Unknown, then confirmed → settles normally.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=cal, reason="again"))
            fake.script_writes("gold", "unknown")
            unsure, unsure_hold = await _sale(engine, story, bank, cal)
            await ls.answer(engine, epistrel, unsure)
            await _ingest(client, story, unsure, "confirmed", "unknown-1")
            assert await _kinds(engine, story, unsure) == ["unknown", "confirmed"]
            assert (await _settled(engine, epistrel, story, unsure)).result == settlement.SETTLED
            assert await _status(engine, unsure_hold) == "converted"
            assert await ls.sword_of(engine, story, cal) == 0
            await ls.drop_jobs(engine, story)
            await ss.set_level(engine, story, None)


async def _settled_after(engine: AsyncEngine, epistrel: Any, outbox_id: str) -> Any:
    """The dispatcher answers ``outbox_id`` as scripted, then its settlement runs."""
    return await ls.settle(engine, epistrel, await ls.answer(engine, epistrel, outbox_id))


async def _discarded(
    engine: AsyncEngine, epistrel: Any, story: str, bank: str, cal: str, mira: str
) -> str:
    """S-LATE-SETTLEMENT's discarded origin: the purchase claimed, the player rewinds before it
    and plays a new turn, then the confirmation arrives → listed, nothing granted."""
    sale = [
        ls.Side(mira, -1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_OUT),
        ls.Side(cal, 1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_IN),
    ]
    swords = await ls.sword_of(engine, story, cal)
    discarded, origin = await ls.intent(engine, story, bank, cal, sale)
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
    found = await ls.settle(engine, epistrel, await ls.last_outcome(engine, discarded))
    assert found.result == settlement.DISCARDED
    assert (discarded, "confirmed_discarded_origin") in await ls.listed(engine, story)
    assert await ls.sword_of(engine, story, cal) == swords
    return discarded


async def _conflicted(
    engine: AsyncEngine, epistrel: Any, story: str, bank: str, cal: str, mira: str
) -> str:
    """S-LATE-SETTLEMENT's intervening action: the merchant's last sword sold elsewhere before
    the confirmation → ``settlement_conflict``."""
    sale = [
        ls.Side(mira, -1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_OUT),
        ls.Side(cal, 1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_IN),
    ]
    conflicted, _ = await ls.intent(engine, story, bank, cal, sale)
    await ls.sell(engine, story, mira)
    await ls.turn(engine, story)
    found = await ls.settle(engine, epistrel, await ls.answer(engine, epistrel, conflicted))
    assert found.result == settlement.CONFLICTED
    assert (conflicted, "settlement_conflict") in await ls.listed(engine, story)
    return conflicted


async def _operations(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story))


@pytest.mark.part("M6", "FR-EXT-22")
async def test_s_reservation_expiry__actions(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Idempotency, ``provider_read_only``, ``settle_refused``, and both actions on a discarded
    origin and on a settlement conflict (see the module docstring)."""
    with moving():
        async with respond_client(stub_settings, engine) as (client, epistrel):
            story, ids = await structured_story(client, "Cal", "Mira", "Jenni")
            cal, mira = ids["Cal"], ids["Mira"]
            fake = await ls.bank(engine, epistrel, story)
            await ls.swords(engine, story, mira)
            bank = fake.name
            fake.script_writes("gold", "confirmed")

            # The terminal rule reads the records, not only the row's status: two confirmations
            # of a row never claimed (still intended) are one settled answer and a duplicate.
            unclaimed, _ = await ls.intent(engine, story, bank, cal, [])
            await _ingest(client, story, unclaimed, "confirmed", "unclaimed-1")
            await _ingest(client, story, unclaimed, "confirmed", "unclaimed-2")
            assert await _kinds(engine, story, unclaimed) == ["confirmed", "duplicate"]

            # Not listed for an action: 404, nothing written.
            unknown = await _settle_now(client, story, new_ulid())
            assert unknown.status_code == 404, unknown.text
            assert unknown.json()["error"]["details"]["reason"] == "no_listing"

            # Discarded origin: settle-now re-verifies and grants it at the next slot, under a
            # key: the retry replays the stored result and writes nothing.
            discarded = await _discarded(engine, epistrel, story, bank, cal, mira)
            done = await _settle_now(client, story, discarded, key="settle-1")
            assert done.status_code == 200, done.text
            assert done.json()["result"] == "settled"
            assert await ls.sword_of(engine, story, cal) == 1
            assert await ls.sword_of(engine, story, mira) == 0
            assert discarded not in {o for o, _ in await ls.listed(engine, story)}
            counted = (
                await _operations(engine, story),
                len(await ls.settlement_rows(engine, story)),
            )
            again = await _settle_now(client, story, discarded, key="settle-1")
            assert again.status_code == 200, again.text
            assert again.headers["Idempotent-Replay"] == "true"
            assert again.json() == done.json()
            assert (
                await _operations(engine, story),
                len(await ls.settlement_rows(engine, story)),
            ) == counted
            # Settled now, it is no longer listed for an action.
            gone = await _settle_now(client, story, discarded)
            assert gone.status_code == 404, gone.text

            # Discarded origin, compensated: a refund row, idempotent under its key.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=mira, reason="restock"))
            refunded = await _discarded(engine, epistrel, story, bank, cal, mira)
            made = await _compensate(
                client, story, refunded, {**REFUND, "delta": PAYOUT, "scope_key": cal}, key="comp-1"
            )
            assert made.status_code == 201, made.text
            refund = made.json()["outbox_id"]
            rows_before = len(await ps.outbox_rows(engine, story))
            replay = await _compensate(
                client, story, refunded, {**REFUND, "delta": PAYOUT, "scope_key": cal}, key="comp-1"
            )
            assert replay.status_code == 201, replay.text
            assert replay.headers["Idempotent-Replay"] == "true"
            assert replay.json()["outbox_id"] == refund
            assert len(await ps.outbox_rows(engine, story)) == rows_before
            listing = _listing(await core_outbox.listings(engine, story), refunded)
            assert (listing.reason, listing.status, listing.compensated_by) == (
                "confirmed_discarded_origin",
                "compensated",
                refund,
            )
            assert await ls.sword_of(engine, story, cal) == 1  # nothing granted for this one
            await ls.answer(engine, epistrel, refund)
            assert refunded not in {o for o, _ in await ls.listed(engine, story)}

            # Settlement conflict: refused while the merchant has no sword, applied once restocked.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=mira, reason="restock"))
            conflicted = await _conflicted(engine, epistrel, story, bank, cal, mira)
            revision = await _revision(engine, story)
            refused = await _settle_now(client, story, conflicted)
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "settle_refused"
            assert refused.json()["error"]["details"]["reasons"]
            assert await _revision(engine, story) == revision
            assert (conflicted, "settlement_conflict") in await ls.listed(engine, story)
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=mira, reason="restock"))
            swords = await ls.sword_of(engine, story, cal)
            done = await _settle_now(client, story, conflicted)
            assert done.status_code == 200, done.text
            assert await ls.sword_of(engine, story, cal) == swords + 1
            assert await ls.sword_of(engine, story, mira) == 0
            assert conflicted not in {o for o, _ in await ls.listed(engine, story)}

            # Settlement conflict, compensated.
            await ss.change(engine, story, ss.set_to("sword", 1, scope_key=mira, reason="restock"))
            other = await _conflicted(engine, epistrel, story, bank, cal, mira)
            made = await _compensate(
                client, story, other, {**REFUND, "delta": PAYOUT, "scope_key": cal}
            )
            assert made.status_code == 201, made.text
            assert (
                _listing(await core_outbox.listings(engine, story), other).status == "compensated"
            )

            # provider_read_only: the bank stops accepting writes; nothing is written.
            await execute_in_tx(
                engine,
                (
                    "UPDATE external_providers SET write_capable = false WHERE name = :n",
                    {"n": bank},
                ),
            )
            rows_before = len(await ps.outbox_rows(engine, story))
            readonly = await _compensate(
                client, story, other, {**REFUND, "delta": PAYOUT, "scope_key": cal}
            )
            assert readonly.status_code == 422, readonly.text
            assert readonly.json()["error"]["code"] == "provider_read_only"
            assert len(await ps.outbox_rows(engine, story)) == rows_before
            await execute_in_tx(
                engine,
                (
                    "UPDATE external_providers SET write_capable = true WHERE name = :n",
                    {"n": bank},
                ),
            )
            # A State no external provider owns is not compensable.
            local = await _compensate(
                client,
                story,
                other,
                {"state": "sword", "delta": 1, "scope_key": cal, "reason": "x"},
            )
            assert local.status_code == 422, local.text
            assert local.json()["error"]["code"] == "state_unpermitted"
            await ls.drop_jobs(engine, story)
            await ss.set_level(engine, story, None)
