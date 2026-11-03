"""Skip authority: who may move the clock by how much (FR-CLOCK-7,8; docs/02 §7.1; #199).

A **skip** is a time advance a render narrates beyond its spoken length: one it declares (a
``time_advance`` of ``minutes`` or ``to``) or one the Orchestrator estimated for an elapsed
span it narrates without declaring (``time_estimation``, ``source = estimate``). Every skip is
classified, then authorized, before it is timed (``core.world_clock.timing``) and checked
against the due events (#196's ``check_advance``):

- **``classify_skip(skip, player_input, directives)``** → ``PlayerInitiated`` when the player's
  input (or an author directive, ``by = author``) states or invites it: an explicit amount
  ("we wait two hours": ``minutes``), a request to move on ("skip to the next day", "later"),
  or an activity that implies elapsed time ("I fall asleep on the couch", "I drive to
  Columbus"); else ``ModelInitiated``, carrying the activities the skip's own text narrates.
- **``authorize(classified, minutes=, cap=, established=)``**: a player- or author-initiated
  skip is ``Accepted`` whatever its length (FR-CLOCK-8: uncapped). A model-initiated one is
  accepted only when ``cap`` (the clock's ``max_unprompted_skip_minutes``) is not 0, it
  narrates an activity the scene established (``established``: the activities of a ``fluent``
  fact or an earlier ``do`` beat of the scene, ``established``), and its minutes do not exceed
  ``cap``; otherwise ``Refused`` with its reason (``REFUSALS``) and the Orchestrator removes it
  (repair reason ``unprompted_skip``).
- **``ACTIVITY_LEXICON``**: the activities, each with the words that name it ("watch": watch,
  movie, film, …; "sleep": sleep, asleep, doze, …). Matching is deterministic and English, like
  every detector table: no model judges authority.

An estimate under ``MIN_CONFIDENCE`` is no estimate (the skip is removed as unestimated,
NFR-PERF-7 (5)). The consumer's ``POST /stories/{id}/clock/advance`` is the author's authority
and never passes through here.

Pure: no I/O, no model call; nothing here logs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Final, Literal

MIN_CONFIDENCE: Final = 0.3
"""An estimate below this confidence is treated as no estimate (#199's planner decision)."""

PLAYER: Final = "player"
AUTHOR: Final = "author"
MODEL: Final = "model"
Initiator = Literal["player", "author", "model"]

NO_ACTIVITY: Final = "no_established_activity"
"""A model-initiated skip narrating no activity the scene established."""
OVER_CAP: Final = "over_cap"
"""A model-initiated skip longer than ``max_unprompted_skip_minutes``."""
DISABLED: Final = "model_skips_disabled"
"""A model-initiated skip while ``max_unprompted_skip_minutes`` is 0."""
REFUSALS: Final = (DISABLED, NO_ACTIVITY, OVER_CAP)

ACTIVITY_LEXICON: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "watch": (
            "watch",
            "watches",
            "watched",
            "watching",
            "movie",
            "movies",
            "film",
            "films",
            "show",
            "episode",
            "episodes",
            "tv",
            "television",
            "credits",
        ),
        "sleep": (
            "sleep",
            "sleeps",
            "slept",
            "sleeping",
            "asleep",
            "nap",
            "naps",
            "napping",
            "doze",
            "dozes",
            "dozed",
            "dozing",
            "drift off",
            "drifts off",
            "drifted off",
        ),
        "drive": ("drive", "drives", "drove", "driving", "driven"),
        "travel": (
            "travel",
            "travels",
            "travelled",
            "traveled",
            "travelling",
            "traveling",
            "journey",
            "trip",
            "ride",
            "rides",
            "rode",
            "riding",
            "flight",
            "fly",
            "flies",
            "flew",
        ),
        "walk": (
            "walk",
            "walks",
            "walked",
            "walking",
            "hike",
            "hikes",
            "hiked",
            "hiking",
            "stroll",
            "strolls",
            "strolled",
        ),
        "wait": ("wait", "waits", "waited", "waiting"),
        "work": ("work", "works", "worked", "working", "shift"),
        "cook": ("cook", "cooks", "cooked", "cooking", "bake", "bakes", "baked", "baking"),
        "eat": ("eat", "eats", "ate", "eating", "dinner", "lunch", "breakfast", "meal"),
        "read": ("read", "reads", "reading", "book", "chapter"),
        "talk": (
            "talk",
            "talks",
            "talked",
            "talking",
            "chat",
            "chats",
            "chatted",
            "chatting",
            "conversation",
        ),
        "play": ("play", "plays", "played", "playing", "game", "match"),
    }
)
"""Each activity a skip may narrate, with the words that name it (#199's planner's
``watch|drive|sleep|wait|walk|travel|work|cook|…``)."""

INVITATIONS: Final = (
    "skip",
    "skips",
    "skipping",
    "later",
    "tomorrow",
    "next day",
    "next morning",
    "next week",
    "until",
    "till",
    "fast forward",
    "fast-forward",
    "meanwhile",
)
"""Words with which a player's input (or a directive) asks the story to move on in time."""

_UNITS: Final = {"minute": 1, "hour": 60, "day": 1440, "week": 10080}
_AMOUNTS: Final = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "fifty": 50,
    "sixty": 60,
    "ninety": 90,
}


def _words(words: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted({w for w in words if w}, key=lambda w: (-len(w), w))
    body = "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in ordered)
    return re.compile(rf"(?<![\w'\u2019])(?:{body})(?![\w'\u2019])", re.IGNORECASE)


@cache
def _lexicon() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, _words(words)) for name, words in ACTIVITY_LEXICON.items())


