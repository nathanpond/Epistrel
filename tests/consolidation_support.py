"""Helpers for the consolidation tests (#143): a structured story with consolidation on, long
runs of turns whose episodic memories are written in bulk, the stub's scripted answers, the
story's consolidation jobs run in the test (no background worker) and the output rows.

``bulk_turns`` plays bare turns through ``Operation`` (a player input by the actor at beat 1)
and then writes one episodic memory per turn straight into ``memories`` through
``core.membership.insert_derived``, each citing its own turn's operation and event, so the
memories follow their turns on every membership change exactly as the episodic writer's do.
Writing hundreds of turns through the writer would run its hooks (embedding, importance,
consolidation) for every one; these rows enqueue nothing and are not indexed for erasure (the
erasure tests write theirs through ``memory_support.turn``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.enums import MemoryKind, OperationKind, Provenance, RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import records
from epistrel.core.tables.memories import memories
from epistrel.orchestrator.jobs.handlers import consolidation
from epistrel.orchestrator.jobs.types import ClaimedJob
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests import canon_support as c
from tests.event_support import rows
from tests.memory_support import use_structured
from tests.profile_support import store_overrides
from tests.summary_support import LEASE_S, worker

PRINCIPAL = "p-test"


def dials(
    *, window_memories: int | None = None, cadence_turns: int | None = None
) -> dict[str, Any]:
    """A story override of the consolidation dials."""
    found: dict[str, Any] = {}
    if window_memories is not None:
        found["window_memories"] = window_memories
    if cadence_turns is not None:
        found["cadence_turns"] = cadence_turns
    return {"memory": {"consolidation": found}}


async def story(engine: AsyncEngine, overrides: dict[str, Any] | None = None) -> c.World:
    """#133's world (Cal the player, Bobby, Ann) on ``Structured (M4)`` (episodic memory and
    consolidation on), with ``overrides`` of its dials."""
    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    if overrides:
        await store_overrides(engine, w.story_id, overrides)
    return w


@dataclass(frozen=True, slots=True)
class Turn:
    """A bulk turn: its operation, its player-input event and its memory."""

    number: int
    operation_id: str
    event_id: str
    memory_id: str


Line = Callable[[int], str]


async def bulk_turns(
    engine: AsyncEngine,
    story_id: str,
    count: int,
    *,
    actor: str,
    name: str,
    line: Line = lambda n: f"line {n}",
    does: bool = False,
) -> list[Turn]:
    """``count`` more turns by ``actor`` (named ``name``), each with one episodic memory
    (``<name> says: <line(n)>``, or ``does:``), written in bulk (see the module docstring)."""
    played: list[tuple[Operation, int]] = []
    for _ in range(count):
        async with Operation(
            engine, story_id=story_id, kind=OperationKind.TURN, principal=PRINCIPAL
        ) as op:
            op.append(
                "player_input",
                {"text": "...", "say": "...", "do": None},
                provenance=Provenance.PLAYER,
                character_id=actor,
                beat=1,
            )
            await op.commit()
        assert op.narrative_turn is not None
        played.append((op, op.narrative_turn))
    events = {
        str(r.operation_id): str(r.id)
        for r in await rows(
            engine,
            "SELECT id, operation_id FROM events WHERE story_id = :s AND type = 'player_input' "
            "AND operation_id = ANY(:ops)",
            s=story_id,
            ops=[op.operation_id for op, _ in played],
        )
    }
    verb = "does" if does else "says"
    made: list[Turn] = []
    values = []
    for op, number in played:
        memory_id = new_ulid()
        event_id = events[op.operation_id]
        values.append(
            {
                "id": memory_id,
                "story_id": story_id,
                "kind": MemoryKind.EPISODIC.value,
                "text": f"{name} {verb}: {line(number)}",
                "holder_character_id": None,
                "character_id": actor,
                "visibility": {"scope": "public"},
                "provenance": RecordProvenance.PLAYER.value,
                "trust_tier": records.trust_tier_of(RecordProvenance.PLAYER).value,
                "operation_id": op.operation_id,
                "source_event_ids": [event_id],
                "narrative_turn": number,
                "turn_variant": op.turn_variant,
                "beat": 1,
                "proposition_ids": [],
                "participants": [actor],
            }
        )
        made.append(Turn(number, op.operation_id, event_id, memory_id))
    async with engine.begin() as conn:
        await insert_derived(conn, memories, values)
    return made


async def private_memory(
    engine: AsyncEngine, story_id: str, holder: str, characters: Sequence[str], line: str
) -> str:
    """A memory scoped to ``characters`` that ``holder`` holds, on a turn of its own (a reply
    voiced by nobody), written in that turn's commit (test-only: M4 writes no private
    memory). Answers its id."""
    memory_id = new_ulid()
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("message_rendered", {"text": line, "render_character_ids": []}, beat=1)

        async def step(conn: AsyncConnection) -> None:
            event = await conn.scalar(
                text("SELECT id FROM events WHERE operation_id = :o ORDER BY seq LIMIT 1"),
                {"o": op.operation_id},
            )
            await insert_derived(
                conn,
                memories,
                [
                    {
                        "id": memory_id,
                        "story_id": story_id,
                        "kind": MemoryKind.EPISODIC.value,
                        "text": line,
                        "holder_character_id": holder,
                        "character_id": None,
                        "visibility": {"scope": "characters", "characters": list(characters)},
                        "provenance": RecordProvenance.DIRECTOR.value,
                        "trust_tier": records.trust_tier_of(RecordProvenance.DIRECTOR).value,
                        "operation_id": op.operation_id,
                        "source_event_ids": [str(event)],
                        "narrative_turn": op.narrative_turn,
                        "turn_variant": op.turn_variant,
                        "beat": 1,
                    }
                ],
            )

        op.add_commit_step(step)
        await op.commit()
    return memory_id


def answer(
    facts: Sequence[tuple[str, str, Sequence[int]]] = (),
    reflections: Sequence[tuple[str, str, Sequence[int]]] = (),
) -> str:
    """The stub's ``consolidation`` answer: ``(text, modality, supports)`` per statement."""

    def item(t: tuple[str, str, Sequence[int]]) -> dict[str, Any]:
        return {"text": t[0], "modality": t[1], "attributed_to": None, "supports": list(t[2])}

    return json.dumps(
        {"facts": [item(f) for f in facts], "reflections": [item(r) for r in reflections]}
    )


