"""An eval case retries its own story's ``StoryBusy`` (#929) instead of becoming a harness error.

A commit that finds the story row held past the lock timeout is refused with ``StoryBusy``
(``lock_timeout``) and wrote nothing; the harness retries the call that raised, with bounded
backoff (``busy.BOUND_S``), and reports a story still busy after the bound as a harness error
naming ``StoryBusy``. Any other error, and ``queue_full``, is never retried.

No test here carries a part marker: this is harness tooling, not a requirement's behaviour.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import InvalidRequest, StoryBusy
from epistrel.core.events import operation
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.evals import busy, scenes
from epistrel.evals.journal import Journal
from epistrel.evals.runner import AdapterConfig, CaseContext, run_suites
from epistrel.evals.suites import CaseModel, Outcome
from epistrel.orchestrator.models.stub import STUB_MODEL
from tests.evals.support import ZERO_METRIC, SyntheticZeroSuite, stub_engine
from tests.integration.test_story_lock_timeout import lock_held_at_commit

FINGERPRINT = "sha256:" + "ef" * 32


def _busy(reason: str = "lock_timeout") -> StoryBusy:
    return StoryBusy("story s is busy", {"story_id": "s", "reason": reason}, retry_after=0)


class Flaky:
    """An awaitable call that raises ``StoryBusy`` ``n`` times, then answers."""

    def __init__(self, n: int, error: Exception | None = None) -> None:
        self.n, self.calls = n, 0
        self.error = error or _busy()

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.n:
            raise self.error
        return "done"


async def test_a_busy_call_is_retried_until_it_answers() -> None:
    budget = busy.BusyRetry()
    flaky = Flaky(2)
    assert await budget.run(flaky) == "done"
    assert (flaky.calls, budget.retries) == (3, 2)
    assert budget.slept_ms >= 2 * busy.FIRST_WAIT_S * 1000 * 0.9


async def test_a_story_still_busy_after_the_bound_is_raised_naming_it() -> None:
    """Calls and sleeps together stay within the bound (with slack); the error is still
    ``StoryBusy`` and says it was retried."""
    budget = busy.BusyRetry(bound_s=1.0)
    started = time.monotonic()
    with pytest.raises(StoryBusy, match=r"still busy after \d+ retries"):
        await budget.run(Flaky(10_000))
    assert time.monotonic() - started <= 1.0 + 0.5
    assert budget.retries >= 1


@pytest.mark.parametrize(
    "error", [_busy("queue_full"), InvalidRequest("bad"), RuntimeError("boom")]
)
async def test_other_errors_are_not_retried(error: Exception) -> None:
    flaky = Flaky(1, error)
    with pytest.raises(type(error)):
        await busy.BusyRetry().run(flaky)
    assert flaky.calls == 1


async def test_a_stop_ends_the_wait() -> None:
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(busy.Stopped):
        await busy.BusyRetry(stop=stop).run(Flaky(1))


def _coroutine(call: Flaky) -> Any:
    """``call`` as a coroutine function (what an ``Engine`` method is)."""

    async def method() -> str:
        return await call()

    return method


class EngineCallSuite(SyntheticZeroSuite):
    """Each case calls ``ctx.engine.busy_probe`` (planted on the engine) once."""

    name = "busy_probe"

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        answer = await ctx.engine.busy_probe()  # type: ignore[attr-defined]
        return Outcome({ZERO_METRIC: answer == "done"}, output=answer)


@pytest.mark.integration
async def test_through_the_runner_a_case_retries_and_is_scored_once(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """A case whose engine call is busy twice completes, is journalled once, and carries its
    retry count; one that stays busy is a harness error naming ``StoryBusy``, never a model
    error, and observes nothing."""
    suite = EngineCallSuite()
    journal = Journal.open(tmp_path / "j.jsonl", model=STUB_MODEL, fingerprint=FINGERPRINT)
    async with stub_engine(pg_url, engine) as lib:
        flaky = Flaky(2)
        object.__setattr__(lib, "busy_probe", _coroutine(flaky))
        report = await run_suites(
            [suite],
            AdapterConfig(),
            [1],
            engine=lib,
            cases={suite.name: [CaseModel(id="c-1")]},
            journal=journal,
        )
        assert flaky.calls == 3
        assert not report.harness_errors
        (metric,) = report.suites[0].metrics
        assert metric.sample_size == 1
        (row,) = journal.entries.values()
        assert row.outcome.detail["story_busy_retries"] == 2
        assert len((tmp_path / "j.jsonl").read_text("utf-8").splitlines()) == 1

        stuck = Flaky(10_000)
        object.__setattr__(lib, "busy_probe", _coroutine(stuck))
        bound = busy.BOUND_S
        busy.BOUND_S = 1.0
        try:
            stuck_report = await run_suites(
                [suite], AdapterConfig(), [1], engine=lib, cases={suite.name: [CaseModel(id="c-2")]}
            )
        finally:
            busy.BOUND_S = bound
    (error,) = stuck_report.harness_errors
    assert "StoryBusy" in error
    assert "still busy" in error
    assert stuck_report.suites[0].metrics[0].sample_size == 0


class LockedEpisodeSuite(SyntheticZeroSuite):
    """Each case writes one episode (on a structured story, as E-03 does) while another
    transaction holds its story's row (the shape of #929's CI failure: the case's commit waits
    past the lock timeout)."""

    name = "locked_episode"

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        cast = [
            CharacterSpec(name="Cal", definition="A traveller.", true_age=30, is_player=True),
            CharacterSpec(name="Ann", definition="A villager.", true_age=34),
        ]
        created = await ctx.story(StorySpec(characters=cast))
        actor = next(c.id for c in created.characters if c.name == "Ann")
        await scenes.structured(ctx.engine, ctx.principal, created.story_id)
        async with lock_held_at_commit(self.monkeypatch, ctx.engine.db, created.story_id):
            written = await scenes.episodes(ctx.engine, created.story_id, [(actor, "waits.")])
        return Outcome({ZERO_METRIC: len(written) == 1}, output=str(len(written)))


@pytest.mark.integration
async def test_a_commit_refused_on_a_held_story_lock_is_retried(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reproduced first on the pre-fix harness (a harness error ``StoryBusy: … another commit
    held its lock``); now the episode is written once, after one retry."""
    monkeypatch.setattr(operation, "LOCK_RETRY_AFTER_S", 0)
    suite = LockedEpisodeSuite(monkeypatch)
    journal = Journal.open(tmp_path / "l.jsonl", model=STUB_MODEL, fingerprint=FINGERPRINT)
    async with stub_engine(pg_url, engine) as lib:
        report = await run_suites(
            [suite],
            AdapterConfig(),
            [1],
            engine=lib,
            cases={suite.name: [CaseModel(id="l-1")]},
            journal=journal,
        )
    assert not report.harness_errors, report.harness_errors
    (metric,) = report.suites[0].metrics
    assert (metric.sample_size, metric.violations) == (1, 0)
    (row,) = journal.entries.values()
    assert row.outcome.detail["story_busy_retries"] == 1


def test_the_proxy_passes_plain_attributes_through() -> None:
    class Thing:
        db = "the-db"

        async def go(self) -> str:
            return "went"

    proxied: Any = busy.busy_engine(Thing())  # type: ignore[arg-type]
    went = asyncio.run(proxied.go())
    assert proxied.db == "the-db"
    assert went == "went"
    assert busy.busy_engine(proxied) is proxied