@cache
def _invitation() -> re.Pattern[str]:
    return _words(INVITATIONS)


@cache
def _amount() -> re.Pattern[str]:
    amounts = "|".join(sorted((re.escape(a) for a in _AMOUNTS), key=len, reverse=True))
    units = "|".join(_UNITS)
    return re.compile(
        rf"(?<![\w'\u2019])(?P<n>\d+|{amounts}|half\s+an?)\s+(?P<unit>{units})s?(?![\w'\u2019])",
        re.IGNORECASE,
    )


def activities(text: str) -> frozenset[str]:
    """The activities (``ACTIVITY_LEXICON``'s names) ``text`` names."""
    return frozenset(name for name, pattern in _lexicon() if pattern.search(text))


def stated_minutes(text: str) -> int | None:
    """The explicit amount of time ``text`` states ("two hours", "20 minutes", "half an hour";
    the first one), in minutes; ``None`` when it states none."""
    match = _amount().search(text)
    if match is None:
        return None
    said = " ".join(match.group("n").casefold().split())
    unit = _UNITS[match.group("unit").casefold()]
    if said.startswith("half"):
        return unit // 2
    count = int(said) if said.isdigit() else _AMOUNTS[said]
    return count * unit


DURATIVE: Final = (
    *INVITATIONS,
    "rest of",
    "the whole",
    "all night",
    "all day",
    "for hours",
    "hours pass",
    "through the night",
    "fall asleep",
    "falls asleep",
    "fell asleep",
    "finish",
    "finishes",
    "finished",
)
"""#632: words with which a reply narrates an activity as elapsing ("they watch the rest of the
movie", "we talk until the diner closes"), beside its activity."""


@cache
def _durative() -> re.Pattern[str]:
    return _words(DURATIVE)


def narrates_elapsing(text: str) -> bool:
    """#632 (the Lite detector): whether ``text`` narrates elapsed time: it states an amount,
    or names an activity together with a word that has it elapse. Deterministic, no model."""
    if stated_minutes(text) is not None:
        return True
    return bool(activities(text)) and bool(_durative().search(text))


def established(texts: Iterable[str]) -> frozenset[str]:
    """The activities the scene established: those named by ``texts`` (its earlier ``do``
    beats, the player's input among them, and its ``fluent`` facts rendered as text)."""
    found: set[str] = set()
    for text in texts:
        found |= activities(text)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Skip:
    """A skip as authority reads it: the text of the beat that narrates it, its reason and its
    minutes (``None``: not known yet, an undeclared skip before its estimate)."""

    text: str
    reason: str = ""
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class PlayerInitiated:
    """A skip the player's input (``by = player``) or an author directive (``by = author``)
    states or invites: uncapped; ``minutes`` is the amount the input states, if any."""

    minutes: int | None = None
    by: Initiator = PLAYER

    @property
    def initiated_by(self) -> Initiator:
        return self.by


@dataclass(frozen=True, slots=True)
class ModelInitiated:
    """A skip a render starts on its own, with the activities its text narrates."""

    activities: frozenset[str] = frozenset()

    @property
    def initiated_by(self) -> Initiator:
        return MODEL


Classified = PlayerInitiated | ModelInitiated


def _invites(text: str) -> tuple[bool, int | None]:
    amount = stated_minutes(text)
    if amount is not None:
        return True, amount
    return bool(_invitation().search(text) or activities(text)), None


def classify_skip(
    skip: Skip, player_input: str, directives: Sequence[str] = ()
) -> PlayerInitiated | ModelInitiated:
    """Who started ``skip`` (see the module docstring)."""
    invited, amount = _invites(player_input)
    if invited:
        return PlayerInitiated(amount, PLAYER)
    for directive in directives:
        invited, amount = _invites(directive)
        if invited:
            return PlayerInitiated(amount, AUTHOR)
    return ModelInitiated(activities(f"{skip.text} {skip.reason}"))


@dataclass(frozen=True, slots=True)
class Accepted:
    """An authorized skip and who started it (``initiated_by`` on its change row)."""

    initiated_by: Initiator


@dataclass(frozen=True, slots=True)
class Refused:
    """A refused model-initiated skip and why (``REFUSALS``)."""

    reason: str
    initiated_by: Initiator = MODEL


def authorize(
    classified: Classified, *, minutes: int, cap: int, established: frozenset[str]
) -> Accepted | Refused:
    """Whether a skip of ``minutes`` may stand (see the module docstring); ``cap`` the clock's
    ``max_unprompted_skip_minutes``, ``established`` the scene's activities."""
    if isinstance(classified, PlayerInitiated):
        return Accepted(classified.by)
    if cap <= 0:
        return Refused(DISABLED)
    if not classified.activities & established:
        return Refused(NO_ACTIVITY)
    if minutes > cap:
        return Refused(OVER_CAP)
    return Accepted(MODEL)


__all__ = [
    "ACTIVITY_LEXICON",
    "AUTHOR",
    "DISABLED",
    "DURATIVE",
    "INVITATIONS",
    "MIN_CONFIDENCE",
    "MODEL",
    "NO_ACTIVITY",
    "OVER_CAP",
    "PLAYER",
    "REFUSALS",
    "Accepted",
    "Classified",
    "Initiator",
    "ModelInitiated",
    "PlayerInitiated",
    "Refused",
    "Skip",
    "activities",
    "authorize",
    "classify_skip",
    "established",
    "narrates_elapsing",
    "stated_minutes",
]
