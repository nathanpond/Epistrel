"""U-19 (M5): resolution obligations, the knowledge half (FR-COMMIT-14; docs/04 §3; #165). One
file per test ID and milestone: U-19's M4 part is ``tests/test_u_19.py``.

The porch story of #154 (``tests.resolution_support``) under per-character partitions. Binding the
knock to Jenni, a modeled character with no recorded position, derives the knowledge the binding
obliges her to have held from the knock on: she knocked, and she was at the porch then (the
binding and its implied obligation, at ``known``, held from the thunk's introduction). The conflict
search then runs over her later sincere lines:

- a later sincere "who was at the porch earlier?" (a wh-question about the knock) conflicts, and
  so does a later sincere line declaring she was at the school at the knock's time (another value
  of ``located_at``'s functional key). Either is a ``Conflict`` citing the line's event, and
  ``depend`` rejects it with nothing written: the thunk stays open;
- complement: a deceptive question, a joking one, a statement about something else and another
  character's question do not conflict. A binding to the player or to an unmodeled person obliges
  no knowledge;
- a non-conflicting binding commits with her knowledge: one ``resolution`` transition per
  proposition (antecedent the thunk, held from its introduction, story-time the knock's), and her
  backdated memory of the act (``provenance = resolution``, her own).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import timeline
from epistrel.core.commit import lifecycle
from epistrel.core.commit.reconcile import Conflict, Ok, introduced_at, reconcile
from epistrel.core.enums import BeliefConfidenceLevel as Level
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.epistemic import replay, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Interval, Proposition
from tests import commit_support as c
from tests import partition_support as parts
from tests import resolution_support as r
from tests.canon_support import PRINCIPAL
from tests.event_support import rows
from tests.testimony_support import enable_testimony

pytestmark = pytest.mark.integration


async def line(
    engine: AsyncEngine,
    story_id: str,
    speaker: str,
    said: str,
    *,
    kind: str = "say",
    intent: str = "sincere",
    propositions: tuple[Proposition, ...] = (),
) -> str:
    """A turn in which ``speaker``'s beat 2 is ``said`` (a ``say`` by default); its event id."""
    payload: dict[str, Any] = {
        "text": said,
        "propositions": [{"proposition": p.model_dump(mode="json")} for p in propositions],
    }
    if kind == "say":
        payload["intent"] = intent
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER, beat=1)
        op.append(f"beat_{kind}", payload, character_id=speaker, beat=2)
        op.append("message_rendered", {"text": said})
        await op.commit()
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT id FROM events WHERE operation_id = :o AND type = :t"),
            {"o": op.operation_id, "t": f"beat_{kind}"},
        )
    return str(found)


async def _knock(engine: AsyncEngine) -> tuple[r.Porch, lifecycle.Thunk]:
    p = await r.porch_story(engine)
    await enable_testimony(engine, p.story_id, "defaulted")
    return p, await r.introduce_knock(engine, p)


async def _reconcile(engine: AsyncEngine, p: r.Porch, thunk: lifecycle.Thunk, who: str) -> Any:
    binding = lifecycle.binding_of(thunk, r.knocked(p, who, r.KNOCK_AT + 300))
    async with engine.connect() as conn:
        return await reconcile(conn, p.story_id, thunk, binding)


async def _bind(engine: AsyncEngine, p: r.Porch, thunk: lifecycle.Thunk, who: str) -> Any:
    async def step(conn: AsyncConnection, op: Operation) -> lifecycle.Dependence:
        return await lifecycle.depend(
            conn, op, thunk.id, r.knocked(p, who, r.KNOCK_AT + 300), beat=2, channel="do"
        )

    return await c.in_turn(engine, p.story_id, step)


