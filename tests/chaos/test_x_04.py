"""X-04: chaos — retry storms (docs/04 §12; #61).

Clients retry respond requests under their idempotency keys against a child engine process
(``epistrel.testing.faults``) while the harness drops connections (client-side cancellation at
random points) and kills the engine with ``SIGKILL`` and restarts it. Every key must end with
exactly one committed turn, every answer for a key must be the same message (the same message
id), and no story may hold a second turn.

Queued turns (#63): ``QUEUE_STORIES`` stories under ``turn_concurrency: queue`` each get
``QUEUE_KEYS`` clients at once, so their turns queue behind each other while the engine is
killed and restarted (the restarted engine's recovery pass drains the queues). Each such story
ends with exactly one committed turn per key, as consecutive turns: no queued turn is lost or
run twice.

What triggers the first kill is what the harness observes, not the wall clock (#305), so a run
exercises its scenario however fast the runner is. The engine's first life holds every turn just
before its commit (``pause_before_commit``). The harness kills it once three things are true: a
turn has reached that point, a queue story has two turns waiting behind its running one, and a
client has dropped a request. One client, drawn from the seed, always drops its first request,
so the last condition is always met. A held turn cannot commit before the kill. So every run
drops a connection, restarts the engine, kills a turn in flight (that turn never commits; the
retry of its key executes the request again), and commits a turn that was queued behind a
running one. The two later restarts come at random times. Every wait is bounded.

``EPISTREL_CHAOS_ITERATIONS`` (default 50, the quick form PR CI runs) is the number of
requests each client sends. A connection the engine refuses while it is down sends nothing,
so it does not count: the client sends that request again. The number of requests that reach
the engine therefore does not depend on how long the restarts take.

``EPISTREL_CHAOS_SEED`` (printed) fixes every random draw: which of each client's requests
drop and after how long, its backoff delays in order, the client that drops its first
request, and how long after the engine is back each later restart comes. It does not replay
the run itself. The engine is a separate process that races the clients on the wall clock,
so between runs of one seed the interleaving can differ, for example: when the first kill
comes and which turn it catches, which turns a later restart catches, and whether a request
was answered before its drop.

``test_x_04__slow_engine`` runs a smaller storm against an engine whose turns wait
``SLOW_START_S`` before they begin, longer than any restart's delay. This is a slow runner,
where a restart at a blind time lands on no turn in flight (#305).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import random
from collections import defaultdict
from dataclasses import dataclass

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.testing.faults import BEFORE_COMMIT, MARKER_TIMEOUT_S, EngineProcess
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
RESTART_AFTER_S = (0.2, 1.2)
"""The later restarts come this long (uniform) after the engine is back."""
LEASE_S = 1.0
QUEUE_STORIES = 4
QUEUE_KEYS = 3
QUEUE_DEADLINE_S = 5
"""The queue stories' turn deadline (their override of ``budgets.deadline_ms``, #113): a waiting
request is answered 202 after it."""
FINAL_ATTEMPTS = 120
REFUSED_S = 60.0
"""A client refused this long without a break fails: the engine never came back (#305)."""
DROPPED, LOST, REFUSED = "dropped", "lost", "refused"
"""What became of a request that got no response (``_send``)."""
OBSERVE_S = 30.0
"""How long the first kill waits, after a turn reached its commit, for a queue story's waiting
turns and a dropped request (#305)."""
POLL_S = 0.05
SLOW_START_S = 2.0
"""``test_x_04__slow_engine``: every turn waits this long before it begins (#305). That is
longer than ``RESTART_AFTER_S``'s upper end, with slack for a request that arrives just before
the harness sees the engine back. A restart at a blind time therefore never lands on a turn in
flight, and before #305 none of the three did."""
SLOW_KEYS = 4
SLOW_QUEUE_STORIES = 2


@dataclass(frozen=True)
class Storm:
    """One storm's size, and how slow its engine is."""

    label: str
    prefix: str
    """The idempotency keys' prefix (keys are unique across the tests' stories)."""
    keys: int
    queue_stories: int
    turn_start_delay_s: float = 0.0


@dataclass(frozen=True)
class Draws:
    """One client's seeded draws, in two streams. ``drops`` decides each request's drop and its
    delay, so the n-th request's draws are the same however the run goes. ``waits`` gives the
    backoff delays, in order."""

    drops: random.Random
    waits: random.Random
    drops_first: bool
    """This client always drops its first request (one client per run, drawn from the seed)."""


def _derived(rng: random.Random) -> random.Random:
    """A child generator seeded from ``rng``, so each task's draws replay from the seed."""
    return random.Random(rng.getrandbits(64))  # noqa: S311 - replayable, not a secret


async def _client(
    base_url: str,
    story: str,
    key: str,
    request: dict[str, object],
    draws: Draws,
    answers: dict[str, list[tuple[str, str]]],
    outcomes: dict[str, int],
) -> None:
    """Send the request ``ITERATIONS`` times; drop some of them part-way (a client-side
    cancellation is a dropped connection). A refused connection sends nothing (the engine is
    down): the client sends the same request again after a backoff, and does not count the
    refusal, so a slow restart cannot use up its requests (#305). Refused for ``REFUSED_S``
    without a break, it fails."""
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        for n in range(ITERATIONS):
            drop = draws.drops.random() < DROP_RATE or (n == 0 and draws.drops_first)
            after = draws.drops.uniform(0.0, 2 * STUB_LATENCY_S) if drop else None
            refused_since = loop.time()
            while (sent := await _send(client, story, request, key, after)) == REFUSED:
                outcomes[REFUSED] += 1
                assert loop.time() - refused_since < REFUSED_S, f"seed={SEED} {key}: refused"
                await asyncio.sleep(draws.waits.uniform(0.05, 0.3))
            if isinstance(sent, str):  # DROPPED, or LOST (backs off)
                outcomes[sent] += 1
                if sent == LOST:
                    await asyncio.sleep(draws.waits.uniform(0.05, 0.3))
            elif sent.status_code == 200:
                outcomes["answered"] += 1
                answers[key].append(message_of(sent))
            else:
                outcomes[f"status_{sent.status_code}"] += 1
                await asyncio.sleep(draws.waits.uniform(0.05, 0.3))


async def _send(
    client: httpx.AsyncClient,
    story: str,
    request: dict[str, object],
    key: str,
    drop_after: float | None,
) -> httpx.Response | str:
    """One request: the engine's response, or what became of it instead: ``DROPPED`` (the
    client gave up on it after ``drop_after``), ``LOST`` (the connection broke under it) or
    ``REFUSED`` (the engine is down: nothing was sent)."""
    attempt = asyncio.create_task(keyed(client, story, request, key))
    if drop_after is not None:
        await asyncio.sleep(drop_after)
        if attempt.cancel():  # still unanswered: the client drops it
            await asyncio.wait({attempt})
            return DROPPED
    try:
        return await attempt
    except httpx.ConnectError:
        return REFUSED
    except httpx.HTTPError:
        return LOST


async def _crashes(
    child: EngineProcess,
    engine: AsyncEngine,
    rng: random.Random,
    stop: asyncio.Event,
    queues: list[str],
    outcomes: dict[str, int],
) -> tuple[str, float]:
    """Kill the engine and start it again, ``RESTARTS`` times. The first kill ends the paused
    first life once ``_ready`` has seen what it waits for. The later ones come at random times,
    unless every client has finished first. Returns the id of the turn the first kill caught
    before its commit, and how long after the clients started it got there."""
    started = asyncio.get_running_loop().time()
    held = await _ready(child, engine, queues, outcomes)
    waited = asyncio.get_running_loop().time() - started
    child.pause_before_commit = False
    await child.restart()
    for _ in range(RESTARTS - 1):
        try:
            await asyncio.wait_for(stop.wait(), rng.uniform(*RESTART_AFTER_S))
            break
        except TimeoutError:
            pass
        await child.restart()
    return held, waited


async def _ready(
    child: EngineProcess, engine: AsyncEngine, queues: list[str], outcomes: dict[str, int]
) -> str:
    """Wait until the paused engine holds a turn just before its commit, a queue story has
    ``QUEUE_KEYS - 1`` turns waiting behind its running one, and a client has dropped a
    request. Returns the held turn's operation id. Bounded: ``MARKER_TIMEOUT_S`` for the turn,
    then ``OBSERVE_S``."""
    try:
        marker = await child.next_marker()
    except TimeoutError:
        raise AssertionError(
            f"seed={SEED}: no turn reached its commit within {MARKER_TIMEOUT_S} s"
        ) from None
    assert marker.startswith(BEFORE_COMMIT), f"seed={SEED}: {marker}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OBSERVE_S
    while True:
        waiting = await _waiting(engine, queues)
        if waiting and outcomes[DROPPED]:
            return marker.removeprefix(BEFORE_COMMIT).strip()
        assert loop.time() < deadline, (
            f"seed={SEED}: after {OBSERVE_S} s, queue stories with {QUEUE_KEYS - 1} turns "
            f"waiting: {waiting}; requests dropped: {outcomes[DROPPED]}"
        )
        await asyncio.sleep(POLL_S)


async def _waiting(engine: AsyncEngine, queues: list[str]) -> int:
    """How many queue stories have ``QUEUE_KEYS - 1`` turns waiting behind their running one."""
    found = await rows(
        engine,
        "SELECT story_id FROM operations WHERE story_id = ANY(:s) AND kind = 'turn' "
        "AND status = 'queued' GROUP BY story_id HAVING count(*) >= :n",
        s=queues,
        n=QUEUE_KEYS - 1,
    )
    return len(found)


async def _stop_after(clients: list[asyncio.Task[None]], stop: asyncio.Event) -> None:
    """Set ``stop`` once every client has sent all its requests: no restart comes after."""
    await asyncio.wait(clients)
    stop.set()


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.integration
@pytest.mark.part("M2", "NFR-REL-4; FR-CONC-6")
async def test_x_04(pg_url: str, engine: AsyncEngine) -> None:
    """Retry storms under drops and crashes: one turn per key, one message per key.

    Complements asserted: the run produced dropped connections, engine restarts that killed
    turns in flight (the turn the first kill caught never committed), turns queued behind a
    running one, and answered retries (the chaos was real), and every key's stored operation
    is its story's one turn."""
    await _storm(pg_url, engine, Storm("X-04", "x04", KEYS, QUEUE_STORIES))


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.integration
@pytest.mark.part("M2", "NFR-REL-4; FR-CONC-6")
async def test_x_04__slow_engine(pg_url: str, engine: AsyncEngine) -> None:
    """The same storm against an engine whose turns wait ``SLOW_START_S`` before they begin,
    longer than any restart's delay: a slow runner (#305).

    Complements asserted: as ``test_x_04``, and the engine was that slow (no turn reached its
    commit sooner)."""
    storm = Storm("X-04 slow-engine", "x04slow", SLOW_KEYS, SLOW_QUEUE_STORIES, SLOW_START_S)
    await _storm(pg_url, engine, storm)


async def _storm(pg_url: str, engine: AsyncEngine, storm: Storm) -> None:
    print(f"{storm.label} seed={SEED} iterations={ITERATIONS} keys={storm.keys}")
    rng = random.Random(SEED)  # noqa: S311 - a replayable seed, not a secret
    settings: Settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):
        stories = [await new_story(client) for _ in range(storm.keys)]
        queues = [
            await new_story(
                client,
                settings={"turn_concurrency": "queue"},
                overrides={"budgets": {"deadline_ms": QUEUE_DEADLINE_S * 1000}},
            )
            for _ in range(storm.queue_stories)
        ]
    requests = {
        f"{storm.prefix}-{SEED}-{n}": (story, body(ids[PLAYER], f"Storm {n}, are you there?"))
        for n, (story, ids) in enumerate(stories)
    }
    queued = {
        f"{storm.prefix}-{SEED}-q{n}-{k}": (story, body(ids[PLAYER], f"Queue {n}, move {k}."))
        for n, (story, ids) in enumerate(queues)
        for k in range(QUEUE_KEYS)
    }
    every = {**requests, **queued}
    crash_draws = _derived(rng)
    first = rng.randrange(len(every))
    draws = [Draws(_derived(rng), _derived(rng), n == first) for n in range(len(every))]
    answers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    outcomes: dict[str, int] = defaultdict(int)
    child = EngineProcess(
        pg_url,
        CREDENTIAL_A,
        lease_s=LEASE_S,
        stub_latency_s=STUB_LATENCY_S,
        pause_before_commit=True,
        turn_start_delay_s=storm.turn_start_delay_s,
    )
    await child.start()
    stop = asyncio.Event()
    try:
        # One group: a bound that fails (the crash schedule's or a client's) cancels the rest
        # at once instead of leaving them to wait on a paused engine.
        async with asyncio.TaskGroup() as group:
            crashes = group.create_task(
                _crashes(child, engine, crash_draws, stop, [s for s, _ in queues], outcomes)
            )
            clients = [
                group.create_task(
                    _client(child.base_url, story, key, request, mine, answers, outcomes)
                )
                for (key, (story, request)), mine in zip(every.items(), draws, strict=True)
            ]
            group.create_task(_stop_after(clients, stop))
        held, waited = crashes.result()
        # Every key ends answered: final, undisturbed retries (a queued turn may still be
        # waiting: 202 until it played).
        async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as final:
            for key, (story, request) in every.items():
                for _ in range(FINAL_ATTEMPTS):
                    response = await keyed(final, story, request, key)
                    if response.status_code not in (202, 423):
                        break
                    await asyncio.sleep(0.25)
                assert response.status_code == 200, f"seed={SEED} {key}: {response.text}"
                answers[key].append(message_of(response))
    finally:
        await child.kill()
    print(f"{storm.label} outcomes={dict(outcomes)} restarts={child.starts - 1}")
    print(f"{storm.label} first kill caught {held} before its commit, {waited:.2f} s in")

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
        s=[story for story, _ in every.values()],
    )
    (caught,) = await rows(
        engine, "SELECT status::text AS status FROM operations WHERE operation_id = :o", o=held
    )
    print(f"{storm.label} originals killed in flight={crashed[0].n}")
    print(f"{storm.label} turns queued behind a running one={overlapped}")
    assert outcomes["answered"] > 0, f"seed={SEED}"
    assert outcomes[DROPPED] > 0, f"seed={SEED}: no connection was dropped"
    assert child.starts > 1, f"seed={SEED}: the engine never restarted"
    assert crashed[0].n > 0, f"seed={SEED}: no crash hit a turn in flight"
    assert caught.status in ("in_flight", "failed"), (
        f"seed={SEED}: the turn the first kill caught before its commit ({held}) ended "
        f"{caught.status}"
    )
    assert overlapped > 0, f"seed={SEED}: no turn was queued"
    assert waited >= storm.turn_start_delay_s, f"seed={SEED}: a turn began within {waited} s"


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
