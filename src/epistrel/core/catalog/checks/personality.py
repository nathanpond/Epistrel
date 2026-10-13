"""The personality check (FR-SCN-3,5,7; #111), and the heuristic it shares with genesis (#122).

**The rule.** Personality comes from the character who fills a role, never from the scenario: a
role defines situation, not personality (FR-SCN-3). Role text that asserts personality is
kept only as guidance that yields to the character ("personality: character over scenario",
FR-SCN-7). For every ``open`` and ``default`` role (the player's included, which is ``open``),
validation warns about each such sentence in the role's setup description and in its seeds'
texts: one ``personality_in_role`` warning per sentence, quoting it as ``text``, with outcome
``ignored``, the rule, and as ``source`` the name of the ``default`` role's character (whose
personality applies instead; ``null`` for an ``open`` role, filled only at story creation, where
#122 names the character). A ``locked`` role's text is not checked: the role and its character
are written together.

**The heuristic** (``personality_sentences``) is deliberately simple, and a false positive only
costs a warning. ``PERSONALITY_TERMS`` lists words and phrases of disposition: temperament,
honesty, warmth, dominance, caution, sociability, temper. A term matches as whole words, case
ignored, also inflected (``liars``, ``honestly``, ``kindness``: the endings in ``SUFFIXES``);
a phrase matches with spaces or hyphens between its words (``quick to anger``,
``short-tempered``). A match is skipped when a negation (``NEGATIONS``) comes within the three
words before it in its sentence ("she is not a liar"), and "kind of"/"kinds of" is never the
disposition. A text is split into sentences at ``.``, ``!`` or ``?`` followed by space, and at
line breaks; each sentence with a match is reported once, as it stands in the text.
"""

from __future__ import annotations

import re
from typing import Final

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import filled
from epistrel.core.catalog.checks.character_documents import name_of
from epistrel.core.catalog.checks.scenario import draft_of, role_field
from epistrel.core.catalog.report import Entry, Outcome

RULE: Final = "personality: character over scenario"

PERSONALITY_TERMS: Final = (
    # honesty and deceit
    "honest",
    "dishonest",
    "truthful",
    "sincere",
    "insincere",
    "liar",
    "habitual liar",
    "deceitful",
    "deceptive",
    "manipulative",
    "two-faced",
    "trustworthy",
    "untrustworthy",
    # warmth
    "warm",
    "cold",
    "kind",
    "unkind",
    "cruel",
    "cruelty",
    "caring",
    "compassionate",
    "callous",
    "heartless",
    "gentle",
    "harsh",
    "loving",
    "affectionate",
    "aloof",
    "friendly",
    "unfriendly",
    "hostile",
    "spiteful",
    "generous",
    "selfish",
    "greedy",
    "jealous",
    "vain",
    "arrogant",
    "humble",
    "rude",
    "polite",
    "kind-hearted",
    "warm-hearted",
    "cold-hearted",
    "good-natured",
    "mean-spirited",
    # dominance and submission
    "dominant",
    "domineering",
    "bossy",
    "controlling",
    "submissive",
    "meek",
    "timid",
    "assertive",
    "pushy",
    # caution and daring
    "cautious",
    "reckless",
    "impulsive",
    "careless",
    "daring",
    "fearless",
    "fearful",
    "anxious",
    "paranoid",
    # sociability
    "shy",
    "outgoing",
    "introverted",
    "extroverted",
    "reserved",
    "talkative",
    "chatty",
    "gregarious",
    "sociable",
    "withdrawn",
    # temper and outlook
    "short-tempered",
    "hot-headed",
    "hot-tempered",
    "quick-tempered",
    "ill-tempered",
    "level-headed",
    "irritable",
    "grumpy",
    "moody",
    "temperamental",
    "volatile",
    "cheerful",
    "optimistic",
    "pessimistic",
    "patient",
    "impatient",
    "stubborn",
    "lazy",
    "diligent",
    "ambitious",
    "brave",
    "cowardly",
    "loyal",
    "disloyal",
    "naive",
    "cynical",
    "suspicious",
    "trusting",
    "easygoing",
    "easy-going",
    "quick to anger",
    "slow to trust",
    "quick to forgive",
)
"""Words and phrases of disposition (see the module docstring)."""

