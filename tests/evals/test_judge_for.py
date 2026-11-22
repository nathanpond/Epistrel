"""Per-purpose judges, the judge-trust gate and the offline re-judge (#747; ADR-204).

``--judge-model-for entailment=<model>`` sends E-03's entailment judgements alone to a judge of
their own; every engine call and every other judge purpose keeps its model. Such a judge does
not enter the configuration fingerprint (a deployment never has one), so the results journal
files E-03's lines under it instead, and the report and the certification record name it.
``evals rejudge`` judges a journal's E-03 reflections again, offline. Stub only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.memory.records import render_line
from epistrel.core.profiles.certification import SCHEMA_VERSION, CertificationRecord
from epistrel.evals import SUITES, AdapterConfig, Rule, Verdict, run, seeds_from
from epistrel.evals import runner as runners
from epistrel.evals.cli import _config, judge_overrides, parser
from epistrel.evals.gate import Direction
from epistrel.evals.judge_model import JudgeEndpoint, JudgeModels
from epistrel.evals.rejudge import (
    CAUSE_CONSOLIDATION,
    CAUSE_JUDGE,
    cause_of,
    read_lines,
    rejudge,
    sources_of,
)
from epistrel.evals.report import MetricResult, render_markdown
from epistrel.evals.runner import journal_scope, run_fingerprint, trusted
from epistrel.evals.suites.e03_consolidation import (
    FAITHFULNESS,
    JUDGE_AGREEMENT,
    ConsolidationCase,
)
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import PROBE_MESSAGES
from epistrel.orchestrator.models.registry import ENTAILMENT, QUALITY_JUDGE
from epistrel.orchestrator.models.stub import STUB_MODEL
from tests.eval.suite_support import SETS
from tests.evals.support import stub_engine

SEEDS = seeds_from(2)
SONNET = "stub-entailment-judge"
BASE = ["run", "--suite", "e03", "--adapter", "stub"]


def _args(*extra: str) -> Any:
    return parser().parse_args([*BASE, *extra])


# -- the CLI and the configuration, without a database ----------------------------------------


def test_judge_model_for_names_one_purpose_s_judge() -> None:
    """``--judge-model-for entailment=M`` gives ``entailment`` its own judge, with
    ``--judge-base-url-for`` and a key read from ``--judge-api-key-env-for``'s variable;
    ``quality_judge`` stays the run model's, so the fingerprint is the plain one.

    Complement asserted: ``--judge-model`` still moves every judge purpose, and a purpose's own
    judge wins over it for that purpose."""
    args = _args(
        "--judge-model-for",
        f"entailment={SONNET}",
        "--judge-base-url-for",
        "entailment=https://judge.example/v1/",
        "--judge-api-key-env-for",
        "entailment=EPISTREL_TEST_JUDGE_KEY",
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("EPISTREL_TEST_JUDGE_KEY", "sk-judge")
        config = _config(args, "stub")
    own = config.judge_endpoint_for(ENTAILMENT.name)
    assert own == JudgeEndpoint(SONNET, "https://judge.example/v1", "sk-judge")
    assert config.judge_endpoint_for(QUALITY_JUDGE.name) is None
    assert (config.judge_name_for(ENTAILMENT.name), config.judge_name) == (SONNET, STUB_MODEL)
    assert config.self_judged
    assert not config.self_judged_for(ENTAILMENT.name)
    assert config.quality_judge_model is None
    assert set(config.judges) == {ENTAILMENT.name}

    both = _config(_args("--judge-model", "j", "--judge-model-for", f"entailment={SONNET}"), "stub")
    assert both.judge_name_for(ENTAILMENT.name) == SONNET
    assert both.judge_name_for(QUALITY_JUDGE.name) == "j"
    assert both.quality_judge_model == "j"
    every = _config(_args("--judge-model", "j"), "stub")
    assert {p: e.model for p, e in every.judges.items()} == {
        ENTAILMENT.name: "j",
        QUALITY_JUDGE.name: "j",
    }


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        (["--judge-base-url-for", "entailment=http://x/v1"], "needs --judge-model-for"),
        (["--judge-model-for", "entailment=a", "--judge-model-for", "entailment=b"], "twice"),
        (
            ["--judge-model-for", "entailment=a", "--judge-api-key-env-for", "entailment=NOPE_X"],
            "is not set",
        ),
    ],
)
def test_judge_flags_refuse_an_incomplete_or_doubled_purpose(
    extra: list[str], error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint with no model, a purpose named twice, or an unset key variable is refused
    before anything runs. Complement: the valid flags parse (the test above)."""
    monkeypatch.delenv("NOPE_X", raising=False)
    with pytest.raises(ValueError, match=error):
        judge_overrides(_args(*extra))


