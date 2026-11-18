"""E-12: engine interventions and prose, in aggregate (docs/04 §8; NFR-QUAL-8,14), its M4 and M11
parts (#158; NFR-QUAL-14 gates from M11 per ADR-164, and this stub test runs in CI either way),
and its M7 part (#233): a certification lists the intervention types the floor covers.

``test_e_12`` plays the ``e12`` suite over every intervention scenario
(``evals/sets/scenarios``) on the stub, two seeds, after calibrating the stub's judge on the
labeled pairs. Every scripted turn triggers exactly the intervention its scenario names: the
pairs per configuration and type, and the first-draft preference rate the scripted judge gives,
are what the set predicts. Every type stays under 300 non-tied pairs, so each row is
``insufficient_evidence`` (never a pass), with its Wilson interval, and the report lists no type
as covered.

Complements asserted:
- the relative rule: 299 non-tied pairs per seed is ``insufficient_evidence`` even with no draft
  preferred; at 300 a mean + SD at most 55 % passes (exactly 55 % included) and above it fails;
- an uncalibrated judge (the stub answers 30 % of the calibration pairs wrongly: 70 % agreement)
  makes every judge row ``report_only``, and the run with it;
- a run with no stored calibration for its judge model fails before any case.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Verdict, run
from epistrel.evals.certify import floors_of, record_from
from epistrel.evals.gate import RELATIVE_MIN_PAIRS, Direction, Metric, Rule, SeedTally, judge
from epistrel.evals.judging import CALIBRATION_FLOOR, load_pairs
from epistrel.evals.report import (
    Host,
    JudgeCalibrationRef,
    MetricResult,
    Report,
    SuiteResult,
    render_markdown,
)
from epistrel.evals.runner import CalibrationMissing
from epistrel.evals.suites import Outcome, e12_direct, scenario_play
from epistrel.evals.suites.e12_direct import (
    DIRECT_COMPLETED,
    E12_DIRECT,
    IDENTITY_TYPES,
    JUDGED_TYPES,
    DirectCase,
    direct_metric_id,
    direct_outcome,
    kept_id,
    kept_outcome,
    mismatch_id,
    not_triggered_id,
    object_blind,
    same_words,
    select_mode,
)
from epistrel.evals.suites.e12_interventions import FLOOR, changed_id, metric_id
from epistrel.evals.suites.scenario_play import CONFIGS, INTERVENTION_TYPES, ScenarioCase
from tests.eval.suite_support import SEEDS, SETS, by_id
from tests.evals.support import stub_engine

pytestmark = pytest.mark.integration


def _expected(cases: list[ScenarioCase]) -> dict[str, tuple[int, int]]:
    """Per row, the set's (non-tied pairs, draft preferred) per seed."""
    found: dict[str, list[int]] = {}
    for case in cases:
        for turn in case.turns:
            for kind in turn.expected:
                tally = found.setdefault(metric_id(case.config, kind), [0, 0])
                if turn.stub_judge != "tie":
                    tally[0] += 1
                    tally[1] += turn.stub_judge == "draft"
    return {k: (n, draft) for k, (n, draft) in found.items()}


def _pairs_flipped(share: float) -> set[str]:
    pairs = load_pairs(SETS)
    return {p.id for i, p in enumerate(pairs) if i % 10 < round(share * 10)}


