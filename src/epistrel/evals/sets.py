"""The labeled sets' checks and digest (#157): ``python -m epistrel.evals --check-sets`` (CI's
lint job) and ``python -m epistrel.evals digest``.

``--check-sets`` fails when

- a suite's set does not load (a malformed case: its suite's ``case_model`` is the schema), or
  holds fewer cases than the suite's ``min_cases``;
- a generated set differs from what its authoring module writes (``authoring.stale``);
- a set's directory has no ``PROVENANCE.md``, or it lacks the review line: ``approved by owner
  YYYY-MM-DD``, or ``approved by owner: pending`` while the owner has not reviewed it yet.
  ``pending`` passes on a branch and fails where approval is required
  (``EPISTREL_REQUIRE_APPROVED_SETS=1``: CI on ``main``), so no unapproved set reaches a release;
- ``docs/evals/sets-digest.md`` is not what ``digest`` writes.

The digest is what the owner reviews: per set its size, its counts by family and by label, and
20 cases drawn by a fixed seed (a scenario's turns shown by their lines and expected
interventions; scripted stub answers left out).

#158 adds the judge calibration set (``judge_calibration/cases.jsonl``: no suite loads it, the
judge calibration does), under the same rules: it loads (``judging.load_pairs``), holds at least
``judging.CALIBRATION_MIN_PAIRS`` pairs, has its ``PROVENANCE.md`` and is in the digest. E-12 and
E-13 share one set (``scenarios/cases.jsonl``), shown once.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import epistrel.evals.judging as judges
from epistrel.evals import authoring
from epistrel.evals.suites import SUITES, MalformedCase, Suite
from epistrel.tools.generated import REPO_ROOT

PROVENANCE = "PROVENANCE.md"
APPROVED = re.compile(r"^approved by owner (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
PENDING = re.compile(r"^approved by owner: pending\s*$", re.MULTILINE)
REQUIRE_APPROVED = "EPISTREL_REQUIRE_APPROVED_SETS"
DIGEST = REPO_ROOT / "docs" / "evals" / "sets-digest.md"
DIGEST_SAMPLES = 20
DIGEST_SEED = 157
STRATIFIED = frozenset(
    {"asymmetry/cases.jsonl", "think_leak/cases.jsonl", "entailment_viewpoint/cases.jsonl"}
)
"""#179's sets, whose digest sample is stratified by family (the fixture, the category) and
template, so every stratum is in front of the reviewer."""
HAND_WRITTEN = frozenset({"smoke", "e02"})
"""Suites whose sets predate #157 (#134's smoke, #155's E-02): no provenance or digest rule."""


def labeled_suites(suites: Iterable[Suite] | None = None) -> list[Suite]:
    """The suites whose sets #157's rules cover."""
    chosen = SUITES.values() if suites is None else suites
    return [s for s in chosen if s.name not in HAND_WRITTEN]


def provenance_problem(directory: Path, *, require_approved: bool) -> str | None:
    path = directory / PROVENANCE
    if not path.is_file():
        return f"{directory.name}: no {PROVENANCE}"
    text = path.read_text(encoding="utf-8")
    if APPROVED.search(text):
        return None
    if PENDING.search(text):
        if require_approved:
            return f"{directory.name}: the owner has not approved this set yet ({PROVENANCE})"
        return None
    return f"{directory.name}: {PROVENANCE} has no 'approved by owner YYYY-MM-DD' line"


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """A row as the digest shows it: no id, family or scripted stub answer; a scenario's turns
    by their line and expected interventions."""
    shown = {
        k: v
        for k, v in row.items()
        if k not in ("id", "family", "stub_answer") and not k.startswith("stub")
    }
    if isinstance(shown.get("turns"), list):
        shown["turns"] = [
            {"say": t.get("say"), "expected": t.get("expected")}
            for t in shown["turns"]
            if isinstance(t, dict)
        ]
    return shown


