"""The embedding pin and the vectors it governs (FR-EMBED-2,3, FR-STORE-30; docs/02 §6; #138).

The deployment has **one vector space at a time**: ``deployment_state.embedding_model_version``
and ``embedding_dimension`` (the **pin**). Core holds the rules; the Orchestrator makes the
model calls (``orchestrator.embeddings``) and runs the jobs that write vectors
(``orchestrator.jobs.handlers.reembed``).

- **The first embedding pins** (``pin_first``): in a short transaction of its own, under the
  deployment row's lock, it records the version and dimension it embedded with, one
  ``config_changes`` row (setting ``embedding.pin``, ``system``, prospective) and retypes
  ``memories.embedding`` from 0050's untyped ``vector`` to ``vector(d)`` with the HNSW cosine
  index (``retype``; pgvector indexes at most ``HNSW_MAX_DIMENSION`` dimensions, so a wider
  model is searched exactly, without an index). Never inside a turn: the embed job pins.
- **Every vector write stamps the pin** (``stamp``): a vector whose dimension is not the
  pinned one is refused with 503 ``embedding_dimension_mismatch`` and nothing is stored.
- **A model change moves the pin** (``change_model``, in the configuration write's
  transaction): the new version, and when the dimension changes every vector is nulled in one
  statement and the column retyped and reindexed. Retained retired and dormant vectors are
  nulled either way (FR-STORE-30: they re-embed on rehydration, #147). It answers the stories
  to re-embed: every non-archived story whose vectors are in some version.
- **Searches never mix spaces**: ``core.retrieval`` reads only vectors stamped with the pin's
  version; a story whose own version (``stories.embedding_model_version``, set at its first
  embedding, advanced by the re-embed job, ``advance_story``) differs from the pin is
  ``pending`` and is searched lexically only.

A memory is an input of the job that embeds it (``memories:<id>``): whether it is active, its
membership time, its retrieval tier and the MD5 of its text. An erasure that redacts it, or a
truncation that discards it, between the read and the commit makes the job drop its output, so
no vector of a text that no longer exists is ever written (the erasure nulls the vectors it
finds in its own transaction, #135).

Logs and results carry identifiers, versions and counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.config_changes import (
    bump_config_revision,
    lock_deployment,
    read_section,
    record_config_change,
)
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.enums import RetrievalTier, StoryStatus
from epistrel.core.errors import EpistrelError
from epistrel.core.input_versions import Version, input_key, register_version_reader
from epistrel.core.membership import active_only
from epistrel.core.settings_schema import ConfigClass, EmbeddingSettings
from epistrel.core.tables import deployment_state, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.memories import memories

HNSW_INDEX = "ix_memories_embedding_hnsw"
HNSW_MAX_DIMENSION = 2_000
"""pgvector's HNSW limit for ``vector``; a wider pin is searched without an index."""
PIN_SETTING = "embedding.pin"
"""The ``config_changes.setting`` of a pin's first write and of every move."""


class EmbeddingDimensionMismatch(EpistrelError):
    """A vector's dimension is not the pinned one: nothing is stored (FR-EMBED-3). ``details``:
    ``expected``, ``actual``, ``model`` and ``pinned_version``."""

    code = "embedding_dimension_mismatch"
    http_status = 503

    def __init__(self, *, expected: int, actual: int, model: str, pinned_version: str) -> None:
        super().__init__(
            f"the embedding model answered {actual} dimensions; the deployment is pinned at "
            f"{expected}",
            details={
                "expected": expected,
                "actual": actual,
                "model": model,
                "pinned_version": pinned_version,
            },
        )


