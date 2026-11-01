"""The pure rules of completeness in both directions (``core.states.completeness``; #190,
FR-STATE-13): which narrated changes a declared effect covers (State, direction, amount within
tolerance), which declared effects nothing narrates, the deterministic rung's appended line, and
the detection rules the scenarios rest on. Unmarked: S-STATE-COMPLETENESS and
S-EXTRACT-ADVERSARIAL (M6) are the gated oracles; these pin the rules."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME, STATE_VALUE
from epistrel.core.states import StateDefinition
from epistrel.core.states import completeness as c
from epistrel.core.states import effects as rules
from epistrel.core.states import verify as sv
from epistrel.core.states.authorize import Counterparty, StateEffect
from epistrel.core.turns.beats import DeclaredEffect, TurnBeat
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.rules import Verdict, VerdictKind
from epistrel.orchestrator.repair.ladder import state_deterministic
from epistrel.orchestrator.structured import effects as state_effects
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

CAL, MIRA, LINDA = (
    "01J0000000000000000000CA10",
    "01J0000000000000000000M1RA",
    "01J00000000000000000000B1N",
)
KEY = "01J00000000000000000000KEY"
GOLD = StateDefinition.model_validate(
    {
        "id": "01JSTATE00000000000000GOLD",
        "name": "gold",
        "type": "number",
        "scope": "character",
        "constraints": {"non_negative": True},
        "initial_value": 0,
        "aliases": ("coins",),
        "unit": "gp",
    }
)
HAS_KEY = StateDefinition.model_validate(
    {
        "id": "01JSTATE0000000000000HASKY",
        "name": "has_key",
        "type": "boolean",
        "scope": "character",
        "initial_value": False,
        "entity_id": KEY,
    }
)
PRICE = StateDefinition.model_validate(
    {
        "id": "01JSTATE00000000000000PRCE",
        "name": "sword_price",
        "type": "number",
        "scope": "story",
        "initial_value": 40,
        "unit": "silver",
    }
)
DEFINITIONS = {d.name: d for d in (GOLD, HAS_KEY, PRICE)}


def _beat(text: str, *, kind: str = "narration", actor: str | None = None) -> c.Beat:
    return c.Beat(2, kind, text, actor, narrator=actor is None)


def _gold(delta: int, scope: str = CAL, *, beat: int = 2, **kw: Any) -> c.Declared:
    return c.Declared(
        beat, 0, StateEffect("gold", scope, delta=Decimal(delta)), kw.pop("reason", "a sale"), **kw
    )


def _diff(text: str, *declared: c.Declared, **kw: Any) -> c.Diff:
    return c.diff([], declared, DEFINITIONS, beats=[_beat(text, **kw)])


COUNTED = "You count out 500 gold and hand it over."


def test_undeclared_payment() -> None:
    """A narrated payment with no effect is ``undeclared``: the State, the amount, the way the
    verb moves it; the effect of that amount covers it, another amount is a mismatch."""
    (u,) = _diff(COUNTED).undeclared
    assert (u.ref.state, u.ref.amount, u.ref.direction, u.mismatch) == (
        "gold",
        Decimal(500),
        c.LOSS,
        False,
    )
    assert u.ref.span == (0, len(COUNTED))
    assert not _diff(COUNTED, _gold(-500)).undeclared
    (mismatch,) = _diff(COUNTED, _gold(-50)).undeclared
    assert mismatch.mismatch
    assert not _diff(COUNTED, _gold(-50)).unnarrated  # its unit is narrated


def test_direction_on_the_actors_own_value() -> None:
    """The direction is checked on the acting character's own value only."""
    hands = "Mira hands over 20 gold."
    assert _diff(hands, _gold(-20, MIRA), actor=MIRA).undeclared == ()
    (wrong,) = _diff(hands, _gold(20, MIRA), actor=MIRA).undeclared
    assert wrong.mismatch
    # Mira's beat moving Cal's gold: the verb's direction is hers, so it is not checked.
    assert _diff(hands, _gold(20, CAL), actor=MIRA).undeclared == ()


def test_tolerance() -> None:
    """Exact for a stated amount, ±10 % for an approximation, any amount when none."""
    assert _diff("Cal hands over about fifty gold.", _gold(-54)).undeclared == ()
    assert _diff("Cal hands over about fifty gold.", _gold(-56)).undeclared != ()
    assert _diff("Cal hands over fifty gold.", _gold(-51)).undeclared != ()
    assert _diff("Cal spends some of his coins.", _gold(-7)).undeclared == ()
    # A number State's change with no amount and no effect is #187's effect_unresolved.
    assert _diff("Cal spends some of his coins.").undeclared == ()


