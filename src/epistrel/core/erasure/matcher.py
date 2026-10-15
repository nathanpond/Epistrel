"""The erasure matcher (ADR-154; #372): which spans of a text refer to a subject.

One deterministic matcher, shared by the subject-reference index (``core.erasure.index``),
erasure's redaction and its residue scan (``core.erasure.redact``, ``core.erasure.verify``) and
the write-path guard (``core.erasure.guard``). Nothing else matches names.

A subject is looked for by its **names** (its names and declared aliases) and its **parts** (the
words of a multi-word name, less titles, particles and the words another subject of the same
scope uses: ``index.with_name_parts``). Every occurrence, on the NFC form of the text, as a whole
word or phrase (Unicode word boundaries, any run of whitespace matching a space, longer terms
first; terms shorter than ``MIN_TERM_CHARS`` never match), is one ``Mention`` of a ``Rule``:

- **name** (certain): a multi-word name or alias, ignoring case;
- **part** (certain): a part, ignoring case but as written (no case-folding: "Strauß" never
  matches "STRAUSS"), each half of a hyphenated word being a part too;
- a single word on the bundled common-word list (``common_words.txt``; a one-word name or alias
  as much as a part, so a character called "Rose" does not erase "rose") matches only when it is
  capitalised: in the middle of a sentence it is certain (its ``name`` or ``part`` rule), and at
  the start of one it is a **candidate**, which an erasure redacts only when an operator
  confirms it, unless it is the whole text, punctuation aside (a name value such as a role's
  ``canonical``, not prose): that is certain. A lowercase common word never matches.

The **start of a sentence** (``sentence_start``) is the start of the text, of a line or of a
quotation (a quotation mark before the word), or a position after ``.``, ``!``, ``?``, ``…`` or
``:`` with only whitespace and brackets in between; the ``.`` of an abbreviated title (``Mr.``,
``Dr.``) does not end a sentence.

The write-path guard compares keyed hashes, never terms (``guard_terms``): each certain term
normalised (``normalized``: NFC, case-folded, whitespace runs as one space), a common word in
its capitalised mid-sentence form only (``CAPITALISED`` before it).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from functools import cache, lru_cache
from importlib import resources

MIN_TERM_CHARS = 2
"""Terms (names, aliases and parts) shorter than this are never matched in prose."""
NAME_PARTICLES = frozenset(
    {
        # articles, conjunctions and name particles
        *("a", "an", "the", "and", "of", "or", "y", "e", "al", "el", "bin", "ibn", "bint"),
        *("da", "das", "de", "del", "della", "dei", "den", "der", "des", "di", "do", "dos"),
        *("du", "la", "le", "les", "lo", "ter", "van", "von", "zu"),
        # titles, honorifics and generational suffixes
        *("mr", "mrs", "ms", "mx", "miss", "dr", "doctor", "prof", "professor", "sir", "dame"),
        *("lady", "lord", "st", "saint", "jr", "sr", "ii", "iii", "iv", "captain", "capt"),
        *("king", "queen", "prince", "princess", "father", "mother", "brother", "sister"),
        *("aunt", "uncle", "master", "mistress", "madam", "madame", "señor", "señora"),
    }
)
"""Words of a name that are never parts on their own (``index.name_parts``), case-folded."""
COMMON_WORDS_FILE = "common_words.txt"
CAPITALISED = "^"
"""Marks a common word's guard term: it is guarded only capitalised, mid-sentence."""
SNIPPET_CHARS = 30
"""How much text a candidate's snippet shows on each side of it."""

_QUOTES = frozenset("\"'\u201c\u201d\u2018\u2019\u00ab\u00bb\u2039\u203a\u201e\u201a")
_SKIPPED = _QUOTES | frozenset("()[]{}<>*_")
"""Quotes, brackets and emphasis marks: skipped when looking back for a sentence's end."""
_ENDS = frozenset(".!?…:")
_LINE_BREAKS = frozenset("\n\r\u2028\u2029\x85")


class Rule(StrEnum):
    """How a mention was matched: ``name`` and ``part`` are certain, ``candidate`` is not."""

    NAME = "name"
    PART = "part"
    CANDIDATE = "candidate"


CERTAIN = frozenset({Rule.NAME, Rule.PART})


@dataclass(frozen=True, slots=True)
class Mention:
    """One occurrence: its span in the NFC text and its rule."""

    start: int
    end: int
    rule: Rule

    @property
    def certain(self) -> bool:
        return self.rule in CERTAIN


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalized(value: str) -> str:
    """``value`` as the guard's hashes compare it: NFC, case-folded, whitespace runs as one
    space."""
    return " ".join(nfc(value).casefold().split())


def _collapsed(value: str) -> str:
    return " ".join(nfc(value).split())


@cache
def common_words() -> frozenset[str]:
    """The bundled common-word list (see the module docstring), case-folded."""
    source = resources.files("epistrel.core.erasure").joinpath(COMMON_WORDS_FILE)
    lines = source.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip().casefold() for line in lines if line.strip() and not line.startswith("#")
    )


def is_common(term: str) -> bool:
    """Whether ``term`` is one word on the common-word list."""
    word = _collapsed(term)
    return " " not in word and word.casefold() in common_words()