@pytest.mark.part("M4", "NFR-QUAL-8")
@pytest.mark.part("M11", "NFR-QUAL-14")
async def test_e_12(pg_url: str, engine: AsyncEngine, tmp_path: Path) -> None:
    suite = SUITES["e12"]
    cases = [c for c in suite.load(SETS) if isinstance(c, ScenarioCase)]
    families = Counter(c.family for c in cases)
    assert set(families) == {
        "rung-1",
        "rung-2",
        "rung-3",
        "rung-4",
        "polished",
        "fallback",
        "plain",
    }
    calibration = tmp_path / "judge-calibration.json"
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            calibrate=True,
            calibration_file=calibration,
        )
    assert report.errors == [], report.errors
    (result,) = report.suites
    assert result.errors == [], result.errors[:5]
    assert result.calibration is not None
    assert result.calibration.status == "calibrated"
    assert result.calibration.agreement == 1.0
    assert result.calibration.pairs >= 200

    rows = by_id(result)
    expected = _expected(cases)
    for config in CONFIGS:
        for kind in INTERVENTION_TYPES:
            row = rows[metric_id(config, kind)]
            n, draft = expected.get(metric_id(config, kind), (0, 0))
            assert row.sample_size == n, (row.id, row.sample_size, n)
            assert row.seed_count == len(SEEDS)
            assert row.judge_based
            # Under 300 non-tied pairs a type is never passed.
            assert row.verdict is Verdict.insufficient_evidence, row
            if n:
                assert row.mean == pytest.approx(draft / n), row.id
                assert row.ci95 is not None
                assert row.mean is not None
                assert row.ci95[0] <= row.mean <= row.ci95[1]
        intervened = sum(len(t.expected) > 0 for c in cases if c.config == config for t in c.turns)
        turns = sum(len(c.turns) for c in cases if c.config == config)
        assert rows[changed_id(config)].mean == pytest.approx(intervened / turns)
    # Every repair rung and both composition outcomes produced pairs on the stub.
    for kind in INTERVENTION_TYPES[:-1]:
        assert any(rows[metric_id(c, kind)].sample_size for c in CONFIGS), kind
    assert result.verdict is Verdict.insufficient_evidence
    assert any(
        "types the floor covers on this run" in n and n.endswith("none.") for n in result.notes
    )
    assert any(n.startswith("E-12 fingerprint `gated`") for n in result.notes)

    # Complement: the relative rule never passes on fewer than 300 pairs a seed.
    metric = Metric("nfr_qual_14.x.y", Rule.relative, FLOOR, Direction.at_most)
    few = [SeedTally(s, RELATIVE_MIN_PAIRS - 1, 0) for s in SEEDS]
    assert judge(metric, few).verdict is Verdict.insufficient_evidence
    clean = [SeedTally(s, RELATIVE_MIN_PAIRS, 0) for s in SEEDS]
    assert judge(metric, clean).verdict is Verdict.passed
    at_floor = [SeedTally(s, 135, 165) for s in SEEDS]  # 55 % draft preferred, SD 0
    assert judge(metric, at_floor).verdict is Verdict.passed
    above = [SeedTally(SEEDS[0], 140, 160), SeedTally(SEEDS[1], 130, 170)]
    assert judge(metric, above).verdict is Verdict.failed

    # Complement: an uncalibrated judge makes every judge row report-only.
    flipped = _pairs_flipped(0.3)
    async with stub_engine(pg_url, engine) as lib:
        weak = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            calibrate=True,
            calibration_file=calibration,
            calibration_flip=flipped,
            cases={suite.name: cases[:4]},
        )
    (weak_result,) = weak.suites
    assert weak_result.calibration is not None
    assert weak_result.calibration.status == "uncalibrated"
    assert weak_result.calibration.agreement is not None
    assert weak_result.calibration.agreement < CALIBRATION_FLOOR
    assert all(m.verdict is Verdict.report_only for m in weak_result.metrics if m.judge_based)
    assert weak_result.verdict is Verdict.report_only
    assert weak.verdict is Verdict.report_only

    # Complement: no stored calibration for the judge model fails before any case.
    async with stub_engine(pg_url, engine) as lib:
        calls = len(lib.models.stub.calls)
        with pytest.raises(CalibrationMissing):
            await run(
                suite,
                AdapterConfig(),
                SEEDS,
                engine=lib,
                sets_dir=SETS,
                calibration_file=tmp_path / "none.json",
            )
        assert len(lib.models.stub.calls) == calls


