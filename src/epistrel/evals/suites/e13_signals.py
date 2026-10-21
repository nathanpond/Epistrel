"""E-13: text-quality signals (docs/04 §8; NFR-QUAL-15, FR-FB-3; #158). No gate: every row is
``report_only``. Voice fidelity and plot adherence are outside the guarantee (NFR-QUAL-13).

On the same intervention scenarios as E-12 (``suites.scenario_play``), per configuration
fingerprint (``structured``, ``gated``):

- **model-free** (``core.quality.signals``): ``nfr_qual_15.<config>.repetition``, the share of
  final messages whose word 3-grams overlap one of the previous three messages' by more than
  0.5 (Jaccard), undefined for a story's first two messages; ``…repeated_opening``, the share whose
  first five words repeat one of the previous three openings; ``…length_accuracy``, the share
  within ±25 % of the requested length's word band (``short`` 40-80, ``medium`` 80-200,
  ``long`` 200-400), messages with no requested length left out;
- **judge-scored** (``quality_judge``'s ``score`` variant, 1-5, reported as ``(score - 1) / 4``
  so 0-1 like every other row, with a normal 95 % interval): ``…voice_consistency``, each final
  message against Bobby's definition (``voice_fits_definition``); ``…driver_adherence``, per
  story, its last ten messages against its drivers (hooks, objectives, planned reveals;
  ``story_driver_adherence``). A single-message score has no calibration set in M4, so these
  rows state ``uncalibrated``;
- ``nfr_qual_15.feedback_correlation``: where users' ratings exist for a fingerprint
  (``Engine.feedback_aggregates(group_by=fingerprint)``, read once in ``setup``; FR-FB-3), the
  Spearman correlation (average ranks, no p-value) between the judge's mean voice score and the
  mean rating, over the fingerprints both have; below five fingerprints it is
  ``insufficient_data`` (no value). An eval's own scratch stories are never rated, so this reads
  the ratings of the database the run uses.

  One run judges only two fingerprints, so the judge's means are **pooled across the committed
  reports** (#463): each report's embedded summary lists its judge-based rows per fingerprint
  (``report.judged_means``; E-13 tags its rows with their fingerprint), ``setup`` reads the
  voice rows of every report in ``reports_dir`` (``docs/evals``, the files ``INDEX.md`` is built
  from), and the correlation pools them with this run's own, weighted by n. A report committed
  before #463 has no such list and adds nothing. The judge's voice scores are uncalibrated in
  every run (no calibration set in M4), so the pooled means are too. Only the means of the
  run's own judge are pooled (#475): each row names its judge (a row without one was judged by
  its report's model), since two judges' scores are not on one scale.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from epistrel.core.feedback_reporting import FeedbackGroupBy
from epistrel.core.quality import signals
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.judging import script_score
from epistrel.evals.metrics import mean_ci, spearman, wilson
from epistrel.evals.report import read_summary, report_files
from epistrel.evals.suites import (
    Aggregate,
    CaseModel,
    Counts,
    Outcome,
    Suite,
    Summary,
    Value,
    register,
)
from epistrel.evals.suites.e12_interventions import MIN_SCENARIOS, SET_PATH
from epistrel.evals.suites.scenario_play import (
    CONFIG_DIALS,
    CONFIGS,
    DEFINITION,
    ScenarioCase,
    drivers_text,
    play,
    script,
)
from epistrel.orchestrator.models.registry import QUALITY_JUDGE
from epistrel.orchestrator.models.stub import StubAdapter

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext

DRIVER_WINDOW = 10
MIN_FINGERPRINTS = 5
CORRELATION = "nfr_qual_15.feedback_correlation"
COMPLETED = "e13.completed"
"""The share of scenario cases that played to the end: a model error fails its case (#134's
rule) and is counted here, never dropped silently."""
COUNTED = ("repetition", "repeated_opening", "length_accuracy")
SCORED = ("voice_consistency", "driver_adherence")


def metric_id(config: str, signal: str) -> str:
    return f"nfr_qual_15.{config}.{signal}"


def unit(score: int) -> float:
    """A 1-5 score as 0-1."""
    return (score - 1) / 4


type Pooled = dict[str, tuple[float, int]]
"""Fingerprint -> (sum of the judge's 0-1 voice scores, how many)."""


def pool(into: Pooled, fingerprint: str, mean: float, n: int) -> None:
    """Add ``n`` scores of mean ``mean`` to ``fingerprint``'s pool."""
    total, count = into.get(fingerprint, (0.0, 0))
    into[fingerprint] = (total + mean * n, count + n)


def committed_voice_means_by_judge(reports_dir: Path) -> dict[str, Pooled]:
    """The judge's voice-consistency means per fingerprint in the committed reports' embedded
    summaries (#463), pooled by n, per judge model (#475: a row's ``judge``, else its report's
    ``model``); a report without them adds nothing."""
    found: dict[str, Pooled] = {}
    for path in report_files(reports_dir):
        summary = read_summary(path) or {}
        for row in _voice_rows(summary.get("judged") or []):
            judge = str(row.get("judge") or summary.get("model") or "")
            pool(found.setdefault(judge, {}), row["fingerprint"], float(row["mean"]), int(row["n"]))
    return found


def committed_voice_means(reports_dir: Path, judge: str | None = None) -> Pooled:
    """``committed_voice_means_by_judge`` for ``judge``, or every judge's pooled (``None``)."""
    by_judge = committed_voice_means_by_judge(reports_dir)
    if judge is not None:
        return dict(by_judge.get(judge, {}))
    found: Pooled = {}
    for pooled in by_judge.values():
        for key, (total, n) in pooled.items():
            pool(found, key, total / n, n)
    return found


def _judge_of(outcomes: Sequence[Outcome], model: str) -> str:
    """The model that judged ``outcomes`` (#475): the cases' ``judge``, else the run model."""
    found = next((o.detail.get("judge") for o in outcomes if o.detail.get("judge")), None)
    return str(found or model)


def _voice_rows(rows: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    for row in rows:
        metric = str(row.get("metric", ""))
        if (
            metric.startswith("nfr_qual_15.")
            and metric.endswith(".voice_consistency")
            and isinstance(row.get("fingerprint"), str)
            and isinstance(row.get("mean"), int | float)
            and isinstance(row.get("n"), int)
            and row["n"] > 0
        ):
            yield row


def feedback_correlation(judged: Mapping[str, float], ratings: Mapping[str, float]) -> Value:
    """Spearman's rho (average ranks, no p-value) between the judge's mean score and users'
    mean rating over the fingerprints both have; ``Value(None, n)`` (``insufficient_data``)
    below ``MIN_FINGERPRINTS`` of them, or when either side is constant."""
    shared = sorted(set(judged) & set(ratings))
    if len(shared) < MIN_FINGERPRINTS:
        return Value(None, len(shared))
    return Value(spearman([judged[k] for k in shared], [ratings[k] for k in shared]), len(shared))


def _metrics() -> tuple[Metric, ...]:
    described = {
        "repetition": (
            Direction.at_most,
            "messages repeating one of the previous three (3-gram Jaccard > 0.5)",
        ),
        "repeated_opening": (Direction.at_most, "messages opening like one of the previous three"),
        "length_accuracy": (Direction.at_least, "messages within ±25% of the requested length"),
        "voice_consistency": (
            Direction.at_least,
            "judge: the speech fits the definition, (score - 1) / 4, uncalibrated",
        ),
        "driver_adherence": (
            Direction.at_least,
            "judge: the last ten messages follow the story drivers, (score - 1) / 4, uncalibrated",
        ),
    }
    found = [
        Metric(metric_id(config, signal), Rule.informational, None, direction, f"{config}: {text}")
        for config in CONFIGS
        for signal, (direction, text) in described.items()
    ]
    found.append(
        Metric(
            CORRELATION,
            Rule.informational,
            None,
            Direction.at_least,
            "Spearman correlation of the judge's mean voice score with users' mean rating per "
            "fingerprint (insufficient data below five fingerprints)",
        )
    )
    found.append(
        Metric(
            COMPLETED,
            Rule.informational,
            None,
            Direction.at_least,
            "scenario cases that played to the end (a model error fails its case)",
        )
    )
    return tuple(found)


@register
class E13Suite(Suite):
    name = "e13"
    title = "E-13 (M4): text-quality signals (no gate)"
    set_path = SET_PATH
    min_cases = MIN_SCENARIOS
    case_model = ScenarioCase
    metrics = _metrics()
    judge_metrics = frozenset({metric_id(c, s) for c in CONFIGS for s in SCORED} | {CORRELATION})
    judge_calibration = "none"
    judge_purposes = (QUALITY_JUDGE.name,)
    reading_notes = (
        "E-13 (M4) reports signals, never a floor: voice fidelity and plot adherence are "
        "outside the guarantee (NFR-QUAL-13). Repetition, openings and length are computed "
        "without a model; the judge's scores are single-message scores, which have no "
        "calibration set in M4, so they are uncalibrated.",
    )

    def __init__(self, reports_dir: Path | None = None) -> None:
        from epistrel.evals.check import REPORTS_DIR

        self.ratings: dict[str, float] = {}
        self.reports_dir = REPORTS_DIR if reports_dir is None else reports_dir
        """Where the committed reports are, whose judge means the correlation pools (#463)."""
        self.committed: dict[str, Pooled] = {}
        """The committed reports' voice means per judge model, then per fingerprint (#475)."""

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        """Read users' mean rating per fingerprint (FR-FB-3), and the committed reports'
        judge means per fingerprint (#463), for the correlation."""
        found = await engine.feedback_aggregates(ctx, FeedbackGroupBy.FINGERPRINT)
        self.ratings = {
            g.key: g.mean_rating
            for g in found.groups
            if g.key is not None and g.mean_rating is not None
        }
        self.committed = committed_voice_means_by_judge(self.reports_dir)

    def committed_for(self, judge: str) -> Pooled:
        """The committed voice means that ``judge`` scored (#475)."""
        return self.committed.get(judge, {})

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, ScenarioCase)  # noqa: S101 - load() validated it
        played = await play(ctx, case)
        finals = [t.final for t in played.turns]
        repetition = [signals.repetition(m, finals[:i]) for i, m in enumerate(finals)]
        openings = [signals.repeated_opening(m, finals[:i]) for i, m in enumerate(finals)]
        lengths = [signals.within_length(m, case.length) for m in finals]
        voice: list[int | None] = []
        for turn in played.turns:
            voice.append(
                await ctx.judge_score(
                    played.story_id,
                    turn.final,
                    DEFINITION,
                    "voice_fits_definition",
                    key=f"t{turn.index}.voice",
                )
            )
        driver = await ctx.judge_score(
            played.story_id,
            "\n\n".join(finals[-DRIVER_WINDOW:]),
            drivers_text(case.drivers),
            "story_driver_adherence",
            key="drivers",
        )
        detail: dict[str, Any] = {
            "seed": ctx.seed,
            "model": ctx.model,
            "judge": ctx.judge_model,
            "config": case.config,
            "repetition": repetition,
            "repeated_opening": openings,
            "length_accuracy": lengths,
            "voice_consistency": voice,
            "driver_adherence": [driver],
        }
        return Outcome({}, output=finals[-1] if finals else "", detail=detail)

    def _values(self, outcomes: Sequence[Outcome]) -> dict[str, list[Any]]:
        found: dict[str, list[Any]] = {}
        for outcome in outcomes:
            if outcome.detail.get("config") not in CONFIGS:
                continue  # a model error failed the case: counted by ``COMPLETED``
            config = str(outcome.detail.get("config"))
            for signal in (*COUNTED, *SCORED):
                found.setdefault(metric_id(config, signal), []).extend(
                    v for v in outcome.detail.get(signal) or [] if v is not None
                )
        return found

    def own_voice_means(self, outcomes: Sequence[Outcome], model: str) -> Pooled:
        """This run's judge voice scores (0-1) per configuration fingerprint."""
        values = self._values(outcomes)
        found: Pooled = {}
        for config in CONFIGS:
            scores = [unit(v) for v in values.get(metric_id(config, "voice_consistency")) or []]
            if scores:
                pool(found, _fingerprint(model, config), sum(scores) / len(scores), len(scores))
        return found

    def pooled_voice_means(self, outcomes: Sequence[Outcome], model: str) -> dict[str, float]:
        """The judge's mean voice score per fingerprint: this run's pooled with the committed
        reports' that the same judge scored (#463, #475), weighted by n."""
        pooled = dict(self.committed_for(_judge_of(outcomes, model)))
        for key, (total, n) in self.own_voice_means(outcomes, model).items():
            pool(pooled, key, total / n, n)
        return {key: total / n for key, (total, n) in pooled.items() if n}

    def correlation(self, outcomes: Sequence[Outcome], model: str) -> Value:
        """The Spearman correlation of the judge's mean voice score (pooled across the run and
        the committed reports, #463) with users' mean rating, per fingerprint both have
        (``Value(None, n)`` below ``MIN_FINGERPRINTS``)."""
        return feedback_correlation(self.pooled_voice_means(outcomes, model), self.ratings)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        found: dict[str, Aggregate] = {}
        values = self._values(outcomes)
        model = str(next((o.detail.get("model") for o in outcomes), "") or "")
        for metric in self.metrics:
            if metric.id == CORRELATION:
                found[metric.id] = self.correlation(outcomes, model)
                continue
            if metric.id == COMPLETED:
                played = sum(o.detail.get("config") in CONFIGS for o in outcomes)
                found[metric.id] = Counts(played, len(outcomes) - played)
                continue
            seen = values.get(metric.id) or []
            signal = metric.id.rsplit(".", 1)[1]
            if signal in SCORED:
                scores = [unit(v) for v in seen]
                found[metric.id] = Value(sum(scores) / len(scores) if scores else None, len(seen))
            else:
                hits = sum(bool(v) for v in seen)
                good, bad = (
                    (len(seen) - hits, hits)
                    if signal != "length_accuracy"
                    else (
                        hits,
                        len(seen) - hits,
                    )
                )
                found[metric.id] = Counts(good, bad)
        return found

    def summarize(self, outcomes: Sequence[Outcome], model: str) -> Summary:
        values = self._values(outcomes)
        metrics: dict[str, dict[str, Any]] = {}
        for metric_key, seen in values.items():
            signal = metric_key.rsplit(".", 1)[1]
            if signal in SCORED:
                interval = mean_ci([unit(v) for v in seen])
            else:
                hits = sum(bool(v) for v in seen)
                interval = wilson(hits, len(seen))
            if interval is not None:
                metrics[metric_key] = {"ci95": list(interval)}
        for config in CONFIGS:
            for signal in SCORED:
                # #463: the judged rows name their fingerprint, so a later run can pool them.
                fields = dict(metrics.get(metric_id(config, signal), {}))
                fields["fingerprint"] = _fingerprint(model, config)
                metrics[metric_id(config, signal)] = fields
        found = self.correlation(outcomes, model)
        own = set(self.own_voice_means(outcomes, model))
        rated = set(self.ratings)
        from_run = len(own & rated)
        committed = self.committed_for(_judge_of(outcomes, model))
        from_reports = len((set(committed) - own) & rated)
        notes = [
            f"E-13 fingerprint `{config}`: `{_fingerprint(model, config)}`." for config in CONFIGS
        ]
        sources = (
            f"{from_run} from this run, {from_reports} more from {len(committed)} "
            "fingerprint(s) judged in the committed reports"
        )
        notes.append(
            "E-13 feedback correlation: "
            + (
                f"Spearman {found.value:.2f} over {found.n} fingerprints ({sources})."
                if found.value is not None
                else f"insufficient data ({found.n} fingerprint(s) with both a judge score and "
                f"users' ratings: {sources}; {MIN_FINGERPRINTS} needed)."
            )
        )
        return Summary(metrics=metrics, notes=notes)

    def script_stub(self, case: CaseModel, stub: StubAdapter) -> None:
        assert isinstance(case, ScenarioCase)  # noqa: S101 - load() validated it
        script(case, stub)
        for index, turn in enumerate(case.turns, start=1):
            script_score(stub, f"{case.id}.t{index}.voice", turn.stub_voice)
        script_score(stub, f"{case.id}.drivers", case.stub_driver)


def _fingerprint(model: str, config: str) -> str:
    from epistrel.evals.runner import run_fingerprint

    return run_fingerprint(model, CONFIG_DIALS[config])  # type: ignore[index]


__all__ = [
    "COMPLETED",
    "CORRELATION",
    "MIN_FINGERPRINTS",
    "E13Suite",
    "committed_voice_means",
    "committed_voice_means_by_judge",
    "feedback_correlation",
    "metric_id",
    "pool",
    "unit",
]
