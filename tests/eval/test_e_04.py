"""E-04: importance calibration (docs/04 §8; FR-MEM-7; #157). The ``e04`` suite scores the
labeled pivotal/trivial set (``evals/sets/importance``) through the importance job's own call
(``jobs.handlers.importance.score``) on the stub, scripted per case: pivotal out-ranks trivial,
Spearman ≥ 0.6 (report-only: importance is ``unmeasured`` in the guarantee table)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.e04_importance import ABOVE_MEDIAN, SCORED, SPEARMAN, ImportanceCase
from tests.eval.suite_support import SETS, by_id, flipped, frozen, stub_run

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "FR-MEM-7")
async def test_e_04(pg_url: str, engine: AsyncEngine) -> None:
    """200 labeled memories, 100 pivotal and 100 trivial (#135's ten among them): every one is
    scored, every pivotal one above 0.5 and every trivial one at or below, and the rank
    correlation of score with label is at least 0.6 on both seeds; every row is report-only.

    Complement asserted: relabeling one pivotal memory trivial lowers the correlation and makes
    the per-case calibration miss it."""
    suite = SUITES["e04"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ImportanceCase)]
    assert len(cases) >= 200
    assert {c.expected for c in cases} == {"pivotal", "trivial"}
    assert {c.id for c in cases} >= {"importance-death-0001", "importance-routine-0001"}
    result = await stub_run(pg_url, engine, suite)
    assert result.verdict is Verdict.report_only
    metrics = by_id(result)
    assert metrics[SCORED].mean == 1.0
    assert metrics[ABOVE_MEDIAN].mean == 1.0
    assert metrics[SPEARMAN].sample_size == len(cases)
    assert all(v >= 0.6 for v in metrics[SPEARMAN].per_seed)

    target = next(c for c in cases if c.expected == "pivotal")
    wrong = flipped(cases, target.id, expected="trivial")
    complement = await stub_run(pg_url, engine, frozen(suite, cases), cases=wrong)
    found = by_id(complement)
    assert found[ABOVE_MEDIAN].mean == pytest.approx(1 - 1 / len(cases))
    assert all(
        v < s for v, s in zip(found[SPEARMAN].per_seed, metrics[SPEARMAN].per_seed, strict=True)
    )
