"""The deterministic contradiction rules and coverage logic (FR-PROP-8,10,11,12, FR-VERIFY-3;
docs/02 §4.9; #140).

**``check`` is pure.** It takes a normalized claim (a ``Proposition``), the holder's records
already fetched (``Record``: a proposition with its id and, for canon, the fact wrapping it;
``ClosedSetRecord``: an asserted closed set) and the story's ``Registry`` (#131), and answers a
``Verdict``. It reads nothing else; ``check_with`` does the fetch.

**Normalization of the record set.** Subjects and objects are expanded through the holder's
``same_as`` records to depth 1 (each id stands for the least id among itself and its direct
``same_as`` partners), symmetric predicates are put in their canonical direction, and the claim
is canonicalized the same way (``registry.canonical``).

**The rules, in FR-PROP-10's order; the first conclusive one wins.** Coverage comes first, then
the contradiction rules. When the holder's records are themselves inconsistent, the winning
rule's evidence lists every conflicting record it found.

1. **Coverage** (rule ``coverage``): the same proposition with the same polarity. "Same" means
   subject, predicate and object; the predicate's key qualifiers are equal; every other
   qualifier the claim names is the record's too. A claimed object set that is a subset of the
   record's set, or one member of it, is the same. The test depends on the claim's time:

   - A **fluent interval** claim is ``supported`` only when its possible span (outer bounds)
     lies inside the union of the matching records' definite spans (inner bounds; adjacent
     intervals join).
   - **Partial coverage** is ``uncertain(partial_coverage)``. The verdict reports the
     ``covered`` sub-intervals (the claim's possible span inside the definite union) and the
     ``uncovered`` ones (the claim's definite span outside the possible union: claimed in every
     reading and covered in none). Each uncovered sub-interval is a ``remainder_claims``
     entry, checked once as its own claim (normally ``novel``; no further splitting). A
     ``contradicted`` remainder makes the verdict ``contradicted``.
   - An **instant** claim is supported by a record at the same or a finer granularity inside
     its granule, or by intervals covering its whole granule. A coarser record that contains it
     makes it ``uncertain(granularity_refinement)``, and the claim itself is the remainder,
     checked without the coverage step (the added precision is a novel detail).
   - An **event** claim stated over an interval ("sometime in 2012") is supported by a matching
     record inside it, and a record that contains it is a granularity refinement.
   - A **permanent** predicate ignores valid-time here.
   - Overlap that the approximate bounds cannot decide is ``uncertain(approximate_bounds)``,
     unless a contradiction rule below is conclusive.

2. **Polarity** (``polarity``): the identical normalized proposition (subject, predicate,
   object and every qualifier) with the opposite polarity, over time that overlaps in every
   admissible reading. It applies to every predicate, weak ones included (FR-PROP-4); a
   permanent predicate overlaps at every time.
3. **Functional key** (``functional_key``): a functional predicate, the same uniqueness key
   (#131's ``uniqueness_key``: the subject plus the key qualifiers), a different object, over
   overlapping time. An event predicate's key is one event, so its time does not matter here,
   and a permanent predicate's never does. Objects related through the predicate's
   ``containment_of`` facts, in either direction up to ``containment_max_depth``, do not
   conflict (Columbus and Ohio). Two place or organization objects that no containment fact
   places in a hierarchy are ``uncertain(world_knowledge)``, and so are two quantities in
   different units: the answer is world knowledge the records do not hold.
4. **Closed set** (``closed_set``): an author's or canon's closed set for the claim's key over
   overlapping time does not list the claim's object, and no member contains it. Closed sets are
   never inferred.
5. **Max cardinality** (``max_cardinality``): a ``multi`` predicate with a declared maximum,
   when the claim's new object would be one too many at some instant of the overlap.
6. **Exclusion** (``exclusion``): a declared exclusion, in either direction (``alive`` and
   ``dead``), holds for the subject over overlapping time, compared on each side's own
   valid-time.
7. **Implication** (``implication``): the claim negates what a holder record implies through
   declared rules (#131's ``imply``, to the profile's ``verification.implication_max_depth``).
   A closure the depth stopped is flagged in the verdict's evidence (``depth_capped``).
8. **Instant identity** (``instant_identity``): two times on the same unique event (a shared
   ``event_id`` or the predicate's uniqueness key, ``registry.same_event``) that are not even
   coarsely compatible. Events without a shared identity never contradict and never merge.

Nothing else contradicts. A weak predicate (FR-PROP-4, ``Predicate.weak``) lacks every
semantic the rules after polarity read, so it never yields ``contradicted`` from cardinality,
exclusion, implication or containment. With ``Options.strict_weak``, a different object on a
weak predicate is ``uncertain(weak_semantics)`` instead of ``novel``. A claim with no matching
record is ``novel``, because the world is open (FR-PROP-8).

**Confidence** (``Verdict.confidence``, the input to #142's gate): 1.0 for rule verdicts and for
``partial_coverage`` and ``granularity_refinement``, which are resolved here; 0.5 for
``approximate_bounds`` and ``world_knowledge``; 0.0 for ``weak_semantics``, ``argument_type``
and the ``unresolved_*`` reasons.

**Holders.** ``check`` works the same for canon and utterance records (one proposition shape,
FR-PROP-2). Belief holders arrive in M5, so a claim checked against one is
``uncertain(unresolved_predicate)``.

**The fetch.** ``check_with`` makes every read that a verdict rests on through
``KeyedLookup.canon_at``, so each is a query key of the turn's read set (#136):

- the subject's and object's ``same_as``;
- the claim's predicate for the expanded subjects, windowed to the claim's possible span (not
  windowed for event and permanent predicates);
- the predicate's containment predicate (any subject);
- each excluding predicate;
- for a negated claim, the antecedent and join predicates of the declared rules that reach it,
  to one level past the depth, so the closure can say it was stopped.

Canon reads take only active, current versions (#130, #133): a dormant fact is never evidence.
Closed sets are read from ``closed_sets``. In M4 the author writes them at genesis, before any
turn, and canon's own (#145) will need keying when they land.

**``verify``** asks the story's effective profile first (#129). With ``verification`` off, it
is inert: no lookup, no verdict. With ``contradiction_checking`` off, no contradiction rule runs.
It records each verdict for the turn's read set. ``recheck``, which is registered with
``core.readset.register_rebase_check``, re-runs the turn's verdicts under the story lock on a
rebase and after an interpretive configuration change; a verdict whose kind changed
regenerates the turn.

Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

import itertools
import logging
import math
import weakref
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import FactCommitState
from epistrel.core.profiles.capabilities import level_at_least, module_on
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.propositions.model import (
    Cardinality,
    Instant,
    Interval,
    LiteralKind,
    Polarity,
    Predicate,
    Proposition,
    TemporalKind,
    TypedLiteral,
    coarsely_compatible,
    covers,
    finer,
    overlaps,
)
from epistrel.core.propositions.registry import (
    IMPLICATION_MAX_DEPTH,
    Registry,
    canonical,
    cardinality_may_contradict,
    imply,
    load,
    same_event,
)
from epistrel.core.propositions.vocabulary import SAME_AS
from epistrel.core.readset import (
    KeyedLookup,
    StructuredReadSet,
    holder_of,
    register_rebase_check,
)
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import closed_sets as closed_sets_table

if TYPE_CHECKING:
    from epistrel.core.canon.facts import Fact
    from epistrel.core.events.operation import Operation

log = logging.getLogger(__name__)

EVIDENCE_STATES: frozenset[FactCommitState] = frozenset(
    {FactCommitState.COMMITTED, FactCommitState.REVISED}
)
"""The commit states a canon fact is evidence in (#146): an ``introduced`` or ``pending`` fact
is not settled yet. (A ``revised`` version is out of the timeline anyway, #154.)"""

CONTAINMENT_MAX_DEPTH = 3
"""How many ``containment_of`` hops relate two objects (Columbus → Ohio → USA is two)."""
HIERARCHY_KINDS = frozenset({"place", "organization"})
"""Object kinds whose containment is world knowledge when no fact places them."""

Span = tuple[int | None, int | None]
"""``[lo, hi)`` in story-minutes; ``None`` is open on that side."""


# -- verdicts -----------------------------------------------------------------------------------


class VerdictKind(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    NOVEL = "novel"
    UNCERTAIN = "uncertain"


class VerdictReason(StrEnum):
    """Why a verdict is ``uncertain`` (#142's gate and #155's gate-reason field read it)."""

    PARTIAL_COVERAGE = "partial_coverage"
    GRANULARITY_REFINEMENT = "granularity_refinement"
    APPROXIMATE_BOUNDS = "approximate_bounds"
    UNRESOLVED_PREDICATE = "unresolved_predicate"
    UNRESOLVED_ENTITY = "unresolved_entity"
    UNRESOLVED_TIME = "unresolved_time"
    WEAK_SEMANTICS = "weak_semantics"
    WORLD_KNOWLEDGE = "world_knowledge"
    ARGUMENT_TYPE = "argument_type"


class Rule(StrEnum):
    """What produced a verdict: ``coverage`` for ``supported``, a contradiction rule for
    ``contradicted``."""

    COVERAGE = "coverage"
    POLARITY = "polarity"
    FUNCTIONAL_KEY = "functional_key"
    MAX_CARDINALITY = "max_cardinality"
    CLOSED_SET = "closed_set"
    EXCLUSION = "exclusion"
    IMPLICATION = "implication"
    INSTANT_IDENTITY = "instant_identity"


CONTRADICTION_RULES = frozenset(Rule) - {Rule.COVERAGE}

CONFIDENCE: Mapping[VerdictReason, float] = {
    VerdictReason.PARTIAL_COVERAGE: 1.0,
    VerdictReason.GRANULARITY_REFINEMENT: 1.0,
    VerdictReason.APPROXIMATE_BOUNDS: 0.5,
    VerdictReason.WORLD_KNOWLEDGE: 0.5,
    VerdictReason.WEAK_SEMANTICS: 0.0,
    VerdictReason.ARGUMENT_TYPE: 0.0,
    VerdictReason.UNRESOLVED_PREDICATE: 0.0,
    VerdictReason.UNRESOLVED_ENTITY: 0.0,
    VerdictReason.UNRESOLVED_TIME: 0.0,
}
"""``Verdict.confidence`` of an ``uncertain`` verdict, by reason; rule verdicts are 1.0."""


HolderKind = Literal["canon", "utterance", "belief"]


@dataclass(frozen=True, slots=True)
class Holder:
    """Whose records a claim is checked against: ``canon`` (narration), the speaker's own
    ``utterance``s, or a character's ``belief``s (M5)."""

    kind: HolderKind = "canon"
    holder_id: str | None = None

    @classmethod
    def utterance(cls, speaker_id: str) -> Holder:
        return cls("utterance", speaker_id)

    @classmethod
    def belief(cls, character_id: str) -> Holder:
        return cls("belief", character_id)

    def key(self) -> str:
        """The read set's holder (``canon`` or ``utterance:<speaker_id>``; a belief's id)."""
        if self.kind == "belief":
            return f"belief:{self.holder_id}"
        return holder_of(self.holder_id if self.kind == "utterance" else None)


CANON = Holder()


@dataclass(frozen=True, slots=True)
class Record:
    """A holder record ``check`` reads: a proposition, its id and, for canon, its fact."""

    proposition: Proposition
    proposition_id: str
    fact_id: str | None = None

    @classmethod
    def of_fact(cls, fact: Fact) -> Record:
        """A canon fact as a record, with the fact's own valid-time, which is authoritative
        (#133). An instant keeps its granularity when the fact spans exactly its granule."""
        stated = fact.proposition.valid_time
        own: Interval | Instant = fact.valid_time
        if isinstance(stated, Instant) and (fact.valid_from, fact.valid_to) == (
            stated.at,
            stated.until,
        ):
            own = stated
        return cls(
            fact.proposition.model_copy(update={"valid_time": own}), fact.proposition_id, fact.id
        )


@dataclass(frozen=True, slots=True)
class ClosedSetRecord:
    """An asserted closed set (FR-PROP-8): ``members`` are all the objects of ``predicate_id``
    for the key (``subject_id`` and the ``key`` qualifiers) over ``[valid_from, valid_to)``.
    An empty set says there is none ("an only child")."""

    id: str
    predicate_id: str
    subject_id: str
    members: tuple[str, ...] = ()
    key: Mapping[str, Any] = field(default_factory=dict)
    valid_from: int | None = None
    valid_to: int | None = None

    def interval(self) -> Interval:
        return Interval.of(self.valid_from, self.valid_to)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One record a verdict rests on: the ``rule`` and the proposition (with its fact) or the
    closed set it cites. ``depth_capped`` marks the implication closure's depth stop, which cites
    no record."""

    rule: Rule
    proposition_id: str | None = None
    fact_id: str | None = None
    closed_set_id: str | None = None
    depth_capped: bool = False

    def as_payload(self) -> dict[str, Any]:
        found: dict[str, Any] = {"rule": self.rule.value}
        for name in ("proposition_id", "fact_id", "closed_set_id"):
            value = getattr(self, name)
            if value is not None:
                found[name] = value
        if self.depth_capped:
            found["depth_capped"] = True
        return found


@dataclass(frozen=True, slots=True)
class Verdict:
    """``check``'s answer (see the module docstring). ``remainder_verdicts`` holds the verdict
    of each ``remainder_claims`` entry, in order."""

    kind: VerdictKind
    reason: VerdictReason | None = None
    evidence: tuple[Evidence, ...] = ()
    covered: tuple[Span, ...] = ()
    uncovered: tuple[Span, ...] = ()
    remainder_claims: tuple[Proposition, ...] = ()
    remainder_verdicts: tuple[Verdict, ...] = ()
    confidence: float = 1.0
    holder: Holder = CANON
    position: Any = None

    @property
    def rules(self) -> frozenset[Rule]:
        return frozenset(e.rule for e in self.evidence if not e.depth_capped)

    @property
    def depth_capped(self) -> bool:
        return any(e.depth_capped for e in self.evidence)

    def cited(self) -> tuple[str, ...]:
        """The proposition and closed-set ids the evidence cites, in order."""
        return tuple(
            i for e in self.evidence for i in (e.proposition_id, e.closed_set_id) if i is not None
        )

    def as_payload(self) -> dict[str, Any]:
        """The verdict as ids, spans and codes: no story text (invariant 3)."""
        return {
            "kind": self.kind.value,
            "reason": None if self.reason is None else self.reason.value,
            "evidence": [e.as_payload() for e in self.evidence],
            "covered": [list(s) for s in self.covered],
            "uncovered": [list(s) for s in self.uncovered],
            "remainders": [v.as_payload() for v in self.remainder_verdicts],
            "confidence": self.confidence,
            "holder": self.holder.key(),
        }


def uncertain(reason: VerdictReason, *, holder: Holder = CANON, position: Any = None) -> Verdict:
    """An ``uncertain`` verdict for ``reason``, with its confidence."""
    return Verdict(
        VerdictKind.UNCERTAIN,
        reason,
        confidence=CONFIDENCE[reason],
        holder=holder,
        position=position,
    )


@dataclass(frozen=True, slots=True)
class Options:
    """What a check may conclude: ``contradictions`` off (``verification.
    contradiction_checking`` off) runs coverage only; the implication and containment depths;
    ``strict_weak`` asks for conflicts beyond polarity on weak predicates."""

    contradictions: bool = True
    implication_max_depth: int = IMPLICATION_MAX_DEPTH
    containment_max_depth: int = CONTAINMENT_MAX_DEPTH
    strict_weak: bool = False


# -- spans --------------------------------------------------------------------------------------


def _lo(x: int | None) -> float:
    return -math.inf if x is None else x


def _hi(x: int | None) -> float:
    return math.inf if x is None else x


def _span(lo: float, hi: float) -> Span:
    return (None if lo == -math.inf else int(lo), None if hi == math.inf else int(hi))


def union(spans: Iterable[Span]) -> list[Span]:
    """The union of ``spans``, sorted; adjacent spans join (``[a, b)`` and ``[b, c)``)."""
    ordered = sorted(((_lo(a), _hi(b)) for a, b in spans), key=lambda s: s[0])
    out: list[tuple[float, float]] = []
    for lo, hi in ordered:
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return [_span(lo, hi) for lo, hi in out]


def intersect(target: Span, spans: Sequence[Span]) -> list[Span]:
    """The parts of ``target`` inside the (sorted, disjoint) ``spans``."""
    lo, hi = _lo(target[0]), _hi(target[1])
    out = []
    for a, b in spans:
        x, y = max(lo, _lo(a)), min(hi, _hi(b))
        if x < y:
            out.append(_span(x, y))
    return out


def subtract(target: Span, spans: Sequence[Span]) -> list[Span]:
    """The parts of ``target`` outside the (sorted, disjoint) ``spans``."""
    lo, hi = _lo(target[0]), _hi(target[1])
    out = []
    cursor = lo
    for a, b in spans:
        x, y = _lo(a), _hi(b)
        if y <= cursor or x >= hi:
            continue
        if x > cursor:
            out.append(_span(cursor, x))
        cursor = max(cursor, y)
        if cursor >= hi:
            break
    if cursor < hi:
        out.append(_span(cursor, hi))
    return out


def _meets(a: Span, b: Span) -> bool:
    return max(_lo(a[0]), _lo(b[0])) < min(_hi(a[1]), _hi(b[1]))


# -- the record set -------------------------------------------------------------------------


@dataclass(slots=True)
class _Context:
    """The normalized record set: ``same_as`` representatives, records by subject and
    predicate, containment edges and closed sets."""

    registry: Registry
    options: Options
    reps: dict[str, str]
    records: list[tuple[Record, Proposition]]
    closed: list[ClosedSetRecord]
    contains: dict[str, set[str]]
    """Containment edges by containment predicate: ``"<pred>:<inner>"`` → outer objects."""
    placed: dict[str, set[str]]
    """Objects some containment fact of each containment predicate names, either side."""

    @classmethod
    def build(
        cls,
        records: Sequence[Record],
        closed: Sequence[ClosedSetRecord],
        registry: Registry,
        options: Options,
    ) -> _Context:
        reps: dict[str, str] = {}
        for record in records:
            p = record.proposition
            if p.predicate_id == SAME_AS and p.polarity is Polarity.AFFIRMED and p.object_id:
                for a, b in ((p.subject_id, p.object_id), (p.object_id, p.subject_id)):
                    reps[a] = min(reps.get(a, a), b)
        ctx = cls(registry, options, reps, [], list(closed), {}, {})
        ctx.records = [(r, ctx.norm(r.proposition)) for r in records]
        containment = {
            p.containment_of for p in registry.predicates.values() if p.containment_of is not None
        }
        for _, p in ctx.records:
            if (
                p.predicate_id in containment
                and p.polarity is Polarity.AFFIRMED
                and p.object_id is not None
            ):
                ctx.contains.setdefault(f"{p.predicate_id}:{p.subject_id}", set()).add(p.object_id)
                placed = ctx.placed.setdefault(p.predicate_id, set())
                placed.update((p.subject_id, p.object_id))
        return ctx

    def rep(self, entity_id: str) -> str:
        return self.reps.get(entity_id, entity_id)

    def norm(self, p: Proposition) -> Proposition:
        """``p`` with ``same_as`` representatives, in canonical direction."""
        update: dict[str, Any] = {"subject_id": self.rep(p.subject_id)}
        if p.object_id is not None:
            update["object_id"] = self.rep(p.object_id)
        if p.object_set is not None:
            update["object_set"] = tuple(sorted({self.rep(o) for o in p.object_set}))
        return canonical(p.model_copy(update=update), self.registry)

    def by(self, predicate_id: str, subject_id: str) -> list[tuple[Record, Proposition]]:
        return [
            (r, p)
            for r, p in self.records
            if p.predicate_id == predicate_id and p.subject_id == subject_id
        ]

    def outer_of(self, predicate_id: str, inner: str) -> set[str]:
        """What contains ``inner`` through ``predicate_id``, up to the containment depth."""
        found: set[str] = set()
        frontier = {inner}
        for _ in range(self.options.containment_max_depth):
            step = set().union(*(self.contains.get(f"{predicate_id}:{x}", set()) for x in frontier))
            step -= found | {inner}
            if not step:
                break
            found |= step
            frontier = step
        return found

    def related(self, predicate: Predicate, a: str, b: str) -> bool:
        """Whether ``a`` and ``b`` are related by ``predicate``'s containment either way."""
        via = predicate.containment_of
        if via is None:
            return False
        return a == b or b in self.outer_of(via, a) or a in self.outer_of(via, b)

    def unplaced(self, predicate: Predicate, *objects: str) -> bool:
        """Whether a containment predicate exists and some object has no containment fact."""
        via = predicate.containment_of
        if via is None:
            return False
        placed = self.placed.get(via, set())
        return any(o not in placed for o in objects)


# -- object and qualifier comparisons ---------------------------------------------------------


def _literal_equal(a: TypedLiteral, b: TypedLiteral) -> bool:
    if a.kind is LiteralKind.STR and b.kind is LiteralKind.STR:
        return str(a.value).casefold() == str(b.value).casefold()
    return a.equals(b)


def _objects(p: Proposition) -> tuple[str, ...]:
    if p.object_id is not None:
        return (p.object_id,)
    return p.object_set or ()


def _object_equal(a: Proposition, b: Proposition) -> bool:
    """The identical object (sets equal, literals typed-equal, strings casefolded)."""
    if a.literal is not None or b.literal is not None:
        return (
            a.literal is not None and b.literal is not None and _literal_equal(a.literal, b.literal)
        )
    if a.object_set is not None or b.object_set is not None:
        return set(_objects(a)) == set(_objects(b)) and bool(_objects(a))
    return a.object_id == b.object_id


def _object_within(claim: Proposition, record: Proposition) -> bool:
    """The claim's object is the record's, or a subset of (or one member of) its set."""
    if claim.literal is not None or record.literal is not None:
        return _object_equal(claim, record)
    mine, theirs = _objects(claim), _objects(record)
    if not mine and not theirs:
        return True
    return bool(mine) and set(mine) <= set(theirs)


def _qualifiers_equal(a: Proposition, b: Proposition) -> bool:
    return a.qualifier_key() == b.qualifier_key()


def _same_for_support(predicate: Predicate, claim: Proposition, record: Proposition) -> bool:
    if not _object_within(claim, record):
        return False
    if predicate.uniqueness_key(claim) != predicate.uniqueness_key(record):
        return False
    theirs = dict(record.qualifier_key())
    return all(theirs.get(k, _MISSING) == v for k, v in claim.qualifier_key())


_MISSING = object()


def _identical(claim: Proposition, record: Proposition) -> bool:
    return _object_equal(claim, record) and _qualifiers_equal(claim, record)


# -- the rules --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Outcome:
    """A rule's finding: the evidence of a conflict, or that it could not decide."""

    evidence: list[Evidence] = field(default_factory=list)
    undecided: VerdictReason | None = None


def _ev(rule: Rule, record: Record) -> Evidence:
    return Evidence(rule, record.proposition_id, record.fact_id)


def _overlap(
    predicate: Predicate, a: Interval | Instant, b: Interval | Instant, *, timeless: bool = False
) -> bool | None:
    if timeless or predicate.temporal_kind is TemporalKind.PERMANENT:
        return True
    return overlaps(a, b)


@dataclass(slots=True)
class _Support:
    kind: Literal["supported", "partial", "refine", "approximate", "none"]
    evidence: list[Evidence] = field(default_factory=list)
    covered: list[Span] = field(default_factory=list)
    uncovered: list[Span] = field(default_factory=list)


def _support(
    claim: Proposition, predicate: Predicate, same: list[tuple[Record, Proposition]]
) -> _Support:
    matches = [
        (r, p)
        for r, p in same
        if p.polarity is claim.polarity and _same_for_support(predicate, claim, p)
    ]
    if not matches:
        return _Support("none")
    if predicate.temporal_kind is TemporalKind.PERMANENT:
        return _Support("supported", [_ev(Rule.COVERAGE, r) for r, _ in matches])
    vt = claim.valid_time
    if isinstance(vt, Instant):
        return _support_instant(vt, matches)
    if predicate.temporal_kind is TemporalKind.EVENT:
        return _support_event(vt, matches)
    target = vt.possible()
    definite = union(p.valid_time.definite() for _, p in matches)
    possible = union(p.valid_time.possible() for _, p in matches)
    if not subtract(target, definite):
        cited = [
            _ev(Rule.COVERAGE, r) for r, p in matches if _meets(p.valid_time.possible(), target)
        ]
        return _Support("supported", cited)
    covered = intersect(target, definite)
    # Uncovered: what the claim says in every reading and no record may cover, so a remainder
    # claim never says more than the claim did.
    uncovered = subtract(vt.definite(), possible)
    meeting = [r for r, p in matches if _meets(p.valid_time.possible(), target)]
    if not covered:
        return _Support("approximate" if meeting else "none")
    if not uncovered:
        return _Support("approximate")
    return _Support(
        "partial", [_ev(Rule.COVERAGE, r) for r in meeting], covered=covered, uncovered=uncovered
    )


def _support_instant(vt: Instant, matches: list[tuple[Record, Proposition]]) -> _Support:
    granule = vt.definite()
    fine = [
        r
        for r, p in matches
        if isinstance(p.valid_time, Instant)
        and not finer(vt.granularity, p.valid_time.granularity)
        and _lo(granule[0]) <= p.valid_time.at
        and p.valid_time.until <= _hi(granule[1])
    ]
    if fine:
        return _Support("supported", [_ev(Rule.COVERAGE, r) for r in fine])
    spans = [p.valid_time for _, p in matches if isinstance(p.valid_time, Interval)]
    if not subtract(granule, union(s.definite() for s in spans)):
        cited = [
            _ev(Rule.COVERAGE, r)
            for r, p in matches
            if isinstance(p.valid_time, Interval) and _meets(p.valid_time.possible(), granule)
        ]
        return _Support("supported", cited)
    coarse = [
        r
        for r, p in matches
        if isinstance(p.valid_time, Instant) and covers(p.valid_time, vt) is True
    ]
    if coarse:
        return _Support("refine", [_ev(Rule.COVERAGE, r) for r in coarse])
    if any(_meets(p.valid_time.possible(), granule) for _, p in matches):
        return _Support("approximate")
    return _Support("none")


def _support_event(vt: Interval, matches: list[tuple[Record, Proposition]]) -> _Support:
    """An event claimed over an interval: some matching event inside it."""
    inside = [r for r, p in matches if covers(vt, p.valid_time) is True]
    if inside:
        return _Support("supported", [_ev(Rule.COVERAGE, r) for r in inside])
    containing = [r for r, p in matches if covers(p.valid_time, vt) is True]
    if containing:
        return _Support("refine", [_ev(Rule.COVERAGE, r) for r in containing])
    if any(overlaps(vt, p.valid_time) is not False for _, p in matches):
        return _Support("approximate")
    return _Support("none")


def _polarity(
    claim: Proposition, predicate: Predicate, same: list[tuple[Record, Proposition]]
) -> _Outcome:
    out = _Outcome()
    for r, p in same:
        if p.polarity is claim.polarity or not _identical(claim, p):
            continue
        found = _overlap(predicate, claim.valid_time, p.valid_time)
        if found:
            out.evidence.append(_ev(Rule.POLARITY, r))
        elif found is None:
            out.undecided = VerdictReason.APPROXIMATE_BOUNDS
    return out


def _quantity_units_differ(a: Proposition, b: Proposition) -> bool:
    return (
        a.literal is not None
        and b.literal is not None
        and a.literal.kind is LiteralKind.QUANTITY
        and b.literal.kind is LiteralKind.QUANTITY
        and a.literal.unit != b.literal.unit
    )


def _functional(
    claim: Proposition,
    predicate: Predicate,
    same: list[tuple[Record, Proposition]],
    ctx: _Context,
) -> _Outcome:
    out = _Outcome()
    if predicate.cardinality is not Cardinality.FUNCTIONAL or claim.polarity is Polarity.NEGATED:
        return out
    key = predicate.uniqueness_key(claim)
    event = predicate.temporal_kind is TemporalKind.EVENT
    for r, p in same:
        if p.polarity is Polarity.NEGATED or predicate.uniqueness_key(p) != key:
            continue
        if _object_within(claim, p):
            continue
        if claim.object_id is not None and p.object_id is not None:
            if ctx.related(predicate, claim.object_id, p.object_id):
                continue
            if ctx.unplaced(predicate, claim.object_id, p.object_id):
                found = _overlap(predicate, claim.valid_time, p.valid_time, timeless=event)
                if found is not False:
                    out.undecided = out.undecided or VerdictReason.WORLD_KNOWLEDGE
                continue
        if _quantity_units_differ(claim, p):
            out.undecided = out.undecided or VerdictReason.WORLD_KNOWLEDGE
            continue
        found = _overlap(predicate, claim.valid_time, p.valid_time, timeless=event)
        if found:
            out.evidence.append(_ev(Rule.FUNCTIONAL_KEY, r))
        elif found is None:
            out.undecided = out.undecided or VerdictReason.APPROXIMATE_BOUNDS
    return out


def _closed_set(claim: Proposition, predicate: Predicate, ctx: _Context) -> _Outcome:
    out = _Outcome()
    if claim.polarity is Polarity.NEGATED or claim.literal is not None:
        return out
    pairs = [(claim.subject_id, _objects(claim))]
    if predicate.symmetric and claim.object_id is not None:
        pairs.append((claim.object_id, (claim.subject_id,)))
    for closed in ctx.closed:
        if closed.predicate_id != claim.predicate_id:
            continue
        if any(claim.qualifiers.get(k) != v for k, v in closed.key.items()):
            continue
        members = {ctx.rep(m) for m in closed.members}
        for subject, objects in pairs:
            if ctx.rep(closed.subject_id) != subject or not objects:
                continue
            outside = [
                o
                for o in objects
                if o not in members and not any(ctx.related(predicate, o, m) for m in members)
            ]
            if not outside:
                continue
            found = _overlap(predicate, claim.valid_time, closed.interval())
            if found:
                out.evidence.append(Evidence(Rule.CLOSED_SET, closed_set_id=closed.id))
            elif found is None:
                out.undecided = VerdictReason.APPROXIMATE_BOUNDS
    return out


def _max_cardinality(
    claim: Proposition, predicate: Predicate, same: list[tuple[Record, Proposition]]
) -> _Outcome:
    out = _Outcome()
    limit = predicate.max_cardinality
    if (
        predicate.cardinality is not Cardinality.MULTI
        or not cardinality_may_contradict(predicate)
        or limit is None
        or claim.polarity is Polarity.NEGATED
        or not (_objects(claim) or claim.literal is not None)
    ):
        return out
    key = predicate.uniqueness_key(claim)
    others: list[tuple[Record, Proposition]] = []
    undecided = False
    for r, p in same:
        if p.polarity is Polarity.NEGATED or predicate.uniqueness_key(p) != key:
            continue
        if _object_within(claim, p) or _object_within(p, claim):
            continue
        found = _overlap(predicate, claim.valid_time, p.valid_time)
        if found:
            others.append((r, p))
        elif found is None:
            undecided = True
    if not others:
        if undecided:
            out.undecided = VerdictReason.APPROXIMATE_BOUNDS
        return out
    timeless = predicate.temporal_kind is TemporalKind.PERMANENT
    target = claim.valid_time.definite()
    cuts = sorted(
        {_lo(target[0]), _hi(target[1])}
        | {
            x
            for _, p in others
            for x in (_lo(p.valid_time.definite()[0]), _hi(p.valid_time.definite()[1]))
            if _lo(target[0]) < x < _hi(target[1])
        }
    )
    worst: list[tuple[Record, Proposition]] = []
    for lo, hi in itertools.pairwise(cuts):
        piece = _span(lo, hi)
        at = [(r, p) for r, p in others if timeless or _meets(p.valid_time.definite(), piece)]
        distinct = {p.object_key() for _, p in at}
        if len(distinct) >= limit and len(distinct) > len({q.object_key() for _, q in worst}):
            worst = at
    if worst:
        out.evidence = [_ev(Rule.MAX_CARDINALITY, r) for r, _ in worst]
    elif undecided:
        out.undecided = VerdictReason.APPROXIMATE_BOUNDS
    return out


def _exclusion(claim: Proposition, predicate: Predicate, ctx: _Context) -> _Outcome:
    out = _Outcome()
    if claim.polarity is Polarity.NEGATED:
        return out
    excluded = set(predicate.exclusions) | {
        q.id for q in ctx.registry.predicates.values() if predicate.id in q.exclusions
    }
    for x in sorted(excluded):
        for r, p in ctx.by(x, claim.subject_id):
            if p.polarity is Polarity.NEGATED:
                continue
            if _objects(claim) and _objects(p) and not _object_equal(claim, p):
                continue
            found = overlaps(claim.valid_time, p.valid_time)
            if found:
                out.evidence.append(_ev(Rule.EXCLUSION, r))
            elif found is None:
                out.undecided = VerdictReason.APPROXIMATE_BOUNDS
    return out


def _implication(claim: Proposition, ctx: _Context) -> tuple[_Outcome, bool]:
    """The claim negates what a record implies; also whether a closure was depth-capped."""
    out = _Outcome()
    rules = ctx.registry.rules()
    if claim.polarity is not Polarity.NEGATED or not rules:
        return out, False
    starts = {rule.when for rule in rules}
    vias = {rule.via for rule in rules if rule.via is not None}
    joins = [
        (r, p) for r, p in ctx.records if p.predicate_id in vias and p.polarity is Polarity.AFFIRMED
    ]
    capped = False
    target = claim.negation()
    for r, p in ctx.records:
        if (
            p.predicate_id not in starts
            or p.subject_id != claim.subject_id
            or p.polarity is not Polarity.AFFIRMED
        ):
            continue
        closure = imply([p, *(q for _, q in joins)], rules, ctx.options.implication_max_depth)
        capped = capped or closure.truncated
        for derived in closure.derived:
            if (
                derived.subject_id != claim.subject_id
                or derived.predicate_id != claim.predicate_id
                or not _object_equal(derived, target)
            ):
                continue
            found = overlaps(derived.valid_time, claim.valid_time)
            if found:
                out.evidence.append(_ev(Rule.IMPLICATION, r))
                out.evidence += [
                    _ev(Rule.IMPLICATION, j)
                    for j, q in joins
                    if q.object_id == claim.object_id and q.subject_id == p.object_id
                ]
                break
            if found is None:
                out.undecided = VerdictReason.APPROXIMATE_BOUNDS
    return out, capped


def _instant_identity(
    claim: Proposition, predicate: Predicate, same: list[tuple[Record, Proposition]]
) -> _Outcome:
    out = _Outcome()
    if claim.polarity is Polarity.NEGATED:
        return out
    for r, p in same:
        if p.polarity is Polarity.NEGATED or not same_event(claim, p, predicate):
            continue
        if not coarsely_compatible(claim.valid_time, p.valid_time):
            out.evidence.append(_ev(Rule.INSTANT_IDENTITY, r))
    return out


def _weak_conflict(
    claim: Proposition, predicate: Predicate, same: list[tuple[Record, Proposition]]
) -> bool:
    return (
        predicate.weak
        and claim.polarity is Polarity.AFFIRMED
        and any(
            p.polarity is Polarity.AFFIRMED
            and not _object_equal(claim, p)
            and overlaps(claim.valid_time, p.valid_time) is not False
            for _, p in same
        )
    )


# -- check ------------------------------------------------------------------------------------


def _check(
    claim: Proposition,
    predicate: Predicate,
    ctx: _Context,
    *,
    remainder: bool = False,
    skip_support: bool = False,
) -> Verdict:
    same = ctx.by(claim.predicate_id, claim.subject_id)
    pending: VerdictReason | None = None
    if not skip_support:
        support = _support(claim, predicate, same)
        if support.kind == "supported":
            return Verdict(VerdictKind.SUPPORTED, evidence=tuple(support.evidence))
        if support.kind == "partial" and not remainder:
            rests = tuple(
                claim.model_copy(update={"valid_time": Interval.of(lo, hi)})
                for lo, hi in support.uncovered
            )
            verdicts = tuple(_check(rest, predicate, ctx, remainder=True) for rest in rests)
            bad = [v for v in verdicts if v.kind is VerdictKind.CONTRADICTED]
            return Verdict(
                VerdictKind.CONTRADICTED if bad else VerdictKind.UNCERTAIN,
                None if bad else VerdictReason.PARTIAL_COVERAGE,
                evidence=tuple(e for v in bad for e in v.evidence)
                if bad
                else tuple(support.evidence),
                covered=tuple(support.covered),
                uncovered=tuple(support.uncovered),
                remainder_claims=rests,
                remainder_verdicts=verdicts,
            )
        if support.kind == "refine" and not remainder:
            rest = _check(claim, predicate, ctx, remainder=True, skip_support=True)
            if rest.kind is VerdictKind.CONTRADICTED:
                return replace(rest, remainder_claims=(claim,), remainder_verdicts=(rest,))
            return Verdict(
                VerdictKind.UNCERTAIN,
                VerdictReason.GRANULARITY_REFINEMENT,
                evidence=tuple(support.evidence),
                remainder_claims=(claim,),
                remainder_verdicts=(rest,),
                confidence=CONFIDENCE[VerdictReason.GRANULARITY_REFINEMENT],
            )
        if support.kind in ("approximate", "partial", "refine"):
            pending = (
                VerdictReason.APPROXIMATE_BOUNDS
                if support.kind == "approximate"
                else VerdictReason.PARTIAL_COVERAGE
                if support.kind == "partial"
                else VerdictReason.GRANULARITY_REFINEMENT
            )
    flags: list[Evidence] = []
    if ctx.options.contradictions:
        capped = False
        for outcome in _contradictions(claim, predicate, same, ctx):
            if isinstance(outcome, bool):
                capped = outcome
                continue
            if outcome.evidence:
                return Verdict(VerdictKind.CONTRADICTED, evidence=tuple(outcome.evidence))
            pending = pending or outcome.undecided
        if capped:
            flags.append(Evidence(Rule.IMPLICATION, depth_capped=True))
        if pending is None and ctx.options.strict_weak and _weak_conflict(claim, predicate, same):
            pending = VerdictReason.WEAK_SEMANTICS
    if pending is not None:
        return Verdict(
            VerdictKind.UNCERTAIN, pending, evidence=tuple(flags), confidence=CONFIDENCE[pending]
        )
    return Verdict(VerdictKind.NOVEL, evidence=tuple(flags))


def _contradictions(
    claim: Proposition,
    predicate: Predicate,
    same: list[tuple[Record, Proposition]],
    ctx: _Context,
) -> Iterable[_Outcome | bool]:
    """The contradiction rules in FR-PROP-10's order, lazily (the first conclusive one wins);
    the implication rule also yields whether its closure was depth-capped."""
    yield _polarity(claim, predicate, same)
    yield _functional(claim, predicate, same, ctx)
    yield _closed_set(claim, predicate, ctx)
    yield _max_cardinality(claim, predicate, same)
    yield _exclusion(claim, predicate, ctx)
    found, capped = _implication(claim, ctx)
    yield capped
    yield found
    yield _instant_identity(claim, predicate, same)


def check(
    claim: Proposition,
    records: Sequence[Record] = (),
    *,
    registry: Registry,
    closed_sets: Sequence[ClosedSetRecord] = (),
    holder: Holder = CANON,
    position: Any = None,
    options: Options = Options(),  # noqa: B008 - frozen
) -> Verdict:
    """The verdict on ``claim`` over the holder's ``records`` and ``closed_sets`` (see the
    module docstring). Pure: no I/O."""
    if holder.kind == "belief":
        return uncertain(VerdictReason.UNRESOLVED_PREDICATE, holder=holder, position=position)
    predicate = registry.get(claim.predicate_id)
    if predicate is None:
        return uncertain(VerdictReason.UNRESOLVED_PREDICATE, holder=holder, position=position)
    ctx = _Context.build(records, closed_sets, registry, options)
    found = _check(ctx.norm(claim), predicate, ctx)
    return replace(found, holder=holder, position=position)


# -- the keyed fetch --------------------------------------------------------------------------


def _window(valid_time: Interval | Instant) -> tuple[int | None, int | None] | None:
    span = valid_time.possible()
    return None if span == (None, None) else span


def _antecedents(registry: Registry, predicate_id: str, depth: int) -> tuple[set[str], set[str]]:
    """The predicates whose records can imply ``predicate_id`` within ``depth`` + 1 rule steps
    (one past the cap, so the closure can say it was stopped), and their join predicates."""
    rules = registry.rules()
    found: set[str] = set()
    vias: set[str] = set()
    frontier = {predicate_id}
    for _ in range(depth + 1):
        step = {r.when for r in rules if r.then in frontier} - found
        vias |= {r.via for r in rules if r.then in frontier and r.via is not None}
        if not step:
            break
        found |= step
        frontier = step
    return found, vias


async def closed_sets_for(
    conn: AsyncConnection, story_id: str, predicate_id: str
) -> list[ClosedSetRecord]:
    """The story's asserted closed sets of ``predicate_id``, by id (``check_with``'s read, and
    #142's ``fact_neighborhood``)."""
    t = closed_sets_table
    rows = await conn.execute(
        select(t.c.id, t.c.key, t.c.members, t.c.valid_from, t.c.valid_to)
        .where(t.c.story_id == story_id, t.c.predicate_id == predicate_id)
        .order_by(t.c.id)
    )
    found = []
    for row in rows:
        key = dict(row.key or {})
        subject = str(key.pop("subject_id", ""))
        found.append(
            ClosedSetRecord(
                id=str(row.id),
                predicate_id=predicate_id,
                subject_id=subject,
                members=tuple(str(m) for m in row.members or ()),
                key=key,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
            )
        )
    return found


async def check_with(
    lookup: KeyedLookup,
    story_id: str,
    claim: Proposition,
    *,
    holder: Holder = CANON,
    position: Any = None,
    through_position: tuple[int, int] | None = None,
    viewpoint: str = OMNISCIENT,
    registry: Registry | None = None,
    options: Options = Options(),  # noqa: B008 - frozen
    fetched: list[Record] | None = None,
) -> Verdict:
    """Fetch what ``claim`` needs through ``lookup`` (each read a query key of the turn's read
    set) and ``check`` it. ``viewpoint`` filters canon (a character's claim is checked only
    against what that character may read). Canon holders only: an utterance holder's records
    are #150's to fetch and pass to ``check``. ``fetched``, when given, receives the records
    the verdict was decided over (#142's entailment call is shown exactly those)."""
    if holder.kind == "utterance":
        raise ValueError("check_with reads canon; pass an utterance holder's records to check()")
    registry = registry or await load(lookup.conn, story_id)
    predicate = registry.get(claim.predicate_id)
    if holder.kind == "belief" or predicate is None:
        return check(claim, registry=registry, holder=holder, position=position, options=options)
    c = canonical(claim, registry)

    async def read(
        predicate_id: str,
        subjects: str | Sequence[str] | None,
        window: tuple[int | None, int | None] | None,
    ) -> list[Record]:
        facts = await lookup.canon_at(
            story_id,
            predicate=predicate_id,
            subjects=subjects,
            window=window,
            viewpoint=viewpoint,
            through_position=through_position,
        )
        # #146: an introduced or pending fact is not settled, so it is no evidence yet.
        return [Record.of_fact(f) for f in facts if f.commit_state in EVIDENCE_STATES]

    records: list[Record] = []
    subjects = {c.subject_id}
    for entity in dict.fromkeys(x for x in (c.subject_id, c.object_id) if x is not None):
        same = await read(SAME_AS, entity, None)
        records += same
        if entity == c.subject_id:
            for r in same:
                p = r.proposition
                if p.polarity is Polarity.AFFIRMED and p.object_id is not None:
                    subjects |= {p.subject_id, p.object_id}
    timeless = predicate.temporal_kind in (TemporalKind.EVENT, TemporalKind.PERMANENT)
    window = None if timeless else _window(c.valid_time)
    subject_set = sorted(subjects)
    records += await read(c.predicate_id, subject_set, window)
    if predicate.containment_of is not None:
        records += await read(predicate.containment_of, None, None)
    closed = await closed_sets_for(lookup.conn, story_id, c.predicate_id)
    if options.contradictions:
        excluded = set(predicate.exclusions) | {
            q.id for q in registry.predicates.values() if predicate.id in q.exclusions
        }
        for x in sorted(excluded):
            records += await read(x, subject_set, _window(c.valid_time))
        if c.polarity is Polarity.NEGATED and registry.rules():
            starts, vias = _antecedents(registry, c.predicate_id, options.implication_max_depth)
            for x in sorted(starts - {c.predicate_id}):
                records += await read(x, subject_set, _window(c.valid_time))
            for x in sorted(vias):
                records += await read(x, None, _window(c.valid_time))
    unique = list({(r.fact_id, r.proposition_id): r for r in records}.values())
    if fetched is not None:
        fetched.extend(unique)
    log.debug("verify fetched %d records, %d closed sets", len(unique), len(closed))
    return check(
        claim,
        unique,
        registry=registry,
        closed_sets=closed,
        holder=holder,
        position=position,
        options=options,
    )


# -- verify and the rebase re-check -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Call:
    """A verdict a turn's attempt rests on, to re-check at its rebase."""

    attempt: int
    story_id: str
    claim: Proposition
    holder: Holder
    through_position: tuple[int, int] | None
    viewpoint: str
    kind: VerdictKind


_CALLS: dict[int, tuple[weakref.ref[StructuredReadSet], list[_Call]]] = {}
"""The verdicts recorded per read set (by identity; dropped when the read set is)."""


def _remember(read_set: StructuredReadSet, call: _Call) -> None:
    key = id(read_set)
    entry = _CALLS.get(key)
    if entry is None or entry[0]() is not read_set:
        entry = (weakref.ref(read_set, lambda _: _CALLS.pop(key, None)), [])
        _CALLS[key] = entry
    entry[1].append(call)


def recorded(read_set: StructuredReadSet) -> list[_Call]:
    """The verdicts the read set's current attempt recorded."""
    entry = _CALLS.get(id(read_set))
    if entry is None or entry[0]() is not read_set:
        return []
    return [c for c in entry[1] if c.attempt == read_set.attempt]


async def options_for(conn: AsyncConnection, story_id: str) -> Options | None:
    """The story's verification options, or ``None`` when verification does not run: the
    pipeline is not ``structured`` or the ``verification`` module is off (#129)."""
    settings = (await effective_configuration(conn, story_id)).settings
    if not level_at_least(settings, "pipeline", "structured") or not module_on(
        settings, "verification"
    ):
        return None
    return Options(
        contradictions=module_on(settings, "contradiction_checking"),
        implication_max_depth=settings.verification.implication_max_depth,
    )


async def verify(
    conn: AsyncConnection,
    read_set: StructuredReadSet,
    story_id: str,
    claim: Proposition,
    *,
    holder: Holder = CANON,
    position: Any = None,
    through_position: tuple[int, int] | None = None,
    viewpoint: str = OMNISCIENT,
    remember: bool = True,
    fetched: list[Record] | None = None,
) -> Verdict | None:
    """The deterministic verdict on ``claim`` under the story's effective profile, its reads
    keyed in ``read_set``; ``None`` (inert, nothing read) when verification is off. The verdict
    is recorded for the turn's rebase re-check unless ``remember`` is off. ``fetched`` receives
    the records it was decided over (``check_with``)."""
    options = await options_for(conn, story_id)
    if options is None:
        return None
    verdict = await check_with(
        KeyedLookup(conn, read_set),
        story_id,
        claim,
        holder=holder,
        position=position,
        through_position=through_position,
        viewpoint=viewpoint,
        options=options,
        fetched=fetched,
    )
    if remember:
        _remember(
            read_set,
            _Call(
                read_set.attempt,
                story_id,
                claim,
                holder,
                through_position,
                viewpoint,
                verdict.kind,
            ),
        )
    return verdict


async def recheck(conn: AsyncConnection, op: Operation) -> bool:
    """The rebase re-check (``core.readset.REBASE_CHECKS``): every verdict the turn's current
    attempt rests on, re-run against current state and configuration on a scratch read set;
    ``False`` (regenerate) when one changed kind or verification no longer runs."""
    source = op.read_set_source
    if not isinstance(source, StructuredReadSet):
        return True
    calls = recorded(source)
    for call in calls:
        again = await verify(
            conn,
            StructuredReadSet(),
            call.story_id,
            call.claim,
            holder=call.holder,
            through_position=call.through_position,
            viewpoint=call.viewpoint,
            remember=False,
        )
        if again is None or again.kind is not call.kind:
            log.debug("rebase re-check: a verdict changed (%d checked)", len(calls))
            return False
    return True


register_rebase_check(recheck)


__all__ = [
    "CANON",
    "CONFIDENCE",
    "CONTAINMENT_MAX_DEPTH",
    "CONTRADICTION_RULES",
    "EVIDENCE_STATES",
    "ClosedSetRecord",
    "Evidence",
    "Holder",
    "Options",
    "Record",
    "Rule",
    "Span",
    "Verdict",
    "VerdictKind",
    "VerdictReason",
    "check",
    "check_with",
    "closed_sets_for",
    "intersect",
    "options_for",
    "recheck",
    "recorded",
    "subtract",
    "uncertain",
    "union",
    "verify",
]
