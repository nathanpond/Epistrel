"""Copied lines in a structured turn with several primaries (FR-MULTI-8,11; #1013).

A later primary's context holds the beats the earlier primaries staged, and a model sometimes
answers with one of them word for word: two characters say the same line. Nothing else compares
a render's beats with what other speakers already said, so this check does.

**What is compared** (``find``). Each ``say`` and ``do`` beat of a character (a narrated
reaction is its reactor's, ``narrated_by`` set) is compared with every earlier ``say`` and
``do`` beat of **another** speaker in the turn, as it now stands (an earlier primary's staged,
repaired text; within one render, the beats before it, a primary's narrated reactions
included). A character repeating its own line is not a copy. Never checked and never a source:
the player's input (beat 1), the player's ``co_author`` narration (``narrated_by =
player``), a narrator beat, a fallback line, an adjudication's attempt line (``attempt``) and
the beats of a render released as the safe fallback (``exempt``).

**What counts** (``normalize``, ``sentence_keys``). The beat's sentences are
``core.repair.rungs.sentences`` (the narrowing rung's, so both agree on what a sentence is).
A sentence is normalized: NFKC, curly quotes and apostrophes made straight, case-folded,
hyphens and dashes made spaces, every other punctuation mark stripped (an apostrophe inside a
word stays), white space collapsed. A sentence is a copy when its normalized form equals a
normalized sentence of an earlier beat of another speaker **and** it has at least
``MIN_WORDS`` words (a ``[\\w']+`` run of the normalized text: "that's" is one word,
"well-known" two). "Yeah." or "I hear you." from two characters is no copy. A sentence that is
also a sentence of the player's input is never a copy (two characters echoing the player are
not flagged against each other).

**What it makes** (``claims_of``). One checked claim per copied sentence, modelled on the State
findings' (``effects.claims_of``): verdict ``uncertain``, ``VerifyResult.copied_from = (source
beat, source speaker id)`` (so ``needs_repair`` and repair tag ``copied_line``), claim id
``{op}:copied:{beat}:{n}``, its span the copied sentence. It goes to the repair ladder with the
beat's other violations (``orchestrator.repair.ladder``: one regeneration, else the beat is
dropped) and never enters ``turn_report.verify``. Pure; no I/O, and no text in any log
(invariant 3).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field

from epistrel.core.repair.rungs import sentences
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.verify.detect import DetectedClaim
from epistrel.core.verify.other_agent import PLAYER_NARRATOR
from epistrel.core.verify.rules import Verdict, VerdictKind
from epistrel.orchestrator.verify.extraction import CheckedClaim
from epistrel.orchestrator.verify.verifier import COPIED_LINE, VerifyResult

MIN_WORDS = 5
"""The fewest words a copied sentence has; shorter everyday replies are never copies."""
CHECKED_KINDS = frozenset({"say", "do"})
"""The beat kinds the check compares."""

_QUOTES = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "`": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u00ab": '"',
        "\u00bb": '"',
    }
)
_DASHES = re.compile("[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_LOOSE_APOSTROPHE = re.compile(r"(?<![\w])'|'(?![\w])")
_PUNCTUATION = re.compile(r"[^\w\s']")
_WORD = re.compile(r"[\w']+")


def normalize(sentence: str) -> str:
    """``sentence`` as the check compares it (see the module docstring)."""
    text = unicodedata.normalize("NFKC", sentence).translate(_QUOTES).casefold()
    text = _DASHES.sub(" ", text)
    text = _LOOSE_APOSTROPHE.sub(" ", text)
    text = _PUNCTUATION.sub(" ", text)
    return " ".join(text.split())


def words(normalized: str) -> int:
    """How many words a normalized sentence has."""
    return len(_WORD.findall(normalized))


def sentence_keys(text: str) -> list[tuple[int, int, str]]:
    """Each sentence of ``text``: its ``[start, end)`` span and its normalized form."""
    return [(s, e, normalize(text[s:e])) for s, e in sentences(text)]


def checked(beat: TurnBeat, exempt: Collection[int] = ()) -> bool:
    """Whether ``beat`` is compared at all, as a copy or as a source (see the module
    docstring)."""
    return (
        beat.beat > 1
        and beat.kind in CHECKED_KINDS
        and beat.character_id is not None
        and not beat.narrator
        and beat.narrated_by != PLAYER_NARRATOR
        and not beat.attempt
        and beat.beat not in exempt
    )


@dataclass(frozen=True, slots=True)
class Copy:
    """One copied sentence: the beat and span it is at, and the earlier beat and speaker it
    repeats."""

    beat: int
    span: tuple[int, int]
    source_beat: int
    source_speaker: str


def find(
    beat: TurnBeat,
    earlier: Iterable[TurnBeat],
    *,
    player: Sequence[str] = (),
    exempt: Collection[int] = (),
) -> list[Copy]:
    """The copied sentences of ``beat`` against ``earlier`` (only those before it, of another
    speaker, are read; ``player``: the player's input texts; ``exempt``: beat numbers never
    compared). The first earlier beat holding a sentence is its source."""
    if not checked(beat, exempt):
        return []
    echoes = {k for text in player for _, _, k in sentence_keys(text)}
    sources: dict[str, tuple[int, str]] = {}
    for other in sorted(earlier, key=lambda b: b.beat):
        if (
            other.beat >= beat.beat
            or not checked(other, exempt)
            or other.character_id == beat.character_id
        ):
            continue
        for _, _, key in sentence_keys(other.text):
            sources.setdefault(key, (other.beat, str(other.character_id)))
    found: list[Copy] = []
    for start, end, key in sentence_keys(beat.text):
        if words(key) < MIN_WORDS or key in echoes or key not in sources:
            continue
        source_beat, speaker = sources[key]
        found.append(Copy(beat.beat, (start, end), source_beat, speaker))
    return found


@dataclass(slots=True)
class Scope:
    """A turn's copy check (with several primaries): the player's input texts and the beat
    numbers never compared (``exempt``: a render released as the safe fallback)."""

    player: tuple[str, ...] = ()
    exempt: set[int] = field(default_factory=set)


def claims_of(found: Sequence[Copy], beat: TurnBeat, operation_id: str) -> list[CheckedClaim]:
    """Each copy as a checked claim of ``beat`` (see the module docstring)."""
    return [
        CheckedClaim(
            claim=DetectedClaim(
                span=(c.beat, c.span[0], c.span[1]),
                layer="detector",
                category="extracted",
                channel=beat.kind,
                speaker=str(beat.character_id),
            ),
            result=VerifyResult(
                claim=None,
                verdict=Verdict(VerdictKind.UNCERTAIN),
                kind=VerdictKind.UNCERTAIN,
                copied_from=(c.source_beat, c.source_speaker),
            ),
            claim_id=f"{operation_id}:copied:{c.beat}:{n}",
        )
        for n, c in enumerate(found)
    ]


def is_copy(claim: CheckedClaim) -> bool:
    """Whether ``claim`` is a copy check's (``claims_of``)."""
    return claim.result is not None and claim.result.copied_from is not None


__all__ = [
    "CHECKED_KINDS",
    "COPIED_LINE",
    "MIN_WORDS",
    "Copy",
    "Scope",
    "checked",
    "claims_of",
    "find",
    "is_copy",
    "normalize",
    "sentence_keys",
    "words",
]