async def outputs(
    engine: AsyncEngine, story_id: str, *, membership: str | None = None
) -> list[Any]:
    """The story's semantic memories and reflections, by position then kind then text."""
    query = (
        "SELECT *, membership::text AS membership_text FROM memories WHERE story_id = :s "
        "AND kind IN ('semantic', 'reflection')"
    )
    params: dict[str, Any] = {"s": story_id}
    if membership is not None:
        query += " AND membership = CAST(:m AS membership)"
        params["m"] = membership
    query += " ORDER BY narrative_turn, kind, text, id"
    return await rows(engine, query, **params)


async def windows(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's committed memory-consolidation operations (``source_window`` and
    membership), by position."""
    return await rows(
        engine,
        "SELECT operation_id, narrative_turn, turn_variant, membership::text AS membership, "
        "source_window FROM operations WHERE story_id = :s AND kind = 'consolidation' "
        "AND status = 'committed' AND source_window->>'consolidation' = 'memories' "
        "ORDER BY narrative_turn, started_at",
        s=story_id,
    )


async def jobs(engine: AsyncEngine, story_id: str, status: str | None = None) -> list[Any]:
    query = (
        "SELECT id, status, payload, result, coalesce_key, attempt, failures, input_versions "
        "FROM jobs WHERE story_id = :s AND kind = 'consolidation'"
    )
    params: dict[str, Any] = {"s": story_id}
    if status is not None:
        query += " AND status = :st"
        params["st"] = status
    return await rows(engine, query + " ORDER BY created_at, id", **params)


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = 'consolidation' AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def claim(engine: AsyncEngine, story_id: str) -> ClaimedJob | None:
    """Claim the story's earliest due ``consolidation`` job."""
    now = clock.now()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                _CLAIM, {"s": story_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)}
            )
        ).one_or_none()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        story_id=row.story_id,
        payload=dict(row.payload or {}),
        attempt=int(row.attempt),
        failures=int(row.failures),
        due_at=row.due_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        coalesce_key=row.coalesce_key,
    )


async def run_jobs(
    engine: AsyncEngine, models: ModelAccessLayer, story_id: str, *, limit: int = 20
) -> list[Any]:
    """Run the story's due ``consolidation`` jobs one at a time through the worker's own
    ``execute`` with ``models`` bound; the outcomes in order."""
    consolidation.bind(models)
    outcomes: list[Any] = []
    for _ in range(limit):
        job = await claim(engine, story_id)
        if job is None:
            break
        outcomes.append(await worker(engine).execute(job))
    return outcomes


def calls(models: ModelAccessLayer, purpose: str = "consolidation") -> int:
    return sum(1 for call in models.stub.calls if call.purpose == purpose)


__all__ = [
    "PRINCIPAL",
    "Turn",
    "answer",
    "bulk_turns",
    "calls",
    "claim",
    "dials",
    "jobs",
    "outputs",
    "private_memory",
    "run_jobs",
    "story",
    "windows",
]
