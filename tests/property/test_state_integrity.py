"""STATE-INTEGRITY: a State's value in every view is the fold of its active changes there, and
no committed value breaks its constraints (docs/04 §6; FR-STATE-1,4,6; #185).

Every STATE-INTEGRITY M6 function lives in this file (the traceability gate counts a test ID per
milestone in one file), one M6 marker each.

**In memory** (300 examples in CI): random sequences of turns (each asking for one change to
one of four States: a bounded number, a number with no initial value, an enum and a text; a
``set`` or a ``delta``, counted or held), truncations, untruncations, swaps to a dormant
continuation, forks and widening definition updates, over a model of the timeline (each turn's
variant is active, discarded or dormant, and a change takes its turn's membership, as
``core.membership`` moves it). The oracle is what each change left when it was committed (its
``new_value``): FR-STATE-4's "rewinding restores the value as of that point" means the active
view's value is the value the last active counted change left. Each step asserts that the pure
fold (``core.states.model.fold``) of the active changes equals the oracle in every story and
that it breaks no constraint; ``check_change`` refuses a change whose result would break one,
and the model then records nothing (the complement).

**On Postgres** (``__postgres``): one seeded sequence of the same steps through the real
operations (``core.states``' writes in turns; #65's truncate and untruncate, #66's swap and
regenerate, #67's fork) and definition updates. After every step, for every State and scope in
every story: ``value_of`` (active) equals the oracle (the stored ``new_value`` of the last active
counted change), the ``state_values`` projection equals ``fold_state``, and no value breaks its
State's constraints. A refused change writes no row and leaves the projection row as it was; the
``as_recorded`` view of a truncated story still folds the discarded changes.

``__module_off``: with ``states.level`` off every write is 409 ``module_off`` and the projection
holds no row for the story; ``prompt_only`` takes declarations and consumer changes but refuses a
turn's change.
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import states
from epistrel.core.enums import (
    OperationKind,
    StateChangeKind,
    StateChangeSource,
    StateChangeStatus,
)
from epistrel.core.errors import InvalidRequest, ModuleOff
from epistrel.core.membership import Position
from epistrel.core.states import StateChange, StateDefinition, Undefined, model
from tests import canon_support as c
from tests import state_support as ss
from tests.event_support import rows
from tests.fork_support import fork
from tests.memory_support import use_structured
from tests.projection_support import swap, truncate, untruncate

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 3_000 if NIGHTLY else 300

DEFINITIONS: tuple[StateDefinition, ...] = (
    StateDefinition.model_validate(
        {
            "name": "gold",
            "type": "number",
            "constraints": {"non_negative": True, "max": 50},
            "initial_value": 10,
        }
    ),
    StateDefinition.model_validate(
        {"name": "debt", "type": "number", "constraints": {"min": -20, "max": 20}}
    ),
    StateDefinition.model_validate(
        {
            "name": "mood",
            "type": "enum",
            "constraints": {"allowed_values": ["calm", "wary", "angry"]},
            "initial_value": "calm",
        }
    ),
    StateDefinition.model_validate({"name": "rumour", "type": "text"}),
)
BY_NAME = {d.name: d for d in DEFINITIONS}
COUNTED_OR_NOT = (
    StateChangeStatus.COMMITTED,
    StateChangeStatus.COMMITTED,
    StateChangeStatus.COMMITTED,
    StateChangeStatus.CONVERTED,
    StateChangeStatus.HELD,
    StateChangeStatus.DROPPED,
)


# -- the model of the timeline -------------------------------------------------------------------


@dataclass
class Row:
    id: int
    state: str
    turn: int
    variant: int
    kind: StateChangeKind
    value: Any
    delta: Decimal | None
    status: StateChangeStatus
    left: Any
    """What the change left when it was committed (the oracle)."""


@dataclass
class Turn:
    number: int
    variant: int
    membership: str = "active"
    group: int = 0


@dataclass
class Story:
    definitions: dict[str, StateDefinition] = field(default_factory=lambda: dict(BY_NAME))
    turns: list[Turn] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    truncations: list[list[Turn]] = field(default_factory=list)
    groups: int = 0

    def turn_of(self, row: Row) -> Turn:
        return next(t for t in self.turns if (t.number, t.variant) == (row.turn, row.variant))

    def head(self) -> int:
        return max((t.number for t in self.turns if t.membership == "active"), default=0)

    def active(self, state: str) -> list[Row]:
        return [r for r in self.rows if r.state == state and self.turn_of(r).membership == "active"]

    def folded(self, state: str) -> Any:
        found = model.fold(
            self.definitions[state].initial_value,
            [
                model.ChangeRow(
                    f"{r.id:012d}", r.kind, r.value, r.delta, r.status, Position(r.turn, r.variant)
                )
                for r in self.active(state)
            ],
        )
        return found.value

    def oracle(self, state: str) -> Any:
        counted = [r for r in self.active(state) if r.status in model.COUNTED]
        if not counted:
            initial = self.definitions[state].initial_value
            return Undefined if initial is None else initial
        return max(counted, key=lambda r: (r.turn, r.id)).left


@dataclass(frozen=True)
class Ask:
    """One turn's change request: a ``set`` (``value``) or a ``delta`` (``amount``)."""

    state: str
    value: Any = None
    amount: int | None = None
    status: StateChangeStatus = StateChangeStatus.COMMITTED

    def change(self) -> StateChange:
        if self.amount is not None:
            return ss.delta(self.state, self.amount, status=self.status)
        return ss.set_to(self.state, self.value, status=self.status)


