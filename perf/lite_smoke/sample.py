"""P-04's resource sampler (#76): every 5 s, database connections (``pg_stat_activity``), the
stack's CPU and memory (``docker stats``) and every story's queue depth (the engine's
``epistrel_turn_queue_depth`` gauge); plus ``/metrics`` snapshots, diffed over the run.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import text

from epistrel.core.db import make_sync_engine
from perf.lite_smoke import PROJECT, SAMPLE_S

_CONNECTIONS = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE state = 'active') AS active "
    "FROM pg_stat_activity WHERE datname = current_database()"
)


def scrape(base_url: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Every sample of ``GET /metrics``: ``(name, sorted labels) -> value``."""
    with urllib.request.urlopen(f"{base_url}/metrics", timeout=10.0) as answer:  # noqa: S310
        body = answer.read().decode()
    found: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            found[(sample.name, tuple(sorted(sample.labels.items())))] = float(sample.value)
    return found


def stage_buckets(
    before: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    after: dict[tuple[str, tuple[tuple[str, str], ...]], float],
) -> dict[str, dict[str, float]]:
    """``epistrel_turn_stage_seconds`` buckets observed between two scrapes: stage -> le -> n."""
    found: dict[str, dict[str, float]] = {}
    for (name, labels), value in after.items():
        if name != "epistrel_turn_stage_seconds_bucket":
            continue
        tags = dict(labels)
        delta = value - before.get((name, labels), 0.0)
        found.setdefault(tags["stage"], {})[tags["le"]] = delta
    return found


def _percent(value: str) -> float:
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        return 0.0


_UNITS = {
    "b": 1 / 2**20,
    "kib": 1 / 1024,
    "mib": 1.0,
    "gib": 1024.0,
    "kb": 1e3 / 2**20,
    "mb": 1e6 / 2**20,
    "gb": 1e9 / 2**20,
}


def _mib(value: str) -> float:
    used = value.split("/")[0].strip().lower()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if used.endswith(unit):
            try:
                return float(used[: -len(unit)]) * _UNITS[unit]
            except ValueError:
                return 0.0
    return 0.0


def docker_stats() -> dict[str, dict[str, float]]:
    """CPU % and memory (MiB) of the perf project's containers, by service."""
    found = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    stats: dict[str, dict[str, float]] = {}
    for line in found.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("Name", ""))
        if not name.startswith(f"{PROJECT}-"):
            continue
        service = name.removeprefix(f"{PROJECT}-").rsplit("-", 1)[0]
        stats[service] = {
            "cpu": _percent(row.get("CPUPerc", "0")),
            "mem_mib": _mib(row.get("MemUsage", "")),
        }
    return stats


@dataclass
class Sampler:
    """Samples every ``SAMPLE_S`` seconds on a thread until ``stop()``; ``clock()`` gives the
    run's time (seconds since its start)."""

    base_url: str
    dsn: str
    clock: Callable[[], float]
    samples: list[dict[str, Any]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="perf-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=60)

    def _run(self) -> None:
        engine = make_sync_engine(self.dsn, pool_size=1, max_overflow=0)
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self.samples.append(self._sample(engine))
                self._stop.wait(max(0.0, SAMPLE_S - (time.monotonic() - started)))
        finally:
            engine.dispose()

    def _sample(self, engine: Any) -> dict[str, Any]:
        sample: dict[str, Any] = {"t": round(self.clock(), 1)}
        try:
            with engine.connect() as conn:
                row = conn.execute(_CONNECTIONS).one()
            sample["db_connections"] = int(row.total)
            sample["db_active"] = int(row.active)
        except Exception as exc:
            sample["db_error"] = type(exc).__name__
        try:
            sample["queue"] = {
                dict(labels)["story_id"]: value
                for (name, labels), value in scrape(self.base_url).items()
                if name == "epistrel_turn_queue_depth"
            }
        except Exception as exc:
            sample["metrics_error"] = type(exc).__name__
        sample["containers"] = docker_stats()
        return sample
