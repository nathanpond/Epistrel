"""``embed`` and ``reembed``: the jobs that write vectors (FR-EMBED-2,3,4, FR-STORE-30; #138).

Vectors are derived data, written only here, never inside a turn.

**``embed``** gives new memories their vectors. One coalesced job per story (``coalesce_key``
``embed:<story>``), enqueued inside the transaction that writes memories
(``core.memory.records.MEMORY_HOOKS``: the turn's commit), so a turn never waits for an
embedding; the memories are searched lexically meanwhile. A run takes the story's hot active
memories with no vector.

**``reembed``** moves a story into the pinned vector space after a model change. A change of
``model_config:embedding.model`` (``PUT /admin/model-config/embedding``; the write probed the new
model first, ``CHANGE``) runs ``on_embedding_model_changed`` in the write's transaction: it moves
the pin (``core.embeddings.change_model``) and enqueues one ``reembed`` job per story whose
vectors are in some version (``coalesce_key`` ``reembed:<story>``, most recently written first,
on the write's own connection). A second change while a job is still queued reuses it (the job
reads the pin when it runs); a running job finishes its batch and re-checks the pin before
writing. Until a story's job finishes, the story's searches are lexical-only
(``embedding_pending``). A run takes the story's hot active memories whose vector is missing or
in another version; retired and dormant rows are not re-embedded (their vectors were nulled;
they re-embed on rehydration, #147).

**A run** (both kinds) does nothing unless the story's ``episodic`` module is on. In one
snapshot it reads the pin and up to ``reembed_batch_size`` (500) memories, oldest first,
declaring each as an input (``memories:<id>``: membership, membership time, tier and text
digest, so an erasure or a truncation in between drops the output). Outside the snapshot it
embeds them through the Model Access Layer (``ModelAccessLayer.embeddings``, batches of
``embedding_batch_max``). Unpinned, the first embedding pins the deployment in a short
transaction of its own (``core.embeddings.pin_first``: the column's type and the HNSW index);
pinned, a vector of another dimension is 503 ``embedding_dimension_mismatch`` and another vector
space ``embedding_version_mismatch``, and nothing is stored. Its ``maintenance`` operation then
commits the vectors (``core.embeddings.stamp``) under the pin held ``FOR SHARE``: if the pin
moved meanwhile nothing is written and the job is queued again. When the batch was the story's
last, the story's version advances to the pin (an ``embed`` run only sets it the first time);
otherwise the job enqueues itself again in the same transaction.

**Status** (``status``, ``GET /admin/reembed/status``): the last model change's versions and
start, and per story its latest ``reembed`` job's status (``queued``, ``running``, ``done``,
``failed`` with the exception class) with its hot memories counted (``total``) and how many are
in the new version (``done``); ``finished_at`` once no story's job is queued or running.
``retry`` (``POST /admin/reembed/retry``) enqueues a new job for every story whose latest one
failed.

Results, logs and metrics carry ids, versions and counts only (invariant 3). ``bind(service)``
gives the jobs and the listener the process's embedding service (``Engine``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import embeddings
from epistrel.core.config_changes import (
    RecordedChange,
    read_section,
    register_change_listener,
)
from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation
from epistrel.core.membership import active_only_sql
from epistrel.core.memory import records
from epistrel.core.settings_schema import EmbeddingSettings
from epistrel.core.tables import model_config, stories
from epistrel.orchestrator.embeddings.adapter import EMBEDDING, EmbeddingIdentity, EmbeddingProbe
from epistrel.orchestrator.embeddings.service import EmbeddingService
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.config import EffectiveConfig, config_setting

logger = logging.getLogger(__name__)

EMBED = "embed"
REEMBED = "reembed"
MAX_ATTEMPTS = 3
STATUS_PAGE_MAX = 200
ACTIVE = active_only_sql("m")

_bound: list[EmbeddingService] = []


class NoEmbeddings(RuntimeError):
    """No embedding service is bound in this process (``bind``)."""


def bind(service: EmbeddingService | None) -> None:
    """Make ``service`` the one the jobs and the model-change listener use; ``None`` unbinds."""
    _bound.clear()
    if service is not None:
        _bound.append(service)


def bound() -> EmbeddingService | None:
    return _bound[0] if _bound else None


@dataclass(frozen=True)
class ChangeContext:
    """What a configuration write knows about the new embedding model: the service it was
    probed through and the probe (its dimension), set by the write (``changing``)."""

    service: EmbeddingService
    probe: EmbeddingProbe | None


CHANGE: ContextVar[ChangeContext | None] = ContextVar("EMBEDDING_CHANGE", default=None)


@contextmanager
def changing(service: EmbeddingService, probe: EmbeddingProbe | None) -> Iterator[None]:
    """Run a configuration write that may change the embedding model with its probe."""
    token = CHANGE.set(ChangeContext(service, probe))
    try:
        yield
    finally:
        CHANGE.reset(token)


def coalesce_key(kind: str, story_id: str) -> str:
    return f"{kind}:{story_id}"


async def schedule(conn: AsyncConnection, kind: str, story_id: str) -> None:
    """Enqueue the story's ``kind`` job in ``conn``'s transaction (coalesced)."""
    await queue.enqueue(
        conn, kind, NoPayload(), story_id=story_id, coalesce_key=coalesce_key(kind, story_id)
    )


async def on_memories_written(
    conn: AsyncConnection, story_id: str, memory_ids: tuple[str, ...]
) -> None:
    """``core.memory.records``' hook: new memories get their vectors in the background."""
    if memory_ids:
        await schedule(conn, EMBED, story_id)


