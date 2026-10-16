"""The erasure matcher (ADR-154; #372): which spans of a text refer to a subject.

One deterministic matcher, shared by the subject-reference index (``core.erasure.index``),
erasure's redaction and its residue scan (``core.erasure.redact``, ``core.erasure.verify``) and
the write-path guard (``core.erasure.guard``). Nothing else matches names.

A subject is looked for by its **names** (its names and declared aliases) and its **parts** (the
words of a multi-word name, less titles, particles and the words another subject of the same
scope uses: ``index.with_name_parts``). Every occurrence, on the NFC form of the text, as a whole
word or phrase (Unicode word boundaries, any run of whitespace matching a space, longer terms
first; terms shorter than ``MIN_TERM_CHARS`` never match), is one ``Mention`` of a ``Rule``.
Text and terms are compared **folded** (``fold``: full case-folding, then NFC again, so
"Strauß", "STRAUSS" and "Strauss" are one word, "İpek", "IPEK" and "i̇pek" another, "Ταΐς" and
"ΤΑΪ́Σ" a third, and ``ı`` is ``i``, so "YILMAZ" is "Yılmaz"; every dash, ``Pd`` and the minus
sign, as ``-``; a soft hyphen is no character at all: #384, #385, #395, #396, #397, ADR-156); a
mention's span is the original text's:

- **name** (certain): a multi-word name or alias, in any case;
- **part** (certain): a part, in any case, each half of a hyphenated word (any dash) being a
  part too;
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
normalised (``normalized``: folded as above, whitespace runs as one space, so the guard and the
matcher agree on case: #388), a common word in its capitalised mid-sentence form only
(``CAPITALISED`` before it).
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
DASHES = "".join(
    sorted(
        {chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Pd"}
        | {"\u2212", "\u2043"}
    )
)
"""Every dash (Unicode ``Pd``), the minus sign and the hyphen bullet (#397): one ``-`` when
folded, and each splits a hyphenated name into parts (``index.name_parts``)."""
SOFT_HYPHEN = "\u00ad"
"""A hyphenation hint, shown only at a line break: folded away, so "Wat\u00adson" is "watson",
and never a split (#395)."""
_FOLD_TABLE = str.maketrans({**dict.fromkeys(DASHES, "-"), "\u0131": "i", SOFT_HYPHEN: None})
"""Dashes as ``-``, dotless ``ı`` as ``i`` (the owner's decision on #396: "YILMAZ".casefold() is
"yilmaz"), the soft hyphen removed."""
_COMBINING_DOT = "\u0307"


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


@lru_cache(maxsize=4096)
def _fold_char(char: str) -> str:
    return char.casefold().replace("i" + _COMBINING_DOT, "i").translate(_FOLD_TABLE)


def _composed(
    out: list[str], starts: list[int], ends: list[int]
) -> tuple[str, list[int], list[int]]:
    """``out`` back in NFC (casefolding ``Ϊ`` + U+0301 gives ``ϊ`` + U+0301, which is ``ΐ``:
    #396), composed per starter and the combining marks after it; a cluster that changes maps
    each of its characters to the cluster's whole source range."""
    joined = "".join(out)
    if unicodedata.is_normalized("NFC", joined):
        return joined, starts, ends
    chars: list[str] = []
    new_starts: list[int] = []
    new_ends: list[int] = []
    i = 0
    while i < len(out):
        j = i + 1
        while j < len(out) and unicodedata.combining(out[j]):
            j += 1
        cluster = "".join(out[i:j])
        composed = unicodedata.normalize("NFC", cluster)
        if composed == cluster:
            chars += out[i:j]
            new_starts += starts[i:j]
            new_ends += ends[i:j]
        else:
            chars += composed
            new_starts += [starts[i]] * len(composed)
            new_ends += [max(ends[i:j])] * len(composed)
        i = j
    return "".join(chars), new_starts, new_ends


def _folded(text: str) -> tuple[str, list[int] | None, list[int] | None]:
    """The NFC ``text`` folded (see ``fold``), with each folded character's source start and
    end in ``text`` (``None``, ``None`` when every character folds to exactly one)."""
    simple = text.casefold().translate(_FOLD_TABLE)
    if (
        len(simple) == len(text)
        and _COMBINING_DOT not in text
        and unicodedata.is_normalized("NFC", simple)
    ):
        return simple, None, None
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for k, char in enumerate(text):
        if char == _COMBINING_DOT and out and out[-1] == "i":
            ends[-1] = k + 1  # "i" + a combining dot above folds as "i" ("İ" does too)
            continue
        for folded in _fold_char(char):  # a soft hyphen folds to nothing
            out.append(folded)
            starts.append(k)
            ends.append(k + 1)
    return _composed(out, starts, ends)


def fold(value: str) -> str:
    """``value`` as the matcher compares it: NFC, fully case-folded (``ß`` is ``ss``, ``İ`` is
    ``i``, as is ``i`` with a combining dot above, and ``ı`` is ``i``), NFC again (``ΐ``), every
    dash (``DASHES``) a ``-``, and no soft hyphen (ADR-156)."""
    return _folded(nfc(value))[0]


def normalized(value: str) -> str:
    """``value`` as the guard's hashes compare it: folded (``fold``), whitespace runs as one
    space."""
    return " ".join(fold(value).split())


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
    """Whether ``term`` is one word on the common-word list, compared folded (``fold``: a name
    "Kıt" is the common word "kit", ADR-156)."""
    word = _collapsed(term)
    return " " not in word and fold(word) in common_words()


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


def _whole(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` is the whole text, punctuation aside: a name value, not
    prose."""
    return bool(_NOT_WORDS.fullmatch(text, 0, start) and _NOT_WORDS.fullmatch(text, end))


@dataclass(frozen=True, slots=True)
class _Term:
    text: str
    folded: str
    rule: Rule
    common: bool


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
                folded = normalized(term)
                if len(term) < MIN_TERM_CHARS or folded in found:
                    continue
                found[folded] = _Term(term, folded, rule, is_common(term))
        self._terms = sorted(found.values(), key=lambda t: (-len(t.folded), t.folded))
        self._by_folded = found
        self.names = tuple(t.text for t in self._terms if t.rule is Rule.NAME)
        self.parts = tuple(t.text for t in self._terms if t.rule is Rule.PART)
        # Matched on the folded text, so no case flag: folding already made case one.
        self._pattern = (
            re.compile(rf"(?<!\w)(?:{'|'.join(_phrase(t.folded) for t in self._terms)})(?!\w)")
            if self._terms
            else None
        )

    @property
    def terms(self) -> tuple[str, ...]:
        """Every term, names and parts, longest first."""
        return tuple(t.text for t in self._terms)

    def mentions(self, value: str | None) -> list[Mention]:
        """Every mention in ``value`` (offsets in its NFC form), in order."""
        if not value or self._pattern is None:
            return []
        text = nfc(value)
        folded, starts, ends = _folded(text)
        found: list[Mention] = []
        for match in self._pattern.finditer(folded):
            a, b = match.span()
            # A combining mark is not ``\w``, so the match stops before one: the mention takes
            # the marks after it, so none is left behind ("ΤΑΪ́Σ́ x" is "[erased] x"; #410). A
            # mark still ends the word, as before: what follows it is not the mention's.
            while b < len(folded) and unicodedata.category(folded[b]).startswith("M"):
                b += 1
            if starts is None or ends is None:
                start, end = a, b
            else:
                # A match must cover whole source characters ("s" is not half of "ß").
                if (a and starts[a - 1] == starts[a]) or (
                    b < len(starts) and starts[b] == starts[b - 1]
                ):
                    continue
                start, end = starts[a], ends[b - 1]
            term = self._by_folded[" ".join(match.group().split())]
            rule = term.rule
            if term.common:
                if not capitalised(text[start:end]):
                    continue
                if sentence_start(text, start) and not _whole(text, start, end):
                    rule = Rule.CANDIDATE
            found.append(Mention(start, end, rule))
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
    "DASHES",
    "MIN_TERM_CHARS",
    "NAME_PARTICLES",
    "SOFT_HYPHEN",
    "Matcher",
    "Mention",
    "Rule",
    "common_words",
    "fold",
    "guard_spans",
    "is_common",
    "matcher",
    "normalized",
    "sentence_start",
    "snippet",
]
