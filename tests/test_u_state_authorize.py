"""``core.states.authorize`` (#188; FR-STATE-10,12): who may change a State, which events justify
each direction, the caps, transfers that balance, and the declaration's shape. Unmarked: the
scenario parts (S-STATE-AUTHORIZATION, S-SIMULTANEOUS) are the gated oracles; this pins the
matrix they rest on (sources x directions x justification labels x caps)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from epistrel.core.errors import InvalidRequest, StateUnpermitted
from epistrel.core.states import StateDefinition
from epistrel.core.states import authorize as a
from epistrel.core.states import effects as rules
from epistrel.core.states.model import UndefinedType, Value, validate_definition
from epistrel.core.turns.beats import DeclaredEffect

Values = dict[tuple[str, str | None], Value | UndefinedType]

CAL, MIRA = "c-cal", "c-mira"
LABELS = ["sale", "reward", "theft", "loot"]


def _gold(**kw: Any) -> StateDefinition:
    found: dict[str, Any] = {
        "name": "gold",
        "type": "number",
        "scope": "character",
        "constraints": {"non_negative": True},
        "initial_value": 0,
        "authorization": {
            "sources": ["any_character", "director", "consumer"],
            "justifications": {"increase": LABELS},
            "caps": {"per_effect": 500, "per_turn": 500},
        },
        "transfer": {"conserved": True, "counterparties": ["till"]},
    }
    found.update(kw)
    return validate_definition(found)


GOLD = _gold()
CHARACTER = a.Source("character", CAL)
DIRECTOR = a.Source("director")
POSSESSION = a.Evidence(2, "do", frozenset({"possession"}))
SAYING = a.Evidence(3, "say", frozenset())
NARRATION = a.Evidence(4, "narration", frozenset(), narration=True)
EVIDENCE = {e.beat: e for e in (POSSESSION, SAYING, NARRATION)}


def _decide(
    effect: a.StateEffect,
    source: a.Source = CHARACTER,
    so_far: list[a.Authorized] | None = None,
    definition: StateDefinition = GOLD,
) -> a.Decision:
    return a.authorize(
        effect,
        source,
        so_far or [],
        definition=definition,
        definitions={"gold": definition},
        current=Decimal(1000),
        evidence=EVIDENCE,
    )


def _change(delta: int, **kw: Any) -> a.StateEffect:
    return a.StateEffect("gold", CAL, delta=Decimal(delta), **kw)


@pytest.mark.parametrize(
    ("source", "allowed"),
    [
        (a.Source("character", CAL), ["any_character"]),
        (a.Source("character", CAL), [f"character:{CAL}"]),
        (a.Source("director"), ["director"]),
    ],
)
def test_u_state_authorize(source: a.Source, allowed: list[str]) -> None:
    """A listed source passes; the same effect from a source the list omits is ``source``."""
    effect = _change(-10)
    listed = _gold(authorization={"sources": allowed})
    assert isinstance(_decide(effect, source, definition=listed), a.Permitted)
    other = ["consumer"] if allowed != ["consumer"] else ["author"]
    closed = _gold(authorization={"sources": other})
    assert _decide(effect, source, definition=closed) == a.Unpermitted(a.SOURCE)
    assert _decide(
        effect,
        a.Source("character", MIRA),
        definition=_gold(authorization={"sources": [f"character:{CAL}"]}),
    ) == a.Unpermitted(a.SOURCE)


def test_reactive_and_co_author_never_authorize() -> None:
    assert _decide(_change(-1), a.Source("reactive", MIRA)) == a.Unpermitted(a.REACTIVE_ASSERTER)
    assert _decide(_change(-1), a.Source("co_author", CAL)) == a.Unpermitted(a.SOURCE)


def test_default_sources() -> None:
    """No authorization: every kind of source, unrestricted justifications, no caps."""
    plain = validate_definition({"name": "gold", "type": "number", "initial_value": 0})
    assert a.authorization_of(plain) == a.Authorization(sources=a.DEFAULT_SOURCES)
    for source in ("consumer", "author", "rule", "director", f"character:{CAL}"):
        assert a.source_permitted(plain, source)
    big = a.StateEffect("gold", None, delta=Decimal(100_000))
    assert isinstance(
        a.authorize(big, DIRECTOR, [], definition=plain, current=Decimal(0), evidence={}),
        a.Permitted,
    )


@pytest.mark.parametrize(
    ("label", "cited", "permitted"),
    [
        ("sale", POSSESSION.beat, True),
        ("theft", POSSESSION.beat, True),
        ("sale", SAYING.beat, False),  # no transfer or possession category
        ("loot", POSSESSION.beat, True),
        ("reward", NARRATION.beat, True),  # a narration beat
        ("reward", SAYING.beat, False),
        ("windfall", POSSESSION.beat, False),  # not in the increase list
        ("sale", None, False),  # no justifying beat
        ("sale", 9, False),  # a beat verification did not keep
        (None, POSSESSION.beat, False),  # no label
    ],
)
def test_justification(label: str | None, cited: int | None, permitted: bool) -> None:
    """An increase of a restricted direction needs a listed label and a kept beat that fits it
    (the transfer side keeps the world-gain rule out of the way)."""
    effect = a.StateEffect(
        "gold",
        CAL,
        kind=a.TRANSFER,
        delta=Decimal(10),
        counterparty=a.Counterparty("gold", MIRA),
        justification=label,
        justified_by=cited,
    )
    decided = _decide(effect)
    assert isinstance(decided, a.Permitted) is permitted
    if not permitted:
        assert decided == a.Unpermitted(a.JUSTIFICATION)


def test_unrestricted_direction_needs_no_label() -> None:
    """A decrease (no list) passes without a justification; an other label needs only a kept
    beat, and a cited beat must still exist."""
    assert isinstance(_decide(_change(-10)), a.Permitted)
    assert isinstance(_decide(_change(-10, justification="fee", justified_by=3)), a.Permitted)
    assert _decide(_change(-10, justified_by=9)) == a.Unpermitted(a.JUSTIFICATION)
    # The consumer and the author need no justification at all.
    assert isinstance(_decide(_change(400), a.Source("consumer")), a.Permitted)


def test_world_gain_needs_director() -> None:
    """A character's increase of a conserved State with no counterparty is a world gain; the
    director's is permitted (within the caps); a non-conserved State has no such rule."""
    loot = _change(100_000, justification="loot", justified_by=POSSESSION.beat)
    assert _decide(loot) == a.Unpermitted(a.WORLD_GAIN)
    found = _decide(
        a.StateEffect("gold", CAL, delta=Decimal(300), justification="loot", justified_by=4),
        DIRECTOR,
    )
    assert isinstance(found, a.Permitted)
    loose = _gold(transfer={"conserved": False})
    assert isinstance(
        _decide(_change(10, justification="loot", justified_by=2), definition=loose), a.Permitted
    )


