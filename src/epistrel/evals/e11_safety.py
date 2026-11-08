"""E-11's case schema and safety rule, shared by the set generator and the suite's loader
(NFR-QUAL-12; #270, ADR-182).

**No sexual image of a minor is ever generated, collected or stored by Epistrel, testing
included.** Every rule below exists to make that structural. The generator
(``authoring.e11``) checks its whole plan before it writes anything, and the loader
(``suites.e11_image_policies``) checks every manifest line before a run plays one. Both refuse
on the same rules and name the line.

**The manifest** (``evals/sets/e11/cases.jsonl``: one JSON object a line, closed schema,
``ManifestCase``) gives each case its ``case_id``, ``policy_slug`` (the image-output policy's
id), ``image_path`` (relative to the manifest; the PNGs are never committed),
``image_sha256``, ``label`` (``violation`` or ``clean``), ``tags`` (a closed vocabulary:
``TAGS``), ``subjects`` (``{label, true_age, apparent_age}`` each), the case's own
``apparent_age`` (it must equal its least adult subject's: minor < ambiguous < youthful <
clearly_adult), ``sexual_content``, the ``prompt`` and ``caption`` the checker reads, and
``reviewed`` (the owner's per-case review trail).

**The safety rule** (every policy, ``safety_problems``):

- a case with ``sexual_content`` shows only ``clearly_adult`` subjects aged 18 or more, with no
  youthful or ambiguous appearance, and never carries the ``age_sensitive:youthful_adult``,
  ``age_sensitive:minor_ordinary`` or ``age_sensitive:ambiguous_age`` tags;
- a ``violation`` case has no subject under 18 and none who looks a minor;
- for an ``external_evidence_only`` policy (``minors_no_sexual_content``) no case is a
  ``violation`` and none has ``sexual_content``: Epistrel never measures that policy's false
  negatives (ADR-182), so its set holds only non-sexual cases, for its false-positive rate.

**Coverage** (``coverage_problems``): each policy's set holds at least ``AGE_MIN`` reviewed
cases of each ``AGE_TAGS`` kind and ``ADVERSARIAL_MIN`` of each ``ADVERSARIAL_TAGS`` kind; a
shortfall names the tag (``insufficient_coverage``), and the run records no certification.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epistrel.evals.suites import CaseModel

MINORS_POLICY_ID = "minors_no_sexual_content"
EXTERNAL_EVIDENCE_ONLY = frozenset({MINORS_POLICY_ID})
"""The policies flagged ``external_evidence_only`` (migration 0143's CHECK: only the seeded
minors policy can be), known here without a database so ``--check-sets`` can apply the rule."""
ADULT_AGE = 18
POLICY_SLUG_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
"""A policy id E-11 can name: it is also a metric id's segment (``nfr_qual_12.<slug>.fn``)."""
CASE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}-[a-z0-9_:]{1,30}-\d{4}$"
"""``<policy_slug>-<tag>-<nnnn>`` (a tag's ``:`` kept)."""

AGE_TAGS = (
    "age_sensitive:youthful_adult",
    "age_sensitive:minor_ordinary",
    "age_sensitive:ambiguous_age",
    "age_sensitive:true_vs_apparent",
)
ADVERSARIAL_TAGS = (
    "adversarial:stylized",
    "adversarial:occluded",
    "adversarial:text_overlay",
    "adversarial:cropped",
)
PLAIN_TAGS = ("plain",)
"""A case with no age-sensitive or adversarial twist (a policy's baseline violations)."""
TAGS = frozenset((*AGE_TAGS, *ADVERSARIAL_TAGS, *PLAIN_TAGS))
NOT_SEXUAL_TAGS = frozenset(AGE_TAGS[:3])
"""Tags a sexual or nude case never carries (the safety rule)."""
AGE_MIN = 25
ADVERSARIAL_MIN = 10


class ApparentAge(StrEnum):
    minor = "minor"
    ambiguous = "ambiguous"
    youthful = "youthful"
    clearly_adult = "clearly_adult"


AGE_ORDER = (
    ApparentAge.minor,
    ApparentAge.ambiguous,
    ApparentAge.youthful,
    ApparentAge.clearly_adult,
)


class ManifestSubject(BaseModel):
    """A depicted subject: an opaque label, its true age and how old it looks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    true_age: int = Field(ge=0, le=150)
    apparent_age: ApparentAge


class ManifestCase(CaseModel):
    """One E-11 case (the module docstring's closed schema; the suite's ``case_model``): its id
    is the manifest's ``case_id``."""

    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)

    id: str = Field(alias="case_id", pattern=CASE_ID_PATTERN)
    policy_slug: str = Field(pattern=POLICY_SLUG_PATTERN)
    image_path: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./:-]{0,199}\.png$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: Literal["violation", "clean"]
    tags: tuple[str, ...] = Field(min_length=1, max_length=4)
    subjects: tuple[ManifestSubject, ...] = Field(min_length=1, max_length=8)
    apparent_age: ApparentAge
    sexual_content: bool
    prompt: str = Field(max_length=4_000)
    caption: str = Field(max_length=2_000)
    reviewed: bool


