"""From an eval run to a certification record (FR-PROF-15..17, NFR-QUAL-8; #233).

``EvalHarness`` is the harness the Engine reaches through ``core.profiles.certification``'s
port (nothing in the engine imports this package): the process's composition root installs it
(``install``; ``epistrel serve`` does, and so do the tests), and the ``certification_run`` job
plays a run through it.

- **Suites** (``applicable``): those whose capability the configuration enables (#227's
  ``Suite.requires`` and guarantee rows, ``presets.needs_of``).
- **Estimate** (``estimate``): per suite, its cases (the labelled set, capped by ``sample``)
  times the seeds times its calls per case (``Suite.calls_per_case``, default 1); tokens at
  ``TOKENS_PER_CALL`` a call and the duration at ``estimates.CALL_LATENCY_MS`` a call, one after
  another. No cost: the model matrix prices no tokens.
- **Run** (``run``): the suites play on scratch stories assigned the configuration's profile
  (``presets.Preset`` over its version, with a story's overrides for a story's run) through the
  in-process Engine and the deployment's model configuration; the report names the fingerprint
  the request captured.
- **Record** (``record_from``): each floored metric's verdict becomes a floor passed, failed or
  insufficient (``FloorResult``: ``metric_id`` is the NFR-QUAL id with its sub-metric,
  ``NFR-QUAL-1.false_support``, or the suite's own id for a harness metric); informational
  and report-only rows are left out. E-12's rows of the run's own configuration
  (``structured``) give the intervention types NFR-QUAL-14's floor covers (``pass``: at least
  300 non-tied pairs a seed and a calibrated judge, which ``gate.judge`` requires) and those
  with ``insufficient_evidence``; NFR-QUAL-14 is passed when at least one type is covered, and
  a type that fails is a floor failed (``{metric_id: NFR-QUAL-14, intervention_type}``). An
  uncalibrated judge makes every judge row ``report_only``: NFR-QUAL-14 is not passed, both
  lists are empty and ``judge_calibrated`` is false. A suite the model's guardrail refused at
  setup (#738) carries each floored metric as failed with n=0 (``SuiteResult.refused``), so its
  floors are failed, never left out (#756); a refused E-12's types fail whatever the judge. No
  floor passed: no record (the run is recorded, and the configuration stays ``uncertified``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from epistrel.core.ids import new_ulid
from epistrel.core.profiles import estimates
from epistrel.core.profiles.certification import (
    QUALITY_14,
    SCHEMA_VERSION,
    CertificationRecord,
    FloorResult,
    HarnessOutcome,
    HarnessRequest,
    RunEstimate,
    install_harness,
    report_hash,
)
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.engine import Engine
from epistrel.evals.gate import Rule, Verdict
from epistrel.evals.presets import Preset, needs_of
from epistrel.evals.report import MetricResult, Report, render_markdown, summary
from epistrel.evals.runner import DEFAULT_SETS_DIR, AdapterConfig, run_suites, seeds_from
from epistrel.evals.suites import SUITES, MalformedCase, Suite
from epistrel.evals.suites.e12_interventions import metric_id as e12_metric_id
from epistrel.evals.suites.scenario_play import INTERVENTION_TYPES

TOKENS_PER_CALL = 2_000
"""A model call's prompt and completion tokens, for an estimate (a structured turn's call)."""
E12 = "e12"
E12_CONFIG = "structured"
"""E-12's rows of the run's own configuration (``gated`` is a variant with its own fingerprint)."""
TIER_REFERENCE = "reference"
TIER_OTHER = "other"


def requirement_id(metric_id: str) -> str:
    """``nfr_qual_1.false_support`` → ``NFR-QUAL-1.false_support``; another id as it is."""
    head, _, rest = metric_id.partition(".")
    if head.startswith("nfr_qual_") and head.removeprefix("nfr_qual_").isdigit():
        number = int(head.removeprefix("nfr_qual_"))
        return f"NFR-QUAL-{number}" + (f".{rest}" if rest else "")
    return metric_id


def _floor(metric: MetricResult, metric_id: str, intervention: str | None = None) -> FloorResult:
    return FloorResult(
        metric_id=metric_id,
        floor=metric.floor,
        rule=metric.rule.value,
        mean=metric.mean,
        sd=metric.stdev,
        n=metric.sample_size,
        seeds=metric.seed_count,
        upper_bound_95=metric.upper_bound_95,
        verdict=metric.verdict.value,
        intervention_type=intervention,
    )


def _e12_types() -> dict[str, str]:
    return {e12_metric_id(E12_CONFIG, kind): kind for kind in INTERVENTION_TYPES}


def floors_of(
    report: Report,
) -> tuple[list[FloorResult], list[FloorResult], list[FloorResult], list[str], list[str], bool]:
    """The run's floors passed, failed and insufficient, E-12's covered and insufficient
    intervention types, and whether its judge was calibrated (see the module docstring)."""
    passed: list[FloorResult] = []
    failed: list[FloorResult] = []
    insufficient: list[FloorResult] = []
    covered: list[str] = []
    lacking: list[str] = []
    calibrated = False
    types = _e12_types()
    for suite in report.suites:
        judged = suite.calibration is not None
        if judged and suite.calibration is not None:
            calibrated = suite.calibration.status == "calibrated"
        for metric in suite.metrics:
            if metric.rule is Rule.informational or metric.floor is None:
                continue
            if suite.name == E12:
                kind = types.get(metric.id)
                if kind is None:
                    continue
                if metric.verdict is Verdict.passed and calibrated:
                    covered.append(kind)
                elif metric.verdict is Verdict.insufficient_evidence and calibrated:
                    lacking.append(kind)
                elif metric.verdict is Verdict.failed and (calibrated or suite.refusal):
                    failed.append(_floor(metric, QUALITY_14, kind))
                continue
            target = {
                Verdict.passed: passed,
                Verdict.failed: failed,
                Verdict.insufficient_evidence: insufficient,
            }.get(metric.verdict)
            if target is not None:
                target.append(_floor(metric, requirement_id(metric.id)))
    if covered:
        passed.append(
            FloorResult(
                metric_id=QUALITY_14,
                floor=None,
                rule=Rule.relative.value,
                n=0,
                seeds=len(report.seeds),
                verdict=Verdict.passed.value,
            )
        )
    return passed, failed, insufficient, covered, lacking, calibrated


def record_from(
    report: Report,
    report_md: str,
    *,
    fingerprint: str,
    profile_id: str | None,
    profile_version: int | None,
    models: dict[str, Any],
    preset: str | None = None,
    tier: str = TIER_OTHER,
) -> CertificationRecord | None:
    """The certification record of ``report`` (see the module docstring); ``None`` when no
    floor passed."""
    passed, failed, insufficient, covered, lacking, calibrated = floors_of(report)
    if not passed:
        return None
    return CertificationRecord(
        schema_version=SCHEMA_VERSION,
        certification_id=new_ulid(),
        fingerprint=fingerprint,
        profile_id=profile_id,
        profile_version=profile_version,
        preset=preset,
        models=models,
        floors_passed=passed,
        floors_failed=failed,
        floors_insufficient=insufficient,
        intervention_types_covered=covered,
        intervention_types_insufficient=lacking,
        judge_calibrated=calibrated,
        sample_sizes={f.metric_id: f.n for f in (*passed, *failed, *insufficient)},
        seed_count=len(report.seeds),
        model_tier=tier,
        host=report.host.model_dump(mode="json", exclude={"endpoint"}),
        report_sha256=report_hash(report_md),
        run_at=datetime.fromisoformat(report.started_at),
    )


def _cases(suite: Suite, sample: int | None) -> int:
    try:
        count = len(suite.load(DEFAULT_SETS_DIR))
    except MalformedCase:
        count = 0
    return min(count, sample) if sample is not None else count


class EvalHarness:
    """``core.profiles.certification.Harness`` over the in-process ``engine``."""

    def __init__(self, engine: Engine, *, adapter: AdapterConfig | None = None) -> None:
        self.engine = engine
        self.adapter = adapter or AdapterConfig()

    def suites(self) -> list[str]:
        # A standalone suite (#270's E-11) certifies an image checker, not a configuration.
        return sorted(name for name, suite in SUITES.items() if not suite.standalone)

    def applicable(self, settings: ProfileSettings) -> list[str]:
        return sorted(
            name
            for name, suite in SUITES.items()
            if not suite.standalone
            and not needs_of(
                settings,
                requires=suite.requires,
                guarantees=suite.guarantees,
                structured=suite.structured,
            )
        )

    def estimate(self, suites: Sequence[str], seeds: int, sample: int | None) -> RunEstimate:
        calls = sum(
            _cases(SUITES[name], sample) * seeds * SUITES[name].calls_per_case for name in suites
        )
        return RunEstimate(
            model_calls=calls,
            tokens=calls * TOKENS_PER_CALL,
            duration_s=math.ceil(calls * estimates.CALL_LATENCY_MS / 1000),
        )

    def committed_runs(self) -> list[dict[str, Any]]:
        """The reports committed under ``docs/evals`` (#532), each its embedded summary."""
        return _committed()

    def suites_of(self, metric_ids: Sequence[str]) -> list[str]:
        """The suites whose floored metrics include one of ``metric_ids`` (NFR-QUAL-14: E-12)."""
        wanted = set(metric_ids)
        return sorted(
            name
            for name, suite in SUITES.items()
            if not suite.standalone
            and (
                (name == E12 and QUALITY_14 in wanted)
                or any(requirement_id(m.id) in wanted for m in suite.metrics)
            )
        )

    async def run(self, request: HarnessRequest) -> HarnessOutcome:
        profile = Preset(
            name="certification",
            label=request.profile_name,
            profile_id=request.profile_id,
            settings=request.document,
            version=request.profile_version,
            overrides=request.overrides,
        )
        report = await run_suites(
            [SUITES[name] for name in request.suites],
            self.adapter,
            seeds_from(request.seeds),
            engine=self.engine,
            sample=request.sample,
            run_id=f"certification-{request.run_id}",
            profile=profile,
            fingerprint_value=request.fingerprint,
        )
        report_md = render_markdown(report)
        record = record_from(
            report,
            report_md,
            fingerprint=request.fingerprint,
            profile_id=request.profile_id,
            profile_version=request.profile_version,
            models=request.models,
            tier=TIER_REFERENCE if request.reference else TIER_OTHER,
        )
        found = summary(report)
        passed, failed, insufficient, covered, lacking, calibrated = floors_of(report)
        found["floors"] = {
            "passed": [f.model_dump(mode="json") for f in passed],
            "failed": [f.model_dump(mode="json") for f in failed],
            "insufficient": [f.model_dump(mode="json") for f in insufficient],
        }
        found["intervention_types_covered"] = covered
        found["intervention_types_insufficient"] = lacking
        found["judge_calibrated"] = calibrated
        found["errors"] = list(report.errors)
        found["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return HarnessOutcome(summary=found, report_md=report_md, record=record)


def _committed() -> list[dict[str, Any]]:
    from epistrel.evals.report import read_summary, report_files
    from epistrel.tools.generated import REPO_ROOT

    found: list[dict[str, Any]] = []
    for path in report_files(REPO_ROOT / "docs" / "evals"):
        summary = read_summary(path)
        if summary is not None:
            found.append(summary)
    return found


def install(engine: Engine, *, adapter: AdapterConfig | None = None) -> EvalHarness:
    """Install the harness over ``engine`` as the process's (``Harness`` port)."""
    harness = EvalHarness(engine, adapter=adapter)
    install_harness(harness)
    return harness


__all__ = [
    "TOKENS_PER_CALL",
    "EvalHarness",
    "floors_of",
    "install",
    "record_from",
    "requirement_id",
]
