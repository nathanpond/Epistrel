"""#996 (FR-CLOCK-12): a render's or a regeneration's change to ``world_clock`` is corrected
once, then dropped, as pure units over the schemas and ``structured.parse``. The end-to-end
turns are ``tests/integration/test_world_clock_effects.py``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from epistrel.core.enums import StateChangeKind, StateChangeSource
from epistrel.core.states.model import CLOCK_SOURCES, clock_change_permitted
from epistrel.orchestrator.models.structured import _Failure, parse
from epistrel.orchestrator.repair.ladder import RegeneratedBeat
from epistrel.orchestrator.structured.render import (
    CLOCK_CORRECTION,
    CLOCK_EFFECT,
    RenderedBeats,
    TrackedState,
    render_schema,
)

SEEN = frozenset({CLOCK_EFFECT})
GOLD = {"kind": "change", "state": "gold", "delta": -5, "reason": "a nail"}
CLOCK_SET = {"kind": "change", "state": "world_clock", "value": 600, "reason": "evening"}
CLOCK_DELTA = {"kind": "change", "state": "world_clock", "delta": 30, "reason": "evening"}
CLOCK_PARTY = {
    "kind": "transfer",
    "state": "gold",
    "delta": -5,
    "counterparty": {"state": "world_clock"},
    "reason": "time is money",
}
ADVANCE = {"kind": "time_advance", "state": "world_clock", "minutes": 20, "reason": "the walk"}


def _schema() -> type[RenderedBeats]:
    return render_schema(
        actor_id=None,
        player_id=None,
        cast_ids=[],
        max_beats=4,
        states={
            "gold": TrackedState("number", "story", "player_visible"),
            "world_clock": TrackedState("clock", "story", "player_visible"),
        },
        states_level="on",
        clock_level="on",
    )


def _render(*effects: dict[str, Any]) -> str:
    return json.dumps({"beats": [{"kind": "narration", "text": "Dusk.", "effects": list(effects)}]})


def _regenerated(*effects: dict[str, Any]) -> str:
    return json.dumps({"text": "Dusk.", "effects": list(effects)})


def test_clock_rule_is_shared() -> None:
    """``clock_change_permitted`` is the rule ``core.states`` refuses a client's change by: a
    delta from a clock source only; a turn is never one."""
    assert StateChangeSource.TURN not in CLOCK_SOURCES
    for source in StateChangeSource:
        assert clock_change_permitted(StateChangeKind.SET, source) is False
        assert clock_change_permitted(StateChangeKind.DELTA, source) is (source in CLOCK_SOURCES)


@pytest.mark.parametrize("clock", [CLOCK_SET, CLOCK_DELTA, CLOCK_PARTY])
def test_render_corrects_once_then_drops(clock: dict[str, Any]) -> None:
    """The first validation refuses with the dedicated correction (``clock_effect``); once it
    was sent, the clock change is dropped and the beat keeps its other effects."""
    schema = _schema()
    first = parse(_render(GOLD, clock), schema)
    assert isinstance(first, _Failure)
    assert CLOCK_EFFECT in first.codes
    assert CLOCK_CORRECTION in first.problem
    assert "world_clock" not in first.public.replace("world clock", "")
    again = parse(_render(GOLD, clock), schema, SEEN)
    assert isinstance(again, RenderedBeats)
    assert [e.state for e in again.beats[0].effects] == ["gold"]
    assert again.clock_effects_dropped == 1


def test_render_mixed_errors_listed_together() -> None:
    """A clock change and another effect error of the beat go back in one correction."""
    failed = parse(_render({**GOLD, "state": "purse"}, CLOCK_DELTA), _schema())
    assert isinstance(failed, _Failure)
    assert CLOCK_EFFECT in failed.codes
    assert "not one of the story's States" in failed.problem
    assert CLOCK_CORRECTION in failed.problem
    # Once corrected, the clock change goes and the other error still stands.
    still = parse(_render({**GOLD, "state": "purse"}, CLOCK_DELTA), _schema(), SEEN)
    assert isinstance(still, _Failure)
    assert CLOCK_EFFECT not in still.codes
    assert CLOCK_CORRECTION not in still.problem


def test_render_time_advance_untouched() -> None:
    """Complement: a time advance is how a render moves the clock; it is neither refused nor
    dropped, before or after a clock correction."""
    for seen in (frozenset(), SEEN):
        found = parse(_render(GOLD, ADVANCE), _schema(), seen)
        assert isinstance(found, RenderedBeats)
        assert [e.kind for e in found.beats[0].effects] == ["change", "time_advance"]
        assert found.clock_effects_dropped == 0


@pytest.mark.parametrize("clock", [CLOCK_SET, CLOCK_DELTA, CLOCK_PARTY])
def test_regenerated_beat_corrects_once_then_drops(clock: dict[str, Any]) -> None:
    """#996's path: ``RegeneratedBeat`` checked no effect, so a clock change reached the commit
    (422 ``reserved_state``). It is now refused once, then dropped."""
    first = parse(_regenerated(GOLD, clock), RegeneratedBeat)
    assert isinstance(first, _Failure)
    assert first.codes == {CLOCK_EFFECT}
    assert first.problem == f"the object: effects.1.state: {CLOCK_CORRECTION}"
    again = parse(_regenerated(GOLD, clock), RegeneratedBeat, SEEN)
    assert isinstance(again, RegeneratedBeat)
    assert [e.state for e in again.effects] == ["gold"]
    assert again.clock_effects_dropped == 1


def test_regenerated_beat_without_clock_unchanged() -> None:
    """Complement: an answer with no clock change validates as before, nothing dropped."""
    found = parse(_regenerated(GOLD), RegeneratedBeat)
    assert isinstance(found, RegeneratedBeat)
    assert found.clock_effects_dropped == 0
    assert RegeneratedBeat.model_validate({"text": "Dusk."}).clock_effects_dropped == 0
