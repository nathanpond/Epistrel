"""#630: a render's transfer ``consideration`` must name an entity of the story; an unknown item
is a structural error (a cheap retry), not a refusal at the State dry-run."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from epistrel.orchestrator.structured.render import TrackedState, render_schema

STATES = {
    "gold": TrackedState(type="number", scope="character", visibility="player_visible"),
    "sword": TrackedState(type="number", scope="character", visibility="player_visible"),
}


def _render(consideration: str, items: Any) -> Any:
    schema = render_schema(
        actor_id="mira",
        player_id="cal",
        cast_ids=["mira", "cal"],
        max_beats=6,
        states=STATES,
        states_level="on",
        items=items,
    )
    effect = {
        "kind": "transfer",
        "state": "gold",
        "scope_key": "cal",
        "delta": -10,
        "counterparty": {"state": "gold", "scope_key": "mira", "delta": 10},
        "consideration": consideration,
        "reason": "a sale",
    }
    beat = {"kind": "do", "text": "She sells it.", "effects": [effect]}
    return schema.model_validate({"beats": [beat]})


def test_an_unknown_consideration_is_a_structural_error() -> None:
    """Complement asserted: an item the story knows validates, and no resolver checks
    nothing."""

    def known(name: str) -> bool:
        return name == "the sword"

    with pytest.raises(ValidationError) as refused:
        _render("the moonstone", known)
    assert "beats.0.effects.0.consideration: names no item of the story" in str(refused.value)
    assert _render("the sword", known).beats[0].effects[0].consideration == "the sword"
    assert _render("the moonstone", None).beats
