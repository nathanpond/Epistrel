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
from epistrel.core.profiles import schema
from epistrel.core.states import StateChange, StateDefinition, Undefined, model
from tests import canon_support as c
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fork_support import fork
from tests.memory_support import use_structured
from tests.projection_support import swap, truncate, untruncate
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat

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
                and r.status in {s.value for s in model.COUNTED}
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


@pytest.mark.integration
@pytest.mark.part("M6", "FR-STATE-1")
async def test_state_integrity__m6_inertness(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """With States delivered for real (#203: no test patch), the module is inert where its
    level is off. On a structured story with ``states.level = off``: declaring a State in core
    (#185) is ``module_off``, and so are the State API's read, set and declaration (#186, 409);
    a turn whose render declares a ``change`` effect (#187) is refused as
    ``invalid_structure``, no ``## Tracked values`` section reaches any render's prompt, and no
    ``state_changes`` row is written. Under ``prompt_only`` effects stay refused the same way
    (the Lite form takes API sets only), with no section either.

    Complement: with the level ``on`` the same story's character render does get its
    ``## Tracked values``."""
    stub_settings = auth_settings(pg_url, model_adapter="stub")
    assert schema.is_delivered("states", "prompt_only")
    assert schema.is_delivered("states", schema.ON)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, es.gold(), gold_of_player=100)
        cal = ids["Cal"]
        await ss.set_level(engine, story, "off")
        with pytest.raises(ModuleOff) as off:
            await ss.define(engine, story, ss.gold(name="silver"))
        assert off.value.details["module"] == "states"
        s = f"/stories/{story}"
        for method, path, payload in (
            ("GET", f"{s}/states", None),
            ("PUT", f"{s}/states/gold", {"delta": 5, "reason": "a purse"}),
            ("POST", f"{s}/states", {"definition": {"name": "silver", "type": "number"}}),
        ):
            refused = await client.request(
                method,
                path,
                json=payload,
                params={"viewpoint": "omniscient"} if method == "GET" else None,
                headers=WRITE,
            )
            assert refused.status_code == 409, (path, refused.text)
            assert refused.json()["error"]["code"] == "module_off"
        changes = len(await ss.change_rows(engine, story))

        for level in ("off", "prompt_only"):
            await ss.set_level(engine, story, level)
            stub.reset()
            ms.renders_for(
                stub,
                "Mira",
                es.render(
                    beat(
                        "do",
                        "Mira takes the coins.",
                        effects=[es.effect("gold", scope_key=cal, delta=-5, reason="a nail")],
                    )
                ),
            )
            es.narrator(stub, es.render(beat("narration", "Smoke curls.")))
            answer = await respond(client, story, body(cal, None, do="asks for a nail"))
            assert answer.status_code == 503, (level, answer.text)
            assert answer.json()["error"]["details"]["reason"] == "invalid_structure", level
            assert all("## Tracked values" not in p for p in ms.render_prompts(stub, "Mira"))
            assert all("## Tracked values" not in p for p in es.prompts(stub, "NarratorRender"))
            assert len(await ss.change_rows(engine, story)) == changes, level
        assert await es.gold_of(engine, story, cal) == 100

        # Complement: on, the character render reads its tracked values.
        await ss.set_level(engine, story, "on")
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", "A nail, then.", intent="sincere")))
        es.narrator(stub, es.render(beat("narration", "Smoke curls.")))
        played = await respond(client, story, body(cal, None, do="asks for a nail"))
        assert played.status_code == 200, played.text
        assert any("## Tracked values" in p for p in ms.render_prompts(stub, "Mira"))


# -- M7: amendments in the sequence (#236) ------------------------------------------------------

BOUNDED = BY_NAME["gold"]
"""Non-negative, at most 50, initially 10: an amendment ``set`` can break either bound."""


