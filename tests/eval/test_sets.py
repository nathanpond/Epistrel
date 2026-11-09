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
from epistrel.evals.judging import CALIBRATION_MIN_PAIRS, load_pairs
from epistrel.evals.suites.priv_think_leak import ThinkLeakCase

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
    # #158's intervention scenarios, shared by E-12 and E-13.
    "e12": 21,
    "e13": 21,
    # #179's zero-leak sets (NFR-QUAL-8's 300 cases a metric) and E-01 M5's viewpoint cases.
    "q04": 300,
    "priv_think_leak": 300,
    "e01_viewpoint": 300,
    # #201's time-estimation set (E-09, NFR-QUAL-9).
    "e09": 200,
    # #270/#271's image-policy set (E-11, NFR-QUAL-12), checked once committed. Its sizes are per
    # policy (100 cases a metric, the tag coverage minimums), and a run enforces them.
    "e11": 1,
}


def test_every_set_loads_meets_its_size_and_is_approved_or_pending() -> None:
    """The NFR-QUAL suites' sets load under their schemas, meet #157's (and #158's) sizes,
    match their authoring modules, carry a provenance line, and the review digest is current;
    #158's judge calibration set holds at least 200 pairs."""
    # #270: E-11's set is optional until the owner commits it (#271); absent, it is not checked.
    suites = [
        s
        for s in labeled.labeled_suites()
        if not (s.set_optional and not (SETS / s.set_file).is_file())
    ]
    assert {s.name for s in suites} == set(SIZES)
    for suite in suites:
        assert suite.min_cases >= SIZES[suite.name], suite.name
        assert len(suite.load(SETS)) >= suite.min_cases, suite.name
    # #179: the private-thought set is 3 x 115 withheld cases (past the 300-case floor, so a
    # seed that loses a few cases to model errors still completes 300) plus 96 controls, 32
    # per category (#575).
    think = [c for c in SUITES["priv_think_leak"].load(SETS) if isinstance(c, ThinkLeakCase)]
    assert sum(not c.control for c in think) == 345
    assert sum(c.control for c in think) == 96
    for category in ("verbatim", "paraphrase", "narrated"):
        assert sum(c.control and c.inputs.category == category for c in think) == 32, category
    assert labeled.problems(SETS, require_approved=False) == []
    assert len(load_pairs(SETS)) >= CALIBRATION_MIN_PAIRS
    for relative, generate in GENERATORS.items():
        assert (SETS / relative).read_text(encoding="utf-8") == render(generate()), relative


def test_time_estimation_set_is_stratified_and_generated(tmp_path: Path) -> None:
    """#201: E-09's 200 cases span the seven activity families and the three kinds (explicit,
    implicit, inferred); the digest shows every family among its sampled cases (stratified,
    no scripted answer) with its counts per kind, and an edited case is drift
    (``--check-sets`` names the set)."""
    from epistrel.evals.suites.e09_time_estimation import FAMILIES, KINDS, TimeEstimationCase

    cases = [c for c in SUITES["e09"].load(SETS) if isinstance(c, TimeEstimationCase)]
    assert len(cases) == 200
    assert {c.family for c in cases} == set(FAMILIES)
    assert {c.kind for c in cases} == set(KINDS)
    for case in cases:
        lo, hi = case.acceptable_minutes
        minutes = case.stub_answer.minutes
        assert minutes is not None, case.id
        assert lo <= minutes <= hi, case.id
    assert "time_estimation/cases.jsonl" in labeled.STRATIFIED
    digest = labeled.digest_text(SETS)
    section = digest.split("## `time_estimation/cases.jsonl` (e09, 200 cases)", 1)[1]
    section = section.split("\n## ", 1)[0]
    sampled = [line for line in section.splitlines() if line.startswith("- `time_estimation-")]
    assert len(sampled) == labeled.DIGEST_SAMPLES
    for family in FAMILIES:
        assert any(f"- `time_estimation-{family}-" in line for line in sampled), family
    assert "stub_answer" not in section
    assert "Labels: explicit " in section

    copy = tmp_path / "sets"
    shutil.copytree(SETS, copy)
    path = copy / "time_estimation" / "cases.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"narrated_text": "', '"narrated_text": "Then ', 1)
    path.write_text("\n".join(lines) + "\n", "utf-8")
    assert "time_estimation/cases.jsonl differs from what its authoring module generates" in (
        labeled.problems(copy, require_approved=False)
    )


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