def test_an_unknown_judge_purpose_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Only ``entailment`` and ``quality_judge`` take a judge of their own."""
    with pytest.raises(SystemExit):
        _args("--judge-model-for", "generation=m")
    assert "unknown judge purpose 'generation'" in capsys.readouterr().err


def test_journal_scope_names_a_judge_the_fingerprint_does_not() -> None:
    """E-03's journal lines are filed under its entailment judge when that judge is not in the
    fingerprint, so a resumed run never reuses another judge's verdicts. Complement: E-03
    self-judged, and a suite with no entailment judging, keep the plain scope."""
    e03, e12 = SUITES["e03"], SUITES["e12"]
    judged = AdapterConfig(judge_for=((ENTAILMENT.name, JudgeEndpoint(SONNET)),))
    assert journal_scope(e03, judged, None) == f"#judge:entailment={SONNET}"
    assert journal_scope(e03, judged, "sha256:ab") == f"sha256:ab#judge:entailment={SONNET}"
    assert journal_scope(e03, AdapterConfig(), None) is None
    assert journal_scope(e12, judged, None) is None
    assert run_fingerprint(STUB_MODEL) == run_fingerprint(
        STUB_MODEL, judge=judged.quality_judge_model
    )


def _row(metric_id: str, rule: Rule, mean: float, verdict: Verdict) -> MetricResult:
    return MetricResult(
        id=metric_id,
        rule=rule,
        direction=Direction.at_least,
        floor=0.98 if rule is not Rule.informational else None,
        mean=mean,
        stdev=0.0,
        sample_size=300,
        seed_count=3,
        per_seed=[mean] * 3,
        verdict=verdict,
    )


def test_trusted_keeps_or_demotes_the_floored_rows() -> None:
    """ADR-204: at 75% agreement the floored rows keep their verdicts; at 74.9% each is report
    only with a note, and the informational rows are untouched."""
    rows = [
        _row(FAITHFULNESS, Rule.rate, 0.99, Verdict.passed),
        _row(JUDGE_AGREEMENT, Rule.informational, 0.75, Verdict.report_only),
    ]
    assert trusted(rows, JUDGE_AGREEMENT, 0.75) == rows
    rows[1] = rows[1].model_copy(update={"mean": 0.749})
    found = {m.id: m for m in trusted(rows, JUDGE_AGREEMENT, 0.75)}
    assert found[FAITHFULNESS].verdict is Verdict.report_only
    assert "74.90%" in (found[FAITHFULNESS].note or "")
    assert found[JUDGE_AGREEMENT] == rows[1]
    missing = {m.id: m for m in trusted(rows[:1], JUDGE_AGREEMENT, 0.75)}
    assert "was not measured" in (missing[FAITHFULNESS].note or "")


def test_a_record_names_its_judges_and_is_version_3() -> None:
    """The certification record keeps the judges (record only, never stored)."""
    assert SCHEMA_VERSION == 3
    assert "judges" in CertificationRecord.model_fields


# -- routing and the re-judge, on the stub ----------------------------------------------------


@pytest.fixture
def judge_layers(monkeypatch: pytest.MonkeyPatch) -> list[JudgeModels]:
    built: list[JudgeModels] = []

    class Recorded(JudgeModels):
        def __init__(self, base: ModelAccessLayer, judge: JudgeEndpoint) -> None:
            super().__init__(base, judge)
            built.append(self)

    monkeypatch.setattr(runners, "JudgeModels", Recorded)
    return built


@pytest.mark.integration
async def test_e03_entailment_goes_to_its_own_judge(
    pg_url: str, engine: AsyncEngine, judge_layers: list[JudgeModels]
) -> None:
    """With ``entailment``'s own judge, every E-03 judge call goes to it and the run model's
    adapter gets zero entailment calls; consolidation stays on the run model. The report names
    the judge for ``entailment`` (``Report.judges``, its own line) while ``quality_judge`` stays
    self-judged, the suite's Judge names it, and the fingerprint is the plain one."""
    config = AdapterConfig(judge_for=((ENTAILMENT.name, JudgeEndpoint(SONNET)),))
    async with stub_engine(pg_url, engine) as lib:
        report = await run(SUITES["e03"], config, SEEDS, engine=lib, sets_dir=SETS, sample=4)
        run_calls = list(lib.models.stub.calls)
    (layer,) = judge_layers
    judge_calls = [c for c in layer.stub.calls if c.messages != PROBE_MESSAGES]
    assert judge_calls
    assert {c.purpose for c in judge_calls} == {ENTAILMENT.name}
    assert {c.model for c in layer.stub.calls} == {SONNET}
    assert [c for c in run_calls if c.purpose == ENTAILMENT.name] == []
    assert "consolidation" in {c.purpose for c in run_calls}
    (result,) = report.suites
    assert result.errors == []
    assert result.judge is not None
    assert (result.judge.model, result.judge.self_judged) == (SONNET, False)
    metrics = {m.id: m for m in result.metrics}
    assert (metrics[FAITHFULNESS].verdict, metrics[JUDGE_AGREEMENT].mean) == (Verdict.passed, 1.0)
    assert report.judge is None
    assert set(report.judges) == {ENTAILMENT.name}
    assert report.judges[ENTAILMENT.name].model == SONNET
    assert report.fingerprint == run_fingerprint(STUB_MODEL)
    markdown = render_markdown(report)
    assert "- **Judge:** the run model (self-judged)" in markdown
    assert f"- **Judge for `entailment`:** `{SONNET}`" in markdown