VALUES = {
    "gold": st.integers(-5, 60).map(Decimal),
    "debt": st.integers(-25, 25).map(Decimal),
    "mood": st.sampled_from(["calm", "wary", "angry", "furious"]),
    "rumour": st.sampled_from(["the mill burned", "x" * 201, "a stranger came"]),
}


@st.composite
def asks(draw: st.DrawFn) -> Ask:
    state = draw(st.sampled_from(sorted(BY_NAME)))
    status = draw(st.sampled_from(COUNTED_OR_NOT))
    if BY_NAME[state].type.value == "number" and draw(st.booleans()):
        return Ask(state, amount=draw(st.integers(-30, 30)), status=status)
    return Ask(state, value=draw(VALUES[state]), status=status)


steps = st.one_of(
    asks().map(lambda a: ("turn", a)),
    asks().map(lambda a: ("turn", a)),
    asks().map(lambda a: ("turn", a)),
    st.integers(0, 6).map(lambda n: ("truncate", n)),
    st.just(("untruncate", None)),
    st.just(("swap", None)),
    st.integers(0, 6).map(lambda n: ("fork", n)),
    st.integers(1, 20).map(lambda n: ("widen", n)),
)


def attempt(story: Story, ask: Ask) -> Any:
    """What ``apply_change`` decides for ``ask`` against the model's active view: the value it
    leaves, or the ``Violation`` it is refused with (``None``: refused as a request, a delta on
    an undefined value or a text too long)."""
    definition = story.definitions[ask.state]
    change = ask.change()
    current = story.folded(ask.state)
    counted = change.status in model.COUNTED
    if change.kind is StateChangeKind.SET:
        if isinstance(change.value, str) and len(change.value) > model.TEXT_MAX:
            return None
        value, delta = change.value, None
    else:
        value, delta = None, None if change.delta is None else Decimal(change.delta)
        if counted and isinstance(current, model.UndefinedType):
            return None
    left = model.step(current, change.kind, value, delta) if counted else current
    refused = model.check_change(definition, change, left, current)
    return refused if refused is not None else left


def play_turn(story: Story, ask: Ask, next_id: int) -> bool:
    """A new turn over the head asking for ``ask``; answers whether a change was recorded."""
    number = story.head() + 1
    for t in story.turns:
        if t.number >= number and t.membership == "discarded":
            t.membership = "dormant"
            t.group = story.groups + 1
    story.groups += 1
    story.truncations = []
    variant = 1 + max((t.variant for t in story.turns if t.number == number), default=0)
    story.turns.append(Turn(number, variant))
    before = list(story.rows)
    decided = attempt(story, ask)
    if decided is None or isinstance(decided, model.Violation):
        if isinstance(decided, model.Violation):
            assert decided.constraint != "reactive_asserter"
            assert model.violation(story.definitions[ask.state], decided.attempted) is not None
        assert story.rows == before  # the complement: nothing written
        return False
    change = ask.change()
    story.rows.append(
        Row(
            next_id,
            ask.state,
            number,
            variant,
            change.kind,
            change.value,
            None if change.delta is None else Decimal(change.delta),
            change.status,
            decided,
        )
    )
    return True


