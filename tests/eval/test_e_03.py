"""E-03: consolidation faithfulness (docs/04 §8; FR-MEM-3,6, NFR-QUAL-3; #157; NFR-QUAL-3 gates
from M11 per ADR-164, and this stub test runs in CI either way). The ``e03``
suite runs source windows (``evals/sets/consolidation``) through #143's consolidation job on
scratch stories, on the stub: every reflection written is judged entailed by its window's
episodes, and the judge agrees with both labeled reflections. #747 (ADR-204): the judge's
agreement is counted per labeled pair, and faithfulness counts only when it is at least 75%."""

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
from epistrel.orchestrator.models.registry import CONSOLIDATION, ENTAILMENT
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


class Lenient(E03Suite):
    """E-03 whose stub judge calls every labeled non-entailed reflection entailed too: it
    mis-calls one pair of every window, so its per-pair agreement is 50%."""

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        assert isinstance(case, ConsolidationCase)
        answer = json.dumps({"verdict": "supported", "confidence": 0.9})
        needle = (f"X-Eval-Case: {case.id}", f"Claim: {case.expected.non_entailed}")
        stub.when(needle, answer, purpose=ENTAILMENT.name)  # before E-03's own: first match wins
        super().script_stub(case, stub)


@pytest.mark.part("M4", "FR-MEM-3,6")
@pytest.mark.part("M11", "NFR-QUAL-3")
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
    for metric in (FAITHFULNESS, PRODUCED):
        assert (metrics[metric].sample_size, metrics[metric].mean) == (SAMPLE, 1.0), metric
    # #747: two labeled pairs a window.
    assert (metrics[JUDGE_AGREEMENT].sample_size, metrics[JUDGE_AGREEMENT].mean) == (
        2 * SAMPLE,
        1.0,
    )
    assert metrics[FAITHFULNESS].verdict is Verdict.passed
    assert metrics[FAITHFULNESS].note is None

    hallucinating = Hallucinating()
    complement = await stub_run(pg_url, engine, hallucinating, sample=10)
    assert by_id(complement)[FAITHFULNESS].mean == 0.0
    assert strict_verdict(complement, FAITHFULNESS) is Verdict.failed


@pytest.mark.part("M11", "NFR-QUAL-3")
async def test_e_03__untrusted_judge(pg_url: str, engine: AsyncEngine) -> None:
    """#747 (ADR-204): a judge that mis-calls one labeled pair of every window agrees with 50%
    of the pairs, below 75%, so faithfulness is report only (with a note quoting the agreement)
    even though every reflection it judged was entailed, and the suite no longer passes on it.

    Complement asserted: the trusted judge (``test_e_03``) leaves the verdict as judged."""
    result = await stub_run(pg_url, engine, Lenient(), sample=10)
    metrics = by_id(result)
    agreement = metrics[JUDGE_AGREEMENT]
    assert (agreement.sample_size, agreement.mean) == (20, 0.5)
    faithfulness = metrics[FAITHFULNESS]
    assert faithfulness.mean == 1.0
    assert faithfulness.verdict is Verdict.report_only
    assert faithfulness.note is not None
    assert "50.00%" in faithfulness.note
    assert "ADR-204" in faithfulness.note
    assert result.verdict is Verdict.report_only
