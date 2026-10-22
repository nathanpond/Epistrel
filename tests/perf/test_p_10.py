"""P-10: capacity exploration (docs/04 §11), the M4 part (NFR-SCALE-1; #153, #498). This is
*informational* and never a release gate: no budget is asserted, and the test is
``perf_capacity``, so it runs only when ``-m perf_capacity`` names it. No workflow names it.

``test_p_10`` extends P-06's vector-scale curve beyond the gated range in one deployment. It
starts from the scale fixture at ``EPISTREL_TEST_CAPACITY_START`` memories (default 1k, P-06's
start) and grows the one story through 250k, 1M and 5M memories. The run stops at
``EPISTREL_TEST_CAPACITY_UP_TO`` (default 5M). Each step is measured by
``epistrel.tools.capacity.measure_steps``: the HNSW build's time and memory, the index's size,
write throughput on the memory and event tables, and retrieval and commit latency. The figures
and the first budget that fails go to ``build/perf/p_10.json`` and the page to
``build/perf/p_10.md``. The operator commits them under ``perf/results/`` and the page as
``docs/capacity-exploration.md``. A requested step above the run's limit is listed as
unmeasured.

    EPISTREL_TEST_CAPACITY_UP_TO=250000 EPISTREL_TEST_PERF_HOST=local-mac \\
        uv run pytest tests/perf/test_p_10.py -m perf_capacity -s

``test_p_10__smoke`` runs the same measurement on a 500 → 1k step with no timing bound, in PR
CI with the other ``perf_smoke`` tests, so the harness cannot rot unseen.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from epistrel.testing.fixtures import scale
from epistrel.tools import capacity
from tests.perf.scale_support import RESULT_DIR, host_info, record, scale_database, server_info

UP_TO_VAR = "EPISTREL_TEST_CAPACITY_UP_TO"
START_VAR = "EPISTREL_TEST_CAPACITY_START"
START = 1_000
SMOKE_START = 500
SMOKE_STEP = 1_000
SMOKE_WORK_MEM = "1MB"


def _log(message: str) -> None:
    print(f"[P-10] {message}", file=sys.stderr, flush=True)


async def _explore(
    pg_url: str,
    start: int,
    requested: tuple[int, ...],
    up_to: int,
    maintenance_work_mem: str = scale.MAINTENANCE_WORK_MEM,
    **sizes: int,
) -> dict[str, Any]:
    steps = capacity.steps_up_to(up_to, requested, start=start)
    async with scale_database(pg_url, scale.ScaleSpec.scaled(start), log=_log) as run:
        begun = {"memories": run.fixture.story.memories, "events": run.fixture.story.events}
        measured = await capacity.measure_steps(
            run.db,
            run.lib,
            run.fixture,
            steps,
            log=_log,
            maintenance_work_mem=maintenance_work_mem,
            **sizes,
        )
        host = host_info(await server_info(run.db))
    found = capacity.document(measured, host, requested=requested, start=begun, sizes=sizes)
    found["page"] = capacity.page(measured, host, requested=requested, start=begun)
    return found


def _as_designed(found: dict[str, Any], steps: list[int], work_mem: str) -> None:
    """Each step measured the bigger set: the index holds the step's vectors and grows."""
    points = found["points"]
    assert [p["memories"] for p in points] == steps
    assert [p["vectors"] for p in points] == steps
    sizes = [p["index_bytes"] for p in points]
    assert sizes == sorted(sizes), sizes
    assert len(set(sizes)) == len(sizes), sizes
    for p in points:
        assert p["hnsw_build_s"] > 0, p
        assert min(p["bulk_memory_rows_per_s"], p["live_memory_rows_per_s"]) > 0, p
        assert p["event_rows_per_s"] > 0, p
        assert p["commit_ms"]["p95"] > 0, p
        assert p["retrieval_ms"]["p95"] > 0, p
        assert p["maintenance_work_mem"] == work_mem


@pytest.mark.perf_capacity
@pytest.mark.part("M4", "NFR-SCALE-1")
async def test_p_10(pg_url: str) -> None:
    """The curve at 250k, 1M and 5M memories (up to the run's limit): every figure is recorded
    with the first budget that fails, and each step above the limit is listed as unmeasured.
    No budget is asserted (informational).

    Complement asserted: the index holds every vector at each step and grows with it, so each
    point measures the bigger set, and the steps measured and unmeasured together are exactly
    the requested ones."""
    up_to = int(os.environ.get(UP_TO_VAR, str(capacity.DEFAULT_STEPS[-1])))
    start = int(os.environ.get(START_VAR, str(START)))
    requested = capacity.DEFAULT_STEPS
    found = await _explore(pg_url, start, requested, up_to)
    page = found.pop("page")
    record("p_10", found)
    (RESULT_DIR / "p_10.md").write_text(page)
    steps = capacity.steps_up_to(up_to, requested, start=start)
    _as_designed(found, steps, scale.MAINTENANCE_WORK_MEM)
    assert sorted(steps + found["unmeasured_steps"]) == sorted(requested)
    measured = [capacity.Step(**p) for p in found["points"]]
    failure = capacity.first_failure(measured)
    assert found["first_failure"] == (
        None if failure is None else {"memories": failure[0], "budget": failure[1]}
    )


@pytest.mark.perf_smoke
async def test_p_10__smoke(pg_url: str) -> None:
    """P-10's plumbing, in PR CI: one 500 → 1k step through the same measurement and page,
    with no timing bound. The build runs under the smallest ``maintenance_work_mem`` (1MB), which
    1k vectors of the fixture's dimension outgrow, so pgvector's NOTICE is seen and the spill
    point recorded.

    Complement asserted: the larger requested steps are listed as unmeasured on the page."""
    requested = (SMOKE_STEP, *capacity.DEFAULT_STEPS)
    found = await _explore(
        pg_url,
        SMOKE_START,
        requested,
        SMOKE_STEP,
        maintenance_work_mem=SMOKE_WORK_MEM,
        queries=5,
        commits=5,
        turns=10,
        live_rows=20,
    )
    record("p_10_smoke", {k: v for k, v in found.items() if k != "page"})
    _as_designed(found, [SMOKE_STEP], SMOKE_WORK_MEM)
    (point,) = found["points"]
    assert 0 < point["build_spilled_after"] < SMOKE_STEP, point
    assert (found["commit_samples"], found["turns_per_step"]) == (5, 10)
    assert found["unmeasured_steps"] == list(capacity.DEFAULT_STEPS)
    assert "Unmeasured: 250,000, 1,000,000, 5,000,000 memories." in found["page"]
    assert "| 1,000 |" in found["page"]