def _e12_report(calibration: str, tallies: dict[str, list[SeedTally]]) -> Report:
    """A run whose E-12 direct rows (#937: what a certification reads) observed ``tallies`` per
    type, beside a full-mode ``e12`` row that passes (informational in certification)."""
    by_id_ = {m.id: m for m in SUITES[E12_DIRECT].metrics}
    rows = [
        MetricResult.of(metric, judge(metric, found))
        for kind, found in tallies.items()
        for metric in [by_id_[direct_metric_id(kind)]]
    ]
    for row in rows:
        row.judge_based = row.id in SUITES[E12_DIRECT].judge_metrics
    full = Metric(metric_id("structured", "repair_rung_1"), Rule.relative, FLOOR, Direction.at_most)
    ref = JudgeCalibrationRef(status=calibration, judge_model="stub", pairs=200)
    suites = [
        SuiteResult(
            name=E12_DIRECT,
            title="E-12 direct",
            ci_gate=False,
            cases=len(tallies),
            seeds=list(SEEDS),
            metrics=rows,
            verdict=Verdict.insufficient_evidence,
            calibration=ref,
        ),
        SuiteResult(
            name="e12",
            title="E-12",
            ci_gate=False,
            cases=1,
            seeds=list(SEEDS),
            metrics=[MetricResult.of(full, judge(full, [SeedTally(s, 300, 0) for s in SEEDS]))],
            verdict=Verdict.passed,
            calibration=ref,
        ),
    ]
    return Report(
        run_id="e12-m7",
        started_at="2026-11-02T10:00:00+00:00",
        model="stub",
        fingerprint="sha256:" + "c" * 64,
        fingerprint_profile="Full v4",
        adapter="stub",
        engine_sha="0" * 7,
        host=Host(kind="ci"),
        seed_base=SEEDS[0],
        seeds=list(SEEDS),
        suites=suites,
    )


def _all_seven(**changed: list[SeedTally]) -> dict[str, list[SeedTally]]:
    """Every type passing (a judged type at 300 non-tied pairs a seed under 55 % draft
    preference, an identity type at 30 unchanged cases a seed), with ``changed`` over it."""
    found = {
        kind: [SeedTally(s, RELATIVE_MIN_PAIRS, 90) for s in SEEDS]
        if kind in JUDGED_TYPES
        else [SeedTally(s, 30, 0) for s in SEEDS]
        for kind in (*JUDGED_TYPES, *sorted(IDENTITY_TYPES))
    }
    return {**found, **changed}


def _record(report: Report) -> Any:
    return record_from(
        report,
        "# E-12\n",
        fingerprint=report.fingerprint,
        profile_id=None,
        profile_version=None,
        models={},
    )


@pytest.mark.part("M7", "FR-PROF-15")
def test_e_12__m7() -> None:
    """A certification lists the intervention types NFR-QUAL-14's floor covers (E-12 M7, read
    from the direct rows since #937): with a calibrated judge and all seven types passing,
    NFR-QUAL-14 is a floor passed, every type is covered and the identity types are listed.
    Complements: six types passing and one at 120 pairs makes NFR-QUAL-14 insufficient (the
    rule before #937 passed it on one type); a failing type is its own floor failed; the
    full-mode rows never count; and with an uncalibrated judge the judged types are in neither
    list, NFR-QUAL-14 is not passed and the record says ``judge_calibrated: false``."""
    record = _record(_e12_report("calibrated", _all_seven()))
    assert record is not None
    assert record.schema_version == 2
    assert sorted(record.intervention_types_covered) == sorted(INTERVENTION_TYPES)
    assert record.intervention_types_insufficient == []
    assert sorted(record.identity_types) == sorted(IDENTITY_TYPES)
    assert record.judge_calibrated is True
    assert [f.metric_id for f in record.floors_passed] == ["NFR-QUAL-14"]

    six = _e12_report(
        "calibrated", _all_seven(repair_rung_3=[SeedTally(s, 120, 30) for s in SEEDS])
    )
    passed, failed, insufficient, covered, lacking, _ = floors_of(six)
    assert passed == []
    assert failed == []
    assert [f.metric_id for f in insufficient] == ["NFR-QUAL-14"]
    assert lacking == ["repair_rung_3"]
    assert "repair_rung_3" not in covered
    assert len(covered) == 6
    assert _record(six) is None  # no floor passed: no record

    worse = _e12_report(
        "calibrated", _all_seven(repair_rung_4=[SeedTally(s, 100, 200) for s in SEEDS])
    )
    passed, failed, insufficient, _, _, _ = floors_of(worse)
    assert passed == []
    assert insufficient == []
    assert [(f.metric_id, f.intervention_type) for f in failed] == [
        ("NFR-QUAL-14", "repair_rung_4")
    ]

    weak = _e12_report("uncalibrated", _all_seven())
    for row in weak.suites[0].metrics:
        if row.judge_based:
            row.verdict = Verdict.report_only  # what an uncalibrated judge makes of judge rows
    assert _record(weak) is None
    passed, _, _, covered, lacking, calibrated = floors_of(weak)
    assert (passed, lacking, calibrated) == ([], [], False)
    assert sorted(covered) == sorted(IDENTITY_TYPES)  # identity rows make no judge call


