"""X-02: belief integrity over time, the M5 part (docs/04 §12; #160; FR-BELIEF-10, FR-EPI-3).

A scripted stub story: canon places ten people (one fact each, committed once). Every turn Bobby
lies to Ann about where one of them is (a ``testimony`` transition citing his verified staged
beat 1, Ann believing at beat 2) or the truth is revealed to her (a ``correction`` citing the
canon fact), on a fixed seed. ``test_x_02`` is the weekly ``soak`` run (``soak.yml``) at 200
turns (``EPISTREL_SOAK_X02_TURNS`` sets fewer); ``test_x_02__pr`` is the same script at 20 turns
in PR CI.

Asserted at every checkpoint and at the end:

- **no false belief becomes canon** (FR-EPI-3): the story's facts are exactly the ten committed
  by the script, unchanged, and a runtime spy on canon's writers (``commit_fact``,
  ``supersede``) sees no call while a transition runs;
- **divergences resolve** (FR-BELIEF-10): for each person, Ann's current belief is canon's place
  when the last event about them was a reveal, and the lie's place (a divergence from canon)
  when it was a lie; each reveal closed the lie it corrected, which keeps its history (held
  until the reveal);
- **every belief has its typed cause**: a runtime spy on ``partitions._insert_belief`` sees it
  called from ``transition()`` only, and each belief's provenance is a transition of it;
- ``belief_state`` equals the fold of the beliefs.

#166's and #169's slices (lies and reveals rendered by the model) extend this script.
"""

from __future__ import annotations

import inspect
import os
import random
from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.epistemic.transitions import Antecedent, Kind, StagedRecord
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.propositions import store
from tests import canon_support as c
from tests import partition_support as ps
from tests.event_support import rows
from tests.memory_support import use_structured

TURNS = int(os.environ.get("EPISTREL_SOAK_X02_TURNS", "200"))
PR_TURNS = 20
PEOPLE = 10
CHECK_EVERY = 10
SEED = 160


