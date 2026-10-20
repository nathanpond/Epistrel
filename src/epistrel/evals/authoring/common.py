"""Shared pieces of #157's set authoring: the case envelope and a seeded chooser.

Every case is ``{id, family, origin, inputs, expected, …}``: ``id`` is
``<set>-<family>-<nnnn>``; ``origin`` is ``whitepaper`` (a whitepaper scenario family: Bobby's
education, Sunday Dinner, the knock, the coin), ``authored`` (a template's first, hand-written
instance) or ``paraphrase`` (a template-driven variant of it: other names, places, years or
wording; no model is used anywhere).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any


class Chooser:
    """A seeded random source per set family, so a set is the same on every machine."""

    def __init__(self, key: str) -> None:
        self.rnd = random.Random(key)  # noqa: S311 - reproducible authoring, not a secret

    def pick[T](self, items: Sequence[T]) -> T:
        return items[self.rnd.randrange(len(items))]

    def picks[T](self, items: Sequence[T], k: int) -> list[T]:
        return self.rnd.sample(list(items), k)

    def year(self, low: int = 2001, high: int = 2060) -> int:
        return self.rnd.randint(low, high)

    def between(self, low: int, high: int) -> int:
        return self.rnd.randint(low, high)


def case(
    set_name: str,
    family: str,
    n: int,
    *,
    origin: str,
    inputs: dict[str, Any],
    expected: Any,
    **extra: Any,
) -> dict[str, Any]:
    """One case in the envelope (see the module docstring)."""
    return {
        "id": f"{set_name}-{family}-{n:04d}",
        "family": family,
        "origin": origin,
        "inputs": inputs,
        "expected": expected,
        **extra,
    }


def origin_of(i: int, *, whitepaper: bool = False) -> str:
    """A template's first instance is authored (or the whitepaper's), its variants paraphrase."""
    if i == 0:
        return "whitepaper" if whitepaper else "authored"
    return "paraphrase"


__all__ = ["Chooser", "case", "origin_of"]