def do_truncate(story: Story, to: int) -> None:
    if to >= story.head():
        return
    gone = [t for t in story.turns if t.membership == "active" and t.number > to]
    for t in gone:
        t.membership = "discarded"
    story.truncations.append(gone)


def do_untruncate(story: Story) -> None:
    if not story.truncations:
        return
    for t in story.truncations.pop():
        t.membership = "active"


def do_swap(story: Story) -> None:
    dormant = [t for t in story.turns if t.membership == "dormant"]
    if not dormant:
        return
    group = max(t.group for t in dormant)
    back = [t for t in dormant if t.group == group]
    first = min(t.number for t in back)
    story.groups += 1
    for t in story.turns:
        if t.membership == "active" and t.number >= first:
            t.membership = "dormant"
            t.group = story.groups
    for t in back:
        t.membership = "active"
    story.truncations = []


def do_fork(story: Story, at: int) -> Story:
    kept = [replace(t) for t in story.turns if t.membership == "active" and t.number <= at]
    keys = {(t.number, t.variant) for t in kept}
    return Story(
        definitions=dict(story.definitions),
        turns=kept,
        rows=[replace(r) for r in story.rows if (r.turn, r.variant) in keys],
    )


def widen(story: Story, by: int) -> None:
    """A definition update that widens every number's bounds (the current value still holds)."""
    for name, definition in list(story.definitions.items()):
        bounds = definition.constraints
        if definition.type.value != "number":
            continue
        story.definitions[name] = definition.model_copy(
            update={
                "constraints": bounds.model_copy(
                    update={
                        "max": None if bounds.max is None else bounds.max + by,
                        "min": None if bounds.min is None else bounds.min - by,
                    }
                )
            }
        )


def check(story: Story) -> None:
    for name, definition in story.definitions.items():
        folded = story.folded(name)
        assert folded == story.oracle(name), (name, folded, story.oracle(name))
        assert model.violation(definition, folded) is None, (name, folded)


@pytest.mark.part("M6", "FR-STATE-1,4,6")
@settings(max_examples=EXAMPLES, deadline=None)
@given(st.lists(steps, min_size=1, max_size=30))
def test_state_integrity(sequence: list[tuple[str, Any]]) -> None:
    stories = [Story()]
    next_id = 0
    refused = 0
    for kind, arg in sequence:
        story = stories[-1]
        if kind == "turn":
            next_id += 1
            refused += not play_turn(story, arg, next_id)
        elif kind == "truncate":
            do_truncate(story, arg)
        elif kind == "untruncate":
            do_untruncate(story)
        elif kind == "swap":
            do_swap(story)
        elif kind == "fork":
            stories.append(do_fork(story, arg))
        else:
            widen(story, arg)
        for each in stories:
            check(each)


# -- Postgres ---------------------------------------------------------------------------------


async def _check(engine: AsyncEngine, story_id: str, cast: dict[str, str]) -> None:
    """Every State of the story, in every scope, against the oracle and its constraints; the
    projection against the fold."""
    async with engine.connect() as conn:
        declared = await states.definitions(conn, story_id)
    for definition in declared:
        keys = [cast["Cal"], cast["Ann"]] if definition.scope.value == "character" else [None]
        for key in keys:
            active = [
                r
                for r in await ss.change_rows(engine, story_id)
                if r.state_id == definition.id
                and r.scope_key == key
                and r.membership == "active"
                and r.status in ("committed", "converted")
            ]
            if active:
                expected: Any = model.from_json(definition.type, active[-1].new_value)
            else:
                expected = definition.initial_value
            assert await ss.value(engine, story_id, definition.name, key) == expected, (
                definition.name,
                key,
            )
            if expected is not None:
                assert model.violation(definition, expected) is None
    assert await ss.stored_values(engine, story_id) == await ss.folded_values(engine, story_id)


