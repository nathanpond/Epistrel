"""#970 (NFR-QUAL-11): an eval run drops each scratch story's ``memories`` partition as the case
ends (the harness has no worker to run #865's ``memory_partition_drop``), pins the embedding
before any story exists, and records a failed in-process job's SQLSTATE, not only its class.
A certify run on 2026-11-19 left 9,628 partitions; the first pin retyped them all, failed, and
errored every q03 case; a later resume filled the container's ``/dev/shm``.

#1000 (#970 AC1): ``run_suites`` itself pins the embedding, exactly once and before its first
scratch story, when an embedding model is configured; with none, it probes and pins nothing."""

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
from epistrel.engine import Engine
from epistrel.evals import runner, scenes
from epistrel.evals.runner import AdapterConfig, CaseContext, Scratch, run_suites, sweep
from epistrel.evals.suites import CaseModel, Outcome
from epistrel.orchestrator.embeddings.adapter import EMBEDDING
from epistrel.orchestrator.jobs.handlers.memory_partitions import coalesce_key
from epistrel.orchestrator.models import config as model_config
from tests.evals.support import ZERO_METRIC, SyntheticZeroSuite, stub_engine
from tests.model_support import restored_model_config

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


async def test_pin_embedding_probes_once_and_pins_only_when_configured(
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


class OneStorySuite(SyntheticZeroSuite):
    """Each case creates one scratch story: the run's first story creation."""

    name = "pin_order"

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        await ctx.story(SPEC)
        return Outcome({ZERO_METRIC: True}, output="done")


def record_start_up(
    monkeypatch: pytest.MonkeyPatch, lib: Engine, events: list[str]
) -> list[dict[str, Any]]:
    """Record, in one ordered list, the runner's ``pin_embedding`` (delegating), the embedding
    port's probe, ``embeddings.pin_first`` and every story creation. The probe and the pin are
    stubbed: the shared test database's pin and ``memories`` type never move. The pins made."""
    pinned: list[dict[str, Any]] = []
    # Whatever the runner binds now (a mutant's included), not ``scenes.pin_embedding``.
    delegate = vars(runner)["pin_embedding"]

    async def pin_embedding(engine: Engine) -> Any:
        events.append("pin_embedding")
        return await delegate(engine)

    async def probe(*_: Any) -> Any:
        events.append("probe")
        return SimpleNamespace(version="stub-embed@1", dimension=768)

    async def pin_first(conn: Any, **kwargs: Any) -> str:
        events.append("pin_first")
        pinned.append(kwargs)
        return "pin"

    create = lib.create_story

    async def create_story(*args: Any, **kwargs: Any) -> Any:
        events.append("create_story")
        return await create(*args, **kwargs)

    monkeypatch.setattr(runner, "pin_embedding", pin_embedding)
    monkeypatch.setattr(embeddings, "pin_first", pin_first)
    monkeypatch.setattr(lib.models.embeddings, "probe", probe)
    object.__setattr__(lib, "create_story", create_story)
    return pinned


@pytest.mark.integration
@pytest.mark.parametrize("configured", [True, False], ids=["embedding", "no-embedding"])
async def test_the_run_pins_the_embedding_before_its_first_scratch_story(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    """#1000: configured, ``run_suites`` probes once and pins once, and both come before the
    first ``create_story``. Complement asserted: with no embedding model, the runner's
    ``pin_embedding`` still runs first but probes and pins nothing."""
    suite = OneStorySuite()
    events: list[str] = []
    async with stub_engine(pg_url, engine) as lib, restored_model_config(engine):
        if configured:
            await model_config.write_config(
                engine,
                EMBEDDING,
                keybox=lib.models.keybox,
                principal="test:pin-order",
                model="stub-embed",
            )
        pinned = record_start_up(monkeypatch, lib, events)
        report = await run_suites(
            [suite],
            AdapterConfig(),
            [1],
            engine=lib,
            cases={suite.name: [CaseModel(id="c-0001")]},
        )
    assert report.harness_errors == []
    assert events.count("create_story") == 1
    first_story = events.index("create_story")
    if configured:
        assert events[:first_story] == ["pin_embedding", "probe", "pin_first"]
        assert events.count("pin_first") == 1
        assert pinned == [{"version": "stub-embed@1", "dimension": 768}]
    else:
        assert events[:first_story] == ["pin_embedding"]
        assert "probe" not in events
        assert pinned == []
