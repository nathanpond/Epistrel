"""#518 (FR-PROP-9): a fluent change is not a contradiction (``core.verify.rules``'
``fluent_change``), beside U-15's rules (``tests/test_u_15.py``). A regression, so no part.

``check`` over records built in memory (it is pure): a claim of the beat's ``mover`` that holds
its own fluent from ``t`` on, against an open-ended record of the old value that starts before
``t``, closes that record at ``t`` and is ``novel``, citing it. Each complement keeps the
rules' answer: no mover or another subject, a claim that starts at or before the record, a
bounded claim or record, a permanent predicate, and a stale re-assertion of the old value."""

from __future__ import annotations

from epistrel.core.propositions.model import Bound, Interval, Proposition
from epistrel.core.verify.detect import Category, DetectedClaim, Layer
from epistrel.core.verify.permission import Asserter
from epistrel.core.verify.rules import Record, Rule, Verdict, VerdictKind, check
from epistrel.orchestrator.verify.verifier import mover_of
from tests import verify_support as vs

HOUSE = [
    vs.prop(vs.KITCHEN, vs.LOCATED_IN, vs.HOUSE),
    vs.prop(vs.GARDEN, vs.LOCATED_IN, vs.HOUSE),
]


def _check(claim: Proposition, *canon: Record, mover: str | None = vs.BOBBY) -> Verdict:
    return check(claim, [*canon, *vs.records(*HOUSE)], registry=vs.REGISTRY, mover=mover)


def _changes(verdict: Verdict, *closed: Record) -> None:
    assert verdict.kind is VerdictKind.NOVEL, verdict
    assert verdict.rules == {Rule.FLUENT_CHANGE}, verdict
    assert verdict.supersedes == tuple(r.fact_id for r in closed)


def _contradicts(verdict: Verdict, rule: Rule) -> None:
    assert verdict.kind is VerdictKind.CONTRADICTED, verdict
    assert rule in verdict.rules, verdict
    assert Rule.FLUENT_CHANGE not in verdict.rules


def test_fluent_change() -> None:
    """An exit (the negated placement) and a move elsewhere (the functional key) from minute 50
    close Bobby's open kitchen placement; the same claims with no mover are contradicted."""
    kitchen = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN))
    leaves = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(50, None), negated=True)
    _changes(_check(leaves, kitchen), kitchen)
    _contradicts(_check(leaves, kitchen, mover=None), Rule.POLARITY)
    moves = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.GARDEN, vs.span(50, None))
    _changes(_check(moves, kitchen), kitchen)
    _contradicts(_check(moves, kitchen, mover=None), Rule.FUNCTIONAL_KEY)
    # Two places no containment fact relates: still a move from the kitchen, not world knowledge.
    florida = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.FLORIDA, vs.span(50, None))
    _changes(_check(florida, kitchen), kitchen)
    # A placement that started earlier, open-ended: closed the same way.
    since = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(10, None)))
    _changes(_check(leaves, since), since)
    # A containing place is no other value: moving within the house closes nothing.
    house = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.HOUSE))
    assert _check(moves, house).rules == frozenset()


def test_fluent_change__complements() -> None:
    """What keeps today's answer: another subject's change, a claim starting at or before the
    record, a bounded claim, a bounded record, a record whose start may fall after the claim's,
    and a permanent predicate."""
    kitchen = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN))
    leaves = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(50, None), negated=True)
    _contradicts(_check(leaves, kitchen, mover=vs.ANN), Rule.POLARITY)
    since = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(50, None)))
    _contradicts(_check(leaves, since), Rule.POLARITY)
    _contradicts(
        _check(leaves.model_copy(update={"valid_time": vs.span(40, None)}), since), (Rule.POLARITY)
    )
    bounded = leaves.model_copy(update={"valid_time": vs.span(50, 60)})
    _contradicts(_check(bounded, kitchen), Rule.POLARITY)
    until = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(None, 100)))
    _contradicts(_check(leaves, until), Rule.POLARITY)
    # Started somewhere in [40, 60]: it may start after minute 50, so it is not closed there.
    approx = Interval(start=Bound(outer=40, inner=60))
    late = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, approx))
    assert _check(leaves, late).kind is not VerdictKind.NOVEL
    siblings = vs.record(vs.prop(vs.BOBBY, vs.SIBLING_OF, vs.ANN))
    denied = vs.prop(vs.BOBBY, vs.SIBLING_OF, vs.ANN, vs.span(50, None), negated=True)
    _contradicts(_check(denied, siblings), Rule.POLARITY)


def test_fluent_change__stale() -> None:
    """After the exit (the kitchen up to minute 50, its negation from it), a stale re-assertion
    of the kitchen at minute 70 is contradicted by the negation; Bobby's own return is a change
    again, closing the negation."""
    before = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(None, 50)))
    gone = vs.record(vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(50, None), negated=True))
    stale = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(70, None))
    _contradicts(_check(stale, before, gone, mover=None), Rule.POLARITY)
    _changes(_check(stale, before, gone), gone)
    # The stale claim stated over the old placement's time is supported, not a change.
    then = vs.prop(vs.BOBBY, vs.LOCATED_AT, vs.KITCHEN, vs.span(10, 40))
    assert _check(then, before, gone).kind is VerdictKind.SUPPORTED


def test_fluent_change__mover() -> None:
    """The mover is the acting character of a ``do`` beat (a render's or the player's own) whose
    claim states a change: never a ``say`` (a lie moves no one), a narrator, reactive narration,
    no asserter, or a claim with no change cue (extraction only)."""
    bobby = Asserter("character", vs.BOBBY)
    assert mover_of(bobby, "do", change=True) == vs.BOBBY
    assert mover_of(Asserter("player", vs.CAL), "do", change=True) == vs.CAL
    assert mover_of(bobby, "do", change=False) is None
    assert mover_of(bobby, "say", change=True) is None
    assert mover_of(bobby, "narration", change=True) is None
    assert mover_of(Asserter("director"), "do", change=True) is None
    assert mover_of(Asserter("reactive", vs.BOBBY, reactor=vs.ANN), "do", change=True) is None
    assert mover_of(None, "do", change=True) is None


def test_fluent_change__cue() -> None:
    """A change cue: a declared claim, or the movement detector's (alone or merged with
    extraction); an extraction-only placement ("never set foot in the cellar") has none."""

    def found(layer: Layer, category: Category, *merged: Layer) -> DetectedClaim:
        return DetectedClaim((2, 0, 5), layer, category, "do", vs.BOBBY, merged=merged)

    assert found("declared", "declared").change_cue
    assert found("detector", "movement").change_cue
    assert found("extraction", "movement", "detector").change_cue
    assert not found("extraction", "movement").change_cue
    assert not found("detector", "possession").change_cue