SEED = 185
STEPS = 36


@pytest.mark.integration
@pytest.mark.part("M6", "FR-STATE-1,4,6")
async def test_state_integrity__postgres(engine: AsyncEngine, states_delivered: None) -> None:
    w = await c.world(engine)
    source = w.story_id
    await use_structured(engine, source)
    await ss.set_level(engine, source, "on")
    # Declared at turn 1, in a turn (a fork copies an operation with an event); the sequence
    # never truncates before it.
    await ss.define_all(
        engine, source, *DEFINITIONS, ss.gold(name="purse", constraints={"max": 30})
    )
    cast = {name: w.characters[name] for name in ("Cal", "Ann")}
    chosen = random.Random(SEED)  # noqa: S311 - a seeded sequence, not a secret
    stories = [source]
    refusals = 0
    done: Counter[str] = Counter()
    for _ in range(STEPS):
        story = stories[-1]
        roll = chosen.random()
        if roll < 0.55:
            refusals += await _turn(engine, story, chosen, cast, regenerate=roll < 0.08)
            done["regenerate" if roll < 0.08 else "turn"] += 1
        elif roll < 0.7:
            head = (
                await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
            )[0]
            cut = chosen.randrange(1, max(2, int(head.next_turn_index)))
            done["truncate"] += await truncate(engine, story, cut) is not None
        elif roll < 0.78:
            done["untruncate"] += await untruncate(engine, story) is not None
        elif roll < 0.88:
            done["swap"] += await swap(engine, story) is not None
        elif roll < 0.94 and len(stories) < 3:
            forked = await fork(engine, story)
            done["fork"] += 1
            stories.append(forked.story_id)
            cast = {
                name: next(f.id for f in forked.characters if f.name == name)
                for name in ("Cal", "Ann")
            }
        else:
            async with engine.begin() as conn:
                await states.update_definition(
                    conn,
                    story,
                    "debt",
                    {"constraints": {"max": 20 + chosen.randrange(1, 9)}},
                    principal=c.PRINCIPAL,
                )
            done["update"] += 1
        await _check(engine, story, cast)
    assert refusals > 0, "the sequence never asked for a change the constraints refuse"
    assert set(done) == {
        "turn",
        "regenerate",
        "truncate",
        "untruncate",
        "swap",
        "fork",
        "update",
    }, done
    # The source and its forks hold their own changes: none is shared.
    ids = [{r.id for r in await ss.change_rows(engine, s)} for s in stories]
    for i, mine in enumerate(ids):
        for theirs in ids[i + 1 :]:
            assert not mine & theirs

    # As recorded: a truncated story's view still folds the discarded change, which the active
    # view no longer does.
    story = stories[0]
    await _turn(engine, story, chosen, cast, force=ss.set_to("rumour", "the bridge fell"))
    head = (await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))[0]
    await truncate(engine, story, int(head.next_turn_index) - 2)
    await _check(engine, story, cast)
    rumour = BY_NAME["rumour"]
    async with engine.connect() as conn:
        rumour_id = (await states.definition(conn, story, "rumour")).id
    every = [r for r in await ss.change_rows(engine, story) if r.state_id == rumour_id]
    assert any(r.membership == "discarded" and r.value == "the bridge fell" for r in every)
    recorded = model.fold(
        rumour.initial_value,
        [
            model.ChangeRow(
                r.id,
                StateChangeKind(r.kind),
                r.value,
                None,
                StateChangeStatus(r.status),
                Position(r.narrative_turn, r.turn_variant, r.slot_k, r.beat, r.step),
            )
            for r in every
        ],
    ).value
    assert await ss.value(engine, story, "rumour", view="as_recorded") == recorded
    assert await ss.value(engine, story, "rumour") != "the bridge fell"


