"""P-04's gate and report (NFR-PERF-3,4; #76).

``evaluate`` turns a run's raw material (the driver's turn records, the sampler's samples, the
``/metrics`` stage histogram diffed over the run, and the ``history`` reconciliation) into
per-story and aggregate figures and the PASS/FAIL of every criterion of NFR-PERF-4's Lite
variant; ``markdown`` renders the report. Both are pure, so the gate logic is tested without a
stack (``tests/perf/test_p_04.py``).

**Aggregate criteria:** turn-commit p95 ≤ 250 ms (NFR-PERF-3), read from the server's
``commit`` stage histogram (250 ms is a bucket edge); no failed request; no lost or duplicated
turn. **Per story, each separately:** every turn submitted at least 2 minutes before the end
completed; after the first 5 minutes, never more than one turn waiting behind the one in
progress (from the driver's outstanding turns); no turn waiting longer than one interval (15 s)
to reach the front of its queue (``queued_ms``); queue-front-to-first-model-call p95 ≤ 1 s
(``first_call_offset_ms``). Engine-only turn time (``elapsed_ms`` minus the model calls'
latency) is reported per story and in aggregate.

**The structured variant** (#153, ``RunInfo.scenario = "structured"``, one turn every 60 s) has
the same criteria plus two aggregate budgets, read from the server's stage histogram like the
commit's: retrieval p95 ≤ 150 ms (NFR-PERF-1, the ``retrieve`` stage, one observation per
retrieval) and deterministic verify p95 ≤ 50 ms (NFR-PERF-2, the ``verify`` stage, one per
claim checked). Both bounds are bucket edges. Its report is titled "P-04 Structured reference
workload" and saved as ``<date>-<time>-structured.md``.

**The multi-character variant** (#182, ``scenario = "multi"``, one two-character
``sequential`` turn every 60 s on the ``Structured`` profile's per-character renders) has the
structured variant's criteria plus, in aggregate: every committed turn carries both primaries'
beats (the driver counts the beats of each primary in the viewpoint's result), no turn reports
``reduced_characters``, ``deferred_characters`` or ``escalation_deferred`` (a run with any fails
however fast it was), and the per-character stages ``MULTI_STAGES`` (``context``: one
character's context, once per render; ``compose``: the reconciliation, the audience filter and
the composition, once per turn) were observed, read as the report's ``context_ms`` and
``compose_ms`` (``MULTI_COLUMNS``). Degradation steps are counted by name; any other than the
stub's own deterministic composition (``STUB_DEGRADATION``) fails the run on the reference host
only. The report names the profile and version the turns ran under and the perception level
their beats were filtered at, both from the respond results. A ``smoke`` run (PR CI's
``perf_smoke``) scales the warm-up and the end margin to 30 s.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from epistrel.observability.stages import MULTI_STAGES
from perf.lite_smoke import (
    COMMIT_P95_BUDGET_S,
    ENFORCED_HOST_CLASS,
    FRONT_TO_FIRST_CALL_P95_S,
    MAX_WAITING,
    RETRIEVAL_P95_BUDGET_S,
    SMOKE_MARGIN_S,
    STOP_BEFORE_S,
    VERIFY_P95_BUDGET_S,
    WARMUP_S,
)

PASS, FAIL = "PASS", "FAIL"
TITLE = re.compile(
    r"^# P-04 (?:Lite|Structured|Multi-character) reference workload: "
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC$",
    re.M,
)
SCENARIO = re.compile(r"^# P-04 (Lite|Structured|Multi-character) reference workload: ", re.M)
SCENARIO_TITLES = {"lite": "Lite", "structured": "Structured", "multi": "Multi-character"}
TITLE_SCENARIOS = {title: scenario for scenario, title in SCENARIO_TITLES.items()}
MULTI_COLUMNS = {f"{stage}_ms": stage for stage in MULTI_STAGES}
"""The multi report's per-character columns (``context_ms``, ``compose_ms``) and the server
stage each reads: ``observability.stages.MULTI_STAGES``, the registry the pipeline observes
(#182; ``test_p_04__multi_gate`` cross-checks the names)."""
STUB_DEGRADATION = frozenset({"composition_fallback"})
"""The degradation the stub makes on every structured turn: its polish echoes the draft with
untraced spans, so the composition check takes the deterministic composition."""
PLANNING_ENTRIES = ("reduced_characters", "deferred_characters", "escalation_deferred")
"""The budget-planning entries a multi run must never see (#176): a primary reduced to reactive
narration or deferred, or a reaction that needed a render it could not get."""
VERDICT = re.compile(r"^\*\*Result: (PASS|FAIL)\*\* \(host class ([a-z]+): ", re.M)
LENGTH = re.compile(r"`turn_concurrency = queue`, (\d+(?:\.\d+)?) minutes")
SIZE = re.compile(r"^- Stories: ([\d,]+) turns ", re.M)


@dataclass(frozen=True, slots=True)
class Header:
    """What a committed report says about itself: when it ran, for how long, on which host
    class, and its verdict (``test_p_04`` reads it)."""

    started: datetime
    minutes: float
    host_class: str
    verdict: str
    turns: int
    scenario: str = "lite"


def header(text: str) -> Header:
    """Parse a report's header; ``ValueError`` when it lacks its date, length or host class."""
    title, verdict, length = TITLE.search(text), VERDICT.search(text), LENGTH.search(text)
    size, scenario = SIZE.search(text), SCENARIO.search(text)
    if title is None or verdict is None or length is None or size is None or scenario is None:
        raise ValueError("not a P-04 report: no date, verdict, host class or run length")
    return Header(
        started=datetime.strptime(title.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC),
        minutes=float(length.group(1)),
        host_class=verdict.group(2),
        verdict=verdict.group(1),
        turns=int(size.group(1).replace(",", "")),
        scenario=TITLE_SCENARIOS[scenario.group(1)],
    )


@dataclass(frozen=True, slots=True)
class RunInfo:
    """What was run, where."""

    started: str
    """ISO time (UTC) the run started."""
    minutes: float
    interval_s: float
    stories: int
    host_class: str
    host: str
    engine_sha: str
    stub_latency_s: float
    turns: int = 10_000
    """Seeded turns per story (two events each)."""
    scenario: str = "lite"
    """``lite`` or ``structured`` (#153)."""
    events: int | None = None
    """Seeded events per story when not two per turn (the structured stories' canon adds one
    ``fact_committed`` event per fact)."""
    memories: int = 0
    """Seeded memories per story (the structured variant)."""
    smoke: bool = False
    """PR CI's ``perf_smoke`` run (#182): the warm-up and the end margin are 30 s."""
    profile: str | None = None
    """The engine profile the turns ran under, ``<name> v<version>`` (the multi variant)."""
    perception: str | None = None
    """The perception level the viewpoint's beats were filtered at (the multi variant)."""
    beliefs: int = 0
    exposures: int = 0
    """The multi variant's knowledge per character (``epistrel.testing.fixtures.knowledge``)."""

    @property
    def end_s(self) -> float:
        return self.minutes * 60

    @property
    def warmup_s(self) -> float:
        """When the backlog criterion starts applying."""
        return SMOKE_MARGIN_S if self.smoke else WARMUP_S

    @property
    def stop_before_s(self) -> float:
        """How long before the end submissions stop (each turn before it must complete)."""
        return SMOKE_MARGIN_S if self.smoke else STOP_BEFORE_S

    @property
    def enforced(self) -> bool:
        return self.host_class == ENFORCED_HOST_CLASS


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    scope: str
    measured: str
    bound: str
    passed: bool

    @property
    def result(self) -> str:
        return PASS if self.passed else FAIL


@dataclass
class Turn:
    """One submitted turn, its submit and (if it settled) finish records merged."""

    story_id: str
    tick: int
    submitted: float
    finished: float | None = None
    outcome: str | None = None
    status: int | None = None
    error: str | None = None
    elapsed_ms: int | None = None
    queued_ms: int | None = None
    first_call_offset_ms: int | None = None
    model_ms: int | None = None
    primary_beats: dict[str, int] | None = None
    """The multi variant: each named primary's beats in the viewpoint's result."""
    planning: dict[str, int] = field(default_factory=dict)
    """The multi variant: the turn's ``PLANNING_ENTRIES`` counts (only those it had)."""
    degradation: list[str] = field(default_factory=list)
    profile: str | None = None
    audience_filter: str | None = None

    @property
    def committed(self) -> bool:
        return self.outcome == "committed"

    @property
    def engine_ms(self) -> int | None:
        if self.elapsed_ms is None or self.model_ms is None:
            return None
        return max(0, self.elapsed_ms - self.model_ms)


@dataclass
class StoryFigures:
    story_id: str
    index: int
    offered: int = 0
    completed: int = 0
    failed: int = 0
    due: int = 0
    """Turns submitted at least 2 minutes before the end."""
    due_completed: int = 0
    lost: list[int] = field(default_factory=list)
    duplicated: list[int] = field(default_factory=list)
    max_waiting: int = 0
    queue_wait_p95_ms: float | None = None
    queue_wait_max_ms: float | None = None
    front_p95_ms: float | None = None
    engine_ms: dict[str, float | None] = field(default_factory=dict)
    max_queue_gauge: float = 0.0
    criteria: list[Criterion] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.criteria)


