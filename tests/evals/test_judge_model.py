"""The optional second judge model (#475; ``epistrel.evals.judge_model``).

The harness's judge calls go to the judge model: E-03's entailment judgement (NFR-QUAL-3) and
E-12/E-13's ``quality_judge`` (NFR-QUAL-14,15), whose #158 calibration is stored under the judge
model. Every engine call keeps the run model. The report names the judge per judged suite and
flags a self-judged one, and the fingerprint covers the judge (FR-PROF-16, NFR-QUAL-11). A
report written before #475 still parses. Stub only: the judge is a stub of its own that reports
the judge's model name, so its calls can be told from the run model's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.evals import SUITES, AdapterConfig, Report, run, run_suites, seeds_from
from epistrel.evals import check as checks
from epistrel.evals import runner as runners
from epistrel.evals.cli import _config, parser
from epistrel.evals.judge_model import JudgeEndpoint, JudgeModels
from epistrel.evals.judging import lookup
from epistrel.evals.report import (
    SUMMARY_PREFIX,
    read_summary,
    render_markdown,
    report_files,
    summary,
)
from epistrel.evals.runner import run_fingerprint
from epistrel.evals.suites.e03_consolidation import FAITHFULNESS, JUDGE_AGREEMENT
from epistrel.evals.suites.e13_signals import committed_voice_means, metric_id
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import PROBE_MESSAGES, ModelParams
from epistrel.orchestrator.models.config import EffectiveConfig
from epistrel.orchestrator.models.registry import ENTAILMENT, QUALITY_JUDGE
from epistrel.orchestrator.models.stub import STUB_MODEL
from tests.auth_support import auth_settings
from tests.eval.suite_support import SETS
from tests.evals.support import stub_engine

SEEDS = seeds_from(2)
JUDGE = "stub-judge"
JUDGE_PURPOSES = {ENTAILMENT.name, QUALITY_JUDGE.name}


# -- the fingerprint, the CLI and the judge's configuration, without a database ---------------


def test_the_fingerprint_covers_the_judge_only_when_one_is_set() -> None:
    """FR-PROF-16 lists every model purpose's identifier: a judge model enters the fingerprint
    as ``quality_judge``'s, so a judged run is a configuration of its own. Complement: with no
    judge the fingerprint is the one every earlier report was keyed by."""
    plain = run_fingerprint("m")
    assert run_fingerprint("m", judge=None) == plain
    judged = run_fingerprint("m", judge="j")
    assert judged != plain
    assert run_fingerprint("m", judge="k") not in {plain, judged}
    assert run_fingerprint("m", {"verification": {"level": "off"}}, judge="j") != judged


def test_the_cli_takes_the_judge_options() -> None:
    """``run`` and ``calibrate-judge`` take ``--judge-model``, ``--judge-base-url`` and
    ``--judge-api-key``. Complement: without them the run model judges."""
    for command in ("run", "calibrate-judge"):
        extra = ["--all"] if command == "run" else []
        args = parser().parse_args(
            [
                command,
                *extra,
                "--base-url",
                "http://localhost:1234/v1",
                "--model",
                "big",
                "--judge-model",
                "small",
                "--judge-base-url",
                "http://localhost:5678/v1/",
                "--judge-api-key",
                "k2",
            ]
        )
        config = _config(args, "openai")
        assert (config.judge_model, config.judge_base_url, config.judge_api_key) == (
            "small",
            "http://localhost:5678/v1",
            "k2",
        )
        assert (config.model_name, config.judge_name, config.self_judged) == ("big", "small", False)
        plain = _config(parser().parse_args([command, *extra, "--model", "big"]), "openai")
        assert (plain.judge_model, plain.judge_name, plain.self_judged) == (None, "big", True)
        assert plain.judge_endpoint is None


@dataclass
class _Base:
    """The parts of the run's Model Access Layer ``JudgeModels`` reads."""

    settings: Any
    db: Any = None
    asked: list[str] = field(default_factory=list)

    async def resolve(self, purpose: str) -> EffectiveConfig:
        self.asked.append(purpose)
        return EffectiveConfig(
            purpose=purpose,
            base_url="http://run/v1",
            model="run-model",
            api_key_ciphertext="run-key-ciphertext",
            params=ModelParams(timeout=120.0),
            revision=7,
        )


async def test_the_judge_layer_swaps_in_the_judge_s_model_endpoint_and_key() -> None:
    """The judge's layer resolves a purpose as the run does (params, revision), then uses the
    judge's model, URL and key. Complement: a judge with no URL or key of its own uses the
    run's."""
    settings = auth_settings(
        "postgresql+psycopg://u:p@localhost:5432/x",
        model_adapter="openai",
        config_key=Fernet.generate_key().decode(),
    )
    base = _Base(settings)
    layer = JudgeModels(base, JudgeEndpoint("judge", "http://judge/v1", "judge-key"))  # type: ignore[arg-type]
    found = await layer.resolve(ENTAILMENT.name)
    assert (found.model, found.base_url, found.revision) == ("judge", "http://judge/v1", 7)
    assert found.params.timeout == 120.0
    assert found.api_key_ciphertext is not None
    decrypted = layer.keybox.decrypt(found.api_key_ciphertext, purpose=ENTAILMENT.name)
    assert decrypted.get_secret_value() == "judge-key"
    assert base.asked == [ENTAILMENT.name]
    same = JudgeModels(base, JudgeEndpoint("judge"))  # type: ignore[arg-type]
    inherited = await same.resolve(QUALITY_JUDGE.name)
    assert (inherited.model, inherited.base_url, inherited.api_key_ciphertext) == (
        "judge",
        "http://run/v1",
        "run-key-ciphertext",
    )
    await layer.aclose()
    await same.aclose()


