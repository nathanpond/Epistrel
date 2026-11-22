"""Owner targets on informational rows (#528, the owner's decision of 2026-11-16: "reported and
checked", not a certification gate).

A report-only metric may carry a ``target``: the report shows ``meets target`` or ``misses
target`` beside its value, the embedded summary and the certification record carry it, and the
INDEX's Targets cell names it. It is never a ``Verdict``: it does not enter a requirement's
verdict, the suite's or the run's. The zero-leak suites' model-error rows are
``*.render_error_rate`` with a 1 % target, counted over the withheld cases, with a guardrail
refusal left out.

No test here carries a part marker: this is the harness's reporting rule (docs/04 §8), like
``test_model_error_scoping.py``.
"""

from __future__ import annotations

import pytest

from epistrel.core.errors import GuardrailViolation
from epistrel.core.profiles.certification import parse_record
from epistrel.evals.certify import record_from
from epistrel.evals.gate import (
    Direction,
    Judgement,
    Metric,
    Rule,
    TargetStatus,
    Verdict,
    metric_problems,
    target_status,
)
from epistrel.evals.index import target_cell
from epistrel.evals.report import (
    Host,
    MetricResult,
    Report,
    SuiteResult,
    render_markdown,
    requirement_verdicts,
    summary,
)
from epistrel.evals.runner import DEFAULT_SETS_DIR, model_error_outcome
from epistrel.evals.suites import SUITES
from epistrel.evals.suites import priv_think_leak as think_leak
from epistrel.evals.suites import q04_asymmetry as q04
from epistrel.orchestrator.models.errors import ModelUnavailable

ROW = Metric(
    "q04.render_error_rate",
    Rule.informational,
    direction=Direction.at_most,
    description="a withheld case ended in a model error",
    target=0.01,
)
LEAK = Metric("nfr_qual_1.asymmetry", Rule.zero, 0.0, Direction.at_most, "a disclosure")


def _row(metric: Metric, mean: float, verdict: Verdict = Verdict.report_only) -> MetricResult:
    judgement = Judgement(verdict, mean, 0.001, 400, 3, (mean,) * 3, None, None, 1200)
    return MetricResult.of(metric, judgement)


def _report(*rows: MetricResult) -> Report:
    report = Report(
        run_id="full-abc-2026-11-22",
        started_at="2026-11-22T00:00:00+00:00",
        model="m",
        fingerprint="sha256:" + "a" * 64,
        fingerprint_profile="Full v1",
        preset="full",
        adapter="openai",
        engine_sha="abc",
        host=Host(kind="hosted", hardware="Anthropic API", server_version="x"),
        seed_base=1,
        seeds=[1, 2, 3],
    )
    report.merge(
        SuiteResult(
            name="q04",
            title="Q-04",
            ci_gate=False,
            cases=400,
            seeds=[1, 2, 3],
            metrics=list(rows),
            verdict=Verdict.passed,
        )
    )
    return report


@pytest.mark.parametrize(
    ("mean", "status", "shown"),
    [
        (0.015, TargetStatus.misses, "report only; **misses target** ≤ 1.00%"),
        (0.005, TargetStatus.meets, "report only; meets target ≤ 1.00%"),
        (0.01, TargetStatus.meets, "report only; meets target ≤ 1.00%"),  # on it meets it
    ],
)
def test_the_row_shows_how_its_mean_compares_with_the_target(
    mean: float, status: TargetStatus, shown: str
) -> None:
    row = _row(ROW, mean)
    assert (row.target, row.target_status) == (0.01, status)
    assert row.verdict is Verdict.report_only
    markdown = render_markdown(_report(_row(LEAK, 0.0, Verdict.passed), row))
    (line,) = [ln for ln in markdown.splitlines() if ln.startswith("| `q04.render_error_rate`")]
    assert shown in line
    (entry,) = [m for m in summary(_report(row))["metrics"] if m["id"] == ROW.id]
    assert (entry["target"], entry["target_status"]) == (0.01, status.value)


