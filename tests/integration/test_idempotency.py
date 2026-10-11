"""Idempotent mutations (#61): the key rule, server keys, failures, waiting, dead originals,
admin and delete replays, the operations read, the result-window setting, and the library."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine
from epistrel.config import Settings
from epistrel.core import clock, idempotency
from epistrel.core.context import RequestContext
from epistrel.core.deployment import read_idempotency_settings, update_idempotency_settings
from epistrel.core.enums import OperationKind
from epistrel.core.errors import IdempotencyConflict, InvalidRequest
from epistrel.core.events import Operation
from epistrel.core.idempotency import (
    IdempotentCall,
    ReferenceReplay,
    Replayed,
    ResultCodec,
    Scope,
)
from epistrel.core.ids import new_ulid
from epistrel.core.ingest import IngestRequest
from epistrel.core.tables import idempotency_keys
from epistrel.orchestrator.jobs.handlers.retention import purge_idempotency
from epistrel.orchestrator.lite.schemas import RespondRequest
from tests.auth_support import auth_settings
from tests.event_support import new_story as new_event_story
from tests.event_support import play_turn, rows
from tests.idempotency_support import KEY, REPLAY, committed_turns, key_row, keyed
from tests.policy_support import unique
from tests.respond_support import PLAYER, body, new_story, respond_client
from tests.story_support import READ, WRITE, count, create, story_body

AUTHOR = RequestContext(principal="p-author")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _error(response: Any) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


async def test_server_key_is_generated_echoed_and_replayable(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Without a key the server mints a ULID, echoes it in the header and the body and stores
    the result under it; replaying with that key answers the identical body. A read gets no
    key."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ids = await new_story(client)
        archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
        key = archived.headers[KEY]
        assert len(key) == 26
        assert archived.json()["idempotency_key"] == key
        stored = await key_row(engine, key)
        assert (stored.scope, stored.scope_id, stored.status) == ("story", story, "done")
        assert stored.result["status"] == 200
        again = await client.post(f"/stories/{story}/archive", headers={**WRITE, KEY: key})
        assert (again.headers.get(REPLAY), again.content) == ("true", archived.content)
        read = await client.get(f"/stories/{story}", headers=READ)
        assert KEY not in read.headers
        assert "idempotency_key" not in read.json()


async def test_malformed_keys_are_refused(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A key outside 1..255 of ``[A-Za-z0-9._:-]`` is 422 at ``headers.Idempotency-Key`` on
    REST and the library; nothing is written."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(client)
        for bad in ("has space", "x" * 256, "semi;colon", "slash/key"):
            refused = await keyed(client, story, body(ids[PLAYER]), bad)
            assert refused.status_code == 422, bad
            assert _error(refused)["details"]["field"] == "headers.Idempotency-Key"
        assert await committed_turns(engine, story) == []
        with pytest.raises(InvalidRequest):
            await epistrel.archive_story(
                RequestContext(principal="p", idempotency_key="no good"), story
            )
        assert (await keyed(client, story, body(ids[PLAYER]), "a.B-9_:z" * 31)).status_code == 200


async def test_failures_are_not_stored(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A refused request frees its key: the same request then runs. The expected revision is
    part of the request, so the stale request under a used key is a conflict afterwards."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        stale = {KEY: "fail-1", "X-Epistrel-Expected-Revision": "7"}
        refused = await client.post(
            f"/stories/{story}/respond", json=body(ids[PLAYER]), headers={**WRITE, **stale}
        )
        assert _error(refused)["code"] == "revision_conflict"
        assert await rows(engine, "SELECT 1 FROM idempotency_keys WHERE key = 'fail-1'") == []
        ran = await keyed(client, story, body(ids[PLAYER]), "fail-1")
        assert ran.status_code == 200, ran.text
        again = await client.post(
            f"/stories/{story}/respond", json=body(ids[PLAYER]), headers={**WRITE, **stale}
        )
        assert _error(again)["code"] == "idempotency_conflict"


async def test_a_duplicate_waits_then_times_out(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate waits for a running original; past its wait it is 503
    ``original_in_flight_timeout`` naming the original's operation, and nothing ran twice."""
    monkeypatch.setattr("epistrel.engine.IDEMPOTENCY_WAIT_S", 0.3)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await new_story(client)
        latch = epistrel.models.stub.hold("generation")
        original = asyncio.create_task(keyed(client, story, body(ids[PLAYER]), "wait-1"))
        await latch.entered.wait()
        timed_out = await keyed(client, story, body(ids[PLAYER]), "wait-1")
        assert timed_out.status_code == 503
        error = _error(timed_out)
        assert error["code"] == "original_in_flight_timeout"
        (running,) = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND status = 'in_flight'",
            s=story,
        )
        assert error["details"]["operation_id"] == running.operation_id
        latch.release()
        assert (await original).status_code == 200
        assert len(await committed_turns(engine, story)) == 1