def test_caps() -> None:
    """Above ``per_effect`` → ``Capped`` to it; ``per_turn`` counts the turn's earlier effects
    (400 + 400 under 500 → the second capped to 100); nothing left → ``cap``; decreases too;
    the consumer is never capped."""
    gain = a.StateEffect("gold", CAL, delta=Decimal(800), justification="loot", justified_by=4)
    assert _decide(gain, DIRECTOR) == a.Capped(Decimal(500), Decimal(800))
    first = a.StateEffect("gold", CAL, delta=Decimal(400), justification="reward", justified_by=4)
    assert isinstance(_decide(first, DIRECTOR), a.Permitted)
    used = a.authorized(first, Decimal(400), {"gold": GOLD})
    assert _decide(first, DIRECTOR, used) == a.Capped(Decimal(100), Decimal(400))
    spent = [a.Authorized(("gold", CAL), Decimal(500))]
    assert _decide(first, DIRECTOR, spent) == a.Unpermitted(a.CAP)
    assert _decide(_change(-700)) == a.Capped(Decimal(-500), Decimal(-700))
    other_purse = [a.Authorized(("gold", MIRA), Decimal(500))]
    assert isinstance(_decide(first, DIRECTOR, other_purse), a.Permitted)
    assert isinstance(_decide(_change(100_000), a.Source("consumer")), a.Permitted)


def test_transfers() -> None:
    """A conserved transfer between tracked parties balances or is ``unbalanced_transfer``; a
    counterparty the State does not list is ``counterparty``; untracked ones are reported; a
    non-conserved transfer may be unbalanced."""
    pay = a.StateEffect(
        "gold", CAL, kind=a.TRANSFER, delta=Decimal(-300), counterparty=a.Counterparty("gold", MIRA)
    )
    assert isinstance(_decide(pay), a.Permitted)
    skewed = a.StateEffect(
        "gold",
        CAL,
        kind=a.TRANSFER,
        delta=Decimal(-300),
        counterparty=a.Counterparty("gold", MIRA, Decimal(250)),
    )
    assert _decide(skewed) == a.Unpermitted(a.UNBALANCED)
    loose = _gold(transfer={"conserved": False, "counterparties": ["till"]})
    assert isinstance(_decide(skewed, definition=loose), a.Permitted)
    elsewhere = a.StateEffect(
        "gold", CAL, kind=a.TRANSFER, delta=Decimal(-5), counterparty=a.Counterparty("bank")
    )
    assert _decide(elsewhere) == a.Unpermitted(a.COUNTERPARTY)
    till = a.StateEffect(
        "gold",
        CAL,
        kind=a.TRANSFER,
        delta=Decimal(20),
        counterparty=a.Counterparty("till"),
        justification="theft",
        justified_by=POSSESSION.beat,
    )
    assert isinstance(_decide(till), a.Permitted)  # an untracked side: not a world gain
    assert a.counterparty_kind({"gold": GOLD}, "till") == a.UNTRACKED
    assert a.counterparty_kind({"gold": GOLD}, "gold") is None


