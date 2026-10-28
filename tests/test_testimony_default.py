"""The deterministic testimony default (``core.epistemic.testimony``, #162): the decision table,
the trust seed and nudges, and the guards on a model's decision. Pure: no database."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from epistrel.core.enums import BeliefConfidenceLevel as Level
from epistrel.core.enums import BeliefTransitionKind as Kind
from epistrel.core.epistemic import testimony as t
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.verify.rules import VerdictKind

CLAIM = Proposition(
    subject_id="01JENT1TY000000000000000A1",
    predicate_id="01JPRED0000000000000000099",
    object_id="01JENT1TY000000000000000A2",
    valid_time=Interval.of(None, None),
)


def _c(verdict: VerdictKind, prior: Level | None = None) -> t.Consistency:
    return t.Consistency(CLAIM, verdict, "01JBE11EF0000000000000000A" if prior else None, prior)


def test_the_decision_table() -> None:
    novel_trusted = t.decide("w", _c(VerdictKind.NOVEL), 0.8, "sincere")
    assert (novel_trusted.decision, novel_trusted.transition, novel_trusted.confidence) == (
        "accept",
        Kind.TESTIMONY,
        Level.BELIEVED,
    )
    novel_doubted = t.decide("w", _c(VerdictKind.NOVEL), 0.5, "sincere")
    assert novel_doubted.confidence is Level.SUSPECTED
    joke = t.decide("w", _c(VerdictKind.NOVEL), 1.0, "joking")
    assert (joke.decision, joke.transition) == ("speech_act_only", None)
    rejected = t.decide("w", _c(VerdictKind.CONTRADICTED, Level.KNOWN), 0.9, "sincere")
    assert (rejected.decision, rejected.transition) == ("reject", None)
    corrected = t.decide("w", _c(VerdictKind.CONTRADICTED, Level.SUSPECTED), 0.8, "sincere")
    assert (corrected.decision, corrected.transition) == ("accept", Kind.CORRECTION)
    weak = t.decide("w", _c(VerdictKind.CONTRADICTED, Level.SUSPECTED), 0.7, "sincere")
    assert weak.decision == "reject"
    raised = t.decide("w", _c(VerdictKind.SUPPORTED, Level.SUSPECTED), 0.2, "sincere")
    assert (raised.transition, raised.confidence) == (Kind.REVISION, Level.BELIEVED)
    capped = t.decide("w", _c(VerdictKind.SUPPORTED, Level.BELIEVED), 0.9, "sincere")
    assert (capped.decision, capped.transition, capped.confidence) == (
        "accept",
        None,
        Level.BELIEVED,
    )
    canon = t.decide("w", _c(VerdictKind.SUPPORTED), 0.2, "sincere")
    assert (canon.transition, canon.confidence) == (Kind.TESTIMONY, Level.BELIEVED)
    unsure = t.decide("w", _c(VerdictKind.UNCERTAIN), 0.9, "sincere")
    assert (unsure.decision, unsure.transition) == ("suspend", None)
    assert unsure.report()["decision"] == "suspend"


def _accepts_at(outcome: t.TestimonyOutcome, floor: Level) -> bool:
    levels = list(Level)
    return (
        outcome.decision == t.ACCEPT
        and outcome.confidence is not None
        and levels.index(outcome.confidence) >= levels.index(floor)
    )


@given(
    verdict=st.sampled_from(list(VerdictKind)),
    prior=st.none() | st.sampled_from(list(Level)),
    intent=st.sampled_from(["sincere", "joking"]),
    trusts=st.tuples(st.floats(0, 1), st.floats(0, 1)).map(sorted),
    floor=st.sampled_from(list(Level)),
)
def test_more_trust_never_takes_an_acceptance_away(
    verdict: VerdictKind,
    prior: Level | None,
    intent: t.PerceivedIntent,
    trusts: list[float],
    floor: Level,
) -> None:
    """#520: ``exposure.accepts`` reads a witness's trust only when the default answers
    differently at no trust and full trust, which is sound only if the default is monotone in
    trust: whatever it accepts at a level at some trust, it accepts at that level or above at
    any higher trust.

    Complement asserted: the bound is tight somewhere: a novel claim is accepted as believed at
    full trust and not at none (so the reads are made when they matter)."""
    low, high = trusts
    consistency = _c(verdict, prior)
    if _accepts_at(t.decide("w", consistency, low, intent), floor):
        assert _accepts_at(t.decide("w", consistency, high, intent), floor)
    novel = _c(VerdictKind.NOVEL)
    assert _accepts_at(t.decide("w", novel, 1.0, "sincere"), Level.BELIEVED)
    assert not _accepts_at(t.decide("w", novel, 0.0, "sincere"), Level.BELIEVED)


def test_a_say_with_no_claim_is_the_speech_act_alone() -> None:
    act = t.SpeechAct(speaker="s", beat=2, propositions=())
    (only,) = t.testimony("w", act, "sincere", consistency=(), trust=0.5)
    assert (only.decision, only.proposition, only.rule) == ("speech_act_only", None, "no_claim")
    act = t.SpeechAct(speaker="s", beat=2, propositions=(CLAIM,))
    (one,) = t.testimony("w", act, "sincere", consistency=[_c(VerdictKind.NOVEL)], trust=0.5)
    assert one.decision == "accept"


def test_hearers_never_read_deception() -> None:
    assert t.perceived("joking") == "joking"
    for intent in ("sincere", "deceptive", "unspecified", None):
        assert t.perceived(intent) == "sincere"


def test_trust_seed_and_nudges() -> None:
    assert t.seed_trust([]) == t.TRUST_NEUTRAL
    assert t.seed_trust([("friend_of", {})]) == t.TRUST_HIGH
    assert t.seed_trust([("sibling_of", {})]) == t.TRUST_HIGH
    assert t.seed_trust([("enemy_of", {})]) == t.TRUST_LOW
    assert t.seed_trust([("enemy_of", {}), ("married_to", {})]) == t.TRUST_HIGH
    assert t.seed_trust([("knows", {"trust": 0.3})]) == 0.3
    assert t.seed_trust([("knows", {"trust": 7}), ("knows", {"trust": True})]) == t.TRUST_NEUTRAL
    assert t.nudged(0.5, rejected=2) == 0.3
    assert t.nudged(0.95, confirmed=2) == 1.0
    assert t.nudged(0.05, overturned=1) == 0.0


def test_the_model_never_overrides_a_contradiction_or_accepts_a_joke() -> None:
    contradicted = t.decide("w", _c(VerdictKind.CONTRADICTED, Level.KNOWN), 0.9, "sincere")
    assert t.apply_adjudication(contradicted, "accept", 0.99, "sincere") is None
    joke = t.decide("w", _c(VerdictKind.NOVEL), 0.9, "joking")
    assert t.apply_adjudication(joke, "accept", 0.99, "joking") is None
    novel = t.decide("w", _c(VerdictKind.NOVEL), 0.5, "sincere")
    assert t.apply_adjudication(novel, "reject", 0.5, "sincere") is None  # below 0.6
    taken = t.apply_adjudication(novel, "reject", 0.9, "sincere")
    assert taken is not None
    assert (taken.decision, taken.transition, taken.adjudicated) == ("reject", None, True)
    same = t.apply_adjudication(novel, "accept", 0.9, "sincere")
    assert same is not None
    assert same.transition is Kind.TESTIMONY
    assert same.adjudicated
    suspended = t.decide("w", _c(VerdictKind.UNCERTAIN), 0.9, "sincere")
    accepted = t.apply_adjudication(suspended, "accept", 0.9, "sincere")
    assert accepted is not None
    assert (accepted.transition, accepted.confidence) == (Kind.TESTIMONY, Level.BELIEVED)
    nothing = t.testimony("w", t.SpeechAct("s", 2, ()), "sincere", consistency=(), trust=0.5)[0]
    assert t.apply_adjudication(nothing, "accept", 0.9, "sincere") is None
