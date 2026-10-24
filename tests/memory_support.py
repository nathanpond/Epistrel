"""Helpers for the memory-record tests (#135): a story on the seeded ``Structured (M4)`` profile
(episodic memory on), turns whose commit writes their episodic memories the way the structured
pipeline's commit does (``write_episodic`` in a commit step), a test-only semantic memory
written the way #143's consolidation will, the rows, and the story's importance jobs run in the
test (no background worker)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.enums import MemoryKind, OperationKind, Provenance, RecordProvenance
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import records
from epistrel.core.seeds.profiles import STRUCTURED_M4_PROFILE_ID, STRUCTURED_M4_VERSION
from epistrel.core.tables.memories import memories
from epistrel.orchestrator.jobs.handlers import importance
from epistrel.orchestrator.jobs.types import ClaimedJob
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.event_support import execute_in_tx, rows
from tests.summary_support import LEASE_S, worker

PRINCIPAL = "p-test"


async def use_structured(engine: AsyncEngine, story_id: str) -> None:
    """Put the story on the seeded ``Structured (M4)`` profile (before any turn)."""
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET engine_profile_id = :p, engine_profile_version_pin = :v "
            "WHERE id = :s",
            {"s": story_id, "p": STRUCTURED_M4_PROFILE_ID, "v": STRUCTURED_M4_VERSION},
        ),
    )


async def turn(
    engine: AsyncEngine,
    story_id: str,
    *,
    actor: str | None = None,
    say: str | None = "Hello there.",
    do: str | None = None,
    reply: str | None = "A reply.",
    voiced: Sequence[str] = (),
    think: tuple[str, str] | None = None,
    latent: tuple[str, str] | None = None,
) -> tuple[Operation, list[records.Memory]]:
    """One turn: the player's input (``say``/``do`` by ``actor``, beat 1), a reply voiced by
    ``voiced`` (beat 2), and optionally a ``think`` and a ``latent`` beat (``(character, text)``);
    its commit writes the episodic memories of its beats. Answers the operation and them."""
    found: dict[str, list[records.Memory]] = {}
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        if say is not None or do is not None:
            said = "\n".join(p for p in (say, do) if p)
            op.append(
                "player_input",
                {"text": said, "say": say, "do": do},
                provenance=Provenance.PLAYER,
                character_id=actor,
                beat=1,
            )
        if reply is not None:
            op.append(
                "message_rendered", {"text": reply, "render_character_ids": list(voiced)}, beat=2
            )
        if think is not None:
            op.append(
                "think", {"character_id": think[0], "text": think[1]}, character_id=think[0], beat=3
            )
        if latent is not None:
            op.append(
                "latent",
                {"character_id": latent[0], "text": latent[1]},
                character_id=latent[0],
                beat=4,
            )

        async def step(conn: AsyncConnection) -> None:
            beats = await records.beats_of_operation(conn, op.operation_id)
            found["memories"] = await records.write_episodic(conn, op, beats)

        op.add_commit_step(step)
        await op.commit()
    return op, found.get("memories", [])


async def derived_memory(
    engine: AsyncEngine,
    story_id: str,
    text_: str,
    sources: Sequence[str],
    *,
    kind: MemoryKind = MemoryKind.SEMANTIC,
    holder: str | None = None,
) -> str:
    """A position-less memory of ``kind`` over the events ``sources``, written by a
    ``consolidation`` operation the way #143's consolidation will (test-only: #135 ships no
    semantic writer). Answers its id."""
    memory_id = new_ulid()
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.CONSOLIDATION, principal=PRINCIPAL
    ) as op:

        async def step(conn: AsyncConnection) -> None:
            row = {
                "id": memory_id,
                "story_id": story_id,
                "kind": kind.value,
                "text": text_,
                "holder_character_id": holder,
                "character_id": None,
                "visibility": {"scope": "objective"},
                "provenance": RecordProvenance.SYSTEM_CONSOLIDATED.value,
                "trust_tier": records.trust_tier_of(RecordProvenance.SYSTEM_CONSOLIDATED).value,
                "operation_id": op.operation_id,
                "source_event_ids": list(sources),
                "narrative_turn": None,
                "turn_variant": None,
            }
            await insert_derived(conn, memories, [row])
            await index_records(conn, [memory_record(SimpleNamespace(**row))])

        op.add_commit_step(step)
        await op.commit()
    return memory_id


async def memory_rows(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The story's ``memories`` rows by id (``embedding`` as text), in writing order."""
    found = await rows(
        engine,
        "SELECT *, embedding::text AS vector FROM memories WHERE story_id = :s "
        "ORDER BY narrative_turn NULLS FIRST, beat NULLS FIRST, created_at, id",
        s=story_id,
    )
    return {str(r.id): r for r in found}


async def embed_all(engine: AsyncEngine, story_id: str, vector: str = "[0.25,0.5,0.75]") -> None:
    """Give every memory of the story a vector (#138's embed job, faked)."""
    await execute_in_tx(
        engine,
        (
            "UPDATE memories SET embedding = CAST(:v AS vector), "
            "embedding_model_version = 'stub-embed@1' WHERE story_id = :s",
            {"s": story_id, "v": vector},
        ),
    )


async def event_ids(engine: AsyncEngine, operation_id: str) -> dict[str, str]:
    """The operation's beat events, ``{type: id}`` (one of each type)."""
    found = await rows(
        engine,
        "SELECT id, type FROM events WHERE operation_id = :o ORDER BY seq",
        o=operation_id,
    )
    return {str(r.type): str(r.id) for r in found}


async def importance_jobs(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT id, status, result, coalesce_key FROM jobs "
        "WHERE story_id = :s AND kind = 'importance_scoring' ORDER BY created_at, id",
        s=story_id,
    )


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = 'importance_scoring' AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def run_importance(
    engine: AsyncEngine, models: ModelAccessLayer, story_id: str, *, limit: int = 10
) -> list[Any]:
    """Run the story's due ``importance_scoring`` jobs one at a time through the worker's own
    ``execute`` with ``models`` bound; the outcomes in order."""
    importance.bind(models)
    outcomes: list[Any] = []
    for _ in range(limit):
        now = clock.now()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    _CLAIM, {"s": story_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)}
                )
            ).one_or_none()
        if row is None:
            break
        job = ClaimedJob(
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
        outcomes.append(await worker(engine).execute(job))
    return outcomes


__all__ = [
    "PRINCIPAL",
    "derived_memory",
    "embed_all",
    "event_ids",
    "importance_jobs",
    "memory_rows",
    "run_importance",
    "turn",
    "use_structured",
]
