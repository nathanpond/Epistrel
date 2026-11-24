"""#947: a scratch story's deletion that is refused ``StoryBusy`` (``lock_timeout``) is retried
under the case's ``BusyRetry`` (#929), outside ``scratch_ddl_lock``, so a busy deletion is no
``cleanup failed`` harness error.

Units over ``runner.Scratch`` with a fake engine; ``drop_partition`` is patched out (it reads
the database). No test here carries a part marker: this is harness tooling, not a
requirement's behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from epistrel.core.errors import StoryBusy, StoryNotFound
from epistrel.engine import Engine
from epistrel.evals import runner
from epistrel.evals.busy import CURRENT, BusyRetry, busy_engine
from epistrel.evals.runner import Scratch, scratch_ddl_lock

BUSY = StoryBusy("busy", {"story_id": "s1", "reason": "lock_timeout"}, retry_after=0)
LANDED = "landed"
"""A scripted outcome: the story is deleted, and the call still answers ``StoryBusy``."""


class FakeEngine:
    """``delete_story`` answers each scripted outcome in turn (an exception is raised; a
    ``"delete"`` deletes; ``LANDED`` deletes and raises ``StoryBusy``), recording whether
    ``scratch_ddl_lock`` was held at each call."""

    def __init__(self, *outcomes: BaseException | str) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []
        self.locked: list[bool] = []
        self.deleted: set[str] = set()

    async def delete_story(self, ctx: Any, story_id: str) -> dict[str, int]:
        del ctx
        self.calls.append(story_id)
        self.locked.append(scratch_ddl_lock().locked())
        if story_id in self.deleted:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        outcome = self.outcomes.pop(0) if self.outcomes else "delete"
        if isinstance(outcome, BaseException):
            raise outcome
        self.deleted.add(story_id)
        if outcome == LANDED:
            raise BUSY
        return {}


@pytest.fixture
def dropped(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    found: list[str] = []

    async def drop_partition(engine: Any, story_id: str) -> bool:
        del engine
        found.append(story_id)
        return True

    monkeypatch.setattr(runner, "drop_partition", drop_partition)
    return found


BACKOFF_LOCKED: list[bool] = []
"""Whether ``scratch_ddl_lock`` was held during each backoff of the last ``_delete_all``."""


async def _delete_all(fake: FakeEngine, *ids: str) -> tuple[Scratch, BusyRetry]:
    BACKOFF_LOCKED.clear()

    async def no_sleep(seconds: float) -> None:
        del seconds
        BACKOFF_LOCKED.append(scratch_ddl_lock().locked())
        await asyncio.sleep(0)

    budget = BusyRetry(sleep=no_sleep)
    scratch = Scratch(busy_engine(cast(Engine, fake)), "label")
    scratch.ids = list(ids)
    token = CURRENT.set(budget)
    try:
        await scratch.delete_all()
    finally:
        CURRENT.reset(token)
    return scratch, budget


async def test_a_busy_deletion_is_retried_and_succeeds(dropped: list[str]) -> None:
    """The first deletion is refused ``StoryBusy``; the retry deletes it. The retry ran outside
    the DDL lock's hold of the refused call (the lock is free between attempts), and the case's
    budget counts it (``story_busy_retries``)."""
    fake = FakeEngine(BUSY)
    scratch, budget = await _delete_all(fake, "s1")
    assert fake.calls == ["s1", "s1"]
    assert fake.locked == [True, True]  # each attempt holds the lock for its own call only
    assert BACKOFF_LOCKED == [False]  # the backoff waits with the lock free
    assert not scratch_ddl_lock().locked()
    assert fake.deleted == {"s1"}
    assert scratch.ids == []
    assert budget.retries == 1
    assert dropped == ["s1"]


async def test_a_retry_that_finds_the_story_gone_counts_as_deleted(dropped: list[str]) -> None:
    """A retry answered ``StoryNotFound`` (the refused attempt's deletion landed after all) is
    the deletion done, not an error."""
    fake = FakeEngine(LANDED)
    scratch, budget = await _delete_all(fake, "s2")
    assert fake.calls == ["s2", "s2"]
    assert scratch.ids == []
    assert budget.retries == 1
    assert dropped == ["s2"]


async def test_a_first_story_not_found_still_raises(dropped: list[str]) -> None:
    """Complement: ``StoryNotFound`` on the first attempt (no retry) is still an error, and the
    id is kept for the sweep."""
    fake = FakeEngine()
    fake.deleted.add("s3")
    with pytest.raises(StoryNotFound):
        await _delete_all(fake, "s3")
    assert dropped == []


async def test_a_non_busy_failure_keeps_the_id(dropped: list[str]) -> None:
    """Complement: any other error is not retried; the id goes back on the list (the run's
    closing sweep deletes it) and the error is raised (``cleanup failed``)."""
    fake = FakeEngine(RuntimeError("delete refused"))
    scratch = Scratch(busy_engine(cast(Engine, fake)), "label")
    scratch.ids = ["s4", "s5"]
    with pytest.raises(RuntimeError):
        await scratch.delete_all()
    assert fake.calls == ["s5"]
    assert scratch.ids == ["s4", "s5"]
    assert dropped == []
    assert not scratch_ddl_lock().locked()
