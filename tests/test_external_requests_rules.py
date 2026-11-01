"""#209's pure rules: a sentence asserting a requested external change's outcome
(``core.states.effects.outcome_spans``), the narrator's ``pending_outcomes`` lines, and an
external clock's pins folded into its advances (``core.world_clock.external_mode.merged``)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from epistrel.core.membership import Position
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.states import effects as rules
from epistrel.core.world_clock import external_mode

GOLD = rules.Pending("gold", "g1", "c1", ("gold", "coins", "gp"), Decimal(300), Decimal(250))


@pytest.mark.parametrize(
    "text",
    [
        "The purchase went through.",
        "Your balance is now 250.",
        "Cal has 250 gold left.",
        "The payment was approved.",
        "Mira takes the coins. The purchase succeeded.",
    ],
)
def test_an_asserted_outcome_is_found(text: str) -> None:
    (found,) = rules.unconfirmed_outcomes(3, "do", text, [GOLD])
    assert (found.tag, found.state, found.scope_key) == (
        "external_outcome_unconfirmed",
        "gold",
        "c1",
    )
    # The asserting sentence alone (the last one in each case), never the attempt before it.
    last = text.rsplit(". ", 1)[-1]
    assert found.span is not None
    assert text[found.span[0] : found.span[1]].strip() == last


@pytest.mark.parametrize(
    "text",
    [
        "Cal hands over the coins.",
        "Mira takes 50 gold.",
        "Cal has 300 gold left.",
        "If the payment goes through, the sword is yours.",
        "Did the purchase go through?",
        "The purchase did not go through.",
    ],
)
def test_an_attempt_is_no_outcome(text: str) -> None:
    assert rules.unconfirmed_outcomes(3, "do", text, [GOLD]) == []


def test_only_prose_beats_are_read_and_nothing_without_a_request() -> None:
    assert rules.unconfirmed_outcomes(3, "think", "The purchase went through.", [GOLD]) == []
    assert rules.unconfirmed_outcomes(3, "do", "The purchase went through.", []) == []


def test_pending_lines() -> None:
    assert (
        rules.pending_line({"state": "gold", "delta": -50, "reason": "a sword"}, "Cal")
        == "gold -50 (Cal's): a sword"
    )
    assert rules.pending_line({"state": "rank", "value": "knight", "reason": "dubbed"}) == (
        "rank set to knight: dubbed"
    )
    assert rules.pending_line(
        {"state": "world_clock", "kind": "time_advance", "minutes": 3, "reason": "talk"}
    ) == ("the clock forward by 3 minutes: talk")


def test_pins_fold_into_the_advances() -> None:
    """Advances written before the switch stay; each pin is the offset from its turn's start
    (whichever variant), and an advance written after leaving external mode adds to the last
    pin: the clock never moves backward."""
    written = [((1, 0, 3, 0), 1, 5), ((4, 0, 2, 0), 1, 7)]
    pinned = [(Position(2), 600), (Position(3, 2), 700)]
    basis = TimeBasis(advances=tuple(external_mode.merged(written, pinned)))
    assert basis.through(Position(1, 1, 0, 9, 0)) == 5
    assert basis.at(Position(2)) == 600
    assert basis.at(Position(3, 1)) == 700  # another variant of the pin's turn reads it too
    assert basis.through(Position(4, 1, 0, 9, 0)) == 707
    assert external_mode.merged(written, []) == written
