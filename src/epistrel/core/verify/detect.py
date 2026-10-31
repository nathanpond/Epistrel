"""Claim detection: every claim a render or a composed message makes, found before any is checked
(FR-VERIFY-13,14, FR-PROP-12; docs/02 §4.9; #148). Pure: nothing here reads the database or
calls a model.

**Detection is not checking.** Verification (#140/#142) can check only a claim it is given, and
a well-formed ``say``/``think``/``do``/``latent`` beat does not prove that every factual claim in
its prose was declared. ``detect_render(beats, …)`` (once per render, every beat, spans
``(beat, start, end)``) and ``detect_message(text, …)`` (once per composed message, #152;
``beat = None``) run the layers every verification level has:

- **(a) declared structure.** Each beat's declared propositions and state effects
  (``DeclaredClaim``, normalized by the caller: #150 runs #140's ``normalize_declared``, which
  may auto-register a relation) become claims of layer ``declared``, category ``declared``,
  spanning the whole beat.
- **(b) deterministic detectors**, always on, English data tables (``detectors/*.json``, no
  stemmer: every inflection is listed): possession, transfer, consumption, injury and movement
  language; quantities (number words 0-999 and thousand/million multiples, digits, a unit or
  currency); time expressions (#139's ``find_time_expressions`` and a clock-time table);
  entity and alias mentions no declared proposition covers; and **protected-content** matching
  (``core.verify.protected``: names, aliases and values hidden from the speaker; never over the
  player's own input, whose text asserts only what it states, #567). Each hit is a
  #140 ``ClaimSketch`` and goes through ``normalize``: possession → ``holds``; transfer →
  ``holds`` for the recipient (and a negated ``holds`` for the giver; when the giver is the
  clause's subject, the recipient's claim names it, ``giver``, #519); consumption →
  ``consumed``; injury → ``injured`` (its cause a qualifier); movement → ``located_at`` (a
  departure from a named place, "left the orchard", "out of the orchard": a negated
  ``located_at`` of it, #440; a departure with no place: a negated ``located_at`` of the
  current placement; a negated departure asserts nothing); a lone clock
  time → ``time_is`` of the scene's place; an uncovered mention on an observable, non-speech beat
  → ``present_at`` of the scene. The subject is the nearest mention before the verb (on a
  ``do`` beat, a sentence-initial pronoun or none is the actor), the object the nearest one
  after it of the predicate's object type. A quantity in the clause of a possession, transfer or
  consumption claim is its ``quantity`` qualifier; alone it is a claim no predicate states yet
  (``uncertain``, never a guess), unless it states a tracked State's balance (#191,
  ``DetectionEnv.states``): then it is that State's ``state_value`` claim
  (``core.states.verify.state_claim``). A negation cue back to the clause start makes a claim
  ``negated``; a question, a conditional or a modal suppresses the clause (nothing is asserted).
  A word the tables list is a noun, not a verb, after a determiner or a possessive ("her
  pocket"). A hit whose argument is an unresolved common noun ("the key" with no such entity) is
  noise and never becomes a claim: only resolved-entity and literal sketches proceed.

Layer (c), the ``extraction_backstop`` model, is the Orchestrator's
(``orchestrator.verify.extraction``), as is the decision to run it (``extraction_mode``).

**Matching.** A detected claim whose normalized shape (subject, predicate, object, qualifiers,
polarity; valid-time aside) equals a declared proposition of its beat is ``matched`` to it
(``matched_declared_id``): the declared claim is the one verified. Every other claim is verified
as a new claim. A claim that fills an open thunk's slot names it (``matched_thunk_id``, #146's
``fills``): #150 hands it to ``core.commit.lifecycle.depend`` (the dependence) once the beat
survives verification.

**Gating input.** A beat is ``flagged`` when a detector hit one of ``RISK_CATEGORIES``; when it
is a ``say``/``think`` beat with an uncovered entity mention (``UNCOVERED_MENTION``, #478: a
named entity that is not the speaker or the scene and that no declared proposition or detector
claim of the beat covers; in these channels the mention becomes no claim, so a beat that
declared ``Linda located_at tavern`` and says "Cal and I were married once." is otherwise never
read); or when it is a ``say``/``do`` beat that declared no proposition (an incomplete
structural parse, ``INCOMPLETE``). ``gated`` extraction runs only on flagged beats. An
uncovered mention in a scene channel (``do``, narration, ``latent``, a message) does not flag:
it is already a ``present_at`` claim that is checked.

**Seeds** (``detect_seed``, #149's publish check and genesis): the same detectors over a seed's
text, speaker ``narrator``, at position 0, with no protected content; an unresolved capitalized
name (#139's ``unknown_name``) there is a claim that cannot normalize (``unresolved_entity``),
not noise, so a seed naming an alias the character dropped fails its publish.

``Detection.report()`` and ``DetectedClaim.payload()`` carry ids, spans and codes only, never
text (invariant 3); a claim's ``shape`` is a hash of its normalized key.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal, cast

from epistrel.core.commit.sketch import ThunkSketch, fills
from epistrel.core.propositions.model import (
    ANY,
    LiteralKind,
    Polarity,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.registry import Registry
from epistrel.core.resolution.entities import (
    NARRATOR,
    PLAYER,
    ResolutionContext,
    resolve_mentions,
)
from epistrel.core.resolution.time import Position, TimeBasis, find_time_expressions, turn_of
from epistrel.core.resolution.types import Mention, MentionKind, ReasonCode
from epistrel.core.verify import inner_state
from epistrel.core.verify.attribution import ClaimAttribution
from epistrel.core.verify.introduce import Introductions
from epistrel.core.verify.normalize import ClaimSketch, Unnormalizable, normalize
from epistrel.core.verify.protected import PrivateRecord, ProtectedIndex, match_thoughts
from epistrel.core.verify.rules import VerdictReason

if TYPE_CHECKING:
    from epistrel.core.states.verify import TrackedState

Layer = Literal["declared", "detector", "extraction"]
LAYERS: tuple[Layer, ...] = ("declared", "detector", "extraction")
Category = Literal[
    "declared",
    "extracted",
    "entity_mention",
    "quantity",
    "possession",
    "transfer",
    "consumption",
    "injury",
    "movement",
    "time",
    "protected_content",
    "inner_state",
    "recall",
]
DETECTOR_CATEGORIES: tuple[Category, ...] = (
    "entity_mention",
    "quantity",
    "possession",
    "transfer",
    "consumption",
    "injury",
    "movement",
    "time",
    "protected_content",
    "inner_state",
    "recall",
)
"""The categories of layer (b); a declared claim's is ``declared``, an extraction claim's
``extracted``."""
RISK_CATEGORIES: frozenset[str] = frozenset(
    {"possession", "transfer", "quantity", "time", "protected_content"}
)
"""A detector hit of one of these flags its beat for ``gated`` extraction."""
INCOMPLETE = "incomplete"
"""The flag of a ``say``/``do`` beat that declared no proposition."""
UNCOVERED_MENTION = "uncovered_mention"
"""The flag of a ``say``/``think`` beat naming an entity that no declared proposition, no
detector claim, the speaker nor the scene covers (#478): that mention becomes no claim, so only
extraction can read what the beat says about the entity."""
STRUCTURED_CHANNELS: frozenset[str] = frozenset({"say", "do"})
"""The beats whose structure must declare their propositions (empty: an incomplete parse)."""
SCENE_CHANNELS: frozenset[str] = frozenset({"do", "narration", "latent", "message"})
"""The beats whose uncovered mention says the entity takes part in the scene (``present_at``);
in ``say`` and ``think`` a mention is the speaker's reference, a hit with no claim."""
MESSAGE = "message"
"""The channel of a composed message (``detect_message``)."""
THINK_REF = "think:"
"""The ``protected_ref`` prefix of a claim that leaks a private thought (#172)."""
INNER_STATE_CHANNELS: frozenset[str] = frozenset({"say", "do", "narration", "message"})
"""The beats the inner-state check reads (#172): every player-facing channel."""

Span = tuple[int | None, int, int]

_DETERMINERS = frozenset(
    {
        "the",
        "a",
        "an",
        "her",
        "his",
        "my",
        "your",
        "their",
        "its",
        "our",
        "this",
        "that",
        "these",
        "those",
        "some",
        "any",
        "every",
        "each",
    }
)
_SUBJECT_PRONOUNS = frozenset({"she", "he", "they", "it"})
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?]+")
_CLAUSE_BREAK = re.compile(r"[,;:]")
_CLAUSE_WORDS = frozenset({"but", "because", "while", "although", "though", "whereas"})


