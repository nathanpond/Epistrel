"""X-03: chaos — crash mid-turn (docs/04 §12; NFR-REL-1,2; #75).

A child engine (``epistrel.testing.faults.EngineProcess``, ``api`` mode, the stub adapter) runs
on its own scratch database. Each iteration starts it with a crash plan, ``crash_at(point,
nth)`` with a uniformly drawn point and ``nth`` (20 % of iterations get none, and a point
the iteration never reaches ``nth`` times is a run without a kill), then sends up to
``OPS`` operations one at a time: turns, truncations, swaps and forks (60/20/10/10, over a pool
of stories that forks join; an infeasible draw is skipped). When the plan fires the child dies
with ``os._exit(137)`` mid-request. The harness then does what a restarted deployment does:
the worker's recovery pass, in-process, until nothing is left in flight. It starts the child
again without a plan, retries the interrupted request under its key, and goes on. Each
iteration ends with a ``SIGKILL`` between requests and a drain of the background jobs (the
harness is the worker, ``api`` mode runs none). Then the predicate (``tests.crash_support``)
holds for every story the iteration touched: nothing in flight, dense ``seq``, every event's
operation committed, consistent counters, and projections equal to a rebuild from the log.
A client-side oracle of each story's head turn must agree with the database.

``EPISTREL_CHAOS_ITERATIONS`` (default 20, the quick form PR CI runs; the weekly run uses 500)
and ``EPISTREL_CHAOS_SEED`` (printed) replay a run.
"""

from __future__ import annotations

import os
import random
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
import pytest

from epistrel.core import timeline
from epistrel.core.db import make_engine
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.testing.faults import POINTS, EngineProcess, crash_at
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.conformance.conftest import migrated_database
from tests.crash_support import drain_jobs, in_flight_keys, integrity, recover
from tests.event_support import rows
from tests.idempotency_support import KEY
from tests.respond_support import FIRST, PLAYER, SECOND, body
from tests.story_support import WRITE, story_body

ITERATIONS = int(os.environ.get("EPISTREL_CHAOS_ITERATIONS", "20"))
SEED = int(os.environ.get("EPISTREL_CHAOS_SEED") or random.SystemRandom().randrange(2**32))
OPS = 10
NO_KILL = 0.2
NTH_MAX = 8
MIX = (("respond", 60), ("truncate", 20), ("swap", 10), ("fork", 10))
LEASE_S = 1.0
STORIES = 2
CRASHED = 137


@dataclass
class Story:
    id: str
    player: str
    head: int = 0
    """The oracle: the head turn the client expects, from what it was answered."""


@dataclass(frozen=True)
class Request:
    kind: str
    story: Story
    path: str
    payload: dict[str, Any]
    key: str


@dataclass
class Tally:
    kills: Counter[str] = field(default_factory=Counter)
    sent: Counter[str] = field(default_factory=Counter)
    skipped: int = 0
    refused: Counter[str] = field(default_factory=Counter)
    retried: int = 0
    keys: dict[str, str] = field(default_factory=dict)
    """Every key the client was answered for, and the operation it was answered with."""


async def _send(child: EngineProcess, request: Request) -> httpx.Response | None:
    """The response, or ``None`` when the child died of its crash point mid-request."""
    headers = {**WRITE, KEY: request.key}
    try:
        async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
            return await client.post(request.path, json=request.payload, headers=headers)
    except httpx.HTTPError:
        status = await child.wait_exit(10)
        assert status == CRASHED, f"seed={SEED}: the engine failed without its crash ({status})"
        return None


async def _choose(db: Any, rng: random.Random, pool: list[Story], label: str) -> Request | None:
    kind = rng.choices([k for k, _ in MIX], weights=[w for _, w in MIX])[0]
    story = rng.choice(pool)
    key = f"x03-{SEED}-{label}"
    base = f"/stories/{story.id}"
    if kind == "respond":
        return Request(kind, story, f"{base}/respond", body(story.player, f"Move {label}."), key)
    if kind == "truncate":
        if story.head < 1:
            return None
        return Request(kind, story, f"{base}/truncate", {"to_turn": rng.randrange(story.head)}, key)
    if kind == "swap":
        chains = (await timeline.undo_state(db, story.id)).dormant_chains
        if not chains:
            return None
        chain = rng.choice(chains)
        target = {"turn": chain.from_turn, "variant": chain.variant}
        return Request(kind, story, f"{base}/swap", {"to_variant_chain": target}, key)
    return Request(kind, story, f"{base}/fork", {"at_turn": rng.randint(0, story.head)}, key)


async def _answered(
    db: Any, request: Request, response: httpx.Response, pool: list[Story], tally: Tally
) -> None:
    """Fold an answer into the oracle. A retry after a crash between commit and response may
    get the reference to the committed operation instead of the result (#61)."""
    found = response.json()
    if response.status_code == 409 and request.kind == "swap":
        # A dormant chain that continues in more than one way names no single chain.
        assert found["error"]["code"] == "unknown_chain", f"seed={SEED}: {found}"
        tally.refused[request.kind] += 1
        return
    expected = 201 if request.kind == "fork" else 200
    assert response.status_code in (expected, 200), f"seed={SEED} {request}: {response.text}"
    tally.keys[request.key] = found.get("operation_id", "")
    story = request.story
    if request.kind == "respond":
        story.head += 1
        if "operation_id" not in found:
            tally.keys[request.key] = await _operation_of(db, request.key)
    elif request.kind == "truncate":
        story.head = request.payload["to_turn"]
    elif request.kind == "swap":
        story.head = await _head(db, story.id)
        if "next_turn_index" in found:
            assert story.head == found["next_turn_index"] - 1, f"seed={SEED}: {found}"
    else:
        fork_id = found.get("story_id") or await _story_of(db, found["operation_id"])
        player = await _player(db, fork_id)
        pool.append(Story(fork_id, player, request.payload["at_turn"]))