@st.composite
def amended_history(draw: st.DrawFn) -> tuple[list[model.ChangeRow], list[model.ChangeRow]]:
    """Positioned changes at non-decreasing story-minutes, and amendment ``set``s, each with an
    optional valid start and window end (FR-STORE-20's position-less corrections)."""
    positioned: list[model.ChangeRow] = []
    minute = 0
    for n in range(draw(st.integers(0, 8))):
        minute += draw(st.integers(0, 30))
        is_set = draw(st.booleans())
        positioned.append(
            model.ChangeRow(
                f"p{n:02}",
                StateChangeKind.SET if is_set else StateChangeKind.DELTA,
                Decimal(draw(st.integers(0, 50))) if is_set else None,
                None if is_set else Decimal(draw(st.integers(-15, 15))),
                StateChangeStatus.COMMITTED,
                Position(n + 1, 0, 0, 1, 1),
                valid_time=minute,
            )
        )
    amended: list[model.ChangeRow] = []
    for n in range(draw(st.integers(0, 3))):
        start = draw(st.none() | st.integers(0, minute + 30))
        end = draw(st.none() | st.integers((start or 0) + 1, minute + 60))
        amended.append(
            model.ChangeRow(
                f"a{n:02}",
                StateChangeKind.SET,
                Decimal(draw(st.integers(-5, 60))),
                None,
                StateChangeStatus.COMMITTED,
                None,
                valid_time=start,
                valid_to=end,
            )
        )
    return positioned, amended


def _story_time(positioned: list[model.ChangeRow], k: int) -> int | None:
    times = [c.valid_time for c in positioned[:k] if c.valid_time is not None]
    return max(times) if times else None


def _oracle(positioned: list[model.ChangeRow], amended: list[model.ChangeRow], k: int) -> Any:
    """The value through the ``k``-th positioned change, stated independently of ``ordered``:
    every change on one timeline sorted by (story-minute, positioned before amendments, order),
    an amendment with no valid start before them all; an amendment counts when its start is at
    or before the checkpoint's minute and its window has not closed by it."""
    at = _story_time(positioned, k)
    timeline: list[tuple[float, int, float, str, model.ChangeRow]] = [
        (float(c.valid_time or 0), 0, 0.0, c.id, c) for c in positioned[:k]
    ]
    for a in amended:
        if at is not None and a.valid_time is not None and a.valid_time > at:
            continue
        if at is not None and a.valid_to is not None and a.valid_to <= at:
            continue
        start = float("-inf") if a.valid_time is None else float(a.valid_time)
        if at is None and a.valid_time is not None:
            start = float("inf")  # no minute known yet: after every positioned change
        own = float("-inf") if a.valid_time is None else float(a.valid_time)
        timeline.append((start, 1, own, a.id, a))
    value: Any = BOUNDED.initial_value
    for *_, change in sorted(timeline, key=lambda t: t[:4]):
        value = model.step(value, change.kind, change.value, change.delta)
    return value


@pytest.mark.part("M7", "FR-STATE-4")
@settings(max_examples=EXAMPLES, deadline=None)
@given(amended_history())
def test_state_integrity__m7_amend(
    history: tuple[list[model.ChangeRow], list[model.ChangeRow]],
) -> None:
    """With amendments in the sequence (#236): the value through every checkpoint is the fold
    of the positioned changes there and the amendment changes eligible there (#221's
    ``eligible`` at the checkpoint's story-minute), each at its valid time and reverting after
    its window; and an amendment ``record_amendment`` would accept
    (``store.amended_checkpoints`` finds no breach) leaves no checkpoint outside the State's
    constraints. Complement: one it refuses names a checkpoint whose value is outside them."""
    from epistrel.core.amendments.eligibility import PartTiming, eligible
    from epistrel.core.amendments.store import amended_checkpoints

    positioned, amended = history
    every = [*positioned, *amended]
    for k in range(len(positioned) + 1):
        at = _story_time(positioned, k)
        inside = [
            a
            for a in amended
            if eligible(PartTiming(valid_from_minutes=a.valid_time), story_time=at).folds
        ]
        assert model.fold(BOUNDED.initial_value, [*positioned[:k], *inside]).value == _oracle(
            positioned, amended, k
        )
    breach = amended_checkpoints(BOUNDED, every, None)
    values = [_oracle(positioned, amended, k) for k in range(len(positioned) + 1)]
    if breach is None:
        assert all(model.violation(BOUNDED, v) is None for v in values), values
    else:
        _, broken, folded = breach
        assert broken is not None
        assert model.violation(BOUNDED, folded) == broken
        assert folded in values


