"""The pure half of #191 (``core.states.verify``; FR-STATE-8, FR-VERIFY-7): a ``state_value``
claim checked against its owner's value, never against records; a declared rule's consequence
on it; and a stated balance detected as one. No database, no model."""

from __future__ import annotations

from decimal import Decimal

from epistrel.core.enums import StateScope, StateType, StateVisibility
from epistrel.core.propositions.model import (
    Implication,
    Interval,
    LiteralKind,
    Polarity,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.registry import owner_lookup
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, STATE_VALUE
from epistrel.core.resolution.entities import (
    NARRATOR,
    EntityEntry,
    EntityIndex,
    ResolutionContext,
)
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.states import verify as sv
from epistrel.core.verify.detect import DetectBeat, DetectionEnv, detect_render
from epistrel.core.verify.rules import (
    Holder,
    Options,
    OwnedValue,
    Record,
    Rule,
    VerdictKind,
    VerdictReason,
    check,
)
from tests import verify_support as vs

CAL = "01JS0000000000000000000001"
MIRA = "01JS0000000000000000000002"
GOLD = "01JS0000000000000000000003"
COINS = "01JS0000000000000000000004"
MOOD = "01JS0000000000000000000005"
OPEN = "01JS0000000000000000000006"
NOTE = "01JS0000000000000000000007"
HOLDS = CORE_BY_NAME["holds"].id


def owned(
    state: str,
    subject: str,
    value: TypedLiteral | None,
    kind: StateType = StateType.NUMBER,
    *,
    tolerance: float = 0.0,
    entity: str | None = None,
) -> OwnedValue:
    return OwnedValue(
        sv.proposition(state, subject, value), f"state:{state}", kind.value, tolerance, entity
    )


def n(value: int | float) -> TypedLiteral:
    return sv.number_literal(Decimal(str(value)))


def s(value: str) -> TypedLiteral:
    return TypedLiteral(kind=LiteralKind.STR, value=value)


def b(value: bool) -> TypedLiteral:
    return TypedLiteral(kind=LiteralKind.BOOL, value=value)


def test_state_value_is_an_owner_lookup_predicate() -> None:
    predicate = CORE_BY_NAME["state_value"]
    assert predicate.id == STATE_VALUE == "01JPRED0000000000000000035"
    assert (predicate.cardinality.value, predicate.temporal_kind.value) == ("functional", "fluent")
    assert predicate.key_qualifiers == ("state",)
    assert owner_lookup(STATE_VALUE) is sv.STATE_LOOKUP
    assert owner_lookup(HOLDS) is None


def test_comparator() -> None:
    """Numbers within the tolerance, enum and boolean exactly, text case-insensitively; no
    value, or another type, is uncertain; no value the viewpoint reads is novel."""
    gold = [owned(GOLD, CAL, n(300), tolerance=5)]
    for claimed, kind in [(300, "supported"), (304, "supported"), (306, "contradicted")]:
        found = sv.verdict(sv.proposition(GOLD, CAL, n(claimed)), gold)
        assert found.kind.value == kind, claimed
    found = sv.verdict(sv.proposition(GOLD, CAL, n(500)), gold)
    assert [(e.rule, e.owner_ref, e.fact_id) for e in found.evidence] == [
        (Rule.FUNCTIONAL_KEY, f"state:{GOLD}", None)
    ]
    assert found.evidence[0].table == "state_values"
    quantity = TypedLiteral(kind=LiteralKind.QUANTITY, value=300, unit="gold")
    assert sv.verdict(sv.proposition(GOLD, CAL, quantity), gold).kind is VerdictKind.SUPPORTED
    negated = sv.proposition(GOLD, CAL, n(500), polarity=Polarity.NEGATED)
    assert sv.verdict(negated, gold).kind is VerdictKind.SUPPORTED
    negated = sv.proposition(GOLD, CAL, n(300), polarity=Polarity.NEGATED)
    found = sv.verdict(negated, gold)
    assert (found.kind, found.evidence[0].rule) == (VerdictKind.CONTRADICTED, Rule.POLARITY)

    mood = [owned(MOOD, MOOD, s("wary"), StateType.ENUM)]
    assert sv.verdict(sv.proposition(MOOD, MOOD, s("wary")), mood).kind is VerdictKind.SUPPORTED
    assert sv.verdict(sv.proposition(MOOD, MOOD, s("Wary")), mood).kind is VerdictKind.CONTRADICTED
    note = [owned(NOTE, NOTE, s("Under the stone"), StateType.TEXT)]
    found = sv.verdict(sv.proposition(NOTE, NOTE, s("under the stone")), note)
    assert found.kind is VerdictKind.SUPPORTED
    door = [owned(OPEN, OPEN, b(True), StateType.BOOLEAN)]
    assert sv.verdict(sv.proposition(OPEN, OPEN, b(True)), door).kind is VerdictKind.SUPPORTED
    assert sv.verdict(sv.proposition(OPEN, OPEN, b(False)), door).kind is VerdictKind.CONTRADICTED

    wrong_type = sv.verdict(sv.proposition(OPEN, OPEN, n(1)), door)
    assert (wrong_type.kind, wrong_type.reason) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.ARGUMENT_TYPE,
    )
    undefined = sv.verdict(sv.proposition(GOLD, CAL, n(1)), [owned(GOLD, CAL, None)])
    assert (undefined.kind, undefined.reason) == (
        VerdictKind.UNCERTAIN,
        VerdictReason.UNRESOLVED_ENTITY,
    )
    assert sv.verdict(sv.proposition(GOLD, MIRA, n(300)), gold).kind is VerdictKind.NOVEL


