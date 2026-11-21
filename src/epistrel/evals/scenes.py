"""Scratch scenes #157's suites build through the engine's own paths.

- ``structured(engine, ctx, story_id, dials)``: put a scratch story on the run's preset (#227;
  Full, the seeded structured row, by default) at its latest version
  (``Engine.assign_engine_profile``) with dial overrides (``Engine.set_profile_overrides``), as
  an author would.
- ``add_entities``: the story's entities (#131's store), the cast's own with their character.
- ``episodes``: turns whose commit writes their episodic memories through
  ``core.memory.records.write_episodic`` (each a ``do`` beat of its actor), the
  writer a structured turn's commit uses, so consolidation and retrieval read real rows.

A suite's shared scene (built once in ``Suite.setup``) is a ``runner.Scratch`` of its own: its
stories are named ``scratch-eval-…`` and owned by ``system:evals``, so the run's sweep deletes
them even when ``teardown`` does not run.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock, embeddings
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.memory import records, story_partitions
from epistrel.core.profiles.story import ProfileAssignment, ProfileOverridesPatch
from epistrel.core.propositions import store
from epistrel.core.settings_schema import merge_patch
from epistrel.core.stories import CreatedStory
from epistrel.core.tables.jobs import jobs
from epistrel.core.tables.model_config import model_config
from epistrel.engine import Engine
from epistrel.evals import busy
from epistrel.evals.presets import RUN_PRESET
from epistrel.orchestrator.embeddings.adapter import EMBEDDING
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import memory_partitions, reembed
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext, JobHandler

PRINCIPAL = "system:evals"


async def structured(
    engine: Engine,
    ctx: RequestContext,
    story_id: str,
    dials: Mapping[str, Any] | None = None,
) -> None:
    """Run ``story_id`` on the run's preset (``presets.RUN_PRESET``, #227: Full unless the run
    names another) at its latest version, with ``dials`` (a settings merge patch)."""
    preset = RUN_PRESET.get()
    await engine.assign_engine_profile(
        ctx,
        story_id,
        ProfileAssignment(profile_id=preset.profile_id, ignore_warnings=True),
    )
    if preset.overrides:
        dials = merge_patch(dict(preset.overrides), dict(dials or {}))
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
        # #929: a commit refused with StoryBusy wrote nothing, so the turn is built again.
        written.extend(
            await busy.retrying(functools.partial(_episode, engine, story_id, actor, line))
        )
    return written


async def _episode(engine: Engine, story_id: str, actor: str, line: str) -> list[records.Memory]:
    kept: list[records.Memory] = []
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
            kept.extend(await records.write_episodic(conn, op, beats))

        op.add_commit_step(step)
        await op.commit()
    return kept


log = logging.getLogger(__name__)

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


def error_label(exc: BaseException) -> str:
    """#970: a failed in-process job's error: the exception's class name and, for a database
    error, its SQLSTATE and condition name (``OperationalError (55P03 lock_not_available)``).
    Never the server's message text, which can quote row values (invariant 3)."""
    label = type(exc).__name__
    orig = getattr(exc, "orig", exc)
    state = getattr(orig, "sqlstate", None)
    if isinstance(state, str) and state:
        name = getattr(getattr(orig, "diag", None), "condition_name", None)
        name = name or _CONDITIONS.get(state, "")
        label += f" ({state} {name})" if name else f" ({state})"
    return label


_CONDITIONS = {
    "55P03": "lock_not_available",
    "57014": "query_canceled",
    "40P01": "deadlock_detected",
    "53100": "disk_full",
    "53200": "out_of_memory",
}
"""Condition names for the SQLSTATEs a retype or a partition drop meets, when the driver's
diagnostics carry none."""


async def _run_claimed(
    engine: Engine, job_id: str, handler: JobHandler, payload: Any, *, terminal: bool = True
) -> bool:
    """Claim the queued job ``job_id`` as a worker would and run ``handler`` on it; ``False``
    when another claim got there first. A failure is recorded (``error_label``; ``terminal``:
    failed at once, else retried by the queue's backoff rules) and raised: a job left running
    would hold its story."""
    now = clock.now()
    async with engine.db.begin() as conn:
        row = (
            await conn.execute(
                _CLAIM, {"j": job_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)}
            )
        ).one_or_none()
    if row is None:
        return False
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
    ctx = JobContext(db=engine.db, job=job, payload=payload)
    try:
        output = await handler.run(ctx)
        await queue.commit_job(engine.db, job, handler, output, ctx.input_versions)
    except Exception as exc:
        attempts = 1 if terminal else (handler.max_attempts or engine.settings.job_max_attempts)
        await queue.fail_job(
            engine.db, job, error_label(exc), max_attempts=attempts, terminal=terminal
        )
        raise
    return True


async def drop_partition(engine: Engine, story_id: str) -> bool:
    """#970: run a deleted story's queued ``memory_partition_drop`` job (#865) in process, as
    ``drain_embeddings`` runs ``embed``: the harness has no worker, and a certify run's
    undropped partitions made the first embedding pin retype thousands of them and filled the
    container's ``/dev/shm``. ``False``: no such job was queued, or it failed (a lock timeout
    under concurrent cases): the failure is logged and the job left to its retries, never the
    case's error, and ``sweep_partitions`` drops what is left."""
    key = memory_partitions.coalesce_key(story_id)
    async with engine.db.connect() as conn:
        job_id = await conn.scalar(
            select(jobs.c.id).where(jobs.c.coalesce_key == key, jobs.c.status == "queued")
        )
    if job_id is None:
        return False
    payload = memory_partitions.DropPayload(story_id=story_id)
    handler = memory_partitions.MEMORY_PARTITION_DROP_HANDLER
    try:
        return await _run_claimed(engine, job_id, handler, payload, terminal=False)
    except Exception as exc:
        log.warning("eval partition drop failed: %s", error_label(exc))
        return False


async def sweep_partitions(engine: Engine) -> dict[str, int]:
    """#970: ``story_partitions.sweep_orphans`` (the hourly sweep a worker would run): every
    partition whose story is gone is dropped. The run's start and end call it."""
    return await story_partitions.sweep_orphans(engine.db)


async def pin_embedding(engine: Engine) -> embeddings.Pin | None:
    """#970: pin the deployment's embedding (probe once, then ``embeddings.pin_first``) before
    the run creates any story, so the first pin's ``memories`` retype runs on an empty table.
    ``None`` when no embedding model is configured; a deployment already pinned is checked, not
    moved (a dimension mismatch raises)."""
    if not await embedding_configured(engine):
        return None
    probe = await engine.models.embeddings.probe()
    async with engine.db.begin() as conn:
        return await embeddings.pin_first(conn, version=probe.version, dimension=probe.dimension)


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
        if await _run_claimed(engine, job_id, reembed.EMBED_HANDLER, NoPayload()):
            runs += 1
    return runs


__all__ = [
    "PRINCIPAL",
    "EntitySpec",
    "add_entities",
    "drain_embeddings",
    "drop_partition",
    "embedding_configured",
    "episodes",
    "error_label",
    "pin_embedding",
    "structured",
    "sweep_partitions",
]
