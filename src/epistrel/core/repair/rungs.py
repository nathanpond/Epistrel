"""The repair ladder's deterministic rungs and classification (FR-REPAIR-1,4; #151).

**Spans.** A repair works on sentences (``sentences``): a sentence ends at ``.``, ``!``, ``?``
or ``…`` (with any closing quotes or brackets) followed by white space, or at the end of the
text. A claim's span in the beat is its detector or extraction span; a declared claim spans
the whole beat, so its sentence is the first one holding the surface of its object (else of its
subject, ``locate``).

**Rung 1, narrowing** (``narrow``). The sentences carrying the violating claims are dropped
and the rest kept, in order, joined by one space: model-free. It applies only when at least one
sentence is left and every violation was located; a partly covered claim whose remainder is
contradicted is narrowed the same way (its sentence goes, the canon that covered the rest
stands).

**Rung 2, substitution** (``substitute``). The claim's object surface is replaced, as a whole
word and in every place it occurs, by the canon value's surface (an entity's canonical name or
a literal's text). The Orchestrator applies it only to a functional predicate whose verdict
cites exactly one canon fact.

**Constraints** (``Constraint``) are typed ``{kind, text, cites}``: ``do_not_assert`` (a
contradicted or undecided claim: "do not assert that …"; an unpermitted one: "do not introduce
facts about …", with the reason), ``avoid_binding`` (#154) and ``policy`` (#68's output
policies).

**Classification** (``classify``, FR-REPAIR-4). From the rung that was accepted and the
neighborhood's size (its facts, memories excluded): rung 4 (the safe fallback), or a
neighborhood above ``cascade_threshold`` (deployment setting, default 5), is ``cascade``; rung
2 with a neighborhood of two facts or more is ``substitution``; everything else (rung 1, rung 2
on a one-fact neighborhood, a regeneration on a neighborhood of five facts or fewer) is
``cosmetic``.

Pure: no I/O, no story text in any log.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

ConstraintKind = Literal["do_not_assert", "avoid_binding", "policy"]

DEFAULT_CASCADE_THRESHOLD = 5
"""A neighborhood of more facts than this is a ``cascade`` (FR-REPAIR-4)."""

_SENTENCE_END = re.compile(r"[.!?\u2026]+[\"'\u201d\u2019)\]]*(?=\s|$)")
_WORD = r"[\w']"


NARROWING, SUBSTITUTION, REGENERATION, FALLBACK = 1, 2, 3, 4
RUNGS = (NARROWING, SUBSTITUTION, REGENERATION, FALLBACK)
"""The ladder's rungs, in order: narrowing, substitution, regeneration, the safe fallback."""


class RepairClass(StrEnum):
    """FR-REPAIR-4's signal."""

    COSMETIC = "cosmetic"
    SUBSTITUTION = "substitution"
    CASCADE = "cascade"


@dataclass(frozen=True, slots=True)
class Constraint:
    """One constraint a regeneration keeps (see the module docstring)."""

    kind: ConstraintKind
    text: str
    cites: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "cites": list(self.cites)}


def sentences(text: str) -> list[tuple[int, int]]:
    """The ``[start, end)`` span of each sentence of ``text``, leading space excluded."""
    found: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if text[start:end].strip():
            found.append(_trimmed(text, start, end))
        start = end
    if text[start:].strip():
        found.append(_trimmed(text, start, len(text)))
    return found


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _word_pattern(surface: str) -> re.Pattern[str] | None:
    words = surface.split()
    if not words:
        return None
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!{_WORD}){body}(?!{_WORD})", re.IGNORECASE)


def locate(text: str, surfaces: Sequence[str | None]) -> tuple[int, int] | None:
    """The span of the first of ``surfaces`` found in ``text`` as a whole word (``None`` when
    none is)."""
    for surface in surfaces:
        pattern = _word_pattern(surface or "")
        if pattern is None:
            continue
        found = pattern.search(text)
        if found is not None:
            return found.start(), found.end()
    return None


def narrow(text: str, spans: Sequence[tuple[int, int]]) -> str | None:
    """Rung 1: ``text`` without the sentences any of ``spans`` touches, or ``None`` when that
    would leave nothing, or change nothing."""
    if not spans:
        return None
    parts = sentences(text)
    kept = [(s, e) for s, e in parts if not any(a < e and s < b for a, b in spans)]
    if not kept or len(kept) == len(parts):
        return None
    return " ".join(text[s:e] for s, e in kept)


def substitute(text: str, surface: str, replacement: str) -> str | None:
    """Rung 2: ``text`` with every whole-word ``surface`` replaced by ``replacement``, or
    ``None`` when ``surface`` is not there (or nothing would change)."""
    pattern = _word_pattern(surface)
    if pattern is None or not replacement.strip():
        return None
    found, count = pattern.subn(lambda _: replacement, text)
    if count == 0 or found == text:
        return None
    return found


def classify(
    rung: int, neighborhood_size: int, cascade_threshold: int = DEFAULT_CASCADE_THRESHOLD
) -> RepairClass:
    """FR-REPAIR-4's class of a repair accepted at ``rung`` (see the module docstring)."""
    if rung == FALLBACK or neighborhood_size > cascade_threshold:
        return RepairClass.CASCADE
    if rung == SUBSTITUTION and neighborhood_size >= 2:
        return RepairClass.SUBSTITUTION
    return RepairClass.COSMETIC


__all__ = [
    "DEFAULT_CASCADE_THRESHOLD",
    "FALLBACK",
    "NARROWING",
    "REGENERATION",
    "RUNGS",
    "SUBSTITUTION",
    "Constraint",
    "ConstraintKind",
    "RepairClass",
    "classify",
    "locate",
    "narrow",
    "sentences",
    "substitute",
]