@pytest.mark.part("M5", "FR-COMMIT-14")
async def test_u_19__knowledge(engine: AsyncEngine) -> None:
    """See the module docstring."""
    p, thunk = await _knock(engine)
    k = p.knock

    # The knowledge a binding to Jenni obliges, from the knock on.
    found = await _reconcile(engine, p, thunk, k.jenni)
    assert isinstance(found, Ok)
    assert found.knowledge is not None
    assert found.knowledge.holder == k.jenni
    assert [(x.subject_id, x.predicate_id, x.object_id) for x in found.knowledge.propositions] == [
        (k.jenni, k.knocked_at, p.porch),
        (k.jenni, r.LOCATED_AT, p.porch),
    ]
    assert found.knowledge.held_from == introduced_at(thunk)
    assert found.knowledge.story_time == r.KNOCK_AT

    # Lines that do not conflict: a deceptive and a joking question, a statement about
    # something else, and another character's sincere question.
    await line(engine, p.story_id, k.jenni, "Who was at the porch earlier?", intent="deceptive")
    await line(engine, p.story_id, k.jenni, "Who was at the porch, a ghost?", intent="joking")
    await line(engine, p.story_id, k.jenni, "The porch light is out.")
    await line(engine, p.story_id, k.bobby, "Jenni, who was at the porch earlier?")
    assert isinstance(await _reconcile(engine, p, thunk, k.jenni), Ok)

    # Her later sincere question conflicts with knowing she knocked.
    asked = await line(engine, p.story_id, k.jenni, "Cal, who was at the porch earlier?")
    conflict = await _reconcile(engine, p, thunk, k.jenni)
    assert isinstance(conflict, Conflict)
    assert conflict.conflicting_lines == (asked,)
    assert conflict.conflicting_records == ()
    assert conflict.bound_entity == k.jenni
    rejected = await _bind(engine, p, thunk, k.jenni)
    assert rejected.kind == "rejected"
    assert rejected.fact is None
    assert rejected.knowledge == ()
    assert (await c.thunk(engine, p.story_id, thunk.id)).open
    assert await rows(engine, "SELECT id FROM beliefs WHERE story_id = :s", s=p.story_id) == []

    # Complement: the player and an unmodeled person hold no partition, so no knowledge.
    for who in (k.cal, k.mailman):
        free = await _reconcile(engine, p, thunk, who)
        assert isinstance(free, Ok)
        assert free.knowledge is None


@pytest.mark.part("M5", "FR-COMMIT-14")
async def test_u_19__knowledge_positions(engine: AsyncEngine, drained_embedding_jobs: None) -> None:
    """A later sincere line declaring Jenni at the school at the knock's time contradicts the
    knowledge that she was at the porch then (``located_at`` is functional): a ``Conflict``
    citing the line. Complement: a binding with no such line commits with her knowledge (her
    ``resolution`` transitions, held from the thunk's introduction at the knock's story-time,
    citing the thunk) and her backdated memory of knocking."""
    p, thunk = await _knock(engine)
    k = p.knock
    at_school = Proposition(
        subject_id=k.jenni,
        predicate_id=r.LOCATED_AT,
        object_id=p.school,
        valid_time=Interval.of(r.KNOCK_AT - 30, r.KNOCK_AT + 30),
    )
    said = await line(
        engine, p.story_id, k.jenni, "I was at the school all evening.", propositions=(at_school,)
    )
    conflict = await _reconcile(engine, p, thunk, k.jenni)
    assert isinstance(conflict, Conflict)
    assert conflict.conflicting_lines == (said,)

    p, thunk = await _knock(engine)
    k = p.knock
    committed = await _bind(engine, p, thunk, k.jenni)
    assert committed.kind == "committed"
    assert len(committed.knowledge) == 2
    transitions = await rows(
        engine,
        "SELECT t.kind, t.antecedent_kind, t.antecedent_ref, t.valid_at, t.confidence_level, "
        "b.held_from_turn, b.held_from_beat, b.holder_character_id, b.provenance = t.id AS caused "
        "FROM belief_transitions t JOIN beliefs b ON b.id = t.belief_id "
        "WHERE t.story_id = :s ORDER BY t.id",
        s=p.story_id,
    )
    assert [str(t.kind) for t in transitions] == ["resolution", "resolution"]
    for t in transitions:
        assert str(t.antecedent_kind) == "resolution"
        assert t.antecedent_ref == {"thunk_id": thunk.id}
        assert (t.valid_at, str(t.confidence_level)) == (r.KNOCK_AT, "known")
        assert (t.held_from_turn, t.held_from_beat) == (thunk.narrative_turn, thunk.beat)
        assert (t.holder_character_id, t.caused) == (k.jenni, True)
    (memory,) = await rows(
        engine,
        "SELECT * FROM memories WHERE story_id = :s AND provenance = 'resolution'",
        s=p.story_id,
    )
    assert committed.memories == (memory.id,)
    assert memory.holder_character_id == k.jenni
    assert memory.visibility == {"scope": "characters", "characters": [k.jenni]}
    assert memory.text == "Jenni knocked at Jenni's porch."
    assert (memory.narrative_turn, memory.beat) == (thunk.narrative_turn, thunk.beat)