def _journal_line(case: ConsolidationCase, seed: int, reflection: str, entailed: bool) -> str:
    return json.dumps(
        {
            "suite": "e03",
            "case": case.id,
            "seed": seed,
            "output": json.dumps([reflection]),
            "detail": {"entailed": [entailed]},
            "observations": {FAITHFULNESS: entailed},
        }
    )


def test_read_lines_and_the_cause_rule(tmp_path: Path) -> None:
    """The journal's E-03 lines are read (a model-error line and a torn line skipped, another
    suite's ignored); a window's sources are its episodes as the turn commit wrote them; a
    failing window is ``consolidation`` only when the candidate also refuses a reflection and
    judged both labeled pairs right."""
    (case, other) = SUITES["e03"].load(SETS)[:2]
    assert isinstance(case, ConsolidationCase)
    path = tmp_path / "j.jsonl"
    path.write_text(
        "\n".join(
            [
                _journal_line(case, 1, "a", True),
                json.dumps(
                    {"suite": "e03", "case": other.id, "seed": 1, "detail": {"model_error": "x"}}
                ),
                json.dumps({"suite": "e05", "case": "x", "seed": 1}),
                '{"suite": "e03", "torn',
            ]
        )
    )
    (line,) = read_lines(path)
    assert (line.case, line.reflections, line.faithful) == (case.id, ("a",), True)
    assert sources_of(case)[0] == render_line(
        "do", case.inputs.episodes[0].by, case.inputs.episodes[0].text
    )
    assert cause_of([False], [True, True]) == CAUSE_CONSOLIDATION
    assert cause_of([True], [True, True]) == CAUSE_JUDGE
    assert cause_of([False], [True, False]) == CAUSE_JUDGE


@pytest.mark.integration
async def test_rejudge_re_judges_a_journal_and_names_each_failing_window_s_cause(
    pg_url: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Two windows the journal's judge failed: one whose reflection is the labeled entailed one
    (the candidate entails it: cause ``judge``) and one whose reflection is the non-entailed one
    (the candidate refuses it, having judged both pairs right: ``consolidation``). The candidate's
    faithfulness and per-pair agreement come back per seed.

    Complement asserted: a window the journal's judge passed gets no cause."""
    suite = SUITES["e03"]
    cases = [c for c in suite.load(SETS)[:3] if isinstance(c, ConsolidationCase)]
    first, second, third = cases
    path = tmp_path / "full-x.jsonl"
    path.write_text(
        "\n".join(
            [
                _journal_line(first, 1, first.expected.entailed, False),
                _journal_line(second, 1, second.expected.non_entailed, False),
                _journal_line(third, 1, third.expected.entailed, True),
                _journal_line(first, 2, first.expected.entailed, True),
            ]
        )
    )
    async with stub_engine(pg_url, engine) as lib:
        for case in cases:
            suite.script_stub(case, lib.models.stub)
        found = await rejudge(
            lib.models, path, {c.id: c for c in cases}, judge_model=STUB_MODEL, concurrency=2
        )
    by_window = {(w.case, w.seed): w for w in found.windows}
    assert by_window[(first.id, 1)].cause == CAUSE_JUDGE
    assert by_window[(second.id, 1)].cause == CAUSE_CONSOLIDATION
    assert by_window[(third.id, 1)].cause is None
    assert by_window[(first.id, 2)].cause is None
    assert found.causes == {CAUSE_JUDGE: 1, CAUSE_CONSOLIDATION: 1}
    assert found.cause == CAUSE_JUDGE  # a tie is the judge's
    assert found.agreement is not None
    assert found.agreement.mean == 1.0
    assert found.faithfulness is not None
    assert found.faithfulness.per_seed == (2 / 3, 1.0)
    assert found.trusted
    dumped = found.as_json()
    assert {f["case"] for f in dumped["failing"]} == {first.id, second.id}


@pytest.mark.integration
async def test_the_rejudge_command_writes_its_result(
    pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``evals rejudge --suite e03 --from-journal`` on the stub prints the candidate's figures
    and writes the JSON result. Complement: a missing journal is a usage error."""
    from epistrel.evals.cli import _rejudge

    (case,) = [c for c in SUITES["e03"].load(SETS)[:1] if isinstance(c, ConsolidationCase)]
    path = tmp_path / "full-y.jsonl"
    path.write_text(_journal_line(case, 1, case.expected.entailed, True) + "\n")
    out = tmp_path / "out.json"
    common = ["rejudge", "--adapter", "stub", "--database-url", pg_url]
    args = parser().parse_args([*common, "--from-journal", str(path), "--out", str(out)])
    assert await _rejudge(args) == 0
    written = json.loads(out.read_text())
    assert (written["suite"], written["judge"], written["causes"]) == ("e03", STUB_MODEL, {})
    assert "rejudge e03 with stub" in capsys.readouterr().out
    missing = parser().parse_args([*common, "--from-journal", str(tmp_path / "none.jsonl")])
    assert await _rejudge(missing) == 2
