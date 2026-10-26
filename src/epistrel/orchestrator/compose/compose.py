"""The composition of a structured turn's message: a constrained, verified polish of its
verified beats, and the deterministic assembly it falls back to (FR-RESP-8, FR-VERIFY-14,
FR-RESP-12, NFR-PERF-6,7; docs/02 §8; #152).

``compose(beats, viewpoint, budget, composer=…)`` takes the turn's beats **as the output
viewpoint receives them** (``core.turns.assembly.viewpoint_beats``: a character viewpoint gets
every beat less ``think`` and ``latent``, ``omniscient`` all of them; #177: where perception is
on, the audience filter's beats, ``core.epistemic.audience``: a beat the viewpoint perceived only
the envelope of is its envelope, assembled as an action, ``*Jenni whispers something to
Marcus*``, and an unperceived beat is not given at all) and answers a ``Composition`` ``{text,
mode: polished|deterministic, check}``. The beats the filter withheld (``Composer.withheld``:
the content behind an envelope, an eavesdropper's unheard words, every unperceived beat) are
what the check holds the text to never disclose. The structured pipeline calls
it after the repair ladder (#151), on the post-repair beats, before the turn's single commit
(an ordinary turn commits its beats and its response together, docs/02 §7, FR-EXT-16).

**The polish** (``polish``): the ``generation`` purpose with the ``composition`` template
(``composition.j2``: the rating guidance first, FR-RATE-4) through ``structured_call`` with a
``{text}`` schema (``ComposedMessage``), given only the viewpoint's beats (index, kind, actor's
name, text) and their deterministic assembly as the draft. The request's prompt parameters
apply; ``max_tokens`` is twice the draft's token estimate. It runs only when the profile's
polish pass is on (``repair.polish.enabled``, the switch #106's module and Lite's output-policy
regenerations share), the turn has more than one beat, and the budget's unreserved remainder
can pay for it and its re-check by every model-assisted output policy; under budget pressure
it is the first step skipped (NFR-PERF-7 (1): ``polish_skipped_budget``).

**The check** (``check_composition``): first a length guard (at most ``LENGTH_FACTOR`` times
the draft, and no longer than the requested length or the draft, whichever is longer, #464:
``longest_polish``), then #68's output gate on the polished text, then #148's claim
detection on it (``ClaimDetector.message``, as the viewpoint's character, ``narrator`` when
omniscient; the model layer always runs when the story's verification level enables it, the
call the turn reserved), then ``trace``:

- **alignment**: the text is cut into units (each quotation, and each sentence outside
  quotes) and each unit is aligned to the beat whose words share its longest common run of
  content words (names, speech verbs and labels aside), when that run is at least
  ``ALIGN_THRESHOLD`` of the unit's content words; other units are connective text;
- **traceability**: every detected claim traces to a viewpoint beat: either it lies in a unit
  aligned to a beat and its words are that beat's own words, or its proposition has a beat
  proposition's shape or is covered by one (#140's coverage rule, ``supported``: a narrowed
  restatement traces, a wider one does not). A beat's propositions are all it asserts (its
  declared ones, the claims verified new and the uncertain ones it surfaced). A claim with no
  proposition is untraced unless it is a beat's own words (fail closed). The bare name of a
  viewpoint beat's actor (a speaker cue, "Bobby says.") is detected in a placed scene as that
  actor's ``present_at`` of the scene; it traces to the actor's own beat;
- **attribution**: a speaker cue (``Name:``, ``Name says``, ``said Name``) on a unit must name
  its ``say`` beat's actor, a ``do`` unit opening with a name must name its actor, narration
  carries no speaker cue, a connective unit's cue must name a speaker of the turn, and an
  omniscient thought keeps its ``[Name thinks]`` label (a latent its ``[latent]``);
- **no private content** (character viewpoint): no unit aligns to a ``think`` or ``latent``
  beat of the turn, no unit shares a 4-word shingle with one (other than the shingles the
  observable beats share), and no claim traces only to one (``think_leak``); nor does any
  claim detection marked as leaking a private thought (#172: another character's stored or
  turn-local ``think`` repeated, or an inner state narrated, ``core.verify.inner_state``);
- **no withheld content** (#177): no unit aligns to a withheld beat, no unit shares a 4-word
  shingle with one (other than those the returned beats share), and no claim traces only to
  one (``fidelity_leak``): a sentence about what the viewpoint only saw the envelope of, or did
  not perceive, fails;
- **completeness**: every viewpoint beat is aligned to a unit, in turn order.

Failures are ``{kind, span, beat}``: ``untraced``, ``attribution``, ``think_leak``,
``fidelity_leak``, ``omitted``, ``order`` or ``length`` (spans are offsets into the composed
text; no text).

**Any failure** releases the deterministic assembly (``deterministic_assembly``) with
``degradation: ["composition_fallback"]``: no second polish attempt. So does a polish the model
could not answer (a fault, or an answer still invalid after #53's retries) and a polished text
that breaks an output policy. The assembly makes no model call: it is checked by layers (a) and
(b) only (for the report) and by the deterministic output policies; in the improbable case that
it breaks one, the character's safe fallback line is released. A one-beat turn is assembled
without a polish (``mode: deterministic``, no degradation entry).

**The budget**: the composed message's detection call (C, one when the story's verification
runs the model layer) is reserved when the turn is planned (``reserve_detection``) so optional
work cannot spend it, and released when the deterministic assembly is used (it adds no new
text, NFR-PERF-6). #156 formalises the turn's reservations.

``Composition.report()`` is ``turn_report.composition``: ``{mode, check: {claims, traced,
failures}, polished, cause, detection_reserved, detection_released}``, ids, spans and codes
only (invariant 3). A rejected polish is recorded by the pipeline as an ``audit`` event
``composition_rejected {text, failures}``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.propositions.model import Polarity, Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import PRESENT_AT
from epistrel.core.turns.assembly import (
    PRIVATE_KINDS,
    Beat,
    deterministic_assembly,
    viewpoint_beats,
)
from epistrel.core.turns.beats import AssertedClaim
from epistrel.core.verify.detect import DetectedClaim, shape_of
from epistrel.core.verify.rules import Record, VerdictKind, check
from epistrel.core.viewpoint import is_omniscient
from epistrel.observability.logging import get_logger
from epistrel.orchestrator.budget import COMPOSED, POLISH, TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.errors import ModelError
from epistrel.orchestrator.models.registry import GENERATION, ComposeBeat, GenerationInputs
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.verify.extraction import DetectionOutcome

log = get_logger(__name__)

COMPOSITION = "composition"
"""The ``generation`` template variant of the polish (``composition.j2``)."""
COMPOSITION_TEMPLATE = "composition.j2"
Mode = Literal["polished", "deterministic"]
POLISHED: Mode = "polished"
DETERMINISTIC: Mode = "deterministic"
COMPOSITION_FALLBACK = "composition_fallback"
"""Degradation: the polished composition failed its check (or could not be had) and the
deterministic assembly of the verified beats was released."""
POLISH_SKIPPED_BUDGET = "polish_skipped_budget"
"""Degradation (NFR-PERF-7 (1)): the budget could not pay for the polish pass, so the
deterministic assembly was released without one."""
LENGTH_FACTOR = 1.5
"""The longest polished text accepted, as a multiple of the draft's length."""
ALIGN_THRESHOLD = 0.6
"""The share of a unit's content words a beat must share in one run to align with it."""
SHINGLE = 4
"""The word shingle a private thought is matched by."""
MIN_POLISH_TOKENS = 64
DRAFT_MAX = 40_000