@pytest.mark.integration
@pytest.mark.part("M7", "FR-STATE-4")
async def test_state_integrity__m7_amend_postgres(
    engine: AsyncEngine, states_delivered: None, clock_delivered: None
) -> None:
    """On Postgres (#236): Cal's gold is set to 100 at turn 1 (minute 0) and spent by 10 at
    turns 2 (minute 60) and 3 (minute 120). An amendment says he held 500 from minute 60: it
    is in ``value_of(through=turn 2)`` (500, after turn 2's spend at the same minute) and
    ``through=turn 3`` (490), not ``through=turn 1`` (100); the projection is the fold and its
    ``since_position`` the last positioned change. A revert restores 80 with nothing deleted.
    Complement: a part that would leave a negative balance at turn 3 (5 from minute 60) is 422
    ``state_constraint_violation`` naming the checkpoint, nothing written; parts on the
    ``world_clock``, a derived State, a transfer-conserved State, an external State or a
    discarded definition are 422 ``state_not_amendable``, nothing written; the
    ``scheduled_events`` row count never changes."""
    from epistrel.core.amendments.store import StatePartDraft
    from epistrel.core.errors import EpistrelError
    from tests import amendment_support as am
    from tests import clock_support as clock
    from tests import provider_support as ps

    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await clock.set_level(engine, story, "on")
    await ss.define_all(
        engine,
        story,
        ss.gold(),
        StateDefinition.model_validate(
            {
                "name": "weight",
                "type": "number",
                "rules": {"kind": "derived", "expression": "1 + 1"},
                "authorization": {"sources": ["rule", "author"]},
            }
        ),
        ss.gold(name="coin", transfer={"conserved": True, "counterparties": ["till"]}),
    )
    scheduled = "SELECT count(*) AS n FROM scheduled_events WHERE story_id = :s"
    before = (await rows(engine, scheduled, s=story))[0].n
    one, _ = await ss.change(engine, story, ss.set_to("gold", 100, scope_key=cal))
    await clock.advance(engine, story, 60)
    two, _ = await ss.change(engine, story, ss.delta("gold", -10, scope_key=cal))
    await clock.advance(engine, story, 60)
    three, _ = await ss.change(engine, story, ss.delta("gold", -10, scope_key=cal))
    turns = [int(op.narrative_turn or 0) for op in (one, two, three)]

    async def through(turn: int | None) -> Any:
        if turn is None:
            return await ss.value(engine, story, "gold", cal)
        return await ss.value(engine, story, "gold", cal, through=ss.at(turn))

    assert [await through(t) for t in turns] == [100, 90, 80]
    _, made, _ = await am.amend(
        engine, story, [StatePartDraft("gold", 500, scope_key=cal, valid_from_minutes=60)]
    )
    assert [await through(t) for t in turns] == [100, 500, 490]
    assert await through(None) == 490
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)
    (part,) = await rows(
        engine, "SELECT * FROM amendment_parts WHERE amendment_id = :a", a=made.amendment_id
    )
    (row,) = await rows(
        engine, "SELECT * FROM state_changes WHERE amendment_part_id = :p", p=part.id
    )
    assert part.written_state_change_id == row.id
    assert (row.kind, row.source, row.contingency, row.status) == (
        "set",
        "author",
        "unconditional",
        "committed",
    )
    assert (row.narrative_turn, row.valid_time_minutes) == (None, 60)
    (stored,) = [
        r
        for r in await rows(engine, "SELECT * FROM state_values WHERE story_id = :s", s=story)
        if r.scope_key == cal
    ]
    assert stored.since_turn == turns[2]

    # A revert restores the fold and deletes nothing.
    count = "SELECT count(*) AS n FROM state_changes WHERE story_id = :s"
    rows_before = (await rows(engine, count, s=story))[0].n
    await am.amend(engine, story, [], kind="undo", revert=made.amendment_id)
    assert [await through(t) for t in turns] == [100, 90, 80]
    assert (await rows(engine, count, s=story))[0].n == rows_before
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)

    # Complement: a breach at turn 3, and the States an amendment may not correct.
    async def refused(draft: StatePartDraft) -> EpistrelError:
        parts_before = await am.table_rows(engine, "amendment_parts", story)
        with pytest.raises(EpistrelError) as found:
            await am.amend(engine, story, [draft])
        assert await am.table_rows(engine, "amendment_parts", story) == parts_before
        assert (await rows(engine, count, s=story))[0].n == rows_before
        return found.value

    breach = await refused(StatePartDraft("gold", 5, scope_key=cal, valid_from_minutes=60))
    assert breach.code == "state_constraint_violation"
    assert breach.details["at_checkpoint"][0] == turns[2]
    assert breach.details["folded"] == -5
    typed = await refused(StatePartDraft("gold", "lots", scope_key=cal))
    assert typed.code == "state_constraint_violation"
    for name, reason in (
        ("world_clock", "clock"),
        ("weight", "derived"),
        ("coin", "transfer_conserved"),
    ):
        scope = cal if name == "coin" else None
        found = await refused(StatePartDraft(name, 1, scope_key=scope))
        assert (found.code, found.details["reason"]) == ("state_not_amendable", reason), name
    # A definition a truncation discarded (declared after turn 3, then cut).
    await ss.define_all(engine, story, ss.gold(name="ribbon", scope="story"))
    await ss.change(engine, story, ss.delta("gold", 1, scope_key=cal))
    await truncate(engine, story, turns[2])
    rows_before = (await rows(engine, count, s=story))[0].n
    gone = await refused(StatePartDraft("ribbon", 1))
    assert (gone.code, gone.details["reason"]) == ("state_not_amendable", "discarded")
    assert (await rows(engine, scheduled, s=story))[0].n == before

    other = await c.world(engine)
    await use_structured(engine, other.story_id)
    await ss.set_level(engine, other.story_id, "on")
    ledger = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(ledger))
    await ps.external_gold(engine, other.story_id, ledger)
    with pytest.raises(EpistrelError) as external:
        await am.amend(
            engine,
            other.story_id,
            [StatePartDraft("gold", 1, scope_key=other.characters["Cal"])],
        )
    assert (external.value.code, external.value.details["reason"]) == (
        "state_not_amendable",
        "external",
    )


