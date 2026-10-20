"""E-10: claim-detection recall (docs/04 §8; FR-VERIFY-13, NFR-QUAL-10; #157). The ``e10``
suite runs its adversarial renders and composed messages (``evals/sets/claim_detection``)
through #148's ``ClaimDetector`` in the shared tavern scene, under both ``extraction_mode``s, on
the stub scripted per case: overall recall clears 95 % in each mode, reported per layer and per
category, and ``gated``'s blind spot (a beat no detector flags) shows."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.e10_claim_detection import (
    CATEGORIES,
    DetectionCase,
    category_id,
    layer_id,
    recall_id,
)
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


@pytest.mark.part("M4", "FR-VERIFY-13; NFR-QUAL-10")
async def test_e_10(pg_url: str, engine: AsyncEngine) -> None:
    """300 labeled claims, at least 40 per category per mode: under ``always`` every one is
    detected (the deterministic detectors find possession, quantities, times and protected
    content first; only extraction finds identity claims), and under ``gated`` every one but
    the identity claims in beats that declared something else, which no detector flags; both
    clear 95 %. A composed message counts alike in both modes.

    Complement asserted: a copy of the set that moves one claim's span off its words fails
    the ``always`` recall floor under the strict rule."""
    suite = SUITES["e10"]
    cases = [c for c in suite.load(SETS) if isinstance(c, DetectionCase)]
    assert len(cases) >= 300
    for category in CATEGORIES:
        assert sum(c.expected.category == category for c in cases) >= 40
    result = await stub_run(pg_url, engine, suite)
    assert_floors_pass(result, strictly=False)
    metrics = by_id(result)
    assert metrics[recall_id("always")].mean == 1.0
    assert strict_verdict(result, recall_id("always")) is Verdict.passed
    blind = sum(c.family == "identity-declared" for c in cases)
    assert blind > 0
    assert metrics[recall_id("gated")].mean == pytest.approx(1 - blind / len(cases))
    assert (metrics[category_id("gated", "identity")].mean or 0.0) < 1.0
    assert (metrics[layer_id("always", "detector")].mean or 0.0) > 0.5
    assert (metrics[layer_id("always", "extraction")].mean or 0.0) > 0.0

    target = next(c for c in cases if c.expected.category == "possession")
    label = target.expected.model_dump()
    moved = flipped(cases, target.id, expected={**label, "start": 0, "end": 1, "beat": 9})
    complement = await stub_run(pg_url, engine, frozen(suite, cases), cases=moved)
    assert strict_verdict(complement, recall_id("always")) is Verdict.failed