FailureKind = Literal[
    "untraced", "attribution", "think_leak", "fidelity_leak", "omitted", "order", "length"
]
Cause = Literal["check", "model", "output_policy", "length"]

SPEECH_VERBS = frozenset(
    {
        "say",
        "says",
        "said",
        "ask",
        "asks",
        "asked",
        "reply",
        "replies",
        "replied",
        "answer",
        "answers",
        "answered",
        "whisper",
        "whispers",
        "whispered",
        "shout",
        "shouts",
        "shouted",
        "mutter",
        "mutters",
        "muttered",
        "murmur",
        "murmurs",
        "murmured",
        "add",
        "adds",
        "added",
        "tell",
        "tells",
        "told",
        "call",
        "calls",
        "called",
        "cry",
        "cries",
        "cried",
        "exclaim",
        "exclaims",
        "exclaimed",
        "respond",
        "responds",
        "responded",
        "continue",
        "continues",
        "continued",
        "laugh",
        "laughs",
        "laughed",
        "snap",
        "snaps",
        "snapped",
    }
)
"""The speaker-cue lexicon: a name next to one of these is the unit's speaker."""
LABEL_WORDS = frozenset({"thinks", "latent"})

_APOSTROPHES = str.maketrans({chr(0x2019): "'", chr(0x2018): "'"})
_WORD = re.compile(r"[0-9a-z]+(?:'[0-9a-z]+)*")
_CLOSERS = '"*)]' + chr(0x201D) + chr(0x2019)
_SENTENCE_END = re.compile(r"[.!?]+[" + re.escape(_CLOSERS) + r"]*(?=\s|$)|\n+")
_QUOTE = re.compile(r'"[^"\n]+"|' + chr(0x201C) + "[^" + chr(0x201D) + r"\n]+" + chr(0x201D))
_THINK_LABEL = re.compile(r"\[\s*([^\]\n]{1,80}?)\s+thinks\s*\]", re.IGNORECASE)
_LATENT_LABEL = re.compile(r"\[\s*latent\s*\]", re.IGNORECASE)
_BREAK = re.compile(r"[.!?\n\"" + chr(0x201C) + chr(0x201D) + "]")


