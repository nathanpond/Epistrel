"""The outbox dispatcher's edges (#210; FR-EXT-12,17,18, NFR-REL-5), against Postgres.

- A story the erasure reconciler blocked (#125) keeps its rows ``intended``: the claim skips
  them until the block clears, and then the row is sent.
- An ``in_process`` provider with no write callable in this process, a story with no binding
  and a provider no longer write-capable leave the row ``intended`` (not claimed).
- An adapter that refuses before sending (``ProviderUnavailable``) releases the claim: the row is
  ``intended`` again and its lease timer is gone.
- The ``outbox_recovery`` sweep enqueues one dispatch job per ``intended`` row with no live job,
  and none for a row that has one or is not ``intended``.
- A fork copies outbox rows as records only (``executable = false``): never claimed or swept.
- ``outbox_outcomes`` is append-only: an update other than ``settled``, a delete and a TRUNCATE
  are refused; ``settled`` may change; story deletion removes both tables' rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import outbox as core_outbox
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events.operation import Operation
from epistrel.core.fork import ForkRequest, fork
from epistrel.core.ids import new_ulid
from epistrel.core.stories import delete_story
from epistrel.orchestrator.jobs.handlers import outbox
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from tests import provider_support as ps
from tests.event_support import new_story

pytestmark = pytest.mark.integration


async def _turn(engine: AsyncEngine, story: str, name: str, count: int = 1) -> list[str]:
    ids = [new_ulid() for _ in range(count)]
    async with Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p-test") as op:
        op.append("player_input", {"text": "pay"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "the coins change hands"})

        async def write(conn: AsyncConnection) -> None:
            await core_outbox.write_intended(
                conn,
                op,
                [
                    core_outbox.Intended(
                        id=row_id,
                        provider_name=name,
                        value_name="gold",
                        scope_key=None,
                        effect={"kind": "change", "state": "gold", "delta": -5, "reason": "dues"},
                        beat=2,
                    )
                    for row_id in ids
                ],
            )

        op.add_commit_step(write)
        await op.commit()
    return ids


async def _status(engine: AsyncEngine, outbox_id: str) -> str:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT status FROM external_effect_outbox WHERE id = :i"), {"i": outbox_id}
        )
    return str(found)


async def _sql(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


class Setup:
    def __init__(self, engine: AsyncEngine, story: str, name: str, fake: ps.ScriptedProvider):
        self.engine, self.story, self.name, self.fake = engine, story, name, fake
        self.registry = InProcessRegistry()
        self.registry.register(name, read=fake.read, write=fake.write)


@pytest.fixture
async def setup(engine: AsyncEngine) -> AsyncIterator[Setup]:
    name = ps.unique_name("bank")
    await ps.register(engine, ps.write_capable_spec(name))
    story = await new_story(engine)
    await ps.bind(engine, story, name, {"player": "p-1"})
    found = Setup(engine, story, name, ps.ScriptedProvider(name))
    yield found
    await ps.drop_outbox_jobs(engine, story)


async def test_a_blocked_story_keeps_its_rows_intended(setup: Setup) -> None:
    engine, story = setup.engine, setup.story
    (row,) = await _turn(engine, story, setup.name)
    await _sql(
        engine,
        "UPDATE stories SET status = 'blocked_by_erasure', status_before_block = 'active' "
        "WHERE id = :s",
        s=story,
    )
    try:
        assert await outbox.dispatch(engine, row, registry=setup.registry) == outbox.HELD
        assert await _status(engine, row) == "intended"
        assert setup.fake.idempotency_keys == []
    finally:
        await _sql(
            engine,
            "UPDATE stories SET status = 'active', status_before_block = NULL WHERE id = :s",
            s=story,
        )
    assert await outbox.dispatch(engine, row, registry=setup.registry) == "confirmed"
    assert setup.fake.idempotency_keys == [row]


async def test_a_row_that_cannot_be_sent_from_here_stays_intended(setup: Setup) -> None:
    engine, story = setup.engine, setup.story
    (row,) = await _turn(engine, story, setup.name)
    # No callable registered in this process (another process's provider).
    assert await outbox.dispatch(engine, row, registry=InProcessRegistry()) == outbox.HELD
    # A read callable but no write callable.
    reads_only = InProcessRegistry()
    reads_only.register(setup.name, read=setup.fake.read)
    assert await outbox.dispatch(engine, row, registry=reads_only) == outbox.HELD
    # No binding for the story.
    await _sql(engine, "DELETE FROM story_provider_bindings WHERE story_id = :s", s=story)
    assert await outbox.dispatch(engine, row, registry=setup.registry) == outbox.HELD
    await ps.bind(engine, story, setup.name, {"player": "p-1"})
    # The provider no longer write-capable.
    await _sql(
        engine, "UPDATE external_providers SET write_capable = false WHERE name = :n", n=setup.name
    )
    try:
        assert await outbox.dispatch(engine, row, registry=setup.registry) == outbox.HELD
    finally:
        await _sql(
            engine,
            "UPDATE external_providers SET write_capable = true WHERE name = :n",
            n=setup.name,
        )
    assert await _status(engine, row) == "intended"
    assert setup.fake.idempotency_keys == []
    assert await outbox.dispatch(engine, row, registry=setup.registry) == "confirmed"


async def test_a_refusal_before_sending_releases_the_claim(setup: Setup) -> None:
    engine, story = setup.engine, setup.story
    (row,) = await _turn(engine, story, setup.name)
    setup.fake.script_writes("gold", ps.Fail("secret_missing"), "confirmed")
    assert await outbox.dispatch(engine, row, registry=setup.registry) == outbox.RELEASED
    assert await _status(engine, row) == "intended"
    async with engine.connect() as conn:
        timers = await conn.scalar(
            text("SELECT count(*) FROM timers WHERE kind = 'outbox_lease' AND target = :i"),
            {"i": row},
        )
    assert timers == 0
    assert await ps.outbox_outcomes(engine, story) == []
    assert await outbox.dispatch(engine, row, registry=setup.registry) == "confirmed"


async def test_the_recovery_sweep_enqueues_once_per_unattended_row(setup: Setup) -> None:
    engine, story = setup.engine, setup.story
    first, second, third = await _turn(engine, story, setup.name, count=3)
    async with engine.begin() as conn:
        await outbox.enqueue(conn, first, story)
    assert await outbox.dispatch(engine, third, registry=setup.registry) == "confirmed"
    await outbox.recover(engine)
    jobs = await ps.outbox_jobs(engine, story)
    assert sorted(j.payload["outbox_id"] for j in jobs) == sorted([first, second])
    await outbox.recover(engine)
    assert len(await ps.outbox_jobs(engine, story)) == 2


async def test_a_fork_copies_no_executable_effect(setup: Setup) -> None:
    """FR-EXT-13: a fork's copies of the outbox are records only (``executable = false``), so
    the dispatcher never claims one; the outcome records follow the copied rows."""
    engine, story = setup.engine, setup.story
    pending, sent = await _turn(engine, story, setup.name, count=2)
    assert await outbox.dispatch(engine, sent, registry=setup.registry) == "confirmed"
    forked = await fork(engine, story, ForkRequest(), principal="p-test")
    await ps.bind(engine, forked.story_id, setup.name, {"player": "p-1"})
    copies = await ps.outbox_rows(engine, forked.story_id)
    assert sorted(r.status for r in copies) == ["confirmed", "intended"]
    assert not any(r.executable for r in copies)
    assert {r.id for r in copies}.isdisjoint({pending, sent})
    (copied,) = await ps.outbox_outcomes(engine, forked.story_id)
    assert copied.outbox_id in {r.id for r in copies}
    for row in copies:
        assert await outbox.dispatch(engine, row.id, registry=setup.registry) == outbox.SKIPPED
    await outbox.recover(engine)
    assert await ps.outbox_jobs(engine, forked.story_id) == []
    assert setup.fake.idempotency_keys == [sent]
    await ps.drop_outbox_jobs(engine, forked.story_id)


async def test_outcome_records_are_append_only(setup: Setup) -> None:
    engine, story = setup.engine, setup.story
    (row,) = await _turn(engine, story, setup.name)
    assert await outbox.dispatch(engine, row, registry=setup.registry) == "confirmed"
    (outcome,) = await ps.outbox_outcomes(engine, story)
    await _sql(engine, "UPDATE outbox_outcomes SET settled = true WHERE id = :i", i=outcome.id)
    for statement in (
        "UPDATE outbox_outcomes SET kind = 'failed' WHERE id = :i",
        "DELETE FROM outbox_outcomes WHERE id = :i",
        "TRUNCATE outbox_outcomes",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            await _sql(engine, statement, i=outcome.id)
    (kept,) = await ps.outbox_outcomes(engine, story)
    assert (kept.kind, kept.settled) == ("confirmed", True)

    counts = await delete_story(engine, story, principal="p-test")
    assert counts["external_effect_outbox"] == 1
    assert counts["outbox_outcomes"] == 1
    assert await ps.outbox_rows(engine, story) == []
