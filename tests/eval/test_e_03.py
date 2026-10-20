"""E-03: consolidation faithfulness (docs/04 §8; FR-MEM-3,6, NFR-QUAL-3; #157). The ``e03``
suite runs source windows (``evals/sets/consolidation``) through #143's consolidation job on
scratch stories, on the stub: every reflection written is judged entailed by its window's
episodes, and the judge agrees with both labeled reflections."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, CaseModel, Verdict
from epistrel.evals.suites.e03_consolidation import (
    FAITHFULNESS,
    JUDGE_AGREEMENT,
    PRODUCED,
    ConsolidationCase,
    E03Suite,
)
from epistrel.orchestrator.models.registry import CONSOLIDATION
from epistrel.orchestrator.models.stub import StubAdapter
from tests.eval.suite_support import SETS, assert_floors_pass, by_id, strict_verdict, stub_run

pytestmark = pytest.mark.integration
SAMPLE = 30


class Hallucinating(E03Suite):
    """E-03 whose stub consolidation writes the window's non-entailed reflection."""

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        super().script_stub(case, stub)
        assert isinstance(case, ConsolidationCase)
        reflection = {
            "text": case.expected.non_entailed,
            "modality": "observation",
            "attributed_to": None,
            "supports": case.expected.supports,
        }
        stub.case(
            case.id,
            json.dumps({"facts": [], "reflections": [reflection]}),
            purpose=CONSOLIDATION.name,
        )


@pytest.mark.part("M4", "FR-MEM-3,6; NFR-QUAL-3")
async def test_e_03(pg_url: str, engine: AsyncEngine) -> None:
    """At least 150 labeled windows; on a seeded sample, each window's consolidation writes its
    reflection, the judge finds it entailed by the window's episodes (faithfulness 100 % on both
    seeds, the floor passes), and the judge calls the labeled entailed reflection entailed and
    the non-entailed one not.

    Complement asserted: when the consolidation writes the non-entailed reflection instead,
    faithfulness fails the strict rule."""
    suite = SUITES["e03"]
    assert len(suite.load(SETS)) >= 150
    result = await stub_run(pg_url, engine, suite, sample=SAMPLE)
    assert_floors_pass(result)
    metrics = by_id(result)
    for metric in (FAITHFULNESS, PRODUCED, JUDGE_AGREEMENT):
        assert (metrics[metric].sample_size, metrics[metric].mean) == (SAMPLE, 1.0), metric

    hallucinating = Hallucinating()
    complement = await stub_run(pg_url, engine, hallucinating, sample=10)
    assert by_id(complement)[FAITHFULNESS].mean == 0.0
    assert strict_verdict(complement, FAITHFULNESS) is Verdict.failed
