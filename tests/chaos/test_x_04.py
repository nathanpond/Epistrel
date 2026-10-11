"""X-04: chaos — retry storms (docs/04 §12; #61).

Clients retry respond requests under their idempotency keys against a child engine process
(``epistrel.testing.faults``) while the harness drops connections (client-side cancellation at
random points) and kills the engine with ``SIGKILL`` and restarts it at random times. Every key
must end with exactly one committed turn, every answer for a key must be the same message (the
same message id), and no story may hold a second turn.

``EPISTREL_CHAOS_ITERATIONS`` (default 50, the quick form PR CI runs) is the number of
requests each of the ``KEYS`` clients sends; ``EPISTREL_CHAOS_SEED`` replays a run (the seed
is printed). Queued turns (``turn_concurrency: queue``) join this test with #63's queue.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections import defaultdict

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.testing.faults import EngineProcess
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.event_support import rows
from tests.idempotency_support import committed_turns, key_row, keyed, message_of
from tests.respond_support import PLAYER, body, new_story, respond_client

ITERATIONS = int(os.environ.get("EPISTREL_CHAOS_ITERATIONS", "50"))
SEED = int(os.environ.get("EPISTREL_CHAOS_SEED") or random.SystemRandom().randrange(2**32))
KEYS = 20
DROP_RATE = 0.3
STUB_LATENCY_S = 0.4
RESTARTS = 3
LEASE_S = 1.0


def _derived(rng: random.Random) -> random.Random:
    """A child generator seeded from ``rng``, so each task's draws replay from the seed."""
    return random.Random(rng.getrandbits(64))  # noqa: S311 - replayable, not a secret


async def _client(
    base_url: str,
    story: str,
    key: str,
    request: dict[str, object],
    rng: random.Random,
    answers: dict[str, list[tuple[str, str]]],
    outcomes: dict[str, int],
) -> None:
    """Send the request ``ITERATIONS`` times; drop some of them part-way (a client-side
    cancellation is a dropped connection); a refused connection is retried later."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        for _ in range(ITERATIONS):
            drop = rng.random() < DROP_RATE
            attempt = asyncio.create_task(keyed(client, story, request, key))
            try:
                if drop:
                    await asyncio.sleep(rng.uniform(0.0, 2 * STUB_LATENCY_S))
                    attempt.cancel()
                response = await attempt
            except asyncio.CancelledError:
                outcomes["dropped"] += 1
                continue
            except httpx.HTTPError:
                outcomes["unreachable"] += 1
                await asyncio.sleep(rng.uniform(0.05, 0.3))
                continue
            if response.status_code == 200:
                outcomes["answered"] += 1
                answers[key].append(message_of(response))
            else:
                outcomes[f"status_{response.status_code}"] += 1
                await asyncio.sleep(rng.uniform(0.05, 0.3))


async def _crashes(child: EngineProcess, rng: random.Random, stop: asyncio.Event) -> None:
    """Kill the engine and start it again at random times; the first crash comes while the
    first turns are still generating."""
    for crash in range(RESTARTS):
        delay = rng.uniform(0.15, 0.4) if crash == 0 else rng.uniform(0.2, 1.2)
        try:
            await asyncio.wait_for(stop.wait(), delay)
            return
        except TimeoutError:
            pass
        await child.restart()


@pytest.mark.chaos
@pytest.mark.integration
@pytest.mark.part("M2", "NFR-REL-4; FR-CONC-6")
async def test_x_04(pg_url: str, engine: AsyncEngine) -> None:
    """Retry storms under drops and crashes: one turn per key, one message per key.

    Complements asserted: the run produced dropped connections, engine restarts that killed
    turns in flight, and answered retries (the chaos was real), and every key's stored
    operation is its story's one turn."""
    print(f"X-04 seed={SEED} iterations={ITERATIONS} keys={KEYS}")
    rng = random.Random(SEED)  # noqa: S311 - a replayable seed, not a secret
    settings: Settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):
        stories = [await new_story(client) for _ in range(KEYS)]
    requests = {
        f"x04-{SEED}-{n}": (story, body(ids[PLAYER], f"Storm {n}, are you there?"))
        for n, (story, ids) in enumerate(stories)
    }
    answers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    outcomes: dict[str, int] = defaultdict(int)
    child = EngineProcess(pg_url, CREDENTIAL_A, lease_s=LEASE_S, stub_latency_s=STUB_LATENCY_S)
    await child.start()
    stop = asyncio.Event()
    try:
        crashes = asyncio.create_task(_crashes(child, _derived(rng), stop))
        await asyncio.gather(
            *(
                _client(
                    child.base_url,
                    story,
                    key,
                    request,
                    _derived(rng),
                    answers,
                    outcomes,
                )
                for key, (story, request) in requests.items()
            )
        )
        stop.set()
        await crashes
        # Every key ends answered: one final, undisturbed retry each.
        async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as final:
            for key, (story, request) in requests.items():
                response = await keyed(final, story, request, key)
                assert response.status_code == 200, f"seed={SEED} {key}: {response.text}"
                answers[key].append(message_of(response))
    finally:
        await child.kill()
    print(f"X-04 outcomes={dict(outcomes)} restarts={child.starts - 1}")

    for key, (story, _) in requests.items():
        context = f"seed={SEED} key={key}"
        assert len(set(answers[key])) == 1, f"{context}: divergent answers {set(answers[key])}"
        (turn,) = await committed_turns(engine, story)
        stored = await key_row(engine, key)
        assert (stored.status, stored.operation_id) == ("done", turn.operation_id), context
        assert answers[key][0][0] in await _message_ids(engine, turn.operation_id), context
    crashed = await rows(
        engine,
        "SELECT count(*) AS n FROM operations WHERE story_id = ANY(:s) AND kind = 'turn' "
        "AND status = 'in_flight'",
        s=[story for story, _ in requests.values()],
    )
    print(f"X-04 originals killed in flight={crashed[0].n}")
    assert outcomes["answered"] > 0, f"seed={SEED}"
    if ITERATIONS >= 20:
        assert outcomes["dropped"] > 0, f"seed={SEED}: no connection was dropped"
        assert child.starts > 1, f"seed={SEED}: the engine never restarted"
        assert crashed[0].n > 0, f"seed={SEED}: no crash hit a turn in flight"


async def _message_ids(engine: AsyncEngine, operation_id: str) -> set[str]:
    found = await rows(
        engine,
        "SELECT id FROM events WHERE operation_id = :o AND type = 'message_rendered'",
        o=operation_id,
    )
    return {r.id for r in found}
