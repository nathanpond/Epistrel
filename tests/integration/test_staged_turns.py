"""A staged turn under M3's operation machinery (#212; FR-EXT-16,17,19, FR-CONC-5,16).

- **The slot and the sweeps.** A staged turn stuck short of its completion (its outcome
  transaction failed) keeps its story's turn slot past its lease: a newcomer's ``acquire``
  finds it busy, the lapsed-lease reclaim and ``orphans.fail_orphans`` skip it (never
  ``failed``, never ``turn_interrupted``); #213: the newcomer's request schedules its resume,
  which records the outcome and completes it, then gives the slot back.
- **A pause or an erasure block after intent** does not refuse the turn's later stages (the
  same operation's work); a pause before intent refuses the turn with 409 ``story_paused``.
- **A discard** (a truncation while the turn waits) abandons it: ``turn_stage = abandoned``,
  its remaining stages never commit (409 ``timeline_changed``), the slot is given back, the
  claimed effect is listed ``reconciliation_required``, and its operation view shows the
  unrecorded stages ``skipped``.
- **FR-CONC-16 at the claim**: a provider no longer write-capable when the turn claims cancels
  the row (listed ``reconciliation_required``), and the outcome step narrates the attempt as
  not carried out.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import orphans, turn_gate
from epistrel.core import outbox as core_outbox
from epistrel.core.ids import new_ulid
from epistrel.core.states import contingency
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.structured import staged
from tests import effect_support as es
from tests import late_settlement_support as ls
from tests import provider_support as ps
from tests import staged_support as sp
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, rows
from tests.respond_support import AUTHOR, respond_client
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _holder(engine: AsyncEngine, story: str) -> list[Any]:
    found = await rows(engine, "SELECT in_flight_turn FROM stories WHERE id = :s", s=story)
    return [r.in_flight_turn for r in found]


REVOKE = "UPDATE external_providers SET write_capable = false WHERE name = :n"


class Boom(Exception):
    """Induced failure."""


async def _status(engine: AsyncEngine, story: str, status: str) -> None:
    before = "active" if status == "blocked_by_erasure" else None
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET status = :t, status_before_block = :b WHERE id = :s",
            {"s": story, "t": status, "b": before},
        ),
    )


async def _waiting(
    client: Any, engine: AsyncEngine, epistrel: Any
) -> tuple[str, str, ps.ScriptedProvider, asyncio.Event, asyncio.Task[Any]]:
    """A purchase turn whose provider holds its answer: the story, Cal, the fake, the release
    and the running ``respond``, once the turn awaits its outcome."""
    story, cal, _, fake = await sp.sync_story(client, engine, epistrel)
    released = asyncio.Event()
    fake.script_writes("gold", ps.Hang(released))
    sp.script_purchase(epistrel.models.stub, cal)
    playing = asyncio.create_task(sp.buy(client, story, cal))
    await asyncio.wait_for(fake.hanging.wait(), 60)
    return story, cal, fake, released, playing


async def test_an_unfinished_staged_turn_keeps_its_slot_until_resumed(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, bank, fake = await sp.sync_story(client, engine, epistrel)
        fake.script_writes("gold", "confirmed")
        # #217: an earlier turn's payment, still unanswered when the purchase is played.
        earlier, _ = await ls.intent(engine, story, bank, cal, [])
        sp.script_purchase(epistrel.models.stub, cal)

        async def boom(*_: Any, **__: Any) -> Any:
            raise Boom("the outcome transaction fails")

        with monkeypatch.context() as patched:
            patched.setattr(contingency, "settle", boom)
            with contextlib.suppress(Boom):
                await sp.buy(client, story, cal)
        op = await sp.staged_op(engine, story)
        assert (op.status, op.turn_stage) == ("committed", "awaiting_outcome")
        # #222 (FR-STORE-23): a staged turn under way is no checkpoint yet.
        from epistrel.core.errors import CheckpointIncomplete
        from epistrel.core.views import Checkpoint, as_originally_recorded, boundary_tx

        with pytest.raises(CheckpointIncomplete):
            await as_originally_recorded(engine, story, Checkpoint(int(op.narrative_turn)))
        # The lease lapses (the process that ran the turn is gone).
        await execute_in_tx(
            engine,
            (
                "UPDATE stories SET in_flight_lease_until = clock_timestamp() "
                "- interval '1 second' WHERE id = :s",
                {"s": story},
            ),
        )
        # #217: the earlier payment's answer arrives late and its settlement is queued; it waits
        # while the staged turn is incomplete (FR-EXT-21).
        late = await ls.answer(engine, epistrel, earlier)
        assert late in [j.payload["outcome_id"] for j in await ls.settle_jobs(engine, story)]
        assert (await ls.settle(engine, epistrel, late)).result == settlement.WAITING
        assert await ls.settlement_rows(engine, story) == []
        # A newcomer finds the story busy, and the request schedules the turn's resume (#213).
        held = await turn_gate.acquire(engine, story, new_ulid(), lease_s=30)
        assert held is not None
        assert held.operation_id == op.operation_id
        assert op.operation_id not in await orphans.fail_orphans(engine, grace_s=0)
        assert op.operation_id not in await orphans.reclaim_expired_slots(engine)
        again = await sp.staged_op(engine, story)
        assert again.status == "committed"  # never failed, never turn_interrupted
        await staged.settle_kicked()
        # #213: resumed from its last committed stage: the outcome recorded once, the turn
        # completed with the deterministic narration, the slot given back.
        done = await sp.staged_op(engine, story)
        assert (done.status, done.turn_stage) == ("committed", "completed")
        assert await sp.stage_names(engine, story) == ["intent", "claim", "outcome", "completion"]
        # #222: its boundary is the completion's commit, after the intent's.
        (committed,) = await rows(
            engine,
            "SELECT committed_at FROM operations WHERE operation_id = :o",
            o=op.operation_id,
        )
        async with engine.connect() as conn:
            at = await boundary_tx(conn, story, Checkpoint(int(done.narrative_turn)))
        (completion,) = [e for e in done.stage_log if e.get("stage") == staged.COMPLETION]
        assert at.isoformat() == completion["at"]
        assert at > committed.committed_at
        texts = await rows(
            engine,
            "SELECT payload->>'text' AS text FROM events WHERE operation_id = :o "
            "AND type = 'beat_narration'",
            o=op.operation_id,
        )
        assert f"Cal's {sp.REASON} went through." in [t.text for t in texts]
        # The purchase, answered before the failure (never re-sent), and the earlier payment.
        assert len(fake.writes) == 2
        assert await _holder(engine, story) == [None]
        # #217: the resumed turn is complete, so the queued settlement proceeds, at the slot
        # after it, never inside it.
        settled = await ls.settle(engine, epistrel, late)
        assert settled.result == settlement.SETTLED
        assert settled.position is not None
        assert (settled.position.turn, settled.position.slot_k) == (done.narrative_turn, 1)
        await ls.drop_jobs(engine, story)


async def test_a_pause_or_block_after_intent_does_not_refuse_later_stages(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        for status in ("paused", "blocked_by_erasure"):
            story, _, _, released, playing = await _waiting(client, engine, epistrel)
            await _status(engine, story, status)
            try:
                released.set()
                answer = await asyncio.wait_for(playing, 60)
            finally:
                await _status(engine, story, "active")
            assert answer.status_code == 200, answer.text
            assert sp.WENT in answer.json()["message"]["text"]
            assert (await sp.staged_op(engine, story)).turn_stage == "completed"
            await ps.drop_outbox_jobs(engine, story)

        # Complement: a pause before the intent refuses the turn.
        story, cal, _, _ = await sp.sync_story(client, engine, epistrel)
        sp.script_purchase(epistrel.models.stub, cal)
        await _status(engine, story, "paused")
        try:
            refused = await sp.buy(client, story, cal)
        finally:
            await _status(engine, story, "active")
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "story_paused"


async def test_a_discard_abandons_the_staged_turn(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, _, _, released, playing = await _waiting(client, engine, epistrel)
        op = await sp.staged_op(engine, story)
        answer = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": op.narrative_turn - 1}, headers=WRITE
        )
        assert answer.status_code == 200, answer.text
        (row,) = await ps.outbox_rows(engine, story)
        assert [r["outbox_id"] for r in answer.json()["reconciliation_required"]] == [row.id]
        assert (await sp.staged_op(engine, story)).turn_stage == "abandoned"
        released.set()
        answer = await asyncio.wait_for(playing, 60)
        assert answer.status_code == 409, answer.text
        assert answer.json()["error"]["code"] == "timeline_changed"
        op = await sp.staged_op(engine, story)
        assert op.turn_stage == "abandoned"
        assert await sp.stage_names(engine, story) == ["intent", "claim"]
        # #222: an abandoned staged turn is a checkpoint at its last logged stage (the claim).
        from epistrel.core.views import Checkpoint, boundary_tx

        async with engine.connect() as conn:
            at = await boundary_tx(conn, story, Checkpoint(int(op.narrative_turn)), variant=None)
        (claim,) = [e for e in op.stage_log if e.get("stage") == staged.CLAIM]
        assert at.isoformat() == claim["at"]
        holder = await _holder(engine, story)
        assert holder == [None]
        view = await client.get(f"/operations/{op.operation_id}", headers=READ)
        assert [(s["name"], s["status"]) for s in view.json()["stages"]] == [
            ("intent", "done"),
            ("claim", "done"),
            ("outcome", "skipped"),
            ("completion", "skipped"),
            ("response", "skipped"),
        ]
        listed = (await core_outbox.listings(engine, story)).listings
        assert ("reconciliation_required", row.id) in {(e.reason, e.outbox_id) for e in listed}
        await ps.drop_outbox_jobs(engine, story)


async def test_a_provider_no_longer_write_capable_at_claim(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, name, fake = await sp.sync_story(client, engine, epistrel)
        stub = epistrel.models.stub
        sp.script_purchase(stub, cal, "Mira pockets the coins; the deal is never sent.")
        claim_all = staged.claim_all

        async def revoked(*args: Any, **kwargs: Any) -> Any:
            # The provider's registration changes between the intent and the claim.
            await execute_in_tx(engine, (REVOKE, {"n": name}))
            return await claim_all(*args, **kwargs)

        with monkeypatch.context() as patched:
            patched.setattr(staged, "claim_all", revoked)
            answer = await sp.buy(client, story, cal)
        assert answer.status_code == 200, answer.text
        (prompt,) = [p for p in es.prompts(stub, "NarratorRender") if staged.OUTCOME_STEP in p]
        assert "not carried out; narrate the attempt as not carried out" in prompt
        (row,) = await ps.outbox_rows(engine, story)
        assert row.status == "cancelled"
        assert fake.idempotency_keys == []  # never sent
        listed = (await epistrel.reconciliation(AUTHOR, story)).listings
        assert [(e.outbox_id, e.reason) for e in listed] == [(row.id, "reconciliation_required")]
        assert (await sp.staged_op(engine, story)).turn_stage == "completed"
