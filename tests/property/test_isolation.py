"""ISOLATION: self-contained stories (docs/04 §6; FR-STORE-14..16).

For random histories: no row a story owns names a row another story owns; a fork at N is a
full, independent copy whose reconstruction through N equals the source's; changes on either
side after the fork never touch the other; and deleting a story removes exactly its own rows,
leaving every other story byte-identical.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.db import make_engine
from epistrel.core.stories import delete_story
from tests.auth_support import auth_settings
from tests.concurrency_support import regenerate, set_instructions, set_pins, summarise
from tests.event_support import new_story, play_turn, rows, scalar
from tests.fork_support import content, fork, mentions, owned_ids, owned_rows
from tests.projection_support import truncate

ACTIONS = ("turn", "turn", "turn", "pins", "instructions", "summary", "regenerate", "truncate")

Step = tuple[str, int]
histories = st.lists(st.tuples(st.sampled_from(ACTIONS), st.integers(0, 6)), min_size=1, max_size=8)


async def _head(engine: AsyncEngine, story: str) -> int:
    """The last active turn (0 for none)."""
    found = await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
    return int(found) - 1


async def _apply(engine: AsyncEngine, story: str, steps: list[Step], tag: str) -> None:
    for i, (action, n) in enumerate(steps):
        head = await _head(engine, story)
        if action == "turn":
            await play_turn(engine, story, f"{tag} turn {i}")
        elif action == "pins":
            await set_pins(engine, story, f"{tag} pin {i}")
        elif action == "instructions":
            await set_instructions(engine, story, f"{tag} instructions {i}")
        elif action == "summary" and head >= 1:
            await summarise(engine, story, 1 + n % head)
        elif action == "regenerate" and head >= 1:
            await regenerate(engine, story)
        elif action == "truncate" and head >= 2:
            await truncate(engine, story, n % head)


def _no_shared_rows(
    owned: dict[str, dict[str, set[str]]], ids: dict[str, set[str]], provenance: set[str]
) -> None:
    """FR-STORE-14: no two stories share an id, and no row of one names another's ids. The fork
    provenance events are the exception by design (#67): the fork's ``story_created`` names its
    source, and the source's ``fork`` event names the fork; ``provenance`` holds their ids."""
    for story, tables in owned.items():
        mine = {
            t: {r for r in found if not any(p in r for p in provenance)}
            for t, found in tables.items()
        }
        for other in owned:
            if other != story:
                assert not ids[story] & ids[other]
                assert mentions(mine, ids[other]) == {}, (story, other)


async def _provenance(engine: AsyncEngine, source: str, fork_operation: str) -> set[str]:
    found = await rows(
        engine, "SELECT id FROM events WHERE story_id = :s AND type = 'fork'", s=source
    )
    return {fork_operation, *(str(r.id) for r in found)}


async def _check(url: str, history: list[Step], cut: int, after_source: list[Step],
                 after_fork: list[Step], delete_fork: bool) -> None:  # fmt: skip
    engine = make_engine(auth_settings(url))
    try:
        source, bystander = await new_story(engine), await new_story(engine)
        await _apply(engine, bystander, [("turn", 0), ("pins", 0), ("turn", 0)], "other")
        await _apply(engine, source, history, "source")
        at = min(cut, await _head(engine, source))
        done = await fork(engine, source, at)
        copy = done.story_id

        # A full copy of the corrected history through N.
        mine = await content(engine, copy, skip_operation=done.operation_id)
        theirs = await content(engine, source, at)
        assert mine.history == theirs.history
        assert (mine.texts, mine.settings, mine.cast) == (
            theirs.texts,
            theirs.settings,
            theirs.cast,
        )

        stories = (source, copy, bystander)
        owned = {s: await owned_rows(engine, s) for s in stories}
        ids = {s: await owned_ids(engine, s) for s in stories}
        _no_shared_rows(owned, ids, await _provenance(engine, source, done.operation_id))

        # Independent after creation: either side changes, the others do not.
        await _apply(engine, source, after_source, "source after")
        assert await owned_rows(engine, copy) == owned[copy]
        assert await owned_rows(engine, bystander) == owned[bystander]
        owned[source] = await owned_rows(engine, source)
        await _apply(engine, copy, after_fork, "fork after")
        assert await owned_rows(engine, source) == owned[source]
        assert await owned_rows(engine, bystander) == owned[bystander]
        owned[copy] = await owned_rows(engine, copy)

        # Deleting one removes exactly its rows; the others are byte-identical.
        victim, kept = (copy, source) if delete_fork else (source, copy)
        await delete_story(engine, victim, principal="p-author")
        assert all(not found for found in (await owned_rows(engine, victim)).values())
        assert await owned_rows(engine, kept) == owned[kept]
        assert await owned_rows(engine, bystander) == owned[bystander]
        await delete_story(engine, kept, principal="p-author")
        await delete_story(engine, bystander, principal="p-author")
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.part("M2", "FR-STORE-14..16")
def test_isolation(pg_url: str) -> None:
    """Random histories (turns, pins, instructions, summaries, regenerations, truncations), a
    fork at a random N, random changes on each side and a delete of either: the invariants in
    the module docstring hold at every step.

    Complement asserted: each side's own changes did land (the row snapshots are retaken after
    them), and the deleted story's rows were there before the delete."""

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(
        history=histories,
        cut=st.integers(0, 8),
        after_source=histories,
        after_fork=histories,
        delete_fork=st.booleans(),
    )
    def isolated(
        history: list[Step],
        cut: int,
        after_source: list[Step],
        after_fork: list[Step],
        delete_fork: bool,
    ) -> None:
        asyncio.run(_check(pg_url, history, cut, after_source, after_fork, delete_fork))

    isolated()
