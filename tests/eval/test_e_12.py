"""E-12: engine interventions and prose, in aggregate (docs/04 §8; NFR-QUAL-8,14), its M4 and M9
parts (#158; NFR-QUAL-14 gates from M9 per ADR-164, and this stub test runs in CI either way).

``test_e_12`` plays the ``e12`` suite over every intervention scenario
(``evals/sets/scenarios``) on the stub, two seeds, after calibrating the stub's judge on the
labeled pairs. Every scripted turn triggers exactly the intervention its scenario names: the
pairs per configuration and type, and the first-draft preference rate the scripted judge gives,
are what the set predicts. Every type stays under 300 non-tied pairs, so each row is
``insufficient_evidence`` (never a pass), with its Wilson interval, and the report lists no type
as covered.

Complements asserted:
- the relative rule: 299 non-tied pairs per seed is ``insufficient_evidence`` even with no draft
  preferred; at 300 a mean + SD at most 55 % passes (exactly 55 % included) and above it fails;
- an uncalibrated judge (the stub answers 30 % of the calibration pairs wrongly: 70 % agreement)
  makes every judge row ``report_only``, and the run with it;
- a run with no stored calibration for its judge model fails before any case.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals.gate import RELATIVE_MIN_PAIRS, Direction, Metric, Rule, SeedTally, judge
from epistrel.evals.judging import CALIBRATION_FLOOR, load_pairs
from epistrel.evals.runner import CalibrationMissing
from epistrel.evals.suites.e12_interventions import FLOOR, changed_id, metric_id
from epistrel.evals.suites.scenario_play import CONFIGS, INTERVENTION_TYPES, ScenarioCase
from tests.eval.suite_support import SEEDS, SETS, by_id
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration


def _expected(cases: list[ScenarioCase]) -> dict[str, tuple[int, int]]:
    """Per row, the set's (non-tied pairs, draft preferred) per seed."""
    found: dict[str, list[int]] = {}
    for case in cases:
        for turn in case.turns:
            for kind in turn.expected:
                tally = found.setdefault(metric_id(case.config, kind), [0, 0])
                if turn.stub_judge != "tie":
                    tally[0] += 1
                    tally[1] += turn.stub_judge == "draft"
    return {k: (n, draft) for k, (n, draft) in found.items()}


def _pairs_flipped(share: float) -> set[str]:
    pairs = load_pairs(SETS)
    return {p.id for i, p in enumerate(pairs) if i % 10 < round(share * 10)}


@pytest.mark.part("M4", "NFR-QUAL-8")
@pytest.mark.part("M9", "NFR-QUAL-14")
async def test_e_12(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    suite = SUITES["e12"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ScenarioCase)]
    families = Counter(c.family for c in cases)
    assert set(families) == {
        "rung-1",
        "rung-2",
        "rung-3",
        "rung-4",
        "polished",
        "fallback",
        "plain",
    }
    calibration = tmp_path / "judge-calibration.json"
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            calibrate=True,
            calibration_file=calibration,
        )
    assert report.errors == [], report.errors
    (result,) = report.suites
    assert result.errors == [], result.errors[:5]
    assert result.calibration is not None
    assert result.calibration.status == "calibrated"
    assert result.calibration.agreement == 1.0
    assert result.calibration.pairs >= 200

    rows = by_id(result)
    expected = _expected(cases)
    for config in CONFIGS:
        for kind in INTERVENTION_TYPES:
            row = rows[metric_id(config, kind)]
            n, draft = expected.get(metric_id(config, kind), (0, 0))
            assert row.sample_size == n, (row.id, row.sample_size, n)
            assert row.seed_count == len(SEEDS)
            assert row.judge_based
            # Under 300 non-tied pairs a type is never passed.
            assert row.verdict is Verdict.insufficient_evidence, row
            if n:
                assert row.mean == pytest.approx(draft / n), row.id
                assert row.ci95 is not None
                assert row.mean is not None
                assert row.ci95[0] <= row.mean <= row.ci95[1]
        intervened = sum(len(t.expected) > 0 for c in cases if c.config == config for t in c.turns)
        turns = sum(len(c.turns) for c in cases if c.config == config)
        assert rows[changed_id(config)].mean == pytest.approx(intervened / turns)
    # Every repair rung and both composition outcomes produced pairs on the stub.
    for kind in INTERVENTION_TYPES[:-1]:
        assert any(rows[metric_id(c, kind)].sample_size for c in CONFIGS), kind
    assert result.verdict is Verdict.insufficient_evidence
    assert any(
        "types the floor covers on this run" in n and n.endswith("none.") for n in result.notes
    )
    assert any(n.startswith("E-12 fingerprint `gated`") for n in result.notes)

    # Complement: the relative rule never passes on fewer than 300 pairs a seed.
    metric = Metric("nfr_qual_14.x.y", Rule.relative, FLOOR, Direction.at_most)
    few = [SeedTally(s, RELATIVE_MIN_PAIRS - 1, 0) for s in SEEDS]
    assert judge(metric, few).verdict is Verdict.insufficient_evidence
    clean = [SeedTally(s, RELATIVE_MIN_PAIRS, 0) for s in SEEDS]
    assert judge(metric, clean).verdict is Verdict.passed
    at_floor = [SeedTally(s, 135, 165) for s in SEEDS]  # 55 % draft preferred, SD 0
    assert judge(metric, at_floor).verdict is Verdict.passed
    above = [SeedTally(SEEDS[0], 140, 160), SeedTally(SEEDS[1], 130, 170)]
    assert judge(metric, above).verdict is Verdict.failed

    # Complement: an uncalibrated judge makes every judge row report-only.
    flipped = _pairs_flipped(0.3)
    async with stub_engine(pg_url, engine) as lib:
        weak = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            calibrate=True,
            calibration_file=calibration,
            calibration_flip=flipped,
            cases={suite.name: cases[:4]},
        )
    (weak_result,) = weak.suites
    assert weak_result.calibration is not None
    assert weak_result.calibration.status == "uncalibrated"
    assert weak_result.calibration.agreement is not None
    assert weak_result.calibration.agreement < CALIBRATION_FLOOR
    assert all(m.verdict is Verdict.report_only for m in weak_result.metrics if m.judge_based)
    assert weak_result.verdict is Verdict.report_only
    assert weak.verdict is Verdict.report_only

    # Complement: no stored calibration for the judge model fails before any case.
    async with stub_engine(pg_url, engine) as lib:
        calls = len(lib.models.stub.calls)
        with pytest.raises(CalibrationMissing):
            await run(
                suite,
                AdapterConfig(),
                SEEDS,
                engine=lib,
                sets_dir=SETS,
                calibration_file=tmp_path / "none.json",
            )
        assert len(lib.models.stub.calls) == calls
