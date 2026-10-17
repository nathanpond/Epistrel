"""Unit checks of ``core.verify.permission`` outside the M4 parts (#144): the exception stubs
for capabilities later milestones deliver, and the reason precedence. S-NOVELTY-PERMISSION's
M4 matrix is in ``tests/scenario/test_s_novelty_permission.py``."""

from __future__ import annotations

from epistrel.core.verify import permission as pm
from epistrel.core.verify.permission import Asserter, Denied, Reason
from tests.scenario.test_s_novelty_permission import BOBBY, CAL, JENNI, KITCHEN, decide


def test_exception_stubs_deny() -> None:
    """Attributed reactive narration (FR-MULTI-9) and ``co_author`` player input (FR-RESP-11)
    are judged without their exceptions until M5, so what they would admit is denied."""
    assert decide(BOBBY, "located_at", KITCHEN, Asserter("reactive", JENNI)) == Denied(
        Reason.OTHER_CHARACTER_SECRET
    )
    assert decide(BOBBY, "located_at", KITCHEN, Asserter("co_author", CAL)) == Denied(
        Reason.PLAYER_AUTHORITY
    )
    assert Asserter("reactive", JENNI).provenance.value == "render"
    assert Asserter("co_author", CAL).provenance.value == "player"


def test_reason_precedence() -> None:
    """One code per denial, in the documented order (``state_change`` never fires in M4)."""
    assert tuple(Reason) == pm.PRECEDENCE
    assert pm.PRECEDENCE[:3] == (
        Reason.EXTERNAL_VALUE,
        Reason.LOCKED_CANON,
        Reason.STATE_CHANGE,
    )
    assert pm.PRECEDENCE[-1] is Reason.PROTECTED_CONTENT
