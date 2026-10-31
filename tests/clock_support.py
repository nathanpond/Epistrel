"""Helpers for the world clock tests (#193): the ``clock`` module delivered for a test and
switched on for a story, a consumer's advance, and the clock read in a view.

``clock`` is #203's to deliver (``core.profiles.schema.DELIVERED``), so until then a test that
needs it patches the delivered set (``deliver``, or the ``clock_delivered`` fixture in
``tests/conftest.py``) and sets the story's ``clock.level`` override (``set_level``). A Lite
story runs the clock at ``prompt_only`` (FR-LITE-5's form; ``on`` needs the structured pipeline,
FR-PROF-4)."""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import world_clock
from epistrel.core.membership import Position
from epistrel.core.profiles import schema
from epistrel.core.states import View
from epistrel.core.world_clock.service import ClockAdvanceRequest, ClockMoved, advance_clock
from tests import canon_support as c

LEVELS = frozenset({"prompt_only", schema.ON})
"""The levels #203 delivers."""
YEAR = 525_960
"""A year of story-time in minutes (365.25 days)."""


def _with_clock() -> types.MappingProxyType[str, frozenset[str]]:
    return types.MappingProxyType({**schema.DELIVERED, "clock": LEVELS})


def deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver the ``clock`` module for the test (#203 delivers it for real)."""
    monkeypatch.setattr(schema, "DELIVERED", _with_clock())


@contextmanager
def delivered() -> Iterator[None]:
    """``deliver`` for a block (a helper no fixture reaches)."""
    with pytest.MonkeyPatch.context() as patch:
        deliver(patch)
        yield


CLOCK_OFF = text(
    "UPDATE story_profile_overrides SET settings = settings - 'clock', revision = revision + 1 "
    "WHERE settings -> 'clock' IS NOT NULL"
)
"""Every story's ``clock`` override dropped (the fixture's teardown)."""


async def set_level(engine: AsyncEngine, story_id: str, level: str | None) -> None:
    """Set the story's ``clock.level`` override (``None``: drop it, the profile's ``off``),
    keeping its other overrides."""
    async with engine.begin() as conn:
        if level is None:
            await conn.execute(
                text(
                    "UPDATE story_profile_overrides SET settings = settings - 'clock', "
                    "revision = revision + 1 WHERE story_id = :s"
                ),
                {"s": story_id},
            )
            return
        await conn.execute(
            text(
                "INSERT INTO story_profile_overrides (story_id, settings, revision) "
                "VALUES (:s, CAST(:o AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE "
                "SET settings = story_profile_overrides.settings || excluded.settings, "
                "revision = story_profile_overrides.revision + 1"
            ),
            {"s": story_id, "o": json.dumps({"clock": {"level": level}})},
        )


async def advance(
    engine: AsyncEngine,
    story_id: str,
    minutes: int | None = None,
    *,
    to: int | str | None = None,
    reason: str = "time passes",
) -> ClockMoved:
    """A consumer's advance between turns (``POST /stories/{id}/clock/advance``'s call)."""
    return await advance_clock(
        engine,
        story_id,
        ClockAdvanceRequest(minutes=minutes, to=to, reason=reason),
        principal=c.PRINCIPAL,
    )


async def offset(
    engine: AsyncEngine,
    story_id: str,
    through: Position = world_clock.HEAD,
    *,
    view: View = View.ACTIVE,
) -> int:
    """The clock's offset in minutes at ``through`` (the story as it stands by default)."""
    async with engine.connect() as conn:
        return (await world_clock.clock_at(conn, story_id, through, view=view)).offset_minutes


async def clock_changes(engine: AsyncEngine, story_id: str) -> int:
    """How many ``state_changes`` rows the story's clock has, whatever their membership."""
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT count(*) FROM state_changes c JOIN state_definitions d "
                "ON d.id = c.state_id WHERE c.story_id = :s AND d.name = 'world_clock'"
            ),
            {"s": story_id},
        )
    return int(found or 0)


# -- scheduled events (#196) ----------------------------------------------------------------------

STOPS = "scheduled_stops"
"""The profile flag (``clock.scheduled_stops``) whose stops #203 delivers with the clock."""


