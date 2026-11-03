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

- ``plan_skip`` (#632, FR-CLOCK-7,8 on the Lite path) reads a narrated time skip in the reply
  deterministically (``core.world_clock.skips``: a stated amount, or an activity narrated as
  elapsing), classifies it (``classify_skip``: the player's input or the author's Lite notes
  invite it, else the model started it) and bounds it (``authorize``: a model-initiated skip
  needs an activity the verbatim window established and stays within
  ``max_unprompted_skip_minutes``, 0 refusing every one). A skip with no stated amount is
  estimated by at most one ``time_estimation`` call (#201), within the turn's budget, before
  the commit. An accepted skip **replaces** the turn's pacing advance (one advance, source
  ``declared_skip`` or ``estimate``); a refused one is not applied (Lite has no repair: the
  reply stands and the clock moves by pacing only) and the turn report lists why.

Nothing else of the clock or States runs under Lite: no claim about time is verified, no
scheduled event stops the clock, and a reply declares no State effect (Lite has no
``effects`` channel); States change only through the State API (FR-STATE-9). Log lines carry
counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import world_clock
from epistrel.core.enums import StateChangeSource
from epistrel.core.events import Operation
from epistrel.core.lite_context import LiteContext, read_lite_context
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.turns import REPLY_BEAT
from epistrel.core.world_clock import external_mode
from epistrel.core.world_clock.pacing import pacing_increment

PACING_REASON = "pacing"
SKIP_REASON = "narrated time skip"
"""The reason a Lite turn's pacing advance records (#194's structured advances' own)."""


@dataclass(frozen=True, slots=True)
class _Line:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class LiteSkip:
    """A narrated skip the reply makes (#632): its minutes and source when accepted, or why it
    was refused (``refused``; nothing applied)."""

    minutes: int = 0
    source: StateChangeSource = StateChangeSource.DECLARED_SKIP
    initiated_by: str = ""
    refused: str | None = None
    estimated: bool = False

    def report(self) -> dict[str, Any]:
        if self.refused is not None:
            return {"skips_refused": [{"reason": self.refused}]}
        return {
            "skip": {
                "minutes": self.minutes,
                "source": self.source.value,
                "initiated_by": self.initiated_by,
                "estimated": self.estimated,
            }
        }


UNESTIMATED = "unestimated"


async def plan_skip(
    db: AsyncEngine,
    models: Any,
    story_id: str,
    *,
    reply: str,
    player_input: str,
    notes: Sequence[str],
    window: Sequence[str],
    budget: Any,
) -> LiteSkip | None:
    """The reply's narrated skip (see the module docstring); ``None`` when it narrates none or
    the story runs no paced clock. Runs before the commit (no transaction across a model
    call, FR-CONC-2)."""
    from epistrel.core.enums import ClockMode
    from epistrel.core.world_clock import skips
    from epistrel.orchestrator.structured import clocking
    from epistrel.orchestrator.structured import time_estimation as estimation

    if not skips.narrates_elapsing(reply):
        return None
    stated = skips.stated_minutes(reply)
    async with db.connect() as conn:
        clock = await clocking.read_clock(conn, story_id)
    if clock is None or clock.external or clock.mode is not ClockMode.PACED:
        return None
    classified = skips.classify_skip(skips.Skip(text=reply), player_input, notes)
    minutes, source, estimated = stated, StateChangeSource.DECLARED_SKIP, False
    if minutes is None:
        found = None
        if budget.try_optional(estimation.TIME_ESTIMATION_KIND, 1):
            mine = skips.activities(reply)
            found = await estimation.estimate(
                models,
                narrated_text=reply,
                activity_context=" ".join(t for t in window if skips.activities(t) & mine),
                clock_minute=clock.minute,
                day_minutes=clock.clockwork.day_minutes,
                budget=budget,
            )
        if found is None:
            return LiteSkip(refused=UNESTIMATED)
        minutes, source, estimated = found.minutes, StateChangeSource.ESTIMATE, True
    verdict = skips.authorize(
        classified, minutes=minutes, cap=clock.max_unprompted, established=skips.established(window)
    )
    if isinstance(verdict, skips.Refused):
        return LiteSkip(source=source, refused=verdict.reason, estimated=estimated)
    return LiteSkip(minutes, source, str(verdict.initiated_by), estimated=estimated)


async def pace(
    conn: AsyncConnection,
    op: Operation,
    story_id: str,
    *,
    say: str,
    reply: str,
    skip: LiteSkip | None = None,
) -> int:
    """The turn's one advance, in ``op``'s commit (see the module docstring): an accepted
    narrated skip's (#632) in place of the pacing increment; the minutes it advanced the clock
    (0: none written)."""
    if await world_clock.clock_level(conn, story_id) is None or op.narrative_turn is None:
        return 0
    definition = await world_clock.clock_definition(conn, story_id)
    if external_mode.is_external(definition):
        return 0  # #209: the provider owns the clock
    found = definition.clock
    assert found is not None  # noqa: S101 - the clock's definition carries its settings
    if skip is not None and skip.refused is None and skip.minutes > 0:
        await world_clock.advance(
            conn,
            op,
            story_id,
            minutes=skip.minutes,
            source=skip.source,
            position=Position(
                int(op.narrative_turn), int(op.turn_variant or 0), 0, REPLY_BEAT, POSITION_MAX
            ),
            reason=SKIP_REASON,
        )
        return skip.minutes
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


__all__ = [
    "PACING_REASON",
    "SKIP_REASON",
    "LiteContext",
    "LiteSkip",
    "pace",
    "plan_skip",
    "read_lite_context",
]