class StagedSay:
    """Bobby's line at beat 1 of the turn, verified (#174's ``TurnStage`` stands in)."""

    def __init__(self) -> None:
        self.op: Operation | None = None

    def staged_beat(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        op = self.op
        if op is None or op.narrative_turn is None or int(ref["beat"]) != 1:
            return None
        return StagedRecord(Position(op.narrative_turn, op.turn_variant or 0, 0, 1, 1))

    def render_step(self, ref: Mapping[str, Any]) -> StagedRecord | None:
        del ref
        return None


class Spies:
    """Who calls ``_insert_belief`` and whether canon is written during a transition."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inserts: list[str] = []
        self.canon_in_transition = 0
        self.depth = 0
        insert, commit, supersede, transit = (
            partitions._insert_belief,
            canon.commit_fact,
            canon.supersede,
            transitions.transition,
        )

        async def spied_insert(*args: Any, **kwargs: Any) -> Any:
            caller = inspect.stack()[1].function
            self.inserts.append(caller)
            return await insert(*args, **kwargs)

        async def spied_commit(*args: Any, **kwargs: Any) -> Any:
            self.canon_in_transition += self.depth
            return await commit(*args, **kwargs)

        async def spied_supersede(*args: Any, **kwargs: Any) -> Any:
            self.canon_in_transition += self.depth
            return await supersede(*args, **kwargs)

        async def spied_transition(*args: Any, **kwargs: Any) -> Any:
            self.depth += 1
            try:
                return await transit(*args, **kwargs)
            finally:
                self.depth -= 1

        monkeypatch.setattr(partitions, "_insert_belief", spied_insert)
        monkeypatch.setattr(canon, "commit_fact", spied_commit)
        monkeypatch.setattr(canon, "supersede", spied_supersede)
        monkeypatch.setattr(transitions, "transition", spied_transition)


async def _play(engine: AsyncEngine, turns: int, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    places = list(w.places.values())
    async with engine.begin() as conn:
        people = [await store.add_entity(conn, story, "person", f"P{i}") for i in range(PEOPLE)]
    rng = random.Random(SEED)  # noqa: S311 - a fixed script, not a secret
    truth = {p: rng.choice(places) for p in people}
    facts = {p: await c.commit(engine, story, c.at(p, truth[p], 0, None)) for p in people}
    spies = Spies(monkeypatch)
    oracle = StagedSay()
    last: dict[str, tuple[str, str]] = {}
    lies: dict[str, str] = {}

    def staged(op: Operation) -> Antecedent:
        oracle.op = op
        return Antecedent.staged_beat(op.operation_id, op.turn_variant or 0, 1)

    def beat_two(op: Operation) -> Position:
        assert op.narrative_turn is not None
        return Position(op.narrative_turn, op.turn_variant or 0, 0, 2, 0)

    for turn in range(1, turns + 1):
        person = rng.choice(people)
        if person in lies and rng.random() < 0.5:
            result = await ps.transit(
                engine,
                story,
                ann,
                c.at(person, truth[person], 0, None),
                cause=Kind.CORRECTION,
                antecedent=Antecedent.record("facts", facts[person].id),
            )
            assert result.closed == (lies.pop(person),)
            last[person] = ("reveal", truth[person])
        else:
            place = rng.choice([p for p in places if p != truth[person]])
            result = await ps.transit(
                engine,
                story,
                ann,
                c.at(person, place, 0, None),
                cause=Kind.TESTIMONY,
                antecedent=staged,
                position=beat_two,
                source=bobby,
                oracle=oracle,
            )
            lies[person] = result.belief.id
            last[person] = ("lie", place)
        if turn % CHECK_EVERY == 0 or turn == turns:
            await _check(engine, story, ann, facts, truth, last, spies)


async def _check(
    engine: AsyncEngine,
    story: str,
    ann: str,
    facts: Mapping[str, Any],
    truth: Mapping[str, str],
    last: Mapping[str, tuple[str, str]],
    spies: Spies,
) -> None:
    # No false belief becomes canon: the facts are the script's, unchanged.
    found = await rows(
        engine,
        "SELECT f.id, p.subject_id, p.object_id FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id WHERE f.story_id = :s",
        s=story,
    )
    assert {(r.id, r.subject_id, r.object_id) for r in found} == {
        (f.id, person, truth[person]) for person, f in facts.items()
    }
    assert spies.canon_in_transition == 0
    assert set(spies.inserts) <= {"_transition"}
    # Divergences resolve: a reveal leaves canon's place, a lie its own.
    async with engine.connect() as conn:
        held = await partitions.partition(conn, story, ann)
    current = {b.proposition.subject_id: b.proposition.object_id for b in held}
    for person, (event, place) in last.items():
        assert current.get(person) == place, (person, event)
        assert (place == truth[person]) == (event == "reveal")
    # Every belief has its typed cause, and the projection is its fold.
    orphans = await rows(
        engine,
        "SELECT b.id FROM beliefs b LEFT JOIN belief_transitions t "
        "ON t.id = b.provenance AND t.belief_id = b.id WHERE b.story_id = :s AND t.id IS NULL",
        s=story,
    )
    assert orphans == []
    corrected = await rows(
        engine,
        "SELECT count(*) AS n FROM beliefs WHERE story_id = :s AND state = 'corrected' "
        "AND (held_to_turn IS NULL OR tx_to IS NULL)",
        s=story,
    )
    assert corrected[0].n == 0
    assert await ps.state_rows(engine, story) == await ps.folded_rows(engine, story)


@pytest.mark.soak
@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-10; FR-EPI-3")
async def test_x_02(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    await _play(engine, TURNS, monkeypatch)


@pytest.mark.integration
@pytest.mark.part("M5", "FR-BELIEF-10; FR-EPI-3")
async def test_x_02__pr(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    await _play(engine, PR_TURNS, monkeypatch)
