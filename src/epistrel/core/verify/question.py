"""The ``question`` detector (#148's table style; #165): a sincere wh-question about an event is
evidence that the asker does not know that part of it (FR-COMMIT-14's "Bobby later sincerely
asking Jenni who was at her door").

``wh_slots(text)`` answers the thunk slots the text's question asks about (``subject``,
``object``), from its wh-word (``detectors/question.json``), when the text is a question: it ends
in ``?`` and a clause of it opens with a wh-word (after an optional vocative such as "Jenni,").
``asks_about(text, slot, terms)`` is whether the question asks about ``slot`` of an event
described by ``terms`` (the event's words: a thunk's sketch and its introducing text). It does
when one of the question's content words (stopwords dropped, a common suffix stemmed) is one of
the event's. Pure: no model, no world knowledge. It reads the text only to compare words and
logs nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from importlib import resources

_WORD = re.compile(r"[a-z][a-z']*")
_CLAUSE = re.compile(r"[,;:.!?]+")
_CURLY_APOSTROPHE = chr(0x2019)


@dataclass(frozen=True, slots=True)
class QuestionTable:
    wh_words: dict[str, frozenset[str]]
    suffixes: tuple[str, ...]
    stopwords: frozenset[str]


@cache
def table() -> QuestionTable:
    """The English question table (``core/verify/detectors/question.json``)."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/question.json")
    found = json.loads(source.read_text("utf-8"))
    return QuestionTable(
        wh_words={
            str(w): frozenset(str(s) for s in slots) for w, slots in found["wh_words"].items()
        },
        suffixes=tuple(sorted((str(s) for s in found["suffixes"]), key=lambda s: -len(s))),
        stopwords=frozenset(str(w) for w in found["stopwords"]),
    )


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold().replace(_CURLY_APOSTROPHE, "'"))


def stem(word: str) -> str:
    """``word`` without its possessive and its longest common suffix (one that leaves three
    letters)."""
    w = word.removesuffix("'s").rstrip("'")
    for suffix in table().suffixes:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def content(text: str) -> frozenset[str]:
    """The stems of ``text``'s content words (stopwords dropped)."""
    stop = table().stopwords
    return frozenset(stem(w) for w in _words(text) if w not in stop and len(w) > 1)


def wh_slots(text: str) -> frozenset[str]:
    """The slots ``text``'s wh-question asks about (empty: not a wh-question)."""
    if not text.rstrip().endswith("?"):
        return frozenset()
    found: set[str] = set()
    wh = table().wh_words
    for clause in _CLAUSE.split(text):
        words = _words(clause)
        if words and words[0] in wh:
            found |= wh[words[0]]
    return frozenset(found)


def asks_about(text: str, slot: str, terms: Iterable[str]) -> bool:
    """Whether ``text`` is a wh-question about ``slot`` of the event ``terms`` describe."""
    if slot not in wh_slots(text):
        return False
    event = frozenset().union(*(content(t) for t in terms)) if terms else frozenset()
    return bool(content(text) & event)


__all__ = ["QuestionTable", "asks_about", "content", "stem", "table", "wh_slots"]