@dataclass
class Evaluation:
    info: RunInfo
    stories: list[StoryFigures]
    aggregate: list[Criterion]
    figures: dict[str, Any]
    stages: dict[str, dict[str, float | None]]
    minutes: list[dict[str, Any]]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.aggregate) and all(s.passed for s in self.stories)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank ``q``-th percentile; ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * q / 100))
    return float(ordered[rank - 1])


def merge(records: Iterable[Mapping[str, Any]]) -> list[Turn]:
    """The driver's ``submit``/``finish`` lines as one ``Turn`` per (story, tick) submission."""
    turns: dict[tuple[str, int], Turn] = {}
    for record in records:
        key = (str(record["story_id"]), int(record["tick"]))
        turn = turns.get(key)
        if turn is None:
            turn = turns[key] = Turn(key[0], key[1], float(record["submitted"]))
        if record.get("event") == "finish":
            turn.finished = float(record["finished"])
            turn.outcome = str(record["outcome"])
            turn.status = record.get("status")
            turn.error = record.get("error")
            turn.elapsed_ms = record.get("elapsed_ms")
            turn.queued_ms = record.get("queued_ms")
            turn.first_call_offset_ms = record.get("first_call_offset_ms")
            turn.model_ms = record.get("model_ms")
            turn.primary_beats = record.get("primary_beats")
            turn.planning = dict(record.get("planning") or {})
            turn.degradation = list(record.get("degradation") or [])
            turn.profile = record.get("profile")
            turn.audience_filter = record.get("audience_filter")
    return sorted(turns.values(), key=lambda t: (t.story_id, t.tick))


