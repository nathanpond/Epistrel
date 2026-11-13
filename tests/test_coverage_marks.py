"""``core.retrieval.coverage_marks`` (#229, FR-PROF-8): a verdict under a coverage gap is marked,
not blocked, and a story with no gap marks nothing. ``coverage.uncovered`` (#888), the ranges a
gap check reads, leaves out exactly the turns the intervals cover."""

from __future__ import annotations

import random
from typing import Any

from epistrel.core.backfill.coverage import Span, runs, uncovered
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


def test_uncovered__keeps_exactly_the_turns_no_interval_covers() -> None:
    """#888: the gaps read through ``uncovered`` are the turn-by-turn check's (the one before
    #888): for random intervals (open-ended, overlapping, adjacent, starting at 0) and random
    active turns, the runs of the turns inside ``uncovered``'s ranges are the runs of the
    turns no interval covers. Full coverage from turn 1 leaves no range."""

    def covered(turn: int, spans: list[Span]) -> bool:
        return any(low <= turn and (high is None or turn <= high) for low, high in spans)

    rng = random.Random(888)  # noqa: S311 - reproducible cases
    for _ in range(2_000):
        spans: list[Span] = []
        for _ in range(rng.randint(0, 4)):
            low = rng.randint(0, 40)
            spans.append((low, None if rng.random() < 0.25 else low + rng.randint(0, 10)))
        turns = sorted(rng.sample(range(1, 60), rng.randint(0, 40)))
        ranges = uncovered(spans)
        assert runs([t for t in turns if covered(t, ranges)]) == runs(
            [t for t in turns if not covered(t, spans)]
        ), (spans, turns, ranges)
    assert uncovered([(1, None)]) == []
    assert uncovered([(0, 3), (4, 9), (10, None)]) == []
    assert uncovered([]) == [(1, None)]
    assert uncovered([(1, 100), (104, None)]) == [(101, 103)]
    assert uncovered([(5, 7), (1, 2)]) == [(3, 4), (8, None)]
