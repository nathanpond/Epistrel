"""#970 (NFR-QUAL-11): an eval run drops each scratch story's ``memories`` partition as the case
ends (the harness has no worker to run #865's ``memory_partition_drop``), pins the embedding
before any story exists, and records a failed in-process job's SQLSTATE, not only its class.
A certify run on 2026-11-19 left 9,628 partitions; the first pin retyped them all, failed, and
errored every q03 case; a later resume filled the container's ``/dev/shm``."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import embeddings
from epistrel.core.memory import story_partitions
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.tables.jobs import jobs
from epistrel.evals import scenes
from epistrel.evals.runner import Scratch, sweep
from epistrel.orchestrator.jobs.handlers.memory_partitions import coalesce_key
from tests.evals.support import stub_engine

SPEC = StorySpec(
    characters=[
        CharacterSpec(name="Wren", definition="A traveller.", true_age=29, is_player=True),
        CharacterSpec(name="Maren", definition="The innkeeper.", true_age=46),
    ]
)


async def _drop_jobs(db: AsyncEngine, story_id: str) -> list[str]:
    async with db.connect() as conn:
        rows = await conn.execute(
            select(jobs.c.status).where(jobs.c.coalesce_key == coalesce_key(story_id))
        )
    return [str(r.status) for r in rows]


@pytest.mark.integration
async def test_a_deleted_scratch_story_leaves_no_partition(
    pg_url: str, engine: AsyncEngine
) -> None:
    """A case's scratch stories, deleted when it ends: each partition is gone from ``memories``
    and its drop job is done, not queued. Complement asserted: while the story lives, its
    partition is attached."""
    async with stub_engine(pg_url, engine) as lib:
        scratch = Scratch(lib, "partition-drain")
        created = await scratch.create(SPEC)
        name = story_partitions.partition_name(created.story_id)
        async with engine.connect() as conn:
            assert name in await story_partitions.attached(conn)
        await scratch.delete_all()
    async with engine.connect() as conn:
        assert name not in await story_partitions.attached(conn)
    assert await _drop_jobs(engine, created.story_id) == ["done"]


@pytest.mark.integration
async def test_the_sweep_drops_what_it_deletes(pg_url: str, engine: AsyncEngine) -> None:
    """An interrupted run's scratch story, deleted by the next run's sweep, loses its partition
    too."""
    async with stub_engine(pg_url, engine) as lib:
        created = await Scratch(lib, "partition-sweep").create(SPEC)
        assert await sweep(lib) >= 1
    async with engine.connect() as conn:
        assert story_partitions.partition_name(created.story_id) not in (
            await story_partitions.attached(conn)
        )
    assert await _drop_jobs(engine, created.story_id) == ["done"]


def test_a_database_error_keeps_its_sqlstate_and_never_its_message() -> None:
    class Driver(Exception):
        sqlstate = "55P03"
        diag = SimpleNamespace(condition_name=None)

    class Wrapped(Exception):
        def __init__(self) -> None:
            super().__init__("canceling statement due to lock timeout: Key (id)=(secret)")
            self.orig = Driver()

    Wrapped.__name__ = "OperationalError"
    assert scenes.error_label(Wrapped()) == "OperationalError (55P03 lock_not_available)"
    Driver.sqlstate = "XX000"
    assert scenes.error_label(Wrapped()) == "OperationalError (XX000)"
    assert scenes.error_label(ValueError("story text")) == "ValueError"


class _Conn:
    pass


class _Db:
    def __init__(self) -> None:
        self.opened = 0

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[_Conn]:
        self.opened += 1
        yield _Conn()


async def test_the_embedding_is_pinned_before_any_story_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured: one probe, then ``pin_first`` with the probe's version and dimension.
    Complement asserted: unconfigured, nothing is probed or pinned."""
    pinned: list[dict[str, Any]] = []
    probes: list[int] = []

    async def probe() -> Any:
        probes.append(1)
        return SimpleNamespace(version="nomic@1", dimension=768)

    async def pin_first(conn: Any, **kwargs: Any) -> str:
        pinned.append(kwargs)
        return "pin"

    engine: Any = SimpleNamespace(
        db=_Db(), models=SimpleNamespace(embeddings=SimpleNamespace(probe=probe))
    )
    monkeypatch.setattr(embeddings, "pin_first", pin_first)
    for configured in (False, True):

        async def embedding_configured(_: Any, configured: bool = configured) -> bool:
            return configured

        monkeypatch.setattr(scenes, "embedding_configured", embedding_configured)
        found: Any = await scenes.pin_embedding(engine)
        assert found == ("pin" if configured else None)
    assert probes == [1]
    assert pinned == [{"version": "nomic@1", "dimension": 768}]
