"""U-10 (M4): importance decay and retrieval ranking math on fixtures (FR-MEM-7, FR-RET-5;
docs/04 §3). One file per test ID and milestone: #135's ``test_u_10__importance`` is here, and
#141's ``test_u_10__ranking`` joins it."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from epistrel.core.memory.records import DEFAULT_IMPORTANCE, decayed_importance
from epistrel.core.profiles.schema import Episodic
from epistrel.core.retrieval.rank import Candidate, Weights, fuse, rank, recency, score
from epistrel.core.settings_schema import DEFAULT_IMPORTANCE_HALF_LIFE_TURNS, MemorySettings
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    IMPORTANCE_SCORING,
    RESERVED,
    ImportanceScore,
)
from epistrel.orchestrator.models.stub import digest_score

FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "sets" / "importance" / "cases.jsonl"
"""E-04's labeled pivotal/trivial set (#157's ``importance`` set), shared with the eval."""


def _cases() -> list[dict[str, object]]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


@pytest.mark.part("M4", "FR-MEM-7")
def test_u_10__importance() -> None:
    """Importance is assignable through the Model Access Layer and decays (FR-MEM-7): the
    ``importance_scoring`` purpose is delivered (off the hot path, ``{text, kind}`` →
    ``{importance: 0..1}``); ``decayed_importance`` halves a score every half-life of committed
    turns (default 200), counts an unscored memory as 0.5, and does not decay a memory with no
    age. On E-04's fixture, every pivotal memory outranks every trivial one after decay, the
    pivotal ones being up to 180 turns older. Complement: without decay a trivial memory
    scored above a pivotal one would win; decay alone never reorders two memories of one age."""
    assert "importance_scoring" in DELIVERED
    assert "importance_scoring" not in RESERVED
    assert IMPORTANCE_SCORING.hot_path is False
    assert set(IMPORTANCE_SCORING.input_schema.model_fields) == {"text", "kind"}
    assert IMPORTANCE_SCORING.response_schema is ImportanceScore
    with pytest.raises(ValueError, match="less than or equal to 1"):
        ImportanceScore.model_validate({"importance": 1.5})

    half = DEFAULT_IMPORTANCE_HALF_LIFE_TURNS
    assert MemorySettings().memory_importance_half_life_turns == half == 200
    assert decayed_importance(0.8, 10, 10, half) == pytest.approx(0.8)
    assert decayed_importance(0.8, 10, 210, half) == pytest.approx(0.4)
    assert decayed_importance(0.8, 10, 410, half) == pytest.approx(0.2)
    assert decayed_importance(0.8, 0, 100, half) == pytest.approx(0.8 / math.sqrt(2))
    assert decayed_importance(None, 0, 200, half) == pytest.approx(DEFAULT_IMPORTANCE / 2)
    assert decayed_importance(0.8, None, 500, half) == pytest.approx(0.8)
    assert decayed_importance(0.8, 50, 10, half) == pytest.approx(0.8)  # after a truncation
    with pytest.raises(ValueError, match="half-life"):
        decayed_importance(0.5, 0, 1, 0)
    # Decay is monotone and order-preserving at one age.
    assert decayed_importance(0.9, 0, 300, half) > decayed_importance(0.3, 0, 300, half)

    cases = _cases()
    assert {c["expected"] for c in cases} == {"pivotal", "trivial"}
    now = 200
    ranked: dict[str, list[float]] = {"pivotal": [], "trivial": []}
    for case in cases:
        inputs = case["inputs"]
        assert isinstance(inputs, dict)
        score = ImportanceScore.model_validate_json(str(case["stub_answer"])).importance
        at = now - int(inputs["age_turns"])
        ranked[str(case["expected"])].append(decayed_importance(score, at, now, half))
    assert min(ranked["pivotal"]) > max(ranked["trivial"])
    # Complement: a stale trivial score above a fresh pivotal one is not fixed by decay.
    assert decayed_importance(0.9, now, now, half) > decayed_importance(0.5, now, now, half)
    assert decayed_importance(0.5, now - 180, now, half) < decayed_importance(0.45, now, now, half)


def test_the_stub_scores_by_text() -> None:
    """The stub's importance is the SHA-256 of the text, in 0..1: the same in every process,
    different for different texts."""
    one, two = digest_score("episodic memory: a"), digest_score("episodic memory: b")
    assert 0.0 <= one <= 1.0
    assert 0.0 <= two <= 1.0
    assert one == digest_score("episodic memory: a")
    assert one != two


@pytest.mark.part("M4", "FR-RET-5")
def test_u_10__ranking() -> None:
    """Retrieval ranking combines semantic similarity, recency decay and importance
    (FR-RET-5): reciprocal-rank fusion scales a memory ranked first by every arm that ran to
    1.0 and ranks the arms' agreement above one arm's; recency halves every half-life of turns
    before the query's position (none for a position-less memory, no negative age); the score
    is ``w_sim·fused + w_rec·recency + w_imp·decayed importance`` with the profile's weights,
    ties by story position (turn, then beat, earliest first), then text, then id (#476: ids
    differ between two replays). Complements: each weight alone orders by its own term (a zero
    weight removes its term's influence), and the order is the same on every run."""
    # Fusion.
    fused = fuse([["a", "b", "c"], ["b", "a"], ["b"]], k=60)
    assert fused["b"] > fused["a"] > fused["c"]
    assert fuse([["x"], ["x"]], k=60)["x"] == pytest.approx(1.0)
    assert fuse([["x"], []], k=60)["x"] == pytest.approx(0.5)  # an arm that ran and found none
    assert fuse([], k=60) == {}
    with pytest.raises(ValueError, match="positive"):
        fuse([["x"]], k=0)
    # Recency.
    assert recency(100, 100, 100) == 1.0
    assert recency(0, 100, 100) == pytest.approx(0.5)
    assert recency(0, 200, 100) == pytest.approx(0.25)
    assert recency(150, 100, 100) == 1.0
    assert recency(None, 100, 100) == 0.0
    # The score, with the profile's default weights (0.6 / 0.2 / 0.2).
    defaults = Episodic()
    weights = Weights(
        defaults.weight_similarity, defaults.weight_recency, defaults.weight_importance
    )
    assert (weights.similarity, weights.recency, weights.importance) == (0.6, 0.2, 0.2)
    assert defaults.recency_half_life_turns == 100
    assert score(1.0, 0.5, 0.25, weights) == pytest.approx(0.6 + 0.1 + 0.05)
    with pytest.raises(ValueError, match="negative"):
        Weights(-0.1, 0.2, 0.2)

    candidates = [
        Candidate("similar", turn=10, importance=0.1, importance_at=10),
        Candidate("recent", turn=99, importance=0.1, importance_at=99),
        Candidate("weighty", turn=10, importance=1.0, importance_at=99),
        Candidate("unscored", turn=None, importance=None, importance_at=None),
    ]
    similarity = {"similar": 1.0, "recent": 0.1, "weighty": 0.1}

    def order(w: Weights) -> list[str]:
        ranked = rank(
            candidates,
            similarity,
            now=100,
            weights=w,
            recency_half_life=100,
            importance_half_life=200,
        )
        return [r.memory_id for r in ranked]

    assert order(weights)[0] == "similar"
    assert order(Weights(1.0, 0.0, 0.0))[0] == "similar"
    assert order(Weights(0.0, 1.0, 0.0))[0] == "recent"
    assert order(Weights(0.0, 0.0, 1.0))[0] == "weighty"
    # The unscored, position-less memory counts its importance as 0.5 and no recency.
    (unscored,) = [
        r
        for r in rank(
            candidates,
            similarity,
            now=100,
            weights=Weights(0.0, 1.0, 1.0),
            recency_half_life=100,
            importance_half_life=200,
        )
        if r.memory_id == "unscored"
    ]
    assert (unscored.similarity, unscored.recency) == (0.0, 0.0)
    assert unscored.importance == pytest.approx(DEFAULT_IMPORTANCE)
    # Ties by story position, then text, and only then by id, deterministic (#476).
    tied = [Candidate(i, turn=50, importance=0.5, importance_at=50) for i in ("b", "a", "c")]
    once = rank(tied, {}, now=100, weights=weights, recency_half_life=100, importance_half_life=200)
    assert [r.memory_id for r in once] == ["a", "b", "c"]
    by_position = [
        Candidate("a-beat-3", turn=50, importance=0.5, importance_at=50, beat=3, text="x"),
        Candidate("b-beat-1-z", turn=50, importance=0.5, importance_at=50, beat=1, text="z"),
        Candidate("c-beat-1-y", turn=50, importance=0.5, importance_at=50, beat=1, text="y"),
        Candidate("d-no-beat", turn=50, importance=0.5, importance_at=50, beat=None, text="w"),
    ]
    ranked = rank(
        by_position, {}, now=100, weights=weights, recency_half_life=100, importance_half_life=200
    )
    assert len({r.score for r in ranked}) == 1
    assert [r.memory_id for r in ranked] == ["c-beat-1-y", "b-beat-1-z", "a-beat-3", "d-no-beat"]
    assert order(weights) == order(weights)
