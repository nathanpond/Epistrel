"""E-05: salience detection (docs/04 §8; FR-COMMIT-3, NFR-QUAL-4; #157). The ``e05`` suite runs
its labeled renders (``evals/sets/salience``) through #146's ``detect_salience`` on the stub,
scripted per case: recall and precision clear their floors, with F-beta (false negatives x3)
reported."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.e05_salience import PRECISION, RECALL, SalienceCase, score
from tests.eval.suite_support import (
    SETS,
    assert_floors_pass,
    by_id,
    flipped,
    frozen,
    strict_verdict,
    stub_run,
)

pytestmark = pytest.mark.integration


@pytest.mark.part("M4", "FR-COMMIT-3; NFR-QUAL-4")
async def test_e_05(pg_url: str, engine: AsyncEngine) -> None:
    """300 labeled renders, 180 with a salient span: on the stub every labeled span is found
    and every predicted span is a labeled one (recall and precision 100 % on both seeds; both
    floors pass), and texture-only renders get none.

    Complements asserted: a span is a hit when it shares half of the shorter span on the same
    beat, and not on another beat; a copy of the set that labels one salient render as texture
    only fails the precision floor under the strict rule."""
    suite = SUITES["e05"]
    cases = [c for c in suite.load(SETS) if isinstance(c, SalienceCase)]
    assert len(cases) >= 300
    assert sum(1 for c in cases if c.expected) >= 150
    result = await stub_run(pg_url, engine, suite)
    assert_floors_pass(result)
    metrics = by_id(result)
    assert metrics[RECALL].sample_size == sum(len(c.expected) for c in cases)

    label = next(c for c in cases if c.expected).expected[0]
    assert score([label], [(label.beat, label.start, label.end)]) == (1, 0, 1, 0)
    assert score([label], [(label.beat + 1, label.start, label.end)]) == (0, 1, 0, 1)
    target = next(c for c in cases if c.expected)
    wrong = flipped(cases, target.id, expected=[])
    complement = await stub_run(pg_url, engine, frozen(suite, cases), cases=wrong)
    assert strict_verdict(complement, PRECISION) is Verdict.failed