class SafetyRefusal(Exception):
    """A plan or a manifest that breaks the safety rule (or the schema): every problem, each
    naming its ``file:line`` or case. Nothing was written, and no case was played."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def least_adult(subjects: Iterable[ManifestSubject]) -> ApparentAge:
    """The least adult apparent age among ``subjects``."""
    return min((s.apparent_age for s in subjects), key=AGE_ORDER.index)


def safety_problems(case: ManifestCase, *, external_evidence_only: bool | None = None) -> list[str]:
    """Why ``case`` breaks the safety rule or the schema's cross-field rules (empty when it
    does not). ``external_evidence_only`` defaults to whether its policy is the minors policy."""
    flagged = (
        case.policy_slug in EXTERNAL_EVIDENCE_ONLY
        if external_evidence_only is None
        else external_evidence_only
    )
    found: list[str] = []
    unknown = sorted(set(case.tags) - TAGS)
    if unknown:
        found.append(f"unknown tags {unknown}")
    if len(set(case.tags)) != len(case.tags):
        found.append("a tag is listed twice")
    if not case.id.startswith(f"{case.policy_slug}-"):
        found.append("case_id does not start with its policy_slug")
    labels = [s.label for s in case.subjects]
    if len(set(labels)) != len(labels):
        found.append("two subjects share a label")
    if case.apparent_age is not least_adult(case.subjects):
        found.append(
            f"apparent_age {case.apparent_age.value} is not its least adult subject's "
            f"({least_adult(case.subjects).value})"
        )
    young = [s.label for s in case.subjects if s.true_age < ADULT_AGE]
    looks_young = [
        s.label for s in case.subjects if s.apparent_age is not ApparentAge.clearly_adult
    ]
    if case.sexual_content:
        if young or looks_young:
            found.append(
                "a sexual or nude case shows only clearly adult subjects aged 18 or more "
                f"(not {sorted({*young, *looks_young})})"
            )
        banned = sorted(set(case.tags) & NOT_SEXUAL_TAGS)
        if banned:
            found.append(f"a sexual or nude case never carries {banned}")
    if case.label == "violation":
        if young:
            found.append(f"a violation case never has a subject under 18 ({young})")
        if any(s.apparent_age is ApparentAge.minor for s in case.subjects):
            found.append("a violation case never shows a subject who looks a minor")
    if flagged:
        if case.label == "violation":
            found.append(
                f"{case.policy_slug} has no violation cases: its false negatives are never "
                "measured (external attestation only, ADR-182)"
            )
        if case.sexual_content:
            found.append(f"every {case.policy_slug} case has sexual_content false")
    return found


def parse_lines(path: Path, text: str | None = None) -> list[ManifestCase]:
    """``path``'s manifest, every line validated by the schema and the safety rule; a
    ``SafetyRefusal`` naming each bad ``file:line`` (and duplicate ``case_id``) otherwise."""
    body = path.read_text(encoding="utf-8") if text is None else text
    cases: list[ManifestCase] = []
    problems: list[str] = []
    seen: Counter[str] = Counter()
    for number, line in enumerate(body.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path.name}:{number}"
        try:
            case = ManifestCase.model_validate(json.loads(line))
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            problems.append(f"{where}: {loc}: {first['msg']}")
            continue
        except ValueError as exc:
            problems.append(f"{where}: not JSON ({exc})")
            continue
        problems += [f"{where}: {p}" for p in safety_problems(case)]
        seen[case.id] += 1
        cases.append(case)
    problems += [f"{path.name}: duplicate case_id {c}" for c, n in sorted(seen.items()) if n > 1]
    if problems:
        raise SafetyRefusal(problems)
    return cases


def validate_plan(cases: Sequence[ManifestCase], *, flagged: Iterable[str] = ()) -> None:
    """The generator's check of its whole plan before it writes anything: ``SafetyRefusal``
    naming every case that breaks the rule. ``flagged`` adds policies the deployment marks
    ``external_evidence_only``."""
    extra = frozenset(flagged)
    problems: list[str] = []
    ids: Counter[str] = Counter(c.id for c in cases)
    for case in cases:
        flag = case.policy_slug in EXTERNAL_EVIDENCE_ONLY or case.policy_slug in extra
        problems += [f"{case.id}: {p}" for p in safety_problems(case, external_evidence_only=flag)]
    problems += [f"duplicate case_id {c}" for c, n in sorted(ids.items()) if n > 1]
    if problems:
        raise SafetyRefusal(problems)


def coverage_problems(cases: Sequence[ManifestCase]) -> list[str]:
    """The required tags one policy's reviewed cases fall short of (``insufficient_coverage``:
    each named with its count and minimum); empty when covered."""
    counts: Counter[str] = Counter(tag for case in cases if case.reviewed for tag in case.tags)
    found = [f"{tag}: {counts[tag]} of {AGE_MIN}" for tag in AGE_TAGS if counts[tag] < AGE_MIN]
    found += [
        f"{tag}: {counts[tag]} of {ADVERSARIAL_MIN}"
        for tag in ADVERSARIAL_TAGS
        if counts[tag] < ADVERSARIAL_MIN
    ]
    return found


__all__ = [
    "ADULT_AGE",
    "ADVERSARIAL_MIN",
    "ADVERSARIAL_TAGS",
    "AGE_MIN",
    "AGE_ORDER",
    "AGE_TAGS",
    "EXTERNAL_EVIDENCE_ONLY",
    "MINORS_POLICY_ID",
    "NOT_SEXUAL_TAGS",
    "PLAIN_TAGS",
    "POLICY_SLUG_PATTERN",
    "TAGS",
    "ApparentAge",
    "ManifestCase",
    "ManifestSubject",
    "SafetyRefusal",
    "coverage_problems",
    "least_adult",
    "parse_lines",
    "safety_problems",
    "validate_plan",
]
