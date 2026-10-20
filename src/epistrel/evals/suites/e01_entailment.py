"""E-01: entailment accuracy, M4's part (docs/04 §8; FR-VERIFY-1,3, NFR-QUAL-2; #157).

Each case is one claim checked against objective canon by #142's ``Verifier`` — deterministic
first (#140's rules over the records passed in, ``RecordsSource``), then the gate, then the
``entailment`` purpose on the escalated residual — exactly as a structured turn checks a
claim, at ``verification.level = entailment`` with the seeded profile's dials (gate 1.0,
minimum confidence 0.6, safe disposition ``repair``). The labeled set
(``evals/sets/entailment/cases.jsonl``) writes claims and canon in a small vocabulary of names
(``Bobby``, ``Ohio``, ``key``…) that the suite maps to fixed entity ids; times are years.

The expected label is the final kind a correct check ends with: ``supported``,
``contradicted``, ``novel``, or ``uncertain`` where nothing in canon or world knowledge decides
the claim (a model ``novel`` or a low-confidence answer takes the safe disposition, which
scores as ``uncertain``). About 30 % of the cases are the **fuzzy residual** (families
``residual-*``): claims the deterministic rules leave ``uncertain`` (world knowledge,
approximate bounds, weak semantics), which only the model can settle.

- ``nfr_qual_2.false_contradiction`` (rate, ≤ 2 %): over the cases labeled anything but
  ``contradicted``, the share the check ended ``contradicted``.
- ``nfr_qual_2.false_support`` (rate, ≤ 1 %): over the cases labeled anything but
  ``supported``, the share it ended ``supported``.
- ``e01.verdict_accuracy`` (report-only): the final kind equals the label.
- ``e01.residual.*`` (report-only): the same three over the residual alone, so the model's
  share is visible apart from the deterministic rules'.

Its stub answers are scripted per case (the label's verdict at confidence 0.9; ``novel`` for a
case labeled ``uncertain``), so CI gates on its stub run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.propositions.model import (
    Bound,
    Granularity,
    Instant,
    Interval,
    Layer,
    LiteralKind,
    Polarity,
    Predicate,
    Proposition,
    TypedLiteral,
    story_minute,
)
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import CORE_BY_ID, CORE_BY_NAME
from epistrel.core.verify.rules import ClosedSetRecord, Options, Record
from epistrel.evals.gate import Direction, Metric, Rule
from epistrel.evals.suites import CaseModel, Outcome, Suite, register
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import ENTAILMENT
from epistrel.orchestrator.verify.verifier import RecordsSource, Verifier, VerifyConfig

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

FALSE_CONTRADICTION = "nfr_qual_2.false_contradiction"
FALSE_SUPPORT = "nfr_qual_2.false_support"
ACCURACY = "e01.verdict_accuracy"
RESIDUAL_ACCURACY = "e01.residual.verdict_accuracy"
RESIDUAL_FALSE_CONTRADICTION = "e01.residual.false_contradiction"
RESIDUAL_FALSE_SUPPORT = "e01.residual.false_support"
RESIDUAL = "residual-"
"""The family prefix of the fuzzy-residual cases."""
CALL_DEADLINE_S = 300.0
STUB_CONFIDENCE = 0.9

Label = Literal["supported", "contradicted", "novel", "uncertain"]

PEOPLE = (
    "Bobby", "Ann", "Cal", "Tom", "Maren", "Wren", "Raj", "Jenni", "Linda", "Marcus", "Elias",
    "Priya", "Owen", "Ruth", "Hugo", "Smith", "Smithson",
)  # fmt: skip
PLACES = (
    "Ohio", "Columbus", "Cleveland", "Massachusetts", "Boston", "Cambridge", "Texas", "Austin",
    "Dallas", "France", "Paris", "Lyon", "Oregon", "Portland", "Kenya", "Nairobi", "kitchen",
    "garden", "cellar", "tavern", "market",
)  # fmt: skip
THINGS = (
    "key", "ring", "coin", "lantern", "deed", "map", "ledger", "the Guild", "Ohio State",
    "the choir", "red", "blue", "green", "yellow",
)  # fmt: skip
NAMES: tuple[str, ...] = PEOPLE + PLACES + THINGS
"""Every name a case may use; each is one entity of the suite's fixed table."""


def entity_id(name: str) -> str:
    """The fixed entity id of ``name`` (``01JE01`` + 20 digits)."""
    return f"01JE01{NAMES.index(name):020d}"


ENTITY_NAMES: Mapping[str, str] = {entity_id(n): n for n in NAMES}

AUTO_PREDICATES = ("favourite_colour", "apprenticed_with")
"""Story-registered (auto) predicates: weak by construction (multi, nothing declared)."""


