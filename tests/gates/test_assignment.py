from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tools.gates.assignment import check_assignment
from tools.gates.spec import SpecError, load_spec

from tests.conftest import REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SPEC = REPO_ROOT / "docs" / "03-requirements-spec.md"


def _run(spec: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.gates", "--spec", str(spec), "check-assignment"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )


def test_real_docs_are_clean() -> None:
    report = check_assignment(load_spec(REAL_SPEC))
    assert report.findings == []
    assert report.defined == report.assigned_unique > 400


def test_real_docs_cli_exits_zero() -> None:
    proc = _run(REAL_SPEC)
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.splitlines()[-2].startswith("defined ")
    assert "current_milestone M0" in proc.stdout


def test_valid_fixture_is_clean() -> None:
    report = check_assignment(load_spec(FIXTURES / "valid.md"))
    assert report.findings == []
    assert (report.defined, report.assigned_unique) == (5, 5)


def test_unassigned_is_reported() -> None:
    report = check_assignment(load_spec(FIXTURES / "unassigned.md"))
    assert report.findings == ["unassigned FR-A-2"]
    assert _run(FIXTURES / "unassigned.md").returncode == 1


def test_duplicate_names_both_milestones() -> None:
    report = check_assignment(load_spec(FIXTURES / "duplicate.md"))
    assert report.findings == ["duplicate FR-A-2 (M2, M4)"]


def test_duplicate_within_one_row() -> None:
    report = check_assignment(load_spec(FIXTURES / "within_row.md"))
    assert report.findings == ["duplicate FR-A-2 (M2, M2)"]


def test_undefined_names_the_milestone() -> None:
    report = check_assignment(load_spec(FIXTURES / "undefined.md"))
    assert report.findings == ["undefined FR-B-7 (M3)"]


def test_redefined_is_reported() -> None:
    report = check_assignment(load_spec(FIXTURES / "redefined.md"))
    assert report.findings == ["redefined FR-A-1"]


def test_two_rows_for_one_milestone_is_structural() -> None:
    with pytest.raises(SpecError, match="two assignment rows"):
        load_spec(FIXTURES / "two_rows.md")
    assert _run(FIXTURES / "two_rows.md").returncode == 2


def test_missing_spec_file_exits_two(tmp_path: Path) -> None:
    proc = _run(tmp_path / "nope.md")
    assert proc.returncode == 2
    assert proc.stdout.startswith("error:")
