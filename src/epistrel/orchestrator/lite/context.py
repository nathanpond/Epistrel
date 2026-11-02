"""The prompt-only world clock and States of a Lite turn (FR-LITE-5, FR-CLOCK-5,6; #203).

Under ``pipeline: lite`` the clock and States run only in their prompt-only forms
(``clock.level`` / ``states.level`` = ``prompt_only``: ``on`` needs the structured pipeline,
FR-PROF-4):

- ``read_lite_context`` (``core.lite_context``, #662: the Lite read set's ``time`` and
  ``tracked_values`` members read the same) reads, in the turn's read snapshot, what the single
  Lite prompt shows:
  ``## Time`` (``time_lines``: the clock as the turn starts, as the anchor shows it or as an
  offset from the story's start, then each open canon scheduled event with its planned time)
  with the clock on, and ``## Tracked values`` (``tracked_values``: every player-visible State
  the story owns, with its value; a hidden one, an externally owned one and the built-in
  ``world_clock`` are left out) with States on. Off, a section is empty and the prompt leaves
  it out. A regenerate reads the clock as its turn started.
- ``pace`` is the turn's one pacing advance (FR-CLOCK-6; #194's ``pacing_increment``): the
  words of the player's input and of the reply, counted as one combined render through the
  story's pacing table, written in the turn's commit as one ``pacing`` advance at the reply's
  beat (``manual`` mode and an ``external`` clock write none). It is #194's deterministic rule:
  no model call.

Nothing else of the clock or States runs under Lite: no claim about time is verified, no
scheduled event stops the clock, no skip is estimated or bounded, and a reply declares no
State effect (Lite has no ``effects`` channel); States change only through the State API
(FR-STATE-9). Log lines carry counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import world_clock
from epistrel.core.enums import StateChangeSource
from epistrel.core.events import Operation
from epistrel.core.lite_context import LiteContext, read_lite_context
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.turns import REPLY_BEAT
from epistrel.core.world_clock import external_mode
from epistrel.core.world_clock.pacing import pacing_increment

PACING_REASON = "pacing"
"""The reason a Lite turn's pacing advance records (#194's structured advances' own)."""


@dataclass(frozen=True, slots=True)
class _Line:
    kind: str
    text: str


async def pace(conn: AsyncConnection, op: Operation, story_id: str, *, say: str, reply: str) -> int:
    """The turn's one pacing advance, in ``op``'s commit (see the module docstring); the
    minutes it advanced the clock (0: none written)."""
    if await world_clock.clock_level(conn, story_id) is None or op.narrative_turn is None:
        return 0
    definition = await world_clock.clock_definition(conn, story_id)
    if external_mode.is_external(definition):
        return 0  # #209: the provider owns the clock
    found = definition.clock
    assert found is not None  # noqa: S101 - the clock's definition carries its settings
    minutes = pacing_increment(
        [_Line("say", say), _Line("narration", reply)],
        tuple(found.pacing_table),
        mode=found.mode,
    )
    if minutes <= 0:
        return 0
    turn, variant = int(op.narrative_turn), int(op.turn_variant or 0)
    await world_clock.advance(
        conn,
        op,
        story_id,
        minutes=minutes,
        source=StateChangeSource.PACING,
        position=Position(turn, variant, 0, REPLY_BEAT, POSITION_MAX),
        reason=PACING_REASON,
    )
    return minutes


__all__ = ["PACING_REASON", "LiteContext", "pace", "read_lite_context"]
