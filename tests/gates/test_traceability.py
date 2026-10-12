from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tools.gates.parts import collect_parts
from tools.gates.spec import Spec, load_spec
from tools.gates.traceability import (
    Entry,
    PlanError,
    Row,
    code_parts,
    parse_plan,
    reconcile,
    run,
)

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


def _interim(part: dict[str, Any], until: str) -> dict[str, Any]:
    return {**part, "interim_until": until}


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


def test_interim_annotation_parses() -> None:
    # #93: the comma inside "(M1, interim until M3)" used to split the entry in two.
    plan = parse_plan(PLAN.replace("I-01 (M1)", "I-01 (M1, interim until M3)"))
    assert plan.rows["FR-A-1"].entries == [Entry("I-01", 1, interim_until=3)]


def test_interim_entries_round_trip_through_render_in_any_cell_position() -> None:
    entries = [
        Entry("C-16", 2, interim_until=3),
        Entry("I-10", 2),
        Entry("S-LATER", 3, interim_until=4),
    ]
    line = Row("FR-B-1", "M", 2, entries, lineno=0).render()
    assert line == (
        "| FR-B-1 | M | M2 | "
        "C-16 (M2, interim until M3), I-10 (M2), S-LATER (M3, interim until M4) |"
    )
    plan = parse_plan(PLAN.replace("| FR-B-1 | M | M2 | I-10 (M2), S-LATER (M3) |", line))
    assert plan.rows["FR-B-1"].entries == entries
    assert plan.rows["FR-B-1"].render() == line


@pytest.mark.parametrize(
    ("cell", "reported"),
    [
        ("I-01 (M1, interim until M3", "I-01 (M1"),  # unclosed
        ("I-01 (M1, Interim until M3)", "I-01 (M1, Interim until M3)"),  # keyword is lower-case
        ("I-01 (M1, interim until 3)", "I-01 (M1, interim until 3)"),  # not a milestone label
        ("I-01 (M1, interim until M3), I-02 M2", "I-02 M2"),  # names the bad entry only
    ],
)
def test_malformed_interim_entries_stay_structural(cell: str, reported: str) -> None:
    with pytest.raises(PlanError, match=re.escape(f"malformed Tests entry {reported!r}")):
        parse_plan(PLAN.replace("I-01 (M1)", cell))


def test_interim_entries_reconcile_with_code() -> None:
    plan = PLAN.replace("I-01 (M1)", "I-01 (M1, interim until M2)")
    plain = _part("test_i_01", "M1", ["FR-A-1"])
    interim = _interim(plain, "M2")
    i_10 = _part("test_i_10", "M2", ["FR-B-1"])
    assert _check(plan, [interim], current=1) == []
    assert _check(plan, [plain], current=1) == [
        "interim mismatch FR-A-1 I-01 (M1): §15 says M2, code says None"
    ]
    assert _check(PLAN, [interim], current=1) == [
        "interim mismatch FR-A-1 I-01 (M1): §15 says None, code says M2"
    ]
    # Retired at M2: the part must be gone from code, also when only §15 carries the annotation.
    assert _check(plan, [interim, i_10], current=2) == [
        "retired part still present: FR-A-1 I-01 (M1, interim until M2)"
    ]
    assert _check(plan, [plain, i_10], current=2) == [
        "interim mismatch FR-A-1 I-01 (M1): §15 says M2, code says None",
        "retired part still present: FR-A-1 I-01 (M1, interim until M2)",
    ]


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


def test_write_with_interim_parts_round_trips(tmp_path: Path) -> None:
    plan_path = tmp_path / "04.md"
    later = "| FR-B-1 | M | M2 | I-10 (M2), S-LATER (M3, interim until M4) |"
    plan_path.write_text(PLAN.replace("| FR-B-1 | M | M2 | I-10 (M2), S-LATER (M3) |", later))
    parts = code_parts(
        [
            _interim(_part("test_i_01", "M1", ["FR-A-1"]), "M2"),  # synced onto the planned entry
            _interim(_part("test_u_03", "M1", ["FR-A-1"]), "M2"),  # new → appended with it
        ]
    )
    findings, _ = run(plan_path, SPEC_M1, parts, 1, write=True)
    assert findings == []
    text = plan_path.read_text()
    assert "| FR-A-1 | M | M1 | I-01 (M1, interim until M2), U-03 (M1, interim until M2) |" in text
    assert later in text  # later than current: preserved verbatim

    assert run(plan_path, SPEC_M1, parts, 1, write=False)[0] == []  # the next run accepts it
    run(plan_path, SPEC_M1, parts, 1, write=True)
    assert plan_path.read_text() == text  # and a second --write changes nothing

    plain = code_parts([_part("test_i_01", "M1", ["FR-A-1"]), _part("test_u_03", "M1", ["FR-A-1"])])
    assert run(plan_path, SPEC_M1, plain, 1, write=True)[0] == []
    assert "| FR-A-1 | M | M1 | I-01 (M1), U-03 (M1) |" in plan_path.read_text()  # code dropped it


def test_write_whose_output_would_not_parse_leaves_the_plan_untouched(tmp_path: Path) -> None:
    plan_path = tmp_path / "04.md"
    plan_path.write_text(PLAN)
    # `test_1x` derives the test ID "1X", which a §15 Tests entry cannot hold.
    parts = code_parts([_part("test_i_01", "M1", ["FR-A-1"]), _part("test_1x", "M1", ["FR-A-1"])])
    with pytest.raises(PlanError, match="refusing to --write: the regenerated §15 does not parse"):
        run(plan_path, SPEC_M1, parts, 1, write=True)
    assert plan_path.read_text() == PLAN


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


def test_cli_write_of_an_interim_part_leaves_a_plan_the_next_run_accepts(tmp_path: Path) -> None:
    # #93: this `--write` exited 2 after rewriting the plan, and every later run exited 2 too.
    plan_path = tmp_path / "04.md"
    plan_path.write_text(PLAN)
    parts_path = tmp_path / "parts.json"
    parts_path.write_text(json.dumps([_interim(_part("test_i_01", "M1", ["FR-A-1"]), "M2")]))

    def gate(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.gates",
                "--spec",
                str(FIXTURES / "spec_m1.md"),
                "--pyproject",
                str(FIXTURES / "pyproject_m1.toml"),  # current_milestone = 1
                "check-traceability",
                "--plan",
                str(plan_path),
                "--parts",
                str(parts_path),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=REPO_ROOT,
        )

    written = gate("--write")
    assert written.returncode == 0, written.stdout
    assert "| FR-A-1 | M | M1 | I-01 (M1, interim until M2) |" in plan_path.read_text()
    again = gate()
    assert again.returncode == 0, again.stdout