class EmbeddingVersionMismatch(EpistrelError):
    """The configured embedding model embeds into another vector space than the pinned one,
    and no model change moved the pin (the adapter was swapped under a pinned deployment):
    nothing is stored. ``details``: ``pinned_version`` and ``version``."""

    code = "embedding_version_mismatch"
    http_status = 503

    def __init__(self, *, pinned_version: str, version: str) -> None:
        super().__init__(
            "the configured embedding model is not the one the deployment is pinned to; "
            "change the embedding model through /admin/model-config/embedding",
            details={"pinned_version": pinned_version, "version": version},
        )


@dataclass(frozen=True)
class Pin:
    """The deployment's vector space; both ``None`` until the first embedding."""

    version: str | None
    dimension: int | None
    pinned_at: datetime | None = None

    @property
    def pinned(self) -> bool:
        return self.version is not None and self.dimension is not None


_PIN = (
    deployment_state.c.embedding_model_version,
    deployment_state.c.embedding_dimension,
    deployment_state.c.embedding_pinned_at,
)


def _pin(row: Any) -> Pin:
    if row is None:
        return Pin(None, None)
    return Pin(row[0], None if row[1] is None else int(row[1]), row[2])


async def read_pin(conn: AsyncConnection, *, share: bool = False) -> Pin:
    """The pin as of ``conn``'s snapshot; ``share`` holds the deployment row ``FOR SHARE`` until
    ``conn`` commits, so no model change moves the pin under a vector write."""
    query = select(*_PIN).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    if share:
        query = query.with_for_update(read=True)
    return _pin((await conn.execute(query)).one_or_none())


async def _lock_pin(conn: AsyncConnection) -> Pin:
    found = (
        await conn.execute(
            select(*_PIN).where(deployment_state.c.id == DEPLOYMENT_ROW_ID).with_for_update()
        )
    ).one_or_none()
    return _pin(found)


def check_dimension(pin: Pin, actual: int, *, model: str) -> None:
    """Raise ``EmbeddingDimensionMismatch`` unless ``actual`` is the pinned dimension (an
    unpinned deployment accepts any)."""
    if pin.pinned and actual != pin.dimension:
        raise EmbeddingDimensionMismatch(
            expected=int(pin.dimension or 0),
            actual=actual,
            model=model,
            pinned_version=str(pin.version),
        )


# -- the column's type and its index ----------------------------------------------------------


async def retype(conn: AsyncConnection, dimension: int) -> None:
    """``memories.embedding`` as ``vector(dimension)`` with the HNSW cosine index (rebuilt from
    the deployment's ``embedding_hnsw_*`` settings). A stored vector of another dimension (none
    in a pinned deployment: a dimension change nulls every vector first) is nulled with its
    version, so the retype never fails. Takes ``memories``' table lock until ``conn`` commits."""
    if dimension < 1:
        raise ValueError("an embedding dimension is positive")
    settings = await read_section(conn, EmbeddingSettings)
    await conn.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
    await conn.execute(
        text(
            "UPDATE memories SET embedding = NULL, embedding_model_version = NULL "
            "WHERE embedding IS NOT NULL AND vector_dims(embedding) <> :d"
        ),
        {"d": dimension},
    )
    d = int(dimension)
    await conn.execute(
        text(
            f"ALTER TABLE memories ALTER COLUMN embedding TYPE vector({d}) "
            f"USING embedding::vector({d})"
        )
    )
    if d <= HNSW_MAX_DIMENSION:
        m, ef = int(settings.embedding_hnsw_m), int(settings.embedding_hnsw_ef_construction)
        await conn.execute(
            text(
                f"CREATE INDEX {HNSW_INDEX} ON memories USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = {m}, ef_construction = {ef})"
            )
        )


