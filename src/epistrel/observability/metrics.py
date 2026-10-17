"""Prometheus metrics and ``GET /metrics`` (FR-OBS-1, invariant 3; #74).

``/metrics`` serves the default registry in the Prometheus text format, unauthenticated
(invariant 4's exempt list). It is on ``routes.public_router``, so a ``worker``-mode process
serves it too (#71). ``render_metrics()`` returns the same text for an embedder that serves it
its own way.

The registry holds:

- ``epistrel_http_request_duration_seconds{route,method,status}``: request latency, 5 ms to 30 s
  buckets; ``route`` is the matched route's name (its ``operation_id``), ``unmatched`` otherwise;
- ``epistrel_operations_total{kind,outcome}``: every ``Engine`` call, job and queued turn
  (``outcome`` ``ok``, ``rejected`` for a 4xx error, ``error`` otherwise);
- #54's model counters (``epistrel_model_*``) and #56/#68's guardrail counters;
- gauges computed at scrape on a dedicated sync connection: ``epistrel_jobs_queued{kind}``,
  ``epistrel_jobs_running``, ``epistrel_timers_overdue_seconds`` (the oldest overdue timer or
  recurring task, 0 when none), ``epistrel_workers_alive`` (heartbeats within
  ``worker_heartbeat_timeout``) and ``epistrel_turn_queue_depth{story_id}`` (turns waiting
  behind the running one, per story with any; #76);
- ``epistrel_turn_stage_seconds{stage}`` (``observability.stages``, #76): Lite turn latency by
  stage;
- ``epistrel_quarantines_total{reason}`` (``author`` or ``repeated_implication``) and
  ``epistrel_sanitizes_total``: #145's mutation primitives (``core.mutation``);
- ``epistrel_thunks_total{event}`` (``introduced``, ``resolved``, ``expired``),
  ``epistrel_ledger_total{event}`` (``created``, ``discharged``) and
  ``epistrel_minimal_commits_total``: #146's commitment lifecycle (``core.commit.lifecycle``).

Label values are enums, ULIDs, route names, HTTP statuses or sanitised model names (#44's rule),
never content. prometheus-client's default process, platform and GC collectors are removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    REGISTRY,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.metrics_core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy import Engine, select, text

from epistrel.core.config_changes import section
from epistrel.core.db import make_sync_engine
from epistrel.core.settings_schema import WorkerHealthSettings
from epistrel.core.tables import deployment_state

for _default in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
    with contextlib.suppress(KeyError):
        REGISTRY.unregister(_default)

REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
UNMATCHED_ROUTE = "unmatched"
SCRAPE_TIMEOUT_S = 2.0

HTTP_DURATION = Histogram(
    "epistrel_http_request_duration_seconds",
    "REST request latency by route name, method and status.",
    ("route", "method", "status"),
    buckets=REQUEST_BUCKETS,
)
OPERATIONS = Counter(
    "epistrel_operations",
    "Engine calls, jobs and queued turns by kind and outcome (ok, rejected or error).",
    ("kind", "outcome"),
)

QUARANTINES = Counter(
    "epistrel_quarantines",
    "Memories quarantined, by reason (author: an author's call; repeated_implication: the "
    "output gate's convergence rule).",
    ("reason",),
)
SANITIZES = Counter("epistrel_sanitizes", "Memory texts sanitized by a surrogate.")
THUNKS = Counter(
    "epistrel_thunks",
    "Thunks by lifecycle event (introduced, resolved by a dependence, expired).",
    ("event",),
)
LEDGER = Counter(
    "epistrel_ledger",
    "Promissory-ledger entries by event (created by a salient reference, discharged).",
    ("event",),
)
MINIMAL_COMMITS = Counter(
    "epistrel_minimal_commits",
    "Salient references discharged by the deterministic minimal commit.",
)

_GAUGES_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM jobs WHERE status = 'running') AS running,
      (SELECT count(*) FROM worker_heartbeats
        WHERE at > now() - make_interval(secs => :timeout)) AS alive,
      (SELECT coalesce(extract(epoch FROM now() - min(due)), 0) FROM (
          SELECT due_at AS due FROM timers
           WHERE due_at < now() AND (lease_until IS NULL OR lease_until < now())
          UNION ALL
          SELECT next_run_at FROM recurring_tasks WHERE next_run_at < now()
        ) AS overdue) AS overdue_s
    """
)
_QUEUED_SQL = text("SELECT kind, count(*) AS n FROM jobs WHERE status = 'queued' GROUP BY kind")
_TURN_QUEUE_SQL = text(
    """
    SELECT q.story_id, count(*) AS n FROM turn_queue q
      JOIN operations o ON o.operation_id = q.operation_id
     WHERE o.status = 'queued'
     GROUP BY q.story_id
    """
)


