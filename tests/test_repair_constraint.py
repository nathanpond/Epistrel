"""#1079 (#1060, ADR-220): whose name an ``unpermitted`` constraint carries
(``RepairLadder._constraint``). No database: the branch reads the claim, its denial and the
display names only.

- The speaker's own action toward another character, denied to protect that character
  (``other_character_secret``, ``player_character``), names the other character: Maren nodding
  to Flynn is "not Maren's to tell" about Flynn.
- A claim whose subject is someone else names its subject, whoever its object is: Maren
  asserting a Carlos→Flynn fact names Carlos, never Flynn (the ``subject == speaker`` term).
- A denial outside ``TOWARD_DENIALS`` (locked canon) keeps naming the subject, the speaker.
"""

from __future__ import annotations

from typing import Any, cast

from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.permission import Denied, Reason
from epistrel.core.verify.rules import Verdict, VerdictKind
from epistrel.orchestrator.repair.ladder import TOWARD_DENIALS, RepairLadder
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import VerifyResult

MAREN = "01JD0000000000000000000K01"
FLYNN = "01JD0000000000000000000K02"
CARLOS = "01JD0000000000000000000K03"
RELATION = "01JP0000000000000000000K01"
NAMES = {MAREN: "Maren", FLYNN: "Flynn", CARLOS: "Carlos"}


def _denied(subject: str, obj: str, reason: Reason) -> CheckedClaim:
    proposition = Proposition(
        subject_id=subject,
        predicate_id=RELATION,
        object_id=obj,
        valid_time=Interval.of(None, None),
    )
    return CheckedClaim(
        claim=DetectedClaim(
            span=(2, 0, 10),
            layer="extraction",
            category="extracted",
            channel="do",
            speaker=MAREN,
            proposition=proposition,
        ),
        result=VerifyResult(
            claim=proposition,
            verdict=Verdict(VerdictKind.UNCERTAIN),
            kind=VerdictKind.UNCERTAIN,
            permission=Denied(reason),
        ),
    )


async def _text(subject: str, obj: str, reason: Reason) -> str:
    ladder = object.__new__(RepairLadder)
    ladder.cast_names = dict(NAMES)
    made = await ladder._constraint(_denied(subject, obj, reason), cast(Any, None), NAMES, "Maren")
    assert made.kind == "do_not_assert"
    return made.text


async def test_another_subjects_fact_names_its_subject() -> None:
    """#1079: Maren asserting Carlos's relation to Flynn is told about Carlos, under either
    toward denial; dropping ``subject == speaker`` would name Flynn."""
    for reason in (Reason.OTHER_CHARACTER_SECRET, Reason.PLAYER_CHARACTER):
        text = await _text(CARLOS, FLYNN, reason)
        assert text.startswith("Do not introduce facts about Carlos:"), text
        assert "Flynn" not in text


async def test_the_speakers_own_action_names_the_other() -> None:
    """The renaming itself: Maren's action toward Flynn names Flynn under a toward denial, and
    keeps naming Maren under any other (locked canon)."""
    assert {r.value for r in (Reason.OTHER_CHARACTER_SECRET, Reason.PLAYER_CHARACTER)} == set(
        TOWARD_DENIALS
    )
    text = await _text(MAREN, FLYNN, Reason.OTHER_CHARACTER_SECRET)
    assert text.startswith("Do not introduce facts about Flynn:"), text
    locked = await _text(MAREN, FLYNN, Reason.LOCKED_CANON)
    assert locked.startswith("Do not introduce facts about Maren:"), locked
