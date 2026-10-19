"""P-04: NFR-PERF-4's reference workload (docs/03, docs/04 §11), the Lite variant (#76) and the
structured variant (``--scenario structured``, #153; ``perf.lite_smoke.structured``).

Ten stories of 20,000 events and 50 pinned memories (``epistrel.testing.perf``), each submitting
one turn every 15 s on a fixed, open-loop schedule with ``turn_concurrency = queue``, for 30
minutes, against the stub model answering every call after a fixed 2 s. One command runs it:

    uv sync --group perf
    uv run python -m perf.lite_smoke --minutes 30 --host-class mac|reference|ci

It brings up the ``epistrel-perf`` compose stack (``compose.perf.yml``), seeds the stories through
the library (or restores the cached ``pg_dump`` of that seed), drives ``respond`` with Locust
(``locustfile.py``), samples database connections, CPU, memory and queue depth every 5 s
(``sample.py``), reconciles every submitted turn against ``history``, and writes the report
(``report.py``): ``perf/results/<date>-<time>-lite.md`` with a raw JSON sidecar. The gate is
enforced (exit 1 on FAIL) only with ``--host-class reference`` (NFR-PERF-4: other hosts are
reported, not gated). A run shorter than 30 minutes is a demo and writes to ``build/perf/``.

``--scenario structured`` runs ten structured stories of 10,000 memories and 20,000 events
(the scale fixture's builder, #153), one single-character turn every 60 s each, against the
stub at 2 s per call whose renders declare 10% contradicted and 10% novel claims, so
verification, permission and repair run under load. It adds NFR-PERF-1's retrieval and
NFR-PERF-2's verify p95 budgets to the gate and writes ``<date>-<time>-structured.md``.

The target can be pointed elsewhere through ``EPISTREL_PERF_BASE_URL``,
``EPISTREL_PERF_CREDENTIAL``, ``EPISTREL_PERF_PRINCIPAL`` and ``EPISTREL_PERF_DSN``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT = "epistrel-perf"
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "perf" / "lite_smoke" / "compose.perf.yml",
)
RESULTS_DIR = REPO_ROOT / "perf" / "results"
SCRATCH_DIR = REPO_ROOT / "build" / "perf"
CACHE_DIR = SCRATCH_DIR / "cache"

STORIES = 10
INTERVAL_S = 15.0
"""One turn per story every 15 s (the Lite variant's schedule)."""
FULL_MINUTES = 30
STOP_BEFORE_S = 120.0
"""Submissions stop 2 minutes before the end: every turn submitted before then must complete."""
WARMUP_S = 300.0
"""The backlog criterion applies after the first 5 minutes."""
POLL_S = 0.5
SAMPLE_S = 5.0
STUB_LATENCY_S = 2.0
COMMIT_P95_BUDGET_S = 0.250
"""NFR-PERF-3, the turn-commit budget P-04 gates on (aggregate p95, server commit stage)."""
FRONT_TO_FIRST_CALL_P95_S = 1.0
MAX_WAITING = 1
RETRIEVAL_P95_BUDGET_S = 0.150
"""NFR-PERF-1, the retrieval budget the structured variant gates on (server ``retrieve`` stage)."""
VERIFY_P95_BUDGET_S = 0.050
"""NFR-PERF-2, the deterministic-verify budget the structured variant gates on (``verify``)."""
HOST_CLASSES = ("mac", "reference", "ci")
SCENARIOS = ("lite", "structured")
STRUCTURED_INTERVAL_S = 60.0
"""One single-character turn per structured story every 60 s (the structured variant)."""
STRUCTURED_MEMORIES = 10_000
STRUCTURED_EVENTS = 20_000
"""Each structured story's size (docs/04 P-04 M4): 10,000 memories and 20,000 events."""
ENFORCED_HOST_CLASS = "reference"

DEFAULT_BASE_URL = "http://127.0.0.1:8100"
DEFAULT_CREDENTIAL = "perf-only-credential-0000000000000000"
DEFAULT_PRINCIPAL = "p-perf"
DEFAULT_DSN = "postgresql+psycopg://epistrel:epistrel-dev@127.0.0.1:5442/epistrel"


@dataclass(frozen=True, slots=True)
class Target:
    """Where the workload runs: the REST base URL, its credential and principal, and the
    database the seed writes to (``EPISTREL_PERF_*``, defaulting to the perf compose stack)."""

    base_url: str
    credential: str
    principal: str
    dsn: str

    @classmethod
    def from_env(cls) -> Target:
        return cls(
            base_url=os.environ.get("EPISTREL_PERF_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            credential=os.environ.get("EPISTREL_PERF_CREDENTIAL", DEFAULT_CREDENTIAL),
            principal=os.environ.get("EPISTREL_PERF_PRINCIPAL", DEFAULT_PRINCIPAL),
            dsn=os.environ.get("EPISTREL_PERF_DSN", DEFAULT_DSN),
        )

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credential}",
            "X-Epistrel-Principal": self.principal,
        }