def max_waiting(turns: Sequence[Turn], *, after: float, end: float) -> int:
    """The most turns ever waiting behind the one in progress, from ``after`` on: at every
    submission, the outstanding (submitted, not yet settled) turns less the one running."""
    worst = 0
    for moment in sorted(t.submitted for t in turns if after <= t.submitted <= end):
        outstanding = sum(
            1
            for t in turns
            if t.submitted <= moment and (t.finished is None or t.finished > moment)
        )
        worst = max(worst, outstanding - 1)
    return worst


def histogram_quantile(buckets: Mapping[str, float], q: float) -> float | None:
    """The upper edge of the bucket holding the ``q``-th percentile (seconds); ``None`` when
    nothing was observed, ``inf`` when it is past the last edge."""
    edges = sorted(((float("inf") if le == "+Inf" else float(le)), n) for le, n in buckets.items())
    total = edges[-1][1] if edges else 0.0
    if total <= 0:
        return None
    for edge, n in edges:
        if n >= total * q / 100:
            return edge
    return float("inf")


def share_within(buckets: Mapping[str, float], bound_s: float) -> float | None:
    """The share of observations at or under ``bound_s`` (a bucket edge)."""
    total = buckets.get("+Inf", 0.0)
    if total <= 0:
        return None
    within = next((n for le, n in buckets.items() if le != "+Inf" and float(le) == bound_s), None)
    if within is None:
        raise ValueError(f"{bound_s} s is not a bucket edge")
    return within / total