# -- reports ------------------------------------------------------------------------------------


def _old_report_json() -> dict[str, Any]:
    """A run's JSON as #474's harness wrote it: no ``judge`` anywhere."""
    return {
        "version": 1,
        "run_id": "abc-2026-10-19",
        "started_at": "2026-10-19T00:00:00+00:00",
        "finished_at": "2026-10-19T01:00:00+00:00",
        "model": "cydonia",
        "model_version": "cydonia",
        "fingerprint": "sha256:" + "0" * 64,
        "fingerprint_profile": "Structured (M4) v1",
        "adapter": "openai",
        "engine_sha": "abc",
        "host": {"kind": "local"},
        "seed_base": 1,
        "seeds": [1, 2],
        "suites": [
            {
                "name": "e03",
                "title": "E-03",
                "ci_gate": True,
                "cases": 1,
                "seeds": [1, 2],
                "verdict": "pass",
            }
        ],
        "sample_size": 1,
        "seed_count": 2,
    }


def test_a_report_written_before_475_still_parses() -> None:
    """An older JSON has no judge fields: it parses with none, its Markdown reads as
    self-judged, and its summary has no suite judges. Every committed report still carries a
    summary the index is built from (the older ones with no judge), and ``--check`` still agrees
    with the index."""
    old = Report.from_json(_old_report_json())
    assert old.judge is None
    assert old.suites[0].judge is None
    assert "- **Judge:** the run model (self-judged)" in render_markdown(old)
    found = summary(old)
    assert (found["judge"], found["judges"]) == (None, {})
    committed = report_files(checks.REPORTS_DIR)
    assert committed
    older = 0
    for path in committed:
        embedded = read_summary(path)
        assert embedded is not None, path
        if "judges" not in embedded:  # written before #475
            older += 1
            assert embedded.get("judge") is None, path
    assert older  # the committed history still holds reports written before #475
    assert checks.problems() == []