def test_canon_reads_only_the_owner_and_a_belief_never_does() -> None:
    """FR-VERIFY-7: a stale record with another number never supports a canon claim (only the
    owner's value is read); a ``belief(A)`` holder's check reads A's records and never the
    owner."""
    stale = Record(sv.proposition(GOLD, CAL, n(500)), "01JS00000000000000000000P1", "01JSFACT")
    claim = sv.proposition(GOLD, CAL, n(500))
    found = check(claim, [stale], registry=vs.REGISTRY, owned=[owned(GOLD, CAL, n(300))])
    assert found.kind is VerdictKind.CONTRADICTED
    assert not [e for e in found.evidence if e.fact_id is not None]
    off = check(
        claim,
        registry=vs.REGISTRY,
        owned=[owned(GOLD, CAL, n(300))],
        options=Options(contradictions=False),
    )
    assert off.kind is VerdictKind.NOVEL  # contradiction checking off: coverage only
    truth = check(
        sv.proposition(GOLD, CAL, n(300)),
        [stale],
        registry=vs.REGISTRY,
        owned=[owned(GOLD, CAL, n(300))],
    )
    assert truth.kind is VerdictKind.SUPPORTED
    believed = Record(sv.proposition(GOLD, CAL, n(500)), "01JS00000000000000000000P2", None, "B1")
    mine = check(
        claim,
        [believed],
        registry=vs.REGISTRY,
        holder=Holder.belief(MIRA),
        owned=[owned(GOLD, CAL, n(300))],
    )
    assert (mine.kind, mine.evidence[0].belief_id) == (VerdictKind.SUPPORTED, "B1")
    other = check(
        sv.proposition(GOLD, CAL, n(400)),
        [believed],
        registry=vs.REGISTRY,
        holder=Holder.belief(MIRA),
    )
    assert other.kind is VerdictKind.CONTRADICTED
    unknown = check(claim, [], registry=vs.REGISTRY, holder=Holder.belief(MIRA))
    assert unknown.kind is VerdictKind.NOVEL


def _holds(amount: str) -> Proposition:
    return Proposition(
        subject_id=CAL,
        predicate_id=HOLDS,
        object_id=COINS,
        qualifiers={"quantity": amount},
        valid_time=Interval.of(0, None),
    )


