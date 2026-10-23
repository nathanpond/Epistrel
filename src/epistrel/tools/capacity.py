"""P-10, capacity exploration (docs/04 §11; NFR-SCALE-1; #153, #498). This is informational
and **never** a CI gate or a release gate.

    uv run python -m epistrel.tools.capacity --up-to 1000000 \\
        [--steps 250000,1000000,5000000] [--start-memories 50000] \\
        [--database-url postgresql+psycopg://...] [--queries 100] \\
        [--out docs/capacity-exploration.md] [--json perf/results/<stamp>-capacity.json]

The tool extends P-06's vector-scale curve beyond the gated range. It builds the scale fixture
(``epistrel.testing.fixtures.scale``) at ``--start-memories`` (default 50k, the default spec's
proportions: twice as many events). Then it grows the one story through each step of
``--steps`` up to ``--up-to``. The default steps are 250k, 1M and 5M memories in one deployment.
``tests/perf/test_p_10.py`` (``perf_capacity``, run by hand, never by CI) drives the same
measurement (``measure_steps``) on the test suite's scale database. At each step it records:

- the HNSW build: its time, the index's size and vector count, and its memory. The memory is the
  ``maintenance_work_mem`` the build ran under (``scale.MAINTENANCE_WORK_MEM``, serial). If the
  graph outgrew it, the tuple count at which pgvector's NOTICE says the graph "no longer fits"
  is recorded too (``build_spilled_after``; ``None``: the whole graph was built in memory);
- write throughput on the memory table, twice: the bulk load with the index dropped (the
  fixture's way of growing), and ``LIVE_MEMORY_ROWS`` rows written with the index live, in
  ``scale.MEMORY_BATCH`` transactions;
- write throughput on the event table: the events a second of ``TURNS_PER_STEP`` turns
  committed in ``scale.TURN_BATCH`` transactions at that size;
- retrieval latency (``Engine.retrieve``, the query embedding taken out) and turn-commit
  latency (``COMMIT_SAMPLES`` single-turn ``Operation.commit()`` spans), each with its
  p50/p95/p99 and maximum.

It reports the **first budget that fails**: retrieval p95 over NFR-PERF-1's 150 ms, or a
commit p95 over NFR-PERF-3's 250 ms. A requested step that was not measured is listed as
unmeasured. No capacity beyond the measured steps is claimed. The run writes ``--out`` (the
page) and ``--json`` (every figure, with the host it ran on). The operator commits both: the
page to ``docs/capacity-exploration.md`` and the JSON under ``perf/results/``. The database is
``--database-url``, which should be an empty scratch database because the run pins it and writes
into it. Without one, the run uses a throwaway PostgreSQL started through testcontainers. The
run uses the stub adapters, so no model is called.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import random
import re
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.turns import stage_turn
from epistrel.engine import Engine
from epistrel.testing import perf
from epistrel.testing.fixtures import scale
from epistrel.tools.model_matrix import database

DEFAULT_STEPS = (250_000, 1_000_000, 5_000_000)
START = 50_000
"""The CLI's fixture as built (NFR-SCALE-1's gated range ends here)."""
TURNS_PER_STEP = 500
COMMIT_SAMPLES = 100
LIVE_MEMORY_ROWS = 2_000
QUERIES = 100
RETRIEVAL_BUDGET_MS = 150.0
"""NFR-PERF-1."""
COMMIT_BUDGET_MS = 250.0
"""NFR-PERF-3."""
OUT = Path("docs/capacity-exploration.md")
VIEWPOINT = "omniscient"
_SPILLED = re.compile(r"hnsw graph no longer fits into maintenance_work_mem after (\d+) tuples")

Log = Callable[[str], Any]


@dataclass
class Step:
    """One measured step."""

    memories: int
    hnsw_build_s: float
    index_bytes: int
    vectors: int
    maintenance_work_mem: str
    build_spilled_after: int | None
    """The tuples built in memory before the graph outgrew ``maintenance_work_mem`` (pgvector's
    NOTICE); ``None`` when it never did."""
    bulk_memory_rows_per_s: float
    live_memory_rows_per_s: float
    event_rows_per_s: float
    commit_ms: dict[str, float] = field(default_factory=dict)
    retrieval_ms: dict[str, float] = field(default_factory=dict)

    def failed_budget(self) -> str | None:
        if self.retrieval_ms.get("p95", 0.0) > RETRIEVAL_BUDGET_MS:
            return f"retrieval p95 {self.retrieval_ms['p95']:.1f} ms > {RETRIEVAL_BUDGET_MS:.0f} ms"
        if self.commit_ms.get("p95", 0.0) > COMMIT_BUDGET_MS:
            return f"commit p95 {self.commit_ms['p95']:.1f} ms > {COMMIT_BUDGET_MS:.0f} ms"
        return None


def steps_up_to(
    up_to: int, steps: Sequence[int] = DEFAULT_STEPS, *, start: int = START
) -> list[int]:
    """The steps to measure: those above the fixture's ``start`` and at most ``up_to``."""
    return sorted(s for s in set(steps) if start < s <= up_to)


