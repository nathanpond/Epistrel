"""The inner-state check: narration or speech asserting what another character privately thinks
or feels (FR-TURN-3, NFR-PRIV-1, FR-VERIFY-14; #172). Pure: nothing here reads the database or
calls a model.

**What it finds.** A sentence holding a cue of the English lexicon (``detectors/inner_state.json``:
verbs of thought and feeling, adverb cues such as ``secretly``, cognition verbs such as
``realized``) whose **subject** is a character, and whose content matches one of that character's
private records (``core.verify.protected.PrivateRecord``: its ``think`` memories and this turn's
``think`` beats, and the statements of its beliefs). "He secretly thought she looked sad", where
the delivery driver's ``think`` holds that she seems sad, is a ``think_leak``; "He gives her a
sympathetic glance" carries no cue and is an observable action, which a player may legitimately
read an inference from.

- **The subject** is the nearest mention before the cue in its clause (a comma or a semicolon
  ends one), else the sentence's first mention or subject pronoun (a participle, "…, thinking
  she seemed sad", takes the sentence's subject). An object pronoun (``her``, ``him``) is never
  a subject. A subject pronoun resolves to the last named character before it, else to what the
  resolver made of it, else, on a ``do`` beat, to the actor.
- **Exempt**: the beat's own speaker in ``say`` (self-disclosure: "I thought you seemed sad")
  and the composed message's viewpoint character (``exempt``).
- **Content** is the sentence's content words (``content_words``: folded, stopwords, names and
  cues dropped, a light suffix stem) covered by a record's at ``COVERAGE`` or more. Paraphrase
  beyond this is caught only to the extent the extraction model catches it (NFR-PRIV-4).

``find`` returns ``InnerStateHit``s (offsets, the subject, the record matched); the detector
turns each into a claim (``core.verify.detect``, category ``inner_state``) that the verifier
marks ``think_leak`` and the repair ladder narrows. No text is logged (invariant 3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from epistrel.core.erasure.matcher import fold
from epistrel.core.resolution.types import Mention, MentionKind
from epistrel.core.verify.protected import PrivateRecord, folded_words

COVERAGE = 0.5
"""The share of a sentence's content words a private record must cover for a hit."""
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
_CLAUSE = re.compile(r"[,;:—]")
_SUFFIXES = ("ing", "ed", "es", "s")


@cache
def lexicon() -> Mapping[str, Any]:
    """``detectors/inner_state.json``."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/inner_state.json")
    loaded: dict[str, Any] = json.loads(source.read_text("utf-8"))
    return loaded


@cache
def _cues() -> tuple[frozenset[str], tuple[tuple[str, ...], ...]]:
    table = lexicon()
    words: set[str] = set()
    phrases: list[tuple[str, ...]] = []
    for entry in (*table["verbs"], *table["adverbs"], *table["cognition"]):
        parts = tuple(fold(entry).split())
        if len(parts) == 1:
            words.add(parts[0])
        else:
            phrases.append(parts)
    return frozenset(words), tuple(phrases)


def stem(word: str) -> str:
    """A light English suffix stem (``seemed``, ``seems`` and ``seeming`` are ``seem``)."""
    word = word.removesuffix("'s")
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def content_words(text: str, *, drop: frozenset[str] = frozenset()) -> set[str]:
    """``text``'s content words: folded, stemmed, without stopwords, cues or ``drop``."""
    stop = frozenset(fold(w) for w in lexicon()["stopwords"])
    cues, _ = _cues()
    return {
        stem(w)
        for _, _, w in folded_words(text)
        if w not in stop and w not in cues and w not in drop and not w.isdigit()
    }


def coverage(claim: set[str], record: set[str]) -> float:
    """The share of ``claim``'s words ``record`` holds (0 for an empty claim)."""
    return len(claim & record) / len(claim) if claim else 0.0


@dataclass(frozen=True, slots=True)
class InnerStateHit:
    """One inner-state sentence matching a private record: its span, subject and record."""

    start: int
    end: int
    subject: str
    record: PrivateRecord