def test_a_missed_target_never_fails_a_requirement_or_the_run() -> None:
    """Complement: a 50 % rate misses the target, and the run still passes."""
    missed = _row(ROW, 0.5)
    assert missed.target_status is TargetStatus.misses
    report = _report(_row(LEAK, 0.0, Verdict.passed), missed)
    assert report.verdict is Verdict.passed
    assert requirement_verdicts(report) == {"NFR-QUAL-1": "pass"}
    assert "q04.render_error_rate" not in summary(report)["requirements"]


def test_a_row_without_a_target_is_unchanged() -> None:
    plain = _row(Metric("q04.control_detected", Rule.informational), 0.1)
    assert (plain.target, plain.target_status) == (None, None)
    (entry,) = summary(_report(plain))["metrics"]
    assert "target" not in entry
    assert target_status(Direction.at_least, 0.9, None) is TargetStatus.no_data


def test_a_target_is_a_fraction_on_an_informational_metric_only() -> None:
    assert metric_problems(ROW) == []
    floored = Metric("nfr_qual_1.asymmetry", Rule.zero, 0.0, Direction.at_most, target=0.01)
    assert metric_problems(floored)
    assert metric_problems(Metric("q04.x", Rule.informational, target=1.5))


def test_the_record_and_the_index_carry_the_target_rows() -> None:
    report = _report(_row(LEAK, 0.0, Verdict.passed), _row(ROW, 0.015))
    record = record_from(
        report,
        render_markdown(report),
        fingerprint=report.fingerprint,
        profile_id=None,
        profile_version=None,
        models={"generation": {"id": "m", "reported_version": None}},
        preset="full",
    )
    assert record is not None
    assert record.schema_version == 4
    (target,) = record.targets
    assert (target.metric_id, target.target, target.status) == (ROW.id, 0.01, "misses target")
    every_floor = (*record.floors_passed, *record.floors_failed, *record.floors_insufficient)
    assert all("render_error_rate" not in f.metric_id for f in every_floor)
    reread = parse_record(record.model_dump(mode="json"))
    assert reread.targets == record.targets
    assert target_cell(reread) == "**`q04.render_error_rate` misses target**"
    met = record.model_copy(
        update={"targets": [target.model_copy(update={"status": "meets target"})]}
    )
    assert target_cell(met) == "`q04.render_error_rate` meets target"
    assert target_cell(record.model_copy(update={"targets": []})) == "—"


@pytest.mark.parametrize("module", [q04, think_leak])
def test_the_zero_leak_rows_count_withheld_model_errors_only(module: object) -> None:
    """The rows' denominator (#528's planner): withheld cases, a control and a guardrail
    refusal left out. The id is renamed with its 1 % target; it stays the model-error row."""
    suite = SUITES["q04" if module is q04 else "priv_think_leak"]
    row = q04.MODEL_ERROR if module is q04 else think_leak.MODEL_ERROR
    assert row.endswith(".render_error_rate")
    assert suite.model_error_metric == row
    (metric,) = [m for m in suite.metrics if m.id == row]
    assert (metric.rule, metric.direction, metric.target) == (
        Rule.informational,
        Direction.at_most,
        0.01,
    )
    cases = suite.load(DEFAULT_SETS_DIR)
    withheld = next(c for c in cases if not getattr(c, "control", False))
    control = next(c for c in cases if getattr(c, "control", False))
    error = ModelUnavailable("generation", "invalid_structure")
    assert model_error_outcome(suite, error, withheld).observations[row] is False
    assert model_error_outcome(suite, error, control).observations[row] is None
    assert suite.model_error_counted(withheld)
    assert not suite.model_error_counted(control)


def test_a_guardrail_refusal_is_not_a_render_error() -> None:
    suite = SUITES["q04"]
    withheld = next(c for c in suite.load(DEFAULT_SETS_DIR) if not getattr(c, "control", False))
    refusal = GuardrailViolation("refused", {"policy_id": "p"})
    outcome = model_error_outcome(suite, refusal, withheld)
    assert outcome.observations[q04.MODEL_ERROR] is None
    assert outcome.detail["model_error"] == refusal.code
