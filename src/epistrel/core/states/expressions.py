"""The two shapes a State's ``rules`` may take, and a derived State's arithmetic (FR-STATE-7;
#192). Pure: nothing here reads the database (``core.states.rules`` applies them).

"Updated by rules" is limited to constraints plus two deterministic triggers; there is no
scripting language:

- ``{"kind": "elapsed", "per_minutes": n, "delta": d, "bounds_clamp": bool}``: the State
  changes by ``d`` for each whole ``n`` minutes the world clock advances (``bounds_clamp``,
  default ``false``: a step that would break a bound moves only up to it; otherwise it is held
  and changes nothing);
- ``{"kind": "derived", "expression": "<arithmetic>"}``: the State is recomputed from other
  States whenever one of them changes. The arithmetic is numbers, State names, parentheses,
  ``+``, ``-``, ``*``, ``/`` (or the minus, multiplication and division signs), and
  ``min(…)``/``max(…)`` of one or more arguments; a unary minus is allowed. Results keep 15
  significant digits (a State's number precision); a division by zero, or an input with no
  value, leaves the State as it is.

Any other shape is 422 ``validation_error`` (``field: rules``, ``reason: rules_shape``; a
malformed expression ``reason: expression``).
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from epistrel.core.errors import InvalidRequest

ELAPSED: Final = "elapsed"
DERIVED: Final = "derived"
EXPRESSION_MAX = 400
"""The longest expression, in characters."""
DIGITS = 15
"""A State number's significant digits (``core.states.model.DIGITS_MAX``)."""

_CONTEXT = decimal.Context(prec=DIGITS, rounding=decimal.ROUND_HALF_EVEN)
_TOKEN = re.compile(
    r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<name>[A-Za-z][A-Za-z0-9_]*)|(?P<op>[-+*/\u00d7\u00f7\u2212(),]))"
)
_SYMBOLS = {"\u00d7": "*", "\u00f7": "/", "\u2212": "-"}
FUNCTIONS: frozenset[str] = frozenset({"min", "max"})


def _invalid(message: str, reason: str) -> InvalidRequest:
    return InvalidRequest(message, {"field": "rules", "reason": reason})


# -- the expression ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Number:
    value: Decimal


@dataclass(frozen=True, slots=True)
class Ref:
    name: str


@dataclass(frozen=True, slots=True)
class Negate:
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Call:
    function: str
    arguments: tuple[Node, ...]


Node = Number | Ref | Negate | Binary | Call


def _tokens(source: str) -> list[str]:
    found: list[str] = []
    at = 0
    text = source.rstrip()
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None or match.end() == at:
            raise _invalid(f"the expression has an unexpected character at {at}", "expression")
        token = match.group("number") or match.group("name") or match.group("op")
        assert token is not None  # noqa: S101 - the pattern matched one group
        found.append(_SYMBOLS.get(token, token))
        at = match.end()
    return found


