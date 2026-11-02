"""#655: a settlement the story's state refuses (here an archived story) backs off instead of
re-enqueueing itself every 5 s: its waits double from ``WAIT`` and stop at ``WAIT_MAX``, and the
re-enqueued job counts them. An incomplete staged turn's wait (S-LATE-SETTLEMENT's case 4) is
not a refusal and keeps ``WAIT``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import stories
from epistrel.core.enums import StateChangeKind, StateContingency
from epistrel.orchestrator.jobs import settlement
from epistrel.orchestrator.jobs.types import JobContext
from tests import late_settlement_support as ls
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def test_the_wait_doubles_up_to_its_bound() -> None:
    waits = [settlement.wait_after(n) for n in range(10)]
    assert waits[:4] == [timedelta(seconds=s) for s in (5, 10, 20, 40)]
    assert waits[-1] == settlement.WAIT_MAX == timedelta(minutes=10)
    assert waits == sorted(waits)
    assert settlement.wait_after(10_000) == settlement.WAIT_MAX


async def _run(engine: AsyncEngine, story: str, outcome: str, waits: int) -> Any:
    """Run the settlement job once with ``waits`` refusals behind it; the job it re-enqueued."""
    await ls.drop_jobs(engine, story)
    payload = settlement.SettlePayload(outcome_id=outcome, waits=waits)
    output = await settlement.SETTLE_HANDLER.run(
        JobContext(db=engine, job=cast(Any, None), payload=payload)
    )
    assert output.result == {"outcome": settlement.WAITING}
    (job,) = await ls.settle_jobs(engine, story)
    return job


async def test_an_archived_storys_settlement_backs_off(
    stub_settings: Settings, engine: AsyncEngine, states_delivered: None
) -> None:
    async with respond_client(stub_settings, engine) as (_client, epistrel):
        story, ids = await structured_story(_client, "Cal", "Mira")
        bank = await ls.bank(engine, epistrel, story)
        bank.script_writes("gold", "confirmed")
        await ls.swords(engine, story, ids["Mira"])
        sale = [
            ls.Side(ids["Mira"], -1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_OUT),
            ls.Side(ids["Cal"], 1, StateContingency.CONDITIONAL, StateChangeKind.TRANSFER_IN),
        ]
        payment, _ = await ls.intent(engine, story, bank.name, ids["Cal"], sale)
        outcome = await ls.answer(engine, epistrel, payment)
        await stories.archive_story(engine, story, principal="p-owner")

        for waits, expected in ((0, 5), (1, 10), (3, 40), (12, 600)):
            started = datetime.now(UTC)
            job = await _run(engine, story, outcome, waits)
            assert job.payload.get("waits") == waits + 1
            delay = (job.due_at - started).total_seconds()
            assert expected - 1 <= delay <= expected + 5, (waits, delay)
        await ls.drop_jobs(engine, story)
