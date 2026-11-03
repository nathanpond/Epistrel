"""Placing corrections the clock cannot (FR-STORE-20, FR-REWRITE-12; #240).

A correction whose valid start is an absolute time ("in 2012") against a partially anchored
clock may be undecidable at a checkpoint (``core.amendments.eligibility``). ``undecidable_at``
checks it at the story's start and at its head when the rewrite is planned; under the story's
``ambiguous_retcon_timing``:

- ``include_if_possible`` (the default): the part is stored ``timing_uncertain`` (it folds in
  wherever it might precede the checkpoint) and listed in the preview's ``timing_uncertain``;
- ``ask_author``: a required ``timing`` question, in every resolution mode, with the options
  ``before_story_start``, ``before_turn:{head}`` and ``after_turn:{head}``, recommended
  ``before_story_start`` for a year or a month and ``after_turn:{head}`` for a day. The answer,
  an option or free text (``parse_bound``: "after turn 40", "before turn 12", "before the story
  began", "at the start"), is stored on the part as its ``position_bound``.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.amendments.eligibility import (
    Eligibility,
    PartTiming,
    checkpoint_period,
    eligible,
    partial_period,
)
from epistrel.core.enums import Membership
from epistrel.core.membership import Position
from epistrel.core.tables import operations, stories

INCLUDE = "include_if_possible"
ASK = "ask_author"
_ABSOLUTE = re.compile(r"^\s*(?:in|since|from|during)?\s*(\d{4}(?:-\d{2}(?:-\d{2})?)?)\s*$", re.I)
_YEAR_ONLY = re.compile(r"^\s*\d{4}\s*$")
_TURN = re.compile(r"^\s*(before|after)\s+turn\s+(\d+)\s*$", re.I)
_START = re.compile(
    r"^\s*(?:before the story (?:began|begins|started)|at the (?:story's )?start|"
    r"before the start|before_story_start)\s*$",
    re.I,
)


def absolute(expression: str | None) -> str | None:
    """An absolute partial date (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, with ``in``/``since``),
    or ``None``. A bare four-digit number is a year only with a preposition (a bare number is a
    clock offset in minutes)."""
    if expression is None:
        return None
    match = _ABSOLUTE.match(expression)
    if match is None:
        return None
    if _YEAR_ONLY.match(expression):
        return None
    try:
        partial_period(match.group(1))
    except ValueError:
        return None
    return match.group(1)


async def setting(conn: AsyncConnection, story_id: str) -> str:
    """The story's ``ambiguous_retcon_timing`` (a story created before #240 may still hold it
    under ``settings.inactive``)."""
    found = await conn.scalar(select(stories.c.settings).where(stories.c.id == story_id))
    settings = dict(found or {})
    value = settings.get("ambiguous_retcon_timing")
    if value is None:
        inactive = dict(settings.get("inactive") or {}).get("ambiguous_retcon_timing")
        value = dict(inactive or {}).get("value")
    return ASK if value == ASK else INCLUDE


async def head(conn: AsyncConnection, story_id: str) -> int:
    found = await conn.scalar(select(stories.c.next_turn_index).where(stories.c.id == story_id))
    return max(int(found or 1) - 1, 0)


async def undecidable_at(conn: AsyncConnection, story_id: str, text: str) -> list[int]:
    """The checkpoints (the story's start, turn 0, and its head) at which a part beginning at
    the absolute ``text`` is undecidable."""
    from epistrel.core import world_clock

    timing = PartTiming(absolute=partial_period(text))
    found: list[int] = []
    last = await head(conn, story_id)
    for turn in sorted({0, last}):
        position = Position(turn) if turn else world_clock.HEAD
        try:
            minute = await world_clock.story_time_at(conn, story_id, position)
            anchor = await world_clock.anchor_at(conn, story_id, position)
            period = checkpoint_period(anchor.absolute_at(minute))
        except Exception:
            period, minute = None, None
        if eligible(timing, story_time=minute, absolute=period) is Eligibility.UNDECIDABLE:
            found.append(turn)
    return found


def options(head_turn: int, text: str) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """The timing question's options (label, effect) and the recommended index."""
    turn = max(head_turn, 1)
    choices: list[tuple[str, dict[str, Any]]] = [
        ("Before the story began", {"bound": {"story_start": True}}),
        (f"Before turn {turn}", {"bound": {"relation": "before", "turn": turn}}),
        (f"After turn {turn}", {"bound": {"relation": "after", "turn": turn}}),
    ]
    day = len(text.split("-")) == 3
    return choices, 2 if day else 0


def parse_bound(text: str, head_turn: int) -> dict[str, Any] | None:
    """A free-text placement as a position bound, or ``None`` when it cannot be read (never
    guessed): "before/after turn N" with N in 1..head, or the story's start."""
    if _START.match(text):
        return {"story_start": True}
    match = _TURN.match(text)
    if match is None:
        return None
    turn = int(match.group(2))
    if not 1 <= turn <= max(head_turn, 1):
        return None
    return {"relation": match.group(1).casefold(), "turn": turn}


async def with_variant(
    conn: AsyncConnection, story_id: str, bound: dict[str, Any]
) -> dict[str, Any]:
    """A turn bound with the variant of that turn active when the author answered (audit)."""
    if "turn" not in bound or "variant" in bound:
        return bound
    variant = await conn.scalar(
        select(operations.c.turn_variant).where(
            operations.c.story_id == story_id,
            operations.c.narrative_turn == int(bound["turn"]),
            operations.c.membership == Membership.ACTIVE.value,
            operations.c.kind == "turn",
        )
    )
    return {**bound, "variant": None if variant is None else int(variant)}


__all__ = [
    "ASK",
    "INCLUDE",
    "absolute",
    "head",
    "options",
    "parse_bound",
    "setting",
    "undecidable_at",
    "with_variant",
]