# -- the polish's schema ------------------------------------------------------------------------


class ComposedMessage(BaseModel):
    """The polish's answer (``response_schema``): the composed message."""

    model_config = ConfigDict(extra="forbid")

    stub_script: ClassVar[str] = COMPOSITION_TEMPLATE
    """The stub adapter answers this schema's calls from scripts keyed by the template
    (``stub.script("composition.j2", …)``), never from the ``generation`` queue."""

    text: str = Field(min_length=1, max_length=DRAFT_MAX)

    @classmethod
    def stub_example(cls, digest: str, messages: Sequence[Message]) -> dict[str, Any]:
        """The stub's default: the draft as given, a polish that changes nothing."""
        del digest
        prompt = "\n".join(m.content for m in messages if m.role == "user")
        found = re.search(r"<draft>\n(.*)\n</draft>", prompt, re.DOTALL)
        return {"text": found.group(1) if found and found.group(1).strip() else "..."}


# -- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Failure:
    """One way a composition failed its check: the kind, where (offsets into the composed
    text) and the beat it concerns."""

    kind: FailureKind
    span: tuple[int, int] | None = None
    beat: int | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "span": None if self.span is None else list(self.span),
            "beat": self.beat,
        }


@dataclass(frozen=True, slots=True)
class CompositionCheck:
    """What the check found: the claims detected, how many traced, and the failures."""

    claims: int = 0
    traced: int = 0
    failures: tuple[Failure, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures

    def report(self) -> dict[str, Any]:
        return {
            "claims": self.claims,
            "traced": self.traced,
            "failures": [f.payload() for f in self.failures],
        }


@dataclass(frozen=True, slots=True)
class Composition:
    """The composed message and how it was made (see the module docstring)."""

    text: str
    mode: Mode
    check: CompositionCheck
    degradation: tuple[str, ...] = ()
    polished: bool = False
    """Whether the polish was called."""
    cause: Cause | None = None
    """Why a polish was not released."""
    rejected_text: str | None = None
    """The polished text the check rejected (``composition_rejected``'s audit record)."""
    rejected: CompositionCheck | None = None
    detection: DetectionOutcome | None = None
    """The released message's detection (``turn_report.detection.message``)."""
    detection_reserved: bool = False
    detection_released: bool = False
    safe_fallback: bool = False
    """The assembly broke a deterministic output policy and the safe fallback line was
    released instead."""

    def report(self) -> dict[str, Any]:
        """``turn_report.composition``: ids, spans and codes only."""
        return {
            "mode": self.mode,
            "check": self.check.report(),
            "polished": self.polished,
            "cause": self.cause,
            "rejected": None if self.rejected is None else self.rejected.report(),
            "detection_reserved": self.detection_reserved,
            "detection_released": self.detection_released,
            "safe_fallback": self.safe_fallback,
        }


# -- units and alignment --------------------------------------------------------------------------


class VerifiedBeat(Beat, Protocol):
    """A beat with the claims it asserts (``core.turns.beats.TurnBeat``)."""

    @property
    def claims(self) -> tuple[AssertedClaim, ...]: ...


def _norm(text: str) -> str:
    return text.casefold().translate(_APOSTROPHES)


def words_of(text: str) -> list[str]:
    """``text``'s words, case-folded (apostrophes kept inside a word)."""
    return _WORD.findall(_norm(text))


class NameIndex:
    """The cast's names as word sequences, longest first, to find them in a text."""

    def __init__(self, names: Mapping[str, str]) -> None:
        found: list[tuple[tuple[str, ...], str]] = []
        for cid, name in names.items():
            tokens = tuple(words_of(name))
            if tokens:
                found.append((tokens, cid))
        self.names = sorted(found, key=lambda t: -len(t[0]))

    def find(self, words: Sequence[str]) -> list[tuple[int, int, str]]:
        """``(index, length, character id)`` of each name in ``words``, left to right."""
        found: list[tuple[int, int, str]] = []
        i = 0
        while i < len(words):
            for tokens, cid in self.names:
                if tuple(words[i : i + len(tokens)]) == tokens:
                    found.append((i, len(tokens), cid))
                    i += len(tokens)
                    break
            else:
                i += 1
        return found

    def lookup(self, text: str) -> str | None:
        tokens = tuple(words_of(text))
        return next((cid for t, cid in self.names if t == tokens), None)

    def content(self, words: Sequence[str]) -> list[str]:
        """``words`` without names, speech verbs and label words (what alignment compares)."""
        named = {i for at, n, _ in self.find(words) for i in range(at, at + n)}
        return [
            w
            for i, w in enumerate(words)
            if i not in named and w not in SPEECH_VERBS and w not in LABEL_WORDS
        ]


@dataclass(frozen=True, slots=True)
class Unit:
    """A quotation or a sentence of the composed text: its span, its words and content words,
    its speaker cue, the character it opens with, and its label."""

    start: int
    end: int
    words: tuple[str, ...]
    content: tuple[str, ...]
    quoted: bool = False
    cue: str | None = None
    lead: str | None = None
    label: tuple[str, str | None] | None = None

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)


