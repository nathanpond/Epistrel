"""Eval reports (NFR-QUAL-8, NFR-QUAL-11; #134): the JSON a run writes, the Markdown table a
person reads, and the index of the committed ones.

**JSON** (``version: 1``, one file per run under ``build/evals/``, never committed): the run's
``sample_size`` (cases per seed, every suite's together), ``seed_count`` and seeds, ``model``
and ``model_version`` (the completion's own ``model`` field; ``null`` when it reported none),
``fingerprint`` (FR-PROF-16, #109), ``host`` (``ci`` or ``local``, with #54's hardware and
server-version fields and the endpoint; never the machine's name), and per suite, per metric:
``mean``, ``stdev``, ``floor``, ``rule``, ``direction``, ``sample_size``, ``seed_count``,
``verdict``, and for a zero-tolerance metric its ``violations`` and ``upper_bound_95``; per
suite, its ``notes`` (the suite's scoring notes, rendered under "How to read"; #437).
Values are fractions 0-1.

**The judge** (#475). ``judge`` names the run's second judge model (its model, the version it
reported and its endpoint), or is ``null`` when the run model judged. Each suite whose rows come
from a judge (``Suite.judge_purposes``: E-03's ``entailment``, E-12/E-13's ``quality_judge``)
records the judge that ran in ``judge``, with ``self_judged`` true when that was the run model.
A report written before #475 has neither field. It still parses, and its judged suites were
self-judged.

**Markdown** (``docs/evals/<fingerprint[:12]>-<YYYY-MM-DD>.md`` for a real model, committed
whatever its verdict as the honest record; ``build/evals/stub-report.md`` for a stub run):
columns metric / floor / rule / mean ± sd / n / seeds / verdict / bound, percentages to two
decimals. A clean zero-tolerance row reads "0 observed in n cases, one-sided 95 % upper bound
≈ x %" — never a claim that the rate is zero. Each report embeds its summary as one HTML
comment (``<!-- epistrel-eval-report {...} -->``), which ``render_index`` reads to regenerate
``docs/evals/INDEX.md``: date (UTC), model, fingerprint, host, and the verdict per NFR-QUAL
requirement the run measured, newest first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from epistrel.evals.gate import (
    RELATIVE_MIN_PAIRS,
    ZERO_MIN_CASES,
    Direction,
    Judgement,
    Metric,
    Rule,
    Verdict,
    gate,
)

if TYPE_CHECKING:
    from epistrel.evals.suites import Suite

REPORT_VERSION: Literal[1] = 1
SUMMARY_PREFIX = "<!-- epistrel-eval-report "
SUMMARY_RE = re.compile(r"^<!-- epistrel-eval-report (?P<json>\{.*\}) -->$", re.MULTILINE)
INDEX_NAME = "INDEX.md"
README_NAME = "README.md"
DIGEST_NAME = "sets-digest.md"
NOT_REPORTS = frozenset({INDEX_NAME, README_NAME, DIGEST_NAME})
EM_DASH = "—"
VERDICT_TEXT: Mapping[Verdict, str] = {
    Verdict.passed: "pass",
    Verdict.failed: "**fail**",
    Verdict.insufficient_evidence: "insufficient evidence",
    Verdict.report_only: "report only",
    Verdict.error: "**error**",
}


class Host(BaseModel):
    """Where a run ran: its kind and #54's hardware and server-version description, and the
    endpoint. No machine name: reports are committed to a public repository (#134)."""

    kind: Literal["ci", "local"]
    hardware: str = ""
    server_version: str = ""
    endpoint: str | None = None


class MetricResult(BaseModel):
    id: str
    description: str = ""
    rule: Rule
    direction: Direction
    floor: float | None
    mean: float | None
    stdev: float | None
    sample_size: int
    seed_count: int
    per_seed: list[float]
    violations: int | None = None
    upper_bound_95: float | None = None
    verdict: Verdict
    ci95: list[float] | None = None
    """A 95 % confidence interval of the value over every seed's observations pooled (#158:
    Wilson for a judge's preference rate, normal for a mean score); ``None`` when not given."""
    judge_based: bool = False
    """The value comes from the quality judge: an uncalibrated judge makes it ``report_only``."""
    fingerprint: str | None = None
    """The configuration fingerprint a per-configuration row measured (E-13's rows, #463);
    ``None`` for a row of the run's own fingerprint or of no single configuration."""

    @classmethod
    def of(cls, metric: Metric, judgement: Judgement) -> MetricResult:
        return cls(
            id=metric.id,
            description=metric.description,
            rule=metric.rule,
            direction=metric.direction,
            floor=metric.floor,
            mean=judgement.mean,
            stdev=judgement.stdev,
            sample_size=judgement.sample_size,
            seed_count=judgement.seed_count,
            per_seed=list(judgement.per_seed),
            violations=judgement.violations,
            upper_bound_95=judgement.upper_bound_95,
            verdict=judgement.verdict,
        )

    @property
    def requirement(self) -> str | None:
        return Metric(self.id, self.rule).requirement


class JudgeCalibrationRef(BaseModel):
    """The judge calibration a suite's judge-based rows ran under (#158): ``calibrated``
    (agreement with the human labels at least the floor, on enough pairs), ``uncalibrated``
    (below it: those rows are ``report_only``) or ``not_run``; the judge model and when."""

    status: Literal["calibrated", "uncalibrated", "not_run"]
    judge_model: str
    date: str | None = None
    pairs: int = 0
    agreement: float | None = None
    ci95: list[float] | None = None
    floor: float = 0.75


class JudgeModel(BaseModel):
    """A run's second judge model (#475): the model, the version its endpoint reported and the
    endpoint (``None`` on the stub)."""

    model: str
    model_version: str | None = None
    endpoint: str | None = None


class SuiteJudge(BaseModel):
    """The judge a judged suite's calls went to (#475): its model and reported version, the
    purposes it judged with, and whether it was the run model judging its own output."""

    model: str
    model_version: str | None = None
    purposes: list[str] = Field(default_factory=list)
    self_judged: bool


class SuiteResult(BaseModel):
    name: str
    title: str
    ci_gate: bool
    guarantees: list[str] = Field(default_factory=list)
    cases: int
    seeds: list[int]
    metrics: list[MetricResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    verdict: Verdict
    duration_s: float | None = None
    mean_case_ms: int | None = None
    notes: list[str] = Field(default_factory=list)
    calibration: JudgeCalibrationRef | None = None
    """For a suite with judge-based rows (#158): the calibration they were judged under."""
    judge: SuiteJudge | None = None
    """For a judged suite (#475): the judge its calls went to; ``None`` for any other suite, and
    in a report written before #475."""
    profile: str | None = None
    """The configuration the suite's stories ran under, when it is not the run's (#179)."""
    fingerprint: str | None = None
    """That configuration's fingerprint (#179); ``None``: the run's."""

    @classmethod
    def errored(cls, suite: Suite, errors: list[str], seeds: list[int]) -> SuiteResult:
        return cls(
            name=suite.name,
            title=suite.title,
            ci_gate=suite.ci_gate,
            guarantees=list(suite.guarantees),
            cases=0,
            seeds=seeds,
            errors=errors,
            verdict=Verdict.error,
            notes=list(suite.reading_notes),
        )


class Report(BaseModel):
    version: Literal[1] = REPORT_VERSION
    run_id: str
    started_at: str
    finished_at: str | None = None
    model: str
    model_version: str | None = None
    fingerprint: str
    fingerprint_profile: str
    adapter: str
    engine_sha: str
    host: Host
    judge: JudgeModel | None = None
    """The run's second judge model (#475); ``None``: the run model judged (self-judged)."""
    real_purposes: list[str] | None = None
    """The purposes a real-model run sent to its endpoint (#179); ``None``: every purpose."""
    seed_base: int
    seeds: list[int]
    suites: list[SuiteResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    verdict: Verdict = Verdict.insufficient_evidence

    @property
    def seed_count(self) -> int:
        return len(self.seeds)

    @property
    def sample_size(self) -> int:
        """Cases per seed, every suite's together."""
        return sum(s.cases for s in self.suites)

    @property
    def harness_errors(self) -> list[str]:
        return [*self.errors, *(e for s in self.suites for e in s.errors)]

    def merge(self, result: SuiteResult) -> None:
        """Add ``result``, replacing an earlier result of the same suite (a ``--suite`` rerun)."""
        self.suites = [s for s in self.suites if s.name != result.name] + [result]
        self.verdict = self.computed_verdict()

    def computed_verdict(self) -> Verdict:
        if self.errors:
            return Verdict.failed
        return gate(s.verdict for s in self.suites)

    def finish(self, when: datetime) -> None:
        self.finished_at = when.isoformat(timespec="seconds")
        self.verdict = self.computed_verdict()

    def as_json(self) -> dict[str, Any]:
        """The JSON document: the model plus the derived run-level fields."""
        document = self.model_dump(mode="json")
        document["sample_size"] = self.sample_size
        document["seed_count"] = self.seed_count
        return document

    @classmethod
    def from_json(cls, document: Mapping[str, Any]) -> Report:
        data = {k: v for k, v in document.items() if k not in ("sample_size", "seed_count")}
        return cls.model_validate(data)

    @property
    def date(self) -> str:
        return self.started_at[:10]


# -- Markdown ---------------------------------------------------------------------------------


def percent(value: float | None) -> str:
    return EM_DASH if value is None else f"{value * 100:.2f}%"


def _floor_text(metric: MetricResult) -> str:
    if metric.rule is Rule.informational or metric.floor is None:
        return "none"
    if metric.rule is Rule.zero:
        return f"0 observed (n ≥ {ZERO_MIN_CASES})"
    if metric.rule is Rule.relative:
        return f"mean + SD ≤ {percent(metric.floor)} (n ≥ {RELATIVE_MIN_PAIRS})"
    sign = "≥" if metric.direction is Direction.at_least else "≤"
    return f"{sign} {percent(metric.floor)}"


def _measured_text(metric: MetricResult) -> str:
    """The per-seed value's mean ± SD: the good rate for a ≥ floor, the bad rate otherwise;
    with its pooled 95 % confidence interval when the suite gives one (#158)."""
    if metric.mean is None:
        return "no observations"
    spread = EM_DASH if metric.stdev is None else percent(metric.stdev)
    ci = (
        f" (95% CI {percent(metric.ci95[0])} to {percent(metric.ci95[1])})"
        if metric.ci95 is not None
        else ""
    )
    return f"{percent(metric.mean)} ± {spread}{ci}"


def calibration_text(calibration: JudgeCalibrationRef) -> str:
    """One line on the judge calibration a suite's judge-based rows ran under (#158)."""
    if calibration.status == "uncalibrated" and calibration.agreement is None:
        return (
            f"Judge calibration: the single-message scores of `{calibration.judge_model}` have "
            "no calibration set in M4, so they are **uncalibrated**; every judge-based row is "
            "report only."
        )
    if calibration.status == "not_run" or calibration.agreement is None:
        return (
            f"Judge calibration: **not run** for `{calibration.judge_model}`; every judge-based "
            "row is report only."
        )
    ci = (
        f" (95% CI {percent(calibration.ci95[0])} to {percent(calibration.ci95[1])})"
        if calibration.ci95
        else ""
    )
    outcome = (
        "calibrated"
        if calibration.status == "calibrated"
        else "**uncalibrated**: every judge-based row is report only"
    )
    return (
        f"Judge calibration: `{calibration.judge_model}` agreed with the human labels on "
        f"{percent(calibration.agreement)}{ci} of {calibration.pairs} pairs "
        f"(floor {percent(calibration.floor)}, {calibration.date or EM_DASH}): {outcome}."
    )


def _version_text(version: str | None) -> str:
    return f"`{version}`" if version else EM_DASH


def judge_text(judge: SuiteJudge) -> str:
    """One line on the judge a judged suite's calls went to (#475)."""
    purposes = ", ".join(f"`{p}`" for p in judge.purposes) or EM_DASH
    if judge.self_judged:
        return (
            f"Judge: **self-judged**. The run model `{judge.model}` (reported version: "
            f"{_version_text(judge.model_version)}) judged its own output ({purposes})."
        )
    return (
        f"Judge: `{judge.model}` (reported version: {_version_text(judge.model_version)}), a "
        f"second model, made this suite's judge calls ({purposes})."
    )


def _bound_text(metric: MetricResult) -> str:
    if metric.rule is not Rule.zero:
        return EM_DASH
    if metric.violations:
        return f"{metric.violations} observed"
    if metric.upper_bound_95 is None or metric.sample_size == 0:
        return EM_DASH
    return (
        f"0 observed in {metric.sample_size} cases, one-sided 95% upper bound "
        f"≈ {percent(metric.upper_bound_95)}"
    )


def _metric_row(metric: MetricResult) -> str:
    cells = [
        f"`{metric.id}`",
        _floor_text(metric),
        metric.rule.value,
        _measured_text(metric),
        str(metric.sample_size),
        str(metric.seed_count),
        VERDICT_TEXT[metric.verdict],
        _bound_text(metric),
    ]
    return "| " + " | ".join(cells) + " |"


def summary(report: Report) -> dict[str, Any]:
    """What ``INDEX.md`` needs of a report, embedded in its Markdown."""
    return {
        "run_id": report.run_id,
        "date": report.date,
        "model": report.model,
        "model_version": report.model_version,
        "fingerprint": report.fingerprint,
        "host": report.host.kind,
        "verdict": report.verdict.value,
        "suites": {s.name: s.verdict.value for s in report.suites},
        "requirements": requirement_verdicts(report),
        "judged": judged_means(report),
        "judge": report.judge.model if report.judge is not None else None,
        "judges": {
            s.name: s.judge.model_dump(mode="json") for s in report.suites if s.judge is not None
        },
    }


def judged_means(report: Report) -> list[dict[str, Any]]:
    """Every judge-based row that measured a named configuration fingerprint, with its mean and
    n (#463): what a later run pools across the committed reports, e.g. E-13's feedback
    correlation, which needs more fingerprints than one run judges. Each row names the model
    that judged it (#475), so rows are pooled only with rows from the same judge."""
    return [
        {
            "metric": m.id,
            "fingerprint": m.fingerprint,
            "mean": m.mean,
            "n": m.sample_size,
            "judge": s.judge.model if s.judge is not None else report.model,
        }
        for s in report.suites
        for m in s.metrics
        if m.judge_based and m.fingerprint is not None and m.mean is not None
    ]


def requirement_verdicts(report: Report) -> dict[str, str]:
    """Each NFR-QUAL requirement the run measured → the gate over its metrics' verdicts."""
    found: dict[str, list[Verdict]] = {}
    for suite in report.suites:
        for metric in suite.metrics:
            requirement = metric.requirement
            if requirement is not None:
                found.setdefault(requirement, []).append(metric.verdict)
    return {req: gate(verdicts).value for req, verdicts in sorted(found.items(), key=_req_key)}


def _req_key(item: tuple[str, Any]) -> int:
    return int(item[0].rsplit("-", 1)[1])


HOW_TO_READ = """\
## How to read this report

- **Rate rule** (NFR-QUAL-8): a rate metric passes when its mean across seeds clears the floor by
  more than one sample standard deviation (≥ floors: mean - SD above the floor; ≤ floors: mean +
  SD below it). One seed, or no observations, is *insufficient evidence*.
- **Observed-zero rule** (NFR-QUAL-8): a zero-tolerance metric passes only with no violation on any
  seed, over at least 300 labeled cases. The bound column gives the one-sided 95% upper bound on
  the true rate (≈ 3/n): a clean run is evidence of a low rate, not proof of a zero one.
- **Relative rule** (NFR-QUAL-14): an engine-intervention type passes when its first-draft
  preference rate's mean across seeds plus one SD is at most the floor, judged only on at least
  300 non-tied pairs per seed; fewer is *insufficient evidence*, never a pass. It is an aggregate
  target: individual messages may still read worse than their drafts.
- A judge-based row is only as good as the judge: before such a run the quality judge is
  calibrated against human-labeled pairs, and a judge whose agreement is below 75% makes every
  judge-based row *report only*.
- A model error during a case is a failed case, never silently excluded. In a suite with a
  model-error row (NFR-QUAL-1's zero-leak suites) it is counted and reported on that row, which
  is report-only, and it is not scored as a disclosure: the suite's floors are judged on its
  completed cases, and a zero-tolerance metric with fewer than 300 completed cases on a seed is
  *insufficient evidence*, never a pass. Model-robustness floors belong to M9 (ADR-164). In
  every other suite a model error counts as a bad observation on each of its metrics. Timings
  are informational: the model may share its machine with other work.
- A floor applies to the configuration this fingerprint names (NFR-QUAL-11); any other
  configuration is unmeasured until it has its own report.
"""


def render_markdown(report: Report) -> str:
    """The report as Markdown (see the module docstring)."""
    seeds = report.seeds
    seed_text = (
        f"{len(seeds)} ({seeds[0]}-{seeds[-1]})" if len(seeds) > 1 else f"{len(seeds)} {seeds}"
    )
    host = report.host
    host_bits: list[str] = [host.kind]
    host_bits += [bit for bit in (host.hardware, host.server_version) if bit]
    endpoint = f"`{report.adapter}`" + (f" at `{host.endpoint}`" if host.endpoint else "")
    if report.judge is not None:
        at = f" at `{report.judge.endpoint}`" if report.judge.endpoint else ""
        judge_line = (
            f"- **Judge:** `{report.judge.model}` (reported version: "
            f"{_version_text(report.judge.model_version)}){at}, a second model for the "
            "harness's judge calls; the run model served every engine call"
        )
    else:
        judge_line = "- **Judge:** the run model (self-judged)"
    lines = [
        f"# Eval report {report.run_id}",
        "",
        f"{SUMMARY_PREFIX}{json.dumps(summary(report), sort_keys=True, ensure_ascii=False)} -->",
        "",
        f"- **Run verdict:** {VERDICT_TEXT[report.verdict]}",
        f"- **Model:** `{report.model}` (reported version: "
        f"{f'`{report.model_version}`' if report.model_version else EM_DASH})",
        f"- **Adapter:** {endpoint}",
        judge_line,
        f"- **Fingerprint:** `{report.fingerprint}` ({report.fingerprint_profile}, with the model "
        "as every story purpose's"
        + (" and the judge as `quality_judge`'s" if report.judge is not None else "")
        + ")",
        *(
            [
                "- **Routed purposes:** "
                + ", ".join(f"`{p}`" for p in report.real_purposes)
                + " on the model; every other purpose on the stub's default answers"
            ]
            if report.real_purposes is not None
            else []
        ),
        f"- **Host:** {'; '.join(host_bits)}",
        f"- **Seeds:** {seed_text}",
        f"- **Sample size:** {report.sample_size} cases per seed",
        f"- **Engine:** `{report.engine_sha}`",
        f"- **Run:** {report.started_at} to {report.finished_at or EM_DASH} (UTC)",
    ]
    if report.errors:
        lines += ["", "**Run errors:**", ""] + [f"- {error}" for error in report.errors]
    for suite in report.suites:
        lines += ["", f"## `{suite.name}`: {suite.title}", ""]
        timing = (
            f"; mean case time {suite.mean_case_ms / 1000:.1f} s (informational)"
            if suite.mean_case_ms is not None
            else ""
        )
        gated = "gated in CI on the stub" if suite.ci_gate else "not gated in CI"
        lines.append(
            f"Suite verdict: {VERDICT_TEXT[suite.verdict]}. {suite.cases} cases x "
            f"{len(suite.seeds)} seeds{timing}; {gated}. Guarantee rows measured: "
            f"{', '.join(f'`{g}`' for g in suite.guarantees) or EM_DASH}."
        )
        if suite.profile is not None:
            lines += [
                "",
                f"Configuration: {suite.profile}"
                + (f" (fingerprint `{suite.fingerprint}`)" if suite.fingerprint else "")
                + ".",
            ]
        if suite.judge is not None:
            lines += ["", judge_text(suite.judge)]
        if suite.calibration is not None:
            lines += ["", calibration_text(suite.calibration)]
        if suite.errors:
            lines += ["", "Harness errors:", ""] + [f"- {error}" for error in suite.errors]
        if suite.metrics:
            lines += [
                "",
                "| Metric | Floor | Rule | Mean ± SD | n | Seeds | Verdict | Bound |",
                "|---|---|---|---|---|---|---|---|",
            ]
            lines += [_metric_row(metric) for metric in suite.metrics]
            described = [m for m in suite.metrics if m.description]
            if described:
                lines.append("")
                lines += [f"- `{m.id}`: {m.description}" for m in described]
    lines += ["", HOW_TO_READ.rstrip()]
    lines += [f"- {note}" for suite in report.suites for note in suite.notes]
    return "\n".join(lines) + "\n"


# -- files and the index ------------------------------------------------------------------------


def read_summary(path: Path) -> dict[str, Any] | None:
    found = SUMMARY_RE.search(path.read_text(encoding="utf-8"))
    if found is None:
        return None
    loaded: dict[str, Any] = json.loads(found.group("json"))
    return loaded


def report_files(reports_dir: Path) -> list[Path]:
    """The committed reports: every Markdown file but the README, the index and the labeled
    sets' review digest (#157)."""
    if not reports_dir.is_dir():
        return []
    return sorted(p for p in reports_dir.glob("*.md") if p.name not in NOT_REPORTS)


INDEX_INTRO = """\
# Eval reports

Every committed eval report, newest first: one row per run of the quality evals against a real
model, whatever its verdict. Generated from the reports' embedded summaries by
`uv run python -m epistrel.evals index`; `uv run python -m epistrel.evals --check` fails when
this file is stale. [README.md](README.md) has the policy (no automated real-model runs) and
the procedure.
"""


def render_index(summaries: Iterable[tuple[str, Mapping[str, Any]]]) -> str:
    """``INDEX.md`` from ``(file name, summary)`` pairs."""
    rows = sorted(summaries, key=lambda item: (item[1]["date"], item[0]), reverse=True)
    requirements = sorted(
        {req for _, s in rows for req in s.get("requirements", {})},
        key=lambda req: int(req.rsplit("-", 1)[1]),
    )
    header = ["Date (UTC)", "Model", "Fingerprint", "Host", "Suites", *requirements, "Run"]
    lines = [INDEX_INTRO, "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    if not rows:
        lines.append("| " + " | ".join([EM_DASH] * len(header)) + " |")
    for name, s in rows:
        digest = str(s["fingerprint"]).split(":", 1)[-1][:12]
        suites = ", ".join(f"`{n}` {v}" for n, v in sorted(s.get("suites", {}).items()))
        cells = [
            s["date"],
            f"`{s['model']}`",
            f"`{digest}`",
            s["host"],
            suites or EM_DASH,
            *(s.get("requirements", {}).get(req, EM_DASH) for req in requirements),
            f"[{s['verdict']}]({name})",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def index_text(reports_dir: Path) -> tuple[str, list[str]]:
    """The index the reports in ``reports_dir`` produce, and the files with no summary."""
    pairs: list[tuple[str, Mapping[str, Any]]] = []
    unreadable: list[str] = []
    for path in report_files(reports_dir):
        found = read_summary(path)
        if found is None:
            unreadable.append(path.name)
        else:
            pairs.append((path.name, found))
    return render_index(pairs), unreadable


def write_index(reports_dir: Path) -> Path:
    text, _ = index_text(reports_dir)
    path = reports_dir / INDEX_NAME
    path.write_text(text, encoding="utf-8")
    return path


def write_json(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_json(), indent=2, sort_keys=True) + "\n", "utf-8")


def write_markdown(report: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")


def unique_run_id(base: str, taken: Sequence[Path]) -> str:
    """``base``, or ``base-2``, ``base-3``… when a report or JSON of that id exists."""
    names = {p.stem for p in taken}
    if base not in names:
        return base
    number = 2
    while f"{base}-{number}" in names:
        number += 1
    return f"{base}-{number}"


__all__ = [
    "INDEX_NAME",
    "REPORT_VERSION",
    "Host",
    "JudgeCalibrationRef",
    "JudgeModel",
    "MetricResult",
    "Report",
    "SuiteJudge",
    "SuiteResult",
    "calibration_text",
    "index_text",
    "judge_text",
    "percent",
    "read_summary",
    "render_index",
    "render_markdown",
    "report_files",
    "requirement_verdicts",
    "summary",
    "unique_run_id",
    "write_index",
    "write_json",
    "write_markdown",
]
