"""The C-17 covering array's generator (#243): factors read from the profile schema, every pair
of levels covered, a stable seed, and a bounded size."""

from __future__ import annotations

import pytest

from epistrel.testing import covering


def test_covering__pairs() -> None:
    """Every level pair of every factor pair appears in some candidate; the same seed gives the
    same array; the array plus its repairs stays within ``SIZE_MAX``. The bounded capability
    dials and every enum level come from the schema. Complement: an array missing one of its
    rows loses a pair, and the check names it."""
    found = covering.factors()
    assert found.factors["repair.max_attempts"] == [0, 10]
    assert found.factors["character_knowledge.nested_beliefs.depth"] == [2, 5]
    assert found.factors["verification.level"] == ["off", "declared", "extraction", "entailment"]
    assert "pins.max_pinned" in found.held
    rows = covering.pairwise(found.factors, seed=243)
    assert covering.lost_pairs(rows, found.factors) == set()
    assert covering.pairwise(found.factors, seed=243) == rows
    result = covering.cover(found.factors, seed=243)
    assert len(result.rows) <= covering.SIZE_MAX, len(result.rows)
    assert result.uncovered_valid(found.factors) == []
    # Complement: drop a row whose pairs no other row covers, and the check names the pair.
    for index in range(len(rows)):
        lost = covering.lost_pairs(rows[:index] + rows[index + 1 :], found.factors)
        if lost:
            assert all(pair in covering.pairs_of(rows[index], found.factors) for pair in lost)
            break
    else:
        pytest.fail("no row of the array covers a pair alone")


def test_covering__refusals_name_a_reason() -> None:
    """A refused candidate names a missing dependency or an undelivered level; a refusal with
    neither (an empty or exception rejection) is not counted as a pass (complement)."""
    found = covering.factors()
    result = covering.cover(found.factors, seed=243)
    assert all(c.valid or c.refused_for_a_reason for c in result.checked)
    empty = covering.Checked({}, valid=False)
    assert not empty.refused_for_a_reason
    lacking = covering.check({"memory.consolidation.enabled": True, "pipeline": "lite"})
    assert not lacking.valid
    assert lacking.missing
