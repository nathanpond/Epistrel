"""A model error counts only in the rates its case's label puts it in (#530, ADR-194).

An eval case a ``ModelError`` ended observes ``False`` on the metrics ``Suite.error_metrics``
declares for it and nothing on the rest; a suite that declares nothing keeps #134's rule (every
metric bad). An aggregate that sums labelled items from ``detail`` (E-05, Q-01) gets the case's
items as missed (``Suite.error_detail``). Journal rows scored under #134's rule replay.

No test here carries a part marker: this is the harness's scoring rule (docs/04 §8, ADR-194),
and E-01's own M11 part is ``tests/eval/test_e_01.py``'s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals.journal import SCORING_REVISION, Journal
from epistrel.evals.report import MetricResult
from epistrel.evals.runner import DEFAULT_SETS_DIR, AdapterConfig, model_error_outcome, run_suites
from epistrel.evals.suites import SUITES, CaseModel, Counts, Outcome, Suite, smoke
from epistrel.evals.suites import e01_entailment as e01
from epistrel.evals.suites import e01_viewpoint as e01v
from epistrel.evals.suites import e02_escalation as e02
from epistrel.evals.suites import e03_consolidation as e03
from epistrel.evals.suites import e04_importance as e04
from epistrel.evals.suites import e05_salience as e05
from epistrel.evals.suites import e06_conformance as e06
from epistrel.evals.suites import e09_time_estimation as e09
from epistrel.evals.suites import e10_claim_detection as e10
from epistrel.evals.suites import q01_recall as q01
from epistrel.evals.suites import q03_abstract as q03
from epistrel.orchestrator.models.errors import ModelUnavailable
from tests.evals.support import SyntheticZeroSuite, stub_engine

ERROR = ModelUnavailable("entailment", "timeout")
DECLARING = (
    "e01",
    "e01_viewpoint",
    "e02",
    "e03",
    "e04",
    "e05",
    "e06",
    "e09",
    "e10",
    "q01",
    "q03",
    "smoke",
)


def _cases(name: str) -> list[CaseModel]:
    return SUITES[name].load(DEFAULT_SETS_DIR)


def _observed(suite: Suite, case: CaseModel) -> dict[str, bool | None]:
    return dict(model_error_outcome(suite, ERROR, case).observations)


def _counted(suite: Suite, case: CaseModel) -> set[str]:
    observed = _observed(suite, case)
    assert all(v is False for v in observed.values() if v is not None)
    return {k for k, v in observed.items() if v is not None}


def _metric(results: list[MetricResult], metric_id: str) -> MetricResult:
    return next(m for m in results if m.id == metric_id)


class RaisingE01(e01.E01Suite):
    """E-01 with every case ending in a model error (its declarations unchanged)."""

    async def evaluate(self, ctx: Any, case: CaseModel) -> Outcome:
        raise ModelUnavailable("entailment", "timeout")


@pytest.mark.integration
async def test_an_e01_error_counts_only_where_its_label_is_measured(
    pg_url: str, engine: AsyncEngine
) -> None:
    """Through the runner: an error on a ``supported`` residual case is one failed
    ``false_contradiction`` observation, and ``false_support``'s sample size is unchanged (0:
    the case is outside its denominator); the symmetric holds for ``contradicted``. Under
    #134's rule (the pre-fix runner) both rates took a failure."""
    by_label: dict[str, CaseModel] = {}
    for case in _cases("e01"):
        assert isinstance(case, e01.EntailmentCase)
        if case.residual:
            by_label.setdefault(case.expected, case)
    suite = RaisingE01()
    async with stub_engine(pg_url, engine) as lib:
        supported = await run_suites(
            [suite], AdapterConfig(), [1], engine=lib, cases={"e01": [by_label["supported"]]}
        )
        contradicted = await run_suites(
            [suite], AdapterConfig(), [1], engine=lib, cases={"e01": [by_label["contradicted"]]}
        )
    on_supported = supported.suites[0].metrics
    assert _metric(on_supported, e01.FALSE_CONTRADICTION).sample_size == 1
    assert _metric(on_supported, e01.FALSE_CONTRADICTION).mean == 1.0
    assert _metric(on_supported, e01.FALSE_SUPPORT).sample_size == 0
    on_contradicted = contradicted.suites[0].metrics
    assert _metric(on_contradicted, e01.FALSE_SUPPORT).sample_size == 1
    assert _metric(on_contradicted, e01.FALSE_SUPPORT).mean == 1.0
    assert _metric(on_contradicted, e01.FALSE_CONTRADICTION).sample_size == 0
    for result in (on_supported, on_contradicted):
        assert _metric(result, e01.ACCURACY).sample_size == 1
        assert _metric(result, e01.RESIDUAL_ACCURACY).sample_size == 1


