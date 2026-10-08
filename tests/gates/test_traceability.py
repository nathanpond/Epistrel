from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tools.gates.parts import collect_parts
from tools.gates.spec import Spec, load_spec
from tools.gates.traceability import PlanError, code_parts, parse_plan, reconcile, run

from tests.conftest import REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures"
SPEC_M1 = load_spec(FIXTURES / "spec_m1.md")  # FR-A-1 (M) at M1; FR-A-2 (S), FR-B-1 (M) at M2

PLAN = """# Testing plan

## 15. Requirement-level traceability (appendix)

Intro.

| Requirement | Priority | Milestone | Tests |
|---|---|---|---|
| FR-A-1 | M | M1 | I-01 (M1) |
| FR-A-2 | S | M2 | — |
| FR-B-1 | M | M2 | I-10 (M2), S-LATER (M3) |

**Not release-gated** (S or C requirements with no named test): FR-A-2 (S).
**M requirements first gated per milestone:** M1 1 · M2 1.

---
"""


def _part(name: str, ms: str, reqs: list[str], file: str = "tests/test_a.py") -> dict[str, Any]:
    return {
        "node_id": f"{file}::{name}",
        "test_id": name.removeprefix("test_").split("__")[0].upper().replace("_", "-"),
        "milestone": ms,
        "requirements": reqs,
        "interim_until": None,
        "source": "",
        "file": file,
        "line": 1,
        "origin": "marker",
    }


def _check(
    plan_text: str, parts: list[dict[str, Any]], current: int, spec: Spec = SPEC_M1
) -> list[str]:
    return reconcile(parse_plan(plan_text), spec, code_parts(parts), current)


def test_consistent_plan_and_code_pass() -> None:
    assert _check(PLAN, [_part("test_i_01", "M1", ["FR-A-1"])], current=1) == []


def test_code_part_absent_from_the_table_fails() -> None:
    parts = [_part("test_i_01", "M1", ["FR-A-1"]), _part("test_i_02", "M1", ["FR-A-1"])]
    assert _check(PLAN, parts, current=1) == ["missing from §15: FR-A-1 ← test_i_02 (M1)"]


def test_planned_entry_with_no_part_fails_only_once_its_milestone_is_current() -> None:
    parts = [_part("test_i_01", "M1", ["FR-A-1"])]
    assert _check(PLAN, parts, current=1) == []
    assert _check(PLAN, parts, current=2) == ["planned but absent: FR-B-1 I-10 (M2)"]


def test_milestone_disagreement_with_section_22_fails() -> None:
    plan = PLAN.replace("| FR-B-1 | M | M2 |", "| FR-B-1 | M | M3 |")
    findings = _check(plan, [_part("test_i_01", "M1", ["FR-A-1"])], current=1)
    assert "milestone mismatch FR-B-1: §15 says M3, §22 says M2" in findings


def test_priority_disagreement_and_missing_row_fail() -> None:
    plan = PLAN.replace("| FR-A-2 | S | M2 | — |\n", "").replace("| FR-A-1 | M |", "| FR-A-1 | S |")
    findings = _check(plan, [_part("test_i_01", "M1", ["FR-A-1"])], current=1)
    assert "missing row: FR-A-2 is assigned in §22 but has no §15 row" in findings
    assert "priority mismatch FR-A-1: §15 says S, docs/03 says M" in findings


def test_docstring_parts_are_ignored_and_unmappable_parts_reported() -> None:
    doc = {**_part("test_whatever", "M1", ["FR-A-1"]), "origin": "docstring"}
    assert _check(PLAN, [_part("test_i_01", "M1", ["FR-A-1"]), doc], current=1) == []
    bad = {**_part("test_i_01", "M1", ["FR-A-1"]), "test_id": None, "node_id": "tests/t.py::test_"}
    assert _check(PLAN, [bad, _part("test_i_01", "M1", ["FR-A-1"])], current=1) == [
        "unmappable part tests/t.py::test_: name does not map to a docs/04 test ID"
    ]


def test_malformed_tests_cell_is_structural() -> None:
    with pytest.raises(PlanError, match="malformed Tests entry"):
        parse_plan(PLAN.replace("I-01 (M1)", "I-01 M1"))


def test_write_merges_rows_at_or_before_current_and_is_idempotent(tmp_path: Path) -> None:
    plan_path = tmp_path / "04.md"
    plan_path.write_text(PLAN)
    parts = code_parts(
        [
            _part("test_i_01", "M1", ["FR-A-1"]),
            _part("test_u_03", "M1", ["FR-A-1"]),  # new at M1 → appended, U before I
            _part("test_i_11", "M2", ["FR-B-1"]),  # M2 > current → row must stay byte-identical
        ]
    )
    findings, _ = run(plan_path, SPEC_M1, parts, 1, write=True)
    text = plan_path.read_text()
    assert "| FR-A-1 | M | M1 | I-01 (M1), U-03 (M1) |" in text
    assert "| FR-B-1 | M | M2 | I-10 (M2), S-LATER (M3) |" in text  # untouched (later than current)
    assert "**M requirements first gated per milestone:** M1 1 · M2 1." in text
    assert findings == [
        "missing from §15: FR-B-1 ← test_i_11 (M2)"
    ]  # still reported, never written

    before = plan_path.read_text()
    run(plan_path, SPEC_M1, parts, 1, write=True)
    assert plan_path.read_text() == before


def test_write_refuses_on_milestone_disagreement(tmp_path: Path) -> None:
    plan_path = tmp_path / "04.md"
    plan_path.write_text(PLAN.replace("| FR-B-1 | M | M2 |", "| FR-B-1 | M | M3 |"))
    with pytest.raises(PlanError, match="refusing to --write"):
        run(plan_path, SPEC_M1, [], 1, write=True)
    assert "| FR-B-1 | M | M3 |" in plan_path.read_text()


def test_real_docs_agree_with_section_22_and_pass_today(tmp_path: Path) -> None:
    spec = load_spec(REPO_ROOT / "docs" / "03-requirements-spec.md")
    parts, _ = collect_parts(REPO_ROOT / "tests", REPO_ROOT, spec)
    plan_path = REPO_ROOT / "docs" / "04-testing-plan.md"
    findings, summary = run(
        plan_path, spec, code_parts([p.to_json() for p in parts]), 0, write=False
    )
    assert findings == []
    assert summary.startswith("438 §15 rows")
    assert summary.endswith("§15 and §22 agree on every milestone")

    # --write on a copy of the real plan at current_milestone = 0 is a byte-for-byte no-op.
    copy = tmp_path / "04.md"
    copy.write_text(plan_path.read_text())
    run(copy, spec, code_parts([p.to_json() for p in parts]), 0, write=True)
    assert copy.read_text() == plan_path.read_text()


def test_cli_passes_today_and_exits_two_without_parts_json(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-parts", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-traceability"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout
    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.gates",
            "check-traceability",
            "--parts",
            str(tmp_path / "x.json"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    assert missing.returncode == 2
    bad = tmp_path / "parts.json"
    bad.write_text(json.dumps([_part("test_i_99", "M2", ["FR-RESP-1"])]))
    broken = subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-traceability", "--parts", str(bad)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    assert broken.returncode == 1
    assert "missing from §15: FR-RESP-1 ← test_i_99 (M2)" in broken.stdout
