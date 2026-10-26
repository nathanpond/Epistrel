"""Reworded disclosures of a private fact, its feeling or its thought (#575): the private-thought
oracle's second matcher, beside #148's exact folded phrases (``fixtures.disclosures``) and #152's
shingle detector (``fixtures.echoes``).

The method is **lemma and stem matching within a sentence**, in-repo and deterministic: no model
and no NLP library (invariants 1, 2 and 5). Each word is folded to a stem: a small lemma table
for irregular and derived forms (``sold``/``sale`` → ``sell``, ``told`` → ``tell``, ``guilty`` →
``guilt``), then a few suffix rules (``-s``, ``-ies``, ``-ing``, ``-ed``, ``-ly``, a final
``-e``), so "sold", "sells" and "selling" share a stem. Function words and generic verbs are
dropped. A sentence (text split at ``.``, ``!``, ``?``, ``;``, ``:`` and line breaks) discloses:

- **the fact** when it holds every content stem of one of the fact's forms that has two or more
  ("selling the farm" → ``sell`` + ``farm``: "Frank has sold the farm.", "The farm's been sold,
  Frank told me."). A one-word form ("poker", "on the market") stays an exact phrase.
- **the feeling** when it holds an emotion word of the same family as one of the fact's
  narrated feelings (fear, shame, pleasure, distress, intent: "dreading" and "worried" are
  both fear) and one of the fact's topic stems (the content stems of its feelings and the head,
  the last content stem, of each of its forms; cast names and generic words excluded):
  "Frank feels guilty about the farm.", "Frank looks like he's dreading telling us about the
  farm."
- **the thought** when it holds at least 60% (and at least two) of the thought's content stems
  (generic words excluded; a thought with fewer than two is left to the shingle detector), in
  any order and inflection: "Frank signed the contract on Tuesday" for "I signed the sale
  contract on Tuesday and I still haven't told any of them."

**Limits** (stated in the suite's report notes and ``docs/evals/README.md``): the matcher covers
inflections, word order, possessives and the listed synonyms only. A paraphrase that shares no
content stem with a listed form, feeling or thought ("Frank's letting the place go to a
developer") is not seen, nor one split across sentences, nor an implication with no fact word
in it. A sentence with an emotion word and a topic word that discloses nothing ("Linda's
worried the house is cold" in the farm-sale case) is a false positive. The negative unit cases
(``test_e_08__m5_think_leak_reworded_neutral``) hold neutral lines that name a fact's world and
every clean scripted line of the set.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from epistrel.evals.fixtures import Fixture, PrivateFact

THOUGHT_SHARE = 0.6
"""The share of a thought's content stems a sentence must hold to restate it."""

_CURLY = chr(0x2019)
"""A typographic apostrophe, read as ``'``."""
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SENTENCE = re.compile(r"[.!?;:\n]+")
_CLITIC = re.compile(r"'(?:s|d|ll|re|ve|m)$|n't$|'$")

_STOP_WORDS = """a an the and or but if so than then that this these those there here of to in on at
    by for from with without about into onto over under after before since until up down out off
    as is am are was were be been being have has had having do does did doing done will would
    shall should can could may might must not no nor never ever still just even yet also too
    very really quite rather only all any some every each either neither both much many more
    most other another such own same what which who whom whose when where why how i me my mine
    myself we us our ours you your yours he him his himself she her hers herself it its itself
    they them their theirs themselves one ones nobody anybody somebody everybody nothing
    something anything everything someone anyone everyone none else once now like going ca wo
    n't s d ll re ve m oh well yes yeah okay ok"""
STOP = frozenset(_STOP_WORDS.split())
"""Function words: never a content stem."""

LEMMA = {
    "sold": "sell",
    "sale": "sell",
    "sales": "sell",
    "told": "tell",
    "lost": "lose",
    "found": "find",
    "left": "leave",
    "took": "take",
    "taken": "take",
    "bought": "buy",
    "stole": "steal",
    "stolen": "steal",
    "theft": "steal",
    "thief": "steal",
    "panicked": "panic",
    "wrote": "write",
    "written": "write",
    "sent": "send",
    "won": "win",
    "winnings": "win",
    "got": "get",
    "gotten": "get",
    "paid": "pay",
    "knew": "know",
    "known": "know",
    "made": "make",
    "said": "say",
    "saw": "see",
    "seen": "see",
    "went": "go",
    "gone": "go",
    "came": "come",
    "kept": "keep",
    "felt": "feel",
    "drank": "drink",
    "drunk": "drink",
    "flew": "fly",
    "flown": "fly",
    "flight": "fly",
    "pilot": "fly",
    "guilty": "guilt",
    "relief": "relieve",
    "forgery": "forge",
    "forger": "forge",
    "eviction": "evict",
    "adoption": "adopt",
    "engagement": "engage",
    "marriage": "marry",
    "married": "marry",
    "pregnancy": "pregnant",
    "gambler": "gamble",
    "fear": "afraid",
    "frightened": "afraid",
    "scared": "afraid",
    "terrified": "afraid",
    "anxiety": "anxious",
    "shame": "ashamed",
    "embarrassed": "ashamed",
    "humiliated": "ashamed",
    "glee": "gleeful",
    "thrill": "thrilled",
    "excitement": "excited",
    "despair": "desperate",
    "temptation": "tempted",
    "stress": "stressed",
    "plot": "plan",
    "scheme": "plan",
    "secretly": "plan",
}
"""Irregular and derived forms, folded to one lemma before the suffix rules."""

