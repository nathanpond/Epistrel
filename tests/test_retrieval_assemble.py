"""Context assembly within a token budget (FR-RET-7, S; #141): ``core.retrieval.assemble``.
FR-RET-7's docs/04 entry is U-31 (M5); this is M4's implementation check, with no part marker
(the issue's test plan lists it as S)."""

from __future__ import annotations

import pytest

from epistrel.core.retrieval.assemble import (
    PIN_SHARE,
    ContextItem,
    Tier,
    assemble,
    tokens_of,
)


def _item(tier: Tier, kind: str, item_id: str, tokens: int) -> ContextItem:
    return ContextItem(tier, kind, item_id, "x" * (tokens * 4))  # type: ignore[arg-type]


def test_assemble_budget() -> None:
    """A budget is filled by tier: ledger items and pins first (pins together within 40% of the
    budget), then occasions, then profiles and canon, then ranked memories; an item that does
    not fit is dropped with its reason and a smaller later one may still fit; the placed items
    never exceed the budget; a repeated item is placed once. Complements: a zero budget places
    nothing; a budget large enough places everything in tier order."""
    assert tokens_of("abcdefgh") == 2
    assert tokens_of("") == 1
    assert PIN_SHARE == 0.4
    items = [
        _item(Tier.MEMORIES, "memory", "m1", 40),
        _item(Tier.LEDGER_AND_PINS, "ledger", "l1", 10),
        _item(Tier.LEDGER_AND_PINS, "pin", "p1", 30),
        _item(Tier.LEDGER_AND_PINS, "pin", "p2", 20),  # over the pins' 40 tokens
        _item(Tier.OCCASIONS, "episode", "e1", 20),
        _item(Tier.PROFILES, "profile", "f1", 45),  # over the remaining budget
        _item(Tier.PROFILES, "canon", "c1", 5),
        _item(Tier.MEMORIES, "memory", "m2", 5),
        _item(Tier.MEMORIES, "memory", "m2", 5),  # a repeat
    ]
    found = assemble(items, 100)
    assert [i.item_id for i in found.items] == ["l1", "p1", "e1", "c1", "m2"]
    assert found.used == 70 <= found.budget == 100
    assert sum(i.tokens for i in found.items if i.kind == "pin") <= int(100 * PIN_SHARE)
    reasons = {(d.item_id, d.reason) for d in found.dropped}
    assert reasons == {("p2", "pin_cap"), ("f1", "budget"), ("m1", "budget"), ("m2", "duplicate")}
    tiers = [int(i.tier) for i in found.items]
    assert tiers == sorted(tiers)
    # Complements.
    assert assemble(items, 0).items == ()
    everything = assemble(items[:-1], 10_000, pin_share=1.0)
    assert [i.item_id for i in everything.items] == [
        "l1",
        "p1",
        "p2",
        "e1",
        "f1",
        "c1",
        "m1",
        "m2",
    ]
    with pytest.raises(ValueError, match="negative"):
        assemble(items, -1)
    with pytest.raises(ValueError, match="between 0 and 1"):
        assemble(items, 10, pin_share=1.5)
