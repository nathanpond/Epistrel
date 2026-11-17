"""E-12 direct mode's per-type intervention sets (#938): generated, sized, linted, sampled.

The committed sets (``evals/sets/interventions/<type>/cases.jsonl``) validate as direct cases,
are at least the size their ``sizing.json`` derives (50 for an identity type), are what
``authoring.interventions`` generates (prefix-stable), pass the content lint and have a current
``SAMPLE.md``; a stub run plays the CI examples and a real-model run the typed sets.

No test here carries a part marker: the sets are test data; E-12's parts gate the suite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from epistrel.evals import sets as labeled
from epistrel.evals.authoring import interventions as iv
from epistrel.evals.authoring import lint
from epistrel.evals.suites import SUITES, MalformedCase
from epistrel.evals.suites.e12_direct import DirectCase
from tests.eval.suite_support import SETS


def _rows(kind: str) -> list[dict[str, object]]:
    path = SETS / iv.set_file(kind)
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


@pytest.mark.parametrize("kind", iv.TYPES)
def test_each_set_validates_is_sized_and_is_generated(kind: str) -> None:
    rows = _rows(kind)
    for row in rows:
        case = DirectCase.model_validate(row)
        assert case.type == kind
        assert case.id.startswith(f"interventions-{kind}-")
    if kind in iv.PAIR_TYPES:
        sized = iv.sizing(SETS, kind)
        assert sized is not None, "a pair type's set has the pilot's sizing.json"
        assert sized["size"] == iv.derived_size(
            sized["tie_rate"], sized["not_triggered_rate"], sized["mismatch_rate"]
        )
        if sized["size"] is None:  # blocked on the owner (#938): the pilot set stands
            assert sized["blocked"]
            assert len(rows) == iv.PILOT_SIZE == iv.size_of(SETS, kind)
        else:
            assert len(rows) >= int(sized["size"]) >= iv.FLOOR
    else:
        assert len(rows) >= labeled.IDENTITY_MIN
    assert rows == iv.cases(kind, len(rows))


def test_generation_is_prefix_stable_and_distinct() -> None:
    for kind in iv.TYPES:
        long = iv.cases(kind, 400)
        assert iv.cases(kind, iv.PILOT_SIZE) == long[: iv.PILOT_SIZE]
        bodies = {
            json.dumps({k: v for k, v in c.items() if k not in ("id", "origin")}) for c in long
        }
        assert len(bodies) == len(long)


def test_the_sizing_rule() -> None:
    """#938: ceil(300 / (1 - tie - not_triggered - mismatch) * 1.15), at least 360, at most
    1,500; none for a non-positive denominator."""
    assert iv.derived_size(0.0, 0.0, 0.0) == iv.FLOOR
    assert iv.derived_size(0.2, 0.1, 0.1) == 575
    assert iv.derived_size(0.5, 0.3, 0.2) is None
    assert iv.derived_size(0.5, 0.2, 0.1) is None  # 1,725 > the ceiling


def test_rung_4_cases_leave_one_attempt_for_two_beats() -> None:
    for row in _rows("repair_rung_4"):
        assert row["dials"] == {"repair": {"max_attempts": 1}}
        assert len(row["beats"]) == 2  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("change", "found"),
    [
        ({"introduces": [{"name": "Ansel Marrow", "age_range": [12, 40]}]}, "under 18"),
        ({"introduces": [{"name": "Ansel Marrow", "age": 16}]}, "under 18"),
        ({"introduces": [{"name": "Real Person", "age_range": [30, 50]}]}, "fictional name pool"),
        ({"say": "Is she a 14-year-old?"}, "an age in prose"),
        ({"say": "Is the kid asleep?"}, "a word naming a child"),
        ({"say": "That was erotic."}, "explicit word"),
    ],
)
def test_the_lint_refuses_minors_real_people_and_explicit_content(
    change: dict[str, object], found: str
) -> None:
    row = {**iv.one("age_repair", 1), **change}
    assert any(found in problem for problem in lint.problems(row))
    assert lint.problems(iv.one("age_repair", 1)) == []


def test_check_sets_catches_a_stale_sample_and_an_undersized_set(tmp_path: Path) -> None:
    copy = tmp_path / "sets"
    shutil.copytree(SETS, copy)
    assert labeled.intervention_problems(copy)[0] == []
    sample = copy / iv.SET / "age_repair" / labeled.SAMPLE
    sample.write_text(sample.read_text("utf-8") + "\nedited\n", encoding="utf-8")
    path = copy / iv.set_file("repair_rung_1")
    lines = path.read_text("utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:10]), encoding="utf-8")
    found, _ = labeled.intervention_problems(copy)
    assert any("age_repair" in p and "SAMPLE.md is stale" in p for p in found)
    assert any("repair_rung_1" in p and "fewer than" in p for p in found)


def test_a_stub_run_plays_the_examples_and_a_live_run_the_typed_sets(tmp_path: Path) -> None:
    suite = SUITES["e12_direct"]
    stub = suite.load_for(SETS, live=False)
    live = suite.load_for(SETS, live=True)
    assert {c.id.split("-")[0] for c in stub} == {"direct"}
    assert {c.id.split("-")[0] for c in live} == {"interventions"}
    assert {c.type for c in live} == set(iv.TYPES)  # type: ignore[attr-defined]
    copy = tmp_path / "sets"
    shutil.copytree(SETS, copy)
    (copy / iv.set_file("age_repair")).unlink()
    with pytest.raises(MalformedCase, match="no labeled set"):
        suite.load_for(copy, live=True)
    assert len(suite.load_for(copy, live=False)) == len(stub)
