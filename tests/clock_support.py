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
