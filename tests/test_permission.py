"""Unit checks of ``core.verify.permission`` outside the M4 parts (#144): #180's co-author
exception (which replaced M4's stub), #176's reactive exception, #519's hand-over exception,
and the reason precedence. S-NOVELTY-PERMISSION's M4 matrix is in
``tests/scenario/test_s_novelty_permission.py``."""

from __future__ import annotations

import pytest

from epistrel.core.propositions.model import Polarity
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.verify import permission as pm
from epistrel.core.verify.permission import (
    Asserter,
    Basis,
    Denied,
    PermissionFacts,
    Permitted,
    Reason,
)
from tests import novelty_support as ns
from tests.scenario.test_s_novelty_permission import (
    BOBBY,
    CAL,
    CAMP,
    ENTITIES,
    JENNI,
    KITCHEN,
    decide,
)


def test_co_author_exception() -> None:
    """#180 (FR-RESP-11) replaces M4's stub: ``co_author`` player input narrating another
    character's observable action (where he is, what he holds, that he spoke) is permitted with
    basis ``co_author`` and canonized with provenance ``player``; his backstory is still
    ``player_authority`` (the player text stands, never canon, never repaired), and so is
    anything about another human's character. Plain player input keeps M4's denial."""
    co_author = Asserter("co_author", CAL)
    assert decide(BOBBY, "located_at", KITCHEN, co_author) == Permitted(Basis.CO_AUTHOR)
    assert decide(BOBBY, "spoke_to", JENNI, co_author) == Permitted(Basis.CO_AUTHOR)
    backstory = decide(BOBBY, "attended_program", CAMP, co_author)
    assert backstory == Denied(Reason.PLAYER_AUTHORITY)
    assert isinstance(backstory, Denied)
    assert not backstory.repaired
    assert decide(BOBBY, "located_at", KITCHEN, Asserter("player", CAL)) == Denied(
        Reason.PLAYER_AUTHORITY
    )
    assert decide(BOBBY, "located_at", KITCHEN, co_author, protected=True) == Denied(
        Reason.PROTECTED_CONTENT
    )
    assert decide(BOBBY, "located_at", KITCHEN, co_author, locked=True) == Denied(
        Reason.LOCKED_CANON
    )
    assert co_author.provenance.value == "player"
    assert Asserter("reactive", JENNI, reactor=BOBBY).provenance.value == "render"


def test_reactive_exception() -> None:
    """#176 (FR-MULTI-9): Jenni's render narrating Bobby's reaction. That he spoke (to her) is
    his observable reaction (``reactive``); a placement change, his backstory, or anything
    about the player through him is not (repaired as the narrator's render would be). A
    reactive asserter names its reactor."""
    narrated = Asserter("reactive", JENNI, reactor=BOBBY)
    assert decide(BOBBY, "spoke", None, narrated) == Permitted(Basis.REACTIVE)
    assert decide(BOBBY, "spoke_to", JENNI, narrated) == Permitted(Basis.REACTIVE)
    assert decide(BOBBY, "located_at", KITCHEN, narrated) == Denied(Reason.OTHER_CHARACTER_SECRET)
    assert decide(BOBBY, "attended_program", CAMP, narrated) == Denied(
        Reason.OTHER_CHARACTER_SECRET
    )
    assert decide(BOBBY, "spoke_to", CAL, narrated) == Denied(Reason.PLAYER_CHARACTER)
    with pytest.raises(ValueError, match="reactor"):
        Asserter("reactive", JENNI)


def test_reason_precedence() -> None:
    """One code per denial, in the documented order (``state_change`` never fires in M4)."""
    assert tuple(Reason) == pm.PRECEDENCE
    assert pm.PRECEDENCE[:3] == (
        Reason.EXTERNAL_VALUE,
        Reason.LOCKED_CANON,
        Reason.STATE_CHANGE,
    )
    assert pm.PRECEDENCE[-1] is Reason.PROTECTED_CONTENT


KEY = "01JD00000000000000000000K1"
"""An item no modeled character is (absent from ``ENTITIES``: world texture)."""


def test_hand_over_exception() -> None:
    """#519 (owner's option (c), FR-VERIFY-11): Jenni's render handing Cal (the player's
    character) the key is her own action, so ``holds(Cal, key)`` with the detected giver Jenni
    is permitted (``own``). Every other claim about Cal is unchanged and stays
    ``player_character``: no giver ("Cal picks up the key", Cal acting), another character as
    the giver (Bobby), another render than the giver's, a director pass, a modeled character
    as the "item", a placement, and a negated ``holds``. Protected content and locked canon
    still win."""
    jenni = Asserter("character", JENNI)
    assert decide(CAL, "holds", KEY, jenni, giver=JENNI) == Permitted(Basis.OWN)
    assert decide(CAL, "holds", KEY, jenni) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "holds", KEY, jenni, giver=BOBBY) == Denied(Reason.PLAYER_CHARACTER)
    bobby = Asserter("character", BOBBY)
    assert decide(CAL, "holds", KEY, bobby, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    director = Asserter("director")
    assert decide(CAL, "holds", KEY, director, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "holds", BOBBY, jenni, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "located_at", KITCHEN, jenni, giver=JENNI) == Denied(Reason.PLAYER_CHARACTER)
    assert decide(CAL, "holds", KEY, jenni, giver=JENNI, protected=True) == Denied(
        Reason.PROTECTED_CONTENT
    )
    assert decide(CAL, "holds", KEY, jenni, giver=JENNI, locked=True) == Denied(Reason.LOCKED_CANON)
    negated = ns.claim(CAL, CORE_BY_NAME["holds"].id, KEY).model_copy(
        update={"polarity": Polarity.NEGATED}
    )
    given = PermissionFacts(entities=ENTITIES, predicate=CORE_BY_NAME["holds"], giver=JENNI)
    assert pm.permission(negated, jenni, pm.owner_of(negated, given), given) == Denied(
        Reason.PLAYER_CHARACTER
    )
