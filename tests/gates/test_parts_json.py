from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures"


def test_json_output_has_one_entry_per_part(tmp_path: Path) -> None:
    out = tmp_path / "parts.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.gates",
            "check-parts",
            "--tests",
            str(FIXTURES / "tests_valid"),
            "--json",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout
    entries = json.loads(out.read_text())
    assert len(entries) == 6
    marked = [e for e in entries if e["node_id"].endswith("::test_u_01")]
    assert [e["milestone"] for e in marked] == ["M4", "M5"]
    first = entries[0]
    assert set(first) == {
        "node_id",
        "test_id",
        "milestone",
        "requirements",
        "interim_until",
        "source",
        "file",
        "line",
        "origin",
    }
    assert "assert True" in first["source"]
    assert entries == sorted(entries, key=lambda e: (e["file"], e["line"], e["milestone"]))
    interim = next(e for e in entries if e["node_id"].endswith("::test_i_27__interim"))
    assert interim["interim_until"] == "M7"


def test_json_is_not_written_when_findings_exist(tmp_path: Path) -> None:
    out = tmp_path / "parts.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.gates",
            "check-parts",
            "--tests",
            str(FIXTURES / "tests_later"),
            "--json",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 1
    assert not out.exists()
