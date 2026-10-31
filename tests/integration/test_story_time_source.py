"""Story-time is read off the world clock, nowhere else (FR-CLOCK-2; #193).

``core.world_clock.story_time_at(conn, story_id, position)`` is the one story-time entry point,
and the ``TimeBasis`` it builds (``time_basis``) the one pure form of it. The grep half: no module
under ``src/epistrel/`` outside ``core/world_clock/`` and ``core/resolution/time.py`` calls the
two-argument ``story_time_at(position, basis)`` or builds a ``TimeBasis`` (a clock-off default
comes from ``world_clock.clock_off_basis``). The writer half, on a story running the clock and
on one running it off: every event of a turn and of an inter-turn slot carries
``valid_time_minutes`` = ``story_time_at`` of its position; a State change carries it; a belief
transition's default ``valid_at`` (``core.epistemic.transitions``) and the resolver's basis
(``core.resolution.context.load_time_basis``) read the same minute. Complements: the clock-off
story's minutes follow the stand-in (ten a turn), not the clock, and a deployment's events carry
none.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import world_clock
from epistrel.core.epistemic.transitions import _story_time
from epistrel.core.membership import Position
from epistrel.core.resolution.context import load_time_basis
from tests import canon_support as c
from tests import clock_support as ck
from tests.event_support import rows

SRC = Path(__file__).resolve().parents[2] / "src" / "epistrel"
ALLOWED = (SRC / "core" / "world_clock", SRC / "core" / "resolution" / "time.py")
TWO_ARGUMENT = re.compile(r"(?<![\w.])story_time_at\(\s*[^,()]+(?:\([^()]*\))?\s*,\s*\w*basis")
BUILDS = re.compile(r"\bTimeBasis\(|default_factory=TimeBasis\b")


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if any(path == a or a in path.parents for a in ALLOWED):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (TWO_ARGUMENT, BUILDS):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(SRC)}:{line}: {match.group(0)}")
    return found


async def _events(engine: AsyncEngine, story: str) -> list[tuple[Position, int | None]]:
    found = await rows(
        engine,
        "SELECT narrative_turn, turn_variant, coalesce(slot_k, 0) AS slot_k, "
        "coalesce(beat, 0) AS beat, coalesce(step, 0) AS step, valid_time_minutes "
        "FROM events WHERE story_id = :s AND narrative_turn IS NOT NULL ORDER BY seq",
        s=story,
    )
    return [
        (
            Position(
                int(r.narrative_turn), int(r.turn_variant or 0), int(r.slot_k), r.beat, r.step
            ),
            r.valid_time_minutes,
        )
        for r in found
    ]


@pytest.mark.integration
async def test_story_time_source(engine: AsyncEngine, clock_delivered: None) -> None:
    assert _offenders() == []
    # The grep bites: a call and a construction outside the clock are both found.
    assert TWO_ARGUMENT.search("now = story_time_at(position.turn, basis)")
    assert TWO_ARGUMENT.search("x = story_time_at(N, TimeBasis(minute_zero=1))") or BUILDS.search(
        "x = story_time_at(N, TimeBasis(minute_zero=1))"
    )
    assert not TWO_ARGUMENT.search("await world_clock.story_time_at(conn, story_id, position)")

    async def nothing(conn: object, op: object) -> None:
        return None

    on = (await c.world(engine)).story_id
    off = (await c.world(engine)).story_id
    await ck.set_level(engine, on, "prompt_only")
    for story in (on, off):
        await c.run(engine, story, nothing)
    await ck.advance(engine, on, 125)
    for story in (on, off):
        await c.run(engine, story, nothing)
        await c.run(engine, story, nothing)

    async with engine.connect() as conn:
        for story in (on, off):
            written = await _events(engine, story)
            assert written, story
            for position, minutes in written:
                assert minutes == await world_clock.story_time_at(conn, story, position)
                assert minutes == await _story_time(conn, story, position)
                assert minutes == (await load_time_basis(conn, story)).at(position)
        # The clock's story: the slot and every later turn sit at its offset.
        assert sorted({m or 0 for _, m in await _events(engine, on)}) == [0, 125]
        # The clock-off story: ten minutes a turn, whatever the clock's row says.
        assert sorted({m or 0 for _, m in await _events(engine, off)}) == [10, 20, 30]
    changed = await rows(
        engine,
        "SELECT valid_time_minutes, narrative_turn FROM state_changes WHERE story_id = :s",
        s=on,
    )
    assert [(r.narrative_turn, r.valid_time_minutes) for r in changed] == [(1, 0)]
    deployment = await rows(
        engine,
        "SELECT count(*) AS n FROM events WHERE story_id IS NULL "
        "AND valid_time_minutes IS NOT NULL",
    )
    assert deployment[0].n == 0