# -- the tables ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Lemma:
    """One verb of a detector table: its inflections and its role options."""

    lemma: str
    forms: tuple[str, ...]
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class VerbTable:
    category: str
    predicate: str
    lemmas: tuple[Lemma, ...]
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Tables:
    verbs: tuple[VerbTable, ...]
    quantity: Mapping[str, Any]
    time: Mapping[str, Any]
    cues: Mapping[str, Any]


VERB_CATEGORIES = ("possession", "transfer", "consumption", "injury", "movement")


def _table(name: str) -> dict[str, Any]:
    source = resources.files("epistrel.core.verify").joinpath(f"detectors/{name}.json")
    loaded: dict[str, Any] = json.loads(source.read_text("utf-8"))
    return loaded


@cache
def tables() -> Tables:
    """The English detector tables (``detectors/*.json``)."""
    verbs = []
    for category in VERB_CATEGORIES:
        raw = _table(category)
        lemmas = tuple(
            Lemma(
                str(e["lemma"]),
                tuple(str(f) for f in e["forms"]),
                {k: v for k, v in e.items() if k not in ("lemma", "forms")},
            )
            for e in raw["lemmas"]
        )
        options = {
            k: v
            for k, v in raw.items()
            if k not in ("category", "language", "description", "lemmas")
        }
        verbs.append(VerbTable(category, str(raw["predicate"]), lemmas, options))
    return Tables(tuple(verbs), _table("quantity"), _table("time"), _table("cues"))


def _alternation(words: Sequence[str]) -> str:
    ordered = sorted(set(words), key=lambda w: (-len(w), w))
    return "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)


@cache
def _verb_patterns() -> tuple[tuple[VerbTable, Lemma, re.Pattern[str]], ...]:
    found = []
    for table in tables().verbs:
        for lemma in table.lemmas:
            pattern = re.compile(rf"(?<![\w'\u2019])(?:{_alternation(lemma.forms)})(?![\w'\u2019])")
            found.append((table, lemma, pattern))
    return tuple(found)


def _number_words() -> dict[str, int]:
    q = tables().quantity
    return {**q["ones"], **q["tens"]}


@cache
def _quantity_pattern() -> re.Pattern[str]:
    q = tables().quantity
    word = _alternation([*q["ones"], *q["tens"]])
    scale = _alternation(list(q["scales"]))
    article = _alternation(list(q["articles"]))
    number = (
        rf"(?:\d[\d,]*(?:\.\d+)?|(?:{word})(?:[\s-]+(?:{word}))*(?:\s+(?:{scale})"
        rf"(?:\s+(?:and\s+)?(?:{word})(?:[\s-]+(?:{word}))*)?)*|{article})"
    )
    units = _alternation(list(q["units"]))
    return re.compile(
        rf"(?<![\w'\u2019])(?P<number>{number})\s+(?P<unit>{units})(?![\w'\u2019])",
        re.IGNORECASE,
    )


def parse_number(text: str) -> int | float | None:
    """``text`` as a number: digits (``1,200``, ``2.5``), number words up to the millions
    (``forty``, ``two hundred and six``, ``three thousand``) or an article (``a dozen``)."""
    q = tables().quantity
    value = " ".join(text.casefold().replace("-", " ").split())
    if not value:
        return None
    if re.fullmatch(r"\d[\d,]*", value):
        return int(value.replace(",", ""))
    if re.fullmatch(r"\d+\.\d+", value):
        return float(value)
    if value in q["articles"]:
        return int(q["articles"][value])
    words = _number_words()
    scales = q["scales"]
    total = 0
    current = 0
    seen = False
    for part in value.split():
        if part == "and":
            continue
        if part in words:
            current += int(words[part])
            seen = True
        elif part == "hundred":
            current = max(current, 1) * 100
            seen = True
        elif part in scales:
            total += max(current, 1) * int(scales[part])
            current = 0
            seen = True
        else:
            return None
    return total + current if seen else None


@cache
def _clock_pattern() -> re.Pattern[str]:
    t = tables().time
    approx = _alternation(list(t["approximators"]))
    named = _alternation(list(t["named"]))
    hours = _alternation([*t["hours"], *(str(n) for n in range(1, 13))])
    fractions = _alternation(list(t["fractions"]))
    meridians = _alternation(list(t["meridians"]))
    clock = (
        rf"(?:{named}"
        rf"|(?:{fractions})\s+(?:{hours})"
        rf"|(?:{hours})\s+o['\u2019]clock(?:\s+(?:{meridians}))?"
        rf"|\d{{1,2}}:\d{{2}}(?:\s*(?:{meridians}))?"
        rf"|(?:{hours})\s*(?:{meridians}))"
    )
    return re.compile(rf"(?<![\w'\u2019])(?:(?:{approx})\s+)?{clock}(?![\w'\u2019])", re.IGNORECASE)