# -- the model-change listener ------------------------------------------------------------------


async def _own_row(conn: AsyncConnection, revision: int) -> EffectiveConfig:
    row = (
        await conn.execute(select(model_config).where(model_config.c.purpose == EMBEDDING))
    ).one_or_none()
    if row is None:
        return EffectiveConfig(EMBEDDING, None, None, None, ModelParams(), revision)
    params = ModelParams.model_validate(dict(row.params or {}))
    return EffectiveConfig(
        EMBEDDING, row.base_url, row.model, row.api_key_ciphertext, params, revision
    )


async def on_embedding_model_changed(conn: AsyncConnection, change: RecordedChange) -> None:
    """A change of the embedding model (see the module docstring)."""
    if change.new is None:
        return  # unconfigured: the next embedding fails ``not_configured``; the pin stays
    context = CHANGE.get()
    service = context.service if context is not None else bound()
    if service is None:
        raise NoEmbeddings("no embedding service is bound to follow the embedding model change")
    config = await _own_row(conn, change.config_revision)
    identity = service.adapter.identity(config)
    probe = context.probe if context is not None else None
    story_ids = await embeddings.change_model(
        conn,
        version=identity.version,
        dimension=probe.dimension if probe is not None else None,
        config_revision=change.config_revision,
        actor=change.actor,
    )
    for story_id in story_ids:
        await schedule(conn, REEMBED, story_id)
    if story_ids:
        logger.info("embedding model changed; %d re-embed job(s) enqueued", len(story_ids))


register_change_listener(config_setting(EMBEDDING, "model"), on_embedding_model_changed)


# -- the run ------------------------------------------------------------------------------------


async def _first_pin(
    db: AsyncEngine, identity: EmbeddingIdentity, dimension: int
) -> embeddings.Pin:
    async with db.begin() as conn:
        return await embeddings.pin_first(conn, version=identity.version, dimension=dimension)


async def _run(ctx: JobContext) -> JobOutput:
    kind = ctx.job.kind
    story_id = ctx.job.story_id
    if story_id is None:
        return JobOutput(result={"embedded": 0, "calls": 0})
    only_missing = kind == EMBED
    async with ctx.snapshot() as conn:
        if not await records.episodic_on(conn, story_id):
            return JobOutput(result={"embedded": 0, "calls": 0, "module": "off"})
        pin = await embeddings.read_pin(conn)
        limit = (await read_section(conn, EmbeddingSettings)).reembed_batch_size
        found = await embeddings.pending_memories(
            conn, story_id, version=pin.version, only_missing=only_missing, limit=limit + 1
        )
        batch = found[:limit]
        for memory in batch:
            await ctx.declare_input(conn, embeddings.memory_input(memory.id))
    more = len(found) > len(batch)
    vectors: dict[str, tuple[float, ...]] = {}
    calls = 0
    if batch:
        service = bound()
        if service is None:
            raise NoEmbeddings(f"no embedding service is bound for the {kind} job")
        embedded = await service.embed([memory.text for memory in batch])
        calls = embedded.calls
        if pin.pinned:
            embeddings.check_dimension(pin, embedded.dimension, model=embedded.identity.model)
            if not embedded.identity.matches(str(pin.version)):
                raise embeddings.EmbeddingVersionMismatch(
                    pinned_version=str(pin.version), version=embedded.identity.version
                )
        else:
            pin = await _first_pin(ctx.db, embedded.identity, embedded.dimension)
        vectors = {m.id: v for m, v in zip(batch, embedded.vectors, strict=True)}
    elif not pin.pinned:
        return JobOutput(result={"embedded": 0, "calls": 0})
    written: dict[str, Any] = {"embedded": 0}

    def stage(op: Operation) -> None:
        async def step(conn: AsyncConnection) -> None:
            current = await embeddings.read_pin(conn, share=True)
            if (current.version, current.dimension) != (pin.version, pin.dimension):
                written["superseded"] = True
                await schedule(conn, kind, story_id)
                return
            written["embedded"] = await embeddings.stamp(conn, story_id, vectors, current)
            if more:
                await schedule(conn, kind, story_id)
                return
            version = await embeddings.story_version(conn, story_id)
            if kind == REEMBED or (version is None and vectors):
                await embeddings.advance_story(conn, story_id, str(current.version))

        op.add_commit_step(step)

    return JobOutput(
        stage=stage,
        result={"calls": calls, "more": more, "version": pin.version},
        result_of=lambda: dict(written),
    )


