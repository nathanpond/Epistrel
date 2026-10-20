"""Shared steps of #157's suite tests: run one registered suite against the stub, two seeds,
through #134's harness (scratch stories on the session database), and re-judge its floored
metrics under the stub-mode strict rule (``gate.strict``: every seed perfect).

The **flipped-label complement** runs a copy of the suite whose stub is scripted from the
original cases (``frozen``) over cases with one label changed: the engine and the stub answer as
they would for the real case, so the wrong label shows as a miss, and the strict rule fails the
floor it bites."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, CaseModel, Suite, run
from epistrel.evals.gate import Rule, Verdict, strict
from epistrel.evals.report import MetricResult, SuiteResult
from epistrel.orchestrator.models.stub import StubAdapter
from tests.evals.support import stub_engine

SETS = Path(__file__).resolve().parents[2] / "evals" / "sets"
SEEDS = [20261019, 20261020]


async def stub_run(
    pg_url: str,
    engine: AsyncEngine,
    suite: Suite,
    *,
    cases: Sequence[CaseModel] | None = None,
    sample: int | None = None,
) -> SuiteResult:
    """``suite`` on the stub, two seeds (its committed set, or ``cases``)."""
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            sample=sample,
            cases={suite.name: list(cases)} if cases is not None else None,
        )
    assert report.errors == [], report.errors
    (result,) = report.suites
    assert result.errors == [], result.errors[:5]
    return result


def by_id(result: SuiteResult) -> dict[str, MetricResult]:
    return {m.id: m for m in result.metrics}


def strict_verdict(result: SuiteResult, metric_id: str) -> Verdict:
    """``metric_id``'s verdict under the stub-mode strict rule."""
    metric = SUITES[result.name].metrics
    declared = next(m for m in metric if m.id == metric_id)
    return strict(declared, by_id(result)[metric_id].per_seed)


def assert_floors_pass(result: SuiteResult, *, strictly: bool = True) -> None:
    """Every floored metric passes its floor (and, by default, the strict rule), with two
    seeds and a sample size; the suite passes."""
    assert result.verdict is Verdict.passed, result
    for metric in result.metrics:
        assert metric.seed_count == len(SEEDS)
        if metric.rule is Rule.informational:
            continue
        assert metric.verdict is Verdict.passed, metric
        assert metric.sample_size > 0
        if strictly:
            assert strict_verdict(result, metric.id) is Verdict.passed, metric


def frozen(suite: Suite, originals: Sequence[CaseModel]) -> Suite:
    """A copy of ``suite`` whose stub is scripted from ``originals`` by case id."""
    by_case = {c.id: c for c in originals}
    base = type(suite)

    class Frozen(base):  # type: ignore[misc, valid-type]
        def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
            super().script_stub(by_case[case.id], stub)

    Frozen.name = suite.name
    return Frozen()


def flipped(cases: Sequence[CaseModel], case_id: str, **changes: object) -> list[CaseModel]:
    """``cases`` with ``case_id``'s fields changed (``expected=…``; ``inputs`` as a dict)."""
    found = []
    for case in cases:
        if case.id == case_id:
            document = case.model_dump(mode="json", by_alias=True)
            document.update(changes)
            case = type(case).model_validate(document)
        found.append(case)
    return found
