"""Scratch scenes #157's suites build through the engine's own paths.

- ``structured(engine, ctx, story_id, dials)``: put a scratch story on the seeded
  ``Structured`` profile's latest version (``Engine.assign_engine_profile``) with dial overrides
  (``Engine.set_profile_overrides``), as an author would.
- ``add_entities``: the story's entities (#131's store), the cast's own with their character.
- ``episodes``: turns whose commit writes their episodic memories through
  ``core.memory.records.write_episodic`` (each a ``do`` beat of its actor), the
  writer a structured turn's commit uses, so consolidation and retrieval read real rows.

A suite's shared scene (built once in ``Suite.setup``) is a ``runner.Scratch`` of its own: its
stories are named ``scratch-eval-…`` and owned by ``system:evals``, so the run's sweep deletes
them even when ``teardown`` does not run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.memory import records
from epistrel.core.profiles.story import ProfileAssignment, ProfileOverridesPatch
from epistrel.core.propositions import store
from epistrel.core.seeds.profiles import STRUCTURED_PROFILE_ID
from epistrel.core.stories import CreatedStory
from epistrel.core.tables.jobs import jobs
from epistrel.core.tables.model_config import model_config
from epistrel.engine import Engine
from epistrel.orchestrator.embeddings.adapter import EMBEDDING
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import reembed
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext

PRINCIPAL = "system:evals"


async def structured(
    engine: Engine,
    ctx: RequestContext,
    story_id: str,
    dials: Mapping[str, Any] | None = None,
) -> None:
    """Run ``story_id`` on the seeded ``Structured`` profile's latest version with ``dials`` (a
    settings merge patch)."""
    await engine.assign_engine_profile(
        ctx,
        story_id,
        ProfileAssignment(profile_id=STRUCTURED_PROFILE_ID, ignore_warnings=True),
    )
    if dials:
        await engine.set_profile_overrides(
            ctx,
            story_id,
            ProfileOverridesPatch.model_validate(
                {"settings": dict(dials), "ignore_warnings": True}
            ),
        )


@dataclass(frozen=True, slots=True)
class EntitySpec:
    name: str
    kind: str = "person"
    gender: str | None = None


async def add_entities(
    engine: Engine,
    created: CreatedStory,
    extra: Sequence[EntitySpec] = (),
    *,
    genders: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """An entity for each cast member (linked to its character) and each of ``extra``; name →
    entity id."""
    found: dict[str, str] = {}
    async with engine.db.begin() as conn:
        for character in created.characters:
            found[character.name] = await store.add_entity(
                conn,
                created.story_id,
                "person",
                character.name,
                character_id=character.id,
                gender=(genders or {}).get(character.name),
            )
        for spec in extra:
            found[spec.name] = await store.add_entity(
                conn, created.story_id, spec.kind, spec.name, gender=spec.gender
            )
    return found


async def episodes(
    engine: Engine, story_id: str, lines: Sequence[tuple[str, str]]
) -> list[records.Memory]:
    """One turn per ``(actor character id, text)``: the actor's ``do``, its episodic memory
    (``<Name> does: <text>``) written by the turn's commit (``write_episodic``)."""
    written: list[records.Memory] = []
    for actor, line in lines:
        async with Operation(
            engine.db, story_id=story_id, kind=OperationKind.TURN, principal=PRINCIPAL
        ) as op:
            op.append(
                "player_input",
                {"text": line, "say": None, "do": line},
                provenance=Provenance.PLAYER,
                character_id=actor,
                beat=1,
            )

            async def step(conn: AsyncConnection, op: Operation = op) -> None:
                beats = await records.beats_of_operation(conn, op.operation_id)
                written.extend(await records.write_episodic(conn, op, beats))

            op.add_commit_step(step)
            await op.commit()
    return written


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'evals', updated_at = :now WHERE id = :j AND status = 'queued' RETURNING *"
)
LEASE_S = 600.0
MAX_EMBED_ROUNDS = 200


async def embedding_configured(engine: Engine) -> bool:
    """Whether the deployment names an embedding model (``model_config:embedding``, set by the
    CLI's ``--embedding-model``): only then do the retrieval suites embed their stories."""
    async with engine.db.connect() as conn:
        found = await conn.scalar(
            select(model_config.c.model).where(model_config.c.purpose == EMBEDDING)
        )
    return bool(found)


async def drain_embeddings(engine: Engine, story_id: str) -> int:
    """Run the story's ``embed`` job in process until no memory of it lacks a vector (#138's
    job, claimed here as a worker would: ``Engine`` binds its embedding service); the runs."""
    async with engine.db.begin() as conn:
        await reembed.schedule(conn, reembed.EMBED, story_id)
    runs = 0
    for _ in range(MAX_EMBED_ROUNDS):
        async with engine.db.connect() as conn:
            job_id = await conn.scalar(
                select(jobs.c.id).where(
                    jobs.c.story_id == story_id,
                    jobs.c.kind == reembed.EMBED,
                    jobs.c.status == "queued",
                )
            )
        if job_id is None:
            break
        now = clock.now()
        async with engine.db.begin() as conn:
            row = (
                await conn.execute(
                    _CLAIM, {"j": job_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)}
                )
            ).one()
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
        ctx = JobContext(db=engine.db, job=job, payload=NoPayload())
        try:
            output = await reembed.EMBED_HANDLER.run(ctx)
            await queue.commit_job(
                engine.db, job, reembed.EMBED_HANDLER, output, ctx.input_versions
            )
        except Exception as exc:
            # A job left running would hold the story: the sweep could not delete it.
            await queue.fail_job(engine.db, job, type(exc).__name__, max_attempts=1, terminal=True)
            raise
        runs += 1
    return runs


__all__ = [
    "PRINCIPAL",
    "EntitySpec",
    "add_entities",
    "drain_embeddings",
    "embedding_configured",
    "episodes",
    "structured",
]
