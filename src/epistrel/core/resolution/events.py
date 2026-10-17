"""The event-type vocabulary: occasion words to event types (FR-RET-2; #139).

``events.json`` holds the types (about forty, each with its inflections and synonyms), English
only in M4 and a data table. ``resolve_event_type(text)`` answers the text's one primary type:
the earliest word in it that names a type (whole words, case-folded; the longer word where two
start together), or ``None``. A scenario adds types through its optional ``world.event_types``
(each name its own word): pass them as ``extra``. #141's occasion lookup ("those guys at the
game") and #150's episodic writer (one type per beat) call it; ``event_type_or_uncertain`` is the
same with ``uncertain(unresolved_event_type)`` for none.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from functools import cache
from importlib import resources

from epistrel.core.resolution.types import APOSTROPHE, Reason, ReasonCode, Uncertain


@cache
def vocabulary() -> Mapping[str, str]:
    """Every word of ``events.json`` → its event type."""
    raw = json.loads(
        resources.files("epistrel.core.resolution").joinpath("events.json").read_text("utf-8")
    )
    words: dict[str, str] = {}
    for entry in raw["types"]:
        for word in entry["words"]:
            words.setdefault(" ".join(word.casefold().split()), entry["type"])
    return words


def event_types() -> tuple[str, ...]:
    """The vocabulary's types, sorted."""
    return tuple(sorted(set(vocabulary().values())))


def _pattern(words: Iterable[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    edge = rf"[\w'{APOSTROPHE}-]"
    return re.compile(rf"(?<!{edge})(?:{alternatives})(?!{edge})", re.IGNORECASE)


@cache
def _compiled() -> re.Pattern[str]:
    return _pattern(vocabulary())


def resolve_event_type(text: str, extra: Iterable[str] = ()) -> str | None:
    """The primary event type of ``text`` (see the module docstring), ``extra`` being the
    scenario's own types."""
    words = dict(vocabulary())
    added = [" ".join(e.casefold().split()) for e in extra if e.strip()]
    for word in added:
        words.setdefault(word, word)
    pattern = _pattern(words) if added else _compiled()
    match = pattern.search(text)
    if match is None:
        return None
    return words[" ".join(match.group().casefold().split())]


def event_type_or_uncertain(text: str, extra: Iterable[str] = ()) -> str | Uncertain:
    """``resolve_event_type``, with ``uncertain(unresolved_event_type)`` for none."""
    found = resolve_event_type(text, extra)
    if found is None:
        return Uncertain(Reason(ReasonCode.UNRESOLVED_EVENT_TYPE, "no_occasion_word"))
    return found


__all__ = ["event_type_or_uncertain", "event_types", "resolve_event_type", "vocabulary"]