EMOTIONS = {
    "fear": """afraid dread nervous anxious worried worry panic panicked jumpy paranoid uneasy
        jittery shaken shaky petrified apprehensive tense edgy""",
    "shame": "guilt ashamed remorseful sheepish",
    "pleasure": """excited thrilled pleased delighted happy glad relieve smug gloating gloat
        gleeful enjoy proud elated greedy""",
    "distress": """desperate torn conflicted confused overwhelmed stressed struggling struggle
        tempted upset distraught troubled""",
    "intent": "plan intend",
}
"""Emotion words by family (a narrated feeling's word matches any word of its family)."""

_GENERIC_WORDS = """tell find know say see look make take come give keep put think feel want need
    let get go thing people man woman time day week cause home lose two back"""


def stem(word: str) -> str:
    """``word``'s stem (lower-cased, clitics dropped, the lemma table, then suffix rules)."""
    w = _CLITIC.sub("", word.lower().replace(_CURLY, "'"))
    w = LEMMA.get(w, w)
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    w = LEMMA.get(w, w)
    for suffix in ("ing", "ed"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            if w[-1] == w[-2] and w[-1] not in "aeioulsz":
                w = w[:-1]
            break
    if len(w) > 5 and w.endswith("ly"):
        w = w[:-2]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    if w.endswith("i"):
        w = w[:-1] + "y"
    return LEMMA.get(w, w)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace(_CURLY, "'"))


def content(text: str) -> list[str]:
    """``text``'s content stems in order (function words dropped)."""
    out = []
    for token in _tokens(text):
        bare = _CLITIC.sub("", token)
        if bare and bare not in STOP and token not in STOP:
            out.append(stem(token))
    return out


_EMOTION: dict[str, str] = {stem(w): family for family, ws in EMOTIONS.items() for w in ws.split()}
GENERIC = frozenset(stem(w) for w in _GENERIC_WORDS.split())
"""Content stems too common to be a feeling's topic or a thought's evidence."""


def sentences(text: str) -> list[set[str]]:
    """``text``'s sentences, each as its set of content stems."""
    return [set(content(part)) for part in _SENTENCE.split(text) if part.strip()]


@dataclass(frozen=True, slots=True)
class Profile:
    """What a reworded disclosure of one private fact holds (the module docstring)."""

    bags: tuple[tuple[str, frozenset[str]], ...]
    families: frozenset[str]
    topics: frozenset[str]
    thought: frozenset[str]
    thought_needed: int


def profile(fixture: Fixture, fact: PrivateFact) -> Profile:
    """``fact``'s stems: its multi-word forms' bags, its feelings' emotion families, its topics,
    and its thought's content stems with the count a restatement needs."""
    names = {stem(n) for n in (*fixture.names, fixture.player.name)}
    bags = []
    heads: set[str] = set()
    for form in fact.forms:
        stems = content(form)
        if stems:
            heads.add(stems[-1])
        if len(set(stems)) >= 2:
            bags.append((form, frozenset(stems)))
    families: set[str] = set()
    for feeling in fact.feelings:
        for s in content(feeling):
            if s in _EMOTION:
                families.add(_EMOTION[s])
            else:
                heads.add(s)
    topics = frozenset(
        s for s in heads if s not in _EMOTION and s not in GENERIC and s not in names
    )
    thought = frozenset(s for s in content(fact.think) if s not in GENERIC)
    needed = max(2, math.ceil(THOUGHT_SHARE * len(thought))) if len(thought) >= 2 else 0
    return Profile(tuple(bags), frozenset(families), topics, thought, needed)


def reworded(text: str | None, fixture: Fixture, fact: PrivateFact) -> list[str]:
    """The reworded disclosures of ``fact`` ``text`` holds, each named by its kind and the stems
    it matched (``fact: farm+sell``, ``feeling: guilt+farm``, ``thought: contract+sign+tuesday``);
    empty when it holds none."""
    if not text:
        return []
    p = profile(fixture, fact)
    found: set[str] = set()
    for words in sentences(text):
        for _, bag in p.bags:
            if bag <= words:
                found.add("fact: " + "+".join(sorted(bag)))
        if p.families and p.topics & words:
            feeling = sorted(w for w in words if _EMOTION.get(w) in p.families)
            if feeling:
                found.add("feeling: " + "+".join([*feeling, *sorted(p.topics & words)]))
        shared = p.thought & words
        if p.thought_needed and len(shared) >= p.thought_needed:
            found.add("thought: " + "+".join(sorted(shared)))
    return sorted(found)


__all__ = [
    "EMOTIONS",
    "GENERIC",
    "LEMMA",
    "STOP",
    "THOUGHT_SHARE",
    "Profile",
    "content",
    "profile",
    "reworded",
    "sentences",
    "stem",
]
