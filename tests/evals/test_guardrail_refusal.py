"""#738: the model's guardrail refusing a suite's content is a verdict, not a harness error. A
refused case fails like a model error; a refused setup fails its suite with no cases. Neither
lands in ``Report.harness_errors``, which stops a certify run."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.errors import GuardrailViolation
from epistrel.engine import Engine
from epistrel.evals import AdapterConfig, CaseModel, Outcome, Report, Verdict, run, seeds_from
from epistrel.evals.report import render_markdown
from epistrel.evals.runner import CaseContext
from tests.evals.support import ZERO_METRIC, SyntheticZeroSuite, stub_engine, synthetic_cases

SEEDS = seeds_from(2)
REFUSED_CASE = "syn-0002"
MINORS = "Input violates 1 policy: a minor is involved or present."


class RefusingCaseSuite(SyntheticZeroSuite):
    """One case's content is refused by the guardrail; the others play."""

    name = "refusing_case"

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        if case.id == REFUSED_CASE:
            raise GuardrailViolation(MINORS, {"violations": []})
        return await super().evaluate(ctx, case)


class RefusingSetupSuite(SyntheticZeroSuite):
    """The guardrail refuses the suite's fixture."""

    name = "refusing_setup"
    torn_down = False

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        raise GuardrailViolation(MINORS, {"violations": []})

    async def teardown(self, engine: Engine, ctx: RequestContext) -> None:
        self.torn_down = True


class BrokenSetupSuite(SyntheticZeroSuite):
    """Any other exception in setup: still a harness error."""

    name = "broken_setup"

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        raise RuntimeError("fixture bug")


async def _run(pg_url: str, engine: AsyncEngine, suite: SyntheticZeroSuite) -> Report:
    async with stub_engine(pg_url, engine) as lib:
        return await run(
            suite, AdapterConfig(), SEEDS, engine=lib, cases={suite.name: synthetic_cases(3)}
        )


@pytest.mark.integration
async def test_a_refused_case_fails_the_case_not_the_harness(
    pg_url: str, engine: AsyncEngine
) -> None:
    """The refused case is a failed observation on every seed; the other cases still count."""
    report = await _run(pg_url, engine, RefusingCaseSuite())
    assert report.harness_errors == []
    (suite,) = report.suites
    assert suite.verdict is not Verdict.error
    (metric,) = suite.metrics
    assert metric.id == ZERO_METRIC
    assert metric.violations == len(SEEDS)  # the refused case, once a seed
    assert metric.sample_size == 3


@pytest.mark.integration
async def test_a_refused_setup_fails_the_suite_not_the_harness(
    pg_url: str, engine: AsyncEngine
) -> None:
    """No case plays, the suite fails with the refusal named, and teardown still ran.

    Complement asserted: any other exception in setup is a harness error, as before."""
    refusing = RefusingSetupSuite()
    report = await _run(pg_url, engine, refusing)
    assert report.harness_errors == []
    (suite,) = report.suites
    assert (suite.verdict, suite.cases) == (Verdict.failed, 0)
    assert suite.refusal == f"setup: GuardrailViolation: {MINORS}"
    assert refusing.torn_down
    assert "Refused by the model's guardrail, so no case played" in render_markdown(report)

    broken = await _run(pg_url, engine, BrokenSetupSuite())
    assert broken.harness_errors == ["setup: RuntimeError: fixture bug"]
    assert broken.suites[0].verdict is Verdict.error