def _budget(
    stage_buckets: Mapping[str, Mapping[str, float]],
    stage: str,
    name: str,
    bound_s: float,
    what: str,
) -> Criterion:
    """An aggregate p95 budget on a server stage: at least 95% of its observations at or under
    ``bound_s`` (a bucket edge). Nothing observed fails: the run did not exercise it."""
    buckets = dict(stage_buckets.get(stage, {}))
    within = share_within(buckets, bound_s) if buckets else None
    ms = f"{bound_s * 1000:g} ms"
    return Criterion(
        name,
        "aggregate",
        f"{within:.1%} of {int(buckets.get('+Inf', 0))} {what} ≤ {ms}"
        if within is not None
        else f"no {what} observed",
        f"≥ 95% ≤ {ms}",
        within is not None and within >= 0.95,
    )


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def _span(seconds: float) -> str:
    """``2 min``, ``5 min`` or ``30 s``."""
    return f"{seconds / 60:g} min" if seconds % 60 == 0 else f"{seconds:g} s"


def _story(
    story_id: str,
    index: int,
    turns: Sequence[Turn],
    info: RunInfo,
    reconciled: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
) -> StoryFigures:
    figures = StoryFigures(story_id, index)
    figures.offered = len(turns)
    committed = [t for t in turns if t.committed and t.finished is not None]
    figures.completed = len(committed)
    figures.failed = sum(1 for t in turns if t.outcome is not None and not t.committed)
    cutoff = info.end_s - info.stop_before_s
    due = [t for t in turns if t.submitted <= cutoff]
    figures.due = len(due)
    figures.due_completed = sum(
        1 for t in due if t.committed and t.finished is not None and t.finished <= info.end_s
    )
    figures.lost = list(reconciled.get("lost", []))
    figures.duplicated = list(reconciled.get("duplicated", []))
    figures.max_waiting = max_waiting(turns, after=info.warmup_s, end=info.end_s)
    waits = [float(t.queued_ms or 0) for t in committed]
    figures.queue_wait_p95_ms = percentile(waits, 95)
    figures.queue_wait_max_ms = max(waits) if waits else None
    fronts = [
        float(t.first_call_offset_ms) for t in committed if t.first_call_offset_ms is not None
    ]
    figures.front_p95_ms = percentile(fronts, 95)
    engine = [float(t.engine_ms) for t in committed if t.engine_ms is not None]
    figures.engine_ms = {f"p{q}": percentile(engine, q) for q in (50, 95, 99)}
    figures.max_queue_gauge = max(
        (float(s.get("queue", {}).get(story_id, 0.0)) for s in samples if s["t"] >= info.warmup_s),
        default=0.0,
    )
    interval_ms = info.interval_s * 1000
    figures.criteria = [
        Criterion(
            f"every turn submitted ≥ {_span(info.stop_before_s)} before the end completed",
            "story",
            f"{figures.due_completed}/{figures.due}",
            "all",
            figures.due > 0 and figures.due_completed == figures.due,
        ),
        Criterion(
            f"turns waiting behind the running one, after {_span(info.warmup_s)}",
            "story",
            str(figures.max_waiting),
            f"≤ {MAX_WAITING}",
            figures.max_waiting <= MAX_WAITING,
        ),
        Criterion(
            "longest wait to reach the queue's front",
            "story",
            _ms(figures.queue_wait_max_ms),
            f"≤ {interval_ms:.0f} ms",
            figures.queue_wait_max_ms is not None and figures.queue_wait_max_ms <= interval_ms,
        ),
        Criterion(
            "queue front to first model call, p95",
            "story",
            _ms(figures.front_p95_ms),
            f"≤ {FRONT_TO_FIRST_CALL_P95_S * 1000:.0f} ms",
            figures.front_p95_ms is not None
            and figures.front_p95_ms <= FRONT_TO_FIRST_CALL_P95_S * 1000,
        ),
    ]
    return figures


