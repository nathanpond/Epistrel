"""E-09 (docs/04 §8; FR-CLOCK-7,8, NFR-QUAL-9; #201): the time-estimation suite ``e09`` on the
stub, through #134's harness. The suite's stories run ``clock.level = on``; until #203 delivers
the ``clock`` module the suite delivers it for its own run and restores the delivered set after.

- ``test_e_09__harness``: a stub forced out of range fails the 85 % floor (run first, to show
  the gate bites), then the stub scripted with each case's midpoint passes on the whole set, every
  family and kind in range, and the pacing-health row is reported, informational, over the
  scenario sample; the delivered set is as before the run. No workflow with a schedule runs
  the evals.
- ``test_e_09__harness_misses``: an estimate the live path would not take is out of range: a
  model error, a confidence of 0.2 (under ``MIN_CONFIDENCE``) and one of 0.4 (under the
  structured layer's default ``uncertain_below``, 0.5); the same case answered at 0.9 is in.
- ``test_e_09__harness_pacing``: a scripted render that narrates "An hour later" with nothing
  establishing an activity is refused (FR-CLOCK-8) and counted as one removal: 2 in 4 turns over
  two seeds, 50 per 100 turns.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.profiles import schema
from epistrel.evals import SUITES, AdapterConfig, CaseModel, Rule, Verdict, run
from epistrel.evals.report import SuiteResult
from epistrel.evals.suites.e09_time_estimation import (
    FAMILIES,
    KINDS,
    PACING,
    PACING_CASES,
    WITHIN,
    E09Suite,
    PacingCase,
    TimeEstimationCase,
    family_id,
    kind_id,
)
from epistrel.evals.suites.scenario_play import ScenarioCase
from epistrel.orchestrator.models.access import EVAL_SCOPE
from epistrel.orchestrator.models.registry import TIME_ESTIMATION
from epistrel.orchestrator.models.stub import FaultKind
from epistrel.tools.generated import REPO_ROOT
from tests.eval.suite_support import SEEDS, SETS, assert_floors_pass, by_id, stub_run
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration


def _estimation_cases() -> list[TimeEstimationCase]:
    cases = SUITES["e09"].load(SETS)
    return [c for c in cases if isinstance(c, TimeEstimationCase)]


class OutOfRange(E09Suite):
    """``e09`` with a broken stub: every answer lies past its case's range."""

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, TimeEstimationCase)
        hi = case.acceptable_minutes[1]
        broken = case.stub_answer.model_copy(update={"minutes": hi * 2 + 60})
        return {TIME_ESTIMATION.name: broken.model_dump_json()}


OutOfRange.name = "e09"


async def _run(
    pg_url: str,
    engine: AsyncEngine,
    suite: E09Suite,
    cases: list[CaseModel],
    *,
    failures: Path | None = None,
    error_case: str | None = None,
) -> SuiteResult:
    """``suite`` on the stub over in-memory ``cases``, two seeds; every model call of
    ``error_case`` fails with an HTTP 500."""
    async with stub_engine(pg_url, engine) as lib:
        if error_case is not None:

            def hook(purpose: str) -> FaultKind | None:
                scope = EVAL_SCOPE.get()
                return "http_500" if scope is not None and scope.case_id == error_case else None

            lib.models.stub.fault_hook = hook
        report = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            cases={suite.name: cases},
            failures=failures,
        )
    assert report.errors == [], report.errors
    (result,) = report.suites
    assert result.errors == [], result.errors[:5]
    return result


@pytest.mark.part("M6", "FR-CLOCK-7,8; NFR-QUAL-9")
async def test_e_09__harness(pg_url: str, engine: AsyncEngine) -> None:
    """The gate fails a broken stub and passes the scripted one on the whole set; the pacing
    row is informational and reported over the scenario sample."""
    cases = _estimation_cases()
    assert len(cases) == 200
    assert {c.family for c in cases} == set(FAMILIES)
    assert {c.kind for c in cases} == set(KINDS)

    # A broken stub first: every estimate out of range fails the floor.
    broken = await _run(pg_url, engine, OutOfRange(), list(cases[:30]))
    assert by_id(broken)[WITHIN].mean == 0.0
    assert by_id(broken)[WITHIN].verdict is Verdict.failed
    assert broken.verdict is Verdict.failed

    suite = SUITES["e09"]
    assert suite.ci_gate
    assert suite.guarantees == ("time_estimation",)
    delivered = schema.DELIVERED
    result = await stub_run(pg_url, engine, suite)
    assert schema.DELIVERED is delivered
    assert result.cases == 200 + PACING_CASES
    assert_floors_pass(result)
    metrics = by_id(result)
    assert metrics[WITHIN].mean == 1.0
    assert metrics[WITHIN].sample_size == 200
    for family in FAMILIES:
        assert metrics[family_id(family)].mean == 1.0, family
    for kind in KINDS:
        assert metrics[kind_id(kind)].mean == 1.0, kind
    pacing = metrics[PACING]
    assert pacing.rule is Rule.informational
    assert pacing.verdict is Verdict.report_only
    assert pacing.per_seed == [0.0, 0.0]
    (note,) = [n for n in result.notes if n.startswith("Pacing health (informational)")]
    assert "0 model-initiated skips removed in" in note
    assert "0.00 per 100 turns" in note
    assert result.profile == "Structured, clock on"
    assert result.fingerprint is not None

    # No workflow with a schedule touches the evals: the real model never runs unattended.
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True)) or {}
        if isinstance(triggers, dict) and "schedule" in triggers:
            assert "evals" not in path.read_text(encoding="utf-8"), path.name


