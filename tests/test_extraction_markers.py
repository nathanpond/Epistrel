"""The extraction backstop's beat markers (#459): the request numbers its beats ``0..n-1`` and
``to_claims`` maps each claim's marker back to the beat it was shown, so a correct claim is
never silently dropped because the model numbered beats from 0 while a render's beats start at
turn index 2."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.verify.detect import MESSAGE, DetectBeat, DetectionEnv
from epistrel.orchestrator.models.registry import ExtractedClaim, ExtractionResult, SceneContext
from epistrel.orchestrator.verify.extraction import extraction_inputs, to_claims

LINDA = "Cal and I were married once."
MARCUS = "Marcus used to sail with the harbour crews."
NO_ENV = cast(DetectionEnv, None)  # every beat here is the narrator's: no name lookup


async def _none(*_: Any) -> None:
    return None


async def _claims(
    beats: list[DetectBeat], *answers: ExtractedClaim
) -> list[tuple[int | None, int, int]]:
    _, by_index = extraction_inputs(beats, NO_ENV, SceneContext())
    shown = {(None if b.channel == MESSAGE else b.index): b for b in beats}
    found = await to_claims(
        ExtractionResult(claims=list(answers)),
        shown,
        (3, 0),
        normalize_one=_none,
        by_index=by_index,
    )
    return [c.span for c in found]


def test_request_numbers_beats_from_zero() -> None:
    beats = [DetectBeat(2, "say", LINDA, NARRATOR), DetectBeat(3, "narration", MARCUS, NARRATOR)]
    inputs, by_index = extraction_inputs(beats, NO_ENV, SceneContext())
    assert [b.index for b in inputs.beats] == [0, 1]
    assert by_index == {0: 2, 1: 3}


async def test_beat_zero_on_a_render_beat_marked_two_is_kept() -> None:
    """The live probe's answer: one render beat at turn index 2, the model answers ``beat: 0``."""
    spans = await _claims(
        [DetectBeat(2, "say", LINDA, NARRATOR)],
        ExtractedClaim(beat=0, span="Cal and I were married once", subject="Linda"),
    )
    assert spans == [(2, 0, len("Cal and I were married once"))]


async def test_single_beat_request_accepts_any_marker() -> None:
    spans = await _claims(
        [DetectBeat(2, "say", LINDA, NARRATOR)],
        ExtractedClaim(beat=2, span="married once"),
    )
    assert spans == [(2, LINDA.index("married"), len(LINDA) - 1)]


async def test_markers_map_back_to_their_beats() -> None:
    beats = [DetectBeat(2, "say", LINDA, NARRATOR), DetectBeat(3, "narration", MARCUS, NARRATOR)]
    spans = await _claims(
        beats,
        ExtractedClaim(beat=1, span="used to sail"),
        ExtractedClaim(beat=0, span="married once"),
    )
    assert sorted(s[0] or 0 for s in spans) == [2, 3]


async def test_unknown_marker_is_placed_by_its_span_or_dropped_visibly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    beats = [DetectBeat(2, "say", LINDA, NARRATOR), DetectBeat(3, "narration", MARCUS, NARRATOR)]
    with caplog.at_level(logging.WARNING, logger="epistrel.orchestrator.verify.extraction"):
        spans = await _claims(
            beats,
            ExtractedClaim(beat=7, span="used to sail"),  # rescued: only beat 3 holds it
            ExtractedClaim(beat=7, span="a ship that never was"),  # nowhere: dropped, logged
        )
    assert [s[0] for s in spans] == [3]
    (record,) = [r for r in caplog.records if "beat marker" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "1 of 2 claims" in record.getMessage()
    assert "never was" not in record.getMessage()  # counts only, no story content


async def test_composed_message_keeps_its_none_key() -> None:
    spans = await _claims(
        [DetectBeat(0, MESSAGE, MARCUS, NARRATOR)],
        ExtractedClaim(beat=0, span="harbour crews"),
    )
    assert spans == [
        (None, MARCUS.index("harbour"), MARCUS.index("harbour") + len("harbour crews"))
    ]
