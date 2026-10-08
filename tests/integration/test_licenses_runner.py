"""The real license runner syncs a runtime-only env and judges the actual closure."""

from __future__ import annotations

from tools.gates.licenses import run

from tests.conftest import REPO_ROOT


def test_real_runner_reports_the_actual_closure(tmp_path: str) -> None:
    report = run(
        REPO_ROOT,
        REPO_ROOT / "uv.lock",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / ".venv-runtime",
        sync=True,
    )
    assert report.judged >= 20
    # psycopg/psycopg-binary are LGPL-3.0-only, allowed since invariant 2 was amended (2026-10-08).
    assert report.findings == [], report.findings
