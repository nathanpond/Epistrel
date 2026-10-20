"""#157's labeled sets: schema, sizes, provenance, authoring and the forbidden imports.

Every NFR-QUAL suite's set loads under its suite's case schema and meets its size, matches what
its authoring module generates, and its directory's ``PROVENANCE.md`` carries the owner's
review line (``approved by owner YYYY-MM-DD``; ``pending`` passes only where approval is not
required). No suite imports a test double of the engine."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from epistrel.evals import SUITES, MalformedCase
from epistrel.evals import sets as labeled
from epistrel.evals.authoring import GENERATORS, render

REPO = Path(__file__).resolve().parents[2]
SETS = REPO / "evals" / "sets"
EVALS_SRC = REPO / "src" / "epistrel" / "evals"
FORBIDDEN = ("tests", "unittest.mock", "epistrel.testing")
ALLOWED = ("epistrel.testing.fixtures",)
"""A suite may use #153's fixture builders (data, not doubles); nothing else under testing."""
SIZES = {
    "e01": 400,
    "e03": 150,
    "e04": 200,
    "e05": 300,
    "e06": 200,
    "e07": 200,
    "e10": 300,
    "q01": 100,
    "q02": 100,
    "q03": 50,
}


def test_every_set_loads_meets_its_size_and_is_approved_or_pending() -> None:
    """The ten NFR-QUAL suites' sets load under their schemas, meet #157's sizes, match their
    authoring modules, carry a provenance line, and the review digest is current."""
    suites = labeled.labeled_suites()
    assert {s.name for s in suites} == set(SIZES)
    for suite in suites:
        assert suite.min_cases >= SIZES[suite.name], suite.name
        assert len(suite.load(SETS)) >= suite.min_cases, suite.name
    assert labeled.problems(SETS, require_approved=False) == []
    for relative, generate in GENERATORS.items():
        assert (SETS / relative).read_text(encoding="utf-8") == render(generate()), relative


def test_provenance_lines_are_required(tmp_path: Path) -> None:
    """A set directory needs its ``PROVENANCE.md`` with the review line: an approval date
    passes everywhere, ``pending`` passes only where approval is not required, and a file
    without the line or no file fails."""
    directory = tmp_path / "set"
    directory.mkdir()
    assert labeled.provenance_problem(directory, require_approved=False) == "set: no PROVENANCE.md"
    path = directory / labeled.PROVENANCE
    path.write_text("# set\n\napproved by owner: pending\n", encoding="utf-8")
    assert labeled.provenance_problem(directory, require_approved=False) is None
    assert "not approved" in str(labeled.provenance_problem(directory, require_approved=True))
    path.write_text("# set\n\napproved by owner 2026-10-21\n", encoding="utf-8")
    assert labeled.provenance_problem(directory, require_approved=True) is None
    path.write_text("# set\n\nreviewed, looks fine\n", encoding="utf-8")
    assert "no 'approved by owner" in str(
        labeled.provenance_problem(directory, require_approved=False)
    )


def test_a_malformed_or_short_set_fails(tmp_path: Path) -> None:
    """A case that breaks its suite's schema fails the set with its line; a set below its size
    fails ``--check-sets``."""
    copy = tmp_path / "sets"
    shutil.copytree(SETS, copy)
    path = copy / "entailment" / "cases.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([*lines[:-1], '{"id": "entailment-x-0001"}']) + "\n", "utf-8")
    with pytest.raises(MalformedCase, match=rf"line {len(lines)}"):
        SUITES["e01"].load(copy)
    path.write_text("\n".join(lines[:10]) + "\n", "utf-8")
    found = labeled.problems(copy, require_approved=False)
    assert "e01: 10 cases, fewer than its 400" in found


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_no_suite_imports_a_test_double() -> None:
    """The suites, the harness and the authoring run the production code paths: nothing under
    ``epistrel.evals`` imports ``tests``, ``unittest.mock`` or ``epistrel.testing`` (bar its
    fixture builders). Complement: the check flags such an import."""
    offending = [
        f"{path.relative_to(REPO)}: {name}"
        for path in sorted(EVALS_SRC.rglob("*.py"))
        for name in _imports(path)
        if name.startswith(FORBIDDEN) and not name.startswith(ALLOWED)
    ]
    assert offending == []
    assert "tests.verify_support".startswith(FORBIDDEN)
    assert "epistrel.testing.faults".startswith(FORBIDDEN)
    assert not "epistrel.testing.fixtures.scale".startswith(FORBIDDEN) or (
        "epistrel.testing.fixtures.scale".startswith(ALLOWED)
    )