def test_words_after_a_cap() -> None:
    """#188's handoff: a capped effect whose prose spells the declared amount in words."""
    (u,) = _diff("Cal pockets eight hundred gold.", _gold(500)).undeclared
    assert (u.ref.amount, u.mismatch) == (Decimal(800), True)
    assert u.ref.direction == c.GAIN


def test_untracked_and_suppressed() -> None:
    """An untracked quantity, a negated, conditional or questioned change: nothing."""
    assert _diff("Cal hands over three loaves.").undeclared == ()
    assert _diff("Cal does not hand over 500 gold.").undeclared == ()
    assert _diff("If Cal hands over 500 gold, Mira smiles.").undeclared == ()
    assert _diff("Will Cal hand over 500 gold?").undeclared == ()
    assert _diff("You have 500 gold.", kind="say", actor=MIRA).undeclared == ()


def _holds(beat: int, start: int) -> DetectedClaim:
    return DetectedClaim(
        span=(beat, start, start + 15),
        layer="detector",
        category="possession",
        channel="do",
        speaker=LINDA,
        proposition=Proposition(
            subject_id=LINDA,
            predicate_id=CORE_BY_NAME["holds"].id,
            object_id=KEY,
            valid_time=Interval.of(None, None),
        ),
    )


def test_tracked_entity() -> None:
    """A detected claim whose object resolves to a State's ``entity_id`` is that State's
    change: undeclared with no effect (its claim id kept), covered by a set of the matching
    direction, a mismatch for the other."""
    text = "She pockets the key on her way out."
    beats = [c.Beat(2, "do", text, LINDA)]
    found = c.diff([("claim-1", _holds(2, 4))], [], DEFINITIONS, beats=beats)
    (u,) = found.undeclared
    assert (u.claim_id, u.ref.state, u.ref.direction) == ("claim-1", "has_key", c.GAIN)
    take = c.Declared(2, 0, StateEffect("has_key", LINDA, value=True))
    drop = c.Declared(2, 0, StateEffect("has_key", LINDA, value=False))
    assert c.diff([("claim-1", _holds(2, 4))], [take], DEFINITIONS, beats=beats).undeclared == ()
    (m,) = c.diff([("claim-1", _holds(2, 4))], [drop], DEFINITIONS, beats=beats).undeclared
    assert m.mismatch
    # #187's sentence scan names it by the entity's name too, with no amount needed.
    terms = [rules.terms_of(HAS_KEY, {KEY: "key"})]
    (f,) = rules.narrated(2, "do", text, (), terms, DEFINITIONS)
    assert f.tag == rules.STATE_UNDECLARED


def test_transfer_other_side_covers() -> None:
    """A transfer covers a change narrated on its other side."""
    transfer = c.Declared(
        2,
        0,
        StateEffect(
            "gold", MIRA, kind="transfer", delta=Decimal(30), counterparty=Counterparty("gold", CAL)
        ),
    )
    assert _diff("Cal hands over 30 gold.", transfer).undeclared == ()


def test_unnarrated() -> None:
    """A declared effect on a player-visible State no beat mentions is ``unnarrated``; a
    mention of its name, alias, unit or amount narrates it; ``silent`` (the effect's or the
    State's) and a hidden State need none."""
    quiet = "Mira nods."
    (d,) = _diff(quiet, _gold(-50)).unnarrated
    assert d.effect.delta == Decimal(-50)
    for text in ("Mira counts the coins.", "The gp is gone.", "That makes 50.", "The gold."):
        assert _diff(text, _gold(-50)).unnarrated == (), text
    assert _diff(quiet, _gold(-50, silent=True)).unnarrated == ()
    silent = {**DEFINITIONS, "gold": GOLD.model_copy(update={"silent": True})}
    assert c.diff([], [_gold(-50)], silent, beats=[_beat(quiet)]).unnarrated == ()
    hidden = {**DEFINITIONS, "gold": GOLD.model_copy(update={"visibility": "hidden"})}
    assert c.diff([], [_gold(-50)], hidden, beats=[_beat(quiet)]).unnarrated == ()
    # Another beat of the turn (an earlier primary's staged one) may carry the narration.
    assert (
        c.diff([], [_gold(-50)], DEFINITIONS, beats=[_beat(quiet)], texts=["50 coins"]) == c.Diff()
    )


