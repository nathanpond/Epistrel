"""E-13: text-quality signals (docs/04 §8; NFR-QUAL-15, FR-FB-3), its M4 part (#158).

``test_e_13`` plays the ``e13`` suite over every intervention scenario on the stub, two seeds.
Each row, per configuration fingerprint, is ``report_only`` (no gate). The model-free signals
cover the messages they are defined on: repetition from a story's third message,
repeated openings from its second, and length accuracy on the scenarios that request a length.
The judge's scores are the scripted ones, as 0-1, with their intervals. The suite states that
its single-message scores are uncalibrated. The feedback correlation reads
``Engine.feedback_aggregates`` and, with fewer than five rated fingerprints, is
``insufficient_data``.

The model-free signals and the correlation are also checked without a model: a repeated
message and opening count, and a varied one does not. Length bands are ±25 %. Spearman runs over
five fingerprints and is undefined below that.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.quality import signals
from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals.suites import Counts, Outcome
from epistrel.evals.suites.e12_interventions import COMPLETED as E12_COMPLETED
from epistrel.evals.suites.e13_signals import (
    COMPLETED,
    CORRELATION,
    MIN_FINGERPRINTS,
    feedback_correlation,
    metric_id,
    unit,
)
from epistrel.evals.suites.scenario_play import CONFIGS, ScenarioCase
from tests.eval.suite_support import SEEDS, SETS, by_id
from tests.evals.support import stub_engine


def words(n: int) -> str:
    return " ".join(["word"] * n)


def test_e_13__signals() -> None:
    """The model-free signals and the correlation, without a model."""
    first = "Bobby leans on the door and grins at you, boss."
    again = "Bobby leans on the door and grins at you, boss!"
    other = "The rain keeps falling while the kettle starts to sing."
    assert signals.repetition(again, [first]) is None  # a story's second message: undefined
    assert signals.repetition(again, [other, first]) is True
    assert signals.repetition(other, [first, again]) is False
    assert signals.overlap(again, [other, first]) == 1.0  # the largest of the three
    assert signals.overlap(other, [first, again]) == 0.0
    assert signals.repeated_opening(again, [first]) is True
    assert signals.repeated_opening(other, [first]) is False
    assert signals.repeated_opening(first, []) is None
    assert signals.repeated_opening("Too short.", [first]) is None
    assert signals.band("short") == (30, 100)
    assert signals.within_length(words(30), "short") is True
    assert signals.within_length(words(100), "short") is True
    assert signals.within_length(words(29), "short") is False
    assert signals.within_length(words(101), "short") is False
    assert signals.within_length(words(50), None) is None
    assert signals.within_length(words(50), 120) is None  # a token cap has no word band
    assert unit(1) == 0.0
    assert unit(5) == 1.0

    judged = {f"fp{i}": float(i) for i in range(MIN_FINGERPRINTS)}
    ratings = {f"fp{i}": float(i) + 0.5 for i in range(MIN_FINGERPRINTS)}
    assert feedback_correlation(judged, ratings).value == pytest.approx(1.0)
    reversed_ratings = {k: -v for k, v in ratings.items()}
    assert feedback_correlation(judged, reversed_ratings).value == pytest.approx(-1.0)
    few = feedback_correlation(judged, {"fp0": 1.0, "fp1": 2.0, "other": 3.0})
    assert (few.value, few.n) == (None, 2)


def test_a_model_error_case_is_counted_never_crashed() -> None:
    """A case a model error failed carries no configuration (the runner's failed-case outcome):
    E-12 and E-13 count it as not completed and aggregate the rest. Regression: the first live
    run crashed on it (``KeyError: 'e12.None.changed'``)."""
    failed = Outcome({}, detail={"model_error": "model_unavailable"})
    played = Outcome({}, detail={"config": "structured", "turns": 4, "changed": 1, "pairs": []})
    e12 = SUITES["e12"].aggregate([failed, played])
    assert e12[E12_COMPLETED] == Counts(1, 1)
    assert e12["e12.structured.changed"] == Counts(1, 3)
    e13 = SUITES["e13"].aggregate([failed])
    assert e13[COMPLETED] == Counts(0, 1)


@pytest.mark.integration
@pytest.mark.part("M4", "NFR-QUAL-15; FR-FB-3")
async def test_e_13(pg_url: str, engine: AsyncEngine) -> None:
    suite = SUITES["e13"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ScenarioCase)]
    async with stub_engine(pg_url, engine) as lib:
        report = await run(suite, AdapterConfig(), SEEDS, engine=lib, sets_dir=SETS)
    assert report.errors == [], report.errors
    (result,) = report.suites
    assert result.errors == [], result.errors[:5]
    assert result.verdict is Verdict.report_only
    assert all(m.verdict is Verdict.report_only for m in result.metrics)
    assert result.calibration is not None
    assert result.calibration.status == "uncalibrated"
    rows = by_id(result)
    for config in CONFIGS:
        mine = [c for c in cases if c.config == config]
        repeated = rows[metric_id(config, "repetition")]
        assert repeated.sample_size == sum(len(c.turns) - 2 for c in mine)
        assert not repeated.judge_based
        opening = rows[metric_id(config, "repeated_opening")]
        assert 0 < opening.sample_size <= sum(len(c.turns) - 1 for c in mine)
        length = rows[metric_id(config, "length_accuracy")]
        assert length.sample_size == sum(len(c.turns) for c in mine if c.length)
        voice = rows[metric_id(config, "voice_consistency")]
        scripted = [unit(t.stub_voice) for c in mine for t in c.turns]
        assert voice.judge_based
        assert voice.mean == pytest.approx(sum(scripted) / len(scripted))
        assert voice.ci95 is not None
        driver = rows[metric_id(config, "driver_adherence")]
        assert driver.mean == pytest.approx(sum(unit(c.stub_driver) for c in mine) / len(mine))
    correlation = rows[CORRELATION]
    assert correlation.mean is None
    assert any("insufficient data" in n for n in result.notes)
    assert any(n.startswith("E-13 fingerprint `structured`") for n in result.notes)
