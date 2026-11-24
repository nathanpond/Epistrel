"""Concurrent eval cases create and delete their scratch stories one at a time (#953).

Creating and deleting a story attaches and detaches its partitions, which take table-level
locks a concurrent case's commit waits behind (#929's amplified repro). The runner serialises
those calls across the run (``runner.scratch_ddl_lock``) while the cases' other engine calls
still overlap. A case that raises still deletes its stories and leaves the lock free.

#998 (#953 AC2): a scratch deletion that fails is reported as the case's ``cleanup failed``
harness error, and a case that is stopped still deletes its stories in ``_play_case``'s
``finally``.

No test here carries a part marker: this is harness tooling, not a requirement's behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.engine import Engine
from epistrel.evals import runner
from epistrel.evals.runner import (
    AdapterConfig,
    CaseContext,
    Stopped,
    run_suites,
    scratch_ddl_lock,
)
from epistrel.evals.suites import CaseModel, Outcome
from tests.evals.support import ZERO_METRIC, SyntheticZeroSuite, stub_engine

CASES = 4
STORIES_PER_CASE = 2


class Tracker:
    """Records who is inside a story create/delete and inside a case's probe call at once."""

    def __init__(self) -> None:
        self.ddl_now = self.ddl_peak = 0
        self.probe_now = self.probe_peak = 0
        self.created = self.deleted = 0

    def wrap(self, call: Callable[..., Awaitable[Any]], kind: str) -> Callable[..., Awaitable[Any]]:
        async def tracked(*args: Any, **kwargs: Any) -> Any:
            self.ddl_now += 1
            self.ddl_peak = max(self.ddl_peak, self.ddl_now)
            try:
                await asyncio.sleep(0.02)  # widen the window an overlap would show in
                return await call(*args, **kwargs)
            finally:
                self.ddl_now -= 1
                if kind == "create":
                    self.created += 1
                else:
                    self.deleted += 1

        return tracked

    async def probe(self) -> str:
        self.probe_now += 1
        self.probe_peak = max(self.probe_peak, self.probe_now)
        try:
            await asyncio.sleep(0.2)
            return "done"
        finally:
            self.probe_now -= 1


class ScratchSuite(SyntheticZeroSuite):
    """Each case creates two scratch stories, then calls the tracker's probe; ``c-0002``
    raises after creating its stories."""

    name = "scratch_serialised"

    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        cast = [CharacterSpec(name="Cal", definition="A traveller.", true_age=30, is_player=True)]
        for _ in range(STORIES_PER_CASE):
            await ctx.story(StorySpec(characters=cast))
        answer = await self.tracker.probe()
        if case.id == "c-0002":
            raise RuntimeError("boom")
        return Outcome({ZERO_METRIC: answer == "done"}, output=answer)


