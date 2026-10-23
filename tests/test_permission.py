"""Unit checks of ``core.verify.permission`` outside the M4 parts (#144): the exception stubs
for capabilities later milestones deliver, #176's reactive exception, and the reason
precedence. S-NOVELTY-PERMISSION's M4 matrix is in
``tests/scenario/test_s_novelty_permission.py``."""

from __future__ import annotations

import pytest

from epistrel.core.verify import permission as pm
from epistrel.core.verify.permission import Asserter, Basis, Denied, Permitted, Reason
from tests.scenario.test_s_novelty_permission import BOBBY, CAL, CAMP, JENNI, KITCHEN, decide


def test_exception_stubs_deny() -> None:
    """``co_author`` player input (FR-RESP-11) is judged without its exception until it is
    delivered, so what it would admit is denied. Attributed reactive narration (FR-MULTI-9) is
    delivered (#176): its rule is ``test_reactive_exception``."""
    assert decide(BOBBY, "located_at", KITCHEN, Asserter("co_author", CAL)) == Denied(
        Reason.PLAYER_AUTHORITY
    )
    assert Asserter("reactive", JENNI, reactor=BOBBY).provenance.value == "render"
    assert Asserter("co_author", CAL).provenance.value == "player"


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
