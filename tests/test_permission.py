"""Unit checks of ``core.verify.permission`` outside the M4 parts (#144): #180's co-author
exception (which replaced M4's stub), #176's reactive exception, and the reason precedence.
S-NOVELTY-PERMISSION's M4 matrix is in ``tests/scenario/test_s_novelty_permission.py``."""

from __future__ import annotations

import pytest

from epistrel.core.verify import permission as pm
from epistrel.core.verify.permission import Asserter, Basis, Denied, Permitted, Reason
from tests.scenario.test_s_novelty_permission import BOBBY, CAL, CAMP, JENNI, KITCHEN, decide


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