def test_e01_declares_by_label_and_residual() -> None:
    suite = SUITES["e01"]
    seen: set[tuple[str, bool]] = set()
    for case in _cases("e01"):
        assert isinstance(case, e01.EntailmentCase)
        counted = _counted(suite, case)
        assert (e01.FALSE_SUPPORT in counted) is (case.expected != "supported")
        assert (e01.FALSE_CONTRADICTION in counted) is (case.expected != "contradicted")
        assert e01.ACCURACY in counted
        assert (e01.RESIDUAL_ACCURACY in counted) is case.residual
        seen.add((case.expected, case.residual))
    assert {label for label, _ in seen} == {"supported", "contradicted", "novel", "uncertain"}


def test_e01_viewpoint_declares_by_objective_label() -> None:
    suite = SUITES["e01_viewpoint"]
    labels = set()
    for case in _cases("e01_viewpoint"):
        assert isinstance(case, e01v.ViewpointCase)
        counted = _counted(suite, case)
        supported = case.expected.objective == "supported"
        assert (e01v.FALSE_SUPPORT in counted) is not supported
        assert {e01v.FALSE_CONTRADICTION, e01v.ACCURACY, e01v.CLASSIFICATION_ACCURACY} <= counted
        labels.add(supported)
    assert labels == {True, False}


def test_e02_declares_by_kind() -> None:
    suite = SUITES["e02"]
    kinds = set()
    for case in _cases("e02"):
        assert isinstance(case, e02.E02Case)
        counted = _counted(suite, case)
        if case.kind == "promotion":
            assert counted == {e02.PROMOTION_RULE}
        else:
            assert e02.PROMOTION_RULE not in counted
            assert len(counted) == 4
        kinds.add(case.kind)
    assert kinds == {"escalation", "promotion"}


def test_e03_e04_q03_smoke_declare_their_measured_rows() -> None:
    (window,) = _cases("e03")[:1]
    assert _counted(SUITES["e03"], window) == {e03.PRODUCED, e03.FAITHFULNESS}
    (item,) = _cases("e04")[:1]
    assert _counted(SUITES["e04"], item) == {e04.SCORED, e04.ABOVE_MEDIAN}
    (abstract,) = _cases("q03")[:1]
    assert _counted(SUITES["q03"], abstract) == {q03.WITH, q03.WITHOUT, q03.REFLECTION_FOUND}
    (turn,) = _cases("smoke")[:1]
    assert _counted(SUITES["smoke"], turn) == {smoke.TURN_COMPLETED, smoke.INSTRUCTION_FOLLOWED}


def test_e05_counts_recall_or_texture_and_misses_the_labelled_spans() -> None:
    suite = SUITES["e05"]
    labelled = [c for c in _cases("e05") if isinstance(c, e05.SalienceCase) and c.expected]
    texture = [c for c in _cases("e05") if isinstance(c, e05.SalienceCase) and not c.expected]
    assert labelled
    assert texture
    assert _counted(suite, labelled[0]) == {e05.ANSWERED, e05.RECALL}
    assert _counted(suite, texture[0]) == {e05.ANSWERED, e05.TEXTURE_CLEAN}
    case = labelled[0]
    assert isinstance(case, e05.SalienceCase)
    errored = model_error_outcome(suite, ERROR, case)
    found = suite.aggregate([errored])
    assert found[e05.RECALL] == Counts(0, len(case.expected))
    assert found[e05.PRECISION] == Counts(0, 0)


def test_e06_counts_repeatable_only_on_repeat_cases() -> None:
    suite = SUITES["e06"]
    repeat = {True: False, False: False}
    for case in _cases("e06"):
        assert isinstance(case, e06.ConformanceCase)
        counted = _counted(suite, case)
        assert {e06.STRUCTURE, e06.LENGTH, e06.CONFORMANCE} <= counted
        assert (e06.REPEATABLE in counted) is case.inputs.repeat
        repeat[case.inputs.repeat] = True
    assert all(repeat.values())