def test_dry_run_transfer_sums_to_zero() -> None:
    """The dry-run folds both tracked sides: a purchase moves gold, it never creates it."""
    values: Values = {("gold", CAL): Decimal(1000), ("gold", MIRA): Decimal(50)}
    effect = DeclaredEffect(
        "gold",
        "a sword",
        scope_key=CAL,
        delta=-300,
        kind="transfer",
        counterparty=("gold", MIRA, None),
    )
    passed, found = rules.dry_run(
        {"gold": GOLD}, values, [rules.Planned(2, 0, CAL, effect, CHARACTER)], evidence=EVIDENCE
    )
    assert found == []
    moved = {p.key: p.new - p.old for p in passed}  # type: ignore[operator]
    assert moved == {("gold", CAL): Decimal(-300), ("gold", MIRA): Decimal(300)}
    assert sum(moved.values()) == 0


def test_dry_run_findings() -> None:
    """Unpermitted → ``state_unpermitted`` (constraint the reason); capped → ``state_capped``
    with the amount the cap leaves."""
    values: Values = {("gold", CAL): Decimal(1000)}
    world = DeclaredEffect(
        "gold", "treasure", scope_key=CAL, delta=100_000, justification="loot", justified_by=2
    )
    _, (found,) = rules.dry_run(
        {"gold": GOLD}, values, [rules.Planned(2, 0, CAL, world, CHARACTER)], evidence=EVIDENCE
    )
    assert (found.tag, found.constraint) == (rules.STATE_UNPERMITTED, a.WORLD_GAIN)
    gain = DeclaredEffect(
        "gold", "a hoard", scope_key=CAL, delta=800, justification="loot", justified_by=4
    )
    _, (capped,) = rules.dry_run(
        {"gold": GOLD}, values, [rules.Planned(4, 0, None, gain, DIRECTOR)], evidence=EVIDENCE
    )
    assert (capped.tag, capped.amount, capped.attempted) == (
        rules.STATE_CAPPED,
        Decimal(500),
        Decimal(1500),
    )


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"authorization": {"sources": []}}, "empty"),
        ({"authorization": {"sources": ["everyone"]}}, "unknown_source"),
        ({"authorization": {"sources": ["director", "director"]}}, "duplicate"),
        ({"authorization": {"caps": {"per_effect": 0}}}, "positive"),
        ({"authorization": {"caps": {"per_turn": -5}}}, "positive"),
        ({"authorization": {"caps": {"every": 5}}}, "shape"),
        ({"authorization": {"justifications": {"sideways": ["x"]}}}, "shape"),
        ({"authorization": {"justifications": {"increase": ["", "x"]}}}, "length"),
        ({"authorization": {"other": 1}}, "unknown"),
        ({"transfer": {"conserved": "yes"}}, "type"),
        ({"transfer": {"counterparties": ["not a name"]}}, "name"),
        ({"transfer": {"rate": 2}}, "unknown"),
    ],
)
def test_definition_shape(patch: dict[str, Any], reason: str) -> None:
    with pytest.raises(InvalidRequest) as refused:
        _gold(**patch)
    assert refused.value.details["reason"] == reason


def test_definition_shape_types() -> None:
    """Caps and transfers belong to number States."""
    for patch in (
        {"authorization": {"caps": {"per_effect": 5}}},
        {"transfer": {"conserved": True}},
    ):
        with pytest.raises(InvalidRequest) as refused:
            validate_definition({"name": "mood", "type": "text", **patch})
        assert refused.value.details["reason"] == "not_number"


def test_rules_need_the_rule_source() -> None:
    """A definition with ``rules`` whose sources omit ``rule`` is 422 ``state_unpermitted``
    (``reason: source``); the default sources and an explicit ``rule`` admit them."""
    # A valid #192 rule on a story-scoped number State: only the source is wrong.
    rules_doc = {"kind": "elapsed", "per_minutes": 60, "delta": -1, "bounds_clamp": True}
    with pytest.raises(StateUnpermitted) as refused:
        _gold(scope="story", rules=rules_doc, authorization={"sources": ["director"]})
    assert (refused.value.code, refused.value.details["reason"]) == ("state_unpermitted", "source")
    assert refused.value.details["source"] == "rule"
    ruled = _gold(scope="story", rules=rules_doc, authorization={"sources": ["director", "rule"]})
    assert ruled.rules
    assert validate_definition(
        {"name": "gold", "type": "number", "initial_value": 0, "rules": rules_doc}
    ).rules
