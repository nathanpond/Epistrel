"""Helpers for the embedding port's tests (#138).

A pin retypes ``memories.embedding`` and builds an index, a schema change the shared session
database must never see, so these tests run on a database of their own
(``tests.conformance.conftest.migrated_database``), put back to unpinned by ``reset`` before
each test. ``structured_story`` makes a story on the ``Structured (M4)`` profile and ``say``
plays a turn whose commit writes its episodic memory (and enqueues its ``embed`` job);
``run_jobs`` runs the story's due jobs of one kind through the worker's own ``execute``.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.db import make_engine
from epistrel.core.retrieval import Hit
from epistrel.engine import Engine
from epistrel.orchestrator.jobs.handlers import reembed as reembed_job
from epistrel.orchestrator.jobs.types import ClaimedJob
from tests.auth_support import auth_settings
from tests.canon_support import world
from tests.conformance.conftest import migrated_database
from tests.event_support import rows
from tests.memory_support import turn, use_structured
from tests.model_support import config_key
from tests.summary_support import LEASE_S, worker


@dataclass
class Scratch:
    """A database of its own, the REST app over it (no lifespan: no worker runs unless a test
    runs one) and the library Engine behind the app."""

    settings: Settings
    db: AsyncEngine
    client: httpx.AsyncClient
    lib: Engine


@asynccontextmanager
async def embedding_db(pg_url: str, **overrides: Any) -> AsyncIterator[Scratch]:
    """A fresh migrated database (dropped afterwards) with the stub adapters unless
    ``overrides`` say otherwise. The embedding jobs bind this Engine's service; the binding
    before is put back afterwards."""
    before = reembed_job.bound()
    values: dict[str, Any] = {"model_adapter": "stub", "config_key": config_key(), **overrides}
    with migrated_database(pg_url, f"epistrel_embed_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, **values)
        db = make_engine(settings)
        app = create_app(settings)
        lib: Engine = app.state.epistrel
        lib.bind(db)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield Scratch(settings, db, client, lib)
        finally:
            reembed_job.bind(before)
            await lib.drainer.stop()
            await lib.models.aclose()
            await db.dispose()


RESET = (
    "DELETE FROM jobs",
    "UPDATE deployment_state SET embedding_model_version = NULL, embedding_dimension = NULL, "
    "embedding_pinned_at = NULL, reembed_state = '{}'::jsonb, "
    "config_revision = config_revision + 1",
    "DROP INDEX IF EXISTS ix_memories_embedding_hnsw",
    "UPDATE memories SET embedding = NULL, embedding_model_version = NULL",
    "ALTER TABLE memories ALTER COLUMN embedding TYPE vector",
    "UPDATE stories SET embedding_model_version = NULL",
    "DELETE FROM model_config WHERE purpose <> 'default'",
)


async def reset(db: AsyncEngine) -> None:
    """The database as unpinned as migration 0053 leaves it (and no job queued)."""
    async with db.begin() as conn:
        for statement in RESET:
            await conn.execute(text(statement))


async def structured_story(db: AsyncEngine) -> tuple[str, dict[str, str]]:
    """A story on the ``Structured (M4)`` profile (its ``episodic`` module on): its id and
    cast."""
    w = await world(db)
    await use_structured(db, w.story_id)
    return w.story_id, dict(w.characters)


async def say(db: AsyncEngine, story_id: str, actor: str, line: str) -> str:
    """One turn in which ``actor`` says ``line``: answers its memory's id."""
    _, written = await turn(db, story_id, actor=actor, say=line, reply=None)
    (memory,) = written
    return memory.id


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = :kind AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def run_jobs(db: AsyncEngine, kind: str, story_id: str, *, limit: int = 10) -> list[Any]:
    """Run the story's due ``kind`` jobs one at a time through the worker's ``execute``; the
    outcomes in order."""
    outcomes: list[Any] = []
    for _ in range(limit):
        now = clock.now()
        async with db.begin() as conn:
            row = (
                await conn.execute(
                    _CLAIM,
                    {
                        "kind": kind,
                        "s": story_id,
                        "now": now,
                        "lease": now + timedelta(seconds=LEASE_S),
                    },
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
        outcomes.append(await worker(db).execute(job))
    return outcomes


async def jobs_of(db: AsyncEngine, kind: str, story_id: str) -> list[Any]:
    return await rows(
        db,
        "SELECT id, status, result, failures, coalesce_key FROM jobs "
        "WHERE kind = :k AND story_id = :s ORDER BY created_at, id",
        k=kind,
        s=story_id,
    )


async def vectors(db: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The story's memories by id: ``vector`` (text, or ``None``), ``version``, ``dims``."""
    found = await rows(
        db,
        "SELECT id, text, embedding::text AS vector, embedding_model_version AS version, "
        "vector_dims(embedding) AS dims, retrieval_tier, membership FROM memories "
        "WHERE story_id = :s ORDER BY created_at, id",
        s=story_id,
    )
    return {str(r.id): r for r in found}


async def pin_of(db: AsyncEngine) -> Any:
    (found,) = await rows(
        db,
        "SELECT embedding_model_version AS version, embedding_dimension AS dimension, "
        "embedding_pinned_at AS pinned_at, reembed_state FROM deployment_state",
    )
    return found


async def column_type(db: AsyncEngine) -> str:
    (found,) = await rows(
        db,
        "SELECT format_type(a.atttypid, a.atttypmod) AS type FROM pg_attribute a "
        "WHERE a.attrelid = 'memories'::regclass AND a.attname = 'embedding'",
    )
    return str(found.type)


async def hnsw_index(db: AsyncEngine) -> str | None:
    found = await rows(
        db, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_memories_embedding_hnsw'"
    )
    return str(found[0].indexdef) if found else None


async def story_version(db: AsyncEngine, story_id: str) -> str | None:
    (found,) = await rows(
        db, "SELECT embedding_model_version AS v FROM stories WHERE id = :s", s=story_id
    )
    return None if found.v is None else str(found.v)


def ids(hits: tuple[Hit, ...]) -> list[str]:
    return [h.memory_id for h in hits]


__all__ = [
    "Scratch",
    "column_type",
    "embedding_db",
    "hnsw_index",
    "ids",
    "jobs_of",
    "pin_of",
    "reset",
    "run_jobs",
    "say",
    "story_version",
    "structured_story",
    "vectors",
]
