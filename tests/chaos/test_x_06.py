"""X-06: chaos — database disruption (docs/04 §12; NFR-REL-2,3; #75).

A dedicated PostgreSQL container (bound to a fixed host port, so a restarted server is at the
same address) serves a child engine (``EngineProcess``, ``api`` mode, the stub adapter) and
nothing else. Between requests the harness kills the server (``docker kill``: no clean
shutdown) and starts it again. While it is down, every request is answered 503
``database_unavailable``: nothing is written and the engine itself stays up. Once it is back,
the engine answers again within ``RECOVERY_S`` without being restarted (its pool reconnects).
Every request refused during the outage is retried under its key and answered. Afterwards
nothing is in flight, no key is left in flight, and the story satisfies the crash predicate
(``tests.crash_support``): dense ``seq``, every event's operation committed, consistent
counters, and projections equal to a rebuild.

``EPISTREL_CHAOS_DISRUPTIONS`` is the number of outages: 1 in PR CI, 3 in the weekly run.
The test needs Docker. With ``EPISTREL_TEST_DATABASE_URL`` set it fails rather than skips:
it cannot kill a server it does not own.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
import psycopg
import pytest
from alembic import command

from epistrel.core.db import make_engine
from epistrel.testing.faults import EngineProcess, free_port
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.conftest import OVERRIDE_VAR, PG_IMAGE, alembic_config
from tests.crash_support import in_flight_keys, integrity, recover
from tests.idempotency_support import KEY
from tests.respond_support import FIRST, PLAYER, SECOND, body
from tests.story_support import READ, WRITE, story_body

DISRUPTIONS = int(os.environ.get("EPISTREL_CHAOS_DISRUPTIONS", "1"))
RECOVERY_S = 20.0
"""How long after the server is back the engine may take to answer again."""
TURNS_BETWEEN = 3
LEASE_S = 1.0


@contextmanager
def dedicated_postgres() -> Iterator[tuple[Any, str]]:
    """A PostgreSQL container of its own on a fixed host port, migrated to head."""
    if os.environ.get(OVERRIDE_VAR):
        pytest.fail(
            f"X-06 kills and restarts its own PostgreSQL container; it needs Docker and cannot "
            f"run against {OVERRIDE_VAR}.",
            pytrace=False,
        )
    from testcontainers.community.postgres import PostgresContainer

    port = free_port()
    container = PostgresContainer(PG_IMAGE, driver="psycopg").with_bind_ports(5432, port)
    container.start()
    try:
        url = container.get_connection_url()
        command.upgrade(alembic_config(url), "head")
        yield container, url
    finally:
        container.stop()


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


async def _server_ready(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(_libpq(url), connect_timeout=2)
            await conn.close()
            return
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.25)


async def _post(client: httpx.AsyncClient, path: str, payload: Any, key: str) -> httpx.Response:
    return await client.post(path, json=payload, headers={**WRITE, KEY: key})


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.integration
@pytest.mark.part("M2", "NFR-REL-2,3")
async def test_x_06() -> None:
    """Database outages answer 503 ``database_unavailable`` and recover without a restart.

    Complements asserted: requests during each outage really failed (503s were seen), the
    engine process stayed up through every outage, the refused requests were retried and
    committed exactly once, and the story kept growing between outages."""
    with dedicated_postgres() as (container, url):
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        child = EngineProcess(url, CREDENTIAL_A, lease_s=LEASE_S, mode="api")
        refused = 0
        try:
            await child.start()
            async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
                created = await _post(client, "/stories", story_body(PLAYER, FIRST, SECOND), "x06")
                assert created.status_code == 201, created.text
                story = created.json()["story_id"]
                player = next(c["id"] for c in created.json()["characters"] if c["name"] == PLAYER)
                path = f"/stories/{story}"
                head = 0
                for disruption in range(DISRUPTIONS):
                    for n in range(TURNS_BETWEEN):
                        key = f"x06-{disruption}-{n}"
                        played = await _post(client, f"{path}/respond", body(player, key), key)
                        assert played.status_code == 200, played.text
                        head += 1
                    container.get_wrapped_container().kill()
                    pending = [
                        (
                            f"{path}/respond",
                            body(player, f"x06-out-{disruption}"),
                            f"x06-out-{disruption}",
                        ),
                        (f"{path}/truncate", {"to_turn": head - 1}, f"x06-cut-{disruption}"),
                    ]
                    for route, payload, key in pending:
                        answer = await _post(client, route, payload, key)
                        assert answer.status_code == 503, answer.text
                        assert answer.json()["error"]["code"] == "database_unavailable"
                        refused += 1
                    read = await client.get(path, headers=READ)
                    assert read.status_code == 503, read.text
                    assert child.exited is None, "the engine died with its database"
                    container.get_wrapped_container().start()
                    await _server_ready(url)
                    await _answers_again(client, path)
                    for route, payload, key in pending:
                        answer = await _post(client, route, payload, key)
                        assert answer.status_code == 200, answer.text
                    head -= 1  # the respond added a turn; the truncation kept head - 1
            await recover(db)
            assert await in_flight_keys(db) == []
            assert await integrity(db, [story]) == []
            assert await _committed(db, story, "turn") == DISRUPTIONS * (TURNS_BETWEEN + 1)
        finally:
            await child.kill()
            await db.dispose()
    assert refused == 2 * DISRUPTIONS


async def _answers_again(client: httpx.AsyncClient, path: str) -> None:
    """The engine answers a read within ``RECOVERY_S`` of the server being back."""
    deadline = time.monotonic() + RECOVERY_S
    while True:
        read = await client.get(path, headers=READ)
        if read.status_code == 200:
            return
        assert read.status_code == 503, read.text
        assert time.monotonic() < deadline, f"no recovery within {RECOVERY_S} s: {read.text}"
        await asyncio.sleep(0.25)


async def _committed(db: Any, story: str, kind: str) -> int:
    from tests.story_support import count

    return await count(
        db,
        "SELECT count(*) FROM operations WHERE story_id = :s AND kind = :k "
        "AND status = 'committed'",
        s=story,
        k=kind,
    )