@pytest.mark.integration
async def test_scratch_creates_and_deletes_never_overlap_while_cases_do(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    tracker = Tracker()
    suite = ScratchSuite(tracker)
    async with stub_engine(pg_url, engine) as lib:
        object.__setattr__(lib, "create_story", tracker.wrap(lib.create_story, "create"))
        object.__setattr__(lib, "delete_story", tracker.wrap(lib.delete_story, "delete"))
        report = await run_suites(
            [suite],
            AdapterConfig(),
            [1],
            engine=lib,
            cases={suite.name: [CaseModel(id=f"c-{i:04d}") for i in range(1, CASES + 1)]},
            concurrency=CASES,
        )
        assert not scratch_ddl_lock().locked()
    assert tracker.ddl_peak == 1, "two scratch creates/deletes ran at once"
    assert tracker.probe_peak > 1, "the cases' other engine calls should still overlap"
    assert tracker.created == CASES * STORIES_PER_CASE
    assert tracker.deleted >= CASES * STORIES_PER_CASE  # the failing case's included
    (error,) = report.harness_errors
    assert "c-0002" in error
    assert "boom" in error
    assert "cleanup failed" not in error


async def test_the_lock_is_one_per_event_loop() -> None:
    """The same lock within a loop; a fresh loop (another test's) gets its own."""
    assert scratch_ddl_lock() is scratch_ddl_lock()
    inner: list[asyncio.Lock] = []

    def other_loop() -> None:
        async def grab() -> None:
            inner.append(scratch_ddl_lock())

        asyncio.run(grab())

    await asyncio.to_thread(other_loop)
    assert inner[0] is not scratch_ddl_lock()


CAST = [CharacterSpec(name="Cal", definition="A traveller.", true_age=30, is_player=True)]


class StoriesSuite(SyntheticZeroSuite):
    """Each case creates ``stories`` scratch stories; with ``stop`` set, it then sets the stop
    and raises ``Stopped`` (as a case waiting out ``StoryBusy`` does once the run is stopped)."""

    name = "scratch_cleanup"

    def __init__(self, stories: int, stop: asyncio.Event | None = None) -> None:
        self.stories = stories
        self.stop = stop
        self.created: list[str] = []

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        for _ in range(self.stories):
            self.created.append((await ctx.story(StorySpec(characters=CAST))).story_id)
        if self.stop is not None:
            self.stop.set()
            raise Stopped("stopped mid-case")
        return Outcome({ZERO_METRIC: True}, output="done")


def wrap_delete(lib: Engine, own: list[str], deleted: list[str], *, fail_first: bool) -> None:
    """Record which of the case's ``own`` stories ``lib.delete_story`` deletes; ``fail_first``:
    the first deletion of one of them (the case's cleanup) raises a non-retryable error, so the
    run's closing sweep deletes what was left. Other scratch stories (an earlier test's, found
    by the opening sweep) pass through untouched."""
    original = lib.delete_story
    armed = fail_first

    async def delete_story(ctx: Any, story_id: str) -> Any:
        nonlocal armed
        if armed and story_id in own:
            armed = False
            raise RuntimeError("delete refused")
        result = await original(ctx, story_id)
        if story_id in own:
            deleted.append(story_id)
        return result

    object.__setattr__(lib, "delete_story", delete_story)


def sweep_counts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """What each of the run's sweeps found: the closing sweep's 0 means every case deleted its
    own stories (the sweep is the backstop, not the cleanup)."""
    found: list[int] = []
    original = runner.sweep

    async def sweep(engine: Engine) -> int:
        n = await original(engine)
        found.append(n)
        return n

    monkeypatch.setattr(runner, "sweep", sweep)
    return found


@pytest.mark.integration
async def test_a_failed_scratch_deletion_is_reported_as_cleanup_failed(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#998: the case's ``delete_story`` raises; the report names the case's cleanup failure,
    and the closing sweep deletes the story it left. Complement asserted: the case's outcome
    still counts (the error is the harness's, not the case's)."""
    suite = StoriesSuite(stories=1)
    swept = sweep_counts(monkeypatch)
    deleted: list[str] = []
    async with stub_engine(pg_url, engine) as lib:
        wrap_delete(lib, suite.created, deleted, fail_first=True)
        report = await run_suites(
            [suite],
            AdapterConfig(),
            [1],
            engine=lib,
            cases={suite.name: [CaseModel(id="c-0001")]},
        )
    assert report.harness_errors == ["case c-0001 seed 1: cleanup failed: RuntimeError"]
    assert swept[-1] == 1, "the closing sweep should find the story the failed cleanup left"
    assert deleted == suite.created
    (result,) = report.suites
    (metric,) = result.metrics
    assert metric.sample_size == 1


@pytest.mark.integration
async def test_a_stopped_case_still_deletes_its_scratch_stories(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#998: a case stopped mid-run (it raises ``Stopped``) deletes both its stories in the
    ``finally``, before the run's closing sweep, which finds none; the run raises ``Stopped``."""
    stop = asyncio.Event()
    suite = StoriesSuite(stories=2, stop=stop)
    swept = sweep_counts(monkeypatch)
    deleted: list[str] = []
    async with stub_engine(pg_url, engine) as lib:
        wrap_delete(lib, suite.created, deleted, fail_first=False)
        with pytest.raises(Stopped):
            await run_suites(
                [suite],
                AdapterConfig(),
                [1],
                engine=lib,
                cases={suite.name: [CaseModel(id="c-0001")]},
                stop=stop,
            )
        assert not scratch_ddl_lock().locked()
    assert len(suite.created) == 2
    assert sorted(deleted) == sorted(suite.created)
    assert swept[-1] == 0, "the stopped case left its stories for the sweep"


@pytest.mark.integration
async def test_a_stopped_case_whose_cleanup_fails_reports_no_cleanup_error(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#998, today's precedence pinned (a known gap, not a promise): when a stopped case's
    deletion also fails, the run raises ``Stopped`` and no report carries the cleanup failure;
    the closing sweep deletes what the case left."""
    stop = asyncio.Event()
    suite = StoriesSuite(stories=1, stop=stop)
    swept = sweep_counts(monkeypatch)
    deleted: list[str] = []
    reports: list[Any] = []
    async with stub_engine(pg_url, engine) as lib:
        wrap_delete(lib, suite.created, deleted, fail_first=True)
        with pytest.raises(Stopped):
            await run_suites(
                [suite],
                AdapterConfig(),
                [1],
                engine=lib,
                cases={suite.name: [CaseModel(id="c-0001")]},
                stop=stop,
                on_suite=reports.append,
            )
    assert reports == []
    assert swept[-1] == 1
    assert deleted == suite.created
