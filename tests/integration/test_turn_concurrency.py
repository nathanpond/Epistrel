"""Turn concurrency beyond S-TURN-CONCURRENCY (#63): the settings PATCH, the turn slot's lease
(reclaim after a crash, release after a failure), queued ingest batches, the long-poll and
``Prefer: respond-async``, failures and restarts of queued turns."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import turn_gate
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.orchestrator.models.stub import FaultKind
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.idempotency_support import KEY, REPLAY, key_row
from tests.policy_support import words
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    new_story,
    respond,
    respond_client,
)
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration

ASYNC = {"Prefer": "respond-async"}
RUN = new_ulid()[-8:]
"""Keys are unique per run: the tests share one database."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _patch(client: httpx.AsyncClient, story: str, patch: Any, **headers: str) -> Any:
    return await client.patch(
        f"/stories/{story}/settings", json=patch, headers={**WRITE, **headers}
    )


def _error(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _story_row(engine: AsyncEngine, story: str) -> Any:
    (row,) = await rows(engine, "SELECT * FROM stories WHERE id = :s", s=story)
    return row


async def _operation(engine: AsyncEngine, operation_id: str) -> Any:
    (row,) = await rows(engine, "SELECT * FROM operations WHERE operation_id = :o", o=operation_id)
    return row


async def _queue(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine, "SELECT * FROM turn_queue WHERE story_id = :s ORDER BY position", s=story
    )


async def _held(stub: Any, client: httpx.AsyncClient, story: str, actor: str) -> Any:
    latch = stub.hold("generation")
    pending = asyncio.ensure_future(respond(client, story, body(actor, "Hold this.")))
    await asyncio.wait_for(latch.entered.wait(), 5)
    return latch, pending


async def test_settings_patch(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A JSON merge patch: set, nested merge, ``null`` resets, unknown keys and bad values are
    422, the default viewpoint must be a cast member (never ``omniscient``: #59's rule on the
    real route), every change is a prospective story-scoped ``config_changes`` row and one
    ``config_revision``; a no-op writes nothing; an archived story's settings are read-only;
    a relaxing ``policies`` item refuses the whole request. Complement: a valid default is
    stored as ``character:<id>`` and the GET shows every change."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        before = await _story_row(engine, story)

        changed = await _patch(
            client,
            story,
            {
                "turn_concurrency": "queue",
                "turn_queue_max": 3,
                "turn_deadline": 20,
                "generation_settings": {"temperature": 0.3},
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.headers.get(KEY)
        answer = changed.json()
        assert answer["config_revision"] == before.config_revision + 1
        assert answer["timeline_revision"] == before.timeline_revision
        assert answer["settings"]["turn_concurrency"] == "queue"
        assert answer["settings"]["turn_queue_max"] == 3
        assert answer["settings"]["turn_deadline"] == 20
        assert {"effective_policies", "inactive_policies", "guardrail_calls"} <= set(answer)
        row = await _story_row(engine, story)
        assert (row.turn_concurrency, row.turn_queue_max) == ("queue", 3)
        assert "turn_concurrency" not in row.settings  # lives in its column
        recorded = await rows(
            engine,
            'SELECT setting, "class", actor, config_revision FROM config_changes '
            "WHERE scope = 'story' AND scope_id = :s ORDER BY setting",
            s=story,
        )
        assert [r.setting for r in recorded] == [  # one row per changed leaf (#64)
            "generation_settings.temperature",
            "turn_concurrency",
            "turn_deadline",
            "turn_queue_max",
        ]
        assert {(r._mapping["class"], r.actor, r.config_revision) for r in recorded} == {
            ("prospective", "p-author", answer["config_revision"])
        }

        merged = await _patch(client, story, {"generation_settings": {"top_p": 0.5}})
        assert merged.json()["settings"]["generation_settings"] == {
            "temperature": 0.3,
            "top_p": 0.5,
            "length": None,
        }
        reset = await _patch(client, story, {"turn_concurrency": None, "turn_queue_max": None})
        settings = reset.json()["settings"]
        assert (settings["turn_concurrency"], settings["turn_queue_max"]) == ("reject", None)
        revision = reset.json()["config_revision"]
        same = await _patch(client, story, {"turn_concurrency": "reject"})
        assert same.json()["config_revision"] == revision  # a no-op writes nothing

        # #59: the default viewpoint is a cast member, never ``omniscient``.
        assert _error(await _patch(client, story, {"default_output_viewpoint": "omniscient"})) == (
            422,
            "validation_error",
        )
        stranger = await _patch(client, story, {"default_output_viewpoint": "character:01ABC"})
        assert _error(stranger) == (422, "viewpoint_unresolved")
        assert stranger.json()["error"]["details"]["field"] == "body.default_output_viewpoint"
        member = f"character:{ids[FIRST]}"
        valid = await _patch(client, story, {"default_output_viewpoint": member})
        assert valid.json()["settings"]["default_output_viewpoint"] == member
        read = await client.get(f"/stories/{story}/settings", headers=READ)
        assert read.json()["settings"]["default_output_viewpoint"] == member

        for bad, field in (
            ({"turn_concurrency": "sometimes"}, "body.turn_concurrency"),
            ({"turn_queue_max": 51}, "body.turn_queue_max"),
            ({"not_a_setting": 1}, "body.not_a_setting"),
        ):
            refused = await _patch(client, story, bad)
            assert _error(refused) == (422, "validation_error"), bad
            assert refused.json()["error"]["details"]["field"] == field
        relaxing = {"name": "Let it be", "rule": words("x"), "message": "-", "effect": "allow"}
        policies = await _patch(
            client, story, {"turn_concurrency": "queue", "policies": [relaxing]}
        )
        assert _error(policies) == (422, "cannot_relax")
        after = await _story_row(engine, story)
        assert after.turn_concurrency == "reject"
        assert after.config_revision == valid.json()["config_revision"]
        own = {"name": "Mine", "rule": words("y"), "message": "No."}
        upsert = await _patch(client, story, {"policies": [own]})
        assert upsert.status_code == 200, upsert.text
        assert "Mine" in {p["name"] for p in upsert.json()["effective_policies"]}

        replay = await _patch(client, story, {"lite_window_messages": 12}, **{KEY: f"set-1-{RUN}"})
        again = await _patch(client, story, {"lite_window_messages": 12}, **{KEY: f"set-1-{RUN}"})
        assert (again.headers.get(REPLAY), again.json()) == ("true", replay.json())

        await client.post(f"/stories/{story}/archive", headers=WRITE)
        archived = await _patch(client, story, {"turn_concurrency": "queue"})
        assert _error(archived) == (409, "story_archived")


async def test_lease_reclaim_after_a_crash(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A turn whose engine died holds the slot only until its lease runs out: the next turn
    reclaims it, marks the dead turn ``failed`` (``turn_interrupted``) and plays. Complement:
    within the lease the next turn is 423 with the holder and ``Retry-After``."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        dead = Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p-dead")
        await dead.begin()
        assert await turn_gate.acquire(engine, story, dead.operation_id, lease_s=60) is None

        busy = await respond(client, story, body(ids[PLAYER], "Anyone?"))
        assert _error(busy) == (423, "story_busy")
        assert busy.json()["error"]["details"]["operation_id"] == dead.operation_id
        assert 50 <= int(busy.headers["Retry-After"]) <= 60

        await _execute(
            engine,
            "UPDATE stories SET in_flight_lease_until = clock_timestamp() - interval '1 second' "
            "WHERE id = :s",
            s=story,
        )
        played = await respond(client, story, body(ids[PLAYER], "Anyone?"))
        assert played.status_code == 200, played.text
        orphan = await _operation(engine, dead.operation_id)
        assert (orphan.status, orphan.error_code) == ("failed", "turn_interrupted")
        assert (await _story_row(engine, story)).in_flight_turn is None
        with pytest.raises(RuntimeError):  # a late commit of the dead turn is refused
            await dead.commit()


async def test_a_failed_turn_frees_the_slot(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#53's hand-off: a 503 releases the turn slot and leaves the key re-executable.
    Complement: the next request, and the same key's retry, play at once."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(client)
        epistrel.models.stub.fault("generation", "http_500")
        request = body(ids[PLAYER], "Hello?")
        failed = await respond(client, story, request, headers={KEY: f"fail-1-{RUN}"})
        assert _error(failed) == (503, "model_unavailable")
        assert (await _story_row(engine, story)).in_flight_turn is None
        retried = await respond(client, story, request, headers={KEY: f"fail-1-{RUN}"})
        assert retried.status_code == 200, retried.text
        assert (await _story_row(engine, story)).in_flight_turn is None


async def test_queued_ingest_is_one_turn(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A batch that finds a ``queue`` story busy is one ``turn_queue`` row; with
    ``respond-async`` it is 202 ``{operation_id, position}`` + ``Location`` at once, a retry
    under its key is 202 for the same turn, the operation reads ``queued``. Complement: after
    the running turn, the batch commits as the next turns and the key replays its result."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(client, settings={"turn_concurrency": "queue"})
        latch, first = await _held(epistrel.models.stub, client, story, ids[PLAYER])
        events = {
            "events": [
                {"client_event_id": "q-1", "kind": "narration", "text": "Thunder."},
                {
                    "client_event_id": "q-2",
                    "kind": "player_turn",
                    "actor_character_id": ids[FIRST],
                    "say": "Did you hear that?",
                },
            ]
        }
        headers = {**WRITE, **ASYNC, KEY: f"batch-1-{RUN}"}
        accepted = await client.post(f"/stories/{story}/ingest", json=events, headers=headers)
        assert accepted.status_code == 202, accepted.text
        operation_id = accepted.json()["operation_id"]
        assert accepted.json() == {"operation_id": operation_id, "position": 1}
        assert accepted.headers["Location"] == f"/operations/{operation_id}"
        assert accepted.headers[KEY] == f"batch-1-{RUN}"
        (queued,) = await _queue(engine, story)
        assert (queued.kind, queued.operation_id) == ("ingest", operation_id)
        row = await _operation(engine, operation_id)
        assert (row.status, row.origin) == ("queued", "ingested")
        read = await client.get(f"/operations/{operation_id}", headers=READ)
        assert read.json()["status"] == "queued"
        duplicate = await client.post(f"/stories/{story}/ingest", json=events, headers=headers)
        assert (duplicate.status_code, duplicate.json()["operation_id"]) == (202, operation_id)

        latch.release()
        assert (await first).status_code == 200
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        assert await _queue(engine, story) == []
        assert (await _operation(engine, operation_id)).status == "committed"
        replay = await client.post(
            f"/stories/{story}/ingest", json=events, headers={**WRITE, KEY: f"batch-1-{RUN}"}
        )
        assert (replay.status_code, replay.headers.get(REPLAY)) == (201, "true")
        results = replay.json()["results"]
        assert [r["turn"] for r in results] == [2, 3]
        assert results[0]["operation_id"] == operation_id
        assert (await key_row(engine, f"batch-1-{RUN}")).operation_id == operation_id


async def test_long_poll_then_202(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A queued request waits up to the story's ``turn_deadline`` and is then answered 202 with
    the turn still queued; its key later replays the turn's result. Complement: on an idle
    ``queue`` story, ``respond-async`` plays at once (200, no queue row)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(
            client, settings={"turn_concurrency": "queue", "turn_deadline": 0.5}
        )
        idle = await respond(client, story, body(ids[PLAYER], "Quick."), headers=ASYNC)
        assert idle.status_code == 200, idle.text
        assert await _queue(engine, story) == []
        # Another process's turn holds the slot (a long lease; no model call to time out).
        holder = "01" + "H" * 24
        assert await turn_gate.acquire(engine, story, holder, lease_s=60) is None
        request = body(ids[FIRST], "I'll wait.")
        waited = await respond(client, story, request, headers={KEY: f"poll-1-{RUN}"})
        assert waited.status_code == 202, waited.text
        assert waited.json()["position"] == 1
        assert len(await _queue(engine, story)) == 1
        await turn_gate.release_now(engine, story, holder)
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        later = await respond(client, story, request, headers={KEY: f"poll-1-{RUN}"})
        assert (later.status_code, later.headers.get(REPLAY)) == (200, "true")
        assert later.json()["turn"] == 2


async def test_queued_failure_reaches_the_waiter(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A queued turn that fails answers its waiting request with its error, ends ``failed``
    with the error recorded, and leaves the queue and the slot. Complement: the next turn
    plays as the turn after the first."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await new_story(client, settings={"turn_concurrency": "queue"})
        calls = 0

        def second_fails(purpose: str) -> FaultKind | None:
            nonlocal calls
            if purpose != "generation":
                return None
            calls += 1
            return "http_500" if calls == 2 else None

        latch, first = await _held(stub, client, story, ids[PLAYER])
        stub.fault_hook = second_fails
        try:
            waiting = asyncio.ensure_future(respond(client, story, body(ids[FIRST], "Go on.")))
            await asyncio.sleep(0.2)
            latch.release()
            assert (await first).status_code == 200
            failed = await waiting
        finally:
            stub.fault_hook = None
        assert _error(failed) == (503, "model_unavailable")
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        statuses = await rows(
            engine,
            "SELECT status, error_code FROM operations WHERE story_id = :s AND kind = 'turn' "
            "ORDER BY started_at",
            s=story,
        )
        assert [(r.status, r.error_code) for r in statuses] == [
            ("committed", None),
            ("failed", "model_unavailable"),
        ]
        assert await _queue(engine, story) == []
        assert (await _story_row(engine, story)).in_flight_turn is None
        nxt = await respond(client, story, body(ids[FIRST], "Again."))
        assert (nxt.status_code, nxt.json()["turn"]) == (200, 2)


async def test_restart_drains_the_queue(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A queue left by a stopped process is drained by the start-up recovery pass; a queued
    turn whose run was interrupted (lease expired) runs again, at most ``MAX_RUNS`` times.
    Complement: past the cap the turn fails ``turn_interrupted`` and never commits."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(client, settings={"turn_concurrency": "queue"})

        async def queue_one(key: str) -> str:
            latch, first = await _held(epistrel.models.stub, client, story, ids[PLAYER])
            accepted = await respond(
                client, story, body(ids[FIRST], f"Later {key}."), headers={**ASYNC, KEY: key}
            )
            assert accepted.status_code == 202, accepted.text
            await epistrel.drainer.stop()  # the process "stops" with the turn still queued
            latch.release()
            assert (await first).status_code == 200
            operation_id: str = accepted.json()["operation_id"]
            return operation_id

        def interrupted(attempts: int) -> Callable[[str], Any]:
            async def mark(operation_id: str) -> None:
                # The drainer died mid-run: the turn holds the slot, in flight, lease expired.
                await _execute(
                    engine,
                    "UPDATE operations SET status = 'in_flight' WHERE operation_id = :o",
                    o=operation_id,
                )
                await _execute(
                    engine,
                    "UPDATE turn_queue SET attempts = :a WHERE operation_id = :o",
                    a=attempts,
                    o=operation_id,
                )
                await _execute(
                    engine,
                    "UPDATE stories SET in_flight_turn = :o, "
                    "in_flight_lease_until = clock_timestamp() - interval '1 second' "
                    "WHERE id = :s",
                    o=operation_id,
                    s=story,
                )

            return mark

        plain = await queue_one(f"restart-1-{RUN}")
        await epistrel.start_workers()
        await asyncio.sleep(0.2)
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        assert (await _operation(engine, plain)).status == "committed"

        rerun = await queue_one(f"restart-2-{RUN}")
        await interrupted(attempts=0)(rerun)
        await epistrel.start_workers()
        await asyncio.sleep(0.2)
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        assert (await _operation(engine, rerun)).status == "committed"

        capped = await queue_one(f"restart-3-{RUN}")
        await interrupted(attempts=turn_gate.MAX_RUNS - 1)(capped)
        await epistrel.start_workers()
        await asyncio.sleep(0.2)
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        lost = await _operation(engine, capped)
        assert (lost.status, lost.error_code) == ("failed", "turn_interrupted")
        assert await _queue(engine, story) == []
        turns = await rows(
            engine,
            "SELECT narrative_turn FROM operations WHERE story_id = :s AND status = 'committed' "
            "AND kind = 'turn' ORDER BY narrative_turn",
            s=story,
        )
        assert [t.narrative_turn for t in turns] == [1, 2, 3, 4, 5]
        answer = await respond(
            client,
            story,
            body(ids[FIRST], f"Later restart-3-{RUN}."),
            headers={KEY: f"restart-3-{RUN}"},
        )
        assert answer.status_code == 200, answer.text  # the failed key is free again