async def _knocked_rows(engine: AsyncEngine, p: r.Porch, holder: str) -> list[Any]:
    """``holder``'s first-order beliefs that someone knocked at the porch, active, by
    ``held_from``."""
    return await rows(
        engine,
        "SELECT b.id, b.polarity, b.confidence_level, b.tx_to, b.held_from_turn, b.held_from_beat, "
        "b.held_from_step, b.held_to_turn, b.held_to_beat, b.held_to_step, b.lineage_id "
        "FROM beliefs b JOIN propositions q ON q.id = b.proposition_id "
        "WHERE b.story_id = :s AND b.holder_character_id = :h AND q.predicate_id = :p "
        "AND b.kind = 'first_order' AND b.membership = 'active' "
        "ORDER BY b.held_from_turn, b.held_from_beat, b.held_from_step",
        s=p.story_id,
        h=holder,
        p=p.knock.knocked_at,
    )


@pytest.mark.part("M5", "FR-COMMIT-14")
async def test_u_19__knowledge_backdated_hold(
    engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#547: Jenni already believes she knocked, from turn 2 (an author act), when turn 3 binds
    the knock to her. The obligation still holds the act from the thunk's introduction: the
    held belief is raised in place to ``known`` and the span from the knock to turn 2 is a
    backdated row of its own (a second ``resolution`` transition citing the thunk), closed where
    the held row starts, so she holds it from the knock with no gap and one open row. Truncating
    the binding turn takes the backdated row away and reverts the raise. Complement: a belief
    contradicting the act held inside that span (she believed she had not knocked) leaves the
    raise alone, with no backdated row."""
    p, thunk = await _knock(engine)
    k = p.knock
    act = r.knocked(p, k.jenni, r.KNOCK_AT)
    await parts.transit(
        engine, p.story_id, k.jenni, act, confidence=transitions.Confidence(Level.BELIEVED)
    )
    (held,) = await _knocked_rows(engine, p, k.jenni)
    assert (held.held_from_turn, str(held.confidence_level)) == (2, "believed")

    committed = await _bind(engine, p, thunk, k.jenni)
    assert committed.kind == "committed"
    early, later = await _knocked_rows(engine, p, k.jenni)
    assert later.id == held.id
    assert (str(later.confidence_level), later.held_to_turn, later.tx_to) == ("known", None, None)
    assert (early.held_from_turn, early.held_from_beat) == (thunk.narrative_turn, thunk.beat)
    assert (early.held_to_turn, early.held_to_beat, early.held_to_step) == (
        later.held_from_turn,
        later.held_from_beat,
        later.held_from_step,
    )
    assert (str(early.polarity), str(early.confidence_level)) == ("affirmed", "known")
    assert early.tx_to is None
    assert early.lineage_id == later.lineage_id
    resolutions = await rows(
        engine,
        "SELECT belief_id, antecedent_ref FROM belief_transitions WHERE story_id = :s "
        "AND kind = 'resolution' AND membership = 'active'",
        s=p.story_id,
    )
    assert {t.antecedent_ref["thunk_id"] for t in resolutions} == {thunk.id}
    assert {early.id, later.id} <= {t.belief_id for t in resolutions}
    async with engine.connect() as conn:
        known = await replay.could_have_known(
            conn, p.story_id, k.jenni, act, Position.end_of(thunk.narrative_turn or 0)
        )
    assert isinstance(known, replay.Known), known
    assert known.path.transition_kind == "resolution"

    # Truncating the binding turn takes the backdated hold away and reverts the raise.
    await timeline.truncate(
        engine, p.story_id, timeline.TruncateRequest(to_turn=2), principal=PRINCIPAL
    )
    (back,) = await _knocked_rows(engine, p, k.jenni)
    assert (back.id, str(back.confidence_level)) == (held.id, "believed")

    # Complement: she believed she had not knocked, from turn 2, until turn 3 told her she had.
    p, thunk = await _knock(engine)
    k = p.knock
    act = r.knocked(p, k.jenni, r.KNOCK_AT)
    await parts.transit(engine, p.story_id, k.jenni, act.negation())
    await parts.transit(engine, p.story_id, k.jenni, act, cause=transitions.Kind.CORRECTION)
    committed = await _bind(engine, p, thunk, k.jenni)
    assert committed.kind == "committed"
    affirmed = [b for b in await _knocked_rows(engine, p, k.jenni) if str(b.polarity) == "affirmed"]
    assert [(b.held_from_turn, b.held_to_turn) for b in affirmed] == [(3, None)]