def _auto_id(name: str) -> str:
    return f"01JE01P{AUTO_PREDICATES.index(name):019d}"


STORY = "01JE0100000000000000000STY"
REGISTRY = Registry(
    STORY,
    1,
    {
        **CORE_BY_ID,
        **{
            _auto_id(n): Predicate(id=_auto_id(n), story_id=STORY, name=n, layer=Layer.AUTO)
            for n in AUTO_PREDICATES
        },
    },
    (),
)


def predicate_id(name: str) -> str:
    """A core or auto predicate's id by name (``KeyError`` for an unknown one)."""
    if name in AUTO_PREDICATES:
        return _auto_id(name)
    return CORE_BY_NAME[name].id


# -- the case vocabulary ----------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Lit(_Model):
    kind: Literal["int", "str"]
    value: int | str


class When(_Model):
    """A valid-time in years: an interval (``from``/``to``, open when absent; an approximate
    bound adds its outer year), or an instant (``at`` ``YYYY[-MM[-DD]]``)."""

    start: int | None = Field(default=None, alias="from", ge=1900, le=2100)
    end: int | None = Field(default=None, alias="to", ge=1900, le=2100)
    start_outer: int | None = Field(default=None, alias="from_outer", ge=1900, le=2100)
    end_outer: int | None = Field(default=None, alias="to_outer", ge=1900, le=2100)
    at: str | None = Field(default=None, pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")


class Prop(_Model):
    """``s p o`` (``o`` a name, or ``lit`` a literal), negated with ``neg``, qualifiers ``q``,
    valid ``t`` (absent: always)."""

    s: str
    p: str
    o: str | None = None
    lit: Lit | None = None
    t: When | None = None
    neg: bool = False
    q: dict[str, str] = Field(default_factory=dict)


class ClosedSet(_Model):
    """``s``'s complete set of ``p`` objects (``members``; empty: none at all)."""

    p: str
    s: str
    members: list[str] = Field(default_factory=list)


class EntailmentInputs(_Model):
    claim: Prop
    canon: list[Prop] = Field(default_factory=list)
    closed_sets: list[ClosedSet] = Field(default_factory=list)
    strict_weak: bool = False
    note: str = Field(min_length=1, max_length=300)
    """What the case says in words (for the reviewer; the model reads the propositions)."""


class EntailmentCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    inputs: EntailmentInputs
    expected: Label

    @property
    def residual(self) -> bool:
        return self.family.startswith(RESIDUAL)


def _minute(year: int, month: int = 1, day: int = 1) -> int:
    return story_minute(datetime(year, month, day, tzinfo=UTC))


def _interval(when: When) -> Interval:
    def bound(year: int | None, outer: int | None) -> Bound | None:
        if year is None:
            return None
        inner = _minute(year)
        return Bound(inner=inner, outer=inner if outer is None else _minute(outer))

    return Interval(start=bound(when.start, when.start_outer), end=bound(when.end, when.end_outer))


def _instant(at: str) -> Instant:
    parts = [int(p) for p in at.split("-")]
    granularity = (Granularity.YEAR, Granularity.MONTH, Granularity.DAY)[len(parts) - 1]
    return Instant.of(_minute(*parts), granularity)


def proposition(p: Prop) -> Proposition:
    """The case vocabulary's ``p`` as #131's proposition over the suite's entity ids."""
    when = p.t
    if when is None:
        valid: Interval | Instant = Interval.of(None, None)
    elif when.at is not None:
        valid = _instant(when.at)
    else:
        valid = _interval(when)
    literal = None
    if p.lit is not None:
        kind = LiteralKind.INT if p.lit.kind == "int" else LiteralKind.STR
        literal = TypedLiteral(kind=kind, value=p.lit.value)
    return Proposition(
        subject_id=entity_id(p.s),
        predicate_id=predicate_id(p.p),
        object_id=entity_id(p.o) if p.o is not None else None,
        literal=literal,
        qualifiers=dict(p.q),
        polarity=Polarity.NEGATED if p.neg else Polarity.AFFIRMED,
        valid_time=valid,
    )


def _record_id(case_id: str, n: int) -> str:
    digest = int(hashlib.sha256(f"{case_id}:{n}".encode()).hexdigest()[:15], 16)
    return f"01JE01R{digest:019d}"[:26]


def records_of(case: EntailmentCase) -> list[Record]:
    return [
        Record(proposition(p), _record_id(case.id, n), _record_id(case.id, n))
        for n, p in enumerate(case.inputs.canon)
    ]


def closed_sets_of(case: EntailmentCase) -> list[ClosedSetRecord]:
    return [
        ClosedSetRecord(
            id=_record_id(case.id, 1000 + n),
            predicate_id=predicate_id(c.p),
            subject_id=entity_id(c.s),
            members=tuple(entity_id(m) for m in c.members),
        )
        for n, c in enumerate(case.inputs.closed_sets)
    ]


def score(expected: str, final: str, residual: bool) -> dict[str, bool | None]:
    """One case's observations of every E-01 metric (``None``: outside the denominator)."""
    found: dict[str, bool | None] = {
        FALSE_CONTRADICTION: None if expected == "contradicted" else final != "contradicted",
        FALSE_SUPPORT: None if expected == "supported" else final != "supported",
        ACCURACY: final == expected,
    }
    if residual:
        found[RESIDUAL_ACCURACY] = found[ACCURACY]
        found[RESIDUAL_FALSE_CONTRADICTION] = found[FALSE_CONTRADICTION]
        found[RESIDUAL_FALSE_SUPPORT] = found[FALSE_SUPPORT]
    return found


@register
class E01Suite(Suite):
    name = "e01"
    title = "E-01: entailment accuracy against objective canon"
    set_path = "entailment/cases.jsonl"
    min_cases = 400
    guarantees = ("entailment_fallback",)
    ci_gate = True
    case_model = EntailmentCase
    metrics = (
        Metric(
            FALSE_CONTRADICTION,
            Rule.rate,
            0.02,
            Direction.at_most,
            "a claim not contradicted by canon ended contradicted",
        ),
        Metric(
            FALSE_SUPPORT,
            Rule.rate,
            0.01,
            Direction.at_most,
            "a claim canon does not support ended supported",
        ),
        Metric(ACCURACY, Rule.informational, description="the final verdict equals the label"),
        Metric(
            RESIDUAL_ACCURACY,
            Rule.informational,
            description="the fuzzy residual alone: the final verdict equals the label",
        ),
        Metric(
            RESIDUAL_FALSE_CONTRADICTION,
            Rule.informational,
            direction=Direction.at_most,
            description="the fuzzy residual alone: false contradictions",
        ),
        Metric(
            RESIDUAL_FALSE_SUPPORT,
            Rule.informational,
            direction=Direction.at_most,
            description="the fuzzy residual alone: false support",
        ),
    )
    reading_notes = (
        "E-01's label is the final kind a correct check ends with; `uncertain` marks a claim "
        "neither canon nor world knowledge decides (the safe disposition). About 30 % of the "
        "cases are the fuzzy residual, the only ones that reach the model; the "
        "`e01.residual.*` rows show them alone.",
    )

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        assert isinstance(case, EntailmentCase)  # noqa: S101 - load() validated it as case_model
        source = RecordsSource(
            records_of(case),
            REGISTRY,
            closed_sets=closed_sets_of(case),
            options=Options(strict_weak=case.inputs.strict_weak),
            entity_names=ENTITY_NAMES,
        )
        verifier = Verifier(
            source,
            VerifyConfig(level="entailment"),
            models=ctx.models,
            budget=TurnBudget(deadline_s=CALL_DEADLINE_S),
        )
        result = await verifier.verify(proposition(case.inputs.claim))
        if result is None:  # pragma: no cover - level entailment is never off
            raise RuntimeError("verification is off")
        final = result.kind.value
        return Outcome(
            score(case.expected, final, case.residual),
            output=final,
            detail={
                "deterministic": result.verdict.kind.value,
                "reason": result.reason.value if result.reason is not None else None,
                "escalated": result.escalated,
                "model_verdict": result.model_verdict,
                "model_confidence": result.model_confidence,
            },
        )

    def stub_answers(self, case: CaseModel) -> Mapping[str, str]:
        assert isinstance(case, EntailmentCase)  # noqa: S101 - load() validated it as case_model
        verdict = "novel" if case.expected == "uncertain" else case.expected
        return {ENTAILMENT.name: json.dumps({"verdict": verdict, "confidence": STUB_CONFIDENCE})}


def label_counts(cases: Sequence[EntailmentCase]) -> dict[str, int]:
    """Cases per label (the digest's counts)."""
    found: dict[str, int] = {}
    for case in cases:
        found[case.expected] = found.get(case.expected, 0) + 1
    return found


__all__ = [
    "ACCURACY",
    "ENTITY_NAMES",
    "FALSE_CONTRADICTION",
    "FALSE_SUPPORT",
    "NAMES",
    "REGISTRY",
    "RESIDUAL_ACCURACY",
    "E01Suite",
    "EntailmentCase",
    "Prop",
    "entity_id",
    "label_counts",
    "proposition",
    "score",
]