SUFFIXES: Final = ("s", "es", "ly", "ness")
"""Endings a term's last word may take and still match: ``liars``, ``honestly``, ``kindness``."""

NEGATIONS: Final = frozenset(
    {
        "not",
        "never",
        "no",
        "nor",
        "neither",
        "hardly",
        "isn't",
        "wasn't",
        "aren't",
        "weren't",
        "isnt",
        "wasnt",
        "nothing",
    }
)
"""Words that, within the three words before a term in its sentence, negate it."""

NEGATION_WINDOW: Final = 3

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")
_SUFFIX = "(?:" + "|".join(SUFFIXES) + ")?"


def _pattern(term: str) -> str:
    words = [re.escape(word) for word in re.split(r"[\s-]+", term) if word]
    return r"(?<![\w-])" + r"[\s-]+".join(words) + _SUFFIX + r"(?![\w-])"


_TERMS = re.compile("|".join(_pattern(t) for t in PERSONALITY_TERMS), re.IGNORECASE)
_NOT_A_DISPOSITION = re.compile(r"\s+of\b", re.IGNORECASE)


def _negated(sentence: str, start: int) -> bool:
    before = [word.lower().replace("\u2019", "'") for word in _WORD.findall(sentence[:start])]
    return any(word in NEGATIONS for word in before[-NEGATION_WINDOW:])


def _asserted(sentence: str) -> bool:
    for match in _TERMS.finditer(sentence):
        if match.group(0).lower() in ("kind", "kinds") and _NOT_A_DISPOSITION.match(
            sentence, match.end()
        ):
            continue
        if not _negated(sentence, match.start()):
            return True
    return False


def personality_sentences(text: str) -> list[str]:
    """Each sentence of ``text`` that asserts personality (see the module docstring), once, in
    order, as it stands in the text (surrounding whitespace removed)."""
    found: list[str] = []
    for part in _SENTENCE.split(text):
        sentence = part.strip()
        if sentence and sentence not in found and _asserted(sentence):
            found.append(sentence)
    return found


def asserts_personality(text: str) -> bool:
    """Whether any sentence of ``text`` asserts personality."""
    return bool(personality_sentences(text))


class PersonalityCheck(Check):
    """See the module docstring."""

    name = "personality"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        draft = draft_of(subject)
        names = {
            member.index: name_of(member.document)
            for member in await filled(subject, ctx)
            if member.role.fill == "default"
        }
        entries: list[Entry] = []
        for index, role in enumerate(draft.cast.roles):
            if role.fill not in ("open", "default"):
                continue
            texts: list[tuple[str, str, str | None]] = [
                (role_field(index, "setup_description"), role.setup_description, None)
            ]
            texts += [
                (role_field(index, "seeded_memories", seed_index, "text"), seed.text, seed.id)
                for seed_index, seed in enumerate(role.seeded_memories)
            ]
            for field, text, seed_id in texts:
                for sentence in personality_sentences(text):
                    entries.append(
                        subject.entry(
                            "personality_in_role",
                            Outcome.ignored,
                            field=field,
                            role=role.id,
                            seed_id=seed_id,
                            text=sentence,
                            source=names.get(index),
                            rule=RULE,
                        )
                    )
        return Findings(entries=entries)


CHECK = PersonalityCheck()

__all__ = [
    "CHECK",
    "NEGATIONS",
    "PERSONALITY_TERMS",
    "RULE",
    "SUFFIXES",
    "PersonalityCheck",
    "asserts_personality",
    "personality_sentences",
]
