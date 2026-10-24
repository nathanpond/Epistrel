"""E-01: entailment accuracy, its M4, M5 and M9 parts (docs/04 §8; FR-VERIFY-1,2,3,5,
NFR-QUAL-2; #157, #179; NFR-QUAL-2 gates from M9 per ADR-164, and these stub tests run in CI
either way). The ``e01`` suite runs its labeled set (``evals/sets/entailment``) through #142's
``Verifier`` on the stub, scripted per case: both NFR-QUAL-2 rates are clear of their floors and
the verdict accuracy is reported, the escalated residual apart. ``test_e_01__viewpoint`` (M5)
runs ``e01_viewpoint`` (``evals/sets/entailment_viewpoint``): a speaker's own claims checked
against canon and its belief partition, where a false contradiction would "correct" a lie or a
false belief."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, Verdict
from epistrel.evals.suites import e01_viewpoint as viewpoint
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


@pytest.mark.part("M4", "FR-VERIFY-1,3")
@pytest.mark.part("M9", "NFR-QUAL-2")
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


@pytest.mark.part("M5", "FR-VERIFY-2,5")
@pytest.mark.part("M9", "NFR-QUAL-2")
async def test_e_01__viewpoint(pg_url: str, engine: AsyncEngine) -> None:
    """At least 300 labeled (claim, canon, viewpoint) cases, one third each lies, false beliefs
    and belief-consistent claims (#166's AC5): on the stub, two seeds, no claim is repaired (the
    false-contradiction rate is 0), none canon does not support ends supported, and every
    verdict equals its label (classification, belief relation and objective verdict), so both
    NFR-QUAL-2 floors pass under the strict rule; no model call is made.

    Complement asserted: the same claims checked without the speaker's beliefs (a story with
    shared character knowledge) repair every lie and false belief canon contradicts as a
    contradiction, which is what the viewpoint dimension prevents."""
    suite = SUITES["e01_viewpoint"]
    cases = [c for c in suite.load(SETS) if isinstance(c, viewpoint.ViewpointCase)]
    assert len(cases) >= 300
    families = {f: sum(c.family == f for c in cases) for f in ("lie", "false_belief", "consistent")}
    assert set(families.values()) == {len(cases) // 3}
    result = await stub_run(pg_url, engine, suite)
    assert_floors_pass(result)
    metrics = by_id(result)
    assert (
        metrics[viewpoint.FALSE_CONTRADICTION].sample_size,
        metrics[viewpoint.FALSE_CONTRADICTION].mean,
    ) == (len(cases), 0.0)
    unsupported = sum(c.expected.objective != "supported" for c in cases)
    assert metrics[viewpoint.FALSE_SUPPORT].sample_size == unsupported
    assert (metrics[viewpoint.ACCURACY].sample_size, metrics[viewpoint.ACCURACY].mean) == (
        len(cases),
        1.0,
    )
    assert metrics[viewpoint.CLASSIFICATION_ACCURACY].mean == 1.0

    contradicted = [
        c for c in cases if c.expected.objective == "contradicted" and c.family != "consistent"
    ]
    assert contradicted
    for case in contradicted:
        with_beliefs = await viewpoint.check_claim(case, None)
        shared = await viewpoint.check_claim(case, None, partitions=False)
        assert (with_beliefs.needs_repair, shared.needs_repair) == (False, True), case.id
        assert shared.speech is None