async def _turn(
    engine: AsyncEngine,
    story: str,
    chosen: random.Random,
    cast: dict[str, str],
    *,
    regenerate: bool = False,
    force: StateChange | None = None,
) -> int:
    """One turn asking for a random change (or ``force``); answers 1 when it was refused, after
    asserting that the refusal wrote nothing."""
    change = force or _random_change(chosen, cast)
    before_rows = await ss.change_rows(engine, story)
    before_values = await ss.stored_values(engine, story)
    head = (await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))[0]
    turn = int(head.next_turn_index) - 1 if regenerate and head.next_turn_index > 1 else None
    try:
        _, (outcome,) = await ss.change(engine, story, change, turn=turn)
    except InvalidRequest:
        assert await ss.change_rows(engine, story) == before_rows
        return 0
    if isinstance(outcome, states.Violation):
        assert outcome.constraint != "reactive_asserter"
        assert await ss.change_rows(engine, story) == before_rows
        assert await ss.stored_values(engine, story) == before_values
        return 1
    return 0


def _random_change(chosen: random.Random, cast: dict[str, str]) -> StateChange:
    name = chosen.choice(["gold", "debt", "mood", "rumour", "purse"])
    status = chosen.choice(COUNTED_OR_NOT)
    if name == "purse":
        who = chosen.choice([cast["Cal"], cast["Ann"]])
        return ss.delta(name, chosen.randrange(-15, 25), scope_key=who, status=status)
    if name in ("gold", "debt") and chosen.random() < 0.6:
        return ss.delta(name, chosen.randrange(-30, 30), status=status)
    values: dict[str, list[Any]] = {
        "gold": [Decimal(v) for v in (0, 5, 40, 60, -1)],
        "debt": [Decimal(v) for v in (-25, -5, 0, 15)],
        "mood": ["calm", "wary", "angry", "furious"],
        "rumour": ["the mill burned", "a stranger came"],
    }
    return ss.set_to(name, chosen.choice(values[name]), status=status)


@pytest.mark.integration
@pytest.mark.part("M6", "FR-STATE-1")
async def test_state_integrity__module_off(engine: AsyncEngine, states_delivered: None) -> None:
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    with pytest.raises(ModuleOff) as off:
        await ss.define(engine, story, ss.gold())
    assert off.value.details["module"] == "states"
    await ss.set_level(engine, story, "on")
    await ss.define(engine, story, ss.gold())
    cal = w.characters["Cal"]
    await ss.set_level(engine, story, None)
    with pytest.raises(ModuleOff):
        await ss.change(engine, story, ss.delta("gold", 5, scope_key=cal))
    async with engine.begin() as conn:
        with pytest.raises(ModuleOff):
            await states.update_definition(
                conn, story, "gold", {"unit": "crowns"}, principal=c.PRINCIPAL
            )
    assert await ss.stored_values(engine, story) == []
    assert await ss.change_rows(engine, story) == []
    # The world clock's name is exempt from the gate (#193 declares it), never from the
    # reservation.
    with pytest.raises(InvalidRequest) as reserved:
        await ss.define(engine, story, ss.gold(name="world_clock", scope="story"))
    assert reserved.value.details["reason"] == "reserved_state"

    # prompt_only: declarations and consumer changes, never a turn's.
    await ss.set_level(engine, story, "prompt_only")
    await ss.define(engine, story, ss.gold(name="favour", scope="story"))
    _, (applied,) = await ss.change(
        engine,
        story,
        ss.delta("gold", 5, scope_key=cal, source=StateChangeSource.CONSUMER),
        kind=OperationKind.CONTROL,
    )
    assert isinstance(applied, states.Applied)
    with pytest.raises(ModuleOff) as turn_off:
        await ss.change(engine, story, ss.delta("gold", 1, scope_key=cal))
    assert turn_off.value.details["level"] == "prompt_only"
    # Complement: with the module on, the turn's change is written and projected.
    await ss.set_level(engine, story, "on")
    await ss.change(engine, story, ss.delta("gold", 1, scope_key=cal))
    assert await ss.value(engine, story, "gold", cal) == Decimal(6)
    assert len(await ss.stored_values(engine, story)) == 1
