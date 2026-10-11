"""The Lite rolling summary's pure parts (#72): the chunk grid, the merge choice, the prompt
label, and the ``lite_summary_segments`` projection's membership rules (no database)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from epistrel.core import summary_port
from epistrel.core.enums import OperationKind
from epistrel.core.projections import LITE_SUMMARY, Rows
from epistrel.core.summary import Segment, estimate_tokens, grid_end, merge_pair
from tests.test_projections import log_event

A, B, M, T = "A" * 26, "B" * 26, "M" * 26, "T" * 26
HEAD = {"next_turn_index": 3, "active_variants": {"1": 1, "2": 1}}


def _segment(level: int, start: int, end: int, op: str = A) -> Segment:
    return Segment(f"S{start}{end}{level}", op, start, end, level, "x" * 40)


def test_grid_cells_start_at_turn_one_and_never_split_a_turn() -> None:
    assert [grid_end(t, 5) for t in (1, 5, 6, 10, 11)] == [5, 5, 10, 10, 15]
    assert grid_end(3, 1) == 3
    assert estimate_tokens("x" * 403) == 100


def test_merge_takes_the_oldest_adjacent_pair_of_the_lowest_level() -> None:
    assert merge_pair([_segment(0, 1, 5)]) is None
    assert merge_pair([_segment(1, 1, 10), _segment(0, 11, 15), _segment(0, 16, 20)]) == 1
    assert merge_pair([_segment(0, 1, 5), _segment(0, 6, 10), _segment(0, 11, 15)]) == 0
    assert merge_pair([_segment(1, 1, 10), _segment(1, 11, 20), _segment(0, 21, 25)]) == 0
    # No two adjacent segments share a level: the two oldest merge across levels.
    assert merge_pair([_segment(2, 1, 20), _segment(1, 21, 30), _segment(0, 31, 35)]) == 0


def test_label_names_the_turns() -> None:
    assert _segment(0, 1, 5).label == "Turns 1-5: " + "x" * 40
    assert _segment(0, 3, 3).label == "Turn 3: " + "x" * 40


def _segment_event(seq: int, op: str, start: int, end: int, *sources: str, level: int = 0) -> Any:
    payload = {
        "text": f"{start}-{end}",
        "covers_from": start,
        "covers_to": end,
        "level": level,
        "source_versions": [{"operation_id": s, "membership": "active"} for s in sources],
    }
    return log_event(
        "lite_summary_segment",
        payload,
        seq=seq,
        operation_id=op,
        kind=OperationKind.CONSOLIDATION,
        turn=end,
    )


def _states(rows: Rows) -> dict[str, str]:
    return {r["operation_id"]: r["membership"] for r in rows}


def test_a_merged_segment_supersedes_its_children_while_it_is_active() -> None:
    rows = Rows(LITE_SUMMARY.table)
    for event in (
        _segment_event(1, A, 1, 1, T),
        _segment_event(2, B, 2, 2, T),
        _segment_event(3, M, 1, 2, A, B, level=1),
    ):
        LITE_SUMMARY.apply(rows, event)
    assert _states(rows) == {A: "superseded", B: "superseded", M: "active"}

    truncated = {"to_turn": 1, "prior_head": HEAD, "operation_ids": [B, M]}
    LITE_SUMMARY.apply(rows, log_event("truncate", truncated, seq=4, kind=OperationKind.CONTROL))
    assert _states(rows) == {A: "active", B: "discarded", M: "discarded"}

    restored = {"truncate_operation_id": T, "operation_ids": [B, M], "restored_head": HEAD}
    LITE_SUMMARY.apply(rows, log_event("untruncate", restored, seq=5, kind=OperationKind.CONTROL))
    assert _states(rows) == {A: "superseded", B: "superseded", M: "active"}

    # A regenerated head turn 2 makes what is positioned at 2 dormant; turn 1's child returns.
    LITE_SUMMARY.apply(rows, log_event("player_input", {"text": "again"}, seq=6, turn=2, variant=2))
    assert _states(rows) == {A: "active", B: "dormant", M: "dormant"}


def test_a_turn_over_a_rewound_tail_makes_its_segments_dormant() -> None:
    rows = Rows(LITE_SUMMARY.table)
    LITE_SUMMARY.apply(rows, _segment_event(1, A, 1, 1, T))
    LITE_SUMMARY.apply(rows, _segment_event(2, B, 2, 2, T))
    truncated = {"to_turn": 1, "prior_head": HEAD, "operation_ids": [B]}
    LITE_SUMMARY.apply(rows, log_event("truncate", truncated, seq=3, kind=OperationKind.CONTROL))
    LITE_SUMMARY.apply(rows, log_event("player_input", {"text": "new"}, seq=4, turn=2, variant=2))
    assert _states(rows) == {A: "active", B: "dormant"}


async def test_a_failing_scheduler_never_fails_the_commit_it_follows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(db: Any, story_id: str) -> bool:
        raise RuntimeError("story text must not reach the log")

    saved = summary_port.scheduler()
    summary_port.set_scheduler(broken)
    try:
        assert await summary_port.notify(cast(Any, None), "S" * 26) is False
    finally:
        summary_port.set_scheduler(saved)
    assert "RuntimeError" in caplog.text
    assert "story text" not in caplog.text
    summary_port.set_scheduler(None)
    try:
        assert await summary_port.notify(cast(Any, None), "S" * 26) is False
    finally:
        summary_port.set_scheduler(saved)
