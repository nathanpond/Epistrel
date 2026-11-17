"""Concurrent eval cases create and delete their scratch stories one at a time (#953).

Creating and deleting a story attaches and detaches its partitions, which take table-level
locks a concurrent case's commit waits behind (#929's amplified repro). The runner serialises
those calls across the run (``runner.scratch_ddl_lock``) while the cases' other engine calls
still overlap. A case that raises still deletes its stories and leaves the lock free.

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
from epistrel.evals.runner import AdapterConfig, CaseContext, run_suites, scratch_ddl_lock
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