def _cue(fragment: str, index: NameIndex) -> str | None:
    """The speaker a fragment names: ``Name:`` at its end, or a name next to a speech verb."""
    stripped = fragment.strip().rstrip(",")
    if stripped.endswith(":"):
        head = stripped[:-1].strip().lstrip("*").strip()
        named = index.lookup(head)
        if named is not None:
            return named
        found = index.find(words_of(head))
        if found and found[-1][0] + found[-1][1] == len(words_of(head)):
            return found[-1][2]
    words = words_of(fragment)
    for at, n, cid in index.find(words):
        after = words[at + n : at + n + 3]
        before = words[at - 1] if at > 0 else ""
        if any(w in SPEECH_VERBS for w in after) or before in SPEECH_VERBS:
            return cid
    return None


def _label(fragment: str, index: NameIndex) -> tuple[tuple[str, str | None] | None, int]:
    """A fragment's thought or latent label and where its text starts after it."""
    think = _THINK_LABEL.search(fragment)
    if think is not None:
        return ("think", index.lookup(think.group(1))), think.end()
    latent = _LATENT_LABEL.search(fragment)
    if latent is not None:
        return ("latent", None), latent.end()
    return None, 0


def _sentences(text: str, start: int, end: int) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    at = start
    for match in _SENTENCE_END.finditer(text, start, end):
        found.append((at, match.end()))
        at = match.end()
    if at < end:
        found.append((at, end))
    trimmed: list[tuple[int, int]] = []
    for a, b in found:
        piece = text[a:b]
        lead = len(piece) - len(piece.lstrip())
        tail = len(piece) - len(piece.rstrip())
        if b - tail > a + lead:
            trimmed.append((a + lead, b - tail))
    return trimmed


def units(text: str, index: NameIndex) -> list[Unit]:
    """The composed text's units, in text order (see the module docstring)."""
    pieces: list[tuple[bool, int, int]] = []
    at = 0
    for match in _QUOTE.finditer(text):
        if match.start() > at:
            pieces.append((False, at, match.start()))
        pieces.append((True, match.start(), match.end()))
        at = match.end()
    if at < len(text):
        pieces.append((False, at, len(text)))
    found: list[Unit] = []
    for quoted, a, b in pieces:
        if quoted:
            inner_a, inner_b = a + 1, b - 1
            before = _BREAK.split(text[:a])[-1] if a else ""
            tail = _BREAK.split(text[b:])
            after = tail[0] if tail else ""
            words = words_of(text[inner_a:inner_b])
            label, _ = _label(before, index)
            found.append(
                Unit(
                    inner_a,
                    inner_b,
                    tuple(words),
                    tuple(index.content(words)),
                    quoted=True,
                    cue=_cue(before, index) or _cue(after, index),
                    label=label,
                )
            )
            continue
        for s, e in _sentences(text, a, b):
            raw = text[s:e]
            label, offset = _label(raw, index)
            body = raw[offset:]
            words = words_of(body)
            if not words_of(raw):
                continue
            opening = index.find(words)
            lead = opening[0][2] if opening and opening[0][0] == 0 else None
            found.append(
                Unit(
                    s,
                    e,
                    tuple(words),
                    tuple(index.content(words)),
                    cue=_cue(raw, index),
                    lead=lead,
                    label=label,
                )
            )
    return found