async def pin_first(
    conn: AsyncConnection, *, version: str, dimension: int, actor: str = SYSTEM_PRINCIPAL
) -> Pin:
    """Pin the deployment at ``version`` / ``dimension`` unless it is pinned already (see the
    module docstring). Pinned at another dimension: ``EmbeddingDimensionMismatch``. The caller
    commits ``conn``'s transaction (a short one of its own)."""
    current = await lock_deployment(conn)
    pin = await _lock_pin(conn)
    if pin.pinned:
        check_dimension(pin, dimension, model=version)
        return pin
    revision = await bump_config_revision(conn, current)
    at = clock.now()
    await conn.execute(
        update(deployment_state)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .values(
            embedding_model_version=version, embedding_dimension=dimension, embedding_pinned_at=at
        )
    )
    await record_config_change(
        conn,
        config_revision=revision,
        setting=PIN_SETTING,
        old=None,
        new={"version": version, "dimension": dimension},
        actor=actor,
        change_class=ConfigClass.prospective,
    )
    await retype(conn, dimension)
    return Pin(version, dimension, at)


async def change_model(
    conn: AsyncConnection,
    *,
    version: str,
    dimension: int | None,
    config_revision: int,
    actor: str,
) -> list[str]:
    """Move the pin to ``version`` (and ``dimension``, when known and different) inside the
    configuration write running in ``conn`` (which holds the deployment lock), and answer the
    stories to re-embed, most recently written first. Unpinned or unchanged: nothing moves and
    no story is answered (the next first embedding pins)."""
    pin = await _lock_pin(conn)
    if not pin.pinned:
        return []
    target = dimension if dimension is not None else int(pin.dimension or 0)
    if version == pin.version and target == pin.dimension:
        return []
    found = await conn.execute(
        select(stories.c.id)
        .outerjoin(memories, memories.c.story_id == stories.c.id)
        .where(
            stories.c.embedding_model_version.is_not(None),
            stories.c.status != StoryStatus.ARCHIVED.value,
        )
        .group_by(stories.c.id)
        .order_by(func.max(memories.c.created_at).desc().nulls_last(), stories.c.id)
    )
    story_ids = [str(r.id) for r in found]
    # Retained retired and dormant vectors re-embed on rehydration or reactivation (#147).
    await conn.execute(
        update(memories)
        .where(
            memories.c.embedding.is_not(None),
            or_(
                ~active_only(memories),
                memories.c.retrieval_tier != RetrievalTier.HOT.value,
            ),
        )
        .values(embedding=None, embedding_model_version=None)
    )
    if target != pin.dimension:
        await conn.execute(
            update(memories)
            .where(memories.c.embedding.is_not(None))
            .values(embedding=None, embedding_model_version=None)
        )
        await retype(conn, target)
    at = clock.now()
    await conn.execute(
        update(deployment_state)
        .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
        .values(
            embedding_model_version=version,
            embedding_dimension=target,
            reembed_state={
                "from_version": pin.version,
                "to_version": version,
                "started_at": at.isoformat(),
            },
        )
    )
    await record_config_change(
        conn,
        config_revision=config_revision,
        setting=PIN_SETTING,
        old={"version": pin.version, "dimension": pin.dimension},
        new={"version": version, "dimension": target},
        actor=actor,
        change_class=ConfigClass.prospective,
    )
    return story_ids


