"""#1060 (the owner's decisions, 2026-11-28): the actor's claims verification does not repair for
want of support, though canon holds nothing that supports them.

**An echo of the actor's own input.** A rendered beat of the turn's actor that restates its own
``player_input`` (the consumer scripted the host's "It's time to vote.", and the render said it
again) adds nothing the input did not already say. The input beat itself is never repaired, so
neither is the claim restating it: a claim of the actor's rendered beat whose words all appear,
in order, inside this turn's ``say`` or ``do`` (``echoes``) passes when it is only uncertain.

**A pending ephemeral directive.** With the story setting ``directive_support`` (FR-DIRECT-2):

- ``support`` (the default): a claim of the actor's rendered beat that shares a content word with
  a directive the turn carries (``directed``) passes when it is only uncertain. The direction
  licenses the beat; it does not make the directive's content canon (an uncertain claim never
  canonizes), so FR-DIRECT-2's "never persisted as canon" holds.
- ``selfonly``: only such a claim about the actor itself (its own action or position: the
  claim's subject is the actor) passes. A claim about another character or the world is
  verified as before.

Both pass only what is *uncertain*: a contradicted claim, a denied one (another character's
secret), a hallucination, a State finding, a copied line or a leaked thought is repaired as it
always was. The pass is recorded with the ``pass`` disposition (#978's), so the turn report
still shows the claim and that nothing supported it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from epistrel.core.settings_schema import DirectiveSupport

_WORD = re.compile(r"[a-z0-9']+")
STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "been",
        "before",
        "being",
        "does",
        "each",
        "from",
        "have",
        "here",
        "into",
        "just",
        "like",
        "make",
        "more",
        "must",
        "next",
        "onto",
        "only",
        "over",
        "should",
        "some",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "very",
        "what",
        "when",
        "will",
        "with",
        "your",
    }
)
"""Words too common to tie a claim to a directive (``content_words`` keeps words of four or
more letters that are not here)."""


def words(text: str) -> list[str]:
    """``text``'s words, lowercased, punctuation dropped ("It's time to vote." → its, time,
    to, vote)."""
    return [w.strip("'") for w in _WORD.findall(text.lower()) if w.strip("'")]


def echoes(span: str, inputs: Iterable[str]) -> bool:
    """Whether ``span`` (a claim's text) restates one of ``inputs``: its words, in order, are a
    run of one input's words. An empty span echoes nothing."""
    said = words(span)
    if not said:
        return False
    needle = f" {' '.join(said)} "
    return any(needle in f" {' '.join(words(line))} " for line in inputs if line)


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_words(text: str) -> frozenset[str]:
    """``text``'s content words, crudely stemmed: four letters or more, not a ``STOPWORDS``
    word ("settles by the fire" → settl, fire)."""
    return frozenset(_stem(w) for w in words(text) if len(w) >= 4 and w not in STOPWORDS)


def directed(
    span: str,
    steers: Iterable[str],
    *,
    mode: DirectiveSupport,
    about_actor: bool,
) -> bool:
    """Whether a pending directive licenses a claim of the actor's beat (see the module
    docstring): the claim's text shares a content word with a directive's, and under
    ``selfonly`` the claim is about the actor (``about_actor``)."""
    if mode == "selfonly" and not about_actor:
        return False
    told = frozenset().union(*(content_words(s) for s in steers if s))
    return bool(told & content_words(span))


__all__ = ["STOPWORDS", "content_words", "directed", "echoes", "words"]
