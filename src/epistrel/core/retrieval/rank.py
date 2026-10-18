"""The retrieval ranking math (FR-RET-5, FR-MEM-7; U-10; #141). Pure: no I/O, no model.

A retrieval's candidates come from up to three search arms (vector, lexical, exact alias). The
arms are fused by **reciprocal-rank fusion** and the fused similarity is combined with recency
and decayed importance:

- ``fuse(rankings, k)``: each arm's list, best first, gives a memory ``1 / (k + rank)`` (rank
  from 1); the sum over the arms is scaled by ``(k + 1) / arms``, where ``arms`` is the number
  of arms that ran. A memory ranked first by every arm that ran fuses to 1.0, one no arm found
  to 0.0. ``k`` is ``retrieval_rrf_k`` (60, the usual constant).
- ``recency(turn, now, half_life)``: ``0.5 ^ ((now - turn) / half_life)`` over turns before
  the query's position (``memory.episodic.recency_half_life_turns``, default 100). A memory with
  no position counts as 0.0, and a memory at or after ``now`` counts as 1.0 (no negative age;
  the pipeline excludes memories after the position anyway).
- ``decayed_importance`` is #135's (``core.memory.records``): ``importance · 0.5^(age /
  memory_importance_half_life_turns)`` from the turn it holds from, an unscored memory counting
  as ``DEFAULT_IMPORTANCE``.
- ``score = w_sim · fused + w_rec · recency + w_imp · decayed_importance`` with the profile's
  ``memory.episodic.weight_*`` dials (0.6 / 0.2 / 0.2 by default; they need not sum to 1).

``rank`` orders by score, best first, ties by memory id, so the same inputs always give the
same order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from epistrel.core.memory.records import decayed_importance

DEFAULT_RRF_K = 60


@dataclass(frozen=True, slots=True)
class Weights:
    """The ranking's three weights (``memory.episodic.weight_similarity|recency|importance``)."""

    similarity: float = 0.6
    recency: float = 0.2
    importance: float = 0.2

    def __post_init__(self) -> None:
        if min(self.similarity, self.recency, self.importance) < 0:
            raise ValueError("a ranking weight is not negative")


@dataclass(frozen=True, slots=True)
class Candidate:
    """What ranking needs of one memory: its id, its position (``turn``), its importance and
    the turn the importance holds from."""

    memory_id: str
    turn: int | None
    importance: float | None
    importance_at: int | None


@dataclass(frozen=True, slots=True)
class Ranked:
    """One ranked memory: its score and the three terms it came from."""

    memory_id: str
    score: float
    similarity: float
    recency: float
    importance: float


def fuse(rankings: Sequence[Sequence[str]], k: int = DEFAULT_RRF_K) -> dict[str, float]:
    """Reciprocal-rank fusion of the arms' lists (see the module docstring). An empty list is an
    arm that ran and found nothing: it still counts in ``arms``."""
    if k < 1:
        raise ValueError("the fusion constant k is a positive integer")
    arms = len(rankings)
    if arms == 0:
        return {}
    total: dict[str, float] = {}
    for ranking in rankings:
        for rank, memory_id in enumerate(dict.fromkeys(ranking), start=1):
            total[memory_id] = total.get(memory_id, 0.0) + 1.0 / (k + rank)
    scale = (k + 1) / arms
    return {memory_id: value * scale for memory_id, value in total.items()}


def recency(turn: int | None, now: int, half_life: float) -> float:
    """``0.5 ^ ((now - turn) / half_life)`` (see the module docstring)."""
    if half_life <= 0:
        raise ValueError("a recency half-life is a positive number of turns")
    if turn is None:
        return 0.0
    age = max(0, now - turn)
    value: float = 0.5 ** (age / half_life)
    return value


def score(similarity: float, recent: float, importance: float, weights: Weights) -> float:
    """``w_sim · similarity + w_rec · recency + w_imp · importance``."""
    return (
        weights.similarity * similarity + weights.recency * recent + weights.importance * importance
    )


def rank(
    candidates: Iterable[Candidate],
    similarity: Mapping[str, float],
    *,
    now: int,
    weights: Weights,
    recency_half_life: float,
    importance_half_life: float,
) -> list[Ranked]:
    """Every candidate scored (its fused ``similarity``, 0.0 when no arm found it), best first,
    ties by memory id."""
    out: list[Ranked] = []
    for c in dict((c.memory_id, c) for c in candidates).values():
        sim = float(similarity.get(c.memory_id, 0.0))
        rec = recency(c.turn, now, recency_half_life)
        imp = decayed_importance(c.importance, c.importance_at, now, importance_half_life)
        out.append(Ranked(c.memory_id, score(sim, rec, imp, weights), sim, rec, imp))
    out.sort(key=lambda r: (-r.score, r.memory_id))
    return out


__all__ = ["DEFAULT_RRF_K", "Candidate", "Ranked", "Weights", "fuse", "rank", "recency", "score"]
