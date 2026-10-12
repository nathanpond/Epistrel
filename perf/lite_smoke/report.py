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
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from perf.lite_smoke import (
    COMMIT_P95_BUDGET_S,
    ENFORCED_HOST_CLASS,
    FRONT_TO_FIRST_CALL_P95_S,
    MAX_WAITING,
    STOP_BEFORE_S,
    WARMUP_S,
)

PASS, FAIL = "PASS", "FAIL"
TITLE = re.compile(r"^# P-04 Lite reference workload: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC$", re.M)
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


def header(text: str) -> Header:
    """Parse a report's header; ``ValueError`` when it lacks its date, length or host class."""
    title, verdict, length = TITLE.search(text), VERDICT.search(text), LENGTH.search(text)
    size = SIZE.search(text)
    if title is None or verdict is None or length is None or size is None:
        raise ValueError("not a P-04 Lite report: no date, verdict, host class or run length")
    return Header(
        started=datetime.strptime(title.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC),
        minutes=float(length.group(1)),
        host_class=verdict.group(2),
        verdict=verdict.group(1),
        turns=int(size.group(1).replace(",", "")),
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

    @property
    def end_s(self) -> float:
        return self.minutes * 60

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


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


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
    cutoff = info.end_s - STOP_BEFORE_S
    due = [t for t in turns if t.submitted <= cutoff]
    figures.due = len(due)
    figures.due_completed = sum(
        1 for t in due if t.committed and t.finished is not None and t.finished <= info.end_s
    )
    figures.lost = list(reconciled.get("lost", []))
    figures.duplicated = list(reconciled.get("duplicated", []))
    figures.max_waiting = max_waiting(turns, after=WARMUP_S, end=info.end_s)
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
        (float(s.get("queue", {}).get(story_id, 0.0)) for s in samples if s["t"] >= WARMUP_S),
        default=0.0,
    )
    interval_ms = info.interval_s * 1000
    figures.criteria = [
        Criterion(
            "every turn submitted ≥ 2 min before the end completed",
            "story",
            f"{figures.due_completed}/{figures.due}",
            "all",
            figures.due > 0 and figures.due_completed == figures.due,
        ),
        Criterion(
            "turns waiting behind the running one, after 5 min",
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
    lines = [
        f"# P-04 Lite reference workload: {info.started}",
        "",
        f"**Result: {verdict}** ({gating}).",
        "",
        f"- Host: {info.host}",
        f"- Engine: `{info.engine_sha}`",
        f"- Workload: {info.stories} stories, one turn every {info.interval_s:g} s each, "
        f"open loop, `turn_concurrency = queue`, {info.minutes:g} minutes, stub model at "
        f"{info.stub_latency_s:g} s per call; submissions stop 2 minutes before the end.",
        f"- Stories: {info.turns:,} turns ({info.turns * 2:,} events) and 50 pinned memories "
        "each, rolling summary up to date (`epistrel.testing.perf`).",
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
        "| Stage | Turns | p50 | p95 | p99 |",
        "|---|---|---|---|---|",
    ]
    for stage, figures in evaluation.stages.items():
        lines.append(
            f"| {stage} | {int(figures['count'] or 0)} | {_seconds(figures['p50'])} "
            f"| {_seconds(figures['p95'])} | {_seconds(figures['p99'])} |"
        )
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
