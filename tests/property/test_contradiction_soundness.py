"""CONTRADICTION-SOUNDNESS and SUPPORT-COVERAGE (docs/04 §6), their M4 parts (#140).

Hypothesis generates a holder's records and a claim over four predicates:

- the auto-registered (weakest) ``apprenticed_with``;
- functional ``located_at``, with ``located_in`` containment facts among the places;
- the author's ``multi`` ``close_friend_of`` with a maximum of 2;
- ``degree_awarded``, functional and keyed by (person, ``degree_id``), with set objects.

Times are exact, approximate or open intervals and instants at minute, hour and day
granularity, over story-time 0 to 10⁶ minutes. Each example has at most 8 records, and
``core.verify.rules.check`` is run on it.

Canon is generated consistent, as ``commit_fact`` keeps it: no two records with the identical
normalized proposition and opposite polarity may overlap in any reading. A record breaking that
is dropped from the example.

**CONTRADICTION-SOUNDNESS** (FR-PROP-3,4,8,10):

- On the weak predicate, ``contradicted`` comes only from a direct polarity conflict: an
  identical record with the opposite polarity overlapping in every reading. Such a conflict
  always yields ``contradicted`` when no identical record of the claim's own polarity overlaps
  the claim. That case is coverage's to answer first, in FR-PROP-10's order; under consistent
  canon it arises only when an instant claim's granule straddles both.
- A ``functional_key`` conflict cites only records of the claim's uniqueness key.
- Claims whose possible span meets no record's possible span (fluent predicates) are never
  ``contradicted``.
- A record whose object is containment-related to the claim's object is never cited as a
  functional conflict.
- A polarity conflict cites the identical proposition negated or (#441) a negation of what
  contains the claim's object, with the containment facts.
- Every ``contradicted`` verdict cites a contradiction rule and a proposition or closed set.

**SUPPORT-COVERAGE** (FR-PROP-1,10):

- ``supported`` implies the claim's time is covered under every admissible reading. A fluent
  interval claim's possible span must lie in the union of the matching records' definite spans.
  An instant claim needs a record at the same or a finer granularity inside its granule, or a
  definite union that covers the granule. An event window needs a matching record inside it.
- Overlap without coverage never yields ``supported``.
- No valid-time, remainders included, is empty.

The complement: every verdict kind, and every contradiction rule these predicates can produce,
is seen across the run (``_SEEN``).

**#441** adds ``test_contradiction_soundness__implied`` (an affirmed claim's consequences under a
declared rule, against the property's own oracle) and ``test_contradiction_soundness__key_set``
(closed sets over a key set).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from epistrel.core.propositions.model import (
    Bound,
    Granularity,
    Instant,
    Interval,
    Proposition,
    TemporalKind,
    finer,
    overlaps,
)
from epistrel.core.verify.rules import (
    CONTRADICTION_RULES,
    ClosedSetRecord,
    Record,
    Rule,
    Verdict,
    VerdictKind,
    check,
    subtract,
    union,
)
from tests import verify_support as vs

HORIZON = 1_000_000
STEPS = 12
STEP = HORIZON // STEPS // 1440 * 1440
"""A whole number of days, so grid instants share their day, hour and minute granules."""
MAX_RECORDS = 8
EXAMPLES = 300
PLACES = (vs.KITCHEN, vs.GARDEN, vs.HOUSE, vs.OHIO, vs.COLUMBUS)
CONTAINMENT = (
    (vs.KITCHEN, vs.HOUSE),
    (vs.GARDEN, vs.HOUSE),
    (vs.COLUMBUS, vs.OHIO),
)
FRIENDS = (vs.ANN, vs.CAL, vs.SMITH, vs.SMITH_2)
SMITHS = (vs.SMITH, vs.SMITH_2)
SCHOOLS = (vs.OSU, vs.FLORIDA)
PREDICATES = (vs.APPRENTICED, vs.LOCATED_AT, vs.FRIENDS_LIMITED, vs.DEGREE)
GRANULARITIES = (Granularity.MINUTE, Granularity.HOUR, Granularity.DAY)

_SEEN: Counter[str] = Counter()


@st.composite
def valid_times(draw: Any, *, instant_only: bool = False) -> Interval | Instant:
    kind = (
        "instant" if instant_only else draw(st.sampled_from(("exact", "approx", "open", "instant")))
    )
    # Half the times sit on a coarse grid, so overlaps, adjacency and equal instants are common.
    grid = draw(st.booleans())
    lo = draw(st.integers(0, STEPS)) * STEP if grid else draw(st.integers(0, HORIZON))
    if kind == "instant":
        return Instant.of(lo, draw(st.sampled_from(GRANULARITIES)))
    length = draw(st.integers(1, 4)) * STEP if grid else draw(st.integers(1, HORIZON // 10))
    hi = lo + length
    if kind == "exact":
        return Interval.of(lo, hi)
    if kind == "open":
        return Interval.of(lo, None) if draw(st.booleans()) else Interval.of(None, hi)
    slack = draw(st.integers(0, HORIZON // 20))
    inner_lo = draw(st.integers(lo, lo + length // 2))
    inner_hi = draw(st.integers(inner_lo + 1, hi))
    return Interval(
        start=Bound(inner=inner_lo, outer=max(0, lo - slack)),
        end=Bound(inner=inner_hi, outer=hi + slack),
    )


@st.composite
def propositions(draw: Any, predicate: str) -> Proposition:
    subject = draw(st.sampled_from((vs.BOBBY, vs.BOBBY, vs.BOBBY, vs.ANN)))
    negated = draw(st.booleans())
    if predicate == vs.DEGREE:
        members = draw(st.lists(st.sampled_from(SCHOOLS), min_size=1, max_size=2, unique=True))
        return vs.prop(
            subject,
            predicate,
            object_set=tuple(members),
            when=draw(valid_times(instant_only=True)),
            negated=negated,
            qualifiers={"degree_id": draw(st.sampled_from(("a", "b")))},
        )
    pool = {vs.APPRENTICED: SMITHS, vs.LOCATED_AT: PLACES, vs.FRIENDS_LIMITED: FRIENDS}[predicate]
    return vs.prop(
        subject, predicate, draw(st.sampled_from(pool)), draw(valid_times()), negated=negated
    )


def _consistent(found: list[Proposition]) -> list[Proposition]:
    """Drop every record that would make canon hold a proposition and its negation at once."""
    kept: list[Proposition] = []
    for p in found:
        if any(
            q.normalized_key() == p.normalized_key()
            and q.polarity is not p.polarity
            and overlaps(q.valid_time, p.valid_time) is not False
            for q in kept
        ):
            continue
        kept.append(p)
    return kept


@st.composite
def examples(draw: Any) -> tuple[Proposition, list[Record]]:
    predicate = draw(st.sampled_from(PREDICATES))
    claim = draw(propositions(predicate))
    found = _consistent(draw(st.lists(propositions(predicate), max_size=MAX_RECORDS)))
    if predicate == vs.LOCATED_AT:
        edges = draw(st.lists(st.sampled_from(CONTAINMENT), unique=True))
        found += [vs.prop(inner, vs.LOCATED_IN, outer) for inner, outer in edges]
    return claim, vs.records(*found[:MAX_RECORDS])


_PINNED: tuple[tuple[Proposition, list[Record]], ...] = (
    (
        vs.prop(vs.BOBBY, vs.FRIENDS_LIMITED, vs.SMITH, vs.span(5, 6)),
        vs.records(
            vs.prop(vs.BOBBY, vs.FRIENDS_LIMITED, vs.ANN, vs.span(0, 10)),
            vs.prop(vs.BOBBY, vs.FRIENDS_LIMITED, vs.CAL, vs.span(0, 10)),
        ),
    ),
    (
        vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.span(0, 40)),
        vs.records(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.OHIO, vs.span(10, 20))),
    ),
    (
        vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.GARDEN, vs.span(5, 6)),
        vs.records(
            vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(0, 10)),
            vs.prop(vs.KITCHEN, vs.LOCATED_IN, vs.HOUSE),
            vs.prop(vs.GARDEN, vs.LOCATED_IN, vs.HOUSE),
        ),
    ),
)
"""Examples Hypothesis always runs besides its own, so every rule the complement asks for is
exercised whatever the seed: a third concurrent friend, partial coverage, and two rooms of one
house."""


def _identical(a: Proposition, b: Proposition) -> bool:
    return a.normalized_key() == b.normalized_key()


def _related(a: str | None, b: str | None, records: list[Record]) -> bool:
    if a is None or b is None:
        return False
    edges = {
        (r.proposition.subject_id, r.proposition.object_id)
        for r in records
        if r.proposition.predicate_id == vs.LOCATED_IN
    }
    return a == b or (a, b) in edges or (b, a) in edges


def _verdict(claim: Proposition, records: list[Record]) -> Verdict:
    verdict = check(claim, records, registry=vs.REGISTRY)
    _SEEN[verdict.kind.value] += 1
    for rule in verdict.rules:
        _SEEN[rule.value] += 1
    return verdict


def _nonempty(valid_time: Interval | Instant) -> bool:
    lo, hi = valid_time.definite()
    return lo is None or hi is None or lo < hi


def _ids(verdict: Verdict) -> set[str]:
    return {e.proposition_id for e in verdict.evidence if e.proposition_id is not None}


@pytest.mark.part("M4", "FR-PROP-3,4,8,10")
def test_contradiction_soundness() -> None:
    """See the module docstring: contradictions only from the rules the predicate has, each
    citing its rule and record; the complement checks the run exercised them."""
    _SEEN.clear()

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    @given(example=examples())
    @example(example=_PINNED[0])
    @example(example=_PINNED[1])
    @example(example=_PINNED[2])
    def run(example: tuple[Proposition, list[Record]]) -> None:
        claim, records = example
        verdict = _verdict(claim, records)
        props = {r.proposition_id: r.proposition for r in records}
        if verdict.kind is VerdictKind.CONTRADICTED:
            assert verdict.rules, verdict
            assert verdict.rules <= CONTRADICTION_RULES
            assert verdict.cited(), verdict
        cited = [e for e in verdict.evidence if e.proposition_id in props]
        if claim.predicate_id == vs.APPRENTICED:
            same = [r.proposition for r in records if r.proposition.predicate_id == vs.APPRENTICED]
            direct = [
                p
                for p in same
                if _identical(p.negation(), claim)
                and overlaps(p.valid_time, claim.valid_time) is True
            ]
            if verdict.kind is VerdictKind.CONTRADICTED:
                assert verdict.rules == {Rule.POLARITY}, verdict
                assert direct, verdict
            agreeing = [
                p
                for p in same
                if _identical(p, claim) and overlaps(p.valid_time, claim.valid_time) is not False
            ]
            if direct and not agreeing:
                assert verdict.kind is VerdictKind.CONTRADICTED, (claim, records, verdict)
        predicate = vs.REGISTRY.get(claim.predicate_id)
        assert predicate is not None
        for e in cited:
            other = props[e.proposition_id or ""]
            if e.rule is Rule.FUNCTIONAL_KEY:
                assert predicate.uniqueness_key(other) == predicate.uniqueness_key(claim)
                assert not _related(claim.object_id, other.object_id, records)
            if e.rule is Rule.POLARITY and not _identical(other.negation(), claim):
                # #441: a negation of what contains the claim's object, or the containment.
                assert other.predicate_id == vs.LOCATED_IN or (
                    other.polarity.value == "negated"
                    and claim.polarity.value == "affirmed"
                    and _related(claim.object_id, other.object_id, records)
                ), (claim, records, verdict)
        fluent = predicate.temporal_kind is TemporalKind.FLUENT
        if fluent and not any(
            overlaps(r.proposition.valid_time, claim.valid_time) is not False
            for r in records
            if r.proposition.predicate_id == claim.predicate_id
        ):
            assert verdict.kind is not VerdictKind.CONTRADICTED

    run()
    for kind in VerdictKind:
        assert _SEEN[kind.value], (kind, _SEEN)
    for rule in (Rule.POLARITY, Rule.FUNCTIONAL_KEY, Rule.MAX_CARDINALITY, Rule.COVERAGE):
        assert _SEEN[rule.value], (rule, _SEEN)


def _covered(claim: Proposition, records: list[Record]) -> bool:
    """Whether the claim's time is covered under every admissible reading (the property's own
    statement, independent of the rule code)."""
    predicate = vs.REGISTRY.get(claim.predicate_id)
    assert predicate is not None
    same = [
        r.proposition
        for r in records
        if r.proposition.predicate_id == claim.predicate_id
        and r.proposition.subject_id == claim.subject_id
        and r.proposition.polarity is claim.polarity
        and set(r.proposition.object_set or (r.proposition.object_id,))
        >= set(claim.object_set or (claim.object_id,))
        and all(r.proposition.qualifiers.get(k) == v for k, v in claim.qualifiers.items())
    ]
    vt = claim.valid_time
    if isinstance(vt, Instant):
        granule = vt.definite()
        fine = any(
            isinstance(p.valid_time, Instant)
            and not finer(vt.granularity, p.valid_time.granularity)
            and granule[0] is not None
            and granule[1] is not None
            and granule[0] <= p.valid_time.at
            and p.valid_time.until <= granule[1]
            for p in same
        )
        spans = union(p.valid_time.definite() for p in same if isinstance(p.valid_time, Interval))
        return fine or not subtract(granule, spans)
    assert predicate.temporal_kind is not TemporalKind.EVENT  # the event predicate is instants
    return not subtract(vt.possible(), union(p.valid_time.definite() for p in same))


@pytest.mark.part("M4", "FR-PROP-1,10")
def test_support_coverage() -> None:
    """See the module docstring: ``supported`` only under full coverage in every admissible
    reading; no empty valid-time anywhere; the complement checks support and partial coverage
    both occurred."""
    _SEEN.clear()

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    @given(example=examples())
    @example(example=_PINNED[1])
    def run(example: tuple[Proposition, list[Record]]) -> None:
        claim, records = example
        verdict = _verdict(claim, records)
        assert _nonempty(claim.valid_time)
        assert all(_nonempty(r.proposition.valid_time) for r in records)
        assert all(_nonempty(c.valid_time) for c in verdict.remainder_claims)
        if verdict.kind is VerdictKind.SUPPORTED:
            assert verdict.rules == {Rule.COVERAGE}
            assert _covered(claim, records), (claim, records, verdict)
            assert _ids(verdict) <= {r.proposition_id for r in records}
        if verdict.reason is not None and verdict.reason.value == "partial_coverage":
            _SEEN["partial"] += 1
            assert verdict.covered
            assert verdict.uncovered
            assert len(verdict.remainder_claims) == len(verdict.uncovered)

    run()
    assert _SEEN[VerdictKind.SUPPORTED.value], _SEEN
    assert _SEEN["partial"], _SEEN


# -- #441: forward implication and closed sets over a key set ---------------------------------

ONLY = "01JC0000000000000000000009"


@st.composite
def exact_times(draw: Any) -> Interval:
    lo = draw(st.integers(0, STEPS - 1)) * STEP
    return Interval.of(lo, lo + draw(st.integers(1, 4)) * STEP)


@st.composite
def implied_examples(
    draw: Any,
) -> tuple[Proposition, list[Record], tuple[ClosedSetRecord, ...], bool]:
    """A varsity membership claim, the teams' schools (``part_of`` joins), Bobby's enrollments
    (either polarity), maybe a closed enrollment set, and whether the varsity rule is declared.
    Times are exact, so the property's own oracle can decide every overlap."""
    claim = vs.prop(vs.BOBBY, vs.MEMBER_OF, vs.TEAM, draw(exact_times()))
    joins = [
        vs.prop(vs.TEAM, vs.PART_OF, school, draw(exact_times()))
        for school in draw(st.lists(st.sampled_from(SCHOOLS), max_size=2, unique=True))
    ]
    enrollments = [
        vs.prop(
            vs.BOBBY,
            vs.ENROLLED_AT,
            draw(st.sampled_from(SCHOOLS)),
            draw(exact_times()),
            negated=draw(st.booleans()),
        )
        for _ in range(draw(st.integers(0, 3)))
    ]
    closed: tuple[ClosedSetRecord, ...] = ()
    if draw(st.booleans()):
        when = draw(exact_times()).definite()
        members = tuple(draw(st.lists(st.sampled_from(SCHOOLS), max_size=2, unique=True)))
        closed = (ClosedSetRecord(ONLY, vs.ENROLLED_AT, vs.BOBBY, members, {}, *when),)
    return claim, vs.records(*joins, *enrollments), closed, draw(st.booleans())


