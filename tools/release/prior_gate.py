"""Reuse the merge gate a tagged commit already passed (ADR-221).

Every push to `main` runs `publish-edge.yml`, which calls `ci.yml` on exactly the pushed commit. A
`v*` tag must point at a commit on `main` (release.yml's `validate`), so that run already gated the
tagged commit. `release.yml`'s `prior` job asks this module whether it did: it waits for the edge
run's `ci / gate` job and answers `reuse=true` only when the job concluded `success`. Anything else
(no edge run, a cancelled or failed gate, a GitHub API error, or the wait running out) answers
`reuse=false`, and the release runs `ci.yml` itself, as before ADR-221.

Only `push` runs count: a `workflow_dispatch` run of publish-edge.yml (an edge rollback) gates the
`ref` it was given, while its `head_sha` is `main`'s tip, so it says nothing about that commit.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EDGE_WORKFLOW = "publish-edge.yml"
GATE_JOB = "ci / gate"


@dataclass(frozen=True)
class Run:
    id: int
    status: str
    event: str
    head_sha: str


@dataclass(frozen=True)
class Decision:
    reuse: bool
    reason: str
    run_id: int | None = None

    def lines(self) -> list[str]:
        out = [f"reuse={'true' if self.reuse else 'false'}", f"reason={self.reason}"]
        if self.run_id is not None:
            out.append(f"run_id={self.run_id}")
        return out


def candidates(runs: Iterable[Run], sha: str) -> list[Run]:
    """The edge runs that gated ``sha`` itself: ``push`` runs at that commit, newest first."""
    return sorted(
        (r for r in runs if r.event == "push" and r.head_sha == sha),
        key=lambda r: r.id,
        reverse=True,
    )


def gate_job(jobs: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The run's `ci / gate` job, or None when the run has not reached it."""
    return next((j for j in jobs if j.get("name") == GATE_JOB), None)


def decide(
    sha: str,
    fetch_runs: Callable[[], list[Run]],
    fetch_jobs: Callable[[int], Sequence[Mapping[str, Any]]],
    *,
    budget_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Decision:
    """Wait (up to ``budget_seconds``) for an edge run's gate over ``sha``; see the module doc."""
    start = clock()
    while True:
        runs = candidates(fetch_runs(), sha)
        if not runs:
            return Decision(False, f"no {EDGE_WORKFLOW} push run for {sha}")
        pending = False
        concluded: list[str] = []
        for run in runs:
            job = gate_job(fetch_jobs(run.id))
            if job is not None and job.get("status") == "completed":
                if job.get("conclusion") == "success":
                    return Decision(True, f"{GATE_JOB} passed in run {run.id}", run.id)
                concluded.append(f"run {run.id}: {job.get('conclusion')}")
            elif run.status != "completed":
                pending = True
            else:
                concluded.append(f"run {run.id}: finished without {GATE_JOB}")
        if not pending:
            return Decision(False, f"{GATE_JOB} did not pass ({'; '.join(concluded)})")
        if clock() - start >= budget_seconds:
            return Decision(False, f"{GATE_JOB} still running after {budget_seconds:.0f} s")
        sleep(poll_seconds)


def _gh_lines(args: list[str]) -> list[dict[str, Any]]:
    done = subprocess.run(  # noqa: S603 - fixed argv: gh api and our own arguments
        ["gh", "api", *args],  # noqa: S607 - gh from the runner PATH
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def gh_fetchers(
    repo: str, sha: str
) -> tuple[Callable[[], list[Run]], Callable[[int], Sequence[Mapping[str, Any]]]]:
    """Fetchers over `gh api` (GH_TOKEN with `actions: read`)."""

    def runs() -> list[Run]:
        rows = _gh_lines(
            [
                f"repos/{repo}/actions/workflows/{EDGE_WORKFLOW}/runs"
                f"?head_sha={sha}&event=push&per_page=20",
                "--jq",
                ".workflow_runs[] | {id, status, event, head_sha}",
            ]
        )
        return [Run(int(r["id"]), r["status"], r["event"], r["head_sha"]) for r in rows]

    def jobs(run_id: int) -> Sequence[Mapping[str, Any]]:
        return _gh_lines(
            [
                f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
                "--paginate",
                "--jq",
                ".jobs[] | {name, status, conclusion}",
            ]
        )

    return runs, jobs


def prior_gate(repo: str, sha: str, budget_seconds: float, poll_seconds: float) -> Decision:
    """`decide` over the GitHub API; an API failure answers `reuse=false` (the release runs CI)."""
    runs, jobs = gh_fetchers(repo, sha)
    try:
        return decide(sha, runs, jobs, budget_seconds=budget_seconds, poll_seconds=poll_seconds)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        return Decision(False, f"could not read the edge run ({type(exc).__name__})")
