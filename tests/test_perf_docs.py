"""The weekly perf run's page renderer (``epistrel.tools.perf_docs``) and P-10's report
(``epistrel.tools.capacity``), without a database (#153)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from epistrel.tools import capacity, perf_docs

HOST = {
    "host": "github-runner",
    "platform": "Linux-6.8-x86_64",
    "vcpus": 4,
    "ram_gb": 15.6,
    "runner_image": "ubuntu24 20261012.1",
    "sha": "0123456789abcdef",
    "postgres": "16.15 (Debian)",
    "pgvector": "0.8.7",
    "shared_buffers": "128MB",
    "maintenance_work_mem": "64MB",
}


def _q(p95: float) -> dict[str, float]:
    return {"p50": p95 / 2, "p95": p95, "p99": p95 * 1.2, "max": p95 * 1.5}


def _results(tmp: Path, *, p01: float = 90.0, p02: float = 12.0) -> Path:
    results = tmp / "perf"
    results.mkdir()
    files: dict[str, dict[str, Any]] = {
        "p_01": {
            "measured_at": "2026-10-18T03:40:00Z",
            "queries": 500,
            "memories": 50_000,
            "events": 100_000,
            "total_ms": _q(p01),
            "stages_ms": {"resolve": _q(10.0), "vector": _q(20.0), "total": _q(p01)},
            "degraded": {},
            "host": HOST,
        },
        "p_02": {
            "measured_at": "2026-10-18T03:50:00Z",
            "claims": 2_000,
            "facts": 50_000,
            "check_ms": _q(p02),
            "verdicts": {"supported->supported": 800},
            "host": HOST,
        },
        "p_06": {
            "measured_at": "2026-10-18T04:30:00Z",
            "events": 2_000,
            "queries_per_step": 200,
            "maintenance_work_mem": "1GB",
            "points": [
                {
                    "memories": m,
                    "hnsw_build_s": m / 10_000,
                    "index_bytes": m * 4_000,
                    "vectors": m,
                    "retrieval_ms": _q(40.0 + m / 1_000),
                    "gated": m <= 50_000,
                    "within_budget": 40.0 + m / 1_000 <= 150.0,
                }
                for m in (1_000, 10_000, 50_000, 100_000)
            ],
            "host": HOST,
        },
        "p_06_retirement": {
            "measured_at": "2026-10-18T03:55:00Z",
            "vectors_ranked": 49_760,
            "ranking_ms": 300.0,
            "forced_run_ms": 420.0,
            "retired": {"covered": 500},
            "over_commit_budget": True,
            "over_single_commit_bound": False,
            "host": HOST,
        },
    }
    for name, body in files.items():
        (results / f"{name}.json").write_text(json.dumps(body))
    return results


def test_the_pages_carry_the_host_and_the_verdicts(tmp_path: Path) -> None:
    """Every table names its host as ``github-runner (not the reference host)``; the budgets
    table gives each p95 its verdict; capacity is claimed only for the measured range, with
    the 100k point reported but not gated.

    Complement asserted: a p95 over its budget reads "over budget", and the page never calls
    the runner the reference host."""
    docs = tmp_path / "docs"
    docs.mkdir()
    perf_docs.render(_results(tmp_path, p01=180.0), docs)
    perf = (docs / "perf.md").read_text()
    assert "**Host: github-runner (not the reference host).**" in perf
    assert perf.count("github-runner (not the reference host)") >= 6
    assert "| P-01 retrieval | NFR-PERF-1 | p95 ≤ 150 ms | 180.0 ms | **over budget** |" in perf
    assert (
        "| P-02 deterministic verify | NFR-PERF-2 | p95 ≤ 50 ms | 12.0 ms | within budget |" in perf
    )
    assert "| 49,760 | 300.0 | 420.0 |" in perf
    assert "Host: reference" not in perf
    capacity_md = (docs / "capacity.md").read_text()
    assert "claimed **only for the measured range**" in capacity_md
    assert "| 100,000 |" in capacity_md
    assert "no (reported)" in capacity_md
    assert "`maintenance_work_mem` 1GB" in capacity_md


def test_history_keeps_every_run(tmp_path: Path) -> None:
    """Each render appends one history row and keeps the earlier ones, oldest first.

    Complement asserted: with no results at all the page says *not measured in this run* and
    adds no row."""
    docs = tmp_path / "docs"
    docs.mkdir()
    results = _results(tmp_path)
    perf_docs.render(results, docs)
    perf_docs.render(results, docs)
    rows = perf_docs.history_rows((docs / "perf.md").read_text())
    assert len(rows) == 2
    assert rows[0].startswith(
        "| 2026-10-18 | 0123456789ab | github-runner (not the reference host) |"
    )
    empty = tmp_path / "none"
    empty.mkdir()
    perf_docs.render(empty, docs)
    page = (docs / "perf.md").read_text()
    assert len(perf_docs.history_rows(page)) == 2
    assert perf_docs.NOT_MEASURED in page


def test_the_structured_workload_is_summarised(tmp_path: Path) -> None:
    """#153: P-04's structured run (its sidecar as ``p_04_structured.json``) is summarised in
    docs/perf.md: the verdict with its host class, the aggregate criteria with the retrieval
    and verify budgets, the stories that pass, and the commit, retrieve and verify stages.

    Complement asserted: a failed budget reads **FAIL**, a run without the sidecar says *not
    measured in this run* for P-04, and the structured run alone adds no history row."""
    sidecar = {
        "info": {
            "host_class": "ci",
            "host": "Linux x86_64, 4 CPUs",
            "engine_sha": "abc123",
            "started": "2026-10-18 03:00 UTC",
            "stories": 10,
            "memories": 10_000,
            "events": 20_000,
            "interval_s": 60.0,
            "minutes": 30,
            "stub_latency_s": 2.0,
        },
        "passed": False,
        "aggregate": [
            {
                "name": "retrieval p95 (server retrieve stage)",
                "measured": "99.0% of 900",
                "bound": "≥ 95% ≤ 150 ms",
                "passed": True,
            },
            {
                "name": "deterministic verify p95 (server verify stage)",
                "measured": "80.0% of 300",
                "bound": "≥ 95% ≤ 50 ms",
                "passed": False,
            },
        ],
        "stories": [{"criteria": [{"passed": True}]}, {"criteria": [{"passed": False}]}],
        "stages": {
            "commit": {"count": 300, "p50": 0.05, "p95": 0.1, "p99": 0.2},
            "retrieve": {"count": 900, "p50": 0.02, "p95": 0.15, "p99": 0.2},
            "verify": {"count": 300, "p50": 0.01, "p95": 0.1, "p99": 0.2},
        },
    }
    docs = tmp_path / "docs"
    docs.mkdir()
    only = tmp_path / "only"
    only.mkdir()
    (only / "p_04_structured.json").write_text(json.dumps(sidecar))
    perf_docs.render(only, docs)
    page = (docs / "perf.md").read_text()
    assert "## P-04: the structured reference workload (NFR-PERF-4, M4)" in page
    assert "**FAIL** on host class `ci` (reported, not gated)" in page
    assert (
        "| retrieval p95 (server retrieve stage) | 99.0% of 900 | ≥ 95% ≤ 150 ms | PASS |" in page
    )
    assert (
        "| deterministic verify p95 (server verify stage) | 80.0% of 300 | ≥ 95% ≤ 50 ms "
        "| **FAIL** |" in page
    )
    assert "1 of 2 stories pass" in page
    assert "| retrieve | 900 | ≤ 20 ms | ≤ 150 ms | ≤ 200 ms |" in page
    assert perf_docs.history_rows(page) == []
    perf_docs.render(_results(tmp_path), docs)
    assert perf_docs.NOT_MEASURED in (docs / "perf.md").read_text().split("## P-04")[1]


def test_capacity_reports_the_first_failing_budget(tmp_path: Path) -> None:
    """P-10's steps stay above the fixture's start and at most ``--up-to``; the report names
    the first step whose retrieval or commit budget failed, each step's build memory, and the
    requested steps left unmeasured; pgvector's "no longer fits" NOTICE gives the spill point.

    Complement asserted: with every step within budget and every requested step measured, the
    report says none failed and lists nothing as unmeasured."""
    assert capacity.steps_up_to(1_000_000) == [250_000, 1_000_000]
    assert capacity.steps_up_to(50_000) == []
    assert capacity.steps_up_to(250_000, start=1_000) == [250_000]

    def step(
        memories: int, p95: float, commit: float = 20.0, spilled: int | None = None
    ) -> capacity.Step:
        return capacity.Step(
            memories=memories,
            hnsw_build_s=10.0,
            index_bytes=2**20,
            vectors=memories,
            maintenance_work_mem="1GB",
            build_spilled_after=spilled,
            bulk_memory_rows_per_s=5_000.0,
            live_memory_rows_per_s=1_000.0,
            event_rows_per_s=900.0,
            commit_ms={"p50": commit / 2, "p95": commit, "p99": commit, "max": commit},
            retrieval_ms={"p50": 1.0, "p95": p95, "p99": p95, "max": p95},
        )

    measured = [step(250_000, 120.0, spilled=180_000), step(1_000_000, 190.0)]
    assert capacity.first_failure(measured) == (1_000_000, "retrieval p95 190.0 ms > 150 ms")
    commit_only = [step(250_000, 100.0, commit=300.0)]
    assert capacity.first_failure(commit_only) == (250_000, "commit p95 300.0 ms > 250 ms")
    host = {"host": "local", "platform": "x", "vcpus": 4, "ram_gb": 16}
    page = capacity.page(measured, host, requested=capacity.DEFAULT_STEPS)
    assert (
        "First budget that fails: at 1,000,000 memories, retrieval p95 190.0 ms > 150 ms." in page
    )
    assert "(not the reference host)" in page
    assert "| 1GB, spilled after 180,000 tuples |" in page
    assert "| 1GB, fit in memory |" in page
    assert "Unmeasured: 5,000,000 memories." in page
    found = capacity.document(
        measured, host, requested=capacity.DEFAULT_STEPS, start={"memories": 1_000}
    )
    assert found["unmeasured_steps"] == [5_000_000]
    assert found["first_failure"] == {
        "memories": 1_000_000,
        "budget": "retrieval p95 190.0 ms > 150 ms",
    }
    assert found["informational"] is True
    notice = "hnsw graph no longer fits into maintenance_work_mem after 180000 tuples"
    assert capacity.spilled_after(["building index", notice]) == 180_000
    assert capacity.spilled_after(["building index"]) is None
    fine = capacity.page([step(250_000, 100.0)], {"host": "local"}, requested=(250_000,))
    assert "none in the measured steps" in fine
    assert "Unmeasured" not in fine