def _label(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "category" in value:
        return str(value["category"])
    if isinstance(value, list):
        return "salient" if value else "texture"
    return "labeled"


def _stratum(row: dict[str, Any]) -> tuple[str, str]:
    inputs = row.get("inputs") if isinstance(row.get("inputs"), dict) else {}
    assert isinstance(inputs, dict)  # noqa: S101 - narrowed above
    return str(row.get("family", "")), str(inputs.get("template", ""))


def stratified(rows: list[dict[str, Any]], rnd: random.Random) -> list[dict[str, Any]]:
    """``DIGEST_SAMPLES`` rows drawn round-robin over the strata (family, template), each
    stratum shuffled by ``rnd``; the strata in sorted order."""
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        strata.setdefault(_stratum(row), []).append(row)
    queues = []
    for key in sorted(strata):
        members = list(strata[key])
        rnd.shuffle(members)
        queues.append(members)
    picked: list[dict[str, Any]] = []
    while len(picked) < min(DIGEST_SAMPLES, len(rows)):
        for queue in queues:
            if queue and len(picked) < DIGEST_SAMPLES:
                picked.append(queue.pop())
    return picked


def digest_text(sets_dir: Path) -> str:
    lines = [
        "# Labeled eval sets: the review digest",
        "",
        "Generated by `uv run python -m epistrel.evals digest` from `evals/sets/` (#157); "
        "`--check-sets` fails when it is stale. For each set: its size, its cases per family "
        f"and per label, and {DIGEST_SAMPLES} cases drawn with a fixed seed. Each set's "
        "`PROVENANCE.md` records how it was authored and the owner's approval.",
        "",
    ]
    shown_sets: list[tuple[str, str]] = []
    for suite in labeled_suites():
        names = [n for f, n in shown_sets if f == suite.set_file]
        if names:
            continue
        shown_sets.append((suite.set_file, suite.name))
    for set_file in (judges.CALIBRATION_SET,):
        shown_sets.append((set_file, "judge calibration"))
    for set_file, owner in shown_sets:
        sharing = [s.name for s in labeled_suites() if s.set_file == set_file]
        owner = ", ".join(sharing) or owner
        path = sets_dir / set_file
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        families = Counter(str(r.get("family", "")) for r in rows)
        labels = Counter(_label(r.get("expected", r.get("label"))) for r in rows)
        lines += [
            f"## `{set_file}` ({owner}, {len(rows)} cases)",
            "",
            "Families: " + ", ".join(f"{k} {v}" for k, v in sorted(families.items())) + ".",
            "",
            "Labels: " + ", ".join(f"{k} {v}" for k, v in sorted(labels.items())) + ".",
            "",
        ]
        seed_key = sharing[0] if sharing else owner
        rnd = random.Random(f"{DIGEST_SEED}:{seed_key}")  # noqa: S311 - a digest
        if set_file in STRATIFIED:
            picked = stratified(rows, rnd)
        else:
            picked = rnd.sample(rows, min(DIGEST_SAMPLES, len(rows)))
        for row in sorted(picked, key=lambda r: str(r["id"])):
            shown = _shown(row)
            lines.append(f"- `{row['id']}`: `{json.dumps(shown, ensure_ascii=False)}`")
        lines.append("")
    return "\n".join(lines)


def write_digest(sets_dir: Path, path: Path = DIGEST) -> Path:
    path.write_text(digest_text(sets_dir), encoding="utf-8")
    return path


def problems(
    sets_dir: Path,
    *,
    suites: Iterable[Suite] | None = None,
    require_approved: bool | None = None,
    digest: Path = DIGEST,
) -> list[str]:
    """Why the labeled sets fail #157's rules (empty when they pass; module docstring)."""
    if require_approved is None:
        require_approved = os.environ.get(REQUIRE_APPROVED) == "1"
    found: list[str] = []
    directories: set[Path] = set()
    for suite in labeled_suites(suites):
        try:
            cases = suite.load(sets_dir)
        except MalformedCase as exc:
            found.append(str(exc))
            continue
        if len(cases) < suite.min_cases:
            found.append(f"{suite.name}: {len(cases)} cases, fewer than its {suite.min_cases}")
        directories.add((sets_dir / suite.set_file).parent)
    try:
        pairs = judges.load_pairs(sets_dir)
    except MalformedCase as exc:
        found.append(str(exc))
    else:
        if len(pairs) < judges.CALIBRATION_MIN_PAIRS:
            found.append(
                f"judge calibration: {len(pairs)} pairs, fewer than {judges.CALIBRATION_MIN_PAIRS}"
            )
        directories.add((sets_dir / judges.CALIBRATION_SET).parent)
    for directory in sorted(directories):
        problem = provenance_problem(directory, require_approved=require_approved)
        if problem is not None:
            found.append(problem)
    found += authoring.stale(sets_dir)
    if not digest.is_file() or digest.read_text(encoding="utf-8") != digest_text(sets_dir):
        found.append(
            "docs/evals/sets-digest.md is stale: run `uv run python -m epistrel.evals digest`"
        )
    return found


__all__ = [
    "APPROVED",
    "DIGEST",
    "PENDING",
    "PROVENANCE",
    "REQUIRE_APPROVED",
    "digest_text",
    "labeled_suites",
    "problems",
    "provenance_problem",
    "write_digest",
]
