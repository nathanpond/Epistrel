"""#652: the repair ladder's safe-fallback rung gives a narrator beat the narrator's own line,
never the first render character's stored line."""

from __future__ import annotations

from epistrel.orchestrator.policies.output_gate import FallbackLines
from epistrel.orchestrator.repair.ladder import NARRATOR_FALLBACK, RepairLadder


async def _ok(text: str) -> bool:
    return True


async def _no_narrator_line(text: str) -> bool:
    return text != NARRATOR_FALLBACK


def _ladder(output_ok: object) -> RepairLadder:
    ladder = object.__new__(RepairLadder)
    ladder.fallback = FallbackLines(name="Mira", character_text="Mira changes the subject.")
    ladder.output_ok = output_ok  # type: ignore[assignment]
    return ladder


async def test_a_narrator_beat_falls_back_to_the_narrators_line() -> None:
    """Complement asserted: a character beat keeps the character's line, and a narrator line
    the output policies refuse falls through to the stored ones."""
    ladder = _ladder(_ok)
    assert await ladder._fallback_line(narrator=True) == NARRATOR_FALLBACK
    assert await ladder._fallback_line() == "Mira changes the subject."
    refused = _ladder(_no_narrator_line)
    assert await refused._fallback_line(narrator=True) == "Mira changes the subject."
