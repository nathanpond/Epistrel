from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tools.gates.config import load_config
from tools.gates.parts import PartsError, check_parts, collect_parts, derive_test_id
from tools.gates.spec import load_spec

from tests.conftest import REPO_ROOT

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SPEC = REPO_ROOT / "docs" / "03-requirements-spec.md"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.gates", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )


def _collect(tree: str, spec_path: Path = REAL_SPEC) -> tuple[list[str], list[str]]:
    spec = load_spec(spec_path)
    parts, findings = collect_parts(FIXTURES / tree, REPO_ROOT, spec)
    return [
        f"{p.node_id} {p.milestone} {','.join(p.requirements)} {p.origin}" for p in parts
    ], findings


def test_valid_tree_collects_every_part() -> None:
    summary, findings = _collect("tests_valid")
    assert findings == []
    assert summary == [
        "tests/gates/fixtures/tests_valid/test_valid.py::test_i_10__event_log 2 "
        "FR-STORE-10,FR-STORE-11,FR-STORE-12,FR-CONC-1,FR-CONC-2 marker",
        "tests/gates/fixtures/tests_valid/test_valid.py::test_u_01 4 "
        "FR-COMMIT-1,FR-COMMIT-2,FR-COMMIT-3 marker",
        "tests/gates/fixtures/tests_valid/test_valid.py::test_u_01 5 FR-COMMIT-4 marker",
        "tests/gates/fixtures/tests_valid/test_valid.py::test_i_27__interim 3 FR-PROF-7 marker",
        "tests/gates/fixtures/tests_valid/test_valid.py::TestGrouped::test_s_double_submit 2 "
        "FR-RESP-1 marker",
        "tests/gates/fixtures/tests_valid/test_valid.py::test_outbox 2 "
        "FR-RESP-2,FR-CONC-4,FR-CONC-5 docstring",
    ]


def test_valid_tree_records_interim_and_test_ids() -> None:
    spec = load_spec(REAL_SPEC)
    parts, _ = collect_parts(FIXTURES / "tests_valid", REPO_ROOT, spec)
    by_name = {p.node_id.rsplit("::", 1)[-1]: p for p in parts}
    assert by_name["test_i_27__interim"].interim_until == 7
    assert by_name["test_i_10__event_log"].test_id == "I-10"
    assert by_name["test_s_double_submit"].test_id == "S-DOUBLE-SUBMIT"
    assert by_name["test_outbox"].test_id == "OUTBOX"
    assert "@pytest.mark.part" in by_name["test_i_10__event_log"].source
    assert "Event log round trip" in by_name["test_i_10__event_log"].source


def test_bad_milestone_is_a_finding() -> None:
    _, findings = _collect("tests_bad_milestone")
    assert findings == [
        "bad milestone tests/gates/fixtures/tests_bad_milestone/test_bad.py::test_i_99: "
        "M9 is not a §22 milestone"
    ]


def test_later_requirement_is_a_finding() -> None:
    _, findings = _collect("tests_later")
    assert findings == [
        "later requirement tests/gates/fixtures/tests_later/test_later.py::test_i_98: "
        "FR-IMG-1 is M8, part is M2"
    ]


def test_docstring_ids_gate_from_the_latest_milestone() -> None:
    summary, findings = _collect("tests_docstring")
    assert findings == []
    # FR-STORE-10 is M2 and FR-GUARD-7 is M3 → the unmarked test gates from M3.
    assert summary == [
        "tests/gates/fixtures/tests_docstring/test_doc.py::test_i_97 3 "
        "FR-STORE-10,FR-GUARD-7 docstring"
    ]


@pytest.mark.parametrize(
    ("tree", "message"),
    [
        ("tests_module_mark", "module-level pytestmark"),
        ("tests_nonliteral", "string literals"),
        ("tests_dup_ms", "two part markers"),
        ("tests_syntax", "syntax error"),
    ],
)
def test_structural_errors(tree: str, message: str) -> None:
    with pytest.raises(PartsError, match=message):
        _collect(tree)
    assert (
        _cli("--spec", str(REAL_SPEC), "check-parts", "--tests", str(FIXTURES / tree)).returncode
        == 2
    )


def test_uncovered_m_requirement_fails_then_passes_once_a_part_exists() -> None:
    spec = load_spec(FIXTURES / "spec_m1.md")
    current = load_config(FIXTURES / "pyproject_m1.toml").current_milestone
    assert current == 1

    parts, findings = collect_parts(FIXTURES / "tests_uncovered", REPO_ROOT, spec)
    report = check_parts(parts, findings, spec, current)
    assert report.findings == ["uncovered FR-A-1 (M, M1)"]
    assert report.gated == 1

    parts, findings = collect_parts(FIXTURES / "tests_covered", REPO_ROOT, spec)
    report = check_parts(parts, findings, spec, current)
    assert report.findings == []
    assert report.summary() == "1 parts, 1 requirements gated at ≤ M1"


def test_uncovered_via_cli_exits_one() -> None:
    proc = _cli(
        "--spec",
        str(FIXTURES / "spec_m1.md"),
        "--pyproject",
        str(FIXTURES / "pyproject_m1.toml"),
        "check-parts",
        "--tests",
        str(FIXTURES / "tests_uncovered"),
    )
    assert proc.returncode == 1
    assert proc.stdout.splitlines()[0] == "uncovered FR-A-1 (M, M1)"


def test_real_tests_pass_today() -> None:
    proc = _cli("check-parts")
    assert proc.returncode == 0, proc.stdout
    current = load_config(REPO_ROOT / "pyproject.toml").current_milestone
    assert f"0 requirements gated at ≤ M{current}" in proc.stdout  # M0/M1 gate no requirements


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("test_i_10", "I-10"),
        ("test_i_18__pins", "I-18"),
        ("test_s_erasure_boundary", "S-ERASURE-BOUNDARY"),
        ("test_outbox", "OUTBOX"),
        ("test_conc_linear", "CONC-LINEAR"),
        ("test_", None),
        ("helper", None),
    ],
)
def test_test_id_mapping(name: str, expected: str | None) -> None:
    assert derive_test_id(name) == expected