EMBED_HANDLER = JobHandler(
    kind=EMBED,
    payload=NoPayload,
    run=_run,
    operation_kind=OperationKind.MAINTENANCE,
    max_attempts=MAX_ATTEMPTS,
)
REEMBED_HANDLER = JobHandler(
    kind=REEMBED,
    payload=NoPayload,
    run=_run,
    operation_kind=OperationKind.MAINTENANCE,
    max_attempts=MAX_ATTEMPTS,
)

records.register_memory_hook(on_memories_written)


# -- status and retry ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoryReembed:
    """One story's re-embed: its latest job's status and its hot memories' progress."""

    story_id: str
    total: int
    done: int
    status: str
    error: str | None


@dataclass(frozen=True)
class ReembedStatus:
    """The last model change's re-embed run (``GET /admin/reembed/status``)."""

    from_version: str | None
    to_version: str | None
    started_at: datetime | None
    finished_at: datetime | None
    stories: tuple[StoryReembed, ...]
    next: str | None


_LATEST = """
SELECT DISTINCT ON (j.story_id) j.story_id, j.status, j.result, j.updated_at
FROM jobs j
WHERE j.kind = 'reembed' AND j.story_id IS NOT NULL AND j.created_at >= :started
ORDER BY j.story_id, j.created_at DESC, j.id DESC
"""


async def status(
    conn: AsyncConnection, *, limit: int = 50, after: str | None = None
) -> ReembedStatus:
    """The re-embed run's status, one page of stories by id after ``after``."""
    state = await embeddings.reembed_state(conn)
    started_raw = state.get("started_at")
    if not started_raw:
        return ReembedStatus(None, None, None, None, (), None)
    started = datetime.fromisoformat(str(started_raw))
    to_version = state.get("to_version")
    page = max(1, min(limit, STATUS_PAGE_MAX))
    found = await conn.execute(
        text(
            f"WITH latest AS ({_LATEST}) "  # noqa: S608 - a fixed query
            "SELECT l.story_id, l.status, l.result, "
            "(SELECT count(*) FROM memories m WHERE m.story_id = l.story_id "
            f"AND {ACTIVE} AND m.retrieval_tier = 'hot') AS total, "
            "(SELECT count(*) FROM memories m WHERE m.story_id = l.story_id "
            f"AND {ACTIVE} AND m.retrieval_tier = 'hot' "
            "AND m.embedding IS NOT NULL AND m.embedding_model_version = :to) AS done "
            "FROM latest l WHERE (CAST(:after AS text) IS NULL OR l.story_id > :after) "
            "ORDER BY l.story_id LIMIT :limit"
        ),
        {"started": started, "to": to_version, "after": after, "limit": page + 1},
    )
    rows = list(found)
    more = len(rows) > page
    rows = rows[:page]
    summary = (
        await conn.execute(
            text(
                f"WITH latest AS ({_LATEST}) "  # noqa: S608 - a fixed query
                "SELECT count(*) FILTER (WHERE status IN ('queued', 'running')) AS open, "
                "max(updated_at) AS last FROM latest"
            ),
            {"started": started},
        )
    ).one()
    finished = summary.last if int(summary.open or 0) == 0 and summary.last is not None else None
    return ReembedStatus(
        from_version=state.get("from_version"),
        to_version=to_version,
        started_at=started,
        finished_at=finished,
        stories=tuple(
            StoryReembed(
                story_id=str(r.story_id),
                total=int(r.total),
                done=int(r.done),
                status=str(r.status),
                error=(r.result or {}).get("error") if r.status == queue.FAILED else None,
            )
            for r in rows
        ),
        next=str(rows[-1].story_id) if more and rows else None,
    )


@dataclass(frozen=True)
class ReembedRetry:
    """``POST /admin/reembed/retry``: the stories a new re-embed job was enqueued for."""

    retried: list[str]
    config_revision: int
    replayed: bool = False


async def retry(conn: AsyncConnection) -> list[str]:
    """Enqueue a new ``reembed`` job for every story whose latest one failed (in ``conn``'s
    transaction); answers those stories."""
    state = await embeddings.reembed_state(conn)
    started_raw = state.get("started_at")
    if not started_raw:
        return []
    found = await conn.execute(
        text(f"SELECT story_id FROM ({_LATEST}) l WHERE l.status = 'failed' ORDER BY story_id"),  # noqa: S608 - a fixed query
        {"started": datetime.fromisoformat(str(started_raw))},
    )
    failed = [str(r.story_id) for r in found]
    retried: list[str] = []
    for story_id in failed:
        exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
        if exists is not None:
            await schedule(conn, REEMBED, story_id)
            retried.append(story_id)
    return retried


__all__ = [
    "CHANGE",
    "EMBED",
    "EMBED_HANDLER",
    "REEMBED",
    "REEMBED_HANDLER",
    "ChangeContext",
    "NoEmbeddings",
    "ReembedRetry",
    "ReembedStatus",
    "StoryReembed",
    "bind",
    "bound",
    "changing",
    "coalesce_key",
    "on_embedding_model_changed",
    "on_memories_written",
    "retry",
    "schedule",
    "status",
]
