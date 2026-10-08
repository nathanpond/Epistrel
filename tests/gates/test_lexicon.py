from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tools.gates.lexicon import LexiconError, check, inflections, parse_lexicon, tokenize
from tools.gates.spec import load_spec

from tests.conftest import REPO_ROOT

DOCS04 = REPO_ROOT / "docs" / "04-testing-plan.md"
ROWS = set(load_spec(REPO_ROOT / "docs" / "03-requirements-spec.md").assignment)
LEXICON = parse_lexicon(DOCS04.read_text(), ROWS)


def _part(milestone: str, body: str, node_id: str = "tests/test_x.py::test_y") -> dict[str, Any]:
    source = (
        '@pytest.mark.part("' + milestone + '", "FR-BELIEF-1")\n'
        "def test_y() -> None:\n"
        '    """Docstring."""\n' + "".join(f"    {line}\n" for line in body.splitlines())
    )
    return {"node_id": node_id, "milestone": milestone, "source": source}


def _lines(parts: list[dict[str, Any]]) -> list[str]:
    return [f.line() for f in check(parts, LEXICON)]


def test_real_lexicon_has_rows_for_m0_through_m8_and_not_counted() -> None:
    assert {t.milestone for t in LEXICON.terms} == {0, 1, 3, 4, 5, 6, 7, 8}
    assert "scenario suite" in LEXICON.not_counted


@pytest.mark.parametrize("word", ["beliefs", "believing", "Believed", "belief"])
def test_m2_part_using_a_belief_form_fails_naming_m5(word: str) -> None:
    assert _lines([_part("M2", f"assert {word} is not None")]) == [
        f'tests/test_x.py::test_y uses "{word}" (belie*, M5) in an M2 part'
    ]


def test_capitalized_state_fails_but_lowercase_state_passes() -> None:
    assert _lines([_part("M2", "x = State()")]) == [
        'tests/test_x.py::test_y uses "State" (State, M6) in an M2 part'
    ]
    assert _lines([_part("M2", "state = 1  # plain state")]) == []


def test_not_counted_phrase_is_removed_before_matching() -> None:
    assert _lines([_part("M2", "run the scenario suite here")]) == []
    assert _lines([_part("M2", "create a scenario here")]) == [
        'tests/test_x.py::test_y uses "scenario" (scenario, M3) in an M2 part'
    ]


def test_prefix_term_matches_erasure() -> None:
    assert _lines([_part("M2", "perform erasure now")]) == [
        'tests/test_x.py::test_y uses "erasure" (eras*, M3) in an M2 part'
    ]


def test_substrings_inside_other_words_do_not_match() -> None:
    assert _lines([_part("M2", "clockwork = 1")]) == []
    assert _lines([_part("M2", "clock = 1")]) == [
        'tests/test_x.py::test_y uses "clock" (clock, M6) in an M2 part'
    ]


def test_same_or_later_part_may_use_the_term() -> None:
    assert _lines([_part("M5", "belief = 1")]) == []
    assert _lines([_part("M8", "belief = State(); image_generation = clock")]) == []


def test_requirement_ids_and_decorators_do_not_trip_the_matcher() -> None:
    # FR-BELIEF-1 sits in the decorator; "S-ERASURE-BOUNDARY" and "(M5)" are stripped from the body.
    assert _lines([_part("M2", 'ref = "S-ERASURE-BOUNDARY (M5)"')]) == []


def test_phrase_and_hyphen_variants() -> None:
    assert _lines([_part("M2", "apply the audience filter")]) == [
        'tests/test_x.py::test_y uses "audience filter" (audience filter, M5) in an M2 part'
    ]
    assert _lines([_part("M2", "x = right_to_be_forgotten()")]) == [
        'tests/test_x.py::test_y uses "right to be forgotten" (right-to-be-forgotten, M3) '
        "in an M2 part"
    ]


def test_multi_part_function_is_checked_at_its_latest_milestone() -> None:
    parts = [
        _part("M4", "belief = 1", node_id="tests/test_x.py::test_u_01"),
        _part("M5", "belief = 1", node_id="tests/test_x.py::test_u_01"),
    ]
    assert _lines(parts) == []


def test_one_finding_per_test_and_term() -> None:
    assert len(_lines([_part("M2", "beliefs = believing = 1")])) == 1


def test_inflections_and_tokenizer() -> None:
    assert {"rewrite", "rewrites", "rewriting", "rewritten"} <= inflections("rewrite")
    assert "parties" in inflections("party")
    assert tokenize("imageCheck story_image foo-bar 42") == [
        "image",
        "Check",
        "story",
        "image",
        "foo",
        "bar",
    ]


def test_table_validation() -> None:
    table = (
        "**Capability lexicon (CI).** text\n\n| Milestone | Capability terms |\n|---|---|\n"
        "| M3 | scenario |\n| M4 | scenario |\n"
    )
    with pytest.raises(LexiconError, match="two milestones"):
        parse_lexicon(table, ROWS)
    with pytest.raises(LexiconError, match="not a §22 milestone"):
        parse_lexicon(table.replace("| M4 | scenario |", "| M12 | other |"), ROWS)


def test_cli_fails_on_violation_and_passes_today(tmp_path: Path) -> None:
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps([_part("M2", "belief = 1")]))
    proc = subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-lexicon", "--parts", str(parts)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 1
    assert proc.stdout.splitlines()[0].endswith("in an M2 part")

    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.gates",
            "check-lexicon",
            "--parts",
            str(tmp_path / "nope.json"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert missing.returncode == 2
    assert "check-parts --json" in missing.stdout

    subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-parts", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    real = subprocess.run(
        [sys.executable, "-m", "tools.gates", "check-lexicon"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert real.returncode == 0, real.stdout
    assert real.stdout.strip().endswith("0 violations")
