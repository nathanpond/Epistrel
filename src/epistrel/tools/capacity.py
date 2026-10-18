"""P-10, capacity exploration (docs/04 §11; NFR-SCALE-1; #153). This is informational and
**never** a CI gate or a release gate.

    uv run python -m epistrel.tools.capacity --up-to 1000000 \\
        [--steps 250000,1000000,5000000] [--database-url postgresql+psycopg://...] \\
        [--queries 100] [--out docs/capacity-exploration.md]

The tool extends P-06's vector-scale curve beyond the gated range. It builds the scale fixture
(``epistrel.testing.fixtures.scale``) at 50k memories, then grows the one story through each
step of ``--steps`` up to ``--up-to``. These are 250k, 1M and 5M memories in one deployment by
default. At each step it records:

- the HNSW build time and index size, and the rebuilt index's vector count;
- write throughput on the memory table (the step's rows a second) and on the event table (the
  events a second of ``TURNS_PER_STEP`` turns committed at that size, in ``TURN_BATCH``
  transactions);
- retrieval latency (``Engine.retrieve``, p50/p95/p99 of ``--queries`` queries, the query
  embedding taken out) and turn-commit latency (each turn batch's seconds per turn).

It reports the **first budget that fails**: retrieval p95 over NFR-PERF-1's 150 ms, or a
commit over NFR-PERF-3's 250 ms. It writes ``--out`` (``docs/capacity-exploration.md``, which
the operator commits) with the host it ran on. No capacity beyond the measured steps is
claimed. The database is ``--database-url``, which should be an empty scratch database because
the run pins it and writes into it, or a throwaway PostgreSQL started through testcontainers.
The run uses the stub adapters, so no model is called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.engine import Engine
from epistrel.testing import perf
from epistrel.testing.fixtures import scale
from epistrel.tools.model_matrix import database

DEFAULT_STEPS = (250_000, 1_000_000, 5_000_000)
START = 50_000
"""The fixture as built (NFR-SCALE-1's gated range ends here)."""
TURNS_PER_STEP = 500
RETRIEVAL_BUDGET_MS = 150.0
COMMIT_BUDGET_MS = 250.0
OUT = Path("docs/capacity-exploration.md")


@dataclass
class Step:
    """One measured step."""

    memories: int
    hnsw_build_s: float
    index_bytes: int
    vectors: int
    memory_rows_per_s: float
    event_rows_per_s: float
    commit_ms_per_turn: float
    retrieval_ms: dict[str, float] = field(default_factory=dict)

    def failed_budget(self) -> str | None:
        if self.retrieval_ms.get("p95", 0.0) > RETRIEVAL_BUDGET_MS:
            return f"retrieval p95 {self.retrieval_ms['p95']:.1f} ms > {RETRIEVAL_BUDGET_MS:.0f} ms"
        if self.commit_ms_per_turn > COMMIT_BUDGET_MS:
            return f"commit {self.commit_ms_per_turn:.1f} ms > {COMMIT_BUDGET_MS:.0f} ms"
        return None


def steps_up_to(up_to: int, steps: Sequence[int] = DEFAULT_STEPS) -> list[int]:
    """The steps to measure: those above the fixture's 50k and at most ``up_to``."""
    return sorted(s for s in set(steps) if START < s <= up_to)


def first_failure(measured: Sequence[Step]) -> tuple[int, str] | None:
    """The first step whose budget failed, and which budget."""
    for step in measured:
        reason = step.failed_budget()
        if reason is not None:
            return step.memories, reason
    return None


def page(measured: Sequence[Step], host: dict[str, Any]) -> str:
    """``docs/capacity-exploration.md``."""
    failure = first_failure(measured)
    lines = [
        "# Capacity exploration (P-10)",
        "",
        "> Written by `python -m epistrel.tools.capacity` (#153). This page is informational "
        "and never a release gate. It extends `docs/capacity.md`'s measured curve and claims "
        "no capacity beyond the steps below.",
        "",
        f"**Host: {host.get('host', 'local')} (not the reference host)**, {host.get('platform')}, "
        f"{host.get('vcpus')} vCPU, {host.get('ram_gb')} GB RAM.",
        "",
        "| Memories | HNSW build (s) | Index (MiB) | Memory rows/s | Event rows/s "
        "| Commit (ms/turn) | Retrieval p50 | p95 | p99 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in measured:
        q = s.retrieval_ms
        lines.append(
            f"| {s.memories:,} | {s.hnsw_build_s:.1f} | {s.index_bytes / 2**20:.1f} | "
            f"{s.memory_rows_per_s:,.0f} | {s.event_rows_per_s:,.0f} | {s.commit_ms_per_turn:.1f} "
            f"| {q.get('p50', 0):.1f} | {q.get('p95', 0):.1f} | {q.get('p99', 0):.1f} |"
        )
    lines += [
        "",
        "First budget that fails: "
        + (
            f"at {failure[0]:,} memories, {failure[1]}."
            if failure
            else "none in the measured steps."
        ),
        "",
    ]
    return "\n".join(lines)


def make_settings(dsn: str) -> Settings:
    return Settings.model_validate(
        {
            "database_url": dsn,
            "model_adapter": "stub",
            "stub_embedding_dimension": scale.DIMENSION,
            "config_key": Fernet.generate_key().decode(),
            "service_credentials": [secrets.token_urlsafe(32)],
        }
    )


def host_info() -> dict[str, Any]:
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except ValueError, OSError, AttributeError:
        memory = 0
    return {
        "host": os.environ.get("EPISTREL_PERF_HOST", "local"),
        "platform": platform.platform(),
        "vcpus": os.cpu_count() or 0,
        "ram_gb": round(memory / 2**30, 1),
    }


async def measure(dsn: str, steps: Sequence[int], queries: int, log: Any = print) -> list[Step]:
    """Build the fixture at 50k in ``dsn``, then grow and measure each step."""
    settings = make_settings(dsn)
    db = make_engine(settings)
    lib = Engine(settings, db=db)
    ctx = RequestContext(principal=scale.PRINCIPAL)
    try:
        fixture = await scale.build_scale_fixture(db, scale.ScaleSpec(), log=log)
        texts = [q for _, q in scale.queries(fixture.spec, fixture.story, queries)]
        measured: list[Step] = []
        for target in steps:
            before = fixture.story.memories
            started = time.perf_counter()
            await scale.grow(db, fixture, target, rebuild=False)
            memory_rate = (fixture.story.memories - before) / max(
                time.perf_counter() - started, 1e-9
            )
            index = await scale.rebuild_index(db, fixture.spec.dimension)
            started = time.perf_counter()
            events = await scale.commit_turns(db, fixture, TURNS_PER_STEP)
            took = time.perf_counter() - started
            timings = []
            for q in texts:
                result = await lib.retrieve(
                    ctx, fixture.story.story_id, q=q, viewpoint="omniscient"
                )
                timings.append(result.timings["total"])
            step = Step(
                memories=fixture.story.memories,
                hnsw_build_s=index.build_s,
                index_bytes=index.bytes,
                vectors=index.vectors,
                memory_rows_per_s=round(memory_rate, 1),
                event_rows_per_s=round(events / took, 1),
                commit_ms_per_turn=round(took * 1000 / TURNS_PER_STEP, 3),
                retrieval_ms={
                    "p50": perf.percentile(timings, 50),
                    "p95": perf.percentile(timings, 95),
                    "p99": perf.percentile(timings, 99),
                },
            )
            log(f"step {target:,}: {json.dumps(asdict(step))}")
            measured.append(step)
        return measured
    finally:
        await lib.models.aclose()
        await db.dispose()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m epistrel.tools.capacity", description=__doc__)
    p.add_argument("--up-to", type=int, required=True, help="the largest step to measure")
    p.add_argument(
        "--steps",
        default=",".join(str(s) for s in DEFAULT_STEPS),
        help="comma-separated memory counts (default 250000,1000000,5000000)",
    )
    p.add_argument("--database-url", default=None)
    p.add_argument("--queries", type=int, default=100)
    p.add_argument("--out", type=Path, default=OUT)
    return p


async def _main(args: argparse.Namespace) -> int:
    steps = steps_up_to(args.up_to, [int(s) for s in args.steps.split(",") if s.strip()])
    if not steps:
        print(f"error: no step above {START:,} and at most --up-to", file=sys.stderr)
        return 2
    async with database(args.database_url) as dsn:
        measured = await measure(dsn, steps, args.queries)
    args.out.write_text(page(measured, host_info()))
    print(f"wrote {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