def test_forward_implication_checks_the_owner() -> None:
    """ADR-160: a declared rule ``holds ⇒ state_value`` checks an affirmed claim's implied
    value against the owner's: a different amount is contradicted, citing the rule and the
    owner's value; the same amount, or no rule, is not."""
    registry = vs.registry_of(Implication(when=HOLDS, then=STATE_VALUE))
    values = [owned(GOLD, CAL, n(300), entity=COINS)]
    found = check(_holds("500 gold"), registry=registry, owned=values)
    assert found.kind is VerdictKind.CONTRADICTED
    assert [(e.rule, e.owner_ref) for e in found.evidence] == [
        (Rule.IMPLICATION, None),
        (Rule.FUNCTIONAL_KEY, f"state:{GOLD}"),
    ]
    assert check(_holds("300 gold"), registry=registry, owned=values).kind is VerdictKind.NOVEL
    assert check(_holds("500 gold"), registry=vs.REGISTRY, owned=values).kind is VerdictKind.NOVEL
    # A belief holder's consequences never consult the owner.
    believer = check(
        _holds("500 gold"), registry=registry, holder=Holder.belief(MIRA), owned=values
    )
    assert believer.kind is VerdictKind.NOVEL


INDEX = EntityIndex(
    [
        EntityEntry(CAL, "person", "Cal", gender="masculine", character_id=CAL),
        EntityEntry(MIRA, "person", "Mira", gender="feminine", character_id=MIRA),
    ]
)
CONTEXT = ResolutionContext(index=INDEX, present=frozenset({CAL, MIRA}), addressed=frozenset({CAL}))
TRACKED = (
    sv.TrackedState(
        GOLD,
        "gold",
        StateType.NUMBER,
        StateScope.CHARACTER,
        StateVisibility.PLAYER_VISIBLE,
        ("coins",),
        "gp",
        COINS,
    ),
)


def _env() -> DetectionEnv:
    return DetectionEnv(
        registry=vs.REGISTRY,
        basis=TimeBasis(),
        context_for=lambda _speaker: CONTEXT,
        states=TRACKED,
        scope_keys=frozenset({CAL, MIRA}),
    )


def test_a_stated_balance_is_a_state_value_claim() -> None:
    """#139/#148: a lone amount naming the State in a sentence that states a balance is a
    ``state_value`` claim of the character it names; a price, a payment or a change is not."""
    beats = [
        DetectBeat(
            1, "narration", "Cal counts his coins on the counter: 500 gold in all.", NARRATOR
        ),
        DetectBeat(2, "say", "You have 500 gold, friend.", MIRA),
        DetectBeat(3, "narration", "The sword costs 500 gold.", NARRATOR),
        DetectBeat(4, "narration", "Cal pays 20 coins for the ale.", NARRATOR),
        DetectBeat(5, "narration", "Cal has no 40 silver.", NARRATOR),
        DetectBeat(6, "narration", "Cal does not have 30 gold left.", NARRATOR),
    ]
    found = detect_render(beats, (3, 0), _env())
    values = {
        c.beat or 0: c.proposition
        for c in found.claims
        if c.proposition is not None and c.proposition.predicate_id == STATE_VALUE
    }
    assert sorted(values) == [1, 2, 6]
    assert values[1] == sv.proposition(GOLD, CAL, n(500))
    assert values[2] == sv.proposition(GOLD, CAL, n(500))
    assert values[6] == sv.proposition(GOLD, CAL, n(30), polarity=Polarity.NEGATED)
    lone = [c for c in found.claims if c.beat == 3 and c.category == "quantity"]
    assert lone
    assert lone[0].proposition is None  # still the lone quantity it was
    without = detect_render(
        beats[:1],
        (3, 0),
        DetectionEnv(registry=vs.REGISTRY, basis=TimeBasis(), context_for=lambda _speaker: CONTEXT),
    )
    assert not [c for c in without.claims if c.proposition is not None]