def sentence_start(text: str, start: int) -> bool:
    """Whether the word at ``start`` of ``text`` begins a sentence (see the module
    docstring)."""
    i = start - 1
    while i >= 0 and (text[i].isspace() or text[i] in _SKIPPED):
        if text[i] in _LINE_BREAKS or text[i] in _QUOTES:
            return True
        i -= 1
    if i < 0:
        return True
    if text[i] not in _ENDS:
        return False
    if text[i] == ".":
        j = i
        while j > 0 and text[j - 1].isalpha():
            j -= 1
        if text[j:i].casefold() in NAME_PARTICLES:
            return False
    return True


def capitalised(word: str) -> bool:
    return bool(word) and word[0].isupper()


_NOT_WORDS = re.compile(r"\W*")


def _whole(text: str, match: re.Match[str]) -> bool:
    """Whether the match is the whole text, punctuation aside: a name value, not prose."""
    return bool(
        _NOT_WORDS.fullmatch(text, 0, match.start()) and _NOT_WORDS.fullmatch(text, match.end())
    )


@dataclass(frozen=True, slots=True)
class _Term:
    text: str
    rule: Rule
    common: bool
    pattern: re.Pattern[str]


def _phrase(term: str) -> str:
    return r"\s+".join(re.escape(part) for part in term.split())


class Matcher:
    """Finds one subject's mentions (see the module docstring). Build it with ``matcher``,
    which caches one per set of terms."""

    def __init__(self, names: Iterable[str], parts: Iterable[str] = ()) -> None:
        found: dict[str, _Term] = {}
        for rule, terms in ((Rule.NAME, names), (Rule.PART, parts)):
            for raw in terms:
                if not isinstance(raw, str):
                    continue
                term = _collapsed(raw)
                if len(term) < MIN_TERM_CHARS or term.casefold() in found:
                    continue
                found[term.casefold()] = _Term(
                    term,
                    rule,
                    is_common(term),
                    re.compile(_phrase(term), re.IGNORECASE),
                )
        self._terms = sorted(found.values(), key=lambda t: (-len(t.text), t.text))
        self.names = tuple(t.text for t in self._terms if t.rule is Rule.NAME)
        self.parts = tuple(t.text for t in self._terms if t.rule is Rule.PART)
        self._pattern = (
            re.compile(
                rf"(?<!\w)(?:{'|'.join(_phrase(t.text) for t in self._terms)})(?!\w)",
                re.IGNORECASE,
            )
            if self._terms
            else None
        )

    @property
    def terms(self) -> tuple[str, ...]:
        """Every term, names and parts, longest first."""
        return tuple(t.text for t in self._terms)

    def _term(self, matched: str) -> _Term:
        for term in self._terms:
            if term.pattern.fullmatch(matched):
                return term
        raise LookupError(matched)  # pragma: no cover - the alternation matched one of them

    def mentions(self, value: str | None) -> list[Mention]:
        """Every mention in ``value`` (offsets in its NFC form), in order."""
        if not value or self._pattern is None:
            return []
        text = nfc(value)
        found: list[Mention] = []
        for match in self._pattern.finditer(text):
            term = self._term(match.group())
            rule = term.rule
            if term.common:
                if not capitalised(match.group()):
                    continue
                if sentence_start(text, match.start()) and not _whole(text, match):
                    rule = Rule.CANDIDATE
            found.append(Mention(match.start(), match.end(), rule))
        return found

    def certain(self, value: str | None) -> list[Mention]:
        return [m for m in self.mentions(value) if m.certain]

    def hits(self, value: str | None) -> bool:
        """Whether ``value`` certainly names the subject."""
        return any(m.certain for m in self.mentions(value))

    def finds(self, value: str | None) -> bool:
        """Whether ``value`` holds any mention, a candidate included."""
        return bool(self.mentions(value))

    def guard_terms(self) -> list[str]:
        """What the write-path guard hashes (see the module docstring), sorted."""
        return sorted(
            {
                f"{CAPITALISED}{normalized(t.text)}" if t.common else normalized(t.text)
                for t in self._terms
            }
        )


@lru_cache(maxsize=1024)
def _cached(names: tuple[str, ...], parts: tuple[str, ...]) -> Matcher:
    return Matcher(names, parts)


def matcher(names: Iterable[str], parts: Iterable[str] = ()) -> Matcher:
    """The ``Matcher`` of these terms (one per distinct set, cached)."""
    return _cached(tuple(names), tuple(parts))


def snippet(value: str, start: int, end: int) -> str:
    """A short piece of the NFC ``value`` around ``[start, end)``, whitespace runs as one
    space: a candidate's context for the operator. Returned only, never stored."""
    text = nfc(value)
    head = max(0, start - SNIPPET_CHARS)
    tail = min(len(text), end + SNIPPET_CHARS)
    piece = " ".join(text[head:tail].split())
    return f"{'…' if head else ''}{piece}{'…' if tail < len(text) else ''}"


def guard_spans(value: str) -> list[str]:
    """The capitalised mid-sentence words of ``value``, normalised and marked (``CAPITALISED``),
    as the guard compares them with a common word's term hash."""
    text = nfc(value)
    return [
        f"{CAPITALISED}{normalized(m.group())}"
        for m in re.finditer(r"\w+", text)
        if capitalised(m.group())
        and len(m.group()) >= MIN_TERM_CHARS
        and not sentence_start(text, m.start())
    ]


__all__ = [
    "CAPITALISED",
    "CERTAIN",
    "MIN_TERM_CHARS",
    "NAME_PARTICLES",
    "Matcher",
    "Mention",
    "Rule",
    "common_words",
    "guard_spans",
    "is_common",
    "matcher",
    "normalized",
    "sentence_start",
    "snippet",
]