def unmeasured(requested: Sequence[int], measured: Sequence[Step]) -> list[int]:
    """The requested steps no measurement covers, in order."""
    done = {s.memories for s in measured}
    return sorted(s for s in set(requested) if s not in done)


def first_failure(measured: Sequence[Step]) -> tuple[int, str] | None:
    """The first step whose budget failed, and which budget."""
    for step in measured:
        reason = step.failed_budget()
        if reason is not None:
            return step.memories, reason
    return None


def percentiles(samples: Sequence[float]) -> dict[str, float]:
    """Nearest-rank p50/p95/p99 and the maximum, rounded (#54's method)."""
    return {
        "p50": round(perf.percentile(samples, 50), 3),
        "p95": round(perf.percentile(samples, 95), 3),
        "p99": round(perf.percentile(samples, 99), 3),
        "max": round(max(samples), 3),
    }


def page(
    measured: Sequence[Step],
    host: Mapping[str, Any],
    *,
    requested: Sequence[int] = (),
    start: Mapping[str, Any] | None = None,
) -> str:
    """``docs/capacity-exploration.md``."""
    failure = first_failure(measured)
    lines = [
        "# Capacity exploration (P-10)",
        "",
        "> Written by `python -m epistrel.tools.capacity` or `tests/perf/test_p_10.py` (#153, "
        "#498). This page is informational and never a release gate. It extends "
        "`docs/capacity.md`'s measured curve and claims no capacity beyond the steps below.",
        "",
        f"**Host: {host.get('host', 'local')} (not the reference host)**, {host.get('platform')}, "
        f"{host.get('vcpus')} vCPU, {host.get('ram_gb')} GB RAM"
        + (
            f"; Postgres {host['postgres']}, pgvector {host.get('pgvector')}, `shared_buffers` "
            f"{host.get('shared_buffers')}"
            if host.get("postgres")
            else ""
        )
        + ".",
    ]
    if start:
        lines += [
            "",
            f"The story starts as the scale fixture at {start.get('memories', 0):,} memories and "
            f"{start.get('events', 0):,} events and grows by memories only; each step also "
            f"commits {TURNS_PER_STEP + COMMIT_SAMPLES:,} turns. Each index build runs serially "
            "under the step's `maintenance_work_mem`, the way a pin builds it.",
        ]
    lines += [
        "",
        "| Memories | HNSW build (s) | Build memory | Index (MiB) | Bulk memory rows/s "
        "| Live memory rows/s | Event rows/s | Commit p50 | p95 | Retrieval p50 | p95 | p99 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in measured:
        q, c = s.retrieval_ms, s.commit_ms
        spilled = (
            f"{s.maintenance_work_mem}, spilled after {s.build_spilled_after:,} tuples"
            if s.build_spilled_after is not None
            else f"{s.maintenance_work_mem}, fit in memory"
        )
        lines.append(
            f"| {s.memories:,} | {s.hnsw_build_s:.1f} | {spilled} | {s.index_bytes / 2**20:.1f} "
            f"| {s.bulk_memory_rows_per_s:,.0f} | {s.live_memory_rows_per_s:,.0f} "
            f"| {s.event_rows_per_s:,.0f} | {c.get('p50', 0):.1f} | {c.get('p95', 0):.1f} "
            f"| {q.get('p50', 0):.1f} | {q.get('p95', 0):.1f} | {q.get('p99', 0):.1f} |"
        )
    lines += [
        "",
        "Latencies are in milliseconds. First budget that fails: "
        + (
            f"at {failure[0]:,} memories, {failure[1]}."
            if failure
            else "none in the measured steps."
        ),
    ]
    missing = unmeasured(requested, measured)
    if missing:
        lines += [
            "",
            "Unmeasured: " + ", ".join(f"{s:,}" for s in missing) + " memories. No figure and "
            "no capacity is claimed for them.",
        ]
    lines.append("")
    return "\n".join(lines)


def document(
    measured: Sequence[Step],
    host: Mapping[str, Any],
    *,
    requested: Sequence[int],
    start: Mapping[str, Any],
    sizes: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Every figure of the run, for ``perf/results/``; ``sizes`` are ``measure_steps``'s
    per-step counts (``queries``, ``commits``, ``turns``, ``live_rows``) when the run did not
    use the defaults."""
    given = dict(sizes or {})
    counts = {
        "queries": given.get("queries", QUERIES),
        "commit_samples": given.get("commits", COMMIT_SAMPLES),
        "turns_per_step": given.get("turns", TURNS_PER_STEP),
        "live_memory_rows": given.get("live_rows", LIVE_MEMORY_ROWS),
    }
    failure = first_failure(measured)
    return {
        "test": "P-10",
        "requirement": "NFR-SCALE-1",
        "informational": True,
        "budgets": {
            "retrieval_p95_ms": RETRIEVAL_BUDGET_MS,
            "commit_p95_ms": COMMIT_BUDGET_MS,
        },
        "requested_steps": sorted(set(requested)),
        "unmeasured_steps": unmeasured(requested, measured),
        "start": dict(start),
        **counts,
        "points": [asdict(s) for s in measured],
        "first_failure": None
        if failure is None
        else {"memories": failure[0], "budget": failure[1]},
        "host": dict(host),
    }


# -- the measurement -------------------------------------------------------------------------------


class _Notices:
    """pgvector's NOTICEs on every connection the engine opens after ``watch``."""

    def __init__(self, db: AsyncEngine) -> None:
        self.messages: list[str] = []
        self._db = db

        def connected(dbapi_connection: Any, _record: Any) -> None:
            driver = getattr(dbapi_connection, "driver_connection", None)
            if driver is not None and hasattr(driver, "add_notice_handler"):
                driver.add_notice_handler(self._seen)

        event.listen(db.sync_engine, "connect", connected)
        self._listener = connected

    def _seen(self, diagnostic: Any) -> None:
        self.messages.append(str(getattr(diagnostic, "message_primary", "") or ""))

    @contextmanager
    def capturing(self) -> Iterator[list[str]]:
        start = len(self.messages)
        found: list[str] = []
        try:
            yield found
        finally:
            found.extend(self.messages[start:])

    def close(self) -> None:
        event.remove(self._db.sync_engine, "connect", self._listener)


def spilled_after(messages: Sequence[str]) -> int | None:
    """The tuple count of pgvector's "no longer fits" NOTICE, if one was raised."""
    for message in messages:
        found = _SPILLED.search(message)
        if found is not None:
            return int(found.group(1))
    return None


async def _rebuild(
    db: AsyncEngine, notices: _Notices, dimension: int, maintenance_work_mem: str
) -> tuple[Any, int | None]:
    """``scale.rebuild_index`` on a fresh connection (so the NOTICE handler is on it)."""
    await db.dispose()
    with notices.capturing() as seen:
        index = await scale.rebuild_index(db, dimension, maintenance_work_mem=maintenance_work_mem)
    return index, spilled_after(seen)


async def commit_latency(db: AsyncEngine, fixture: scale.ScaleFixture, n: int) -> list[float]:
    """``n`` single-turn commits on the fixture's story: each ``Operation.commit()`` span (ms),
    the two beats of a turn as the fixture's turns write them, with no model call."""
    story = fixture.story
    cast = list(story.characters.values())
    rng = random.Random(f"p10:{fixture.spec.seed}:{story.head}")  # noqa: S311 - filler text
    samples: list[float] = []
    with perf.summaries_muted():
        for i in range(n):
            op = Operation(
                db, story_id=story.story_id, kind=OperationKind.TURN, principal=scale.PRINCIPAL
            )
            await op.begin()
            say = perf.filler(rng, 160)
            stage_turn(
                op,
                actor_character_id=story.player,
                say=say,
                do=None,
                reply=perf.filler(rng, 300),
                render_character_ids=[cast[1 + i % (len(cast) - 1)]],
                model=perf.SEED_MODEL,
                prompt_hash=hashlib.sha256(say.encode()).hexdigest(),
                finish_reason="stop",
            )
            started = time.perf_counter()
            await op.commit()
            samples.append((time.perf_counter() - started) * 1000.0)
    story.head += n
    story.events += 2 * n
    return samples


async def live_memory_writes(db: AsyncEngine, fixture: scale.ScaleFixture, n: int) -> float:
    """Write ``n`` more memories with the HNSW index live; answers rows a second."""
    story = fixture.story
    turn_ops = await scale.turn_operations(db, story.story_id)
    rows = scale.memory_plan(fixture.spec, story, turn_ops, count=n, offset=story.memories)
    started = time.perf_counter()
    story.memories += await scale.insert_memories(db, rows)
    return n / max(time.perf_counter() - started, 1e-9)


async def measure_steps(
    db: AsyncEngine,
    lib: Engine,
    fixture: scale.ScaleFixture,
    steps: Sequence[int],
    *,
    queries: int = QUERIES,
    commits: int = COMMIT_SAMPLES,
    turns: int = TURNS_PER_STEP,
    live_rows: int = LIVE_MEMORY_ROWS,
    maintenance_work_mem: str = scale.MAINTENANCE_WORK_MEM,
    log: Log = print,
) -> list[Step]:
    """Grow the built ``fixture`` through ``steps`` and measure each (the module docstring)."""
    ctx = RequestContext(principal=scale.PRINCIPAL)
    texts = [q for _, q in scale.queries(fixture.spec, fixture.story, queries)]
    notices = _Notices(db)
    measured: list[Step] = []
    try:
        for target in steps:
            before = fixture.story.memories
            started = time.perf_counter()
            await scale.grow(db, fixture, target, rebuild=False)
            bulk_rate = (fixture.story.memories - before) / max(time.perf_counter() - started, 1e-9)
            index, spilled = await _rebuild(
                db, notices, fixture.spec.dimension, maintenance_work_mem
            )
            fixture.index = index
            log(f"step {target:,}: index built in {index.build_s:.1f} s")
            for q in texts[: min(10, len(texts))]:  # warm-up, untimed
                await lib.retrieve(ctx, fixture.story.story_id, q=q, viewpoint=VIEWPOINT)
            timings: list[float] = []
            for q in texts:
                result = await lib.retrieve(ctx, fixture.story.story_id, q=q, viewpoint=VIEWPOINT)
                timings.append(float(result.timings["total"]))
            commit_samples = await commit_latency(db, fixture, commits)
            started = time.perf_counter()
            events = await scale.commit_turns(db, fixture, turns)
            event_rate = events / max(time.perf_counter() - started, 1e-9)
            step = Step(
                memories=target,
                hnsw_build_s=index.build_s,
                index_bytes=index.bytes,
                vectors=index.vectors,
                maintenance_work_mem=maintenance_work_mem,
                build_spilled_after=spilled,
                bulk_memory_rows_per_s=round(bulk_rate, 1),
                live_memory_rows_per_s=0.0,
                event_rows_per_s=round(event_rate, 1),
                commit_ms=percentiles(commit_samples),
                retrieval_ms=percentiles(timings),
            )
            # Last: the live writes add to the story, which the next step grows from.
            step.live_memory_rows_per_s = round(await live_memory_writes(db, fixture, live_rows), 1)
            log(f"step {target:,}: {json.dumps(asdict(step))}")
            measured.append(step)
    finally:
        notices.close()
    return measured


# -- the command -----------------------------------------------------------------------------------


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


async def measure(
    dsn: str,
    steps: Sequence[int],
    queries: int,
    *,
    start: int = START,
    log: Log = print,
) -> tuple[list[Step], dict[str, Any]]:
    """Build the fixture at ``start`` memories in ``dsn``, then grow and measure each step;
    answers the steps and the fixture's starting size."""
    settings = make_settings(dsn)
    db = make_engine(settings)
    lib = Engine(settings, db=db)
    try:
        fixture = await scale.build_scale_fixture(db, scale.ScaleSpec.scaled(start), log=log)
        begun = {"memories": fixture.story.memories, "events": fixture.story.events}
        return await measure_steps(db, lib, fixture, steps, queries=queries, log=log), begun
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
    p.add_argument("--start-memories", type=int, default=START)
    p.add_argument("--database-url", default=None)
    p.add_argument("--queries", type=int, default=QUERIES)
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--json", type=Path, default=None, help="also write every figure here")
    return p


async def _main(args: argparse.Namespace) -> int:
    requested = [int(s) for s in args.steps.split(",") if s.strip()]
    steps = steps_up_to(args.up_to, requested, start=args.start_memories)
    if not steps:
        print(f"error: no step above {args.start_memories:,} and at most --up-to", file=sys.stderr)
        return 2
    async with database(args.database_url) as dsn:
        measured, begun = await measure(dsn, steps, args.queries, start=args.start_memories)
    host = host_info()
    args.out.write_text(page(measured, host, requested=requested, start=begun))
    print(f"wrote {args.out}")
    if args.json is not None:
        found = document(measured, host, requested=requested, start=begun)
        args.json.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.json}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
