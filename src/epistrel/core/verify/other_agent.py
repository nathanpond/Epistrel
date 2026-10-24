"""Other-agent claims in player input (FR-RESP-11, FR-AUTH-3; docs/02 §4.9; #180, on #148's
detectors). Pure: nothing here reads the database or calls a model.

The player's input supplies only its actor's speech and action (FR-AUTH-3). What it says
*another* character does, says or thinks is an **other-agent claim** (category
``other_agent_claim``): ``detect`` finds them deterministically, from the English lexicon
``detectors/speech_attribution.json`` and the inner-state cues (``detectors/inner_state.json``):

- in the input's ``do``: a sentence whose subject is another modeled character (its name first,
  after an optional lead word such as "then"; or a subject pronoun, resolved to the
  last-mentioned character, an unresolved one left alone). A thought cue makes it a ``think``,
  a speech verb or a quotation a ``say``, anything else a ``do``. A subject joined with the
  player ("Jenni and I") is the player's own action;
- in the input's ``say``: only quoted speech attributed to another character (a quotation next to
  "<name> says" or "says <name>"): everything else the player says is its own speech, an
  attribution of someone's belief included (#167's, never authority).

An unmodeled name never matches (it stays #144's world texture), and a lexicon miss passes as the
player's text (a documented limit: no extraction runs for this). The orchestrator decides what a
claim becomes (``orchestrator.structured.player_authority``: a request under ``own_character``,
narration under ``co_author``). ``first_person`` rewrites a narrated span's first-person words to
the player character's name, so "Jenni hands me the key" is narrated as "Jenni hands Cal the key".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Literal

from epistrel.core.epistemic import roles as reaction_rules

OTHER_AGENT_CLAIM = "other_agent_claim"
"""#148's detector category of a player-input span about another character (#180)."""

PLAYER_NARRATOR = "player"
"""The ``narrated_by`` of a beat the player narrated for another character under
``player_authority = co_author`` (#180; a reactive reaction's names the narrating character)."""

ClaimKind = Literal["do", "say", "think"]
Channel = Literal["say", "do"]


@cache
def lexicon() -> Mapping[str, Any]:
    """``detectors/speech_attribution.json``."""
    source = resources.files("epistrel.core.verify").joinpath("detectors/speech_attribution.json")
    loaded: dict[str, Any] = json.loads(source.read_text("utf-8"))
    return loaded


@dataclass(frozen=True, slots=True)
class OtherAgentClaim:
    """One span of player input about another character: its kind, the character, the channel
    it was found in and its ``[start, end)`` there, and the text as typed."""

    kind: ClaimKind
    character_id: str
    channel: Channel
    start: int
    end: int
    text: str
    category: str = OTHER_AGENT_CLAIM

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "character_id": self.character_id,
            "text": self.text,
            "span": [self.channel, self.start, self.end],
        }


def sentences(text: str) -> Iterator[tuple[int, int]]:
    """The ``[start, end)`` of each sentence of ``text`` (whitespace trimmed), a sentence end
    inside a quotation not counted."""
    quotes = set(lexicon()["quotes"])
    opening, closing = "“", "”"
    start = 0
    inside = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in quotes:
            was = inside
            if ch == opening:
                inside = True
            elif ch == closing:
                inside = False
            else:
                inside = not inside
            if was and not inside and i > 0 and text[i - 1] in ".!?":
                # A quotation that ends its sentence ('Jenni says "Here." I nod.').
                yield from _trimmed(text, start, i + 1)
                start = i + 1
        elif ch == "\n" or (ch in ".!?" and not inside):
            end = i + 1
            while end < n and (text[end] in ".!?" or (text[end] in quotes and ch != "\n")):
                end += 1
            yield from _trimmed(text, start, end)
            start = end
            inside = False
            i = end
            continue
        i += 1
    yield from _trimmed(text, start, n)