class SchedulerGauges(Collector):
    """Job, timer and worker gauges read at scrape time from the bound database (none when no
    app is running in this process)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: Engine | None = None
        self._url: str | None = None
        self._binds = 0

    def bind(self, url: str) -> None:
        with self._lock:
            self._binds += 1
            if self._engine is None:
                self._url = url
                self._engine = make_sync_engine(
                    url, pool_size=1, max_overflow=0, connect_args={"connect_timeout": 2}
                )

    def unbind(self) -> None:
        with self._lock:
            self._binds = max(0, self._binds - 1)
            if self._binds == 0 and self._engine is not None:
                self._engine.dispose()
                self._engine = None
                self._url = None

    def describe(self) -> Iterator[Metric]:
        return iter(())

    def collect(self) -> Iterator[Metric]:
        with self._lock:
            engine = self._engine
        if engine is None:
            return
        try:
            with engine.connect() as conn:
                conn.execute(text(f"SET LOCAL statement_timeout = {int(SCRAPE_TIMEOUT_S * 1000)}"))
                settings = section(
                    WorkerHealthSettings,
                    conn.scalar(select(deployment_state.c.settings).limit(1)),
                )
                row = conn.execute(
                    _GAUGES_SQL, {"timeout": settings.worker_heartbeat_timeout}
                ).one()
                queued = conn.execute(_QUEUED_SQL).all()
                waiting = conn.execute(_TURN_QUEUE_SQL).all()
        except Exception:
            return
        family = GaugeMetricFamily(
            "epistrel_jobs_queued", "Queued background jobs by kind.", labels=["kind"]
        )
        for kind, count in queued:
            family.add_metric([str(kind)], float(count))
        yield family
        turns = GaugeMetricFamily(
            "epistrel_turn_queue_depth",
            "Turns waiting in a story's turn queue behind the one in progress (stories with "
            "none waiting are absent).",
            labels=["story_id"],
        )
        for story_id, count in waiting:
            turns.add_metric([str(story_id)], float(count))
        yield turns
        yield GaugeMetricFamily(
            "epistrel_jobs_running", "Background jobs running now.", value=float(row.running)
        )
        yield GaugeMetricFamily(
            "epistrel_timers_overdue_seconds",
            "Age of the oldest overdue timer or recurring task (0: none).",
            value=float(row.overdue_s or 0),
        )
        yield GaugeMetricFamily(
            "epistrel_workers_alive",
            "Worker loops with a heartbeat within worker_heartbeat_timeout.",
            value=float(row.alive),
        )


GAUGES = SchedulerGauges()
REGISTRY.register(GAUGES)


def render_metrics() -> bytes:
    """The default registry in the Prometheus text format (blocking: the gauges query)."""
    return generate_latest(REGISTRY)


router = APIRouter(tags=["ops"])


@router.get(
    "/metrics",
    operation_id="metrics",
    summary="Prometheus metrics",
    description="The Prometheus text exposition of the engine's metrics: identifiers and counts "
    "only, never story content (invariant 3). Served without a credential (invariant 4).",
    response_class=Response,
    responses={200: {"content": {"text/plain": {"schema": {"type": "string"}}}}},
)
async def metrics() -> Response:
    body = await asyncio.to_thread(render_metrics)
    return Response(content=body, media_type=CONTENT_TYPE_LATEST)


__all__ = [
    "GAUGES",
    "HTTP_DURATION",
    "LEDGER",
    "MINIMAL_COMMITS",
    "OPERATIONS",
    "QUARANTINES",
    "REQUEST_BUCKETS",
    "SANITIZES",
    "THUNKS",
    "SchedulerGauges",
    "render_metrics",
    "router",
]
