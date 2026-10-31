"""State rules, pure (#192): the two rule shapes, the derived arithmetic, the cycle check and the
topological order, a definition's rule checks, and an elapsed tick's bound handling."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from epistrel.core.errors import InvalidRequest, StateUnpermitted
from epistrel.core.states import expressions as ex
from epistrel.core.states.model import validate_definition
from epistrel.core.states.rules import _bounded


def _reason(error: InvalidRequest) -> Any:
    return error.details["reason"]


def test_expression_arithmetic() -> None:
    node = ex.parse_expression("gold + max(silver, 2) \u00d7 3 \u2212 -1 / 4")
    assert ex.names(node) == {"gold", "silver"}
    assert ex.evaluate(node, {"gold": Decimal(1), "silver": Decimal(5)}) == Decimal("16.25")
    assert ex.evaluate(ex.parse_expression("min(a, b, 7)"), {"a": Decimal(9), "b": Decimal(8)}) == 7
    third = ex.evaluate(ex.parse_expression("1 / 3"), {})
    assert third is not None
    assert len(third.as_tuple().digits) == 15
    # A division by zero or a missing input leaves the value alone.
    assert ex.evaluate(ex.parse_expression("a / 0"), {"a": Decimal(1)}) is None
    assert ex.evaluate(ex.parse_expression("a + b"), {"a": Decimal(1)}) is None


@pytest.mark.parametrize("source", ["", "gold +", "(gold", "min()", "gold $ 2", "gold gold"])
def test_expression_refusals(source: str) -> None:
    with pytest.raises(InvalidRequest) as refused:
        ex.parse_expression(source)
    assert _reason(refused.value) == "expression"


def test_rule_shapes() -> None:
    assert ex.parse_rules({}) is None
    elapsed = ex.parse_rules({"kind": "elapsed", "per_minutes": 30, "delta": -1})
    assert elapsed == ex.ElapsedRule(30, Decimal(-1), False)
    derived = ex.parse_rules({"kind": "derived", "expression": "a + b"})
    assert isinstance(derived, ex.DerivedRule)
    assert derived.inputs == {"a", "b"}
    for bad in (
        {"kind": "script", "code": "x += 1"},
        {"kind": "elapsed", "per_minutes": 0, "delta": 1},
        {"kind": "elapsed", "per_minutes": True, "delta": 1},
        {"kind": "elapsed", "per_minutes": 5, "delta": 0},
        {"kind": "elapsed", "per_minutes": 5, "delta": 1, "every": "day"},
        {"kind": "elapsed", "per_minutes": 5, "delta": 1, "bounds_clamp": "yes"},
        {"kind": "derived", "expression": "a", "extra": 1},
    ):
        with pytest.raises(InvalidRequest) as refused:
            ex.parse_rules(bad)
        assert _reason(refused.value) == "rules_shape", bad


def test_cycles_and_order() -> None:
    assert ex.find_cycle({"a": frozenset({"b"}), "b": frozenset({"x"})}) is None
    assert ex.find_cycle({"a": frozenset({"b"}), "b": frozenset({"a"})}) == ["a", "b", "a"]
    graph = {"c": frozenset({"a", "b"}), "b": frozenset({"a"}), "d": frozenset({"z"})}
    assert ex.downstream(graph, "a") == ["b", "c"]
    assert ex.downstream(graph, "z") == ["d"]
    assert ex.downstream(graph, "q") == []


NUMBER: dict[str, Any] = {"name": "hunger", "type": "number", "initial_value": 10}


def test_definition_rule_checks() -> None:
    elapsed = {"kind": "elapsed", "per_minutes": 60, "delta": -1}
    assert validate_definition({**NUMBER, "rules": elapsed}).rules == elapsed
    cases: list[tuple[dict[str, Any], str]] = [
        ({**NUMBER, "type": "text", "initial_value": "x", "rules": elapsed}, "rules_type"),
        ({**NUMBER, "scope": "character", "rules": elapsed}, "rules_scope"),
        ({**NUMBER, "initial_value": None, "rules": elapsed}, "elapsed_initial"),
        ({**NUMBER, "rules": {"kind": "derived", "expression": "a"}}, "derived_initial"),
        (
            {
                "name": "worth",
                "type": "number",
                "constraints": {"min": 0},
                "rules": {"kind": "derived", "expression": "a"},
            },
            "derived_constraints",
        ),
        (
            {
                "name": "worth",
                "type": "number",
                "rules": {"kind": "derived", "expression": "worth"},
            },
            "rules_cycle",
        ),
    ]
    for definition, reason in cases:
        with pytest.raises(InvalidRequest) as refused:
            validate_definition(definition)
        assert _reason(refused.value) == reason
    with pytest.raises(StateUnpermitted) as unpermitted:
        validate_definition({**NUMBER, "rules": elapsed, "authorization": {"sources": ["turn"]}})
    assert unpermitted.value.details["reason"] == "source"
    # The default sources (none named) admit rules.
    validate_definition({**NUMBER, "rules": elapsed, "authorization": {"sources": []}})


def test_elapsed_bounds() -> None:
    state = validate_definition({**NUMBER, "constraints": {"min": 0, "max": 10}})
    assert _bounded(state, Decimal(10), Decimal(-3), clamp=True) == -3
    assert _bounded(state, Decimal(1), Decimal(-3), clamp=True) == -1
    assert _bounded(state, Decimal(1), Decimal(-3), clamp=False) == 0
    assert _bounded(state, Decimal(0), Decimal(-3), clamp=True) == 0
    assert _bounded(state, Decimal(9), Decimal(4), clamp=True) == 1
