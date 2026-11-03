"""``core.retrieval.coverage_marks`` (#229, FR-PROF-8): a verdict under a coverage gap is marked,
not blocked, and a story with no gap marks nothing."""

from __future__ import annotations

from typing import Any

from epistrel.core.retrieval.coverage_marks import CoverageGap, gap_affected, mark_verdicts

GAP = CoverageGap(capability="pipeline", from_turn=1, to_turn=4)


def test_gap_affected__marks_a_claim_at_or_after_the_gap() -> None:
    assert gap_affected([GAP], 5) == {"capability": "pipeline", "through_turn": 4}
    assert gap_affected([GAP], 1) == {"capability": "pipeline", "through_turn": 4}
    assert gap_affected([CoverageGap(capability="pipeline", from_turn=6, to_turn=7)], 5) is None
    assert gap_affected([], 5) is None


def test_mark_verdicts__adds_the_mark_and_keeps_the_verdict() -> None:
    report: dict[str, Any] = {
        "verdicts": [{"claim_id": "c1", "verdict": "novel"}, {"claim_id": "c2"}]
    }
    mark_verdicts(report, [GAP], 5)
    assert [v["gap_affected"] for v in report["verdicts"]] == [
        {"capability": "pipeline", "through_turn": 4}
    ] * 2
    assert report["verdicts"][0]["verdict"] == "novel"
    clean: dict[str, Any] = {"verdicts": [{"claim_id": "c1"}]}
    mark_verdicts(clean, [], 5)
    assert "gap_affected" not in clean["verdicts"][0]
