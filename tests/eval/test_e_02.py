"""E-02: escalation yield and shadow tightening (docs/04 §8; NFR-PERF-5, FR-OBS-2,4,
FR-VERIFY-3,8; #155). The ``e02`` suite (``epistrel.evals.suites.e02_escalation``) runs
through #134's harness against the stub, as CI runs it: its scripted set of 312 escalations
(every gate reason, under confident, low-confidence, ``novel`` and unparseable answers) and
its promotion cases, refused below the rule and allowed above it."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals.suites.e02_escalation import (
    E02_SHAPE,
    INSTRUMENTATION,
    NO_SILENT_SUPPORTED,
    PROMOTION_RULE,
    SHADOW_RECORDED,
    YIELD_REPORTED,
    E02Case,
    _complete,
    _not_silent,
    _shadowed,
)
from tests.escalation_support import count_rows
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration

SETS = Path(__file__).resolve().parents[2] / "evals" / "sets"
SEED = [20261019]

GOOD_ROW = {
    "claim_id": "op:0",
    "gate_reason": "world_knowledge",
    "predicate_shape": E02_SHAPE,
    "deterministic_confidence": 0.5,
    "threshold": 1.0,
    "tentative_verdict": "uncertain",
    "final_kind": "uncertain",
    "consequence": "none",
    "pass": "render",
    "at": "2026-10-19T00:00:00Z",
    "called": True,
    "model_verdict": "supported",
    "model_confidence": 0.55,
    "model_uncertain": None,
    "flipped": False,
    "shadow_threshold": 0.25,
    "shadow_decision": False,
    "shadow_agreed": True,
}


@pytest.mark.part("M4", "NFR-PERF-5; FR-OBS-2,4; FR-VERIFY-3,8")
async def test_e_02(pg_url: str, engine: AsyncEngine) -> None:
    """Every observation of every E-02 metric is good on the stub run: each of the >= 300
    escalation records carries its required fields, none ends a silent ``supported``
    (FR-VERIFY-8), each holds its shadow decision with the live gate unchanged, and the yield
    report counts them; promotion is refused exactly below ``gate_promotion_min_cases`` (200)
    or ``gate_promotion_min_agreement`` (0.98) and allowed above. The suite is report-only and
    its test shape is removed afterwards.

    Complement asserted: the set holds both promotion outcomes, and the row checks refuse a
    row with a missing field, a low-confidence ``supported`` that ended supported, and a
    shadow decision that disagrees with the proposal."""
    suite = SUITES["e02"]
    cases = suite.load(SETS)
    escalation_cases = [c for c in cases if isinstance(c, E02Case) and c.kind == "escalation"]
    promotions = [c for c in cases if isinstance(c, E02Case) and c.kind == "promotion"]
    assert len(escalation_cases) * 4 >= 300
    expected = [
        (c.cases or 0) >= 200 and (c.cases or 0) > 0 and (c.agreed or 0) / c.cases >= 0.98
        for c in promotions
        if c.cases is not None
    ]
    assert True in expected
    assert False in expected

    async with stub_engine(pg_url, engine) as lib:
        report = await run(suite, AdapterConfig(), SEED, engine=lib, sets_dir=SETS)
    (result,) = report.suites
    assert result.errors == [], result.errors
    assert result.verdict is Verdict.report_only
    by_id = {m.id: m for m in result.metrics}
    for metric, n in (
        (INSTRUMENTATION, len(escalation_cases)),
        (NO_SILENT_SUPPORTED, len(escalation_cases)),
        (SHADOW_RECORDED, len(escalation_cases)),
        (YIELD_REPORTED, len(escalation_cases)),
        (PROMOTION_RULE, len(promotions)),
    ):
        assert (by_id[metric].sample_size, by_id[metric].mean) == (n, 1.0), metric
    # The test shape is gone, and the scratch stories with their rows.
    assert (
        await count_rows(
            engine, "SELECT count(*) FROM gate_thresholds WHERE shape LIKE 'eval|e02%'"
        )
        == 0
    )
    assert (
        await count_rows(
            engine, "SELECT count(*) FROM escalations WHERE predicate_shape LIKE 'eval|e02%'"
        )
        == 0
    )

    # Complement: the row checks bite.
    assert _complete(GOOD_ROW)
    assert _not_silent(GOOD_ROW, 0.6)
    assert _shadowed(GOOD_ROW)
    assert not _complete({**GOOD_ROW, "claim_id": None})
    assert not _complete({**GOOD_ROW, "flipped": None})
    assert not _not_silent({**GOOD_ROW, "final_kind": "supported"}, 0.6)
    assert not _shadowed({**GOOD_ROW, "shadow_decision": True})
    assert not _shadowed({**GOOD_ROW, "shadow_agreed": None})