def test_committed_judge_means_pool_only_with_the_same_judge(tmp_path: Path) -> None:
    """E-13's committed voice means are pooled per judge model (#475): a row names its judge,
    and a row written before #475 was judged by its report's model. Complement: with no judge
    named, every judge's rows pool as before."""
    voice = metric_id("structured", "voice_consistency")

    def row(fingerprint: str, mean: float, n: int, judge: str | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {"metric": voice, "fingerprint": fingerprint, "mean": mean, "n": n}
        if judge is not None:
            found["judge"] = judge
        return found

    def committed(name: str, model: str, rows: list[dict[str, Any]]) -> None:
        document = {"date": "2026-10-20", "fingerprint": "sha256:x", "model": model, "judged": rows}
        (tmp_path / name).write_text(f"{SUMMARY_PREFIX}{json.dumps(document)} -->\n", "utf-8")

    committed("old.md", "m", [row("fp-a", 0.5, 2)])
    committed("judged.md", "m", [row("fp-a", 1.0, 2, judge="j"), row("fp-b", 0.2, 1, judge="j")])
    assert committed_voice_means(tmp_path, "m") == {"fp-a": (1.0, 2)}
    assert committed_voice_means(tmp_path, "j") == {"fp-a": (2.0, 2), "fp-b": (0.2, 1)}
    assert committed_voice_means(tmp_path) == {"fp-a": (3.0, 4), "fp-b": (0.2, 1)}


# -- routing, on the stub ---------------------------------------------------------------------


@pytest.fixture
def judge_layers(monkeypatch: pytest.MonkeyPatch) -> list[JudgeModels]:
    """Every judge layer the runner builds, so a test can read its stub's calls."""
    built: list[JudgeModels] = []

    class Recorded(JudgeModels):
        def __init__(self, base: ModelAccessLayer, judge: JudgeEndpoint) -> None:
            super().__init__(base, judge)
            built.append(self)

    monkeypatch.setattr(runners, "JudgeModels", Recorded)
    return built


def _purposes(calls: list[Any]) -> set[str]:
    return {c.purpose for c in calls}


@pytest.mark.integration
async def test_e03_judge_calls_go_to_the_judge_model(
    pg_url: str, engine: AsyncEngine, judge_layers: list[JudgeModels]
) -> None:
    """With a judge model, E-03's entailment judgements go to the judge's stub, which reports
    the judge's model name; the consolidation stays on the run model's stub, and the run model
    gets no entailment call. The report names the judge and its reported version for E-03, and
    the fingerprint covers it.

    Complement asserted: the same run with no judge model sends the entailment calls to the run
    model and marks E-03 self-judged."""
    suite = SUITES["e03"]
    async with stub_engine(pg_url, engine) as lib:
        judged = await run(
            suite, AdapterConfig(judge_model=JUDGE), SEEDS, engine=lib, sets_dir=SETS, sample=4
        )
        run_calls = list(lib.models.stub.calls)
    (layer,) = judge_layers
    judge_calls = [c for c in layer.stub.calls if c.messages != PROBE_MESSAGES]
    assert judge_calls
    assert _purposes(judge_calls) == {ENTAILMENT.name}
    assert {c.model for c in layer.stub.calls} == {JUDGE}
    assert ENTAILMENT.name not in _purposes(run_calls)
    assert "consolidation" in _purposes(run_calls)
    assert JUDGE not in {c.model for c in run_calls}
    assert judged.errors == []
    (result,) = judged.suites
    assert result.errors == []
    metrics = {m.id: m for m in result.metrics}
    assert metrics[FAITHFULNESS].mean == 1.0
    assert metrics[JUDGE_AGREEMENT].mean == 1.0
    assert result.judge is not None
    assert (result.judge.model, result.judge.model_version, result.judge.self_judged) == (
        JUDGE,
        JUDGE,
        False,
    )
    assert result.judge.purposes == [ENTAILMENT.name]
    assert judged.judge is not None
    assert (judged.judge.model, judged.judge.model_version) == (JUDGE, JUDGE)
    assert judged.fingerprint == run_fingerprint(STUB_MODEL, judge=JUDGE)
    assert judged.model == STUB_MODEL
    markdown = render_markdown(judged)
    assert f"- **Judge:** `{JUDGE}` (reported version: `{JUDGE}`)" in markdown
    assert f"Judge: `{JUDGE}` (reported version: `{JUDGE}`), a second model" in markdown
    embedded = summary(judged)
    assert embedded["judge"] == JUDGE
    assert embedded["judges"]["e03"]["self_judged"] is False

    async with stub_engine(pg_url, engine) as lib:
        alone = await run(suite, AdapterConfig(), SEEDS, engine=lib, sets_dir=SETS, sample=4)
        alone_calls = list(lib.models.stub.calls)
    assert len(judge_layers) == 1  # no judge layer without a judge model
    assert ENTAILMENT.name in _purposes(alone_calls)
    (own,) = alone.suites
    assert own.judge is not None
    assert (own.judge.model, own.judge.self_judged) == (STUB_MODEL, True)
    assert alone.judge is None
    assert alone.fingerprint == run_fingerprint(STUB_MODEL)
    assert "Judge: **self-judged**. The run model `stub`" in render_markdown(alone)
    assert {m.id: m.mean for m in own.metrics} == {m.id: m.mean for m in result.metrics}


@pytest.mark.integration
async def test_e12_quality_judge_and_its_calibration_use_the_judge_model(
    pg_url: str, engine: AsyncEngine, tmp_path: Path, judge_layers: list[JudgeModels]
) -> None:
    """With a judge model, the #158 calibration and E-12's pairwise judgements go to the judge;
    the calibration is stored and looked up under the judge model, and E-12's turns stay on the
    run model.

    Complement asserted: the run model gets no ``quality_judge`` call, no calibration is stored
    under it, and a later ``--suite e12`` with the same judge finds the judge's calibration."""
    store = tmp_path / "judge-calibration.json"
    suite = SUITES["e12"]
    config = AdapterConfig(judge_model=JUDGE)
    async with stub_engine(pg_url, engine) as lib:
        report = await run_suites(
            [suite],
            config,
            SEEDS,
            engine=lib,
            sets_dir=SETS,
            sample=3,
            calibrate=True,
            calibration_file=store,
        )
        run_calls = list(lib.models.stub.calls)
    (layer,) = judge_layers
    assert QUALITY_JUDGE.name in _purposes(layer.stub.calls)
    assert {c.model for c in layer.stub.calls} == {JUDGE}
    assert QUALITY_JUDGE.name not in _purposes(run_calls)
    assert "generation" in _purposes(run_calls)
    assert report.errors == []
    (result,) = report.suites
    assert result.errors == []
    assert result.calibration is not None
    assert (result.calibration.judge_model, result.calibration.status) == (JUDGE, "calibrated")
    assert result.judge is not None
    assert (result.judge.model, result.judge.self_judged) == (JUDGE, False)
    assert lookup(store, JUDGE) is not None
    assert lookup(store, STUB_MODEL) is None

    async with stub_engine(pg_url, engine) as lib:
        again = await run_suites(
            [suite], config, SEEDS, engine=lib, sets_dir=SETS, sample=1, calibration_file=store
        )
    assert again.errors == []
    (rerun,) = again.suites
    assert rerun.calibration is not None
    assert rerun.calibration.judge_model == JUDGE
    with pytest.raises(runners.CalibrationMissing):
        async with stub_engine(pg_url, engine) as lib:
            await run_suites(
                [suite], AdapterConfig(), SEEDS, engine=lib, sets_dir=SETS, calibration_file=store
            )