def test_e09_counts_its_family_and_kind_and_pacing_nothing() -> None:
    suite = SUITES["e09"]
    for case in _cases("e09")[:40]:
        assert isinstance(case, e09.TimeEstimationCase)
        assert _counted(suite, case) == {
            e09.WITHIN,
            e09.family_id(case.family),
            e09.kind_id(case.kind),
        }
    pacing = e09.pacing_cases(DEFAULT_SETS_DIR, 1)
    assert pacing
    assert _counted(suite, pacing[0]) == set()


def test_e10_counts_its_own_category_in_both_modes() -> None:
    suite = SUITES["e10"]
    for case in _cases("e10")[:20]:
        assert isinstance(case, e10.DetectionCase)
        counted = _counted(suite, case)
        categories = {m.id for m in suite.metrics if ".layer." not in m.id} - {
            e10.recall_id(mode) for mode in e10.MODES
        }
        own = {e10.category_id(mode, case.expected.category) for mode in e10.MODES}
        assert counted & categories == own
        assert {e10.recall_id(mode) for mode in e10.MODES} <= counted


def test_q01_misses_every_relevant_item_instead_of_failing_its_aggregate() -> None:
    """Before #530, an errored Q-01 case's outcome had no ``found``/``missed``/``rr``, so the
    seed's ``aggregate`` raised ``KeyError``."""
    suite = SUITES["q01"]
    case = _cases("q01")[0]
    assert isinstance(case, q01.RecallCase)
    assert _counted(suite, case) == {q01.ALL_FOUND}
    errored = model_error_outcome(suite, ERROR, case)
    found = suite.aggregate([errored])
    assert found[q01.RECALL] == Counts(0, len(set(case.expected.relevant)))


def test_a_suite_that_declares_nothing_fails_every_metric() -> None:
    """#134's rule is the default, so a new suite is conservative."""
    suite = SyntheticZeroSuite()
    observed = _observed(suite, CaseModel(id="c-1"))
    assert observed == {m.id: False for m in suite.metrics}


@pytest.mark.parametrize("name", DECLARING)
def test_every_declaration_names_the_suites_own_metrics(name: str) -> None:
    """Every case of every declaring suite's set declares only metrics of its suite, and
    at least one."""
    suite = SUITES[name]
    ids = {m.id for m in suite.metrics}
    cases = [*suite.load(DEFAULT_SETS_DIR), *suite.companions(DEFAULT_SETS_DIR, 3)]
    for case in cases:
        declared = suite.error_metrics(case)
        assert declared is not None
        assert declared <= ids
    assert any(suite.error_metrics(c) for c in cases)


def test_a_pre_530_model_error_row_plays_again(tmp_path: Path) -> None:
    """A journalled model error scored under #134's rule (no ``scoring`` on the line) is not
    reused; a completed case's row is; rows written now carry the current revision."""
    path = tmp_path / "j.jsonl"
    journal = Journal.open(path, model="m", fingerprint="sha256:" + "0" * 64)
    case_ok, case_err = CaseModel(id="ok"), CaseModel(id="err")
    errored = Outcome({"x": False}, detail={"model_error": "model_unavailable", "reason": "x"})
    for case, outcome in ((case_ok, Outcome({"x": True})), (case_err, errored)):
        journal.record(
            "s", None, case, 1, outcome, 10, engine_sha="a", model_version=None, preset=None
        )
    lines = path.read_text("utf-8").splitlines()
    assert all(f'"scoring": {SCORING_REVISION}' in line for line in lines)
    path.write_text(
        "".join(line.replace(f', "scoring": {SCORING_REVISION}', "") + "\n" for line in lines),
        "utf-8",
    )
    reopened = Journal.open(path, model="m", fingerprint="sha256:" + "0" * 64, retry_errors="none")
    assert reopened.lookup("s", None, case_ok, 1) is not None
    assert reopened.lookup("s", None, case_err, 1) is None
    current = Journal.open(path, model="m", fingerprint="sha256:" + "0" * 64, retry_errors="none")
    current.record(
        "s", None, case_err, 1, errored, 10, engine_sha="a", model_version=None, preset=None
    )
    again = Journal.open(path, model="m", fingerprint="sha256:" + "0" * 64, retry_errors="none")
    assert again.lookup("s", None, case_err, 1) is not None
