"""X-04: chaos — retry storms (docs/04 §12; #61).

Clients retry respond requests under their idempotency keys against a child engine process
(``epistrel.testing.faults``) while the harness drops connections (client-side cancellation at
random points) and kills the engine with ``SIGKILL`` and restarts it at random times. Every key
must end with exactly one committed turn, every answer for a key must be the same message (the
same message id), and no story may hold a second turn.

Queued turns (#63): ``QUEUE_STORIES`` stories under ``turn_concurrency: queue`` each get
``QUEUE_KEYS`` clients at once, so their turns queue behind each other while the engine is
killed and restarted (the restarted engine's recovery pass drains the queues). Each such story
ends with exactly one committed turn per key, as consecutive turns: no queued turn is lost or
run twice.

``EPISTREL_CHAOS_ITERATIONS`` (default 50, the quick form PR CI runs) is the number of
requests each client sends; ``EPISTREL_CHAOS_SEED`` replays a run (the seed is printed).
"""

from __future__ import annotations

import asyncio
import itertools
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
QUEUE_STORIES = 4
QUEUE_KEYS = 3
QUEUE_DEADLINE_S = 5
"""The queue stories' ``turn_deadline``: a waiting request is answered 202 after it."""
FINAL_ATTEMPTS = 120


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
@pytest.mark.chaos_quick
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
        queues = [
            await new_story(
                client,
                settings={"turn_concurrency": "queue", "turn_deadline": QUEUE_DEADLINE_S},
            )
            for _ in range(QUEUE_STORIES)
        ]
    requests = {
        f"x04-{SEED}-{n}": (story, body(ids[PLAYER], f"Storm {n}, are you there?"))
        for n, (story, ids) in enumerate(stories)
    }
    queued = {
        f"x04-{SEED}-q{n}-{k}": (story, body(ids[PLAYER], f"Queue {n}, move {k}."))
        for n, (story, ids) in enumerate(queues)
        for k in range(QUEUE_KEYS)
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
                for key, (story, request) in {**requests, **queued}.items()
            )
        )
        stop.set()
        await crashes
        # Every key ends answered: final, undisturbed retries (a queued turn may still be
        # waiting: 202 until it played).
        async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as final:
            for key, (story, request) in {**requests, **queued}.items():
                for _ in range(FINAL_ATTEMPTS):
                    response = await keyed(final, story, request, key)
                    if response.status_code not in (202, 423):
                        break
                    await asyncio.sleep(0.25)
                assert response.status_code == 200, f"seed={SEED} {key}: {response.text}"
                answers[key].append(message_of(response))
    finally:
        await child.kill()
    print(f"X-04 outcomes={dict(outcomes)} restarts={child.starts - 1}")

    overlapped = 0
    for n, (story, _) in enumerate(queues):
        context = f"seed={SEED} queue story {n}"
        overlapped += await _queued_while_running(engine, story)
        turns = await committed_turns(engine, story)
        assert sorted(t.narrative_turn for t in turns) == list(range(1, QUEUE_KEYS + 1)), context
        keys = [k for k, (s, _) in queued.items() if s == story]
        bound = {(await key_row(engine, k)).operation_id for k in keys}
        assert bound == {t.operation_id for t in turns}, context  # one turn per key, none twice
        for key in keys:
            assert len(set(answers[key])) == 1, f"{context} {key}: {set(answers[key])}"
        left = await rows(engine, "SELECT 1 FROM turn_queue WHERE story_id = :s", s=story)
        assert left == [], context
    for key, (story, _) in requests.items():
        context = f"seed={SEED} key={key}"
        assert len(set(answers[key])) == 1, f"{context}: divergent answers {set(answers[key])}"
        (turn,) = await committed_turns(engine, story)
        stored = await key_row(engine, key)
        assert (stored.status, stored.operation_id) == ("done", turn.operation_id), context
        assert answers[key][0][0] in await _message_ids(engine, turn.operation_id), context
    # A crashed original stays in flight, or is failed once the next turn reclaims its
    # expired turn lease (#63).
    crashed = await rows(
        engine,
        "SELECT count(*) AS n FROM operations WHERE story_id = ANY(:s) AND kind = 'turn' "
        "AND status IN ('in_flight', 'failed')",
        s=[story for story, _ in {**requests, **queued}.values()],
    )
    print(f"X-04 originals killed in flight={crashed[0].n}")
    print(f"X-04 turns queued behind a running one={overlapped}")
    assert outcomes["answered"] > 0, f"seed={SEED}"
    if ITERATIONS >= 20:
        assert outcomes["dropped"] > 0, f"seed={SEED}: no connection was dropped"
        assert child.starts > 1, f"seed={SEED}: the engine never restarted"
        assert crashed[0].n > 0, f"seed={SEED}: no crash hit a turn in flight"
        assert overlapped > 0, f"seed={SEED}: no turn was queued"


async def _message_ids(engine: AsyncEngine, operation_id: str) -> set[str]:
    found = await rows(
        engine,
        "SELECT id FROM events WHERE operation_id = :o AND type = 'message_rendered'",
        o=operation_id,
    )
    return {r.id for r in found}


async def _queued_while_running(engine: AsyncEngine, story_id: str) -> int:
    """How many committed turns were queued while the turn before them ran: a queued turn's
    operation row is written at enqueue, so it started before its predecessor committed."""
    found = await rows(
        engine,
        "SELECT started_at, committed_at FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND status = 'committed' ORDER BY narrative_turn",
        s=story_id,
    )
    return sum(1 for a, b in itertools.pairwise(found) if b.started_at < a.committed_at)
