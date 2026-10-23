"""Grounded resurfacing: a character's recall of a past private moment is checked against what
it actually recorded (FR-VERIFY-9, FR-EPI-11, FR-TURN-3; #172).

**A recall claim** is a first-person ``say`` sentence opening a memory cue of the English table
``core/verify/detectors/recall.json`` ("I remember you seemed sad that day", "that time when …");
``find_recalls`` returns each one's span and recalled content (the words after the cue, to the
sentence's end). A negated cue ("I don't remember") and a question assert no memory.

**Backing** (``backing``). The recall is grounded iff a dated record the speaker holds backs it:
one of its own active memories (``holder_character_id`` = the speaker: what it perceived, its
own ``think``s, as #161's routing wrote them) from a turn before the recall's. The record backs
the recall when it covers ``core.verify.inner_state.COVERAGE`` or more of the recall's content
words (folded, stemmed, stopwords dropped): "you seemed sad" is backed by "She seems sad today",
"you were crying at the bus stop" by nothing. The best backing (highest coverage, then the most
recent) is cited.

- Backed: ``grounded``. The verifier answers ``supported`` with rule ``recall`` (FR-VERIFY-9's
  encouraged callback) and the recall stands; ``core.epistemic.context`` steers renders toward
  such callbacks with the ``## Your private memories`` section.
- Unbacked: a **confabulation**. The verifier answers ``novel``, never canonized, repaired with
  the constraint "Do not claim to remember …" (#151) and recorded as an escalation record with
  gate reason ``confabulation`` (#155).

There is no story clock before M6 (FR-CLOCK), so a record backs a recall from any earlier turn;
the referent day of "that day" is not compared. Reads only memories (never ``facts``: FR-EPI-1's
separation). No text is logged (invariant 3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure.matcher import fold
from epistrel.core.membership import active_only
from epistrel.core.tables.memories import memories
from epistrel.core.verify.inner_state import COVERAGE, content_words, coverage

RECALL = "recall"
"""The rule of a grounded recall's ``supported`` verdict, and the detector category."""
CONFABULATION = "confabulation"
"""The repair tag (and escalation gate reason) of an unbacked recall."""
BACKING_MAX = 500
"""The most of the speaker's records one recall reads (newest first)."""
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")


@cache
def table() -> Mapping[str, Any]:
    """``core/verify/detectors/recall.json``."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/recall.json")
    loaded: dict[str, Any] = json.loads(source.read_text("utf-8"))
    return loaded


@cache
def _pattern() -> tuple[re.Pattern[str], re.Pattern[str]]:
    def alternation(words: list[str]) -> str:
        ordered = sorted({fold(w) for w in words}, key=lambda w: (-len(w), w))
        return "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)

    found = table()
    cue = re.compile(rf"(?<![\w'])(?:{alternation(found['cues'])})(?![\w'])")
    negated = re.compile(rf"(?<![\w'])(?:{alternation(found['negated'])})(?![\w'])")
    return cue, negated


@dataclass(frozen=True, slots=True)
class RecallClaim:
    """One recall sentence: its span in the text and the recalled content."""

    start: int
    end: int
    content: str


def find_recalls(text: str) -> list[RecallClaim]:
    """The recall claims of ``text`` (see the module docstring)."""
    if not text:
        return []
    cue, negated = _pattern()
    found: list[RecallClaim] = []
    for sentence in _SENTENCE.finditer(text):
        start, end = sentence.span()
        body = sentence.group()
        # Folding keeps a Latin text's offsets; the cue table is plain English.
        folded = fold(body.replace("\u2019", "'"))
        if len(folded) != len(body) or body.rstrip().endswith("?") or negated.search(folded):
            continue
        match = cue.search(folded)
        if match is None:
            continue
        content = body[match.end() :].strip(" ,;:.!")
        if content_words(content):
            found.append(RecallClaim(start, end, content))
    return found


@dataclass(frozen=True, slots=True)
class Backing:
    """The record that backs a recall: its memory id, turn and coverage."""

    memory_id: str
    turn: int
    coverage: float


@dataclass(frozen=True, slots=True)
class RecallCheck:
    """A recall's verdict: ``grounded`` with its ``backing``, or a confabulation."""

    grounded: bool
    backing: Backing | None = None

    def payload(self) -> dict[str, Any]:
        """``turn_report.verify.verdicts[].recall``: ids and numbers only."""
        return {
            "grounded": self.grounded,
            "memory_id": None if self.backing is None else self.backing.memory_id,
            "coverage": None if self.backing is None else round(self.backing.coverage, 3),
        }


async def backing(
    conn: AsyncConnection, story_id: str, speaker: str, *, turn: int, content: str
) -> Backing | None:
    """The best record of ``speaker``'s from before ``turn`` backing ``content`` (see the
    module docstring), or ``None``."""
    wanted = content_words(content)
    if not wanted:
        return None
    m = memories.c
    rows = await conn.execute(
        select(m.id, m.text, m.narrative_turn)
        .where(
            m.story_id == story_id,
            m.holder_character_id == speaker,
            m.narrative_turn < turn,
            m.quarantined_at.is_(None),
            active_only(memories),
        )
        .order_by(m.narrative_turn.desc(), m.id.desc())
        .limit(BACKING_MAX)
    )
    best: Backing | None = None
    for row in rows:
        share = coverage(wanted, content_words(str(row.text)))
        if share >= COVERAGE and (best is None or share > best.coverage):
            best = Backing(str(row.id), int(row.narrative_turn), share)
    return best


async def check_recall(
    conn: AsyncConnection, story_id: str, speaker: str, *, turn: int, content: str
) -> RecallCheck:
    """``content``'s recall by ``speaker`` at ``turn``: grounded, or a confabulation."""
    found = await backing(conn, story_id, speaker, turn=turn, content=content)
    return RecallCheck(found is not None, found)


__all__ = [
    "CONFABULATION",
    "RECALL",
    "Backing",
    "RecallCheck",
    "RecallClaim",
    "backing",
    "check_recall",
    "find_recalls",
    "table",
]
