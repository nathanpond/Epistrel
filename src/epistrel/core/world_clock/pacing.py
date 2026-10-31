"""The pacing rule: a render advances the clock by its spoken and acted length (FR-CLOCK-6;
docs/02 §7.1; #194).

In ``paced`` mode each **render** (FR-CLOCK-17's beat: one character's or the narrator's lines
in one turn, M5's ``BeatDecl``s sharing a ``TurnStage.open_render`` first beat; the player's
input is a render of its own) advances the clock by the words of its ``say`` and ``do`` lines,
the narrator's ``narration`` included (reactive narration counts inside the render that narrates
it, never separately). ``think`` and ``latent`` lines, a line marked ``flashback`` and the
State-effect declarations count nothing.

- ``word_count(text)``: whitespace-delimited words once bracketed stage directions
  (``[...]``) are stripped; a hyphenated word is one word.
- ``pacing_increment(render_beats, table, mode=)``: the minutes the story's ``pacing_table``
  (``[[lo, hi | null, minutes]]``, half-open word ranges ``[lo, hi)``; the default is fewer
  than 5 words → 0, 5 to 50 → 1, more than 50 → 2) gives the render's word count; ``manual``
  (and ``external``, whose provider owns the clock) always 0.
- ``validate_pacing_table``: the table's shape (``core.states.model.check_pacing_table``,
  which every definition, genesis and ``PATCH /clock`` runs).

Sequential renders add up and simultaneous renders take the largest
(``core.world_clock.timing``). Pure: no I/O and no model call (U-20).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

from epistrel.core.enums import ClockMode
from epistrel.core.states.model import DEFAULT_PACING_TABLE, check_pacing_table

PACED_KINDS = frozenset({"say", "do", "narration"})
"""The lines a render's pacing counts: what is spoken and acted (the narrator's narration is
its acted channel); ``think`` and ``latent`` count nothing."""
_STAGE_DIRECTION = re.compile(r"\[[^\]]*\]")

PacingTable = tuple[tuple[int, int | None, int], ...]


class PacedLine(Protocol):
    """A line pacing reads: its kind and text (and ``flashback``, when it has one)."""

    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...


def validate_pacing_table(table: Sequence[Sequence[int | None]]) -> PacingTable:
    """``table`` checked (422 ``validation_error``, ``reason: shape``) and as tuples."""
    return check_pacing_table(table)


def word_count(text: str) -> int:
    """The words of ``text`` once its bracketed stage directions are stripped."""
    return len(_STAGE_DIRECTION.sub(" ", text).split())


def render_words(render_beats: Iterable[PacedLine]) -> int:
    """The spoken and acted words of one render's lines (see the module docstring)."""
    return sum(
        word_count(line.text)
        for line in render_beats
        if line.kind in PACED_KINDS and not getattr(line, "flashback", False)
    )


def minutes_for(words: int, table: PacingTable = DEFAULT_PACING_TABLE) -> int:
    """The minutes ``table`` gives ``words`` (the row whose ``[lo, hi)`` holds it)."""
    for lo, hi, minutes in table:
        if words >= lo and (hi is None or words < hi):
            return minutes
    return 0  # pragma: no cover - a validated table covers every count from 0


def pacing_increment(
    render_beats: Iterable[PacedLine],
    table: PacingTable = DEFAULT_PACING_TABLE,
    *,
    mode: ClockMode | str = ClockMode.PACED,
) -> int:
    """The minutes one render advances the clock by (see the module docstring)."""
    if ClockMode(mode) is not ClockMode.PACED:
        return 0
    return minutes_for(render_words(render_beats), table)


__all__ = [
    "PACED_KINDS",
    "PacedLine",
    "PacingTable",
    "minutes_for",
    "pacing_increment",
    "render_words",
    "validate_pacing_table",
    "word_count",
]