def _cue_at(words: Sequence[str]) -> int | None:
    cues, phrases = _cues()
    for i, w in enumerate(words):
        if w in cues:
            return i
        if any(tuple(words[i : i + len(p)]) == p for p in phrases):
            return i
    return None


def find(
    text: str,
    mentions: Sequence[Mention],
    records: Sequence[PrivateRecord],
    *,
    channel: str,
    speaker: str,
    actor: str | None = None,
    exempt: frozenset[str] = frozenset(),
) -> list[InnerStateHit]:
    """The inner-state sentences of ``text`` (see the module docstring). ``mentions`` are the
    text's resolved mentions, ``records`` the private records of the story's characters."""
    if not text or not records:
        return []
    table = lexicon()
    subjects = frozenset(fold(w) for w in table["subject_pronouns"])
    objects = frozenset(fold(w) for w in table["object_pronouns"])
    by_holder: dict[str, list[tuple[PrivateRecord, set[str]]]] = {}
    for record in records:
        by_holder.setdefault(record.holder, []).append((record, content_words(record.text)))
    named = [m for m in mentions if m.entity_id is not None and m.kind is not MentionKind.GROUP]
    found: list[InnerStateHit] = []
    for sentence in _SENTENCE.finditer(text):
        s_start, s_end = sentence.span()
        tokens = [t for t in folded_words(text) if s_start <= t[0] < s_end]
        at = _cue_at([w for _, _, w in tokens])
        if at is None:
            continue
        subject = _subject(
            text, tokens, at, named, subjects, objects, s_start, channel=channel, actor=actor
        )
        if subject is None or subject in exempt or (channel == "say" and subject == speaker):
            continue
        holder_records = by_holder.get(subject)
        if not holder_records:
            continue
        drop = frozenset(
            w
            for m in named
            if s_start <= m.span[0] < s_end
            for _, _, w in folded_words(text[m.span[0] : m.span[1]])
        )
        claim = content_words(text[s_start:s_end], drop=drop)
        for record, words in holder_records:
            if coverage(claim, words) >= COVERAGE:
                found.append(InnerStateHit(s_start, s_end, subject, record))
                break
    return found


def _subject(
    text: str,
    tokens: Sequence[tuple[int, int, str]],
    at: int,
    named: Sequence[Mention],
    subjects: frozenset[str],
    objects: frozenset[str],
    s_start: int,
    *,
    channel: str,
    actor: str | None,
) -> str | None:
    """The cue's subject (see the module docstring)."""
    cue = tokens[at][0]
    clause_start = s_start
    for m in _CLAUSE.finditer(text, s_start, cue):
        clause_start = m.end()

    def candidates(lo: int, hi: int) -> list[tuple[int, str | None]]:
        out: list[tuple[int, str | None]] = []
        for m in named:
            if lo <= m.span[0] < hi and fold(text[m.span[0] : m.span[1]]) not in objects:
                if m.kind is MentionKind.PRONOUN:
                    out.append((m.span[0], _pronoun(text, m.span[0], named, m, channel, actor)))
                else:
                    out.append((m.span[0], m.referent))
        for start, _, word in tokens:
            if lo <= start < hi and word in subjects and not any(c[0] == start for c in out):
                out.append((start, _pronoun(text, start, named, None, channel, actor)))
        return sorted(out)

    near = candidates(clause_start, cue)
    if near:
        return near[-1][1]
    first = candidates(s_start, cue)
    return first[0][1] if first else None


def _pronoun(
    text: str,
    at: int,
    named: Sequence[Mention],
    resolved: Mention | None,
    channel: str,
    actor: str | None,
) -> str | None:
    before = [
        m
        for m in named
        if m.span[1] <= at and m.kind is not MentionKind.PRONOUN and m.referent is not None
    ]
    if before:
        return max(before, key=lambda m: m.span[0]).referent
    if channel == "do" and actor is not None:
        return actor  # a do beat's opening pronoun is its actor (#148's convention)
    return None if resolved is None else resolved.referent


__all__ = [
    "COVERAGE",
    "InnerStateHit",
    "content_words",
    "coverage",
    "find",
    "lexicon",
    "stem",
]
