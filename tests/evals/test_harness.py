"""The eval harness (#134): NFR-QUAL-8's gate rules, the report's fields, and a run of every
registered suite against the stub adapter through the real code paths."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.profiles.fingerprint import FINGERPRINT_PATTERN
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.evals import (
    SUITES,
    AdapterConfig,
    Direction,
    Metric,
    Report,
    Rule,
    SeedTally,
    Verdict,
    cli,
    gate,
    judge,
    run,
    run_suites,
    seeds_from,
    upper_bound_95,
)
from epistrel.evals import check as checks
from epistrel.evals.report import (
    INDEX_NAME,
    read_summary,
    render_markdown,
    unique_run_id,
    write_index,
    write_markdown,
)
from epistrel.evals.runner import (
    PRINCIPAL,
    SCRATCH_PREFIX,
    CaseContext,
    NotScratch,
    Scratch,
    run_fingerprint,
    scratch_story_ids,
    story_rows,
    sweep,
)
from epistrel.orchestrator.models.registry import QualityJudgeVerdict
from tests.evals.support import (
    LEAK,
    ZERO_METRIC,
    FloorlessSuite,
    SyntheticZeroSuite,
    stub_engine,
    synthetic_cases,
)

SEEDS = seeds_from(2)

# -- the rules, without a run --------------------------------------------------------------------


def _rate(floor: float, direction: Direction = Direction.at_least) -> Metric:
    return Metric("x.rate", Rule.rate, floor, direction)


def test_rate_rule_needs_the_mean_clear_of_the_floor_by_more_than_one_sd() -> None:
    """≥ floors pass only when mean - SD is above the floor, ≤ floors when mean + SD is below
    it; on the floor is not clear of it. Complement: the same means with a wider spread fail."""
    metric = _rate(0.90)
    tight = [SeedTally(1, 96, 4), SeedTally(2, 94, 6)]  # 0.96, 0.94: mean .95, sd ≈ .014
    assert judge(metric, tight).verdict is Verdict.passed
    wide = [SeedTally(1, 99, 1), SeedTally(2, 91, 9)]  # 0.99, 0.91: mean .95, sd ≈ .057
    assert judge(metric, wide).verdict is Verdict.failed
    exact = [SeedTally(1, 9, 1), SeedTally(2, 9, 1)]  # mean exactly the floor, sd 0
    assert judge(metric, exact).verdict is Verdict.failed
    at_most = _rate(0.02, Direction.at_most)
    assert judge(at_most, [SeedTally(1, 99, 1), SeedTally(2, 99, 1)]).verdict is Verdict.passed
    assert judge(at_most, [SeedTally(1, 98, 2), SeedTally(2, 98, 2)]).verdict is Verdict.failed
    found = judge(metric, tight)
    assert found.mean == pytest.approx(0.95)
    assert found.stdev == pytest.approx(0.0141421, rel=1e-4)
    assert (found.sample_size, found.seed_count) == (100, 2)


def test_one_seed_or_no_observation_is_insufficient_evidence() -> None:
    """A standard deviation needs two seeds; a metric nobody observed has no evidence. A
    violation on one seed still fails a zero-tolerance metric."""
    metric = _rate(0.5)
    assert judge(metric, [SeedTally(1, 10, 0)]).verdict is Verdict.insufficient_evidence
    assert judge(metric, [SeedTally(1), SeedTally(2)]).verdict is Verdict.insufficient_evidence
    zero = Metric("x.zero", Rule.zero, 0.0, Direction.at_most)
    assert judge(zero, [SeedTally(1, 400, 0)]).verdict is Verdict.insufficient_evidence
    assert judge(zero, [SeedTally(1, 399, 1)]).verdict is Verdict.failed
    assert judge(zero, []).verdict is Verdict.insufficient_evidence
    info = Metric("x.info", Rule.informational)
    assert judge(info, [SeedTally(1, 1, 1), SeedTally(2, 2, 0)]).verdict is Verdict.report_only


def test_upper_bound_is_the_exact_one_sided_binomial_bound() -> None:
    assert upper_bound_95(300) == pytest.approx(1 - 0.05 ** (1 / 300))
    assert upper_bound_95(300) == pytest.approx(0.01, abs=0.0005)
    assert upper_bound_95(3000) == pytest.approx(3 / 3000, rel=0.01)
    assert upper_bound_95(0) == 1.0


def test_gate_folds_verdicts() -> None:
    p, f, i, r, e = (
        Verdict.passed,
        Verdict.failed,
        Verdict.insufficient_evidence,
        Verdict.report_only,
        Verdict.error,
    )
    assert gate([p, r]) is p
    assert gate([p, i, r]) is i
    assert gate([p, i, f]) is f
    assert gate([p, e]) is f
    assert gate([r]) is r


def test_metric_problems_catch_undeclared_floors() -> None:
    """``--check``'s per-metric rules: a floored rule needs a floor, a zero-tolerance one is 0
    on the ``at_most`` side, an informational one has none, ids are lower-case and dotted."""
    from epistrel.evals.gate import metric_problems

    assert (
        metric_problems(Metric("nfr_qual_2.false_support", Rule.rate, 0.01, Direction.at_most))
        == []
    )
    assert metric_problems(Metric("x.rate", Rule.rate, None)) == ["x.rate: rule rate but no floor"]
    assert metric_problems(Metric("x.zero", Rule.zero, 0.0, Direction.at_least))
    assert metric_problems(Metric("x.info", Rule.informational, 0.5))
    assert metric_problems(Metric("NFR-QUAL-2", Rule.rate, 0.5))
    assert Metric("nfr_qual_10.recall", Rule.rate, 0.95).requirement == "NFR-QUAL-10"
    assert Metric("smoke.turn_completed", Rule.rate, 0.95).requirement is None


# -- E-08 against the stub -------------------------------------------------------------------------


async def _zero_run(
    pg_url: str, engine: AsyncEngine, n: int, leak: tuple[str, int] | None
) -> Report:
    async with stub_engine(pg_url, engine) as lib:
        if leak is not None:
            case_id, seed = leak
            lib.models.stub.case(case_id, f"Here it is: {LEAK}", seed=seed)
        suite = SyntheticZeroSuite()
        return await run(
            suite, AdapterConfig(), SEEDS, engine=lib, cases={suite.name: synthetic_cases(n)}
        )


@pytest.mark.integration
@pytest.mark.part("M4", "NFR-QUAL-8")
async def test_e_08__m4_gate_rules(pg_url: str, engine: AsyncEngine) -> None:
    """The observed-zero rule through a stub run of the synthetic zero-tolerance suite, every
    case one model call through the Model Access Layer: 299 clean cases on every seed fail
    (fewer than 300); 300 cases with one deterministically injected violation (the scripted
    adapter leaks on a fixed case of a fixed seed) fail; 300 clean cases on every seed pass,
    reported with a one-sided 95% upper bound of about 1%. Every result reports its sample size,
    seed count and model/version.

    Complement asserted: the injected violation shows on its own seed only, and the clean runs
    never answer with the leak."""
    short = await _zero_run(pg_url, engine, 299, None)
    leaked = await _zero_run(pg_url, engine, 300, ("syn-0150", SEEDS[1]))
    clean = await _zero_run(pg_url, engine, 300, None)
    for report, n in ((short, 299), (leaked, 300), (clean, 300)):
        (metric,) = report.suites[0].metrics
        assert metric.id == ZERO_METRIC
        assert (metric.sample_size, metric.seed_count) == (n, 2)
        assert (report.model, report.model_version, report.seed_count) == ("stub", "stub", 2)
        assert report.sample_size == n
        assert report.harness_errors == []
    (short_metric,) = short.suites[0].metrics
    assert (short_metric.violations, short_metric.verdict) == (0, Verdict.failed)
    (leak_metric,) = leaked.suites[0].metrics
    assert (leak_metric.violations, leak_metric.verdict) == (1, Verdict.failed)
    assert leak_metric.per_seed == [0.0, pytest.approx(1 / 300)]
    assert leak_metric.upper_bound_95 is None
    (clean_metric,) = clean.suites[0].metrics
    assert (clean_metric.violations, clean_metric.verdict) == (0, Verdict.passed)
    assert clean_metric.upper_bound_95 == pytest.approx(0.01, abs=0.0005)
    assert (short.verdict, leaked.verdict, clean.verdict) == (
        Verdict.failed,
        Verdict.failed,
        Verdict.passed,
    )
    markdown = render_markdown(clean)
    assert "0 observed in 300 cases, one-sided 95% upper bound ≈ 0.99%" in markdown
    assert "leak-free" not in markdown.lower()


# -- every registered suite against the stub --------------------------------------------------


REPORT_FIELDS = {
    "version",
    "run_id",
    "sample_size",
    "seed_count",
    "seeds",
    "model",
    "model_version",
    "fingerprint",
    "host",
    "suites",
    "verdict",
}
METRIC_FIELDS = {"id", "mean", "stdev", "floor", "rule", "verdict", "sample_size", "seed_count"}


@pytest.mark.integration
async def test_harness_stub_run(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    """Every registered suite runs on the stub, two seeds, through scratch stories: the report
    carries every field, each suite's stub floors pass when CI gates on it, nothing outside the
    scratch stories changed, and the Markdown names every metric.

    Complement asserted: a suite whose rate metric has no floor fails ``--check``."""
    before = await story_rows(engine)
    async with stub_engine(pg_url, engine) as lib:
        report = await run_suites(
            list(SUITES.values()), AdapterConfig(), SEEDS, engine=lib, failures=tmp_path / "f.jsonl"
        )
        # The turns' calls: not the preflight probe, which goes to the adapter directly.
        played = [
            c for c in lib.models.stub.calls if c.purpose == "generation" and len(c.messages) > 1
        ]
    assert set(report.as_json()) >= REPORT_FIELDS
    assert re.match(FINGERPRINT_PATTERN, report.fingerprint)
    assert report.fingerprint == run_fingerprint("stub")
    assert report.host.kind in {"ci", "local"}
    assert {s.name for s in report.suites} == set(SUITES)
    assert report.harness_errors == []
    for suite in report.suites:
        assert suite.cases > 0
        assert suite.seeds == SEEDS
        for metric in suite.metrics:
            assert set(metric.model_dump()) >= METRIC_FIELDS
            assert metric.sample_size == suite.cases
            assert metric.seed_count == 2
        if suite.ci_gate:
            assert suite.verdict is Verdict.passed, suite
    # Every turn was a real one on a scratch story, under its case's eval scope and seed.
    assert played
    assert all("X-Eval-Case: smoke-" in c.messages[0].content for c in played)
    assert {c.params.seed for c in played} == set(SEEDS)
    assert await story_rows(engine) == before
    assert await scratch_story_ids(engine) == []
    markdown = render_markdown(report)
    for suite in report.suites:
        for metric in suite.metrics:
            assert f"`{metric.id}`" in markdown
    assert "verified" not in markdown.lower()
    assert not (tmp_path / "f.jsonl").exists()  # the stub floors held: no failed case
    assert checks.problems(suites=list(SUITES.values()), reports_dir=_index_dir(tmp_path)) == []
    found = checks.problems(
        suites=[*SUITES.values(), FloorlessSuite()], reports_dir=_index_dir(tmp_path)
    )
    assert found == ["suite floorless: floorless.rate: rule rate but no floor"]


def _index_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "reports"
    folder.mkdir(exist_ok=True)
    write_index(folder)
    return folder


# -- isolation and the scratch stories -------------------------------------------------------


@pytest.mark.integration
async def test_the_judge_refuses_any_story_that_is_not_the_case_s_own(
    pg_url: str, engine: AsyncEngine
) -> None:
    """``quality_judge`` runs only on a case's scratch stories; a live story is refused before
    any call. The sweep deletes scratch stories only: a story another principal named with the
    scratch prefix stays."""
    cast = [
        CharacterSpec(name="Wren", definition="A traveller.", true_age=29, is_player=True),
        CharacterSpec(name="Maren", definition="The innkeeper.", true_age=46),
    ]
    async with stub_engine(pg_url, engine) as lib:
        live = await lib.create_story(
            RequestContext(principal="p-evals-live"),
            StorySpec(name=f"{SCRATCH_PREFIX}not-mine", characters=cast),
        )
        scratch = Scratch(lib, "judge-test")
        ctx = CaseContext(lib, scratch, SEEDS[0], "judge-test")
        try:
            own = await ctx.story(StorySpec(characters=cast))
            with pytest.raises(NotScratch):
                await ctx.judge(live.story_id, "A.", "B.")
            calls = len(lib.models.stub.calls)
            verdict = await ctx.judge(own.story_id, "A plain line.", "A plainer line.")
            assert isinstance(verdict, QualityJudgeVerdict)
            assert [c.purpose for c in lib.models.stub.calls[calls:]] == ["quality_judge"]
            async with engine.connect() as conn:
                owner = (
                    await conn.execute(
                        text("SELECT owner_principal, name FROM stories WHERE id = :id"),
                        {"id": own.story_id},
                    )
                ).one()
            assert owner[0] == PRINCIPAL
            assert owner[1].startswith(f"{SCRATCH_PREFIX}judge-test-")
            assert (
                await sweep(lib) == 1
            )  # the case's story, left behind as an interrupted run would
            assert await scratch_story_ids(engine) == []
        finally:
            await lib.delete_story(RequestContext(principal="p-evals-live"), live.story_id)


@pytest.mark.integration
def test_a_malformed_case_fails_the_run(pg_url: str, tmp_path: Path) -> None:
    """A labeled set with a malformed line is a harness error: the suite's verdict is
    ``error`` and the CLI exits 1 after writing the report. Complement: the well-formed set
    exits 0."""
    sets = tmp_path / "sets" / "smoke"
    sets.mkdir(parents=True)
    good = '{"id": "smoke-001", "say": "Say \\"lantern\\", please.", "keyword": "lantern"}\n'
    (sets / "cases.jsonl").write_text(good, encoding="utf-8")
    argv = [
        "run",
        "--suite",
        "smoke",
        "--adapter",
        "stub",
        "--database-url",
        pg_url,
        "--sets-dir",
        str(tmp_path / "sets"),
        "--json-dir",
        str(tmp_path / "json"),
        "--markdown",
        str(tmp_path / "stub-report.md"),
        "--fail-on-gate",
        "--new-run",
    ]
    assert cli.main(argv) == 0
    (sets / "cases.jsonl").write_text(good + '{"id": "smoke-002", "say": ""}\n', encoding="utf-8")
    assert cli.main(argv) == 1
    report = Report.from_json(
        json.loads(max((tmp_path / "json").glob("*.json"), key=_mtime).read_text())
    )
    (suite,) = report.suites
    assert suite.verdict is Verdict.error
    assert "line 2" in suite.errors[0]
    assert "**error**" in (tmp_path / "stub-report.md").read_text()


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


# -- reports and the index ------------------------------------------------------------------


def _report(model: str, day: str, verdicts: dict[str, Verdict]) -> Report:
    from epistrel.evals.report import Host, MetricResult, SuiteResult

    metrics = [
        MetricResult(
            id=metric_id,
            rule=Rule.rate,
            direction=Direction.at_least,
            floor=0.9,
            mean=0.95,
            stdev=0.01,
            sample_size=10,
            seed_count=2,
            per_seed=[0.94, 0.96],
            verdict=verdict,
        )
        for metric_id, verdict in verdicts.items()
    ]
    suite = SuiteResult(
        name="e01",
        title="t",
        ci_gate=False,
        cases=10,
        seeds=SEEDS,
        metrics=metrics,
        verdict=gate(verdicts.values()),
    )
    report = Report(
        run_id=f"abc-{day}",
        started_at=f"{day}T00:00:00+00:00",
        model=model,
        fingerprint="sha256:" + "a" * 64,
        fingerprint_profile="Structured (M4) v1",
        adapter="openai",
        engine_sha="abc",
        host=Host(kind="local", hostname="h"),
        seed_base=SEEDS[0],
        seeds=SEEDS,
    )
    report.merge(suite)
    report.finish(__import__("datetime").datetime(2026, 10, 17))
    return report


def test_index_lists_reports_newest_first_with_a_verdict_per_requirement(tmp_path: Path) -> None:
    """``INDEX.md`` regenerates from the reports' embedded summaries: newest first, one column
    per NFR-QUAL requirement (the gate over its sub-metrics). ``--check`` sees a stale index.
    Complement: a hand-edited index is stale."""
    older = _report("m-old", "2026-10-01", {"nfr_qual_2.false_support": Verdict.passed})
    newer = _report(
        "m-new",
        "2026-10-17",
        {
            "nfr_qual_2.false_support": Verdict.passed,
            "nfr_qual_2.false_contradiction": Verdict.failed,
        },
    )
    write_markdown(older, tmp_path / f"{older.run_id}.md")
    write_markdown(newer, tmp_path / f"{newer.run_id}.md")
    embedded = read_summary(tmp_path / f"{newer.run_id}.md")
    assert embedded is not None
    assert embedded["requirements"] == {"NFR-QUAL-2": "fail"}
    write_index(tmp_path)
    index = (tmp_path / INDEX_NAME).read_text()
    rows = [line for line in index.splitlines() if line.startswith("| 2026")]
    assert [r.split(" | ")[1] for r in rows] == ["`m-new`", "`m-old`"]
    assert "| NFR-QUAL-2 |" in index
    assert checks.problems(suites=list(SUITES.values()), reports_dir=tmp_path) == []
    (tmp_path / INDEX_NAME).write_text(index + "edited\n")
    assert checks.problems(suites=list(SUITES.values()), reports_dir=tmp_path) == [
        f"{tmp_path / INDEX_NAME} is stale: run `uv run python -m epistrel.evals index`"
    ]


def test_run_ids_take_a_suffix_on_the_same_day() -> None:
    taken = [Path("a-2026-10-17.json"), Path("a-2026-10-17-2.md")]
    assert unique_run_id("a-2026-10-18", taken) == "a-2026-10-18"
    assert unique_run_id("a-2026-10-17", taken) == "a-2026-10-17-3"


def test_check_cross_references_the_guarantee_table() -> None:
    """Every model-dependent guarantee row's metric is measured by a suite or pending with the
    story that ships it; a pending metric a suite already measures is an error too.
    Complement: with nothing pending, the unmeasured rows are named."""
    assert checks.problems() == []
    unmeasured = checks.problems(pending={})
    assert (
        "guarantee row entailment_fallback: its metric NFR-QUAL-2 is measured by no eval suite"
        in unmeasured
    )
    assert (
        "guarantee row quality_judge: its metric NFR-QUAL-14 is measured by no eval suite"
        in unmeasured
    )

    class Measures(SyntheticZeroSuite):
        name = "measures_2"
        guarantees = ("entailment_fallback",)
        metrics = (Metric("nfr_qual_2.false_support", Rule.rate, 0.01, Direction.at_most),)

    found = checks.problems(suites=[*SUITES.values(), Measures()])
    assert found == ["NFR-QUAL-2 is measured now: remove it from PENDING (#157)"]

    class Wrong(SyntheticZeroSuite):
        name = "wrong_row"
        guarantees = ("generation", "context_assembly", "nope")

    assert checks.suite_problems(Wrong()) == [
        "suite wrong_row: declares guarantee row generation but reports no NFR-QUAL-7 metric",
        "suite wrong_row: guarantee row context_assembly is not model-dependent",
        "suite wrong_row: no guarantee row 'nope'",
    ]
