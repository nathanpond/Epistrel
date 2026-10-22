"""Q-02: referential and temporal resolution (docs/04 §9; FR-RET-2,3, NFR-QUAL-6; #157). The
``q02`` suite resolves #139's generated queries (``evals/sets/retrieval/resolution.jsonl``)
through the retrieval pipeline's resolve stage on the harness; its rows are report-only."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.q02_resolution import MENTIONS, QUERIES, ResolutionCase
from tests.eval.suite_support import SETS, by_id, flipped, stub_run

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "FR-RET-2,3; NFR-QUAL-6")
async def test_q_02(pg_url: str, engine: AsyncEngine) -> None:
    """120 labeled queries over S-THOSEGUYS's six shapes (ambiguous pronouns and unknown names
    among them, whose right answer is ``uncertain``): every mention resolves as labeled and every
    query is all correct; the rows are report-only (docs/04 sets no floor).

    Complement asserted: one mislabeled mention lowers both accuracies."""
    suite = SUITES["q02"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ResolutionCase)]
    assert len(cases) >= 120
    assert any(e is None for c in cases for _, e in c.expected.mentions)
    result = await stub_run(pg_url, engine, suite)
    assert result.verdict is Verdict.report_only
    metrics = by_id(result)
    assert (metrics[MENTIONS].mean, metrics[QUERIES].mean) == (1.0, 1.0)
    assert metrics[MENTIONS].sample_size == sum(len(c.expected.mentions) for c in cases)

    target = next(c for c in cases if c.expected.mentions[0][1] is not None)
    labels = target.expected.model_dump()
    labels["mentions"] = [[target.expected.mentions[0][0], None], *labels["mentions"][1:]]
    complement = await stub_run(
        pg_url, engine, suite, cases=flipped(cases, target.id, expected=labels)
    )
    found = by_id(complement)
    assert (found[MENTIONS].mean or 0.0) < 1.0
    assert found[QUERIES].mean == pytest.approx(1 - 1 / len(cases))
