"""Model-free text-quality signals (NFR-QUAL-15, E-13; #158). Pure: no I/O, no model.

These are **signals**, never a gate: voice fidelity and plot adherence are outside the engine's
guarantee (NFR-QUAL-13). The eval harness (``evals.suites.e13_signals``) reports them per
configuration fingerprint.

- **Repetition** (``repetition``): a message's word 3-grams (lower-cased, punctuation stripped)
  against each of the previous ``REPETITION_WINDOW`` (3) final messages' 3-grams, regardless of
  speaker, as a Jaccard overlap; the message's overlap is the largest of the three (against
  their union, a verbatim repeat of one would be diluted by the others). A message is
  **repetitive** when the overlap is above ``REPETITION_THRESHOLD`` (0.5). Undefined
  (``None``) for the first ``REPETITION_WINDOW - 1`` messages of a story, which have too little
  before them to compare.
- **Repeated opening** (``repeated_opening``): the message's first ``OPENING_WORDS`` (5) words
  equal the first five of any of the previous three messages. Undefined for a story's first
  message and for a message shorter than five words.
- **Length accuracy** (``within_length``): a message's word count is within ``LENGTH_TOLERANCE``
  (±25 %) of its requested length's band (``LENGTH_BANDS``: ``short`` 40-80 words, ``medium``
  80-200, ``long`` 200-400): from 75 % of the band's lower bound to 125 % of its upper bound. A
  message with no requested length (or an integer token cap) has no band and is excluded.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

REPETITION_THRESHOLD = 0.5
REPETITION_WINDOW = 3
NGRAM = 3
OPENING_WORDS = 5
LENGTH_TOLERANCE = 0.25
LENGTH_BANDS: Mapping[str, tuple[int, int]] = {
    "short": (40, 80),
    "medium": (80, 200),
    "long": (200, 400),
}
"""Each named response length as a band of words (#57's ``length`` names)."""

RIGHT_QUOTE = chr(0x2019)
"""A typographic apostrophe, read as ``'``."""
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")


def words(text: str) -> list[str]:
    """``text``'s words, lower-cased, punctuation stripped (apostrophes kept inside a word)."""
    return _WORD.findall(text.casefold().replace(RIGHT_QUOTE, "'"))


def ngrams(text: str, n: int = NGRAM) -> set[tuple[str, ...]]:
    """``text``'s word ``n``-grams."""
    found = words(text)
    return {tuple(found[i : i + n]) for i in range(len(found) - n + 1)}


def jaccard(a: set[tuple[str, ...]], b: set[tuple[str, ...]]) -> float:
    """The size of the intersection over the size of the union (0 for two empty sets)."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def overlap(message: str, previous: Sequence[str]) -> float | None:
    """The largest 3-gram Jaccard overlap of ``message`` with one of the last
    ``REPETITION_WINDOW`` of ``previous`` (oldest first); ``None`` when fewer than
    ``REPETITION_WINDOW - 1`` precede it."""
    if len(previous) < REPETITION_WINDOW - 1:
        return None
    own = ngrams(message)
    return max(jaccard(own, ngrams(earlier)) for earlier in previous[-REPETITION_WINDOW:])


def repetition(message: str, previous: Sequence[str]) -> bool | None:
    """Whether ``message`` repeats the messages before it (module docstring); ``None`` when
    undefined."""
    found = overlap(message, previous)
    return None if found is None else found > REPETITION_THRESHOLD


def opening(text: str) -> tuple[str, ...] | None:
    """A message's first ``OPENING_WORDS`` words; ``None`` when it is shorter."""
    found = words(text)
    return tuple(found[:OPENING_WORDS]) if len(found) >= OPENING_WORDS else None


def repeated_opening(message: str, previous: Sequence[str]) -> bool | None:
    """Whether ``message`` opens like one of the previous three; ``None`` when undefined."""
    own = opening(message)
    if own is None or not previous:
        return None
    return any(opening(earlier) == own for earlier in previous[-REPETITION_WINDOW:])


def band(length: str | int | None) -> tuple[int, int] | None:
    """The word band a requested length allows (``None``: no named length requested)."""
    if not isinstance(length, str):
        return None
    found = LENGTH_BANDS.get(length)
    if found is None:
        return None
    low, high = found
    return round(low * (1 - LENGTH_TOLERANCE)), round(high * (1 + LENGTH_TOLERANCE))


def within_length(message: str, length: str | int | None) -> bool | None:
    """Whether ``message``'s word count is within its requested length's tolerance band;
    ``None`` with no named length."""
    allowed = band(length)
    if allowed is None:
        return None
    count = len(words(message))
    return allowed[0] <= count <= allowed[1]


__all__ = [
    "LENGTH_BANDS",
    "LENGTH_TOLERANCE",
    "NGRAM",
    "OPENING_WORDS",
    "REPETITION_THRESHOLD",
    "REPETITION_WINDOW",
    "band",
    "jaccard",
    "ngrams",
    "opening",
    "overlap",
    "repeated_opening",
    "repetition",
    "within_length",
    "words",
]
