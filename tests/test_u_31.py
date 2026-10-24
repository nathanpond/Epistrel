"""U-31: context assembly by priority tier, over an already viewpoint-filtered set only (docs/04
§3; FR-RET-7, NFR-PRIV-4; #141, #170).

``core.retrieval.assemble.assemble`` with the stub tokenizer (``len // 4``): tiers in priority
order and items within a tier in rank order, never over the budget, items dropped whole, a
lower-tier item placed only when every higher one is placed or cannot fit in what remains, and
the result lists what was dropped. It accepts only a ``ViewpointFiltered`` set
(``core.retrieval.filtered``). A plain iterable is refused at runtime as mypy refuses it
statically, a ``ViewpointFiltered`` cannot be made without the module-private seal, and the
seal is called only by the modules that apply a viewpoint filter (``tools.gates
check-character-context``). So the helper cannot widen what a character sees."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tools.gates.character_context import SEALERS, scan

from epistrel.core.retrieval.assemble import ContextItem, Tier, assemble
from epistrel.core.retrieval.filtered import ViewpointFiltered, _seal

VIEWPOINT = "character:01JCHARACTER0000000000000A"


def _item(tier: Tier, kind: str, item_id: str, tokens: int) -> ContextItem:
    return ContextItem(tier, kind, item_id, "x" * (tokens * 4))  # type: ignore[arg-type]


@pytest.mark.part("M5", "FR-RET-7; NFR-PRIV-4")
def test_u_31__filtered_input(tmp_path: Path) -> None:
    """See the module docstring. Complements: the sealed set assembles; the same items unsealed
    do not."""
    items = [
        _item(Tier.MEMORIES, "memory", "m1", 30),  # rank 1 of the memories
        _item(Tier.MEMORIES, "memory", "m2", 10),  # rank 2
        _item(Tier.PROFILES, "profile", "f1", 20),
        _item(Tier.OCCASIONS, "episode", "e1", 25),
        _item(Tier.LEDGER_AND_PINS, "pin", "p1", 10),
        _item(Tier.LEDGER_AND_PINS, "ledger", "l1", 5),
    ]
    found = assemble(_seal(items, VIEWPOINT), 75)
    # Tiers in priority order; within a tier, rank order as given.
    assert [i.item_id for i in found.items] == ["p1", "l1", "e1", "f1", "m2"]
    tiers = [int(i.tier) for i in found.items]
    assert tiers == sorted(tiers)
    # Never over the budget; dropped whole (an item is placed at its full size or not at all).
    assert found.used == sum(i.tokens for i in found.items) == 70 <= 75
    assert all(i.text in {x.text for x in items} for i in found.items)
    # The lower-tier m2 is placed only because the higher m1 cannot fit in what remained.
    assert [(d.item_id, d.reason, d.tokens) for d in found.dropped] == [("m1", "budget", 30)]
    everything = assemble(_seal(items, VIEWPOINT), 1_000, pin_share=1.0)
    assert {i.item_id for i in everything.items} == {i.item_id for i in items}
    assert everything.dropped == ()

    # Only an already viewpoint-filtered set: a plain list is refused...
    with pytest.raises(TypeError, match="viewpoint-filtered"):
        assemble(items, 75)  # type: ignore[arg-type]
    unsealed: Any = tuple(items)
    with pytest.raises(TypeError, match="viewpoint-filtered"):
        assemble(unsealed, 75)
    # ...and no one can make one without the seal.
    with pytest.raises(TypeError, match="only by the module that filtered it"):
        ViewpointFiltered(tuple(items), VIEWPOINT)
    sealed = _seal(items, VIEWPOINT)
    assert sealed.viewpoint == VIEWPOINT
    assert len(sealed) == len(items)

    # The seal is called only by the filtering modules (retrieval's pipeline, after #159's
    # filter; a character's context assembly), in the repository and in a planted tree.
    assert scan() == []
    assert {
        "src/epistrel/core/retrieval/filtered.py",
        "src/epistrel/core/retrieval/pipeline.py",
        "src/epistrel/core/epistemic/context.py",
    } == SEALERS
    planted = tmp_path / "src/epistrel/orchestrator/widen.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "def widen(items, seal):\n    return _seal(items, 'character:x')\n", encoding="utf-8"
    )
    (found_line,) = scan(tmp_path)
    assert found_line.startswith("src/epistrel/orchestrator/widen.py:2: seals a viewpoint")