# -- direct mode (#936) -------------------------------------------------------------------------

_HOOKED = (
    '{"id": "direct-test-unchanged", "type": "repair_rung_1", "say": "Where is your coat?", '
    '"beats": [{"kind": "say", "text": "On its hook, boss.", "intent": "sincere"}, {"kind": '
    '"do", "text": "Bobby nods at the hallway.", "propositions": [{"subject": "coat", '
    '"relation": "located_at", "object": "hook"}]}], "stub": {"judge": "draft"}}'
)


# One say beat, nothing else scripted: age_repair alone fires (#938).
_BROKEN = (
    '{"id": "direct-test-broken", "type": "age_repair", "say": "Who knocked?", "beats": '
    '[{"kind": "say", "text": "A courier, boss.", "intent": "sincere"}], "introduces": [{"name": '
    '"a courier", "age_range": [20, 60]}]}'
)


@pytest.mark.part("M11", "NFR-QUAL-14")
async def test_e_12__direct_mode(
    pg_url: str, engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#936, #937: the direct suite plays every example case once through the real turn
    pipeline (only the render pinned to the prepared draft). Every judged type yields pairs,
    the scripted judge's sides are the rows' rates, a tie adds no pair, and each row is
    ``insufficient_evidence`` under 300 pairs. Each identity type fired on five cases, every one
    released unchanged: its row counts them and is ``insufficient_evidence`` below 50.
    Complements: a draft that contradicts nothing is ``not_triggered`` and adds no pair, and
    so is an introduction that gave its age; an identity case that fired alone and whose
    released text is forced to differ from its draft is a harness error naming the case (a
    change another intervention made beside it is a mismatch, #938)."""
    suite = SUITES[E12_DIRECT]
    cases = [c for c in suite.load(SETS) if isinstance(c, DirectCase)]
    by_type = Counter(c.type for c in cases)
    assert set(by_type) == set(INTERVENTION_TYPES)
    assert min(by_type.values()) >= 5
    unchanged = DirectCase.model_validate_json(_HOOKED)
    calibration = tmp_path / "judge-calibration.json"
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite,
            AdapterConfig(),
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            calibrate=True,
            calibration_file=calibration,
            cases={suite.name: [*cases, unchanged]},
        )
    assert report.errors == [], report.errors
    (result,) = report.suites
    assert result.errors == [], result.errors[:5]
    rows = by_id(result)
    played = [*cases, unchanged]
    for kind in INTERVENTION_TYPES:
        mine = [c for c in played if c.type == kind]
        quiet = [c for c in mine if c.id.endswith("-0006") or c is unchanged]
        fired = [c for c in mine if c not in quiet]
        row = rows[direct_metric_id(kind)]
        assert row.verdict is Verdict.insufficient_evidence, row
        assert rows[mismatch_id(kind)].mean == 0, kind
        assert rows[not_triggered_id(kind)].mean == pytest.approx(len(quiet) / len(mine)), kind
        if kind in IDENTITY_TYPES:
            assert row.rule is Rule.identity
            assert not row.judge_based
            assert row.note == f"identity ({len(fired) * len(SEEDS)} cases)", row.note
            assert row.ci95 is None
            continue
        judged = [c for c in fired if c.stub is None or c.stub.judge != "tie"]
        draft = sum(c.stub is not None and c.stub.judge == "draft" for c in judged)
        assert row.sample_size == len(judged), (kind, row.sample_size)
        assert row.mean == pytest.approx(draft / len(judged)), kind
        assert row.judge_based
    assert rows[DIRECT_COMPLETED].mean == 1
    assert any("E-12 direct fingerprint `structured`" in n for n in result.notes)
    assert any("enough evidence on this run" in n and n.endswith("none.") for n in result.notes)

    # Complement: an identity intervention that changed the prose fails loudly. On the stub
    # pipeline only another intervention changes the text (a mismatch, #938), so the draft the
    # turn report gives is altered: age_repair fired alone, and the release differs from it.
    broken = DirectCase.model_validate_json(_BROKEN)
    real = scenario_play.first_draft

    async def altered(*args: Any, **kwargs: Any) -> Any:
        draft, *rest = await real(*args, **kwargs)
        return (f"{draft} Altered.", *rest)

    monkeypatch.setattr(e12_direct, "first_draft", altered)
    async with stub_engine(pg_url, engine) as lib:
        bad = await run(
            suite,
            AdapterConfig(),
            SEEDS[:1],
            engine=lib,
            sets_dir=SETS,
            calibration_file=calibration,
            cases={suite.name: [broken]},
        )
    (bad_result,) = bad.suites
    assert any("direct-test-broken" in e and "IdentityBroken" in e for e in bad_result.errors), (
        bad_result.errors
    )
    assert bad_result.verdict in (Verdict.failed, Verdict.error), bad_result.verdict


async def test_e_12__direct_mode_calls_the_production_ladder(
    pg_url: str, engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#936: the harness runs the pipeline's own repair entry point, not a copy: with
    ``pipeline.repair_turn`` made to raise, the direct case fails."""
    from epistrel.orchestrator.structured import pipeline

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("repair_turn reached")

    monkeypatch.setattr(pipeline, "repair_turn", broken)
    suite = SUITES[E12_DIRECT]
    first = next(c for c in suite.load(SETS) if isinstance(c, DirectCase))
    async with stub_engine(pg_url, engine) as lib:
        report = await run(
            suite,
            AdapterConfig(),
            SEEDS[:1],
            engine=lib,
            sets_dir=SETS,
            calibrate=True,
            calibration_file=tmp_path / "judge-calibration.json",
            cases={suite.name: [first]},
        )
    (result,) = report.suites
    assert any("repair_turn reached" in e for e in result.errors), result.errors


def test_e_12__direct_verdict_rule() -> None:
    """#936: a direct row is judged by the relative rule: 299 non-tied pairs a seed is
    ``insufficient_evidence`` whatever the rate; at 300, mean + SD = 56 % fails."""
    (metric,) = (m for m in SUITES[E12_DIRECT].metrics if m.id == direct_metric_id("repair_rung_3"))
    assert metric.rule is Rule.relative
    assert metric.floor == FLOOR
    assert judge(metric, [SeedTally(s, 299, 0) for s in SEEDS]).verdict is (
        Verdict.insufficient_evidence
    )
    assert judge(metric, [SeedTally(s, 0, 299) for s in SEEDS]).verdict is (
        Verdict.insufficient_evidence
    )
    assert judge(metric, [SeedTally(s, 132, 168) for s in SEEDS]).verdict is Verdict.failed
    assert judge(metric, [SeedTally(s, 135, 165) for s in SEEDS]).verdict is Verdict.passed


def test_e_12__direct_outcome_and_mode() -> None:
    """#936: how a played direct case counts, and which suites ``--e12-mode`` selects."""
    assert direct_outcome("repair_rung_1", (), changed=False) == "not_triggered"
    assert direct_outcome("repair_rung_1", ("repair_rung_1",), changed=False) == "not_triggered"
    assert direct_outcome("repair_rung_1", ("repair_rung_1", "age_repair"), changed=True) == "pair"
    assert direct_outcome("repair_rung_2", ("repair_rung_1",), changed=True) == "mismatch"
    # #938: an identity type's case counts its own change only.
    assert direct_outcome("age_repair", ("age_repair",), changed=False) == "identity"
    assert direct_outcome("age_repair", ("age_repair",), changed=True) == "broken"
    assert direct_outcome("age_repair", ("repair_rung_3", "age_repair"), changed=True) == "mismatch"
    assert direct_outcome("age_repair", ("repair_rung_3",), changed=True) == "mismatch"
    assert direct_outcome("composition_fallback", (), changed=False) == "not_triggered"
    # ADR-197: rung 2 is an identity type compared under object_blind (a substitution only).
    assert "repair_rung_2" in IDENTITY_TYPES
    assert object_blind("Bobby drops the coat on an armchair.") == object_blind(
        "Bobby drops the coat on a hook."
    )
    assert object_blind("Bobby drops the coat on a hook.") != object_blind(
        "Bobby drops it on a hook."
    )
    every = ["e01", "e12", "e12_direct", "q01"]
    assert select_mode(every, None, default="full") == ["e01", "e12", "q01"]
    assert select_mode(every, "direct", default="full") == ["e01", "e12_direct", "q01"]
    assert select_mode(["e12"], None, default="both") == ["e12_direct", "e12"]
    assert select_mode(["e12_direct"], None, default="full") == ["e12_direct"]
    with pytest.raises(ValueError, match="needs e12"):
        select_mode(["q01"], "direct", default="full")


def _polished(seed: int, counted: str) -> Outcome:
    detail = {"seed": seed, "type": "composition_polished", "counted": counted}
    if counted == "pair":
        detail["preferred"] = "final"
    return Outcome({}, detail=detail)


def test_e_12__a_words_kept_polish_is_identity() -> None:
    """#938, ADR-198 (the owner's option (a)): a polish that ran and kept the draft's words is
    ``kept``, never judged; the type's row passes as identity on at least 50 polishes over two
    seeds with under 5 % rewording, and is otherwise the relative rule over the rewording ones."""
    assert same_words('*Bobby folds his arms.*\n\n"Mm."', 'Bobby folds his arms. "Mm."')
    assert not same_words("Bobby folds his arms.", "Bobby crosses his arms.")
    polished = ("composition_polished",)
    # A verbatim polish records no type (its text equals the assembly), yet it ran: kept.
    assert kept_outcome("composition_polished", (), False, polished=True, kept=True) == "kept"
    assert kept_outcome("composition_polished", polished, True, polished=True, kept=True) == "kept"
    assert kept_outcome("composition_polished", polished, True, polished=True, kept=False) == "pair"
    # Something else fired: the words-kept rule does not apply.
    both = ("repair_rung_3", "composition_polished")
    assert kept_outcome("composition_polished", both, True, polished=True, kept=True) == "pair"
    assert (
        kept_outcome("composition_polished", ("repair_rung_3",), True, polished=True, kept=True)
        == "mismatch"
    )
    assert (
        kept_outcome("composition_polished", (), False, polished=False, kept=True)
        == "not_triggered"
    )
    suite = SUITES[E12_DIRECT]
    row = direct_metric_id("composition_polished")
    (kept_metric,) = (m for m in suite.metrics if m.id == kept_id("composition_polished"))
    assert kept_metric.rule is Rule.informational

    def summary(kept: int, reworded: int, seeds: tuple[int, ...] = (1, 2)) -> dict[str, Any]:
        outcomes = [
            _polished(seed, "kept" if i < kept else "pair")
            for seed in seeds
            for i in range(kept + reworded)
        ]
        totals = suite.aggregate(outcomes)
        assert totals[kept_id("composition_polished")].good == kept * len(seeds)  # type: ignore[union-attr]
        return dict(suite.summarize(outcomes, "m").metrics.get(row, {}))

    passed = summary(25, 1)  # 50 kept of 52 polishes, 3.8 % reworded
    assert passed["verdict"] is Verdict.passed
    assert passed["note"] == "identity: words kept in 50 of 52 polishes"
    assert "verdict" not in summary(24, 2)  # 7.7 % reworded: the relative rule judges
    assert "verdict" not in summary(24, 0)  # 48 polishes: short of 50
    assert "verdict" not in summary(50, 0, seeds=(1,))  # one seed
    assert summary(24, 0)["note"] == "words kept in 48 of 48 polishes"


def test_e_12__one_section() -> None:
    """#937: the report shows E-12 once, the direct rows first and the full-turn rows beside
    them; an identity row reads "identity (n cases)"."""
    report = _e12_report("calibrated", _all_seven())
    report.suites.reverse()  # whatever order the suites ran in
    text = render_markdown(report)
    assert text.count("## E-12: engine interventions and prose (NFR-QUAL-14)") == 1
    assert text.index("### `e12_direct`") < text.index("### `e12`:")
    assert "(the realism check: informational in a certification)" in text
    assert "identity (60 cases)" in text