class _Parser:
    """A recursive-descent parser: ``sum := term (('+'|'-') term)*``, ``term := unary
    (('*'|'/') unary)*``, ``unary := '-' unary | atom``, ``atom := number | name |
    ('min'|'max') '(' sum (',' sum)* ')' | '(' sum ')'``."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.at = 0

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self, wanted: str | None = None) -> str:
        token = self.peek()
        if token is None or (wanted is not None and token != wanted):
            raise _invalid(
                f"the expression expected {wanted or 'more'} at token {self.at}", "expression"
            )
        self.at += 1
        return token

    def parse(self) -> Node:
        node = self.sum()
        if self.peek() is not None:
            raise _invalid(f"the expression has extra input at token {self.at}", "expression")
        return node

    def sum(self) -> Node:
        node = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            node = Binary(op, node, self.term())
        return node

    def term(self) -> Node:
        node = self.unary()
        while self.peek() in ("*", "/"):
            op = self.take()
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.peek() == "-":
            self.take()
            return Negate(self.unary())
        return self.atom()

    def atom(self) -> Node:
        piece = self.take()
        if piece == "(":
            node = self.sum()
            self.take(")")
            return node
        if piece[0].isdigit():
            return Number(Decimal(piece))
        if piece[0].isalpha():
            if piece in FUNCTIONS and self.peek() == "(":
                self.take("(")
                arguments = [self.sum()]
                while self.peek() == ",":
                    self.take(",")
                    arguments.append(self.sum())
                self.take(")")
                return Call(piece, tuple(arguments))
            return Ref(piece)
        raise _invalid(f"the expression has an unexpected {piece!r}", "expression")


def parse_expression(source: str) -> Node:
    """``source`` parsed (422 ``validation_error``, ``reason: expression``, when it is not the
    arithmetic the module docstring describes)."""
    if not source.strip() or len(source) > EXPRESSION_MAX:
        raise _invalid(
            f"a derived State's expression is 1 to {EXPRESSION_MAX} characters", "expression"
        )
    return _Parser(_tokens(source)).parse()


def names(node: Node) -> frozenset[str]:
    """The State names ``node`` reads."""
    if isinstance(node, Ref):
        return frozenset({node.name})
    if isinstance(node, Negate):
        return names(node.operand)
    if isinstance(node, Binary):
        return names(node.left) | names(node.right)
    if isinstance(node, Call):
        return frozenset().union(*(names(a) for a in node.arguments))
    return frozenset()


def evaluate(node: Node, values: Mapping[str, Decimal]) -> Decimal | None:
    """``node``'s value over ``values`` (by State name), to 15 significant digits; ``None`` when
    an input has no value or a division is by zero."""
    try:
        found = _evaluate(node, values)
    except KeyError, decimal.DivisionByZero, decimal.InvalidOperation:
        return None
    return None if found is None else _CONTEXT.plus(found)


def _evaluate(node: Node, values: Mapping[str, Decimal]) -> Decimal | None:
    if isinstance(node, Number):
        return node.value
    if isinstance(node, Ref):
        return values[node.name]
    if isinstance(node, Negate):
        inner = _evaluate(node.operand, values)
        return None if inner is None else -inner
    if isinstance(node, Binary):
        left, right = _evaluate(node.left, values), _evaluate(node.right, values)
        if left is None or right is None:
            return None
        if node.op == "+":
            return _CONTEXT.add(left, right)
        if node.op == "-":
            return _CONTEXT.subtract(left, right)
        if node.op == "*":
            return _CONTEXT.multiply(left, right)
        if right == 0:
            return None
        return _CONTEXT.divide(left, right)
    found = [_evaluate(a, values) for a in node.arguments]
    if any(f is None for f in found):
        return None
    numbers = [f for f in found if f is not None]
    return min(numbers) if node.function == "min" else max(numbers)


# -- the rules ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ElapsedRule:
    """``{kind: elapsed, per_minutes, delta, bounds_clamp}`` (see the module docstring)."""

    per_minutes: int
    delta: Decimal
    bounds_clamp: bool = False


@dataclass(frozen=True, slots=True)
class DerivedRule:
    """``{kind: derived, expression}`` (see the module docstring); ``inputs`` are the State
    names the expression reads."""

    expression: str
    node: Node
    inputs: frozenset[str]


Rule = ElapsedRule | DerivedRule


def _number(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float | Decimal):
        return None
    found = Decimal(str(raw)) if isinstance(raw, float) else Decimal(raw)
    return found if found.is_finite() else None


def parse_rules(rules: Mapping[str, Any]) -> Rule | None:
    """A State's ``rules`` document as its rule (``None``: no rule), or 422 ``validation_error``
    (``reason: rules_shape`` or ``expression``) for any shape the module docstring does not
    list."""
    if not rules:
        return None
    kind = rules.get("kind")
    if kind == ELAPSED:
        if not set(rules) <= {"kind", "per_minutes", "delta", "bounds_clamp"}:
            raise _invalid("an elapsed rule has per_minutes, delta and bounds_clamp", "rules_shape")
        per, delta = rules.get("per_minutes"), _number(rules.get("delta"))
        clamp = rules.get("bounds_clamp", False)
        if isinstance(per, bool) or not isinstance(per, int) or per < 1:
            raise _invalid("an elapsed rule's per_minutes is a whole number from 1", "rules_shape")
        if delta is None or delta == 0 or len(delta.normalize().as_tuple().digits) > DIGITS:
            raise _invalid("an elapsed rule's delta is a non-zero number", "rules_shape")
        if not isinstance(clamp, bool):
            raise _invalid("an elapsed rule's bounds_clamp is true or false", "rules_shape")
        return ElapsedRule(per, delta, clamp)
    if kind == DERIVED:
        if set(rules) != {"kind", "expression"} or not isinstance(rules["expression"], str):
            raise _invalid("a derived rule has an expression, and nothing else", "rules_shape")
        source = rules["expression"]
        node = parse_expression(source)
        return DerivedRule(source, node, names(node))
    raise _invalid(
        "a State's rules are {kind: elapsed, per_minutes, delta, bounds_clamp} or "
        "{kind: derived, expression}",
        "rules_shape",
    )


def find_cycle(graph: Mapping[str, frozenset[str]]) -> list[str] | None:
    """A cycle among derived States (``graph``: each derived State's inputs), as the names
    along it, or ``None``."""
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(name: str) -> list[str] | None:
        state[name] = 1
        path.append(name)
        for child in sorted(graph.get(name, frozenset())):
            if state.get(child) == 1:
                return [*path[path.index(child) :], child]
            if child not in state and child in graph:
                found = visit(child)
                if found is not None:
                    return found
        path.pop()
        state[name] = 2
        return None

    for name in sorted(graph):
        if name not in state:
            found = visit(name)
            if found is not None:
                return found
    return None


def downstream(graph: Mapping[str, frozenset[str]], changed: str) -> list[str]:
    """The derived States that read ``changed``, directly or through others, in topological
    order (each after every derived State it reads); ``graph`` has no cycle."""
    readers: dict[str, set[str]] = {}
    for name, inputs in graph.items():
        for found in inputs:
            readers.setdefault(found, set()).add(name)
    reached: set[str] = set()
    frontier = [changed]
    while frontier:
        for reader in readers.get(frontier.pop(), set()):
            if reader not in reached:
                reached.add(reader)
                frontier.append(reader)
    ordered: list[str] = []
    done: set[str] = set()

    def place(name: str) -> None:
        if name in done:
            return
        done.add(name)
        for found in sorted(graph.get(name, frozenset())):
            if found in reached:
                place(found)
        ordered.append(name)

    for name in sorted(reached):
        place(name)
    return ordered


__all__ = [
    "DERIVED",
    "ELAPSED",
    "FUNCTIONS",
    "Binary",
    "Call",
    "DerivedRule",
    "ElapsedRule",
    "Negate",
    "Node",
    "Number",
    "Ref",
    "Rule",
    "downstream",
    "evaluate",
    "find_cycle",
    "names",
    "parse_expression",
    "parse_rules",
]
