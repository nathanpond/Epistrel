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
- ``docs/evals/sets-digest.md`` is not what ``digest`` writes;
- one of #938's per-type E-12 intervention sets (``interventions/<type>/cases.jsonl``) does not
  load as a direct case, is smaller than its ``sizing.json`` derives (50 for an identity type),
  fails the content lint (``authoring.lint``: no minor, real person or explicit content), or its
  ``SAMPLE.md`` (the owner's review sample, written by ``digest``) is stale.

A suite whose set is optional (``Suite.set_optional``: #270's E-11, whose images are generated
on the owner's machine and never committed) is skipped while its set file is absent, and held
to every rule above once it exists: its ``PROVENANCE.md`` still fails on ``main`` while it is
``pending``. E-11's rows are shown by ``case_id`` and label (a manifest, no images).

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
from epistrel.evals.authoring import interventions
from epistrel.evals.authoring import lint as content_lint
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
    {
        "asymmetry/cases.jsonl",
        "think_leak/cases.jsonl",
        "entailment_viewpoint/cases.jsonl",
        "time_estimation/cases.jsonl",
        *(interventions.set_file(kind) for kind in interventions.TYPES),
    }
)
"""#179's sets, whose digest sample is stratified by family (the fixture, the category) and
template, and #201's time-estimation set, stratified by activity family, so every stratum is in
front of the reviewer."""
HAND_WRITTEN = frozenset({"smoke/cases.jsonl", "e02/cases.jsonl", "interventions/examples.jsonl"})
"""Set files no provenance or digest rule covers: those that predate #157 (#134's smoke, #155's
E-02) and #936's CI-only E-12 direct examples (scripted stub answers, never a live set)."""


def labeled_suites(suites: Iterable[Suite] | None = None) -> list[Suite]:
    """The suites whose sets #157's rules cover."""
    chosen = SUITES.values() if suites is None else suites
    return [s for s in chosen if s.set_file not in HAND_WRITTEN]


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


def _optional(set_file: str) -> bool:
    return any(s.set_optional and s.set_file == set_file for s in labeled_suites())


def _row_id(row: dict[str, Any]) -> str:
    """A row's id: ``id``, or an E-11 manifest line's ``case_id``."""
    return str(row.get("id", row.get("case_id", "")))


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """A row as the digest shows it: no id, family or scripted stub answer; a scenario's turns
    by their line and expected interventions."""
    shown = {
        k: v
        for k, v in row.items()
        if k not in ("id", "case_id", "family", "stub_answer") and not k.startswith("stub")
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


def _row_label(row: dict[str, Any]) -> str:
    """A row's label in the digest's counts: #201's time-estimation cases by their kind (the
    first word of their notes: explicit, implicit, inferred), every other set by ``_label``."""
    if "acceptable_minutes" in row:
        return str(row.get("notes", "")).split(":", 1)[0] or "labeled"
    return _label(row.get("expected", row.get("label")))


def _stratum(row: dict[str, Any]) -> tuple[str, str]:
    inputs = row.get("inputs") if isinstance(row.get("inputs"), dict) else {}
    assert isinstance(inputs, dict)  # noqa: S101 - narrowed above
    # #938's intervention cases carry their trigger variant at the top level.
    return str(row.get("family", "")), str(inputs.get("template", row.get("template", "")))


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
    for kind in interventions.TYPES:
        shown_sets.append((interventions.set_file(kind), f"e12_direct live, {kind}"))
    for set_file, owner in shown_sets:
        sharing = [s.name for s in labeled_suites() if s.set_file == set_file]
        owner = ", ".join(sharing) or owner
        path = sets_dir / set_file
        if not path.is_file() and (_optional(set_file) or set_file.startswith(_LIVE_PREFIX)):
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        families = Counter(str(r.get("family", "")) for r in rows)
        labels = Counter(_row_label(r) for r in rows)
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
        for row in sorted(picked, key=_row_id):
            shown = _shown(row)
            lines.append(f"- `{_row_id(row)}`: `{json.dumps(shown, ensure_ascii=False)}`")
        lines.append("")
    return "\n".join(lines)


_LIVE_PREFIX = f"{interventions.SET}/"
SAMPLE = "SAMPLE.md"
IDENTITY_MIN = 50
"""The fewest cases an identity type's set holds (owner R3, #938)."""
EXPECTED = {
    "repair_rung_1": "narrowing: the sentence putting the coat on the armchair is dropped",
    "repair_rung_2": "substitution: the armchair is replaced by canon's place, the hook",
    "repair_rung_3": "regeneration: the run's model rewrites the beat without the armchair claim",
    "repair_rung_4": (
        "safe fallback (one attempt for two contradicting beats): a beat becomes Bobby's "
        "fallback line"
    ),
    "composition_polished": "the composition polishes the beats into one message",
    "composition_fallback": (
        "the prepared polish adds the armchair claim, the check refuses it, and the draft is "
        "released unchanged"
    ),
    "age_repair": "the age repair gives the newcomer a concrete adult age; the prose is unchanged",
}
"""What each type's case expects, as the review sample states it."""


def _quoted(text: str) -> str:
    return '"' + text.replace("\n", " ") + '"'


def sample_text(sets_dir: Path, kind: str) -> str:
    """``interventions/<kind>/SAMPLE.md``: the owner's review sample (#938): ``DIGEST_SAMPLES``
    cases drawn with ``DIGEST_SEED``, stratified by trigger variant, each shown as its draft,
    its trigger and the intervention it expects."""
    rows = [
        json.loads(line)
        for line in (sets_dir / interventions.set_file(kind)).read_text("utf-8").splitlines()
        if line
    ]
    rnd = random.Random(f"{DIGEST_SEED}:{interventions.SET}:{kind}")  # noqa: S311 - a sample
    picked = sorted(stratified(rows, rnd), key=_row_id)
    variants = Counter(str(r.get("template", "")) for r in rows)
    lines = [
        f"# `{interventions.SET}/{kind}`: review sample",
        "",
        "Generated by `uv run python -m epistrel.evals digest` (#938); `--check-sets` fails "
        f"when it is stale. The set holds {len(rows)} cases; these {len(picked)} were drawn "
        f"with seed {DIGEST_SEED}, stratified by trigger variant ("
        + ", ".join(f"{k} {v}" for k, v in sorted(variants.items()))
        + "). `PROVENANCE.md` beside this file has the recipe, the pilot's rates and the size.",
        "",
        f"**Expected intervention:** `{kind}`: {EXPECTED[kind]}.",
        "",
        "Canon in every case (S-COAT): the coat is on the hook, the hook is in the hallway, the "
        "armchair is in the parlor, and Wren (the player) and Bobby are in the parlor.",
        "",
    ]
    for row in picked:
        lines += [f"## `{_row_id(row)}` ({row.get('template', '')})", ""]
        lines.append(f"- **Wren says:** {_quoted(str(row['say']))}")
        lines.append("- **Draft:**")
        for beat in row["beats"]:
            claims = "".join(
                f" (declares {c['subject']} {c['relation']} {c['object']})"
                for c in beat.get("propositions", [])
            )
            lines.append(f"  - {beat['kind']}: {_quoted(beat['text'])}{claims}")
        for intro in row.get("introduces", []):
            span = intro.get("age_range")
            age = f"age {span[0]} to {span[1]}" if span else f"age {intro.get('age')}"
            lines.append(f"- **Introduces:** {intro['name']} ({age}; {intro.get('clue', '')})")
        if row.get("polish"):
            lines.append(f"- **Prepared polish:** {_quoted(str(row['polish']))}")
        if row.get("dials"):
            lines.append(f"- **Dials:** `{json.dumps(row['dials'])}`")
        lines.append("")
    return "\n".join(lines)


def write_digest(sets_dir: Path, path: Path = DIGEST) -> Path:
    """The digest, and each intervention type's review sample (#938)."""
    path.write_text(digest_text(sets_dir), encoding="utf-8")
    for kind in interventions.TYPES:
        if (sets_dir / interventions.set_file(kind)).is_file():
            sample = sets_dir / interventions.SET / kind / SAMPLE
            sample.write_text(sample_text(sets_dir, kind), encoding="utf-8")
    return path


def intervention_problems(
    sets_dir: Path, *, require_approved: bool = False
) -> tuple[list[str], set[Path]]:
    """#938's per-type E-12 sets: each loads as a ``DirectCase``, is at least its size (a pair
    type's ``sizing.json``, which it must have; ``IDENTITY_MIN`` for an identity type), passes
    the content lint and has a current ``SAMPLE.md``. A ``sizing.json`` that records the type as
    ``blocked`` (the pilot gave no size the rule allows; the owner decides) passes like a
    ``pending`` approval: on a branch, not where approval is required. Also the directories
    whose ``PROVENANCE.md`` the caller checks."""
    suite = SUITES["e12_direct"]
    found: list[str] = []
    directories: set[Path] = set()
    for kind in interventions.TYPES:
        relative = interventions.set_file(kind)
        directory = sets_dir / interventions.SET / kind
        try:
            cases = suite.load_file(sets_dir, relative)
        except MalformedCase as exc:
            found.append(str(exc))
            continue
        directories.add(directory)
        if any(getattr(c, "type", None) != kind for c in cases):
            found.append(f"{relative}: holds a case of another type")
        if kind in interventions.PAIR_TYPES:
            sized = interventions.sizing(sets_dir, kind)
            least = None if sized is None or sized.get("size") is None else int(sized["size"])
            if sized is None:
                found.append(f"{relative}: no {interventions.SIZING} (run the pilot, #938)")
            elif least is None and (require_approved or not sized.get("blocked")):
                reason = sized.get("blocked") or "no size"
                found.append(f"{relative}: not sized ({reason})")
        else:
            least = IDENTITY_MIN
        if least is not None and len(cases) < least:
            found.append(f"{relative}: {len(cases)} cases, fewer than its {least}")
        rows = [
            json.loads(line)
            for line in (sets_dir / relative).read_text("utf-8").splitlines()
            if line
        ]
        for row in rows:
            found += content_lint.problems(row)
        sample = directory / SAMPLE
        if not sample.is_file() or sample.read_text("utf-8") != sample_text(sets_dir, kind):
            found.append(
                f"{relative}: {SAMPLE} is stale: run `uv run python -m epistrel.evals digest`"
            )
    return found, directories


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
        if suite.set_optional and not (sets_dir / suite.set_file).is_file():
            continue
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
    live, live_directories = intervention_problems(sets_dir, require_approved=require_approved)
    found += live
    directories |= live_directories
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
    "IDENTITY_MIN",
    "PENDING",
    "PROVENANCE",
    "REQUIRE_APPROVED",
    "SAMPLE",
    "digest_text",
    "intervention_problems",
    "labeled_suites",
    "problems",
    "provenance_problem",
    "sample_text",
    "write_digest",
]
