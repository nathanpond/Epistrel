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
five fingerprints and is undefined below that. The correlation pools the judge's voice means
of the committed reports' embedded summaries with the run's own, so five rated fingerprints can
be reached across runs (#463).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.quality import signals
from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals.gate import Direction, Rule
from epistrel.evals.metrics import spearman
from epistrel.evals.report import (
    SUMMARY_PREFIX,
    Host,
    MetricResult,
    Report,
    SuiteResult,
    summary,
)
from epistrel.evals.suites import Counts, Outcome, Value
from epistrel.evals.suites.e12_interventions import COMPLETED as E12_COMPLETED
from epistrel.evals.suites.e13_signals import (
    COMPLETED,
    CORRELATION,
    MIN_FINGERPRINTS,
    E13Suite,
    _fingerprint,
    committed_voice_means,
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


def committed_report(directory: Path, name: str, judged: list[dict[str, Any]] | None) -> None:
    """A committed report's Markdown with only its embedded summary; ``None``: a report
    written before #463, whose summary has no ``judged`` list."""
    summary: dict[str, Any] = {"date": "2026-10-20", "fingerprint": "sha256:x", "model": "m"}
    if judged is not None:
        summary["judged"] = judged
    (directory / name).write_text(
        f"# Eval report\n\n{SUMMARY_PREFIX}{json.dumps(summary)} -->\n", encoding="utf-8"
    )


def voice_row(fingerprint: str, mean: float, n: int, config: str = "structured") -> dict[str, Any]:
    return {
        "metric": metric_id(config, "voice_consistency"),
        "fingerprint": fingerprint,
        "mean": mean,
        "n": n,
    }


def played(config: str, voice: list[int]) -> Outcome:
    return Outcome(
        {},
        detail={
            "config": config,
            "repetition": [],
            "repeated_opening": [],
            "length_accuracy": [],
            "voice_consistency": voice,
            "driver_adherence": [3],
        },
    )


@dataclass
class Group:
    key: str | None
    mean_rating: float | None


@dataclass
class RatedEngine:
    """``Engine.feedback_aggregates(group_by=fingerprint)``'s answer, without a database."""

    groups: list[Group]

    async def feedback_aggregates(self, ctx: object, group_by: object) -> RatedEngine:
        return self


def test_committed_voice_means_pool_by_n(tmp_path: Path) -> None:
    """#463: the judge's voice means are read from every committed report's embedded summary
    and pooled per fingerprint, weighted by n.

    Complement asserted: a report written before #463 (no ``judged``), a driver-adherence row,
    a malformed row, the index and the README add nothing."""
    committed_report(tmp_path, "a.md", [voice_row("fp-a", 0.5, 2), voice_row("fp-b", 0.25, 4)])
    committed_report(
        tmp_path,
        "b.md",
        [
            voice_row("fp-a", 1.0, 6, config="gated"),
            {**voice_row("fp-c", 0.9, 3), "metric": metric_id("structured", "driver_adherence")},
            {**voice_row("fp-d", 0.9, 3), "n": 0},
            {**voice_row("fp-e", 0.9, 3), "mean": None},
        ],
    )
    committed_report(tmp_path, "old.md", None)
    committed_report(tmp_path, "INDEX.md", [voice_row("fp-x", 0.1, 1)])
    committed_report(tmp_path, "README.md", [voice_row("fp-x", 0.1, 1)])
    found = committed_voice_means(tmp_path)
    assert found == {"fp-a": (0.5 * 2 + 1.0 * 6, 8), "fp-b": (1.0, 4)}
    assert committed_voice_means(tmp_path / "missing") == {}


async def test_e_13__correlation_pools_committed_reports(tmp_path: Path) -> None:
    """#463: one run judges two fingerprints, so the correlation pools the committed reports'
    judge means with the run's own and, with users' ratings for five fingerprints, reports
    Spearman's rho. The run's own fingerprints and three committed ones are ranked alike by
    the judge and by users, so rho is 1. The note says where the fingerprints came from, and
    E-13's judged rows carry their fingerprint for a later run to pool.

    Complement asserted: the same run with no committed reports has two fingerprints and is
    ``insufficient_data``; a committed mean for one of the run's own fingerprints is pooled
    with the run's scores, not counted twice."""
    model = "m"
    structured, gated = (_fingerprint(model, c) for c in ("structured", "gated"))
    outcomes = [played("structured", [5, 5]), played("gated", [4, 4])]  # 1.0 and 0.75
    ratings = {"fp-1": 1.0, "fp-2": 2.0, "fp-3": 3.0, gated: 4.0, structured: 5.0, "x": 1.0}
    engine = RatedEngine([Group(k, v) for k, v in ratings.items()] + [Group(None, 3.0)])

    alone = E13Suite(reports_dir=tmp_path)
    await alone.setup(engine, RequestContext(principal="t"))  # type: ignore[arg-type]
    assert alone.ratings == ratings
    assert alone.correlation(outcomes, model) == Value(None, 2)
    note = alone.summarize(outcomes, model).notes[-1]
    assert "insufficient data (2 fingerprint(s)" in note
    assert "2 from this run, 0 more from 0 fingerprint(s)" in note

    committed_report(
        tmp_path,
        "older.md",
        [
            voice_row("fp-1", 0.1, 4),
            voice_row("fp-2", 0.2, 4),
            voice_row("fp-3", 0.3, 4),
            voice_row(structured, 0.5, 2),  # pooled with the run's two 1.0s: 0.75
        ],
    )
    suite = E13Suite(reports_dir=tmp_path)
    await suite.setup(engine, RequestContext(principal="t"))  # type: ignore[arg-type]
    pooled = suite.pooled_voice_means(outcomes, model)
    assert pooled[structured] == pytest.approx(0.75)
    assert pooled[gated] == pytest.approx(0.75)
    # The structured mean ties gated's after pooling: average ranks give rho just under 1.
    found = suite.correlation(outcomes, model)
    assert found.n == 5
    assert found.value is not None
    assert found.value == pytest.approx(spearman([0.1, 0.2, 0.3, 0.75, 0.75], [1, 2, 3, 4, 5]))
    assert 0.9 < found.value < 1.0
    summary = suite.summarize(outcomes, model)
    assert summary.notes[-1] == (
        f"E-13 feedback correlation: Spearman {found.value:.2f} over 5 fingerprints "
        "(2 from this run, 3 more from 4 fingerprint(s) judged in the committed reports)."
    )
    for config, key in (("structured", structured), ("gated", gated)):
        for signal in ("voice_consistency", "driver_adherence"):
            assert summary.metrics[metric_id(config, signal)]["fingerprint"] == key


def test_judged_means_in_the_embedded_summary() -> None:
    """#463: a report's embedded summary lists its judge-based rows that name a fingerprint,
    with mean and n, which ``committed_voice_means`` reads back.

    Complement asserted: a row with no fingerprint, a model-free row and a row with no mean
    are left out."""
    rule, direction = Rule.informational, Direction.at_least

    def metric(id_: str, **changes: Any) -> MetricResult:
        values: dict[str, Any] = {
            "id": id_,
            "rule": rule,
            "direction": direction,
            "floor": None,
            "mean": 0.8,
            "stdev": None,
            "sample_size": 8,
            "seed_count": 2,
            "per_seed": [0.8, 0.8],
            "verdict": Verdict.report_only,
            "judge_based": True,
            "fingerprint": "sha256:fp",
        }
        values.update(changes)
        return MetricResult(**values)

    voice = metric_id("structured", "voice_consistency")
    result = SuiteResult(
        name="e13",
        title="E-13",
        ci_gate=False,
        cases=4,
        seeds=[1, 2],
        verdict=Verdict.report_only,
        metrics=[
            metric(voice),
            metric(metric_id("gated", "voice_consistency"), fingerprint=None),
            metric(metric_id("structured", "repetition"), judge_based=False),
            metric(metric_id("structured", "driver_adherence"), mean=None),
        ],
    )
    report = Report(
        run_id="r",
        started_at="2026-10-20T00:00:00+00:00",
        model="m",
        fingerprint="sha256:run",
        fingerprint_profile="Structured (M4)",
        adapter="openai",
        engine_sha="abc",
        host=Host(kind="local"),
        seed_base=1,
        seeds=[1, 2],
        suites=[result],
    )
    assert summary(report)["judged"] == [
        {"metric": voice, "fingerprint": "sha256:fp", "mean": 0.8, "n": 8}
    ]


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
        assert voice.fingerprint == _fingerprint(report.model, config)  # #463
        driver = rows[metric_id(config, "driver_adherence")]
        assert driver.mean == pytest.approx(sum(unit(c.stub_driver) for c in mine) / len(mine))
    correlation = rows[CORRELATION]
    assert correlation.mean is None
    assert any("insufficient data" in n for n in result.notes)
    assert any(n.startswith("E-13 fingerprint `structured`") for n in result.notes)
    # #463: the embedded summary carries the judged rows a later run pools.
    judged = {(j["metric"], j["fingerprint"]) for j in summary(report)["judged"]}
    assert {
        (metric_id(c, "voice_consistency"), _fingerprint(report.model, c)) for c in CONFIGS
    } <= judged
