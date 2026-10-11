"""S-DOUBLE-SUBMIT: idempotent retries of one respond request (docs/04 §7; #61).

The same request under one ``Idempotency-Key`` is sent again (a) after the original completed,
(b) while the original is still generating, and (c) after the engine was killed between
generation and commit (a child engine process, ``epistrel.testing.faults``, killed with
``SIGKILL``). Every case commits exactly one turn and every retry answers the same message; the
same key with a different payload is refused.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.testing.faults import BEFORE_COMMIT, EngineProcess
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.idempotency_support import (
    REPLAY,
    committed_turns,
    key_row,
    keyed,
    message_of,
    turn_statuses,
)
from tests.respond_support import PLAYER, body, generation_calls, new_story, respond_client

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M2", "FR-CONC-6; NFR-REL-4")
async def test_s_double_submit(stub_settings: Settings, engine: AsyncEngine, pg_url: str) -> None:
    """(a) after completion, (b) during generation, (c) after a crash between generation and
    commit: one committed turn per key and the same message on every answer; a different
    payload under the key is 409. Complement: a different key is a new turn."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # (a) After the original completed: the stored answer, no second generation.
        story, ids = await new_story(client)
        request = body(ids[PLAYER], "Is anyone there?")
        first = await keyed(client, story, request, "dbl-a")
        assert first.status_code == 200, first.text
        calls = len(generation_calls(stub))
        again = await keyed(client, story, request, "dbl-a")
        assert (again.status_code, again.headers.get(REPLAY)) == (200, "true")
        assert again.content == first.content
        assert len(generation_calls(stub)) == calls
        assert len(await committed_turns(engine, story)) == 1
        other = await keyed(client, story, body(ids[PLAYER], "Something else."), "dbl-a")
        assert (other.status_code, other.json()["error"]["code"]) == (409, "idempotency_conflict")
        fresh = await keyed(client, story, request, "dbl-a2")
        assert fresh.status_code == 200
        assert message_of(fresh) != message_of(first)
        assert len(await committed_turns(engine, story)) == 2

        # (b) While the original is still generating: the duplicate waits for it.
        story, ids = await new_story(client)
        request = body(ids[PLAYER], "Hold the door.")
        latch = stub.hold("generation")
        original = asyncio.create_task(keyed(client, story, request, "dbl-b"))
        await latch.entered.wait()
        duplicate = asyncio.create_task(keyed(client, story, request, "dbl-b"))
        await asyncio.sleep(0.4)
        assert not duplicate.done()
        latch.release()
        answered, waited = await original, await duplicate
        assert (answered.status_code, waited.status_code) == (200, 200), waited.text
        assert waited.headers.get(REPLAY) == "true"
        assert waited.content == answered.content
        assert len(await committed_turns(engine, story)) == 1
        refused = await keyed(client, story, body(ids[PLAYER], "Another thing."), "dbl-b")
        assert refused.status_code == 409

        # (c) The engine dies between generation and commit; the retry re-executes once.
        story, ids = await new_story(client)
        request = body(ids[PLAYER], "Did the lights just flicker?")
        child = EngineProcess(pg_url, CREDENTIAL_A, pause_before_commit=True)
        async with child, httpx.AsyncClient(base_url=child.base_url, timeout=30) as remote:
            pending = asyncio.create_task(keyed(remote, story, request, "dbl-c"))
            marker = await child.next_marker()
            assert marker.startswith(BEFORE_COMMIT)
            await child.kill()
            with contextlib.suppress(httpx.HTTPError):
                await pending
            assert pending.exception() is not None or pending.result().status_code >= 500
        assert await committed_turns(engine, story) == []
        assert (await key_row(engine, "dbl-c")).status == "in_flight"
        retried = await keyed(client, story, request, "dbl-c")
        assert retried.status_code == 200, retried.text
        replayed = await keyed(client, story, request, "dbl-c")
        assert (replayed.headers.get(REPLAY), replayed.content) == ("true", retried.content)
        (turn,) = await committed_turns(engine, story)
        assert (await key_row(engine, "dbl-c")).operation_id == turn.operation_id
        assert message_of(retried)[0] in {b["message_id"] for b in retried.json()["beats"]}
        # The crashed attempt never committed: the retry reclaimed its expired turn lease and
        # marked it failed (#63), so a late commit of it would be refused.
        assert sorted(await turn_statuses(engine, story)) == ["committed", "failed"]
        rejected = await keyed(client, story, body(ids[PLAYER], "Different words."), "dbl-c")
        assert rejected.status_code == 409
