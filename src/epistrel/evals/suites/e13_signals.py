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
  Spearman correlation (average ranks, no p-value) between the run's mean voice score and the
  mean rating, over the fingerprints both have; below five fingerprints it is
  ``insufficient_data`` (no value). An eval's own scratch stories are never rated, so this reads
  the ratings of the database the run uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from epistrel.core.feedback_reporting import FeedbackGroupBy
from epistrel.core.quality import signals
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.judging import script_score
from epistrel.evals.metrics import mean_ci, spearman, wilson
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
from epistrel.orchestrator.models.stub import StubAdapter

if TYPE_CHECKING:
    from epistrel.core.context import RequestContext
    from epistrel.engine import Engine
    from epistrel.evals.runner import CaseContext

DRIVER_WINDOW = 10
MIN_FINGERPRINTS = 5
CORRELATION = "nfr_qual_15.feedback_correlation"
COUNTED = ("repetition", "repeated_opening", "length_accuracy")
SCORED = ("voice_consistency", "driver_adherence")


def metric_id(config: str, signal: str) -> str:
    return f"nfr_qual_15.{config}.{signal}"


def unit(score: int) -> float:
    """A 1-5 score as 0-1."""
    return (score - 1) / 4


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
    reading_notes = (
        "E-13 (M4) reports signals, never a floor: voice fidelity and plot adherence are "
        "outside the guarantee (NFR-QUAL-13). Repetition, openings and length are computed "
        "without a model; the judge's scores are single-message scores, which have no "
        "calibration set in M4, so they are uncalibrated.",
    )

    def __init__(self) -> None:
        self.ratings: dict[str, float] = {}

    async def setup(self, engine: Engine, ctx: RequestContext) -> None:
        """Read users' mean rating per fingerprint (FR-FB-3) for the correlation."""
        found = await engine.feedback_aggregates(ctx, FeedbackGroupBy.FINGERPRINT)
        self.ratings = {
            g.key: g.mean_rating
            for g in found.groups
            if g.key is not None and g.mean_rating is not None
        }

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
            config = str(outcome.detail.get("config"))
            for signal in (*COUNTED, *SCORED):
                found.setdefault(metric_id(config, signal), []).extend(
                    v for v in outcome.detail.get(signal) or [] if v is not None
                )
        return found

    def correlation(self, outcomes: Sequence[Outcome], model: str) -> Value:
        """The Spearman correlation of the judge's mean voice score with users' mean rating, per
        fingerprint both have (``Value(None, n)`` below ``MIN_FINGERPRINTS``)."""
        from epistrel.evals.runner import run_fingerprint

        values = self._values(outcomes)
        judged: dict[str, float] = {}
        for config in CONFIGS:
            scores = values.get(metric_id(config, "voice_consistency")) or []
            if scores:
                key = run_fingerprint(model, CONFIG_DIALS[config])
                judged[key] = sum(scores) / len(scores)
        return feedback_correlation(judged, self.ratings)

    def aggregate(self, outcomes: Sequence[Outcome]) -> Mapping[str, Aggregate]:
        found: dict[str, Aggregate] = {}
        values = self._values(outcomes)
        model = str(next((o.detail.get("model") for o in outcomes), "") or "")
        for metric in self.metrics:
            if metric.id == CORRELATION:
                found[metric.id] = self.correlation(outcomes, model)
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
        found = self.correlation(outcomes, model)
        notes = [
            f"E-13 fingerprint `{config}`: `{_fingerprint(model, config)}`." for config in CONFIGS
        ]
        notes.append(
            "E-13 feedback correlation: "
            + (
                f"Spearman {found.value:.2f} over {found.n} fingerprints."
                if found.value is not None
                else f"insufficient data ({found.n} fingerprint(s) with both a judge score and "
                f"users' ratings; {MIN_FINGERPRINTS} needed)."
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
    "CORRELATION",
    "MIN_FINGERPRINTS",
    "E13Suite",
    "feedback_correlation",
    "metric_id",
    "unit",
]