def test_narration_line() -> None:
    assert (
        c.narration_line(_gold(-50, reason="paid the toll."), GOLD) == "Paid the toll (gold -50)."
    )
    flag = c.Declared(2, 0, StateEffect("has_key", LINDA, value=True), "found it")
    assert c.narration_line(flag, HAS_KEY) == "Found it (has key: true)."


def test_deterministic_rung_appends() -> None:
    """The deterministic rung keeps an unnarrated effect and appends its line; with an
    undeclared sentence of another State it narrows that first."""
    effect = DeclaredEffect("gold", "paid the toll", scope_key=CAL, delta=-50)
    beat = TurnBeat("narration", 2, "The gate opens.", None, effects=(effect,))
    line = rules.Finding(
        c.STATE_UNNARRATED, 2, "gold", effect=0, narration="Paid the toll (gold -50)."
    )
    done = state_deterministic(beat, [line])
    assert done is not None
    assert (done.text, done.effects) == ("The gate opens. Paid the toll (gold -50).", (effect,))
    text = "The gate opens. She pockets the key."
    pocket = rules.Finding(rules.STATE_UNDECLARED, 2, "has_key", span=(16, len(text)))
    done = state_deterministic(
        TurnBeat("narration", 2, text, None, effects=(effect,)), [pocket, line]
    )
    assert done is not None
    assert done.text == "The gate opens. Paid the toll (gold -50)."


def test_noun_reading() -> None:
    """A change verb's form read as a noun is no change ("final offer")."""
    assert not rules.change_cue("Forty silver, final offer.")
    assert rules.change_cue("She offers forty silver.")
    assert not rules.change_cue("Her pockets are empty.")


def test_story_value_claim() -> None:
    """A story State's value is stated by a sentence naming its amount with no change verb
    (a price, not only a balance); a relative amount is not its value."""
    tracked = sv.tracked([PRICE])
    for sentence in ("Forty silver, final offer.", "The sword costs forty silver."):
        found = sv.state_claim(
            tracked, "silver", 40, subject=None, scope_keys=frozenset(), sentence=sentence
        )
        assert found is not None, sentence
        assert found.predicate_id == STATE_VALUE
        assert found.subject_id == PRICE.id
    for sentence in ("Another forty silver.", "She hands over forty silver."):
        assert (
            sv.state_claim(
                tracked, "silver", 40, subject=None, scope_keys=frozenset(), sentence=sentence
            )
            is None
        ), sentence


def test_value_claim_folds_earlier_effects() -> None:
    """#191's handoff: a value stated after an earlier beat's effect is judged against the
    value that effect leaves, not the turn's starting value."""
    book = state_effects.Book("story", "on", {"gold": GOLD}, {("gold", CAL): Decimal(500)})
    beats = [
        TurnBeat(
            "do",
            2,
            "Mira takes 300 gold.",
            MIRA,
            effects=(DeclaredEffect("gold", "a sale", scope_key=CAL, delta=-300),),
        ),
        TurnBeat("narration", 3, "Cal has 200 gold left.", None),
    ]
    claim = sv.proposition(str(GOLD.id), CAL, sv.number_literal(200))

    def checked(beat: int, kind: VerdictKind) -> CheckedClaim:
        return CheckedClaim(
            DetectedClaim((beat, 0, 10), "detector", "quantity", "narration", "n", claim),
            VerifyResult(claim=claim, verdict=Verdict(kind), kind=kind),
            "c1",
        )

    (later,) = state_effects.folded([checked(3, VerdictKind.CONTRADICTED)], beats, book)
    assert later.result is not None
    assert later.result.kind is VerdictKind.SUPPORTED
    # The declaring beat itself, and a beat before it, keep their verdicts.
    (same,) = state_effects.folded([checked(2, VerdictKind.CONTRADICTED)], beats, book)
    assert same.result is not None
    assert same.result.kind is VerdictKind.CONTRADICTED
    wrong = sv.proposition(str(GOLD.id), CAL, sv.number_literal(500))
    stale = CheckedClaim(
        DetectedClaim((3, 0, 10), "detector", "quantity", "narration", "n", wrong),
        VerifyResult(
            claim=wrong, verdict=Verdict(VerdictKind.SUPPORTED), kind=VerdictKind.SUPPORTED
        ),
    )
    (judged,) = state_effects.folded([stale], beats, book)
    assert judged.result is not None
    assert judged.result.kind is VerdictKind.CONTRADICTED
