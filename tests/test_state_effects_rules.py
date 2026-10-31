"""The pure rules of a turn's State effects (``core.states.effects``; #187): the dry-run in beat
order, the narrated-change read, and what a finding tells a character's render (never a value).
Unmarked: the scenario parts (S-STATE-SPEND, S-STATE-INSUFFICIENT, S-NOVELTY-PERMISSION) are the
gated oracles; these pin the rules they rest on."""

from __future__ import annotations

from decimal import Decimal

from epistrel.core.states import StateDefinition, UndefinedType
from epistrel.core.states import effects as rules
from epistrel.core.states.model import Value
from epistrel.core.turns.beats import DeclaredEffect

GOLD = StateDefinition.model_validate(
    {
        "id": "s-gold",
        "name": "gold",
        "type": "number",
        "scope": "character",
        "constraints": {"non_negative": True, "max": 120},
        "initial_value": 0,
        "aliases": ("coins",),
        "unit": "gp",
    }
)
DEFINITIONS = {"gold": GOLD}
CAL = "c-cal"
Values = dict[tuple[str, str | None], Value | UndefinedType]


def _plan(beat: int, *effects: DeclaredEffect) -> list[rules.Planned]:
    return [rules.Planned(beat, n, "c-mira", e) for n, e in enumerate(effects)]


def _at(value: Decimal) -> Values:
    return {("gold", CAL): value}


def _spend(amount: int) -> DeclaredEffect:
    return DeclaredEffect("gold", "a purchase", scope_key=CAL, delta=-amount)


def test_dry_run_folds_in_beat_order() -> None:
    """Earlier passing effects are what a later one sees; a refused one changes nothing."""
    values: Values = {("gold", CAL): Decimal(100)}
    passed, found = rules.dry_run(
        DEFINITIONS, values, [*_plan(2, _spend(60)), *_plan(3, _spend(60)), *_plan(4, _spend(30))]
    )
    assert [(p.beat, p.old, p.new) for p in passed] == [
        (2, Decimal(100), Decimal(40)),
        (4, Decimal(40), Decimal(10)),
    ]
    (refused,) = found
    assert (refused.tag, refused.beat, refused.constraint) == (
        "state_constraint",
        3,
        "non_negative",
    )
    assert refused.current == Decimal(40)


def test_dry_run_unresolved_and_restored_out_of_bounds() -> None:
    """No amount is ``effect_unresolved``. A value a truncation restored above a since-lowered
    ``max`` (#185's handoff) refuses every effect that leaves it out of bounds and takes one
    that brings it back within them."""
    unresolved = DeclaredEffect("gold", "most of it", scope_key=CAL)
    _, (found,) = rules.dry_run(DEFINITIONS, {}, _plan(2, unresolved))
    assert (found.tag, found.effect) == ("effect_unresolved", 0)
    restored: Values = {("gold", CAL): Decimal(150)}
    _, (still,) = rules.dry_run(DEFINITIONS, restored, _plan(2, _spend(10)))
    assert (still.tag, still.constraint) == ("state_constraint", "max")
    passed, none = rules.dry_run(DEFINITIONS, restored, _plan(2, _spend(40)))
    assert not none
    assert passed[0].new == Decimal(110)


def test_narrated_changes() -> None:
    """A change verb with a State's term is a change: with an amount ``state_undeclared``,
    without one ``effect_unresolved``; a declared effect covers it; a negation does not
    change anything; a ``say`` is not read."""
    terms = [rules.terms_of(GOLD)]
    (found,) = rules.narrated(2, "do", "Mira pockets 20 gold.", (), terms, DEFINITIONS)
    assert (found.tag, found.span) == ("state_undeclared", (0, 21))
    (vague,) = rules.narrated(
        2, "narration", "He spends most of his coins.", (), terms, DEFINITIONS
    )
    assert vague.tag == "effect_unresolved"
    assert not rules.narrated(2, "do", "Mira pockets 20 gold.", (_spend(20),), terms, DEFINITIONS)
    assert not rules.narrated(2, "do", "Mira does not take any gold.", (), terms, DEFINITIONS)
    assert not rules.narrated(2, "say", "Pay me 20 gold.", (), terms, DEFINITIONS)


def test_a_character_is_told_no_value() -> None:
    """The repair line of a character's beat names the rule, never the value; the narrator's
    names both."""
    _, (found,) = rules.dry_run(DEFINITIONS, _at(Decimal(77)), _plan(2, _spend(500)))
    character = rules.constraint_line(found, GOLD, narrator=False)
    assert "there is not enough gold" in character
    assert "77" not in character
    assert "77 gp" in rules.constraint_line(found, GOLD, narrator=True)
    assert (
        rules.character_line(GOLD) == "gold (number; one per character; unit gp; also called coins)"
    )
