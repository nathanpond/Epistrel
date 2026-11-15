"""The scale fixture writes its memories as a stream (#914; P-10, #502): ``memory_rows`` yields
the rows ``memory_plan`` lists, unchanged, and ``insert_memories`` holds one batch of them at a
time, so the capacity tool's memory no longer grows with the step it adds."""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest

from epistrel.testing.fixtures import scale

SPEC = scale.ScaleSpec(memories=300, events=200, facts=100, characters=6, dimension=16)
TURN_OPS = {t: f"op{t}" for t in range(1, 41)}

FIRST_DIGEST = "9691b2cbc2b31966"
GROWN_DIGEST = "33738f92283118cf"
"""The rows the list-form ``memory_plan`` wrote before #914, for ``SPEC``'s 300 memories and a
grow of 500 more (computed with ``origin/main`` @ 896a8bc's ``scale.py``)."""


def _story() -> scale.ScaleStory:
    cast = {name: f"c{i:02d}" for i, name in enumerate(scale.person_names(SPEC.characters))}
    return scale.ScaleStory(
        story_id="s", characters=cast, people={**cast, "Zed": "e99"}, places={}, items={}, head=40
    )


def _digest(rows: Iterable[Mapping[str, Any]]) -> str:
    """The rows' content in order, but for the ULID ``id`` ``memory_row`` draws on every call."""
    found = hashlib.sha256()
    for row in rows:
        found.update(
            json.dumps({k: v for k, v in row.items() if k != "id"}, sort_keys=True).encode()
        )
    return found.hexdigest()[:16]


def test_the_streamed_rows_are_the_rows_the_list_wrote() -> None:
    """``memory_rows`` yields, in order, exactly the rows the list form wrote before #914, for
    the first build and for a grow continuing at an offset.

    Complement asserted: the grow's rows differ from the first build's, so the offset is
    honoured, not ignored."""
    story = _story()
    first = list(scale.memory_rows(SPEC, story, TURN_OPS))
    grown = list(scale.memory_rows(SPEC, story, TURN_OPS, count=500, offset=300))
    assert (len(first), len(grown)) == (300, 500)
    assert _digest(first) == FIRST_DIGEST
    assert _digest(grown) == GROWN_DIGEST
    assert _digest(scale.memory_plan(SPEC, story, TURN_OPS)) == FIRST_DIGEST
    assert _digest(grown) != _digest(first)


class _Sink:
    """A stand-in database for ``insert_memories``: each transaction's rows are counted and
    dropped, as the database keeps them out of the process."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield None


async def _peak(monkeypatch: pytest.MonkeyPatch, rows: Iterable[Mapping[str, Any]]) -> int:
    sink = _Sink()

    async def insert_derived(_conn: Any, _table: Any, chunk: list[Mapping[str, Any]]) -> None:
        sink.batches.append(len(chunk))

    monkeypatch.setattr(scale, "insert_derived", insert_derived)
    written = await scale.insert_memories(sink, rows)  # type: ignore[arg-type]
    assert written == sum(sink.batches)
    assert max(sink.batches) <= scale.MEMORY_BATCH
    _, peak = tracemalloc.get_traced_memory()
    return peak


async def test_a_streamed_grow_holds_one_batch_whatever_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing a grow through ``insert_memories(memory_rows(...))`` peaks at about one batch of
    rows, whether the grow adds four batches or twelve: the peak does not scale with the step.

    Complement asserted: the list form of the same twelve batches (``memory_plan``, what
    ``grow`` held before #914) peaks several times higher."""
    spec = scale.ScaleSpec(memories=1_000, events=200, facts=100, characters=6, dimension=256)
    story = _story()

    async def streamed(batches: int) -> int:
        tracemalloc.start()
        try:
            count = batches * scale.MEMORY_BATCH
            rows = scale.memory_rows(spec, story, TURN_OPS, count=count, offset=1_000)
            return await _peak(monkeypatch, rows)
        finally:
            tracemalloc.stop()

    small, large = await streamed(4), await streamed(12)
    tracemalloc.start()
    try:
        listed = scale.memory_plan(
            spec, story, TURN_OPS, count=12 * scale.MEMORY_BATCH, offset=1_000
        )
        whole = await _peak(monkeypatch, listed)
    finally:
        tracemalloc.stop()
    assert large < 1.5 * small
    assert whole > 4 * large
