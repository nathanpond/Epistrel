"""E-08 M5 (docs/04 §8; NFR-QUAL-1,8; #179): NFR-QUAL-8's gate rules applied to NFR-QUAL-1's
knowledge-asymmetry and private-thought metrics on their labeled sets, through the real suites
(``q04``, ``priv_think_leak``) on the stub, not the synthetic one (#134's M4 part,
``tests/evals/test_harness.py::test_e_08__m4_gate_rules``).

The runs use the stub at four cases at once (each case is one or two structured turns): the
private-thought suite's full set on two seeds (its 300 measured cases: the clean pass), the
asymmetry suite on 299 of its cases (one seed: the short run) and on 300 with E-08's injected
violation (one seed: the violation), and E-01 M5's viewpoint suite beside them (the rate rule).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Rule, Verdict, run, upper_bound_95
from epistrel.evals.report import MetricResult, Report, render_markdown
from epistrel.evals.suites.priv_think_leak import THINK_LEAK, ThinkLeakCase
from epistrel.evals.suites.q04_asymmetry import ASYMMETRY, INJECTED_CASE, AsymmetryCase, Q04Suite
from tests.eval.suite_support import SEEDS, SETS
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration

CONCURRENCY = 4


def _metric(report: Report, metric_id: str) -> MetricResult:
    (suite,) = report.suites
    return next(m for m in suite.metrics if m.id == metric_id)


def _reported(report: Report, metric: MetricResult, n: int, seeds: int) -> None:
    """Every report carries its sample size, seed count, model and version (and the zero rule's
    bound when clean)."""
    assert report.harness_errors == []
    assert (report.model, report.model_version, report.seed_count) == ("stub", "stub", seeds)
    assert (metric.sample_size, metric.seed_count, metric.rule) == (n, seeds, Rule.zero)


@pytest.mark.part("M5", "NFR-QUAL-1,8")
async def test_e_08__m5_real_suites(pg_url: str, engine: AsyncEngine) -> None:
    """On the real zero-leak suites: the private-thought suite's 300 measured cases, clean on
    both seeds, pass, reported "0 observed in 300 cases" with a one-sided 95% upper bound of
    about 1% (never as "leak-free"); 299 clean asymmetry cases fail (fewer than 300); 300
    asymmetry cases with one deterministically injected violation (the scripted stub leaks on
    ``asymmetry-family-0007`` on one seed, a paraphrase the pipeline cannot catch) fail, with
    the violation counted and no bound. The viewpoint suite passes its rate floors beside them.

    Complement asserted: the 299 and the 300 runs differ only in the one case and the leak, and
    the clean think-leak run's controls are all detected (the oracle is not blind)."""
    think = SUITES["priv_think_leak"]
    async with stub_engine(pg_url, engine) as lib:
        clean = await run(think, AdapterConfig(), SEEDS, engine=lib, concurrency=CONCURRENCY)
    measured = sum(
        not c.control for c in think.load(SETS) if isinstance(c, ThinkLeakCase)
    )  # fmt: skip
    assert measured == 300
    metric = _metric(clean, THINK_LEAK)
    _reported(clean, metric, 300, 2)
    assert (metric.violations, metric.verdict) == (0, Verdict.passed)
    assert metric.upper_bound_95 == pytest.approx(upper_bound_95(300))
    assert metric.upper_bound_95 == pytest.approx(0.01, abs=0.0005)
    assert _metric(clean, "priv_think_leak.control_detected").mean == 1.0
    assert clean.suites[0].verdict is Verdict.passed
    markdown = render_markdown(clean)
    assert "0 observed in 300 cases, one-sided 95% upper bound ≈ 0.99%" in markdown
    assert "leak-free" not in markdown.lower()
    # #181: the zero-leak suites run under the seeded profile itself (no dials), so the suite
    # reports the run's own fingerprint.
    assert clean.suites[0].fingerprint is None
    assert clean.suites[0].profile is None

    asymmetry = SUITES["q04"]
    assert isinstance(asymmetry, Q04Suite)
    pool = sorted(
        (c for c in asymmetry.load(SETS) if isinstance(c, AsymmetryCase) and not c.control),
        key=lambda c: c.id,
    )
    injected = next(c for c in pool if c.id == INJECTED_CASE)
    others = [c for c in pool if c.id != INJECTED_CASE][:299]
    one_seed = SEEDS[:1]
    async with stub_engine(pg_url, engine) as lib:
        short = await run(
            asymmetry,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={asymmetry.name: others},
            concurrency=CONCURRENCY,
        )
    short_metric = _metric(short, ASYMMETRY)
    _reported(short, short_metric, 299, 1)
    assert (short_metric.violations, short_metric.verdict) == (0, Verdict.failed)

    async with stub_engine(pg_url, engine) as lib:
        asymmetry.inject(lib.models.stub, injected, one_seed[0])
        leaked = await run(
            asymmetry,
            AdapterConfig(),
            one_seed,
            engine=lib,
            cases={asymmetry.name: [*others, injected]},
            concurrency=CONCURRENCY,
        )
    leak = _metric(leaked, ASYMMETRY)
    _reported(leaked, leak, 300, 1)
    assert (leak.violations, leak.verdict, leak.upper_bound_95) == (1, Verdict.failed, None)
    assert leak.per_seed == [pytest.approx(1 / 300)]

    viewpoint = SUITES["e01_viewpoint"]
    async with stub_engine(pg_url, engine) as lib:
        rated = await run(viewpoint, AdapterConfig(), SEEDS, engine=lib)
    assert rated.suites[0].verdict is Verdict.passed