async def reembed_state(conn: AsyncConnection) -> dict[str, Any]:
    """The last model change's ``from_version``, ``to_version`` and ``started_at`` (``{}``
    before any)."""
    found = await conn.scalar(
        select(deployment_state.c.reembed_state).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return dict(found or {})


# -- the vectors ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingMemory:
    """A hot active memory whose vector is missing or in another version: what to embed."""

    id: str
    text: str


def _wanted(story_id: str, *, version: str | None, only_missing: bool) -> list[Any]:
    clauses: list[Any] = [
        memories.c.story_id == story_id,
        active_only(memories),
        memories.c.retrieval_tier == RetrievalTier.HOT.value,
    ]
    if only_missing or version is None:
        clauses.append(memories.c.embedding.is_(None))
    else:
        clauses.append(
            or_(
                memories.c.embedding.is_(None),
                memories.c.embedding_model_version.is_distinct_from(version),
            )
        )
    return clauses


async def pending_memories(
    conn: AsyncConnection,
    story_id: str,
    *,
    version: str | None,
    only_missing: bool,
    limit: int,
) -> list[PendingMemory]:
    """Up to ``limit`` of the story's hot active memories with no vector (``only_missing``) or
    with no vector in ``version``, oldest first."""
    found = await conn.execute(
        select(memories.c.id, memories.c.text)
        .where(*_wanted(story_id, version=version, only_missing=only_missing))
        .order_by(memories.c.created_at, memories.c.id)
        .limit(limit)
    )
    return [PendingMemory(str(r.id), r.text) for r in found]


async def stamp(
    conn: AsyncConnection, story_id: str, vectors: Mapping[str, Sequence[float]], pin: Pin
) -> int:
    """Write each memory's vector stamped with the pin's version, only while it is hot and
    active; answers the rows written. Any vector of another dimension refuses the whole write
    (``EmbeddingDimensionMismatch``, nothing stored)."""
    if not pin.pinned:
        raise RuntimeError("vectors are written only into a pinned deployment")
    for vector in vectors.values():
        check_dimension(pin, len(vector), model=str(pin.version))
    written = 0
    for memory_id in sorted(vectors):
        result = await conn.execute(
            update(memories)
            .where(
                memories.c.id == memory_id,
                memories.c.story_id == story_id,
                active_only(memories),
                memories.c.retrieval_tier == RetrievalTier.HOT.value,
            )
            .values(embedding=list(vectors[memory_id]), embedding_model_version=pin.version)
        )
        written += int(result.rowcount)
    return written


async def story_version(conn: AsyncConnection, story_id: str) -> str | None:
    """The version the story's vectors are in (``None`` before its first embedding)."""
    found = await conn.scalar(
        select(stories.c.embedding_model_version).where(stories.c.id == story_id)
    )
    return None if found is None else str(found)


async def advance_story(conn: AsyncConnection, story_id: str, version: str) -> None:
    await conn.execute(
        update(stories).where(stories.c.id == story_id).values(embedding_model_version=version)
    )


async def is_pending(conn: AsyncConnection, story_id: str, pin: Pin | None = None) -> bool:
    """Whether the story's vectors are not (all) in the pinned version yet: it has a version
    and it is another one (a re-embed is due or running). A story never embedded is not
    pending: its memories are new, searched lexically until the embed job reaches them."""
    pin = pin if pin is not None else await read_pin(conn)
    if not pin.pinned:
        return False
    version = await story_version(conn, story_id)
    return version is not None and version != pin.version


# -- a memory as a job input ------------------------------------------------------------------


def memory_input(memory_id: str) -> str:
    """The input key of a memory a job embeds."""
    return input_key(memories.name, memory_id)


async def _memory_version(conn: AsyncConnection, memory_id: str) -> Version | None:
    row = (
        await conn.execute(
            select(
                active_only(memories).label("active"),
                memories.c.membership_tx,
                memories.c.retrieval_tier,
                func.md5(memories.c.text).label("digest"),
            ).where(memories.c.id == memory_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return [bool(row.active), row.membership_tx.isoformat(), str(row.retrieval_tier), row.digest]


def _memory_active(version: Version) -> bool:
    return bool(version) and version[0] is True and version[2] == RetrievalTier.HOT.value


register_version_reader(memories.name, _memory_version, active=_memory_active)


__all__ = [
    "HNSW_INDEX",
    "HNSW_MAX_DIMENSION",
    "PIN_SETTING",
    "EmbeddingDimensionMismatch",
    "EmbeddingVersionMismatch",
    "PendingMemory",
    "Pin",
    "advance_story",
    "change_model",
    "check_dimension",
    "is_pending",
    "memory_input",
    "pending_memories",
    "pin_first",
    "read_pin",
    "reembed_state",
    "retype",
    "stamp",
    "story_version",
]