def _variant(case: TimeEstimationCase, suffix: str, confidence: float) -> TimeEstimationCase:
    answer = case.stub_answer.model_copy(update={"confidence": confidence})
    return case.model_copy(update={"id": f"{case.id}-{suffix}", "stub_answer": answer})


@pytest.mark.part("M6", "FR-CLOCK-7,8; NFR-QUAL-9")
async def test_e_09__harness_misses(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    """A model error and an estimate under the live path's confidence floor are out of range,
    with the same in-range minutes that pass at confidence 0.9."""
    base = _estimation_cases()[0]
    lo, hi = base.acceptable_minutes
    assert lo <= base.stub_answer.minutes <= hi
    cases: list[CaseModel] = [
        _variant(base, "ok", 0.9),
        _variant(base, "error", 0.9),
        _variant(base, "low", 0.2),
        _variant(base, "uncertain", 0.4),
    ]
    failures = tmp_path / "failures.jsonl"
    result = await _run(
        pg_url, engine, E09Suite(), cases, failures=failures, error_case=f"{base.id}-error"
    )
    assert by_id(result)[WITHIN].per_seed == [0.25, 0.25]
    assert by_id(result)[WITHIN].verdict is Verdict.failed
    records = [json.loads(line) for line in failures.read_text(encoding="utf-8").splitlines()]
    missed = {(r["case"]["id"], r["seed"]) for r in records}
    assert missed == {
        (f"{base.id}-{suffix}", seed) for suffix in ("error", "low", "uncertain") for seed in SEEDS
    }
    for record in records:
        assert record["observations"][WITHIN] is False
        assert record["detail"]["estimate"] is None
        assert record["output"] == "no estimate"


TURN = {"stub_judge": "final", "stub_voice": 4, "expected": []}


def _render(*beats: tuple[str, str]) -> str:
    return json.dumps(
        {
            "beats": [
                {"kind": kind, "text": text, **({"intent": "sincere"} if kind == "say" else {})}
                for kind, text in beats
            ]
        }
    )


@pytest.mark.part("M6", "FR-CLOCK-7,8; NFR-QUAL-9")
async def test_e_09__harness_pacing(pg_url: str, engine: AsyncEngine) -> None:
    """A model-initiated skip the scene established no activity for is refused and counted:
    one removal in two turns a seed, 50 per 100 turns; the estimation floor is unaffected."""
    scenario = ScenarioCase.model_validate(
        {
            "id": "scenarios-skip-0001",
            "family": "skip",
            "origin": "authored",
            "config": "structured",
            "drivers": {},
            "turns": [
                {
                    **TURN,
                    "say": "Where did you leave your coat?",
                    "stub": {
                        "render": _render(
                            ("say", "On the hook, boss."), ("do", "Bobby points at the hallway.")
                        )
                    },
                },
                {
                    **TURN,
                    "say": "Anything else to report?",
                    "stub": {
                        "render": _render(
                            ("say", "Not a thing."),
                            ("do", "An hour later, Bobby stretches by the window."),
                        ),
                        "regeneration": _render(("do", "Bobby stretches by the window.")),
                    },
                },
            ],
        }
    )
    cases: list[CaseModel] = [
        _estimation_cases()[0],
        PacingCase(id=f"pacing-{scenario.id}", scenario=scenario),
    ]
    result = await _run(pg_url, engine, E09Suite(), cases)
    metrics = by_id(result)
    assert metrics[PACING].per_seed == [0.5, 0.5]
    assert metrics[PACING].verdict is Verdict.report_only
    assert metrics[WITHIN].per_seed == [1.0, 1.0]
    (note,) = [n for n in result.notes if n.startswith("Pacing health (informational)")]
    assert "2 model-initiated skips removed in 4 turns" in note
    assert "50.00 per 100 turns" in note
