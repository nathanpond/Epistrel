"""ADR-221: `tools.release prior-gate` reuses the tagged commit's edge gate only when it passed."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Any

import pytest
from tools.release import prior_gate as pg
from tools.release.__main__ import main
from tools.release.prior_gate import GATE_JOB, Decision, Run, candidates, decide

SHA = "a" * 40
OTHER = "b" * 40


def _job(status: str, conclusion: str | None) -> dict[str, Any]:
    return {"name": GATE_JOB, "status": status, "conclusion": conclusion}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _decide(
    runs: list[Run], jobs: Mapping[int, list[list[dict[str, Any]]]], budget: float = 600
) -> tuple[Decision, Clock]:
    """``jobs[run_id]`` is the job list per successive poll (the last one repeats)."""
    clock = Clock()
    seen: dict[int, int] = {}

    def fetch_jobs(run_id: int) -> list[Mapping[str, Any]]:
        polls = jobs.get(run_id, [[]])
        i = seen.get(run_id, 0)
        seen[run_id] = i + 1
        return list(polls[min(i, len(polls) - 1)])

    decision = decide(
        SHA,
        lambda: runs,
        fetch_jobs,
        budget_seconds=budget,
        poll_seconds=60,
        clock=clock,
        sleep=clock.sleep,
    )
    return decision, clock


def test_a_passed_gate_is_reused() -> None:
    decision, clock = _decide(
        [Run(7, "completed", "push", SHA)], {7: [[_job("completed", "success")]]}
    )
    assert decision == Decision(True, f"{GATE_JOB} passed in run 7", 7)
    assert decision.lines() == ["reuse=true", f"reason={GATE_JOB} passed in run 7", "run_id=7"]
    assert clock.sleeps == []


def test_a_running_gate_is_waited_for() -> None:
    decision, clock = _decide(
        [Run(7, "in_progress", "push", SHA)],
        {7: [[], [_job("in_progress", None)], [_job("completed", "success")]]},
    )
    assert decision.reuse
    assert clock.sleeps == [60, 60]


def test_the_gate_counts_even_while_the_edge_publish_runs() -> None:
    """The run is still publishing edge; its gate job already passed."""
    decision, _ = _decide(
        [Run(7, "in_progress", "push", SHA)], {7: [[_job("completed", "success")]]}
    )
    assert decision.reuse


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_a_gate_that_did_not_pass_is_not_reused(conclusion: str) -> None:
    decision, clock = _decide(
        [Run(7, "completed", "push", SHA)], {7: [[_job("completed", conclusion)]]}
    )
    assert not decision.reuse
    assert conclusion in decision.reason
    assert decision.lines()[0] == "reuse=false"
    assert clock.sleeps == []


def test_no_edge_run_is_not_reused() -> None:
    decision, _ = _decide([], {})
    assert not decision.reuse
    assert "no publish-edge.yml push run" in decision.reason


def test_only_push_runs_at_the_tagged_commit_count() -> None:
    """A dispatch run (an edge rollback) gates its `ref`, not its head_sha; another commit's run
    says nothing about this one."""
    runs = [Run(8, "completed", "workflow_dispatch", SHA), Run(9, "completed", "push", OTHER)]
    assert candidates(runs, SHA) == []
    decision, _ = _decide(
        runs, {8: [[_job("completed", "success")]], 9: [[_job("completed", "success")]]}
    )
    assert not decision.reuse


def test_a_completed_run_without_the_gate_is_not_reused() -> None:
    decision, _ = _decide([Run(7, "completed", "push", SHA)], {7: [[]]})
    assert not decision.reuse
    assert "finished without" in decision.reason


def test_a_later_passed_attempt_wins_over_a_failed_one() -> None:
    runs = [Run(5, "completed", "push", SHA), Run(7, "completed", "push", SHA)]
    assert [r.id for r in candidates(runs, SHA)] == [7, 5]
    decision, _ = _decide(
        runs, {5: [[_job("completed", "success")]], 7: [[_job("completed", "failure")]]}
    )
    assert decision == Decision(True, f"{GATE_JOB} passed in run 5", 5)


def test_the_wait_runs_out_and_the_release_runs_ci() -> None:
    decision, clock = _decide(
        [Run(7, "queued", "push", SHA)], {7: [[_job("queued", None)]]}, budget=150
    )
    assert not decision.reuse
    assert "still running after 150 s" in decision.reason
    assert clock.now >= 150


def test_an_api_error_runs_ci(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(args: list[str]) -> list[dict[str, Any]]:
        raise subprocess.CalledProcessError(1, ["gh", "api", *args])

    monkeypatch.setattr(pg, "_gh_lines", broken)
    assert main(["prior-gate", "--repo", "o/r", "--sha", SHA, "--budget-minutes", "0"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "reuse=false"
    assert "CalledProcessError" in out[1]


def test_the_cli_asks_for_push_runs_at_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []

    def fake(args: list[str]) -> list[dict[str, Any]]:
        asked.append(args)
        if "/runs?" in args[0]:
            return [{"id": 3, "status": "completed", "event": "push", "head_sha": SHA}]
        return [{"name": GATE_JOB, "status": "completed", "conclusion": "success"}]

    monkeypatch.setattr(pg, "_gh_lines", fake)
    assert pg.prior_gate("o/r", SHA, 0, 1).reuse
    assert asked[0][0] == (
        f"repos/o/r/actions/workflows/publish-edge.yml/runs?head_sha={SHA}&event=push&per_page=20"
    )
    assert asked[1][0] == "repos/o/r/actions/runs/3/jobs?per_page=100"
    assert "--paginate" in asked[1]
