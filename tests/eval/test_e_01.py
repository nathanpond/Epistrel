"""E-01: entailment accuracy, M4's part (docs/04 §8; FR-VERIFY-1,3, NFR-QUAL-2; #157). The
``e01`` suite runs its labeled set (``evals/sets/entailment``) through #142's ``Verifier`` on
the stub, scripted per case: both NFR-QUAL-2 rates are clear of their floors and the verdict
accuracy is reported, the escalated residual apart."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites.e01_entailment import (
    ACCURACY,
    FALSE_CONTRADICTION,
    FALSE_SUPPORT,
    RESIDUAL_ACCURACY,
    EntailmentCase,
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


@pytest.mark.part("M4", "FR-VERIFY-1,3; NFR-QUAL-2")
async def test_e_01(pg_url: str, engine: AsyncEngine) -> None:
    """At least 400 labeled claims, about 30 % of them the fuzzy residual: on the stub, the
    false-contradiction rate (over the claims not labeled contradicted) and the false-support
    rate (over the claims not labeled supported) are 0 on both seeds, so both floors pass, and
    every final verdict equals its label (the deterministic rules and the scripted model).

    Complement asserted: a copy of the set with one ``supported`` claim relabeled ``novel``
    (the engine still supports it) fails the false-support floor under the strict rule."""
    suite = SUITES["e01"]
    cases = suite.load(SETS)
    assert len(cases) >= 400
    residual = [c for c in cases if isinstance(c, EntailmentCase) and c.residual]
    assert 0.25 <= len(residual) / len(cases) <= 0.35
    result = await stub_run(pg_url, engine, suite)
    assert_floors_pass(result)
    metrics = by_id(result)
    labels = [c.expected for c in cases if isinstance(c, EntailmentCase)]
    assert metrics[FALSE_CONTRADICTION].sample_size == sum(x != "contradicted" for x in labels)
    assert metrics[FALSE_SUPPORT].sample_size == sum(x != "supported" for x in labels)
    assert (metrics[ACCURACY].sample_size, metrics[ACCURACY].mean) == (len(cases), 1.0)
    assert (metrics[RESIDUAL_ACCURACY].sample_size, metrics[RESIDUAL_ACCURACY].mean) == (
        len(residual),
        1.0,
    )

    target = next(c for c in cases if isinstance(c, EntailmentCase) and c.expected == "supported")
    wrong = flipped(cases, target.id, expected="novel")
    complement = await stub_run(pg_url, engine, frozen(suite, cases), cases=wrong)
    assert strict_verdict(complement, FALSE_SUPPORT) is Verdict.failed
    assert by_id(complement)[FALSE_SUPPORT].mean == pytest.approx(
        1 / (len(labels) - labels.count("supported") + 1)
    )