def _meets_exactly(a: Interval, b: Interval) -> bool:
    (a_lo, a_hi), (b_lo, b_hi) = a.definite(), b.definite()
    return max(a_lo or 0, b_lo or 0) < min(a_hi or HORIZON * 2, b_hi or HORIZON * 2)


def _implied_conflicts(
    claim: Proposition, records: list[Record], closed: tuple[ClosedSetRecord, ...]
) -> set[str]:
    """The property's oracle: the ids (join, then conflicting record or closed set) of each
    enrollment the rule derives that canon negates or a closed set leaves out, over time all
    three share."""
    found: set[str] = set()
    for j in records:
        p = j.proposition
        if p.predicate_id != vs.PART_OF or p.object_id is None:
            continue
        assert isinstance(p.valid_time, Interval)
        assert isinstance(claim.valid_time, Interval)
        if not _meets_exactly(claim.valid_time, p.valid_time):
            continue
        lo = max(claim.valid_time.definite()[0] or 0, p.valid_time.definite()[0] or 0)
        hi = min(claim.valid_time.definite()[1] or 0, p.valid_time.definite()[1] or 0)
        derived = Interval.of(lo, hi)
        for r in records:
            q = r.proposition
            if (
                q.predicate_id == vs.ENROLLED_AT
                and q.object_id == p.object_id
                and q.polarity.value == "negated"
                and isinstance(q.valid_time, Interval)
                and _meets_exactly(derived, q.valid_time)
            ):
                found |= {j.proposition_id, r.proposition_id}
        for c in closed:
            if p.object_id not in c.members and _meets_exactly(derived, c.interval()):
                found |= {j.proposition_id, c.id}
    return found