@pytest.fixture
async def story_id(stub_settings: Settings, engine: AsyncEngine) -> str:
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ids = await new_story(client)
    return story


def _call(engine: AsyncEngine, story: str, key: str) -> IdempotentCall:
    return IdempotentCall(
        engine,
        scope=Scope.STORY,
        scope_id=story,
        key=key,
        operation="test",
        principal="p-test",
        codec=ResultCodec.of(dict[str, int]),
        wait_s=10.0,
    )


async def _control(engine: AsyncEngine, story: str) -> Operation:
    async with Operation(
        engine, story_id=story, kind=OperationKind.CONTROL, principal="p-test"
    ) as op:
        await op.commit()
    return op


async def test_dead_originals(
    engine: AsyncEngine, story_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim whose lease lapsed with nothing committed is taken over, and the late original's
    commit is then refused (``superseded``); one whose operation committed answers every retry
    with the reference, never a second run; a failure after the commit leaves the same
    tombstone."""
    monkeypatch.setattr(idempotency, "LEASE_S", 0.3)
    material = {"n": 1}

    dead = _call(engine, story_id, "dead-1")
    await dead.claim(material)
    await dead._stop_renewal()  # the original's process died
    heir = _call(engine, story_id, "dead-1")
    await heir.claim(material)
    assert heir.claimed
    with idempotency.calling(dead), pytest.raises(IdempotencyConflict) as late:
        await _control(engine, story_id)
    assert late.value.details == {"reason": "superseded"}
    with idempotency.calling(heir):
        op = await _control(engine, story_id)
    await heir.finish({"done": 1})
    assert (await key_row(engine, "dead-1")).operation_id == op.operation_id
    with pytest.raises(Replayed):
        await _call(engine, story_id, "dead-1").claim(material)

    committed = _call(engine, story_id, "dead-2")
    await committed.claim(material)
    with idempotency.calling(committed):
        bound = await _control(engine, story_id)
    await committed._stop_renewal()  # died after the commit, before storing the result
    await asyncio.sleep(0.35)
    for _ in range(2):
        with pytest.raises(ReferenceReplay) as reference:
            await _call(engine, story_id, "dead-2").claim(material)
        assert reference.value.operation_id == bound.operation_id
    assert (await key_row(engine, "dead-2")).status == "done"

    failing = _call(engine, story_id, "dead-3")
    await failing.claim(material)
    with idempotency.calling(failing):
        await _control(engine, story_id)
    await failing.release()
    with pytest.raises(ReferenceReplay):
        await _call(engine, story_id, "dead-3").claim(material)
    with pytest.raises(IdempotencyConflict):
        await _call(engine, story_id, "dead-3").claim({"n": 2})


async def test_admin_writes_replay_and_record_an_operation(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A keyed admin create replays its 201 with the identical body and creates one rating;
    the write is recorded as a story-less ``control`` operation whose stored result the
    operations read returns. A keyed delete replays its 204."""
    async with respond_client(stub_settings, engine) as (client, _):
        name = unique("Keyed")
        headers = {**WRITE, KEY: f"rating-{name}"}
        created = await client.post("/admin/content-ratings", json={"name": name}, headers=headers)
        assert created.status_code == 201, created.text
        again = await client.post("/admin/content-ratings", json={"name": name}, headers=headers)
        assert (again.status_code, again.headers.get(REPLAY)) == (201, "true")
        assert again.content == created.content
        assert (
            await count(engine, "SELECT count(*) FROM content_ratings WHERE name = :n", n=name) == 1
        )
        stored = await key_row(engine, f"rating-{name}")
        (op,) = await rows(
            engine,
            "SELECT story_id, kind, status, idempotency_key FROM operations "
            "WHERE operation_id = :o",
            o=stored.operation_id,
        )
        assert tuple(op) == (None, "control", "committed", f"rating-{name}")
        read = (await client.get(f"/operations/{stored.operation_id}", headers=READ)).json()
        assert read["result"]["status"] == 201
        assert read["result"]["body"]["rating"]["name"] == name
        retired = await client.delete(
            f"/admin/content-ratings/{created.json()['id']}", headers=WRITE
        )
        assert retired.status_code == 200

        victim = (await create(client, story_body("Ada")))["story_id"]
        deleting = {**WRITE, KEY: f"delete-{victim}"}
        gone = await client.delete(f"/stories/{victim}", headers=deleting)
        assert (gone.status_code, gone.headers[KEY]) == (204, f"delete-{victim}")
        again = await client.delete(f"/stories/{victim}", headers=deleting)
        assert (again.status_code, again.headers.get(REPLAY)) == (204, "true")
        missing = await client.delete(f"/stories/{victim}", headers=WRITE)
        assert missing.status_code == 404


async def test_operations_read(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``GET /operations/{id}``: 404 for an unknown id; ``in_flight`` while a turn runs; a
    turn's result only for the viewpoint it was produced for (another named one is 409, none
    named omits it); an ingested batch lists its operations."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        unknown = await client.get(f"/operations/{'0' * 26}", headers=READ)
        assert (unknown.status_code, _error(unknown)["code"]) == (404, "not_found")
        story, ids = await new_story(client)
        latch = epistrel.models.stub.hold("generation")
        running = asyncio.create_task(keyed(client, story, body(ids[PLAYER]), "ops-1"))
        await latch.entered.wait()
        (flight,) = await rows(
            engine,
            "SELECT operation_id FROM operations WHERE story_id = :s AND status = 'in_flight'",
            s=story,
        )
        seen = (await client.get(f"/operations/{flight.operation_id}", headers=READ)).json()
        assert (seen["status"], seen["result"], seen["narrative_turn"]) == ("in_flight", None, None)
        latch.release()
        turn = (await running).json()
        own = turn["output_viewpoint"]
        operation = turn["beats"][0]["operation_id"]
        mine = (await client.get(f"/operations/{operation}?viewpoint={own}", headers=READ)).json()
        assert mine["result"]["body"]["message"] == turn["message"]
        assert (mine["kind"], mine["status"], mine["viewpoint"]) == ("turn", "committed", own)
        assert mine["timeline_revision"] == turn["timeline_revision"]
        assert mine["narrative_turn"] == turn["turn"]
        assert mine["origin"] == "generated"
        assert mine["batch_operation_ids"] is None
        assert isinstance(mine["attempts"], list)
        plain = (await client.get(f"/operations/{operation}", headers=READ)).json()
        assert plain["result"] is None
        other = await client.get(f"/operations/{operation}?viewpoint=omniscient", headers=READ)
        assert (other.status_code, _error(other)["code"]) == (409, "idempotency_conflict")

        events = [
            {"client_event_id": f"ops-n{n}", "kind": "narration", "text": f"Night {n}."}
            for n in range(3)
        ]
        batch = await client.post(
            f"/stories/{story}/ingest", json={"events": events}, headers=WRITE
        )
        assert batch.status_code == 201, batch.text
        operation_ids = [r["operation_id"] for r in batch.json()["results"]]
        last = (await client.get(f"/operations/{operation_ids[-1]}", headers=READ)).json()
        assert (last["origin"], last["batch_operation_ids"]) == ("ingested", operation_ids)
        first = (await client.get(f"/operations/{operation_ids[0]}", headers=READ)).json()
        assert first["result"]["status"] == 201
        assert first["idempotency_key"] == batch.headers[KEY]


async def test_result_window_setting_is_prospective(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``idempotency_result_window`` (hours) sets ``expires_at`` of calls that start after the
    change; a write that changes nothing writes nothing; a bad stored value falls back."""
    before = await read_idempotency_settings(engine)
    revision = await count(engine, "SELECT config_revision FROM deployment_state")
    try:
        await update_idempotency_settings(engine, principal="p-operator", result_window_hours=2)
        assert await count(engine, "SELECT config_revision FROM deployment_state") == revision + 1
        await update_idempotency_settings(engine, principal="p-operator", result_window_hours=2)
        assert await count(engine, "SELECT config_revision FROM deployment_state") == revision + 1
        async with respond_client(stub_settings, engine) as (client, _):
            story, _ids = await new_story(client)
            archived = await client.post(f"/stories/{story}/archive", headers=WRITE)
        stored = await key_row(engine, archived.headers[KEY])
        window = stored.expires_at - stored.created_at
        assert timedelta(hours=1, minutes=59) < window < timedelta(hours=2, minutes=1)
    finally:
        await update_idempotency_settings(
            engine,
            principal="p-operator",
            result_window_hours=before.idempotency_result_window,
        )
    assert idempotency.idempotency_settings({"idempotency_result_window": 0}) == (
        idempotency.idempotency_settings(None)
    )


async def test_library_replays_and_references(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The library: a keyed retry returns the stored result with ``replayed=True``; after the
    window it raises ``ReferenceReplay`` naming the original operation; a keyless call stores
    its result under a generated key."""
    lib = Engine(stub_settings, db=engine)
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
    keyed_ctx = RequestContext(principal="p-lib", idempotency_key="lib-1")
    request = RespondRequest.model_validate(body(ids[PLAYER]))
    first = await lib.respond(keyed_ctx, story, request)
    again = await lib.respond(keyed_ctx, story, request)
    assert (first.replayed, again.replayed) == (False, True)
    assert again.message == first.message
    stored = await key_row(engine, "lib-1")
    later = stored.expires_at + timedelta(minutes=1)
    with clock.frozen_at(later), pytest.raises(ReferenceReplay) as reference:
        await lib.respond(keyed_ctx, story, request)
    assert reference.value.operation_id == stored.operation_id
    assert reference.value.body()["timeline_revision"] == first.timeline_revision

    before = await count(engine, "SELECT count(*) FROM idempotency_keys")
    narration = {"client_event_id": "lib-n1", "kind": "narration", "text": "Rain."}
    ingested = await lib.ingest(
        RequestContext(principal="p-lib"),
        story,
        IngestRequest.model_validate({"events": [narration]}),
    )
    assert ingested.created
    assert not ingested.replayed
    assert await count(engine, "SELECT count(*) FROM idempotency_keys") == before + 1


async def test_idempotency_purge_settles_stale_keys(engine: AsyncEngine) -> None:
    """``idempotency_purge`` (#71) settles keys a dead call left ``in_flight`` once their lease
    lapsed more than a result window ago: committed → a ``done`` tombstone with no result; no
    operation, or one that never existed → deleted (the key is free). Complement: a key whose
    lease lapsed within the window, and a key whose operation is still queued, are untouched."""

    story = await new_event_story(engine)
    committed = await play_turn(engine, story, "kept as a tombstone")
    queued = await play_turn(engine, story, "still queued")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE operations SET status = 'queued' WHERE operation_id = :o"),
            {"o": queued.operation_id},
        )
    now = clock.now()
    long_ago, lately = now - timedelta(hours=25), now - timedelta(hours=1)
    cases = {
        "committed": (committed.operation_id, long_ago),
        "no-op": (None, long_ago),
        "ghost-op": (new_ulid(), long_ago),
        "recent": (None, lately),
        "queued": (queued.operation_id, long_ago),
    }
    run = new_ulid()
    async with engine.begin() as conn:
        for name, (operation_id, lease) in cases.items():
            await conn.execute(
                idempotency_keys.insert().values(
                    scope="story",
                    scope_id=story,
                    key=f"{run}-{name}",
                    request_hash="h",
                    operation_id=operation_id,
                    status="in_flight",
                    owner_token="dead",
                    lease_until=lease,
                )
            )
    counts = await purge_idempotency(engine)
    assert counts["keys_settled"] >= 1
    assert counts["keys_freed"] >= 2
    left = {
        r.key.removeprefix(f"{run}-"): (r.status, r.result)
        for r in await rows(
            engine,
            "SELECT key, status, result FROM idempotency_keys WHERE key LIKE :k",
            k=f"{run}-%",
        )
    }
    assert left == {
        "committed": ("done", None),
        "recent": ("in_flight", None),
        "queued": ("in_flight", None),
    }