# -- the result ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclaredClaim:
    """A proposition or state effect a beat declared (layer a), as the caller normalized it."""

    id: str
    proposition: Proposition | None = None
    unnormalized: Unnormalizable | None = None
    effect: bool = False
    attribution: ClaimAttribution | None = None
    """#167: the declared claim reports someone's belief or speech act (not the speaker's own
    assertion)."""


@dataclass(frozen=True, slots=True)
class DetectBeat:
    """One beat of a render: its index, channel (``say``, ``think``, ``do``, ``latent``,
    ``narration``), text, speaker (an entity id, ``narrator`` or ``player``) and declarations."""

    index: int
    channel: str
    text: str
    speaker: str = NARRATOR
    declared: tuple[DeclaredClaim, ...] = ()


def shape_of(p: Proposition) -> tuple[Any, ...]:
    """What matching compares: the normalized key and the polarity (valid-time excluded)."""
    return (*p.normalized_key(), p.polarity.value)


def shape_hash(p: Proposition) -> str:
    """``shape_of`` as a hash (ids and literal values never appear in a report)."""
    body = json.dumps(shape_of(p), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class DetectedClaim:
    """One detected claim (see the module docstring): where it is, which layer and category
    found it, its proposition (or why it has none), what it matched and what it touches."""

    span: Span
    layer: Layer
    category: Category
    channel: str
    speaker: str
    proposition: Proposition | None = None
    unnormalized: Unnormalizable | None = None
    sketch: ClaimSketch | None = None
    matched_declared_id: str | None = None
    matched_thunk_id: str | None = None
    protected_ref: str | None = None
    confidence: float | None = None
    merged: tuple[Layer, ...] = ()
    """Other layers that found the same claim (cross-layer dedup)."""
    attribution: ClaimAttribution | None = None
    """#167: whom the claim attributes its proposition to (declared, or the attribution
    lexicon's cue before its span): checked against the speaker's nested beliefs, never as the
    speaker's own assertion."""
    recalled: str | None = field(default=None, compare=False, repr=False)
    """#172: a ``recall`` claim's recalled content (never in a payload)."""
    giver: str | None = None
    """#519: the entity that hands the item over, on a transfer's affirmed ``holds`` of its
    recipient when the giver is the clause's subject ("Jenni hands Cal the key"): the
    permission check reads it (a render's own character handing the player's character an
    item is that render's own action)."""

    @property
    def think_leak(self) -> bool:
        """#172: the claim repeats or narrates another character's private thought (a
        ``think:`` reference: the thought side index or the inner-state check)."""
        return self.protected_ref is not None and self.protected_ref.startswith(THINK_REF)

    @property
    def beat(self) -> int | None:
        return self.span[0]

    @property
    def protected(self) -> bool:
        return self.category == "protected_content"

    @property
    def matches(self) -> int:
        """How many findings of the other layers matched this declared claim (``merged``)."""
        return len(self.merged) if self.layer == "declared" else 0

    @property
    def shape(self) -> str | None:
        return None if self.proposition is None else shape_hash(self.proposition)

    def payload(self) -> dict[str, Any]:
        """A ``turn_report.detection.claims`` entry: spans, ids and codes only."""
        return {
            "span": list(self.span),
            "layer": self.layer,
            "category": self.category,
            "channel": self.channel,
            "shape": self.shape,
            "unnormalized": None if self.unnormalized is None else self.unnormalized.reason.value,
            "matched_declared_id": self.matched_declared_id,
            "matched_thunk_id": self.matched_thunk_id,
            "protected_ref": self.protected_ref,
            "merged": list(self.merged),
        }


@dataclass(frozen=True, slots=True)
class BeatDetection:
    """What detection found on one beat (``beat = None``: the composed message): its mentions
    (#139's, persisted on the beat's payload by #150), the detector hits by category, the
    noise dropped and the gating flags."""

    beat: int | None
    channel: str
    mentions: tuple[Mention, ...] = ()
    hits: Mapping[str, int] = field(default_factory=dict)
    noise: int = 0
    flags: tuple[str, ...] = ()

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def incomplete(self) -> bool:
        return INCOMPLETE in self.flags

    def mentions_payload(self) -> list[dict[str, Any]]:
        """The beat payload's ``mentions`` (spans and ids, no text; #139's ``as_payload``)."""
        return [m.as_payload() for m in self.mentions]


@dataclass(frozen=True, slots=True)
class Detection:
    """One render's (or one composed message's) detection: its claims in text order and what
    each beat held."""

    claims: tuple[DetectedClaim, ...]
    beats: tuple[BeatDetection, ...]
    protected_index_truncated: bool = False

    def flagged_beats(self) -> tuple[int | None, ...]:
        return tuple(b.beat for b in self.beats if b.flagged)

    def by_layer(self, layer: Layer) -> tuple[DetectedClaim, ...]:
        return tuple(c for c in self.claims if c.layer == layer)

    def counts(self) -> dict[str, int]:
        """``declared`` claims; ``detector_hits`` and ``extraction_claims``, each layer's
        findings (those merged into an earlier layer's claim included); ``matched``, the
        findings that matched a declared claim."""
        return counts_of(self.claims)

    def with_claims(self, claims: Sequence[DetectedClaim]) -> Detection:
        return replace(self, claims=tuple(claims))


def counts_of(claims: Sequence[DetectedClaim]) -> dict[str, int]:
    """``Detection.counts`` over any claims (one render's, or a turn's)."""
    return {
        "declared": sum(1 for c in claims if c.layer == "declared"),
        "detector_hits": sum(1 for c in claims if c.layer == "detector" or "detector" in c.merged),
        "extraction_claims": sum(
            1 for c in claims if c.layer == "extraction" or "extraction" in c.merged
        ),
        "matched": sum(c.matches for c in claims),
    }


@dataclass(frozen=True, slots=True)
class DetectionEnv:
    """What detection reads besides the text: the registry, the story's ``TimeBasis``, the
    ``ResolutionContext`` of each speaker (``context_for``), the protected-content index of
    each speaker (``protected_for``; ``None``: none, as for a seed), the scene's place (#137;
    ``None``: no scene known), each entity's current placement, and the open thunks."""

    registry: Registry
    basis: TimeBasis
    context_for: Callable[[str], ResolutionContext]
    protected_for: Callable[[str], ProtectedIndex | None] = lambda _speaker: None
    scene_entity: str | None = None
    placement: Mapping[str, str] = field(default_factory=dict)
    open_thunks: Sequence[tuple[str, ThunkSketch]] = ()
    introductions: Introductions | None = None
    """The turn's minted ids for new places and objects (``core.verify.introduce``, #479);
    ``None``: nothing is minted (an eval, the shadow job)."""
    private: tuple[PrivateRecord, ...] = ()
    """#172: the characters' private records (their ``think``s and belief statements) the
    thought side index and the inner-state check read (``core.verify.protected``)."""
    states: tuple[TrackedState, ...] = ()
    """#191: the story's States (``core.states.verify.tracked``): a lone quantity naming a
    number State in a sentence that states a balance is a ``state_value`` claim."""
    scope_keys: frozenset[str] = frozenset()
    """#191: the story's characters, the scope keys of a per-character State."""

    def with_private(self, records: Sequence[PrivateRecord]) -> DetectionEnv:
        """This environment with ``records`` added to its private records (a render's
        ``think`` beats, after it)."""
        known = {r.ref for r in self.private}
        return replace(self, private=(*self.private, *(r for r in records if r.ref not in known)))


# -- the detector pass -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    word: str
    sentence: int
    clause: int


def _tokens(text: str) -> list[_Token]:
    found: list[_Token] = []
    sentence = clause = 0
    last = 0
    for match in _WORD.finditer(text):
        between = text[last : match.start()]
        if found and _SENTENCE_END.search(between):
            sentence += 1
            clause += 1
        elif found and _CLAUSE_BREAK.search(between):
            clause += 1
        word = match.group().casefold().replace("\u2019", "'")
        if word in _CLAUSE_WORDS and found:
            clause += 1
        found.append(_Token(match.start(), match.end(), word, sentence, clause))
        last = match.end()
    return found


def _token_at(tokens: Sequence[_Token], offset: int) -> int | None:
    for i, token in enumerate(tokens):
        if token.start <= offset < token.end:
            return i
        if token.start > offset:
            return i
    return None


def _phrase_in(words: Sequence[str], phrase: str) -> bool:
    parts = phrase.split()
    n = len(parts)
    return any(list(words[i : i + n]) == parts for i in range(len(words) - n + 1))


def _suppressed(text: str, tokens: Sequence[_Token], at: int) -> bool:
    """A question, a conditional or a modal before ``tokens[at]`` in its clause."""
    cues = tables().cues
    token = tokens[at]
    end = text.find("?", token.end)
    stop = _SENTENCE_END.search(text, token.end)
    if end != -1 and (stop is None or stop.start() >= end):
        return True
    before = [t.word for t in tokens[:at] if t.clause == token.clause]
    if any(_phrase_in(before, c) for c in cues["conditionals"]):
        return True
    return any(_phrase_in(before, m) for m in cues["modals"])


def _negated(tokens: Sequence[_Token], at: int) -> bool:
    negation = frozenset(tables().cues["negation"])
    token = tokens[at]
    return any(t.word in negation for t in tokens[:at] if t.clause == token.clause)


@dataclass(slots=True)
class _Beat:
    """The working state of one beat's detector pass."""

    index: int | None
    channel: str
    text: str
    speaker: str
    context: ResolutionContext
    tokens: list[_Token]
    mentions: list[Mention]
    hits: dict[str, int] = field(default_factory=dict)
    noise: int = 0
    risky: set[str] = field(default_factory=set)
    """Risk categories that flag the beat: an unmatched claim's, or a hit that could not
    become a claim (an unresolved argument), never a suppressed one."""
    used: set[str] = field(default_factory=set)
    uncovered: bool = False
    """An uncovered mention that became no claim (``UNCOVERED_MENTION``)."""
    sketches: list[tuple[str, ClaimSketch, tuple[int, int]]] = field(default_factory=list)
    givers: dict[int, str] = field(default_factory=dict)
    """#519: a transfer sketch's giver, by its index in ``sketches``."""

    def hit(self, category: str) -> None:
        self.hits[category] = self.hits.get(category, 0) + 1


def _usable(m: Mention) -> bool:
    return m.entity_id is not None and m.kind is not MentionKind.GROUP


def _actor(beat: _Beat) -> str | None:
    if beat.speaker in (NARRATOR,):
        return None
    if beat.speaker == PLAYER:
        return beat.context.player_id
    return beat.speaker


def _kind_of(beat: _Beat, entity_id: str) -> str | None:
    entry = beat.context.index.get(entity_id)
    return None if entry is None else entry.kind


def _subject(beat: _Beat, start: int, sentence: int) -> str | None:
    before = [
        m
        for m in beat.mentions
        if _usable(m) and m.span[1] <= start and _sentence_of(beat, m.span[0]) == sentence
    ]
    if beat.channel == "do":
        if not before:
            return _actor(beat)
        first = before[-1]
        word = beat.text[first.span[0] : first.span[1]].casefold()
        at = _token_at(beat.tokens, first.span[0])
        initial = at is not None and (at == 0 or beat.tokens[at - 1].sentence != sentence)
        if word in _SUBJECT_PRONOUNS and initial and _actor(beat) is not None:
            return _actor(beat)
    if not before:
        return None
    return before[-1].entity_id


def _sentence_of(beat: _Beat, offset: int) -> int | None:
    at = _token_at(beat.tokens, offset)
    return None if at is None else beat.tokens[at].sentence


def _clause_of(beat: _Beat, offset: int) -> int | None:
    at = _token_at(beat.tokens, offset)
    return None if at is None else beat.tokens[at].clause


def _following(
    beat: _Beat, end: int, clause: int, kinds: frozenset[str] | None, *, skip: set[str]
) -> list[Mention]:
    return [
        m
        for m in beat.mentions
        if _usable(m)
        and m.span[0] >= end
        and _clause_of(beat, m.span[0]) == clause
        and m.entity_id not in skip
        and (kinds is None or _kind_of(beat, str(m.entity_id)) in kinds)
    ]


def _object_kinds(registry: Registry, predicate: str) -> frozenset[str] | None:
    from epistrel.core.verify.normalize import predicate_for

    found = predicate_for(registry, predicate)
    if found is None or found.object_type == ANY:
        return None
    return frozenset({found.object_type})


def _after_word(beat: _Beat, end: int, clause: int, words: Sequence[str]) -> Mention | None:
    """The first usable mention right after one of ``words`` following ``end`` in the clause."""
    wanted = frozenset(words)
    for i, token in enumerate(beat.tokens):
        if token.start < end or token.clause != clause or token.word not in wanted:
            continue
        for m in beat.mentions:
            if _usable(m) and m.span[0] >= token.end:
                nxt = beat.tokens[i + 1] if i + 1 < len(beat.tokens) else None
                if nxt is not None and m.span[0] <= nxt.end + 8:
                    return m
                break
    return None


def _verb_hits(beat: _Beat, registry: Registry, env: DetectionEnv) -> None:
    taken: list[tuple[int, int]] = []
    hits = []
    for table, lemma, pattern in _verb_patterns():
        for match in pattern.finditer(beat.text.casefold()):
            hits.append((match.start(), match.end(), table, lemma))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    for start, end, table, lemma in hits:
        if any(start < b and a < end for a, b in taken):
            continue
        if any(m.span[0] < end and start < m.span[1] for m in beat.mentions if _usable(m)):
            continue
        at = _token_at(beat.tokens, start)
        if at is None:
            continue
        if at > 0 and beat.tokens[at - 1].word in _DETERMINERS:
            continue
        taken.append((start, end))
        token = beat.tokens[at]
        beat.hit(table.category)
        if _suppressed(beat.text, beat.tokens, at):
            beat.noise += 1
            continue
        negated = _negated(beat.tokens, at)
        polarity = Polarity.NEGATED if negated else Polarity.AFFIRMED
        made = _verb_claims(beat, table, lemma, start, end, token, polarity, registry, env)
        if not made:
            beat.noise += 1
            beat.risky.add(table.category)


def _sketch(
    subject: str,
    relation: str,
    obj: str | None,
    polarity: Polarity,
    span: tuple[int, int],
    qualifiers: Mapping[str, Any] | None = None,
) -> ClaimSketch:
    return ClaimSketch(
        relation_text=relation,
        subject_id=subject,
        object_id=obj,
        polarity=polarity,
        span=span,
        qualifiers=dict(qualifiers or {}),
    )


def _leaves_place(
    beat: _Beat, table: VerbTable, lemma: Lemma, end: int, place: Mention
) -> bool | None:
    """Whether a movement verb leaves the place it names (#440): ``True`` after a source marker
    ("out of the orchard") or as a departure verb's object ("left the orchard"); ``False`` when
    the subject reaches it (any other movement, or a departure toward a destination marker,
    "fled to the barn"); ``None`` when a departure verb's place follows another word ("fled
    through the orchard"), which asserts nothing."""
    articles = frozenset(table.options.get("articles", ()))
    between = [
        t.word
        for t in beat.tokens
        if t.start >= end and t.end <= place.span[0] and t.word not in articles
    ]
    if between and between[-1] in table.options.get("destination_markers", ()):
        return False
    for marker in table.options.get("source_markers", ()):
        words = marker.split()
        if between[-len(words) :] == words:
            return True
    if not lemma.options.get("departure"):
        return False
    return True if not between else None


def _verb_claims(
    beat: _Beat,
    table: VerbTable,
    lemma: Lemma,
    start: int,
    end: int,
    token: _Token,
    polarity: Polarity,
    registry: Registry,
    env: DetectionEnv,
) -> bool:
    subject = _subject(beat, start, token.sentence)
    span = (start, end)
    category = table.category
    if category == "injury":
        hurts = lemma.options.get("hurts", "object")
        if hurts == "subject":
            if subject is None:
                return False
            beat.sketches.append(
                (category, _sketch(subject, "injured", None, polarity, span), span)
            )
            beat.used.add(subject)
            return True
        victims = _following(beat, end, token.clause, frozenset({"person"}), skip=set())
        if not victims:
            return False
        victim = str(victims[0].entity_id)
        qualifiers = {"cause": subject} if subject is not None and subject != victim else {}
        beat.sketches.append(
            (category, _sketch(victim, "injured", None, polarity, span, qualifiers), span)
        )
        beat.used |= {victim, *([subject] if subject else [])}
        return True
    if subject is None:
        return False
    if category == "movement":
        places = _following(beat, end, token.clause, frozenset({"place"}), skip={subject})
        if places:
            place = str(places[0].entity_id)
            leaving = _leaves_place(beat, table, lemma, end, places[0])
            if leaving is None:
                return False
            if leaving and polarity is Polarity.NEGATED:
                return False
            located = Polarity.NEGATED if leaving else polarity
            beat.sketches.append(
                (category, _sketch(subject, "located_at", place, located, span), span)
            )
            beat.used |= {subject, place}
            return True
        departure = bool(lemma.options.get("departure")) or any(
            t.word in table.options.get("departure_cues", ())
            for t in beat.tokens
            if t.start >= end and t.clause == token.clause
        )
        current = env.placement.get(subject)
        if not departure or current is None or polarity is Polarity.NEGATED:
            return False
        beat.sketches.append(
            (category, _sketch(subject, "located_at", current, Polarity.NEGATED, span), span)
        )
        beat.used.add(subject)
        return True
    item_kinds = _object_kinds(registry, table.predicate)
    if category == "transfer":
        return _transfer(beat, table, lemma, subject, end, token, polarity, span, item_kinds)
    objects = _following(beat, end, token.clause, item_kinds, skip={subject})
    if not objects:
        return False
    obj = str(objects[0].entity_id)
    beat.sketches.append((category, _sketch(subject, table.predicate, obj, polarity, span), span))
    beat.used |= {subject, obj}
    return True


def _transfer(
    beat: _Beat,
    table: VerbTable,
    lemma: Lemma,
    subject: str,
    end: int,
    token: _Token,
    polarity: Polarity,
    span: tuple[int, int],
    item_kinds: frozenset[str] | None,
) -> bool:
    recipient_cues = table.options.get("recipient_cues", ())
    giver_cues = table.options.get("giver_cues", ())
    direction = lemma.options.get("direction", "give")
    if direction == "take":
        giver_m = _after_word(beat, end, token.clause, giver_cues)
        giver = None if giver_m is None else str(giver_m.entity_id)
        items = _following(beat, end, token.clause, item_kinds, skip={subject, giver or ""})
        if not items:
            return False
        item = str(items[0].entity_id)
        beat.sketches.append(("transfer", _sketch(subject, "holds", item, polarity, span), span))
        if giver is not None and polarity is Polarity.AFFIRMED:
            beat.sketches.append(
                ("transfer", _sketch(giver, "holds", item, Polarity.NEGATED, span), span)
            )
        beat.used |= {subject, item, *([giver] if giver else [])}
        return True
    recipient_m = _after_word(beat, end, token.clause, recipient_cues)
    people = _following(beat, end, token.clause, None, skip={subject})
    items = [
        m for m in people if item_kinds is None or _kind_of(beat, str(m.entity_id)) in item_kinds
    ]
    recipient = None if recipient_m is None else str(recipient_m.entity_id)
    if recipient is None:
        others = [m for m in people if m not in items]
        recipient = None if not others else str(others[0].entity_id)
    items = [m for m in items if str(m.entity_id) != recipient]
    if not items:
        return False
    item = str(items[0].entity_id)
    if recipient is not None:
        if polarity is Polarity.AFFIRMED:
            beat.givers[len(beat.sketches)] = subject
        beat.sketches.append(("transfer", _sketch(recipient, "holds", item, polarity, span), span))
    if polarity is Polarity.AFFIRMED:
        beat.sketches.append(
            ("transfer", _sketch(subject, "holds", item, Polarity.NEGATED, span), span)
        )
    beat.used |= {subject, item, *([recipient] if recipient else [])}
    return True


def _quantity_hits(beat: _Beat) -> list[tuple[int, int, TypedLiteral]]:
    q = tables().quantity
    found = []
    for match in _quantity_pattern().finditer(beat.text):
        number = parse_number(match.group("number"))
        if number is None:
            continue
        unit = match.group("unit").casefold()
        unit = str(q["unit_lemmas"].get(unit, unit))
        literal = TypedLiteral(kind=LiteralKind.QUANTITY, value=number, unit=unit)
        found.append((match.start(), match.end(), literal))
    return found


def render_quantity(literal: TypedLiteral) -> str:
    """A quantity literal as a ``quantity`` qualifier value: ``<number> <unit>``."""
    value = literal.value
    number = int(value) if isinstance(value, float) and value.is_integer() else value
    return f"{number} {literal.unit}" if literal.unit else str(number)


def _quantities(beat: _Beat) -> list[tuple[str, ClaimSketch, tuple[int, int]]]:
    """Attach each quantity to a claim of its clause; a lone one is its own claim."""
    lone: list[tuple[str, ClaimSketch, tuple[int, int]]] = []
    for start, end, literal in _quantity_hits(beat):
        beat.hit("quantity")
        at = _token_at(beat.tokens, start)
        if at is not None and _suppressed(beat.text, beat.tokens, at):
            beat.noise += 1
            continue
        clause = _clause_of(beat, start)
        sentence = _sentence_of(beat, start)
        for i, (category, sketch, span) in enumerate(beat.sketches):
            same = _clause_of(beat, span[0]) == clause or _sentence_of(beat, span[0]) == sentence
            takes = (
                category in ("possession", "transfer", "consumption")
                and same
                and sketch.polarity is Polarity.AFFIRMED
                and "quantity" not in sketch.qualifiers
            )
            if takes:
                qualifiers = {**sketch.qualifiers, "quantity": render_quantity(literal)}
                beat.sketches[i] = (category, replace(sketch, qualifiers=qualifiers), span)
                break
        else:
            lone.append(
                (
                    "quantity",
                    ClaimSketch(relation_text="quantity", literal=literal, span=(start, end)),
                    (start, end),
                )
            )
    return lone


def _times(beat: _Beat, scene_entity: str | None) -> list[tuple[str, ClaimSketch, tuple[int, int]]]:
    """#139's relative expressions become the time of their clause's claims; a clock time is a
    ``time_is`` claim of the scene's place."""
    for expression in find_time_expressions(beat.text):
        beat.hit("time")
        clause = _clause_of(beat, expression.span[0])
        taken = False
        for i, (category, sketch, span) in enumerate(beat.sketches):
            if _clause_of(beat, span[0]) == clause and not sketch.time_text:
                beat.sketches[i] = (category, replace(sketch, time_text=expression.text), span)
                taken = True
        if not taken:
            beat.risky.add("time")  # a time no claim takes: extraction may find its event
    found: list[tuple[str, ClaimSketch, tuple[int, int]]] = []
    for match in _clock_pattern().finditer(beat.text):
        beat.hit("time")
        at = _token_at(beat.tokens, match.start())
        if at is not None and _suppressed(beat.text, beat.tokens, at):
            beat.noise += 1
            continue
        said = " ".join(match.group().split())
        found.append(
            (
                "time",
                ClaimSketch(
                    relation_text="time_is",
                    subject_id=scene_entity,
                    literal=TypedLiteral(kind=LiteralKind.STR, value=said.casefold()),
                    span=match.span(),
                ),
                match.span(),
            )
        )
    return found


def _mentions(
    beat: _Beat, covered: set[str], scene_entity: str | None, *, seed: bool
) -> list[tuple[str, ClaimSketch | Unnormalizable, tuple[int, int]]]:
    """Uncovered entity mentions (see the module docstring)."""
    found: list[tuple[str, ClaimSketch | Unnormalizable, tuple[int, int]]] = []
    seen: set[str] = set()
    actor = _actor(beat)
    for m in beat.mentions:
        if m.kind is MentionKind.GROUP:
            continue
        if m.entity_id is None:
            unknown = m.reason is not None and m.reason.code is ReasonCode.UNKNOWN_NAME
            if seed and unknown:
                beat.hit("entity_mention")
                found.append(
                    (
                        "entity_mention",
                        Unnormalizable(VerdictReason.UNRESOLVED_ENTITY, "unknown_name"),
                        m.span,
                    )
                )
            elif unknown:
                beat.noise += 1
            continue
        if m.kind in (MentionKind.PRONOUN, MentionKind.FIRST_PERSON, MentionKind.SECOND_PERSON):
            continue
        entity = str(m.entity_id)
        if entity in covered or entity in beat.used or entity == actor or entity in seen:
            continue
        if entity == scene_entity:
            continue
        seen.add(entity)
        beat.hit("entity_mention")
        if beat.channel not in SCENE_CHANNELS:
            beat.uncovered = True  # a reference with no claim: only extraction can read it
            continue
        if scene_entity is None or seed:
            continue
        found.append(
            (
                "entity_mention",
                ClaimSketch(
                    relation_text="present_at",
                    subject_id=entity,
                    object_id=scene_entity,
                    span=m.span,
                ),
                m.span,
            )
        )
    return found


def _declared_ids(beat: DetectBeat) -> set[str]:
    found: set[str] = set()
    for d in beat.declared:
        p = d.proposition
        if p is None:
            continue
        found.add(p.subject_id)
        if p.object_id is not None:
            found.add(p.object_id)
        found |= set(p.object_set or ())
    return found


def _normalize_one(
    sketch: ClaimSketch,
    speaker: str,
    position: Position,
    env: DetectionEnv,
    context: ResolutionContext,
) -> Proposition | Unnormalizable:
    found = normalize(
        sketch,
        speaker,
        position,
        registry=env.registry,
        context=context,
        basis=env.basis,
        introduce=env.introductions,
    )
    if isinstance(found, Proposition | Unnormalizable):
        return found
    return Unnormalizable(VerdictReason.UNRESOLVED_PREDICATE, "unregistered")


def _detect_beat(
    beat: DetectBeat,
    position: Position,
    env: DetectionEnv,
    *,
    message: bool,
    seed: bool = False,
) -> tuple[list[DetectedClaim], BeatDetection]:
    index = None if message else beat.index
    context = env.context_for(beat.speaker)
    resolver_speaker = NARRATOR if beat.channel == "do" else beat.speaker
    mentions = resolve_mentions(beat.text, resolver_speaker, context) if beat.text else []
    work = _Beat(
        index, beat.channel, beat.text, beat.speaker, context, _tokens(beat.text), mentions
    )
    claims: list[DetectedClaim] = []
    for d in beat.declared:
        claims.append(
            DetectedClaim(
                span=(index, 0, len(beat.text)),
                layer="declared",
                category="declared",
                channel=beat.channel,
                speaker=beat.speaker,
                proposition=d.proposition,
                unnormalized=d.unnormalized if d.proposition is None else None,
                matched_declared_id=d.id,
                attribution=d.attribution,
            )
        )
    _verb_hits(work, env.registry, env)
    lone = _quantities(work)
    lone += _times(work, env.scene_entity)
    mention_claims = _mentions(work, _declared_ids(beat), env.scene_entity, seed=seed)
    found_claims: list[DetectedClaim] = []
    for i, (category, sketch, _span) in enumerate([*work.sketches, *lone]):
        tracked = _state_value(work, sketch, env) if category == "quantity" else None
        if tracked is not None:
            found_claims.append(_claim(index, beat, category, sketch, tracked))
            continue
        found = _normalize_one(sketch, beat.speaker, position, env, context)
        if isinstance(found, Unnormalizable) and sketch.literal is None:
            work.noise += 1  # an unresolved common noun: noise, never a claim
            work.risky.add(category)
            continue
        found_claims.append(_claim(index, beat, category, sketch, found, work.givers.get(i)))
    for _category, sketch_or_reason, span in mention_claims:
        if isinstance(sketch_or_reason, Unnormalizable):
            found_claims.append(
                DetectedClaim(
                    span=(index, *span),
                    layer="detector",
                    category="entity_mention",
                    channel=beat.channel,
                    speaker=beat.speaker,
                    unnormalized=sketch_or_reason,
                )
            )
            continue
        found = _normalize_one(sketch_or_reason, beat.speaker, position, env, context)
        found_claims.append(_claim(index, beat, "entity_mention", sketch_or_reason, found))
    # Every channel is scanned for protected content but a speaker's own ``think`` and the
    # player's own input (#567): a hit claims the hidden record's proposition, which the
    # player's text never states ("Who has the deed?" is no "Tom holds the deed"), and the
    # input is never repaired, so the hit would stand as its assertion and reach every hearer
    # by testimony. The composed message the player's character reads is still scanned.
    if not seed and not (beat.channel == "think" and beat.speaker != NARRATOR):
        if message or beat.speaker != PLAYER:
            found_claims += _protected(work, env.protected_for(beat.speaker))
        found_claims += _private(work, env, message=message)
    # A detector claim with a declared claim's shape is that claim (declared > detector).
    by_shape = {
        shape_of(c.proposition): i for i, c in enumerate(claims) if c.proposition is not None
    }
    for claim in found_claims:
        target = None if claim.proposition is None else by_shape.get(shape_of(claim.proposition))
        # #172: a protected hit restating what the beat declares as someone's belief ("Bobby
        # thinks it was the delivery guy") is that attribution, checked against the speaker's
        # nested beliefs: not a second claim.
        if target is not None and (not claim.protected or claims[target].attribution is not None):
            old = claims[target]
            claims[target] = replace(
                old,
                merged=tuple(dict.fromkeys((*old.merged, "detector"))),
                giver=old.giver or claim.giver,  # #519: the text names who handed it over
            )
            continue
        claims.append(claim)
        if claim.category in RISK_CATEGORIES:
            work.risky.add(claim.category)
    for i, claim in enumerate(claims):
        if claim.proposition is None:
            continue
        for thunk_id, thunk_sketch in env.open_thunks:
            if fills(thunk_sketch, claim.proposition):
                claims[i] = replace(claim, matched_thunk_id=thunk_id)
                break
    flags = sorted(work.risky & RISK_CATEGORIES)
    if work.uncovered:
        flags.append(UNCOVERED_MENTION)
    if beat.channel in STRUCTURED_CHANNELS and not message and not beat.declared:
        flags.append(INCOMPLETE)
    claims.sort(key=lambda c: (c.layer != "declared", c.span[1], c.span[2]))
    return claims, BeatDetection(
        beat=index,
        channel=beat.channel,
        mentions=tuple(mentions),
        hits=dict(work.hits),
        noise=work.noise,
        flags=tuple(flags),
    )


def _state_value(beat: _Beat, sketch: ClaimSketch, env: DetectionEnv) -> Proposition | None:
    """#191: a lone quantity as a ``state_value`` claim (``core.states.verify.state_claim``):
    its subject the mention before it in its sentence (on a ``do`` beat with none, the actor),
    negated by a negation cue in its clause; ``None`` when it is no State's balance."""
    from epistrel.core.states.verify import state_claim

    literal, span = sketch.literal, sketch.span
    if (
        not env.states
        or literal is None
        or span is None
        or literal.kind is not LiteralKind.QUANTITY
    ):
        return None
    at = _token_at(beat.tokens, span[0])
    if at is None or isinstance(literal.value, str | bool):
        return None
    sentence = beat.tokens[at].sentence
    words = [t for t in beat.tokens if t.sentence == sentence]
    negated = _negated(beat.tokens, at)
    return state_claim(
        env.states,
        literal.unit,
        literal.value,
        subject=_subject(beat, span[0], sentence),
        scope_keys=env.scope_keys,
        sentence=beat.text[words[0].start : words[-1].end],
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
    )


def _claim(
    index: int | None,
    beat: DetectBeat,
    category: str,
    sketch: ClaimSketch,
    found: Proposition | Unnormalizable,
    giver: str | None = None,
) -> DetectedClaim:
    start, end = sketch.span or (0, len(beat.text))
    return DetectedClaim(
        span=(index, start, end),
        layer="detector",
        category=cast(Category, category),
        channel=beat.channel,
        speaker=beat.speaker,
        proposition=found if isinstance(found, Proposition) else None,
        unnormalized=found if isinstance(found, Unnormalizable) else None,
        sketch=sketch,
        giver=giver,
    )


def _protected(beat: _Beat, index: ProtectedIndex | None) -> list[DetectedClaim]:
    if index is None:
        return []
    found: list[DetectedClaim] = []
    seen: set[tuple[str, int]] = set()
    for start, end, term in index.match(beat.text):
        key = (term.ref_id, start)
        if key in seen:
            continue
        seen.add(key)
        beat.hit("protected_content")
        proposition = term.proposition
        found.append(
            DetectedClaim(
                span=(beat.index, start, end),
                layer="detector",
                category="protected_content",
                channel=beat.channel,
                speaker=beat.speaker,
                proposition=proposition,
                unnormalized=None
                if proposition is not None
                else Unnormalizable(VerdictReason.UNRESOLVED_ENTITY, "protected_value"),
                protected_ref=term.ref_id,
            )
        )
    return found


def _private(beat: _Beat, env: DetectionEnv, *, message: bool) -> list[DetectedClaim]:
    """#172: another character's thought repeated (the side index), an inner state narrated
    (``core.verify.inner_state``) and, on a character's ``say``, its recall claims."""
    from epistrel.core.epistemic.resurface import find_recalls

    found: list[DetectedClaim] = []

    def claim(start: int, end: int, category: str, ref: str | None, **extra: Any) -> None:
        beat.hit(category)
        found.append(
            DetectedClaim(
                span=(beat.index, start, end),
                layer="detector",
                category=cast(Category, category),
                channel=beat.channel,
                speaker=beat.speaker,
                protected_ref=ref,
                **extra,
            )
        )

    if env.private and (message or beat.speaker != PLAYER):
        for start, end, record in match_thoughts(beat.text, env.private, speaker=beat.speaker):
            claim(start, end, "protected_content", f"{THINK_REF}{record.ref}")
        if beat.channel in INNER_STATE_CHANNELS:
            exempt = frozenset({beat.speaker}) if message else frozenset()
            for hit in inner_state.find(
                beat.text,
                beat.mentions,
                env.private,
                channel=beat.channel,
                speaker=beat.speaker,
                actor=beat.speaker if beat.channel == "do" else None,
                exempt=exempt,
            ):
                if any(c.span[1] <= hit.start < c.span[2] for c in found):
                    continue
                claim(hit.start, hit.end, "inner_state", f"{THINK_REF}{hit.record.ref}")
    if beat.channel == "say" and not message and beat.speaker not in (PLAYER, NARRATOR):
        for recall in find_recalls(beat.text):
            claim(recall.start, recall.end, "recall", None, recalled=recall.content)
    return found


def detect_render(beats: Sequence[DetectBeat], position: Position, env: DetectionEnv) -> Detection:
    """One render's detection, layers (a) and (b), over every beat (see the module docstring).
    ``position`` is the render's ``(turn, beat)``; each beat is normalized at its own beat."""
    turn = turn_of(position)
    claims: list[DetectedClaim] = []
    found: list[BeatDetection] = []
    truncated = False
    for beat in beats:
        made, record = _detect_beat(beat, (turn, beat.index), env, message=False)
        claims += made
        found.append(record)
        index = env.protected_for(beat.speaker)
        truncated = truncated or bool(index is not None and index.truncated)
    return Detection(tuple(claims), tuple(found), truncated)


def detect_message(
    text: str,
    position: Position,
    env: DetectionEnv,
    *,
    speaker: str = NARRATOR,
    declared: Sequence[DeclaredClaim] = (),
) -> Detection:
    """The composed message's detection (FR-VERIFY-14; #152): the same layers over the final
    text, as ``speaker`` (the output viewpoint's character, ``narrator`` when omniscient); its
    declared claims are the verified beats' (spans are ``(None, start, end)``)."""
    beat = DetectBeat(0, MESSAGE, text, speaker, tuple(declared))
    made, record = _detect_beat(beat, position, env, message=True)
    index = env.protected_for(speaker)
    return Detection(tuple(made), (record,), bool(index is not None and index.truncated))


def detect(
    render: Sequence[DetectBeat] | str,
    speaker: str,
    position: Position,
    level: str,
    env: DetectionEnv,
    *,
    declared: Sequence[DeclaredClaim] = (),
) -> Detection:
    """``detect_render`` for a render's beats, ``detect_message`` for a composed message's
    text. ``level`` is the story's ``verification.level`` (#129): layers (a) and (b) run at every
    level, so it does not change what is found here; layer (c) is the Orchestrator's."""
    del level
    if isinstance(render, str):
        return detect_message(render, position, env, speaker=speaker, declared=declared)
    return detect_render(render, position, env)


def detect_seed(text: str, env: DetectionEnv, *, channel: str = "narration") -> Detection:
    """A seed's (or a definition field's) detection at publish and genesis (#149): speaker
    ``narrator``, position 0, no protected content, unresolved names reported."""
    beat = DetectBeat(0, channel, text, NARRATOR)
    seed_env = replace(env, protected_for=lambda _speaker: None)
    made, record = _detect_beat(beat, (0, 0), seed_env, message=True, seed=True)
    return Detection(tuple(made), (record,))


def unresolved(detection: Detection) -> list[DetectedClaim]:
    """The claims whose entity did not resolve (a seed's ``seed_unnormalizable``)."""
    return [
        c
        for c in detection.claims
        if c.unnormalized is not None and c.unnormalized.reason is VerdictReason.UNRESOLVED_ENTITY
    ]


def merge(
    detection: Detection, extracted: Sequence[DetectedClaim], *, overlap: float = 0.5
) -> Detection:
    """Add layer (c)'s claims: an extraction claim overlapping a claim of the same beat by at
    least ``overlap`` of the longer span (or with the same shape) is that claim, merged (the
    earlier layer's polarity and time stand; an extraction proposition fills a detector claim
    that had none); every other one is added. Priority: declared > detector > extraction."""
    claims = list(detection.claims)
    for new in extracted:
        target = None
        for i, old in enumerate(claims):
            if old.beat != new.beat:
                continue
            a0, a1 = old.span[1], old.span[2]
            b0, b1 = new.span[1], new.span[2]
            longer = max(1, a1 - a0, b1 - b0)
            shared = max(0, min(a1, b1) - max(a0, b0))
            same_shape = (
                old.proposition is not None
                and new.proposition is not None
                and shape_of(old.proposition) == shape_of(new.proposition)
            )
            if old.layer == "declared" and not same_shape:
                continue
            if same_shape or shared / longer >= overlap:
                target = i
                break
        if target is None:
            claims.append(new)
            continue
        old = claims[target]
        update: dict[str, Any] = {"merged": tuple(dict.fromkeys((*old.merged, "extraction")))}
        if old.proposition is None and new.proposition is not None and old.layer != "declared":
            update |= {"proposition": new.proposition, "unnormalized": None}
        claims[target] = replace(old, **update)
    claims.sort(key=lambda c: (-1 if c.beat is None else c.beat, c.layer != "declared", c.span[1]))
    return detection.with_claims(claims)


__all__ = [
    "DETECTOR_CATEGORIES",
    "INCOMPLETE",
    "LAYERS",
    "MESSAGE",
    "RISK_CATEGORIES",
    "SCENE_CHANNELS",
    "STRUCTURED_CHANNELS",
    "THINK_REF",
    "UNCOVERED_MENTION",
    "BeatDetection",
    "Category",
    "DeclaredClaim",
    "DetectBeat",
    "DetectedClaim",
    "Detection",
    "DetectionEnv",
    "Layer",
    "Span",
    "counts_of",
    "detect",
    "detect_message",
    "detect_render",
    "detect_seed",
    "merge",
    "parse_number",
    "render_quantity",
    "shape_hash",
    "shape_of",
    "tables",
    "unresolved",
]