@pytest.mark.part("M4", "FR-PROP-3,8,10")
def test_contradiction_soundness__implied() -> None:
    """Forward implication (#441) is sound and complete against the property's own oracle:
    without the declared rule a membership is never ``contradicted`` (team membership
    establishes nothing about enrollment); with it, the claim is ``contradicted`` exactly when a
    derived enrollment is negated by canon or left out of a closed set over shared time, and
    the verdict cites the rule (a join) and only records or sets the oracle names. Complement:
    both outcomes occur."""
    seen: Counter[str] = Counter()

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    @given(example=implied_examples())
    def run(example: tuple[Proposition, list[Record], tuple[ClosedSetRecord, ...], bool]) -> None:
        claim, records, closed, ruled = example
        reg = vs.registry_of(vs.VARSITY_RULE) if ruled else vs.REGISTRY
        verdict = check(claim, records, registry=reg, closed_sets=closed)
        expected = _implied_conflicts(claim, records, closed) if ruled else set()
        if not expected:
            assert verdict.kind is not VerdictKind.CONTRADICTED, (example, verdict)
            return
        seen["contradicted"] += 1
        assert verdict.kind is VerdictKind.CONTRADICTED, (example, verdict)
        assert Rule.IMPLICATION in verdict.rules
        assert verdict.rules - {Rule.IMPLICATION} <= {Rule.POLARITY, Rule.CLOSED_SET}
        assert set(verdict.cited()) <= expected, (example, verdict)

    run()
    assert seen["contradicted"], seen


