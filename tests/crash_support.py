"""Helpers for the crash and disruption chaos tests (X-03, X-06; #75): the recovery a restarted
deployment runs, the background jobs a harness drains itself, and the integrity predicate a
story must satisfy after any crash.

The predicate (``integrity``) is what "no partial operation" means in the database:

- no operation is ``in_flight`` and no story's turn slot is held (the recovery pass failed or
  reclaimed what the crash left; leases and in-flight keys are not partial state);
- every story's ``seq`` is dense from 1;
- every event belongs to a ``committed`` operation (a failed or abandoned one wrote nothing);
- the story's counters agree with its operations: ``next_turn_index`` is one past the highest
  active committed turn, and ``timeline_revision`` is the highest revision any committed
  operation recorded since the story's own genesis (a fork's copied operations keep the
  source's revisions);
- the projections equal a rebuild from the log (``projections.rebuild``, NFR-REL-2).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import orphans, projections
from epistrel.orchestrator.jobs.worker import recovery_pass
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.event_support import rows
from tests.projection_support import snapshot
from tests.summary_support import run_due_jobs

RECOVERY_TIMEOUT_S = 20.0
GRACE_S = 1.0
"""The child engines' lease (``EngineProcess(lease_s=1.0)``): what a crash left is orphaned
once it is this old."""


async def in_flight(db: AsyncEngine) -> list[str]:
    """Every operation still ``in_flight``, and every story whose turn slot is held."""
    ops = await rows(db, "SELECT operation_id FROM operations WHERE status = 'in_flight'")
    slots = await rows(db, "SELECT id FROM stories WHERE in_flight_turn IS NOT NULL")
    return [f"operation {r.operation_id} in_flight" for r in ops] + [
        f"story {r.id} turn slot held" for r in slots
    ]


async def recover(
    db: AsyncEngine, *, grace_s: float = GRACE_S, timeout_s: float = RECOVERY_TIMEOUT_S
) -> int:
    """Run the worker's recovery pass until nothing is left ``in_flight`` (the crashed
    process's leases must lapse first; an operation counts as orphaned after ``grace_s``, the
    child engine's lease); how many passes it took. Fails after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    passes = 0
    before, orphans.GRACE_S = orphans.GRACE_S, grace_s
    try:
        while True:
            await recovery_pass(db)
            passes += 1
            left = await in_flight(db)
            if not left:
                return passes
            if time.monotonic() >= deadline:
                raise AssertionError(f"still in flight after {timeout_s} s of recovery: {left}")
            await asyncio.sleep(0.25)
    finally:
        orphans.GRACE_S = before


async def drain_jobs(db: AsyncEngine, models: ModelAccessLayer, story_ids: Iterable[str]) -> int:
    """Run every due ``lite_summary`` job of the stories (the harness is the worker); how many."""
    ran = 0
    for story_id in story_ids:
        ran += len(await run_due_jobs(db, models, story_id))
    return ran


async def in_flight_keys(db: AsyncEngine) -> list[str]:
    found = await rows(db, "SELECT key FROM idempotency_keys WHERE status = 'in_flight'")
    return [r.key for r in found]


async def integrity(
    db: AsyncEngine, story_ids: Iterable[str], *, rebuild: bool = True
) -> list[str]:
    """Every way the stories break the predicate (see the module docstring); ``[]`` when sound."""
    problems = await in_flight(db)
    for story_id in story_ids:
        problems += [f"{story_id}: {p}" for p in await _story_problems(db, story_id, rebuild)]
    return problems


async def _story_problems(db: AsyncEngine, story_id: str, rebuild: bool) -> list[str]:
    problems: list[str] = []
    (seqs,) = await rows(
        db,
        "SELECT count(*) AS n, coalesce(min(seq), 1) AS lo, coalesce(max(seq), 0) AS hi "
        "FROM events WHERE story_id = :s",
        s=story_id,
    )
    if (seqs.lo, seqs.hi) != (1, seqs.n):
        problems.append(f"seq not dense: {seqs.n} events over {seqs.lo}..{seqs.hi}")
    orphaned = await rows(
        db,
        "SELECT e.seq, o.status::text AS status FROM events e "
        "JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND o.status <> 'committed' ORDER BY e.seq",
        s=story_id,
    )
    problems += [f"event seq {r.seq} of a {r.status} operation" for r in orphaned]
    (counters,) = await rows(
        db,
        "SELECT s.next_turn_index, s.timeline_revision, "
        "(SELECT coalesce(max(narrative_turn), 0) FROM operations o WHERE o.story_id = s.id "
        " AND o.kind = 'turn' AND o.status = 'committed' AND o.membership = 'active') AS head, "
        "(SELECT max(timeline_revision) FROM operations o WHERE o.story_id = s.id "
        " AND o.status = 'committed' AND o.committed_at >= (SELECT max(g.committed_at) "
        "  FROM operations g WHERE g.story_id = s.id AND g.kind = 'genesis' "
        "  AND g.status = 'committed')) AS revision "
        "FROM stories s WHERE s.id = :s",
        s=story_id,
    )
    if counters.next_turn_index != counters.head + 1:
        problems.append(
            f"next_turn_index {counters.next_turn_index} but the active head is {counters.head}"
        )
    if counters.timeline_revision != counters.revision:
        problems.append(
            f"timeline_revision {counters.timeline_revision} but its operations recorded "
            f"{counters.revision}"
        )
    if rebuild:
        incremental = await snapshot(db, story_id)
        await projections.rebuild(db, story_id)
        if (rebuilt := await snapshot(db, story_id)) != incremental:
            problems.append(f"projections differ from a rebuild: {incremental} != {rebuilt}")
    return problems


__all__ = [
    "RECOVERY_TIMEOUT_S",
    "drain_jobs",
    "in_flight",
    "in_flight_keys",
    "integrity",
    "recover",
]