def _per_minute(
    samples: Sequence[Mapping[str, Any]], stories: Sequence[tuple[str, int]], info: RunInfo
) -> list[dict[str, Any]]:
    """Per minute of the run: the most turns waiting per story (the engine's gauge), and the
    database connections, CPU and memory (max of the 5 s samples)."""
    rows: list[dict[str, Any]] = []
    by_minute: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_minute[int(sample["t"] // 60)].append(sample)
    for minute in range(math.ceil(info.minutes)):
        group = by_minute.get(minute, [])
        row: dict[str, Any] = {"minute": minute + 1, "samples": len(group)}
        row["queue"] = [
            max((float(s.get("queue", {}).get(sid, 0.0)) for s in group), default=0.0)
            for sid, _ in stories
        ]
        row["db_connections"] = max((s.get("db_connections", 0) for s in group), default=0)
        for service in ("engine", "db"):
            row[f"{service}_cpu"] = max(
                (s.get("containers", {}).get(service, {}).get("cpu", 0.0) for s in group),
                default=0.0,
            )
            row[f"{service}_mem_mib"] = max(
                (s.get("containers", {}).get(service, {}).get("mem_mib", 0.0) for s in group),
                default=0.0,
            )
        rows.append(row)
    return rows


def degradation_counts(turns: Sequence[Turn]) -> dict[str, int]:
    """Every degradation step of the committed turns, by name."""
    found: dict[str, int] = defaultdict(int)
    for turn in turns:
        for step in turn.degradation:
            found[step] += 1
    return dict(sorted(found.items()))


def multi_criteria(
    info: RunInfo,
    committed: Sequence[Turn],
    stage_buckets: Mapping[str, Mapping[str, float]],
    incomplete: int = 0,
) -> list[Criterion]:
    """The multi variant's aggregate criteria (see the module docstring). ``incomplete``: the
    committed turns whose ``history`` lacks a primary's beat (the run's reconciliation)."""
    short = [
        t
        for t in committed
        if t.primary_beats is None
        or len(t.primary_beats) < 2
        or any(n < 1 for n in t.primary_beats.values())
    ]
    planning: dict[str, int] = defaultdict(int)
    for turn in committed:
        for name, n in turn.planning.items():
            planning[name] += n
    steps = degradation_counts(committed)
    other = {k: v for k, v in steps.items() if k not in STUB_DEGRADATION}
    unobserved = [
        column
        for column, stage in MULTI_COLUMNS.items()
        if dict(stage_buckets.get(stage, {})).get("+Inf", 0.0) <= 0
    ]
    return [
        Criterion(
            "both primaries' beats in every committed turn",
            "aggregate",
            f"{len(committed) - len(short)}/{len(committed)} in the results, "
            f"{incomplete} incomplete in history",
            "all",
            bool(committed) and not short and not incomplete,
        ),
        Criterion(
            "reduced, deferred or escalation-deferred characters",
            "aggregate",
            ", ".join(f"{k} {planning.get(k, 0)}" for k in PLANNING_ENTRIES),
            "0",
            not any(planning.values()),
        ),
        Criterion(
            "per-character stages observed (context_ms, compose_ms)",
            "aggregate",
            "missing " + ", ".join(unobserved) if unobserved else "both",
            "both",
            not unobserved,
        ),
        Criterion(
            "degradation steps beyond the stub's composition fallback",
            "aggregate",
            ", ".join(f"{k} {v}" for k, v in other.items()) or "none",
            "none (gated on the reference host only)",
            not other or not info.enforced,
        ),
    ]


def multi_figures(
    committed: Sequence[Turn], stages: Mapping[str, Mapping[str, float | None]]
) -> dict[str, Any]:
    """The multi variant's own figures: the per-character columns, the degradation counts and
    what the turns ran under."""
    return {
        **{column: dict(stages.get(stage, {})) for column, stage in MULTI_COLUMNS.items()},
        "degradation": degradation_counts(committed),
        "profiles": sorted({t.profile for t in committed if t.profile}),
        "audience_filters": sorted({t.audience_filter for t in committed if t.audience_filter}),
    }


def evaluate(
    info: RunInfo,
    stories: Sequence[tuple[str, int]],
    records: Iterable[Mapping[str, Any]],
    samples: Sequence[Mapping[str, Any]],
    stage_buckets: Mapping[str, Mapping[str, float]],
    reconciled: Mapping[str, Mapping[str, Any]],
) -> Evaluation:
    """Every figure and criterion of the run (see the module docstring). ``stories`` is
    ``(story_id, index)``; ``reconciled`` maps a story to its ``lost``/``duplicated`` ticks."""
    turns = merge(records)
    by_story: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        by_story[turn.story_id].append(turn)
    per_story = [
        _story(sid, index, by_story.get(sid, []), info, reconciled.get(sid, {}), samples)
        for sid, index in stories
    ]
    commit = dict(stage_buckets.get("commit", {}))
    within = share_within(commit, COMMIT_P95_BUDGET_S) if commit else None
    committed = [t for t in turns if t.committed]
    failed = [t for t in turns if t.outcome is not None and not t.committed]
    lost = sum(len(s.lost) for s in per_story)
    duplicated = sum(len(s.duplicated) for s in per_story)
    stages = {
        stage: {
            "count": buckets.get("+Inf", 0.0),
            **{f"p{q}": histogram_quantile(buckets, q) for q in (50, 95, 99)},
        }
        for stage, buckets in sorted(stage_buckets.items())
    }
    commit_p95 = stages.get("commit", {}).get("p95")
    aggregate = [
        Criterion(
            "turn commit p95 (server commit stage)",
            "aggregate",
            f"{within:.1%} of {int(commit.get('+Inf', 0))} commits ≤ 250 ms"
            if within is not None
            else "no commits observed",
            "≥ 95% ≤ 250 ms",
            within is not None and within >= 0.95,
        ),
        Criterion(
            "failed requests",
            "aggregate",
            str(len(failed)),
            "0",
            not failed,
        ),
        Criterion(
            "lost or duplicated turns",
            "aggregate",
            f"{lost} lost, {duplicated} duplicated",
            "0",
            lost == 0 and duplicated == 0,
        ),
    ]
    if info.scenario in ("structured", "multi"):
        aggregate += [
            _budget(
                stage_buckets,
                "retrieve",
                "retrieval p95 (server retrieve stage)",
                RETRIEVAL_P95_BUDGET_S,
                "retrievals",
            ),
            _budget(
                stage_buckets,
                "verify",
                "deterministic verify p95 (server verify stage)",
                VERIFY_P95_BUDGET_S,
                "claims checked",
            ),
        ]
    if info.scenario == "multi":
        incomplete = sum(len(r.get("incomplete", [])) for r in reconciled.values())
        aggregate += multi_criteria(info, committed, stage_buckets, incomplete)
    engine = [float(t.engine_ms) for t in committed if t.engine_ms is not None]
    figures = {
        "offered": len(turns),
        "completed": len(committed),
        "failed": len(failed),
        "failures": sorted({f"{t.status}:{t.error}" for t in failed}),
        "lost": lost,
        "duplicated": duplicated,
        "commit_p95_bucket_s": commit_p95,
        "commit_share_within_250ms": within,
        "queue_wait_p95_ms": percentile([float(t.queued_ms or 0) for t in committed], 95),
        "front_p95_ms": percentile(
            [
                float(t.first_call_offset_ms)
                for t in committed
                if t.first_call_offset_ms is not None
            ],
            95,
        ),
        "engine_ms": {f"p{q}": percentile(engine, q) for q in (50, 95, 99)},
    }
    if info.scenario == "multi":
        figures["multi"] = multi_figures(committed, stages)
    return Evaluation(
        info=info,
        stories=per_story,
        aggregate=aggregate,
        figures=figures,
        stages=stages,
        minutes=_per_minute(samples, stories, info),
    )


def _cell(value: float | None, unit: str = " ms") -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "> 60 s"
    return f"{value:.0f}{unit}"


def _seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "> 60 s"
    return f"≤ {value * 1000:g} ms"


def exit_code(evaluation: Evaluation) -> int:
    """The run's exit status: 1 for a FAIL on the reference host class, else 0 (NFR-PERF-4:
    other hosts are reported, not gated)."""
    return 1 if evaluation.info.enforced and not evaluation.passed else 0


def markdown(evaluation: Evaluation) -> str:
    """The report: the verdict, every criterion, per-story and aggregate figures, the server's
    stage latencies, and queue depth and resources per minute."""
    info = evaluation.info
    verdict = PASS if evaluation.passed else FAIL
    gating = (
        f"host class {info.host_class}: gated (exit 1 on FAIL)"
        if info.enforced
        else f"host class {info.host_class}: reported, not gated"
    )
    title = SCENARIO_TITLES.get(info.scenario, info.scenario)
    if info.scenario == "multi":
        events = info.events if info.events is not None else info.turns * 2
        stories_line = (
            f"- Stories: {info.turns:,} turns ({events:,} events, canon's `fact_committed` "
            f"included) and {info.memories:,} memories each (the scale fixture's builder), "
            "with per-character knowledge (`epistrel.testing.fixtures.knowledge`: 30% of each "
            f"character's memories private, {info.beliefs} beliefs, {info.exposures} unresolved "
            "exposures); profile "
            f"`{info.profile or 'unknown'}`, perception `{info.perception or 'unknown'}`; "
            "two-character `sequential` turns (the player speaks, the two co-located primaries "
            "render in request order, the other characters elsewhere and fixed `silent`); the "
            "stub's renders declare 10% contradicted and 10% novel claims, the rest testify "
            "about the first primary (`perf.lite_smoke.multi`)."
        )
    elif info.scenario == "structured":
        events = info.events if info.events is not None else info.turns * 2
        stories_line = (
            f"- Stories: {info.turns:,} turns ({events:,} events, canon's `fact_committed` "
            f"included) and {info.memories:,} memories each, on the `Structured (M4)` profile "
            "(the scale fixture's builder, `epistrel.testing.fixtures.scale`); single-character "
            "turns; the stub's renders declare 10% contradicted and 10% novel claims "
            "(`epistrel.testing.structured_load`)."
        )
    else:
        stories_line = (
            f"- Stories: {info.turns:,} turns ({info.turns * 2:,} events) and 50 pinned memories "
            "each, rolling summary up to date (`epistrel.testing.perf`)."
        )
    lines = [
        f"# P-04 {title} reference workload: {info.started}",
        "",
        f"**Result: {verdict}** ({gating}).",
        "",
        f"- Host: {info.host}",
        f"- Engine: `{info.engine_sha}`",
        f"- Workload: {info.stories} stories, one turn every {info.interval_s:g} s each, "
        f"open loop, `turn_concurrency = queue`, {info.minutes:g} minutes, stub model at "
        f"{info.stub_latency_s:g} s per call; submissions stop {_span(info.stop_before_s)} "
        "before the end" + (" (a smoke run: warm-up and end margin 30 s)." if info.smoke else "."),
        stories_line,
        "",
        "## Criteria",
        "",
        "| Criterion | Scope | Measured | Bound | Result |",
        "|---|---|---|---|---|",
    ]
    for c in evaluation.aggregate:
        lines.append(f"| {c.name} | {c.scope} | {c.measured} | {c.bound} | {c.result} |")
    for story in evaluation.stories:
        for c in story.criteria:
            lines.append(
                f"| {c.name} | story {story.index + 1} | {c.measured} | {c.bound} | {c.result} |"
            )
    lines += [
        "",
        "## Per story",
        "",
        "| Story | Offered | Completed | Failed | Lost | Duplicated | Max waiting (after 5 min) "
        "| Max queue gauge | Queue wait p95 / max | Front to first call p95 "
        "| Engine-only p50 / p95 / p99 | Result |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in evaluation.stories:
        engine = " / ".join(_cell(s.engine_ms.get(f"p{q}")) for q in (50, 95, 99))
        lines.append(
            f"| {s.index + 1} (`{s.story_id}`) | {s.offered} | {s.completed} | {s.failed} "
            f"| {len(s.lost)} | {len(s.duplicated)} | {s.max_waiting} | {s.max_queue_gauge:g} "
            f"| {_cell(s.queue_wait_p95_ms)} / {_cell(s.queue_wait_max_ms)} "
            f"| {_cell(s.front_p95_ms)} | {engine} | {PASS if s.passed else FAIL} |"
        )
    f = evaluation.figures
    engine = " / ".join(_cell(f["engine_ms"].get(f"p{q}")) for q in (50, 95, 99))
    lines += [
        "",
        "## Aggregate",
        "",
        f"- Offered {f['offered']}, completed {f['completed']}, failed {f['failed']}"
        + (f" ({', '.join(f['failures'])})" if f["failures"] else "")
        + f"; lost {f['lost']}, duplicated {f['duplicated']}.",
        f"- Turn commit (server `commit` stage): p95 {_seconds(f['commit_p95_bucket_s'])}"
        + (
            f", {f['commit_share_within_250ms']:.1%} within 250 ms."
            if f["commit_share_within_250ms"] is not None
            else "."
        ),
        f"- Queue wait p95 {_cell(f['queue_wait_p95_ms'])}; queue front to first model call p95 "
        f"{_cell(f['front_p95_ms'])}.",
        f"- Engine-only turn time (`elapsed_ms` less the model calls' latency) p50 / p95 / p99: "
        f"{engine}.",
        "",
        "## Server stage latencies",
        "",
        "From `epistrel_turn_stage_seconds`, diffed over the run; each percentile is the upper "
        "edge of the bucket that holds it.",
        "",
        "| Stage | Observations | p50 | p95 | p99 |",
        "|---|---|---|---|---|",
    ]
    for stage, figures in evaluation.stages.items():
        lines.append(
            f"| {stage} | {int(figures['count'] or 0)} | {_seconds(figures['p50'])} "
            f"| {_seconds(figures['p95'])} | {_seconds(figures['p99'])} |"
        )
    if info.scenario == "multi":
        lines += multi_lines(evaluation)
    header = " | ".join(f"S{s.index + 1}" for s in evaluation.stories)
    lines += [
        "",
        "## Queue depth over time",
        "",
        "Turns waiting behind the running one (`epistrel_turn_queue_depth`), the most seen in "
        "each minute's 5 s samples.",
        "",
        f"| Minute | {header} |",
        "|---|" + "---|" * len(evaluation.stories),
    ]
    for row in evaluation.minutes:
        cells = " | ".join(f"{v:g}" for v in row["queue"])
        lines.append(f"| {row['minute']} | {cells} |")
    lines += [
        "",
        "## Resources",
        "",
        "The most seen in each minute's 5 s samples: database connections (`pg_stat_activity`), "
        "CPU (% of one core) and memory of the engine and database containers (`docker stats`).",
        "",
        "| Minute | DB connections | Engine CPU | Engine memory | DB CPU | DB memory |",
        "|---|---|---|---|---|---|",
    ]
    for row in evaluation.minutes:
        lines.append(
            f"| {row['minute']} | {row['db_connections']} | {row['engine_cpu']:.0f}% "
            f"| {row['engine_mem_mib']:.0f} MiB | {row['db_cpu']:.0f}% "
            f"| {row['db_mem_mib']:.0f} MiB |"
        )
    lines += ["", f"Raw data: the JSON sidecar beside this file. Verdict: **{verdict}**.", ""]
    return "\n".join(lines)


def multi_lines(evaluation: Evaluation) -> list[str]:
    """The multi report's own section: the per-character columns, the degradation counts and
    what the turns ran under."""
    found = evaluation.figures.get("multi", {})
    lines = [
        "",
        "## Multi-character",
        "",
        "Per-character stages from `epistrel_turn_stage_seconds` (`observability.stages."
        "MULTI_STAGES`): `context_ms` is one render's context (`build_context`: the partition "
        "read and lazy resolutions included), `compose_ms` one turn's reconciliation, audience "
        "filter and composition, the polish's model time left out.",
        "",
        "| Column | Stage | Observations | p50 | p95 | p99 |",
        "|---|---|---|---|---|---|",
    ]
    for column, stage in MULTI_COLUMNS.items():
        q = found.get(column) or {}
        lines.append(
            f"| `{column}` | {stage} | {int(q.get('count') or 0)} | {_seconds(q.get('p50'))} "
            f"| {_seconds(q.get('p95'))} | {_seconds(q.get('p99'))} |"
        )
    steps = found.get("degradation") or {}
    lines += [
        "",
        "- Degradation steps (committed turns): "
        + (", ".join(f"`{k}` {v}" for k, v in steps.items()) or "none")
        + ".",
        "- Profiles: "
        + (", ".join(f"`{p}`" for p in found.get("profiles") or []) or "n/a")
        + "; perception (audience filter): "
        + (", ".join(f"`{a}`" for a in found.get("audience_filters") or []) or "n/a")
        + ".",
    ]
    return lines


def sidecar(evaluation: Evaluation, records: Sequence[Mapping[str, Any]], samples: Any) -> Any:
    """The raw JSON kept beside the report."""
    from dataclasses import asdict

    return {
        "info": asdict(evaluation.info),
        "passed": evaluation.passed,
        "aggregate": [asdict(c) for c in evaluation.aggregate],
        "stories": [
            {**asdict(s), "criteria": [asdict(c) for c in s.criteria]} for s in evaluation.stories
        ],
        "figures": evaluation.figures,
        "stages": evaluation.stages,
        "minutes": evaluation.minutes,
        "records": list(records),
        "samples": samples,
    }
