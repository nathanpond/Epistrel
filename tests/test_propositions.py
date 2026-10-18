"""Unit tests of the proposition model, the core vocabulary and the registry's pure helpers
(#131): valid-time logic, typed literals, predicate semantics, canonical direction, implication
closure, the typed story rules and the stored-row codec. The milestone parts that cover the
requirements are U-15's slices (``tests/test_u_15.py``) and S-PREDICATE-LAYERS
(``tests/scenario/test_s_predicate_layers.py``)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import Range

from epistrel.core.errors import InvalidRequest
from epistrel.core.propositions import codec, registry, vocabulary
from epistrel.core.propositions.model import (
    DEFAULT_EPOCH,
    Bound,
    Cardinality,
    Granularity,
    Implication,
    Instant,
    Interval,
    InvalidValidTime,
    Layer,
    LiteralKind,
    Polarity,
    Predicate,
    Proposition,
    QualifierKind,
    TemporalKind,
    TypedLiteral,
    coarsely_compatible,
    covers,
    exactly_equal,
    granule,
    overlaps,
    refines,
    story_minute,
)
from epistrel.tools import predicates as predicates_doc

A = "01JA0000000000000000000001"
B = "01JA0000000000000000000002"
C = "01JA0000000000000000000003"
D = "01JA0000000000000000000004"
P = "01JB0000000000000000000001"
Q = "01JB0000000000000000000002"
R = "01JB0000000000000000000003"
DAY = 24 * 60


def at(moment: str) -> int:
    return story_minute(datetime.fromisoformat(moment).replace(tzinfo=UTC))


def prop(
    predicate: str = P,
    subject: str = A,
    obj: str | None = B,
    valid: Interval | Instant | None = None,
    **fields: Any,
) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=valid if valid is not None else Interval.of(0, 100),
        **fields,
    )


# -- granules and story-time ---------------------------------------------------------------------


def test_granules_follow_the_calendar() -> None:
    t = at("2012-12-31T17:45")
    assert granule(t, Granularity.MINUTE) == (t, t + 1)
    assert granule(t, Granularity.HOUR) == (at("2012-12-31T17:00"), at("2012-12-31T18:00"))
    assert granule(t, Granularity.DAY) == (at("2012-12-31T00:00"), at("2013-01-01T00:00"))
    assert granule(t, Granularity.MONTH) == (at("2012-12-01T00:00"), at("2013-01-01T00:00"))
    assert granule(t, Granularity.YEAR) == (at("2012-01-01T00:00"), at("2013-01-01T00:00"))
    assert granule(at("2012-02-10T00:00"), Granularity.MONTH)[1] == at("2012-03-01T00:00")
    # Another epoch moves the calendar under the same minutes.
    epoch = datetime(2020, 6, 15, 12, 30, tzinfo=UTC)
    assert granule(0, Granularity.DAY, epoch) == (-(12 * 60 + 30), DAY - (12 * 60 + 30))
    assert story_minute(DEFAULT_EPOCH) == 0


# -- valid-time construction ---------------------------------------------------------------------


def test_an_empty_or_inverted_interval_is_refused() -> None:
    for start, end in ((5, 5), (6, 5)):
        with pytest.raises(InvalidValidTime) as refused:
            Interval.of(start, end)
        assert refused.value.code == "validation_error"
        assert refused.value.details["reason"] == "invalid_valid_time"
    with pytest.raises(InvalidValidTime):
        Interval(start=Bound(inner=10, outer=20))  # outer start after its inner
    with pytest.raises(InvalidValidTime):
        Interval(end=Bound(inner=20, outer=10))  # outer end before its inner
    with pytest.raises(InvalidValidTime):
        Interval(start=Bound(inner=20, outer=0), end=Bound(inner=20, outer=40))
    with pytest.raises(InvalidValidTime):
        Instant(at=5, granularity=Granularity.DAY, until=5)
    # Complement: a one-minute interval and open-ended ones are valid.
    assert Interval.of(5, 6).exact
    assert Interval.of(None, 6).definite() == (None, 6)
    assert Interval.of(5, None).possible() == (5, None)
    assert not Interval(start=Bound(inner=10, outer=0)).exact


def test_an_instant_is_its_granule() -> None:
    instant = Instant.of(at("2012-05-10T09:42"), Granularity.HOUR)
    assert (instant.at, instant.until) == (at("2012-05-10T09:00"), at("2012-05-10T10:00"))
    assert instant.definite() == instant.possible()


# -- the relations under every admissible reading ------------------------------------------------


def test_covers() -> None:
    canon = Interval.of(2010, 2014)
    assert covers(canon, Interval.of(2011, 2013)) is True
    assert covers(canon, canon) is True
    # Complement: overlap without coverage is not coverage.
    assert overlaps(canon, Interval.of(2000, 2020)) is True
    assert covers(canon, Interval.of(2000, 2020)) is False
    assert covers(canon, Interval.of(2015, 2016)) is False
    # Approximate bounds: a claim that might reach past canon's inner bound is undecidable.
    approximate = Interval(start=Bound(inner=2010, outer=2008), end=Bound(inner=2014, outer=2016))
    assert covers(approximate, Interval.of(2011, 2013)) is True
    assert covers(approximate, Interval.of(2009, 2013)) is None
    assert covers(approximate, Interval.of(2000, 2013)) is False
    claim = Interval(start=Bound(inner=2011, outer=2011), end=Bound(inner=2013, outer=2015))
    assert covers(canon, claim) is None
    # Open-ended.
    assert covers(Interval.of(None, None), canon) is True
    assert covers(canon, Interval.of(None, 2012)) is False


def test_an_instant_is_covered_by_its_whole_granule() -> None:
    canon = Interval.of(100, 200)
    assert covers(canon, Instant.of(100, Granularity.MINUTE)) is True
    assert covers(canon, Instant.of(199, Granularity.MINUTE)) is True
    assert covers(canon, Instant.of(200, Granularity.MINUTE)) is False  # t = end
    day = Instant.of(at("2012-05-10T12:00"), Granularity.DAY)
    assert covers(Interval.of(at("2012-05-10T08:00"), at("2012-05-11T08:00")), day) is False


def test_overlaps() -> None:
    assert overlaps(Interval.of(0, 10), Interval.of(5, 15)) is True
    assert overlaps(Interval.of(0, 10), Interval.of(10, 15)) is False  # adjacent
    approximate = Interval(start=Bound(inner=0, outer=0), end=Bound(inner=10, outer=20))
    assert overlaps(approximate, Interval.of(15, 30)) is None
    assert overlaps(approximate, Interval.of(25, 30)) is False
    assert overlaps(Interval.of(None, None), Interval.of(5, 6)) is True
    assert overlaps(Interval.of(None, 3), Interval.of(5, None)) is False


def test_exact_equality() -> None:
    hour = Instant.of(at("2012-05-10T09:00"), Granularity.HOUR)
    assert exactly_equal(hour, Instant.of(at("2012-05-10T09:30"), Granularity.HOUR)) is True
    assert exactly_equal(hour, Instant.of(at("2012-05-10T09:00"), Granularity.DAY)) is False
    assert exactly_equal(Interval.of(0, 10), Interval.of(0, 10)) is True
    assert exactly_equal(Interval.of(0, 10), Interval.of(0, 11)) is False
    assert exactly_equal(Interval.of(0, None), Interval.of(0, 10)) is False
    loose = Interval(start=Bound(inner=5, outer=0), end=Bound.at(10))
    assert exactly_equal(loose, Interval.of(3, 10)) is None
    assert exactly_equal(loose, Interval.of(7, 10)) is False
    assert exactly_equal(hour, Interval.of(hour.at, hour.until)) is False


def test_coarse_compatibility_and_refinement() -> None:
    may10 = Instant.of(at("2012-05-10T00:00"), Granularity.DAY)
    nine = Instant.of(at("2012-05-10T09:00"), Granularity.HOUR)
    five = Instant.of(at("2012-05-10T17:00"), Granularity.HOUR)
    may11 = Instant.of(at("2012-05-11T00:00"), Granularity.DAY)
    assert coarsely_compatible(may10, nine)
    assert coarsely_compatible(nine, may10)
    assert coarsely_compatible(may10, five)
    assert not coarsely_compatible(nine, five)  # each compatible with the day, not each other
    assert not coarsely_compatible(may11, nine)
    assert coarsely_compatible(may10, may10)
    assert coarsely_compatible(Interval.of(0, 10), Interval.of(5, 20))
    assert not coarsely_compatible(Interval.of(0, 10), Interval.of(10, 20))
    assert refines(nine, may10)
    assert not refines(may10, nine)
    assert not refines(nine, five)
    assert not refines(nine, Interval.of(may10.at, may10.until))


# -- literals and propositions --------------------------------------------------------------------


def test_typed_literals_compare_by_kind() -> None:
    forty = TypedLiteral(kind=LiteralKind.INT, value=40)
    assert forty.equals(TypedLiteral(kind=LiteralKind.FLOAT, value=40.0))
    assert not forty.equals(TypedLiteral(kind=LiteralKind.STR, value="40"))
    assert not forty.equals(TypedLiteral(kind=LiteralKind.DURATION, value=40))
    km = TypedLiteral(kind=LiteralKind.QUANTITY, value=5, unit="km")
    assert km.equals(TypedLiteral(kind=LiteralKind.QUANTITY, value=5, unit="km"))
    assert not km.equals(TypedLiteral(kind=LiteralKind.QUANTITY, value=5, unit="mi"))
    assert TypedLiteral(kind=LiteralKind.DATE, value="2012-05-10").value == "2012-05-10"
    for kind, value, unit in (
        (LiteralKind.INT, True, None),
        (LiteralKind.INT, 4.5, None),
        (LiteralKind.FLOAT, float("inf"), None),
        (LiteralKind.DATE, "May 10", None),
        (LiteralKind.BOOL, 1, None),
        (LiteralKind.QUANTITY, 5, None),
        (LiteralKind.STR, "x", "km"),
    ):
        with pytest.raises(ValidationError):
            TypedLiteral(kind=kind, value=value, unit=unit)


def test_a_proposition_has_one_object() -> None:
    with pytest.raises(ValidationError):
        prop(literal=TypedLiteral(kind=LiteralKind.INT, value=1))
    with pytest.raises(ValidationError):
        prop(obj=None, object_set=())
    joint = prop(obj=None, object_set=(C, B, C))
    assert joint.object_set == (B, C)
    assert joint.object_key() == ("set", (B, C))
    assert prop(obj=None).object_key() == ("none", None)
    numeric = prop(obj=None, literal=TypedLiteral(kind=LiteralKind.INT, value=40))
    same = prop(obj=None, literal=TypedLiteral(kind=LiteralKind.FLOAT, value=40.0))
    text = prop(obj=None, literal=TypedLiteral(kind=LiteralKind.STR, value="40"))
    assert numeric.normalized_key() == same.normalized_key() != text.normalized_key()
    negated = prop().negation()
    assert negated.polarity is Polarity.NEGATED
    assert negated.negation().polarity is Polarity.AFFIRMED
    assert negated.normalized_key() == prop().normalized_key()
    qualified = prop(qualifiers={"role": "captain", "ids": (A, B)})
    assert qualified.qualifier_key() == (("ids", (A, B)), ("role", "captain"))


# -- predicate semantics --------------------------------------------------------------------------


def predicate(**fields: Any) -> Predicate:
    return Predicate(**{"id": P, "story_id": D, "name": "p", "layer": Layer.AUTHOR, **fields})


def test_predicate_semantics_are_checked() -> None:
    assert predicate(cardinality=Cardinality.FUNCTIONAL).max_cardinality == 1
    bad: list[dict[str, Any]] = [
        {"cardinality": Cardinality.FUNCTIONAL, "max_cardinality": 2},
        {"subject_type": "spaceship"},
        {"key_qualifiers": ("degree_id",)},
        {"symmetric": True, "inverse_of": Q},
        {"implications": (Implication(when=Q, then=R),)},
    ]
    for fields in bad:
        with pytest.raises(ValidationError):
            predicate(**fields)
    assert predicate(implications=(Implication(when=P, then=R),)).implications[0].then == R


def test_weak_semantics() -> None:
    auto = predicate(layer=Layer.AUTO, qualifiers=None)
    assert auto.weak
    assert not registry.cardinality_may_contradict(auto)
    for strong in (
        {"cardinality": Cardinality.FUNCTIONAL},
        {"max_cardinality": 2},
        {"exclusions": (Q,)},
        {"containment_of": Q},
        {"implications": (Implication(when=P, then=Q),)},
    ):
        assert not predicate(**strong).weak, strong
    assert registry.cardinality_may_contradict(predicate(cardinality=Cardinality.FUNCTIONAL))
    assert registry.cardinality_may_contradict(predicate(max_cardinality=3))
    assert not registry.cardinality_may_contradict(predicate(exclusions=(Q,)))


def test_uniqueness_keys_and_qualifiers() -> None:
    degree = vocabulary.CORE_BY_NAME["degree_awarded"]
    a = prop(degree.id, obj=None, object_set=(B,), qualifiers={"degree_id": "ba"})
    b = prop(degree.id, obj=None, object_set=(C,), qualifiers={"degree_id": "ms"})
    a2 = prop(degree.id, obj=None, object_set=(C,), qualifiers={"degree_id": "ba", "field": "law"})
    assert degree.uniqueness_key(a) != degree.uniqueness_key(b)
    assert degree.uniqueness_key(a) == degree.uniqueness_key(a2)  # field is not in the key
    assert degree.qualifier_errors(a2) == []
    assert degree.qualifier_errors(prop(degree.id, qualifiers={"degree_id": 3})) == [
        "'degree_id' must be str"
    ]
    assert degree.qualifier_errors(prop(degree.id, qualifiers={"mood": "x"})) == [
        "'mood' is not a qualifier of degree_awarded"
    ]
    scheduled = vocabulary.CORE_BY_NAME["scheduled"]
    good = prop(scheduled.id, qualifiers={"participants": (A, B), "place": C})
    assert scheduled.qualifier_errors(good) == []
    wrong = prop(scheduled.id, qualifiers={"participants": "Ann", "place": [C]})
    assert len(scheduled.qualifier_errors(wrong)) == 2
    kinds = predicate(
        qualifiers={
            "n": QualifierKind.INT,
            "x": QualifierKind.FLOAT,
            "ok": QualifierKind.BOOL,
            "on": QualifierKind.DATE,
        }
    )
    assert (
        kinds.qualifier_errors(prop(qualifiers={"n": 1, "x": 2, "ok": True, "on": "2012-01-02"}))
        == []
    )
    assert kinds.qualifier_errors(prop(qualifiers={"n": 1, "x": 2.5, "ok": True})) == []
    assert len(kinds.qualifier_errors(prop(qualifiers={"n": True, "ok": 1, "on": "x"}))) == 3
    assert (
        predicate(layer=Layer.AUTO, qualifiers=None).qualifier_errors(
            prop(qualifiers={"anything": "goes"})
        )
        == []
    )


# -- the core vocabulary -------------------------------------------------------------------------


def test_the_core_vocabulary() -> None:
    core = vocabulary.CORE_PREDICATES
    assert len({p.id for p in core}) == len({p.name for p in core}) == len(core)
    assert all(p.layer is Layer.CORE and p.story_id is None and p.docs for p in core)
    families = {vocabulary.family_of(p.id) for p in core}
    assert families == set(vocabulary.FAMILIES)
    names = vocabulary.CORE_NAMES
    for required in (
        "same_as",
        "located_at",
        "present_at",
        "holds",
        "owns",
        "alive",
        "dead",
        "member_of",
        "enrolled_at",
        "attended_program",
        "degree_awarded",
        "employed_by",
        "age",
        "name",
        "scheduled",
    ):
        assert required in names
    by = vocabulary.CORE_BY_NAME
    assert by["dead"].exclusions == (by["alive"].id,)
    assert by["same_as"].symmetric
    assert by["held_by"].inverse_of == by["holds"].id
    assert by["held_by"].cardinality is Cardinality.FUNCTIONAL
    assert by["located_at"].containment_of == by["located_in"].id
    member = by["member_of"]
    assert member.cardinality is Cardinality.MULTI
    assert member.temporal_kind is TemporalKind.FLUENT
    assert member.qualifiers == {"role": QualifierKind.STR}
    assert member.key_qualifiers == ()
    education = {by[n].id for n in ("member_of", "enrolled_at", "attended_program")} | {
        by["degree_awarded"].id
    }
    assert education == set(vocabulary.EDUCATION_PREDICATES)
    assert all(not vocabulary.CORE_BY_ID[i].implications for i in education)
    # Every id a predicate names is a core predicate.
    for p in core:
        for ref in (*p.exclusions, p.containment_of, p.inverse_of):
            assert ref is None or ref in vocabulary.CORE_BY_ID


def test_the_vocabulary_document_is_rendered_from_the_code() -> None:
    """``docs/predicates.md`` is exactly what the vocabulary renders (CI's ``--check``), with a
    table per family and every predicate's documentation."""
    rendered = predicates_doc.render()
    assert predicates_doc.DOC_PATH.read_text(encoding="utf-8") == rendered
    assert predicates_doc.main(["--check"]) == 0
    for family in vocabulary.FAMILIES:
        assert f"## {family.title}" in rendered
    for p in vocabulary.CORE_PREDICATES:
        assert f"`{p.name}`<br>`{p.id}`" in rendered
        assert p.docs.replace("|", "\\|") in rendered
    assert "functional per (subject, degree_id)" in rendered
    assert "inverse of `holds`" in rendered
    assert "excludes `alive`" in rendered


def test_a_stale_vocabulary_document_fails_the_check(tmp_path: Any) -> None:
    stale = tmp_path / "predicates.md"
    stale.write_text("# old\n", encoding="utf-8")
    assert predicates_doc.main(["--check", "--path", str(stale)]) == 1
    assert predicates_doc.main(["--write", "--path", str(stale)]) == 0
    assert predicates_doc.main(["--check", "--path", str(stale)]) == 0


# -- the registry's pure helpers -------------------------------------------------------------------


def _registry(*story: Predicate, rules: tuple[Implication, ...] = ()) -> registry.Registry:
    found = dict(vocabulary.CORE_BY_ID)
    found.update({p.id: p for p in story})
    return registry.Registry(D, 1, found, rules)


def test_canonical_direction() -> None:
    by = vocabulary.CORE_BY_NAME
    reg = _registry()
    friends = prop(by["friend_of"].id, subject=B, obj=A)
    assert (
        registry.canonical(friends, reg).subject_id,
        registry.canonical(friends, reg).object_id,
    ) == (A, B)
    already = prop(by["friend_of"].id, subject=A, obj=B)
    assert registry.canonical(already, reg) == already
    held = registry.canonical(prop(by["held_by"].id, subject=C, obj=A), reg)
    assert (held.predicate_id, held.subject_id, held.object_id) == (by["holds"].id, A, C)
    child = registry.canonical(prop(by["child_of"].id, subject=B, obj=A), reg)
    assert (child.predicate_id, child.subject_id, child.object_id) == (by["parent_of"].id, A, B)
    # Not symmetric, unregistered, or object-less: unchanged.
    knows = prop(by["knows"].id, subject=B, obj=A)
    assert registry.canonical(knows, reg) == knows
    assert registry.canonical(prop(P, subject=B, obj=A), reg).subject_id == B
    alive = prop(by["alive"].id, obj=None)
    assert registry.canonical(alive, reg) == alive


def test_direct_polarity_conflict() -> None:
    affirmed = prop(valid=Interval.of(0, 10))
    assert registry.direct_polarity_conflict(affirmed, affirmed.negation()) is True
    later = prop(valid=Interval.of(10, 20)).negation()
    assert registry.direct_polarity_conflict(affirmed, later) is False
    loose = prop(valid=Interval(start=Bound(inner=5, outer=0), end=Bound(inner=8, outer=25)))
    assert registry.direct_polarity_conflict(loose, later) is None
    # Complement: a different object, or the same polarity, is no polarity conflict.
    assert registry.direct_polarity_conflict(affirmed, prop(obj=C).negation()) is False
    assert registry.direct_polarity_conflict(affirmed, affirmed) is False


def test_event_identity_is_never_time_alone() -> None:
    by = vocabulary.CORE_BY_NAME
    knock = predicate(id=Q, temporal_kind=TemporalKind.EVENT)
    nine = prop(Q, obj=None, valid=Instant.of(at("2012-05-10T09:00"), Granularity.HOUR))
    nine_again = prop(Q, obj=None, valid=Instant.of(at("2012-05-10T09:00"), Granularity.HOUR))
    assert not registry.same_event(nine, nine_again, knock)
    e1 = "01JC0000000000000000000001"
    assert registry.same_event(
        nine.model_copy(update={"event_id": e1}),
        nine_again.model_copy(update={"event_id": e1}),
        None,
    )
    born = by["born"]
    birth = prop(born.id, obj=None, valid=Instant.of(at("2000-01-01T09:00"), Granularity.HOUR))
    other = prop(born.id, obj=None, valid=Instant.of(at("2000-01-01T17:00"), Granularity.HOUR))
    assert registry.same_event(birth, other, born)  # a person's birth: one event, by its key
    assert not registry.same_event(birth, prop(born.id, subject=B, obj=None), born)
    assert not registry.same_event(birth, nine, born)


def test_implication_closure() -> None:
    by = vocabulary.CORE_BY_NAME
    member, enrolled, part_of = by["member_of"].id, by["enrolled_at"].id, Q
    team, school = C, D
    facts = [
        prop(member, subject=A, obj=team, valid=Interval.of(10, 50)),
        prop(part_of, subject=team, obj=school, valid=Interval.of(0, 40)),
    ]
    rule = Implication(when=member, then=enrolled, via=part_of)
    closure = registry.imply(facts, [rule])
    assert not closure.truncated
    assert closure.depth == 1
    (derived,) = closure.derived
    assert (derived.predicate_id, derived.subject_id, derived.object_id) == (enrolled, A, school)
    assert derived.valid_time == Interval.of(10, 40)  # the intersection of the antecedents
    # Complement: declared rules only; without the rule, nothing follows.
    assert registry.imply(facts, []).derived == ()
    # A negated antecedent implies nothing; disjoint valid-times join to nothing.
    assert registry.imply([facts[0].negation(), facts[1]], [rule]).derived == ()
    apart = [facts[0], prop(part_of, subject=team, obj=school, valid=Interval.of(60, 70))]
    assert registry.imply(apart, [rule]).derived == ()
    # A chain of four rules stops at the depth, and says so.
    chain = [P, Q, R, by["knows"].id, by["owns"].id]
    rules = [Implication(when=a, then=b) for a, b in itertools.pairwise(chain)]
    start = [prop(P)]
    full = registry.imply(start, rules, max_depth=4)
    assert [p.predicate_id for p in full.derived] == chain[1:]
    assert not full.truncated
    capped = registry.imply(start, rules)  # default depth 3
    assert registry.IMPLICATION_MAX_DEPTH == 3
    assert [p.predicate_id for p in capped.derived] == chain[1:4]
    assert capped.truncated
    assert capped.depth == 3
    assert registry.imply(start, rules, max_depth=0).derived == ()


def test_valid_time_intersection() -> None:
    inter = registry.intersection
    assert inter(Interval.of(0, 10), Interval.of(5, 20)) == Interval.of(5, 10)
    assert inter(Interval.of(0, 10), Interval.of(10, 20)) is None
    assert inter(Interval.of(None, 10), Interval.of(5, None)) == Interval.of(5, 10)
    instant = Instant.of(5, Granularity.MINUTE)
    assert inter(instant, Interval.of(0, 10)) == instant
    assert inter(Interval.of(0, 10), instant) == instant
    assert inter(instant, Interval.of(6, 10)) is None
    assert inter(instant, instant) == instant
    assert inter(instant, Instant.of(6, Granularity.MINUTE)) is None
    loose = Interval(start=Bound(inner=5, outer=0), end=Bound(inner=10, outer=12))
    assert inter(loose, Interval.of(9, 30)) == Interval(
        start=Bound(inner=9, outer=9), end=Bound(inner=10, outer=12)
    )


def test_rule_cycles_are_refused() -> None:
    registry.check_rules([Implication(when=P, then=Q), Implication(when=Q, then=R)])
    with pytest.raises(InvalidRequest) as refused:
        registry.check_rules(
            [Implication(when=P, then=Q), Implication(when=Q, then=R), Implication(when=R, then=P)]
        )
    assert refused.value.details["reason"] == "rule_cycle"


def test_scenario_rules_parse_the_loose_lists() -> None:
    rules = registry.ScenarioRules.parse(
        {
            "predicates": [{"name": "apprenticed_with", "cardinality": "functional"}],
            "closed_sets": [{"predicate": "enrolled_at", "subject": "bobby", "members": ["osu"]}],
            "rules": [{"when": "member_of", "then": "enrolled_at", "via": "part_of"}],
            "states": [{"ignored": True}],
        }
    )
    assert rules.predicates[0].cardinality is Cardinality.FUNCTIONAL
    assert rules.closed_sets[0].members == ("osu",)
    assert rules.rules[0].via == "part_of"
    assert not rules.empty
    assert registry.ScenarioRules.parse({}).empty
    for document, field_ref, reason in (
        (
            {"predicates": [{"name": "member_of"}]},
            "starting_state.predicates[0].name",
            "core_predicate_name",
        ),
        (
            {"predicates": [{"name": "x"}, {"name": "x"}]},
            "starting_state.predicates[1].name",
            "predicate_declared_twice",
        ),
        (
            {"predicates": [{"name": "x", "cardinality": "some"}]},
            "starting_state.predicates[0].cardinality",
            None,
        ),
        (
            {"closed_sets": [{"predicate": "x", "subject": "s", "members": []}]},
            "starting_state.closed_sets[0].members",
            None,
        ),
        ({"rules": [{"when": "a"}]}, "starting_state.rules[0].then", None),
        ({"predicates": [{"name": "9lives"}]}, "starting_state.predicates[0].name", None),
    ):
        with pytest.raises(InvalidRequest) as refused:
            registry.ScenarioRules.parse(document)
        assert refused.value.details["field"] == field_ref, document
        if reason is not None:
            assert refused.value.details["reason"] == reason
    assert registry.rules_from_snapshot(None).empty
    assert registry.rules_from_snapshot(
        {"starting_state": {"rules": [{"when": "a", "then": "b"}]}}
    ).rules


def test_auto_names() -> None:
    assert registry.auto_name("Apprenticed  with") == "apprenticed_with"
    assert registry.auto_name("was apprenticed-to!") == "was_apprenticed_to"
    assert registry.auto_name("Straße") == "strasse"
    for nothing in ("  ", "!!", "42 things"):
        with pytest.raises(InvalidRequest):
            registry.auto_name(nothing)


def test_strengthening_and_proposed_changes() -> None:
    auto = predicate(layer=Layer.AUTO, qualifiers=None)
    functional = registry.strengthened(auto, {"cardinality": "functional"})
    assert functional.max_cardinality == 1
    assert not functional.weak
    keyed = registry.strengthened(
        auto, {"qualifiers": {"rank": "str"}, "key_qualifiers": ["rank"], "max_cardinality": 2}
    )
    assert keyed.key_qualifiers == ("rank",)
    assert keyed.max_cardinality == 2
    with pytest.raises(InvalidRequest):
        registry.strengthened(auto, {"key_qualifiers": ["rank"]})  # an undeclared key
    assert registry.ProposedChanges(symmetric=True).fields() == {"symmetric": True}
    assert registry.ProposedChanges().fields() == {}
    with pytest.raises(ValidationError):
        registry.ProposedChanges.model_validate({"layer": "core"})
    assert "cardinality" in registry.PROPOSABLE
    assert "name" not in registry.PROPOSABLE
    assert registry.setting_of(P) == f"predicate:{P}"
    core = registry.core_registry()
    assert core.named("member_of") is vocabulary.CORE_BY_NAME["member_of"]
    assert core.named("nothing") is None
    assert core.rules() == ()
    mine = predicate(name="member_of")
    assert _registry(mine).named("member_of") is mine  # the story's own name first


# -- the stored shape ------------------------------------------------------------------------------


def test_predicate_rows_round_trip() -> None:
    for p in (*vocabulary.CORE_PREDICATES, predicate(layer=Layer.AUTO, qualifiers=None)):
        row = codec.predicate_row(p)
        assert set(codec.SEMANTIC_COLUMNS) <= set(row)
        assert codec.predicate_from_row(row) == p
    with_rule = predicate(implications=(Implication(when=P, then=Q),))
    assert codec.predicate_row(with_rule)["implications"] == [{"when": P, "then": Q}]


def test_proposition_rows_round_trip() -> None:
    cases = [
        prop(valid=Interval.of(5, 10)),
        prop(valid=Interval(start=Bound(inner=5, outer=0), end=None)),
        prop(valid=Interval.of(None, 10), polarity=Polarity.NEGATED),
        prop(obj=None, object_set=(B, C), qualifiers={"degree_id": "ba", "ids": (A,)}),
        prop(obj=None, literal=TypedLiteral(kind=LiteralKind.INT, value=40)),
        prop(valid=Instant.of(at("2012-05-10T09:00"), Granularity.DAY), event_id=C),
    ]
    for case in cases:
        row = codec.proposition_row(case)
        span = row["valid_span"]
        assert isinstance(span, Range)
        assert not span.isempty
        row["valid_span"] = Range(span.lower, span.upper, bounds="[)")
        assert codec.proposition_from_row(row) == case
    instant = codec.proposition_row(cases[-1])
    assert instant["instant_granularity"] == "day"
    assert instant["valid_to_inner"] is None
    approximate = codec.proposition_row(cases[1])
    assert (approximate["from_exact"], approximate["to_exact"]) == (False, True)
    assert codec.proposition_row(cases[0])["valid_span"] == Range(5, 10, bounds="[)")
    # One bound given on one side only reads back as an exact bound.
    half = {**codec.proposition_row(cases[0]), "valid_from_outer": None}
    assert codec.proposition_from_row(half).valid_time == Interval.of(5, 10)