@st.composite
def key_set_examples(draw: Any) -> tuple[Proposition, ClosedSetRecord]:
    """A degree claim (its degree id, and its level or none) and a closed set over the key set
    ``degree_id`` (some of the ids, narrowed to bachelor's degrees or not, over some time)."""
    level = draw(st.sampled_from(("bachelor", "master", None)))
    qualifiers = {"degree_id": draw(st.sampled_from(("a", "b", "c")))}
    if level is not None:
        qualifiers["level"] = level
    claim = vs.prop(
        vs.BOBBY,
        vs.DEGREE,
        object_set=(draw(st.sampled_from(SCHOOLS)),),
        when=draw(valid_times(instant_only=True)),
        qualifiers=qualifiers,
    )
    when = draw(exact_times()).definite()
    closed = ClosedSetRecord(
        ONLY,
        vs.DEGREE,
        vs.BOBBY,
        tuple(draw(st.lists(st.sampled_from(("a", "b", "c")), min_size=1, unique=True))),
        draw(st.sampled_from(({}, {"level": "bachelor"}))),
        valid_from=when[0],
        valid_to=when[1],
        key_set="degree_id",
    )
    return claim, closed


@pytest.mark.part("M4", "FR-PROP-8,10")
def test_contradiction_soundness__key_set() -> None:
    """A closed set over a key set (#441) contradicts a claim exactly when the claim names every
    qualifier the set is narrowed by, with its values, names a degree id the set does not list,
    and its instant lies in the set's interval; the verdict cites the set. Complement: both
    outcomes occur."""
    seen: Counter[str] = Counter()

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(example=key_set_examples())
    def run(example: tuple[Proposition, ClosedSetRecord]) -> None:
        claim, closed = example
        verdict = check(claim, [], registry=vs.REGISTRY, closed_sets=(closed,))
        lo, hi = claim.valid_time.definite()
        assert lo is not None
        assert hi is not None
        inside = overlaps(claim.valid_time, closed.interval())
        outside = (
            all(claim.qualifiers.get(k) == v for k, v in closed.key.items())
            and claim.qualifiers["degree_id"] not in closed.members
        )
        if outside and inside is True:
            seen["contradicted"] += 1
            assert verdict.kind is VerdictKind.CONTRADICTED, (example, verdict)
            assert verdict.cited() == (closed.id,)
        elif not outside or inside is False:
            seen["open"] += 1
            assert verdict.kind is not VerdictKind.CONTRADICTED, (example, verdict)

    run()
    assert seen["contradicted"], seen
    assert seen["open"], seen