async def _operation_of(db: Any, key: str) -> str:
    (row,) = await rows(db, "SELECT operation_id FROM idempotency_keys WHERE key = :k", k=key)
    return str(row.operation_id)


async def _story_of(db: Any, operation_id: str) -> str:
    (row,) = await rows(
        db, "SELECT story_id FROM operations WHERE operation_id = :o", o=operation_id
    )
    return str(row.story_id)


async def _player(db: Any, story_id: str) -> str:
    (row,) = await rows(
        db, "SELECT id FROM characters WHERE story_id = :s AND name = :n", s=story_id, n=PLAYER
    )
    return str(row.id)


async def _head(db: Any, story_id: str) -> int:
    (row,) = await rows(db, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id)
    return int(row.next_turn_index) - 1


async def _create(child: EngineProcess) -> Story:
    async with httpx.AsyncClient(base_url=child.base_url, timeout=60) as client:
        response = await client.post(
            "/stories", json=story_body(PLAYER, FIRST, SECOND), headers=WRITE
        )
    assert response.status_code == 201, response.text
    created = response.json()
    players = [c["id"] for c in created["characters"] if c["name"] == PLAYER]
    return Story(created["story_id"], players[0])


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.integration
@pytest.mark.part("M2", "NFR-REL-1,2")
async def test_x_03(pg_url: str) -> None:
    """Kills at random crash points across turns, truncations, swaps and forks leave no
    partial operation, and every rebuild equals the incremental projections.

    Complements asserted: crash points fired (more than one point when the run is long enough),
    interrupted requests were retried under their keys and answered, forks joined the pool, and
    every key the client was answered for maps to exactly one committed operation."""
    print(f"\nX-03 seed={SEED} iterations={ITERATIONS} ops={OPS}")
    rng = random.Random(SEED)  # noqa: S311 - a replayable seed, not a secret
    tally = Tally()
    with migrated_database(pg_url, f"epistrel_x03_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub")
        db = make_engine(settings)
        models = ModelAccessLayer.from_settings(settings)
        models.bind(db)
        child = EngineProcess(url, CREDENTIAL_A, lease_s=LEASE_S, mode="api")
        try:
            await child.start()
            pool = [await _create(child) for _ in range(STORIES)]
            await child.kill()
            for iteration in range(ITERATIONS):
                await _iteration(db, models, child, rng, pool, tally, iteration)
            await _final(db, pool, tally)
        finally:
            await child.kill()
            await models.aclose()
            await db.dispose()
    print(
        f"X-03 kills={dict(tally.kills)} sent={dict(tally.sent)} skipped={tally.skipped} "
        f"refused={dict(tally.refused)} retried={tally.retried} stories={len(pool)}"
    )
    assert sum(tally.kills.values()) > 0, f"seed={SEED}: no crash point fired"
    assert tally.retried == sum(tally.kills.values()), f"seed={SEED}"
    if ITERATIONS >= 20:
        assert len(tally.kills) > 1, f"seed={SEED}: only {set(tally.kills)} fired"
        assert len(pool) > STORIES, f"seed={SEED}: no fork joined the pool"


async def _iteration(
    db: Any,
    models: ModelAccessLayer,
    child: EngineProcess,
    rng: random.Random,
    pool: list[Story],
    tally: Tally,
    iteration: int,
) -> None:
    plan = None if rng.random() < NO_KILL else crash_at(rng.choice(POINTS), rng.randint(1, NTH_MAX))
    child.crash = plan
    await child.start()
    touched: set[str] = set()
    context = f"seed={SEED} iteration={iteration} plan={plan}"
    for step in range(OPS):
        request = await _choose(db, rng, pool, f"{iteration}-{step}")
        if request is None:
            tally.skipped += 1
            continue
        touched.add(request.story.id)
        tally.sent[request.kind] += 1
        response = await _send(child, request)
        if response is None:
            assert plan is not None, context
            tally.kills[plan.point] += 1
            await recover(db)
            child.crash = None
            await child.start()
            response = await _send(child, request)
            assert response is not None, f"{context}: the retry crashed"
            tally.retried += 1
        before = len(pool)
        await _answered(db, request, response, pool, tally)
        touched.update(story.id for story in pool[before:])
    await child.kill()
    await recover(db)
    await drain_jobs(db, models, touched)
    stories = [story for story in pool if story.id in touched]
    assert await integrity(db, [s.id for s in stories]) == [], context
    for story in stories:
        assert await _head(db, story.id) == story.head, f"{context} story={story.id}"


async def _final(db: Any, pool: list[Story], tally: Tally) -> None:
    """Every story sound, every key settled, one committed operation per answered key."""
    assert await integrity(db, [s.id for s in pool]) == [], f"seed={SEED}"
    assert await in_flight_keys(db) == [], f"seed={SEED}"
    for key, operation_id in tally.keys.items():
        (row,) = await rows(
            db,
            "SELECT k.status, k.operation_id, o.status::text AS op FROM idempotency_keys k "
            "JOIN operations o ON o.operation_id = k.operation_id WHERE k.key = :k",
            k=key,
        )
        assert (row.status, row.op) == ("done", "committed"), f"seed={SEED} {key}"
        assert operation_id in ("", row.operation_id), f"seed={SEED} {key}"
    doubled = await rows(
        db,
        "SELECT idempotency_key FROM operations WHERE status = 'committed' "
        "AND idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING count(*) > 1",
    )
    assert doubled == [], f"seed={SEED}: keys with two commits {doubled}"
