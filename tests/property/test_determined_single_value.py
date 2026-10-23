"""DETERMINED-SINGLE-VALUE (docs/04 §6; FR-COMMIT-6, FR-EPI-8, FR-PROP-3; #169).

No determined proposition ever has more than one truth value at a valid-time across all
partitions, and no functional predicate has two objects for one uniqueness key over overlapping
time. Over random casts and random sequences of hidden commits (a value decided once, scoped to
its noticers), guesses (a character's own belief of a value), reveals (sincere or deceptive,
by a knower or not) and attempts to assign a value through a viewpoint, applied with
``core.commit.hidden``'s rules (``scope_of``, ``knows``, ``widened``, ``refuse_holder``), the
invariant ``single_value_violations`` holds after every step: each key has at most one current
value; what any character reads of a key is that value or nothing (absence: unknown), never a
value of its own; a character's divergent version lives only in its beliefs; a reveal only adds
readers; a viewpoint assignment is refused. Complement: the invariant check is not vacuous (two
values of a key over overlapping time are reported; disjoint times, or one value, are not), and
an unknowing character's different guess changes no canon value. It runs in memory (300
examples); one Postgres run commits a hidden value, gives an unknowing character a different
belief, refuses a character holder (422 ``viewpoint_cannot_assign_truth``) and an author's retcon
of the key, and checks the stored canon against the same invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.commit import hidden
from epistrel.core.commit.hidden import KeyedValue, single_value_violations
from epistrel.core.enums import OperationKind, RecordProvenance
from epistrel.core.errors import ViewpointCannotAssignTruth
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.visibility import Visibility
from tests import canon_support as cs
from tests import commit_support as c
from tests import partition_support as parts
from tests import testimony_support as ts
from tests.event_support import rows

CAST = tuple(f"C{i}" for i in range(5))
KEYS = ("knocker", "message", "sender")
VALUES = ("Bobby", "the mailman", "Sam", "the meme", "the text")


@dataclass
class World:
    """Canon (each key's one value and scope) and every character's own beliefs."""

    canon: dict[str, tuple[str, Visibility]] = field(default_factory=dict)
    beliefs: dict[str, dict[str, str]] = field(default_factory=dict)

    def values(self) -> list[KeyedValue]:
        return [KeyedValue(k, v, None, None) for k, (v, _) in self.canon.items()]

    def read(self, character: str, key: str) -> str | None:
        """What ``character`` reads of ``key`` in canon: the value, or nothing."""
        found = self.canon.get(key)
        if found is None or not hidden.knows(found[1], character):
            return None
        return found[0]


actions = st.one_of(
    st.tuples(
        st.just("hide"),
        st.sampled_from(KEYS),
        st.sampled_from(VALUES),
        st.lists(st.sampled_from(CAST), max_size=3),
    ),
    st.tuples(
        st.just("guess"), st.sampled_from(CAST), st.sampled_from(KEYS), st.sampled_from(VALUES)
    ),
    st.tuples(
        st.just("reveal"),
        st.sampled_from(KEYS),
        st.sampled_from(CAST),
        st.lists(st.sampled_from(CAST), max_size=4),
        st.sampled_from(("sincere", "deceptive")),
    ),
    st.tuples(st.just("assign"), st.sampled_from(CAST), st.sampled_from(KEYS)),
)


def apply(world: World, action: tuple[Any, ...]) -> None:
    """One step under ``core.commit.hidden``'s rules."""
    kind = action[0]
    if kind == "hide":
        _, key, value, noticers = action
        if key not in world.canon:  # decided once: a determined key takes no second value
            world.canon[key] = (value, hidden.scope_of(noticers))
    elif kind == "guess":
        _, character, key, value = action
        world.beliefs.setdefault(character, {})[key] = value
    elif kind == "reveal":
        _, key, speaker, perceivers, intent = action
        found = world.canon.get(key)
        if found is None or intent == "deceptive" or not hidden.knows(found[1], speaker):
            return
        value, scope = found
        world.canon[key] = (value, hidden.widened(scope, perceivers))
    else:
        _, character, _key = action
        with pytest.raises(ViewpointCannotAssignTruth):
            hidden.refuse_holder(f"character:{character}")


@pytest.mark.part("M5", "FR-COMMIT-6; FR-EPI-8; FR-PROP-3")
@settings(max_examples=300, deadline=None)
@given(st.lists(actions, max_size=40))
def test_determined_single_value(steps: list[tuple[Any, ...]]) -> None:
    """See the module docstring (the in-memory part)."""
    world = World()
    for action in steps:
        before = dict(world.canon)
        apply(world, action)
        assert single_value_violations(world.values()) == []
        for key, (value, scope) in world.canon.items():
            if key in before:
                old_value, old_scope = before[key]
                assert value == old_value  # determined: never a second value
                old = set(old_scope.characters or ())
                assert old <= set(scope.characters or ())  # a reveal only adds readers
            for character in CAST:
                assert world.read(character, key) in (None, value)
        if action[0] == "guess":
            assert world.canon == before  # a guess is an owned belief, never canon
    # Complement: the check is not vacuous.
    two = [KeyedValue("k", "a", 0, 10), KeyedValue("k", "b", 5, 20)]
    assert len(single_value_violations(two)) == 1
    assert (
        single_value_violations([KeyedValue("k", "a", 0, 10), KeyedValue("k", "b", 10, 20)]) == []
    )
    assert single_value_violations([KeyedValue("k", "a", None, None)] * 2) == []


async def _current(engine: AsyncEngine, story_id: str, predicate: str) -> list[KeyedValue]:
    """The stored current values of ``predicate`` as the invariant reads them."""
    found = await rows(
        engine,
        "SELECT f.subject_id, p.object_id, f.valid_from, f.valid_to FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s "
        "AND f.predicate_id = :p AND f.tx_to IS NULL AND f.membership = 'active'",
        s=story_id,
        p=predicate,
    )
    return [
        KeyedValue((r.subject_id, predicate), r.object_id, r.valid_from, r.valid_to) for r in found
    ]


@pytest.mark.integration
@pytest.mark.part("M5", "FR-COMMIT-6; FR-EPI-8; FR-PROP-3")
async def test_determined_single_value__postgres(
    engine: AsyncEngine, testimony_delivered: None
) -> None:
    """See the module docstring (the Postgres run)."""
    k = await c.knock_story(engine)
    await ts.enable_testimony(engine, k.story_id, "defaulted")
    async with engine.begin() as conn:
        phone = await store.add_entity(conn, k.story_id, "item", "the phone")
        meme = await store.add_entity(conn, k.story_id, "other", "the meme")
        text = await store.add_entity(conn, k.story_id, "other", "the text")
        rules = registry.ScenarioRules.parse(
            {
                "predicates": [
                    {
                        "name": "message_content",
                        "subject_type": "item",
                        "object_type": "other",
                        "cardinality": "functional",
                        "temporal_kind": "permanent",
                        "docs": "What the message on a phone says.",
                    }
                ]
            }
        )
        found = await registry.register_author(conn, k.story_id, rules)
    content = found.named("message_content")
    assert content is not None

    def says(value: str) -> Proposition:
        return Proposition(
            subject_id=phone,
            predicate_id=content.id,
            object_id=value,
            valid_time=Interval.of(None, None),
        )

    async def noticed(conn: AsyncConnection, op: Operation) -> hidden.HiddenCommit:
        event = await conn.scalar(
            sql("SELECT id FROM events WHERE operation_id = :o AND type = 'player_input'"),
            {"o": op.operation_id},
        )
        return await hidden.commit_hidden(
            conn, op, says(text), noticers=[k.jenni], event_id=str(event), beat=1
        )

    truth = await c.in_turn(engine, k.story_id, noticed)
    assert truth.visibility == Visibility(scope="characters", characters=(k.jenni,))
    # Bobby, unknowing, holds his own version: a belief record, never a second value.
    guess = await parts.transit(engine, k.story_id, k.bobby, says(meme), kind=OperationKind.CONTROL)
    assert guess.belief.holder_character_id == k.bobby
    values = await _current(engine, k.story_id, content.id)
    assert [v.value for v in values] == [text]
    assert single_value_violations(values) == []

    # A viewpoint cannot assign a value.
    async def assign(conn: AsyncConnection, op: Operation) -> None:
        await canon.commit_fact(
            conn,
            k.story_id,
            says(meme),
            provenance=RecordProvenance.RENDER,
            operation=op,
            holder=k.bobby,
        )

    with pytest.raises(ViewpointCannotAssignTruth):
        await c.in_turn(engine, k.story_id, assign)
    assert [v.value for v in await _current(engine, k.story_id, content.id)] == [text]

    # Even an author's retcon of the key leaves one current value, never two.
    await cs.commit(engine, k.story_id, says(meme), provenance="author", kind=OperationKind.CONTROL)
    values = await _current(engine, k.story_id, content.id)
    assert [v.value for v in values] == [meme]
    assert single_value_violations(values) == []
