"""The genesis dispatcher (FR-SCN-11; #122): every scenario field has a handling by its class,
the build-time check is clean, and a seed's structured form names one capability."""

from __future__ import annotations

import pytest

from epistrel.core.catalog.scenario_models import ScenarioDraft, Seed, scenario_schema_classes
from epistrel.core.genesis import materialize as m
from epistrel.core.genesis.settings_map import SETTING_CAPABILITIES, is_used, unmapped_settings
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios


def test_every_scenario_field_is_handled() -> None:
    """FR-SCN-11: the build-time check passes, and every field of the document has a handling."""
    assert m.handling_problems() == []
    assert set(m.handling()) == set(scenario_schema_classes())


def test_an_unhandled_or_misplaced_field_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path override naming no field, and a director field set to defer, are both reported."""
    broken = {**m.PATH_HANDLING, "world.nowhere": m.Handling.render}
    broken["drivers.plot_hooks"] = m.Handling.defer
    monkeypatch.setattr(m, "PATH_HANDLING", broken)
    problems = m.handling_problems()
    assert "world.nowhere: a path override names no field" in problems
    assert "drivers.plot_hooks: a director field is never deferred" in problems


def test_never_deferred_classes_never_defer() -> None:
    for path, found in m.handling().items():
        field_class = scenario_schema_classes()[path][0]
        if field_class in m.NEVER_DEFERRED:
            assert found not in (m.Handling.defer, m.Handling.render_and_defer), path


def test_settings_map_is_complete() -> None:
    assert unmapped_settings() == []
    assert is_used(SETTING_CAPABILITIES["turn_concurrency"])
    assert not is_used(SETTING_CAPABILITIES["interaction_mode"])


@pytest.mark.parametrize(
    ("seed", "capability"),
    [
        ({"audience": ["public"], "disclosure": "open"}, m.STRUCTURED),
        ({"audience": ["public"], "disclosure": "secret"}, m.PARTITIONS),
        ({"audience": ["self"], "disclosure": "open"}, m.PARTITIONS),
        ({"audience": ["public"], "disclosure": "open", "kind": "belief"}, m.PARTITIONS),
        ({"audience": ["public"], "disclosure": "open", "kind": "scheduled"}, m.CLOCK),
    ],
)
def test_seed_capability(seed: dict[str, object], capability: str) -> None:
    assert m.seed_capability(Seed.model_validate({"text": "A fact.", **seed})) == capability


def test_materialize_lists_every_item_once() -> None:
    """The fixture's deferred items, worked out independently, are exactly what the
    dispatcher lists; each carries the item's original JSON and the resolved characters."""
    body = scenarios.complete()
    draft = ScenarioDraft.model_validate(body)
    cast = [
        m.CastRole(role_id="mother", character_id="C-MOTHER"),
        m.CastRole(role_id="brother", character_id="C-BROTHER"),
        m.CastRole(role_id="aunt", character_id="C-AUNT"),
        m.CastRole(
            role_id="player",
            character_id="C-PLAYER",
            is_player=True,
            persona={"name": "Sam", "age": 23},
        ),
    ]
    found = m.materialize("S", 1, body, draft, cast)
    assert sorted((d.field_ref, d.capability_needed) for d in found.deferred) == (
        lg.expected_deferred(body)
    )
    relationship = next(
        d for d in found.deferred if d.field_ref == "cast.roles[0].relationships[0]"
    )
    assert relationship.payload == {
        "value": {"to_role": "player", "name": "mother_of"},
        "characters": {"mother": "C-MOTHER", "player": "C-PLAYER"},
    }
    assert found.settings() == lg.expected_inactive(body, [c.role_id for c in cast])