@pytest.mark.integration
@pytest.mark.part("M7", "FR-STATE-4")
async def test_state_integrity__m7_directives(engine: AsyncEngine, states_delivered: None) -> None:
    """With directive State sets in the sequence (#226): turns' random changes interleaved with
    the author's sets and deltas at inter-turn slots (``source = author``); after every step
    every State's value is the fold of its active changes and no committed value breaks its
    constraints. A directive whose change would break one is 422 ``state_constraint_violation``
    with nothing written."""
    import dataclasses

    from epistrel.core.errors import EpistrelError
    from epistrel.core.ids import new_ulid
    from epistrel.orchestrator import directives

    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(engine, story, *DEFINITIONS, ss.gold(name="purse", constraints={"max": 30}))
    cast = {name: w.characters[name] for name in ("Cal", "Ann")}
    chosen = random.Random(SEED + 7)  # noqa: S311 - a seeded sequence, not a secret
    refused = 0
    authored = 0
    for _ in range(24):
        if chosen.random() < 0.5:
            await _turn(engine, story, chosen, cast)
        else:
            change = dataclasses.replace(
                _random_change(chosen, cast),
                source=StateChangeSource.AUTHOR,
                status=StateChangeStatus.COMMITTED,
            )
            before = await ss.change_rows(engine, story)
            failure: EpistrelError | None = None
            try:
                await directives._apply_now(
                    engine,
                    story,
                    new_ulid(),
                    directives.DirectiveRequest(text="The author sets a value.", kind="durable"),
                    directives._Normalized(states=[change]),
                    [],
                    principal="p-author",
                )
            except EpistrelError as failed:
                failure = failed
            if failure is None:
                authored += 1
            else:
                assert failure.code in ("state_constraint_violation", "validation_error"), failure
                assert await ss.change_rows(engine, story) == before
                refused += failure.code == "state_constraint_violation"
        await _check(engine, story, cast)
    assert authored > 0
    sources = {r.source for r in await ss.change_rows(engine, story)}
    assert "author" in sources
