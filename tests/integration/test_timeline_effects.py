"""External effects across the timeline edits' edges (#211; FR-EXT-12,13,14), against Postgres.

- A claim that took the row lock before a truncation committed wins: the truncation waits for
  it, reads the row ``claimed`` and answers it as ``reconciliation_required`` (the other order,
  the truncation first, is S-EFFECT-LIFECYCLE (a)).
- ``core.outbox.mark`` keeps one open mark per row and reason; a resolved mark lets the row be
  marked again.
- An edit held back for its reactivation check (#120, 202 ``RatingCheckStarted``) classifies
  the effects in the transaction where the check job applies it: ``reconciliation_required``
  and ``cancelled_on_reactivation`` are part of ``rating_check_jobs.result``.
- Every control operation writes no outbox row (no compensation is issued automatically).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import outbox as core_outbox
from epistrel.core import timeline
from epistrel.core.ids import new_ulid
from epistrel.core.seeds.ratings import TEEN_ID
from epistrel.orchestrator.jobs.handlers import outbox
from tests import provider_support as ps
from tests.event_support import new_story
from tests.integration.test_outbox_dispatch import Setup, _turn
from tests.integration.test_rating_change import (
    _edit,
    _ended,
    _forget_checks,
    _held,
    _set_aside,
)
from tests.rating_change_support import guard_rating, rating_db, rows
from tests.respond_support import AUTHOR, body, played

pytestmark = pytest.mark.integration


@pytest.fixture
async def setup(engine: AsyncEngine) -> AsyncIterator[Setup]:
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name))
    story = await new_story(engine)
    await ps.bind(engine, story, name, {"player": "p-1"})
    found = Setup(engine, story, name, ps.ScriptedProvider(name))
    yield found
    await ps.drop_outbox_jobs(engine, story)


async def _count(engine: AsyncEngine, story: str) -> int:
    return len(await ps.outbox_rows(engine, story))


async def test_a_claim_holding_the_row_lock_is_listed_by_the_truncation(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, story = setup.engine, setup.story
    await _turn(engine, story, setup.name, count=0)
    (row,) = await _turn(engine, story, setup.name)
    reading, release = asyncio.Event(), asyncio.Event()
    original = core_outbox.origin_active

    async def slow(conn: Any, operation_id: str) -> bool:
        found = await original(conn, operation_id)
        reading.set()
        await release.wait()  # the claim holds the row lock, having read ``active``
        return found

    monkeypatch.setattr(core_outbox, "origin_active", slow)
    claiming = asyncio.create_task(outbox.claim(engine, row, registry=setup.registry))
    await asyncio.wait_for(reading.wait(), timeout=30)
    truncating = asyncio.create_task(
        timeline.truncate(engine, story, timeline.TruncateRequest(to_turn=1), principal="p-test")
    )
    await asyncio.sleep(0.3)
    assert not truncating.done()  # waiting on the row lock
    release.set()
    claimed = await claiming
    assert isinstance(claimed, outbox.Claimed)
    done = await truncating
    assert [(r.outbox_id, r.status) for r in done.reconciliation_required] == [(row, "claimed")]
    listed = {e.outbox_id: e.reason for e in (await core_outbox.listings(engine, story)).listings}
    assert listed == {row: "reconciliation_required"}
    # The send goes ahead (the claim's commit was the in-flight point); still listed after.
    assert await outbox.send(engine, claimed) == "confirmed"
    listed = {e.outbox_id: e.reason for e in (await core_outbox.listings(engine, story)).listings}
    assert listed == {row: "reconciliation_required"}


async def test_one_open_mark_per_row_and_reason(setup: Setup) -> None:
    engine, story = setup.engine, setup.story
    (row,) = await _turn(engine, story, setup.name)
    async with engine.begin() as conn:
        assert await core_outbox.mark(conn, story, row, "reconciliation_required")
        assert not await core_outbox.mark(conn, story, row, "reconciliation_required")
        assert await core_outbox.mark(conn, story, row, "cancelled_on_reactivation")
        await conn.execute(
            text("UPDATE reconciliation_marks SET resolved_by = 'test' WHERE outbox_id = :o"),
            {"o": row},
        )
        assert await core_outbox.mark(conn, story, row, "reconciliation_required")
        with pytest.raises(ValueError, match="not a reconciliation reason"):
            await core_outbox.mark(conn, story, row, "compensated")


async def _outbox_row(s: Any, story_id: str, turn: int, variant: int, status: str) -> str:
    """An outbox row of the turn variant's operation, as #210's commit would have written it
    (``status`` already what the dispatcher and #211's flips made of it)."""
    found = await rows(
        s.db,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND narrative_turn = :t AND turn_variant = :v",
        s=story_id,
        t=turn,
        v=variant,
    )
    operation_id = found[0][0]
    outbox_id = await _insert(s.db, story_id, operation_id, status)
    return outbox_id


async def _insert(db: AsyncEngine, story_id: str, operation_id: str, status: str) -> str:
    outbox_id = new_ulid()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO external_effect_outbox (id, story_id, provider_name, value_name, "
                "effect, status, operation_id, narrative_turn, turn_variant, membership, "
                "membership_tx) SELECT :i, :s, 'bank', 'gold', "
                '\'{"kind": "change", "state": "gold", "delta": -5}\'::jsonb, :st, '
                "o.operation_id, o.narrative_turn, o.turn_variant, o.membership, o.membership_tx "
                "FROM operations o WHERE o.operation_id = :o"
            ),
            {"i": outbox_id, "s": story_id, "st": status, "o": operation_id},
        )
    return outbox_id


async def test_a_held_back_swap_records_the_effects_in_the_check_result(pg_url: str) -> None:
    async with rating_db(pg_url) as s:
        await guard_rating(s, TEEN_ID)
        story_id, player = await _set_aside(s, TEEN_ID, "A cat purrs.", "A cat naps.")
        # Turn 2's payment was cancelled when it was rewound; a newer turn 2 paid and confirmed.
        cancelled = await _outbox_row(s, story_id, 2, 1, "cancelled")
        s.lib.models.stub.script("generation", ["Wind in the eaves."])
        await played(s.client, story_id, body(player, "Again?"))
        confirmed = await _outbox_row(s, story_id, 2, 2, "confirmed")
        before = len(await rows(s.db, "SELECT id FROM external_effect_outbox"))
        await _forget_checks(s, story_id)
        chain = {"to_variant_chain": {"turn": 2, "variant": 1}}
        job = await _held(await _edit(s, story_id, "swap", chain), story_id)
        found = await _ended(s, story_id, job)
        assert found["status"] == "reactivated", found
        result = found["outcome"]["result"]
        assert result["reconciliation_required"] == [
            {"outbox_id": confirmed, "provider": "bank", "status": "confirmed", "value": "gold"}
        ]
        assert result["cancelled_on_reactivation"] == [
            {"outbox_id": cancelled, "provider": "bank", "status": "cancelled", "value": "gold"}
        ]
        listed = {
            e.outbox_id: e.reason for e in (await s.lib.reconciliation(AUTHOR, story_id)).listings
        }
        assert listed == {
            confirmed: "reconciliation_required",
            cancelled: "cancelled_on_reactivation",
        }
        assert len(await rows(s.db, "SELECT id FROM external_effect_outbox")) == before
