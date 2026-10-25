"""Context assembly: fill a token budget by priority tier (FR-RET-7, FR-MEM-6; #141). Pure.

A retrieval's context is a list of items, each with a ``tier``, an ``item_id``, a ``kind`` and
its ``text``. The tiers, in priority order:

1. ``LEDGER_AND_PINS``: the ``context_sources`` hook's items (#146's open ledger items, then
   pinned memories, the caller ordering pins by decayed importance). Pins may take at most
   ``PIN_SHARE`` (40%) of the budget together;
2. ``OCCASIONS``: the located episodes, then the participants' assessments;
3. ``PROFILES``: the resolved characters' standing profiles, then the canon about the resolved
   entities;
4. ``MEMORIES``: the ranked memories, best first.

``assemble(items, budget)`` takes only an already viewpoint-filtered set
(``core.retrieval.filtered.ViewpointFiltered``, U-31 M5, NFR-PRIV-4; #170): anything else is a
``TypeError``, so the helper cannot widen what a character sees. It walks them in that order (a
stable sort by tier: within a tier the order given) and places each item that still fits: an
item over the remaining budget is dropped (``budget``), and a pin over the pins' share is
dropped (``pin_cap``), and the walk goes on, so a smaller later item may still fit. The placed
items never exceed the budget, whatever the story's age (FR-MEM-6's bounded context). A repeated
``(kind, item_id)`` is placed once (the first, highest-priority occurrence; the repeat is
dropped as ``duplicate``).

Tokens are M3's estimate (``core.summary.estimate_tokens``: ``len(text) // 4``, no tokenizer),
at least 1 per item.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

from epistrel.core.retrieval.filtered import ViewpointFiltered
from epistrel.core.summary import estimate_tokens

PIN_SHARE = 0.4
"""The share of the budget pinned memories may take together."""

DEFAULT_TOKEN_BUDGET = 3_000
"""``memory.episodic.retrieval_token_budget``'s default."""


class Tier(IntEnum):
    LEDGER_AND_PINS = 1
    OCCASIONS = 2
    PROFILES = 3
    MEMORIES = 4


ItemKind = Literal["ledger", "pin", "episode", "assessment", "profile", "canon", "memory"]
DropReason = Literal["budget", "pin_cap", "duplicate"]


def tokens_of(text: str) -> int:
    """An item's tokens: ``len(text) // 4``, at least 1."""
    return max(1, estimate_tokens(text))


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One candidate for the context."""

    tier: Tier
    kind: ItemKind
    item_id: str
    text: str

    @property
    def tokens(self) -> int:
        return tokens_of(self.text)


@dataclass(frozen=True, slots=True)
class Dropped:
    """An item the budget left out, and why."""

    tier: Tier
    kind: ItemKind
    item_id: str
    tokens: int
    reason: DropReason


@dataclass(frozen=True, slots=True)
class Context:
    """The assembled context: the placed items in order, what was dropped, the budget and the
    tokens used."""

    items: tuple[ContextItem, ...] = ()
    dropped: tuple[Dropped, ...] = ()
    budget: int = DEFAULT_TOKEN_BUDGET
    used: int = 0


def assemble(
    items: ViewpointFiltered[ContextItem], budget: int, *, pin_share: float = PIN_SHARE
) -> Context:
    """Fill ``budget`` tokens by tier with ``items``, an already viewpoint-filtered set (see the
    module docstring)."""
    if not isinstance(items, ViewpointFiltered):
        raise TypeError("the context assembler takes only an already viewpoint-filtered set")
    if budget < 0:
        raise ValueError("a token budget is not negative")
    if not 0.0 <= pin_share <= 1.0:
        raise ValueError("the pins' share is between 0 and 1")
    pin_cap = int(budget * pin_share)
    placed: list[ContextItem] = []
    dropped: list[Dropped] = []
    seen: set[tuple[str, str]] = set()
    used = pins = 0
    for item in sorted(items, key=lambda i: int(i.tier)):
        tokens = item.tokens
        key = (item.kind, item.item_id)
        reason: DropReason | None = None
        if key in seen:
            reason = "duplicate"
        elif item.kind == "pin" and pins + tokens > pin_cap:
            reason = "pin_cap"
        elif used + tokens > budget:
            reason = "budget"
        if reason is not None:
            dropped.append(Dropped(item.tier, item.kind, item.item_id, tokens, reason))
            continue
        seen.add(key)
        placed.append(item)
        used += tokens
        if item.kind == "pin":
            pins += tokens
    return Context(tuple(placed), tuple(dropped), budget, used)


__all__ = [
    "DEFAULT_TOKEN_BUDGET",
    "PIN_SHARE",
    "Context",
    "ContextItem",
    "DropReason",
    "Dropped",
    "ItemKind",
    "Tier",
    "assemble",
    "tokens_of",
]