def _run(a: Sequence[str], b: Sequence[str]) -> int:
    """The longest common contiguous run of ``a`` and ``b``."""
    best = 0
    previous = [0] * (len(b) + 1)
    for x in a:
        current = [0] * (len(b) + 1)
        for j, y in enumerate(b, start=1):
            if x == y:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    n = len(needle)
    return any(tuple(haystack[i : i + n]) == tuple(needle) for i in range(len(haystack) - n + 1))


def _best(
    content: Sequence[str], candidates: Sequence[Sequence[str]], after: int = 0
) -> tuple[int | None, float]:
    """The candidate ``content`` aligns best with and its score (ties: the first at or after
    ``after``, else the first)."""
    if not content or not candidates:
        return None, 0.0
    scores = [_run(content, c) / len(content) for c in candidates]
    top = max(scores)
    if top <= 0:
        return None, 0.0
    tied = [i for i, s in enumerate(scores) if s == top]
    chosen = next((i for i in tied if i >= after), tied[0])
    return chosen, top


def _shingles(words: Sequence[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _private_shingles(
    hidden: Sequence[Beat], visible: Sequence[Beat]
) -> dict[int, set[tuple[str, ...]]]:
    """Each hidden beat's shingles (``SHINGLE`` words, fewer for a shorter text of two words
    or more) that no visible beat shares, by beat."""
    found: dict[int, set[tuple[str, ...]]] = {}
    visible_words = [words_of(b.text) for b in visible]
    for beat in hidden:
        words = words_of(beat.text)
        if len(words) < 2:
            continue
        n = min(SHINGLE, len(words))
        own = _shingles(words, n)
        for other in visible_words:
            own -= _shingles(other, n)
        if own:
            found[beat.beat] = own
    return found


def _leaks(unit: Unit, shingles: set[tuple[str, ...]]) -> bool:
    sizes = {len(s) for s in shingles}
    return any(_shingles(unit.words, n) & shingles for n in sizes)


def _covered(claim: Proposition, props: Sequence[Proposition], registry: Registry) -> bool:
    """Whether a beat proposition has ``claim``'s shape or covers it (#140's coverage)."""
    shape = shape_of(claim)
    for p in props:
        if shape_of(p) == shape:
            return True
        timed = claim.model_copy(update={"valid_time": p.valid_time})
        if check(timed, [Record(p, "beat")], registry=registry).kind is VerdictKind.SUPPORTED:
            return True
    return False


def _props(beats: Sequence[VerifiedBeat]) -> list[Proposition]:
    return [c.proposition for b in beats for c in b.claims if c.proposition is not None]


def _unit_at(found: Sequence[Unit], offset: int) -> int | None:
    for i, u in enumerate(found):
        if u.start <= offset < u.end:
            return i
    before = [i for i, u in enumerate(found) if u.start <= offset]
    return before[-1] if before else (0 if found else None)


def trace(
    text: str,
    beats: Sequence[VerifiedBeat],
    claims: Sequence[DetectedClaim],
    *,
    viewpoint: str,
    names: Mapping[str, str],
    hidden: Sequence[VerifiedBeat] = (),
    registry: Registry,
    withheld: Sequence[VerifiedBeat] = (),
) -> CompositionCheck:
    """The final check of ``text`` against the viewpoint's ``beats`` and the ``claims``
    detection found in it (see the module docstring). ``hidden``: the turn's ``think`` and
    ``latent`` beats a character viewpoint does not receive; ``withheld`` (#177): the beats the
    audience filter withheld from it. Pure."""
    omniscient = is_omniscient(viewpoint)
    index = NameIndex(names)
    found = units(text, index)
    failures: list[Failure] = []

    def fail(kind: FailureKind, unit: Unit | None, beat: int | None) -> None:
        item = Failure(kind, None if unit is None else unit.span, beat)
        if item not in failures:
            failures.append(item)

    visible_words = [index.content(words_of(b.text)) for b in beats]
    hidden_words = [index.content(words_of(b.text)) for b in hidden]
    withheld_words = [index.content(words_of(b.text)) for b in withheld]
    aligned: list[int | None] = []
    last = -1
    for unit in found:
        # A tie goes to the next beat not yet aligned (#177: two beats with the same words, as
        # two identical envelopes, are two units), else the first.
        best, score = _best(unit.content, visible_words, last + 1)
        h_best, h_score = _best(unit.content, hidden_words)
        if h_best is not None and h_score >= ALIGN_THRESHOLD and h_score > score:
            fail("think_leak", unit, hidden[h_best].beat)
            aligned.append(None)
            continue
        w_best, w_score = _best(unit.content, withheld_words)
        if w_best is not None and w_score >= ALIGN_THRESHOLD and w_score > score:
            fail("fidelity_leak", unit, withheld[w_best].beat)
            aligned.append(None)
            continue
        if best is not None and score >= ALIGN_THRESHOLD:
            aligned.append(best)
            last = best
        else:
            aligned.append(None)

    # Attribution.
    speakers = {b.character_id for b in beats if b.kind in ("say", "think") and b.character_id}
    for unit, at in zip(found, aligned, strict=True):
        if at is None:
            if unit.cue is not None and unit.cue not in speakers:
                fail("attribution", unit, None)
            if unit.label is not None and unit.label[0] == "think":
                fail("attribution", unit, None)
            continue
        beat = beats[at]
        kind = beat.kind
        if kind == "say":
            if unit.cue is not None and unit.cue != beat.character_id:
                fail("attribution", unit, beat.beat)
        elif kind == "do":
            if unit.lead is not None and unit.lead != beat.character_id:
                fail("attribution", unit, beat.beat)
        elif kind == "narration":
            if unit.cue is not None:
                fail("attribution", unit, beat.beat)
        elif kind == "think":
            if unit.label != ("think", beat.character_id):
                fail("attribution", unit, beat.beat)
        elif kind == "latent" and (unit.label is None or unit.label[0] != "latent"):
            fail("attribution", unit, beat.beat)
        if kind not in PRIVATE_KINDS and unit.label is not None:
            fail("attribution", unit, beat.beat)

    # Private content.
    if not omniscient:
        for number, shingles in _private_shingles(hidden, beats).items():
            for unit in found:
                if _leaks(unit, shingles):
                    fail("think_leak", unit, number)
    else:
        observable = [b for b in beats if b.kind not in PRIVATE_KINDS]
        thoughts = [b for b in beats if b.kind == "think"]
        by_beat = {b.beat: b for b in thoughts}
        for number, shingles in _private_shingles(thoughts, observable).items():
            thinker = by_beat[number].character_id
            for unit in found:
                if _leaks(unit, shingles) and unit.label != ("think", thinker):
                    fail("attribution", unit, number)

    # Withheld content (#177): what the viewpoint saw only the envelope of, or did not perceive.
    for number, shingles in _private_shingles(withheld, beats).items():
        for unit in found:
            if _leaks(unit, shingles):
                fail("fidelity_leak", unit, number)

    # Completeness and order.
    present = {at for at in aligned if at is not None}
    for i, beat in enumerate(beats):
        if i not in present:
            fail("omitted", None, beat.beat)
    furthest = -1
    for unit, at in zip(found, aligned, strict=True):
        if at is None:
            continue
        if at < furthest:
            fail("order", unit, beats[at].beat)
        furthest = max(furthest, at)

    # Traceability.
    visible_props = _props(beats)
    actors = {b.character_id for b in beats if b.character_id is not None}
    hidden_props = _props(hidden)
    withheld_props = _props(withheld)
    traced = 0
    for claim in claims:
        start, end = claim.span[1], claim.span[2]
        at_unit = _unit_at(found, start)
        holder = None if at_unit is None else found[at_unit]
        at = None if at_unit is None else aligned[at_unit]
        if claim.think_leak:
            # #172: another character's private thought, repeated or narrated as an inner state
            # (an omniscient reader sees every thought, labelled).
            if omniscient:
                traced += 1
            else:
                fail("think_leak", holder, None)
            continue
        said = words_of(text[start:end])
        if at is not None and _contains(words_of(beats[at].text), said):
            traced += 1
            continue
        proposition = claim.proposition
        if proposition is not None and _covered(proposition, visible_props, registry):
            traced += 1
            continue
        if (
            proposition is not None
            and proposition.predicate_id == PRESENT_AT
            and proposition.polarity is Polarity.AFFIRMED
            and index.lookup(text[start:end]) in actors
        ):
            # A speaker cue or a lead naming a visible beat's actor ("Bobby says."): detection
            # reads the bare name as presence in the scene, which the actor's own beat shows.
            traced += 1
            continue
        private = (proposition is not None and _covered(proposition, hidden_props, registry)) or (
            any(_contains(words_of(b.text), said) for b in hidden)
        )
        unheard = (
            proposition is not None and _covered(proposition, withheld_props, registry)
        ) or any(_contains(words_of(b.text), said) for b in withheld)
        if private:
            fail("think_leak", holder, None)
        elif unheard:
            fail("fidelity_leak", holder, None)
        else:
            fail("untraced", holder, None)
    return CompositionCheck(claims=len(claims), traced=traced, failures=tuple(failures))


# -- the polish and the composition -------------------------------------------------------------


type Detect = Callable[[str, bool], Awaitable[tuple[DetectionOutcome, Registry]]]
"""Claim detection of a composed text (``True``: with the model layer, the reserved call) and
the registry its claims normalized under."""


def polish_inputs(
    base: GenerationInputs,
    beats: Sequence[Beat],
    names: Mapping[str, str],
    draft: str,
    viewpoint: str,
) -> GenerationInputs:
    """The polish's ``generation`` inputs: the turn's rating guidance and custom
    instructions, and the viewpoint's beats with their draft; nothing else the render read."""
    return GenerationInputs(
        rating_guidance=base.rating_guidance,
        custom_instructions=base.custom_instructions,
        player_input=base.player_input,
        composition_beats=[
            ComposeBeat(
                index=b.beat,
                kind=b.kind,
                name=names.get(b.character_id or "", ""),
                text=b.text[:8_000],
            )
            for b in beats
        ][:100],
        composition_draft=draft[:DRAFT_MAX],
        composition_omniscient=is_omniscient(viewpoint),
    )


def longest_polish(draft: str, max_chars: int | None = None) -> float:
    """The longest polished text accepted: ``LENGTH_FACTOR`` times the draft and, when the
    request names a length (#464), no longer than that length or the draft, whichever is
    longer (a polish may join beats; it may not take the message past the requested length)."""
    longest = LENGTH_FACTOR * max(len(draft), 1)
    if max_chars is not None:
        longest = min(longest, max(len(draft), max_chars))
    return longest


def polish_params(base: ModelParams, draft: str) -> ModelParams:
    """The polish's parameters: the request's temperature and top-p, ``max_tokens`` twice the
    draft's token estimate (four characters a token)."""
    return ModelParams(
        temperature=base.temperature,
        top_p=base.top_p,
        max_tokens=max(MIN_POLISH_TOKENS, 2 * math.ceil(len(draft) / 4)),
    )


async def polish(
    models: ModelAccessLayer,
    inputs: GenerationInputs,
    *,
    params: ModelParams,
    budget: TurnBudget,
) -> str | None:
    """The polish call (see the module docstring); ``None`` when it produced no usable text."""
    found = await models.structured_call(
        GENERATION.name, ComposedMessage, inputs, params=params, budget=budget, variant=COMPOSITION
    )
    if isinstance(found, Uncertain):
        return None
    text = found.text.strip()
    return text or None


def reserve_detection(budget: TurnBudget, calls: int) -> bool:
    """Whether the composed message's detection call (C) is reserved: #156's planner reserves
    it as the owner-only ``composed`` slot before the turn's first call; a budget planned
    without it (no planner) reserves it here when the unreserved remainder can."""
    if calls <= 0:
        return False
    if budget.named.get(COMPOSED, 0) >= calls:
        return True
    if not budget.can_afford_unreserved(calls):
        return False
    budget.reserve(COMPOSED, calls)
    return True


@dataclass(slots=True)
class Composer:
    """What ``compose`` needs from the turn (the pipeline builds it)."""

    models: ModelAccessLayer
    inputs: GenerationInputs
    """The turn's generation inputs (rating guidance, custom instructions, player input)."""
    params: ModelParams
    names: Mapping[str, str]
    detect: Detect
    output_ok: Callable[[str], Awaitable[bool]]
    """#68's output gate on a polished text."""
    deterministic_ok: Callable[[str], bool]
    """The deterministic output policies on the assembly (no model call)."""
    fallback_text: str
    """The safe fallback line, released if the assembly breaks a deterministic policy."""
    polish_on: bool = True
    assisted_checks: int = 0
    """The model-assisted output policies: the polished text's re-check costs one call each."""
    detection_calls: int = 0
    """C: the composed message's detection calls (one when the model layer runs)."""
    hidden: Sequence[VerifiedBeat] = field(default_factory=tuple)
    """The turn's ``think``/``latent`` beats the viewpoint does not receive."""
    max_chars: int | None = None
    """The requested response length in characters (FR-RESP-3; #464): a polished text longer
    than it, and longer than the draft, is refused like any over-long polish."""
    withheld: Sequence[VerifiedBeat] = field(default_factory=tuple)
    """#177: the beats the audience filter withheld from the viewpoint (an envelope's content,
    an unperceived beat): a text that discloses one fails with ``fidelity_leak``."""


async def check_composition(
    text: str,
    beats: Sequence[VerifiedBeat],
    *,
    viewpoint: str,
    composer: Composer,
    with_model: bool = True,
) -> tuple[CompositionCheck, DetectionOutcome]:
    """#148's detection over ``text`` and the final check (``trace``) against ``beats``."""
    outcome, registry = await composer.detect(text, with_model)
    found = trace(
        text,
        beats,
        outcome.claims,
        viewpoint=viewpoint,
        names=composer.names,
        hidden=composer.hidden,
        registry=registry,
        withheld=composer.withheld,
    )
    return found, outcome


async def compose(
    beats: Sequence[VerifiedBeat],
    viewpoint: str,
    budget: TurnBudget,
    *,
    composer: Composer,
    reserved: bool = False,
) -> Composition:
    """The turn's message for ``viewpoint`` (see the module docstring). ``beats``: the turn's
    beats in order (the player's input excluded); ``reserved``: C is held."""
    shown = viewpoint_beats(beats, viewpoint)
    draft = deterministic_assembly(shown, composer.names, viewpoint)
    held = composer.detection_calls if reserved else 0
    before = budget.held

    def settle() -> None:
        # Composition is the turn's last model work: whatever it still holds (C when the
        # assembly is used, an unspent polish slot) goes back.
        budget.release_to(before)
        budget.release_named(COMPOSED)

    async def deterministic(
        degradation: tuple[str, ...] = (),
        *,
        polished: bool = False,
        cause: Cause | None = None,
        rejected_text: str | None = None,
        rejected: CompositionCheck | None = None,
        spent: bool = False,
    ) -> Composition:
        settle()
        found, outcome = await check_composition(
            draft, shown, viewpoint=viewpoint, composer=composer, with_model=False
        )
        safe = bool(draft) and not composer.deterministic_ok(draft)
        return Composition(
            text=composer.fallback_text if safe else draft,
            mode=DETERMINISTIC,
            check=found,
            degradation=degradation,
            polished=polished,
            cause=cause,
            rejected_text=rejected_text,
            rejected=rejected,
            detection=outcome,
            detection_reserved=reserved,
            detection_released=bool(held) and not spent,
            safe_fallback=safe,
        )

    if not composer.polish_on or len(shown) <= 1:
        return await deterministic()
    cost = 1 + composer.assisted_checks + (0 if reserved else composer.detection_calls)
    if not budget.try_optional(POLISH, cost):
        return await deterministic((POLISH_SKIPPED_BUDGET,))
    try:
        with budget.charging(POLISH):
            text = await polish(
                composer.models,
                polish_inputs(composer.inputs, shown, composer.names, draft, viewpoint),
                params=polish_params(composer.params, draft),
                budget=budget,
            )
    except ModelError:
        text = None
    fallback = (COMPOSITION_FALLBACK,)
    if text is None:
        return await deterministic(fallback, polished=True, cause="model")
    if len(text) > longest_polish(draft, composer.max_chars):
        rejected = CompositionCheck(failures=(Failure("length", (0, len(text))),))
        return await deterministic(
            fallback, polished=True, cause="length", rejected_text=text, rejected=rejected
        )
    if not await composer.output_ok(text):
        return await deterministic(
            fallback,
            polished=True,
            cause="output_policy",
            rejected_text=text,
            rejected=CompositionCheck(),
        )
    found, outcome = await check_composition(text, shown, viewpoint=viewpoint, composer=composer)
    if not found.passed:
        log.debug("composition rejected: %d failures", len(found.failures))
        return await deterministic(
            fallback, polished=True, cause="check", rejected_text=text, rejected=found, spent=True
        )
    settle()
    return Composition(
        text=text,
        mode=POLISHED,
        check=found,
        polished=True,
        detection=outcome,
        detection_reserved=reserved,
    )


__all__ = [
    "ALIGN_THRESHOLD",
    "COMPOSITION",
    "COMPOSITION_FALLBACK",
    "COMPOSITION_TEMPLATE",
    "DETERMINISTIC",
    "LENGTH_FACTOR",
    "POLISHED",
    "POLISH_SKIPPED_BUDGET",
    "ComposedMessage",
    "Composer",
    "Composition",
    "CompositionCheck",
    "Failure",
    "NameIndex",
    "Unit",
    "VerifiedBeat",
    "check_composition",
    "compose",
    "deterministic_assembly",
    "longest_polish",
    "polish",
    "polish_inputs",
    "polish_params",
    "reserve_detection",
    "trace",
    "units",
    "viewpoint_beats",
    "words_of",
]
