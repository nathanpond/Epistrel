"""S-FORK-SNAPSHOT, M2 part: a fork reads one snapshot of its source (docs/04 §7, FR-CONC-10;
#67). Turns and edits commit on the source while the fork copies; the copy equals the source at
exactly the revision it names, never a mix."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import fork as forking
from epistrel.core import timeline
from epistrel.core.errors import DatabaseUnavailable
from tests.concurrency_support import assert_single_chain, revision, set_pins
from tests.event_support import new_story, play_turn, scalar
from tests.fork_support import Content, content, fork

TURNS = 30
FORKS = 8
PRINCIPAL = "p-player"


async def _state(engine: AsyncEngine, story: str) -> Content:
    return await content(engine, story)


@pytest.mark.part("M2", "FR-CONC-10")
async def test_s_fork_snapshot__interleaved(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn, a pinned-memory edit and a truncation commit on the source after the fork has
    read the source's revision and before it reads the operations to copy: the fork equals the
    source as it was at that revision, all three are absent, and the source has them.
    Complement: the interleaved writes did commit (the source moved three revisions on)."""
    story = await new_story(engine)
    for n in range(1, 5):
        await play_turn(engine, story, f"turn {n}")
    await set_pins(engine, story, "before the fork")
    at_start = await _state(engine, story)
    start = await revision(engine, story)

    original = forking._select_operations
    interleaved: list[int] = []

    async def write_meanwhile(conn: AsyncConnection, *args: Any) -> Any:
        if not interleaved:
            await play_turn(engine, story, "turn 5, during the fork")
            await set_pins(engine, story, "during the fork")
            await timeline.truncate(
                engine, story, timeline.TruncateRequest(to_turn=2), principal=PRINCIPAL
            )
            interleaved.append(await revision(engine, story))
        return await original(conn, *args)

    monkeypatch.setattr(forking, "_select_operations", write_meanwhile)
    done = await fork(engine, story)
    assert interleaved == [start + 3]
    assert done.forked_from.revision == start
    assert done.forked_from.at_turn == 4
    copied = await content(engine, done.story_id, skip_operation=done.operation_id)
    assert copied == at_start
    assert (await _state(engine, story)).next_turn_index == 3
    await assert_single_chain(engine, done.story_id)


@pytest.mark.part("M2", "FR-CONC-10")
async def test_s_fork_snapshot(engine: AsyncEngine) -> None:
    """Turns and pinned-memory edits commit on the source one after another while forks run
    concurrently: every fork equals the snapshot of the source taken right after the commit
    that produced the revision it names. Complement: the forks landed on several different
    revisions, so the source really moved under them."""
    story = await new_story(engine)
    snapshots: dict[int, Content] = {await revision(engine, story): await _state(engine, story)}
    done = asyncio.Event()

    async def record() -> None:
        # The only writer: nothing commits between its last commit and this snapshot.
        snapshots[await revision(engine, story)] = await _state(engine, story)
        await asyncio.sleep(0)

    async def play() -> None:
        try:
            for n in range(1, TURNS + 1):
                await play_turn(engine, story, f"turn {n}")
                await record()
                if n % 4 == 0:
                    await set_pins(engine, story, f"pinned after turn {n}")
                    await record()
        finally:
            done.set()

    async def forks() -> list[forking.Forked]:
        made: list[forking.Forked] = []
        while not done.is_set() or len(made) < FORKS:
            made.append(await fork(engine, story))
            await asyncio.sleep(0.01)
        return made

    _, made = await asyncio.gather(play(), forks())
    assert len({f.forked_from.revision for f in made}) > 1
    for forked in made:
        expected = snapshots[forked.forked_from.revision]
        copied = await content(engine, forked.story_id, skip_operation=forked.operation_id)
        assert copied == expected, forked.forked_from
        assert expected.next_turn_index == forked.forked_from.at_turn + 1


class _SerializationFailure(Exception):
    sqlstate = forking.SERIALIZATION_FAILURE


@pytest.mark.part("M2", "FR-CONC-10")
async def test_s_fork_snapshot__retry(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot copy that hits a serialization failure is retried from scratch, and after
    ``SNAPSHOT_ATTEMPTS`` failures the fork is 503 ``database_unavailable`` with no story
    written. Complement: any other database error is not retried."""
    story = await new_story(engine)
    await play_turn(engine, story, "turn 1")
    original = forking._copy
    calls: list[int] = []

    def failing(times: int, error: Exception) -> Any:
        async def copy(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) <= times:
                raise OperationalError("fork", {}, error)
            return await original(*args, **kwargs)

        return copy

    stories_before = await scalar(engine, "SELECT count(*) FROM stories")
    monkeypatch.setattr(forking, "_copy", failing(2, _SerializationFailure()))
    done = await fork(engine, story)
    assert len(calls) == 3
    assert await scalar(engine, "SELECT count(*) FROM stories") == stories_before + 1

    calls.clear()
    monkeypatch.setattr(
        forking, "_copy", failing(forking.SNAPSHOT_ATTEMPTS, _SerializationFailure())
    )
    with pytest.raises(DatabaseUnavailable) as refused:
        await fork(engine, story)
    assert (refused.value.http_status, refused.value.code) == (503, "database_unavailable")
    assert len(calls) == forking.SNAPSHOT_ATTEMPTS
    assert await scalar(engine, "SELECT count(*) FROM stories") == stories_before + 1

    calls.clear()
    monkeypatch.setattr(forking, "_copy", failing(1, RuntimeError("not a serialization failure")))
    with pytest.raises(OperationalError):
        await fork(engine, story)
    assert len(calls) == 1
    assert done.story_id
