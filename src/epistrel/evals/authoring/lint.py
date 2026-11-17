"""The content lint over the generated E-12 intervention sets (#938; ``--check-sets``).

No case may contain a minor, a real person or explicit sexual content:

- **names**: an introduction's ``name`` is from ``authoring.names.POOL``; the cast is S-COAT's;
- **ages**: only in structured fields (``age``, ``age_range``), and at least 18; prose naming an
  age (``"12-year-old"``, ``"aged 9"``) is refused whatever the number, as are words that name a
  child;
- **explicit content**: a small whole-word, case-insensitive list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from epistrel.evals.authoring.names import POOL

ADULT = 18
EXPLICIT = (
    "sex",
    "sexual",
    "sexy",
    "naked",
    "nude",
    "nudity",
    "erotic",
    "orgasm",
    "genitals",
    "porn",
    "pornographic",
    "aroused",
    "intercourse",
)
CHILD_WORDS = (
    "child",
    "children",
    "kid",
    "kids",
    "boy",
    "girl",
    "toddler",
    "baby",
    "teen",
    "teenager",
    "schoolboy",
    "schoolgirl",
    "minor",
    "underage",
)
_EXPLICIT = re.compile(r"\b(" + "|".join(EXPLICIT) + r")\b", re.IGNORECASE)
_CHILD = re.compile(r"\b(" + "|".join(CHILD_WORDS) + r")\b", re.IGNORECASE)
_PROSE_AGE = re.compile(r"\b\d{1,3}\s*[- ]?\s*years?[- ]old\b|\baged?\s+\d{1,3}\b", re.IGNORECASE)
_RULES = (
    ("explicit word", _EXPLICIT),
    ("a word naming a child", _CHILD),
    ("an age in prose", _PROSE_AGE),
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def problems(row: Mapping[str, Any]) -> list[str]:
    """Why one generated case fails the lint (empty when it passes)."""
    found: list[str] = []
    case_id = str(row.get("id", "?"))
    for intro in row.get("introduces") or []:
        name = str(intro.get("name", ""))
        if name not in POOL:
            found.append(f"{case_id}: introduction {name!r} is not in the fictional name pool")
        age = intro.get("age")
        if age is not None and age < ADULT:
            found.append(f"{case_id}: introduction {name!r} is aged {age}, under {ADULT}")
        span = intro.get("age_range")
        if span and min(span) < ADULT:
            found.append(f"{case_id}: introduction {name!r} has an age range under {ADULT}")
    texts = [
        *_strings({k: v for k, v in row.items() if k not in ("id", "introduces")}),
        *_strings(
            [{k: v for k, v in i.items() if k != "name"} for i in row.get("introduces") or []]
        ),
    ]
    for text in texts:
        for what, pattern in _RULES:
            hit = pattern.search(text)
            if hit is not None:
                found.append(f"{case_id}: {what} {hit.group(0)!r}")
    return found


__all__ = ["ADULT", "CHILD_WORDS", "EXPLICIT", "problems"]