def _with_stops() -> types.MappingProxyType[str, frozenset[str]]:
    return types.MappingProxyType(
        {**schema.DELIVERED, "clock": LEVELS, STOPS: frozenset({schema.ON})}
    )


def deliver_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver ``clock`` and ``scheduled_stops`` for the test (#203 delivers them for real)."""
    monkeypatch.setattr(schema, "DELIVERED", _with_stops())


TIME_CLAIMS = "time_claims"
"""#200: the profile flag (``clock.time_claims``) that checks a character's time claims against
its time belief, which #203 delivers with the clock."""


def deliver_time_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver ``clock`` with ``scheduled_stops`` and ``time_claims`` for the test (#203)."""
    monkeypatch.setattr(
        schema,
        "DELIVERED",
        types.MappingProxyType({**_with_stops(), TIME_CLAIMS: frozenset({schema.ON})}),
    )


async def set_awareness(engine: AsyncEngine, story_id: str, awareness: str) -> None:
    """The ``world_clock`` definition's ``time_awareness`` (#200; the API's ``PATCH /clock``
    sets it the same way, and #202 seeds it from the scenario)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE state_definitions SET time_awareness = CAST(:a AS time_awareness) "
                "WHERE story_id = :s AND name = 'world_clock'"
            ),
            {"a": awareness, "s": story_id},
        )


async def set_clock(
    engine: AsyncEngine,
    story_id: str,
    level: str = "on",
    *,
    stops: bool = True,
    time_claims: bool | None = None,
) -> None:
    """Set the story's ``clock`` override: ``level`` and the ``scheduled_stops`` flag (the
    profile's switch for due-event stops; the clock's own ``stop_at_due_events`` is the
    other), and #200's ``time_claims`` flag when given."""
    found: dict[str, Any] = {"level": level, STOPS: stops}
    if time_claims is not None:
        found[TIME_CLAIMS] = time_claims
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO story_profile_overrides (story_id, settings, revision) "
                "VALUES (:s, CAST(:o AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE "
                "SET settings = story_profile_overrides.settings || excluded.settings, "
                "revision = story_profile_overrides.revision + 1"
            ),
            {"s": story_id, "o": json.dumps({"clock": found})},
        )


async def anchor_at(engine: AsyncEngine, story_id: str, minute: int) -> None:
    """The author's starting anchor: the exact minute of the day ``minute`` at offset 0 (no
    route sets it)."""
    from epistrel.core.world_clock.anchor import statement_of

    hour, meridian = (minute // 60) % 12 or 12, "pm" if minute >= 720 else "am"
    statement = statement_of(f"{hour}:{minute % 60:02d} {meridian}")  # one reading only
    assert statement is not None
    await _set_anchor(engine, story_id, statement)


async def anchor_range(engine: AsyncEngine, story_id: str, lo: int, hi: int) -> None:
    """The author's starting anchor: only a range, the minutes ``[lo, hi)`` of the day, at
    offset 0 (#197)."""
    from epistrel.core.world_clock.anchor import Statement

    await _set_anchor(engine, story_id, Statement(time=((lo, hi),)))


async def _set_anchor(engine: AsyncEngine, story_id: str, statement: Any) -> None:
    from epistrel.core.world_clock.anchor import Anchor

    found = Anchor.unanchored().refine(statement, 0, Position(0))
    assert isinstance(found, Anchor)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE state_definitions SET anchor = CAST(:a AS jsonb) "
                "WHERE story_id = :s AND name = 'world_clock'"
            ),
            {"a": json.dumps(found.document()), "s": story_id},
        )


async def events(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's scheduled events as it stands (``core.world_clock.stops.events_at``)."""
    from epistrel.core.world_clock import stops

    async with engine.connect() as conn:
        return list(await stops.events_at(conn, story_id))


async def statuses(engine: AsyncEngine, story_id: str, event_id: str) -> list[str]:
    """Every lifecycle step of one event on the active timeline, in order (its
    ``scheduled_event`` events)."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT e.payload->>'status' AS status FROM events e "
                "JOIN operations o ON o.operation_id = e.operation_id "
                "WHERE e.story_id = :s AND e.type = 'scheduled_event' "
                "AND e.payload->>'event_id' = :e AND o.membership = 'active' ORDER BY e.seq"
            ),
            {"s": story_id, "e": event_id},
        )
        return [str(r.status) for r in found]
