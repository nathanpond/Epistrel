"""Eval reports (NFR-QUAL-8, NFR-QUAL-11; #134): the JSON a run writes, the Markdown table a
person reads, and the index of the committed ones.

**JSON** (``version: 1``, one file per run under ``build/evals/``, never committed): the run's
``sample_size`` (cases per seed, every suite's together), ``seed_count`` and seeds, ``model``
and ``model_version`` (the completion's own ``model`` field; ``null`` when it reported none),
``fingerprint`` (FR-PROF-16, #109), ``host`` (``ci``/``local``/a host name, with #54's hardware
and server-version fields and the endpoint), and per suite, per metric: ``mean``, ``stdev``,
``floor``, ``rule``, ``direction``, ``sample_size``, ``seed_count``, ``verdict``, and for a
zero-tolerance metric its ``violations`` and ``upper_bound_95``. Values are fractions 0-1.

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
EM_DASH = "—"
VERDICT_TEXT: Mapping[Verdict, str] = {
    Verdict.passed: "pass",
    Verdict.failed: "**fail**",
    Verdict.insufficient_evidence: "insufficient evidence",
    Verdict.report_only: "report only",
    Verdict.error: "**error**",
}


class Host(BaseModel):
    kind: str
    hostname: str
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
    sign = "≥" if metric.direction is Direction.at_least else "≤"
    return f"{sign} {percent(metric.floor)}"


def _measured_text(metric: MetricResult) -> str:
    """The per-seed value's mean ± SD: the good rate for a ≥ floor, the bad rate otherwise."""
    if metric.mean is None:
        return "no observations"
    spread = EM_DASH if metric.stdev is None else percent(metric.stdev)
    return f"{percent(metric.mean)} ± {spread}"


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
    }


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
- A model error during a case counts as a failed case; it is never excluded. Timings are
  informational: the model may share its machine with other work.
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
    host_bits = [host.kind, host.hostname]
    host_bits += [bit for bit in (host.hardware, host.server_version) if bit]
    endpoint = f"`{report.adapter}`" + (f" at `{host.endpoint}`" if host.endpoint else "")
    lines = [
        f"# Eval report {report.run_id}",
        "",
        f"{SUMMARY_PREFIX}{json.dumps(summary(report), sort_keys=True, ensure_ascii=False)} -->",
        "",
        f"- **Run verdict:** {VERDICT_TEXT[report.verdict]}",
        f"- **Model:** `{report.model}` (reported version: "
        f"{f'`{report.model_version}`' if report.model_version else EM_DASH})",
        f"- **Adapter:** {endpoint}",
        f"- **Fingerprint:** `{report.fingerprint}` ({report.fingerprint_profile}, with the model "
        "as every story purpose's)",
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
    return "\n".join(lines) + "\n"


# -- files and the index ------------------------------------------------------------------------


def read_summary(path: Path) -> dict[str, Any] | None:
    found = SUMMARY_RE.search(path.read_text(encoding="utf-8"))
    if found is None:
        return None
    loaded: dict[str, Any] = json.loads(found.group("json"))
    return loaded


def report_files(reports_dir: Path) -> list[Path]:
    """The committed reports: every Markdown file but the README and the index."""
    if not reports_dir.is_dir():
        return []
    return sorted(p for p in reports_dir.glob("*.md") if p.name not in (INDEX_NAME, README_NAME))


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
    "MetricResult",
    "Report",
    "SuiteResult",
    "index_text",
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
