"""X-05: chaos — model/provider timeouts and failures (docs/04 §12; NFR-REL-3, FR-MODEL-6).

Guarded replies (``epistrel.testing.ops``, the model-using operation until #57's turn) run over
REST against the stub while every model call fails at random (``FAULT_RATE``) with one of the
stub's fault kinds, and the harness holds a purpose now and then and releases it after a random
delay, sometimes after the call's timeout. Every request must finish within the turn deadline
plus ``SLACK_S``, and every operation must be all-or-nothing: committed with exactly its one
event and a revision step, or failed with no event, no revision change, and a 503 that names
the purpose.

``EPISTREL_CHAOS_ITERATIONS`` (default 50, the quick form PR CI runs) sets the number of
requests and ``EPISTREL_CHAOS_SEED`` replays a run; the seed is printed.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import Counter

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.models.stub import FAULT_KINDS, FaultKind, StubLatch
from epistrel.testing.ops import TEST_EVENT
from tests.auth_support import auth_settings
from tests.event_support import new_story
from tests.integration.test_model_unavailable import guarded_client
from tests.story_support import WRITE
from tests.structured_support import POLICY_PROMPT, story_state

ITERATIONS = int(os.environ.get("EPISTREL_CHAOS_ITERATIONS", "50"))
SEED = int(os.environ.get("EPISTREL_CHAOS_SEED") or random.SystemRandom().randrange(2**32))
FAULT_RATE = 0.2
TURN_DEADLINE_S = 5.0
CALL_TIMEOUT_S = 1.0
SLACK_S = 5.0
HOLD_RATE = 0.1
STORIES = 5
PURPOSES = ("guardrail_check", "generation")


async def release_after(latch: StubLatch, delay: float) -> None:
    """The harness releases a held purpose, sometimes after the call has timed out."""
    await asyncio.sleep(delay)
    latch.release()


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.integration
@pytest.mark.part("M2", "NFR-REL-3; FR-MODEL-6")
async def test_x_05(pg_url: str, engine: AsyncEngine) -> None:
    """Random provider faults never block a request or leave a partial operation.

    Complements asserted: the run produced both clean 503s and committed replies (the faults
    were real and the path still works), and a committed reply's event is found by the same
    per-operation search that finds none for a failed one."""
    print(f"\nX-05 seed={SEED} iterations={ITERATIONS}")
    rng = random.Random(SEED)  # noqa: S311 - a replayable seed, not a secret
    settings: Settings = auth_settings(pg_url, model_adapter="stub")
    stories = [await new_story(engine) for _ in range(STORIES)]
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()

    def draw(_: str) -> FaultKind | None:
        return rng.choice(FAULT_KINDS) if rng.random() < FAULT_RATE else None

    async with guarded_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for iteration in range(ITERATIONS):
            stub.reset()
            stub.fault_hook = draw
            story = stories[iteration % STORIES]
            before = await story_state(engine, story)
            release: asyncio.Task[None] | None = None
            if rng.random() < HOLD_RATE:
                latch = stub.hold(rng.choice(PURPOSES))
                delay = rng.uniform(0.0, 1.5 * CALL_TIMEOUT_S)
                release = asyncio.create_task(release_after(latch, delay))
            body = {
                "said": f"step {iteration} on the road",
                "model_prompt": POLICY_PROMPT,
                "on_uncertain": rng.choice(["violation", "pass"]),
                "deadline_s": TURN_DEADLINE_S,
                "timeout_s": CALL_TIMEOUT_S,
            }
            started = time.monotonic()
            response = await asyncio.wait_for(
                client.post(f"/_test/stories/{story}/guarded-reply", json=body, headers=WRITE),
                TURN_DEADLINE_S + SLACK_S,
            )
            elapsed = time.monotonic() - started
            if release is not None:
                await release
            after = await story_state(engine, story)
            context = f"seed={SEED} iteration={iteration} body={response.text}"

            assert elapsed <= TURN_DEADLINE_S + SLACK_S, context
            (op,) = set(after.statuses) - set(before.statuses)
            if response.status_code == 200:
                outcomes["committed"] += 1
                assert after.statuses[op] == "committed", context
                assert after.events[op] == [TEST_EVENT], context
                assert after.timeline_revision == before.timeline_revision + 1, context
                assert response.json()["timeline_revision"] == after.timeline_revision, context
            else:
                outcomes["failed"] += 1
                assert response.status_code == 503, context
                error = response.json()["error"]
                assert error["code"] == "model_unavailable", context
                assert error["details"]["purpose"] in PURPOSES, context
                reasons[error["details"]["reason"]] += 1
                assert after.statuses[op] == "failed", context
                assert op not in after.events, context
                assert after.timeline_revision == before.timeline_revision, context

    async with engine.connect() as conn:
        stuck = await conn.scalar(
            text(
                "SELECT count(*) FROM operations WHERE story_id = ANY(:ids) "
                "AND status = 'in_flight'"
            ),
            {"ids": stories},
        )
        partial = await conn.scalar(
            text(
                "SELECT count(*) FROM operations o WHERE o.story_id = ANY(:ids) AND "
                "o.kind = 'turn' AND (o.status = 'committed') <> EXISTS "
                "(SELECT 1 FROM events e WHERE e.operation_id = o.operation_id)"
            ),
            {"ids": stories},
        )
    print(f"X-05 outcomes={dict(outcomes)} reasons={dict(reasons)}")
    assert stuck == 0, f"seed={SEED}"
    assert partial == 0, f"seed={SEED}"
    assert sum(outcomes.values()) == ITERATIONS
    if ITERATIONS >= 20:
        assert outcomes["failed"] > 0, f"seed={SEED}: no fault reached a request"
        assert outcomes["committed"] > 0, f"seed={SEED}: nothing committed"
