"""``evals run --case`` (#735): a targeted rerun of named cases. Exact ids, repeatable; a suite
holding none of them plays nothing; the report names the cases, is partial, never merges with
another run, and a later ``--suite`` run never merges into it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import AdapterConfig, CaseModel, Outcome, Suite, cli, run
from epistrel.evals import report as reports
from epistrel.evals.gate import Metric, Rule
from epistrel.evals.runner import CaseContext, kept
from tests.evals.support import stub_engine

PLAYED = "case_filter.played"
IDS = ("scenarios-plain-0001", "scenarios-plain-0002", "scenarios-rung-1-0002")


def _cases(*ids: str) -> list[CaseModel]:
    return [CaseModel(id=i) for i in ids]


def test_kept_takes_exact_ids_in_order_and_empty_keeps_all() -> None:
    cases = _cases(*IDS)
    assert [c.id for c in kept(cases, {"scenarios-rung-1-0002", "scenarios-plain-0001"})] == [
        "scenarios-plain-0001",
        "scenarios-rung-1-0002",
    ]
    assert kept(cases, ()) == cases
    assert kept(cases, {"plain-0001"}) == []  # exact ids, never a substring


def test_case_is_repeatable_and_a_case_run_is_always_new(tmp_path: Path) -> None:
    """With ``--case`` the run never picks up the same day's run of its fingerprint.
    Complement asserted: without it, the same ``--suite`` run merges into that run."""
    (tmp_path / "full-abc-2026-11-20.json").write_text("{}", encoding="utf-8")
    argv = ["run", "--adapter", "stub", "--suite", "e12", "--json-dir", str(tmp_path)]
    cased = cli.parser().parse_args([*argv, "--case", IDS[0], "--case", IDS[1]])
    assert cased.cases == [IDS[0], IDS[1]]
    plain = cli.parser().parse_args(argv)
    for args in (cased, plain):
        args.reports_dir = tmp_path
    assert cli._run_id(cased, "full-abc-2026-11-20") == ("full-abc-2026-11-20-2", None)
    merged_into = tmp_path / "full-abc-2026-11-20.json"
    assert cli._run_id(plain, "full-abc-2026-11-20") == ("full-abc-2026-11-20", merged_into)


def _report(**fields: Any) -> reports.Report:
    return reports.Report(
        run_id="full-abc-2026-11-20",
        started_at="2026-11-20T00:00:00+00:00",
        model="stub",
        fingerprint="sha256:" + "0" * 64,
        fingerprint_profile="Full v1",
        adapter="stub",
        engine_sha="0" * 7,
        host={"kind": "ci"},
        seed_base=1,
        seeds=[1],
        **fields,
    )


def test_a_partial_report_says_so() -> None:
    partial = _report(only_cases=[IDS[0]])
    assert f"**Partial:** only 1 named cases (`{IDS[0]}`" in reports.render_markdown(partial)
    assert "**Partial:**" not in reports.render_markdown(_report())
    again = reports.Report.from_json(json.loads(partial.model_dump_json()))
    assert again.only_cases == [IDS[0]]


class _Counts(Suite):
    """Records each case it plays."""

    name = "case_filter_probe"
    title = "run --case keeps the named cases"
    metrics = (Metric(PLAYED, Rule.informational),)
    played: ClassVar[list[str]] = []

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        self.played.append(case.id)
        return Outcome({PLAYED: True})


@pytest.mark.integration
async def test_only_the_named_cases_play(pg_url: str, engine: AsyncEngine) -> None:
    """Complement asserted: the same run without ``only`` plays every case, and its report
    names none."""
    suite = _Counts()
    cases = {suite.name: _cases(*IDS)}
    _Counts.played.clear()
    async with stub_engine(pg_url, engine) as lib:
        partial = await run(suite, AdapterConfig(), [1], engine=lib, cases=cases, only=[IDS[2]])
        assert _Counts.played == [IDS[2]]
        _Counts.played.clear()
        whole = await run(suite, AdapterConfig(), [1], engine=lib, cases=cases)
    assert sorted(_Counts.played) == list(IDS)  # cases may finish in any order
    assert (partial.only_cases, whole.only_cases) == ([IDS[2]], None)