def _trimmed(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    chunk = text[start:end]
    if not chunk.strip():
        return
    lead = len(chunk) - len(chunk.lstrip())
    tail = len(chunk) - len(chunk.rstrip())
    yield start + lead, end - tail


def _starts_with(folded: str, word: str) -> bool:
    return folded.startswith(word) and (len(folded) == len(word) or not folded[len(word)].isalnum())


def _subject(
    sentence: str, by_name: Sequence[tuple[str, str]], last: str | None
) -> tuple[str | None, int]:
    """The character a sentence's subject names (``None``: none) and where its predicate
    starts."""
    table = lexicon()
    folded = sentence.casefold()
    offset = 0
    for lead in table["lead_words"]:
        if _starts_with(folded, lead):
            rest = folded[len(lead) :]
            stripped = rest.lstrip(" ,")
            offset = len(folded) - len(stripped)
            folded = stripped
            break
    for name, cid in by_name:
        lead_name = name.casefold()
        if _starts_with(folded, lead_name):
            after = folded[len(lead_name) :].lstrip()
            if any(after.startswith(j + " ") or after == j for j in table["joint_subjects"]):
                return None, 0
            return cid, offset + len(lead_name)
    for pronoun in table["subject_pronouns"]:
        if _starts_with(folded, pronoun):
            return last, offset + len(pronoun)
    return None, 0


def _mentioned(sentence: str, by_name: Sequence[tuple[str, str]]) -> str | None:
    """The last character ``sentence`` names (for the next sentence's pronoun)."""
    folded = sentence.casefold()
    found: tuple[int, str] | None = None
    for name, cid in by_name:
        for match in re.finditer(rf"(?<![\w]){re.escape(name.casefold())}(?![\w])", folded):
            if found is None or match.start() > found[0]:
                found = (match.start(), cid)
    return None if found is None else found[1]


@cache
def _speech() -> re.Pattern[str]:
    words = sorted(lexicon()["verbs"], key=lambda w: (-len(w), w))
    return re.compile(rf"(?<![\w'\u2019])(?:{'|'.join(map(re.escape, words))})(?![\w'\u2019])")


def _kind(predicate: str) -> ClaimKind:
    folded = " ".join(predicate.casefold().split())
    category = reaction_rules.classify(predicate)
    if category == "inner":
        return "think"
    if reaction_rules.quoted(predicate) or _speech().search(folded):
        return "say"
    return "do"


def _by_name(others: Mapping[str, str]) -> list[tuple[str, str]]:
    return sorted(((n, c) for n, c in others.items() if n.strip()), key=lambda kv: -len(kv[0]))


def detect_do(text: str, others: Mapping[str, str]) -> list[OtherAgentClaim]:
    """The other-agent claims of the input's ``do`` (``others``: every modeled character's name
    but the actor's → its id)."""
    by_name = _by_name(others)
    found: list[OtherAgentClaim] = []
    last: str | None = None
    for start, end in sentences(text):
        sentence = text[start:end]
        cid, at = _subject(sentence, by_name, last)
        if cid is not None:
            found.append(OtherAgentClaim(_kind(sentence[at:]), cid, "do", start, end, sentence))
        last = _mentioned(sentence, by_name) or last
    return found


_QUOTED = re.compile(r"[\"“][^\"“”]*[\"”]")


def detect_say(text: str, others: Mapping[str, str]) -> list[OtherAgentClaim]:
    """The quoted speech the input's ``say`` attributes to another character."""
    by_name = _by_name(others)
    verbs = _speech().pattern
    found: list[OtherAgentClaim] = []
    for quote in _QUOTED.finditer(text):
        before = text[: quote.start()]
        after = text[quote.end() :]
        for name, cid in by_name:
            n = re.escape(name)
            lead = re.search(rf"(?i)(?<![\w]){n}\s+{verbs}\s*[,:]?\s*$", before)
            if lead is not None:
                found.append(OtherAgentClaim("say", cid, "say", lead.start(), quote.end(), ""))
                break
            tail = re.match(rf"(?i)\s*,?\s*(?:{n}\s+{verbs}|{verbs}\s+{n})(?![\w])[.!?]?", after)
            if tail is not None:
                found.append(
                    OtherAgentClaim("say", cid, "say", quote.start(), quote.end() + tail.end(), "")
                )
                break
    return [
        OtherAgentClaim(c.kind, c.character_id, c.channel, c.start, c.end, text[c.start : c.end])
        for c in found
    ]


def detect(say: str | None, do: str | None, others: Mapping[str, str]) -> list[OtherAgentClaim]:
    """Every other-agent claim of one input: its ``say``'s, then its ``do``'s (see the module
    docstring)."""
    return [
        *(detect_say(say, others) if say else []),
        *(detect_do(do, others) if do else []),
    ]


def first_person(text: str, name: str) -> str:
    """``text`` with its first-person words rewritten to ``name`` (a narrated span's)."""
    table: Mapping[str, str] = lexicon()["first_person"]
    words = sorted(table, key=lambda w: (-len(w), w))
    pattern = re.compile(
        rf"(?<![\w'\u2019])({'|'.join(map(re.escape, words))})(?![\w'\u2019])", re.IGNORECASE
    )
    return pattern.sub(lambda m: table[m.group(1).casefold()].format(name=name), text)


def without(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` with ``spans`` removed (whitespace closed up)."""
    return reaction_rules.without(text, spans)


__all__ = [
    "OTHER_AGENT_CLAIM",
    "PLAYER_NARRATOR",
    "ClaimKind",
    "OtherAgentClaim",
    "detect",
    "detect_do",
    "detect_say",
    "first_person",
    "lexicon",
    "sentences",
    "without",
]
