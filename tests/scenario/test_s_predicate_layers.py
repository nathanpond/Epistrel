"""S-PREDICATE-LAYERS: weakest semantics for unknown relations (docs/04 §7), its M4 part.

#131's slice covers the registry the scenario stands on: an undeclared relation from a render is
auto-registered with the weakest semantics, so a second apprenticeship can never contradict the
first on cardinality while a direct negation over overlapping time is still a polarity
conflict; a strengthening proposal is inert until the author approves it; the core predicates
carry their documented semantics; and author-declared predicates and rules take effect as
declared. #140 adds the verdicts on top (``novel``, ``contradicted``, the model residual).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import (
    Cardinality,
    Interval,
    Layer,
    Proposition,
    TemporalKind,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from tests.event_support import new_story, scalar


@pytest.mark.integration
@pytest.mark.part("M4", "FR-PROP-3,4,5")
async def test_s_predicate_layers__registry(engine: AsyncEngine) -> None:
    """ "Bobby apprenticed with the blacksmith": the relation is registered on first use as a
    weak ``multi`` predicate with no maximum, exclusions, implications or containment; a second
    apprenticeship with another smith raises no cardinality conflict, while "Bobby did not
    apprentice with the blacksmith" over overlapping time is a direct polarity conflict. The
    engine's ``functional`` proposal changes nothing until approved. Core predicates ship
    their documented semantics, and an author's predicates and rules take effect as declared.
    Complement: after the author approves, the same second apprenticeship can conflict."""
    story = await new_story(engine)
    async with engine.begin() as conn:
        bobby = await store.add_entity(conn, story, "person", "Bobby")
        smith = await store.add_entity(conn, story, "person", "the blacksmith")
        other = await store.add_entity(conn, story, "person", "the farrier")
        apprenticed = await registry.auto_register(conn, story, "apprenticed with")
        reg = await registry.load(conn, story)
        first = Proposition(
            subject_id=bobby,
            predicate_id=apprenticed.id,
            object_id=smith,
            valid_time=Interval.of(0, 1000),
        )
        await store.write_proposition(conn, story, first, reg)
        second = first.model_copy(update={"object_id": other})
        await store.write_proposition(conn, story, second, reg)
    # Weakest semantics: remembered, never contradicted on semantics it was not given.
    assert apprenticed.layer is Layer.AUTO
    assert apprenticed.cardinality is Cardinality.MULTI
    assert apprenticed.max_cardinality is None
    assert (apprenticed.exclusions, apprenticed.implications, apprenticed.containment_of) == (
        (),
        (),
        None,
    )
    assert apprenticed.weak
    assert not registry.cardinality_may_contradict(apprenticed)
    assert registry.direct_polarity_conflict(first, second) is False
    # A direct polarity conflict is logical, not semantic: it applies to a weak predicate too.
    denied = first.negation().model_copy(update={"valid_time": Interval.of(500, 600)})
    assert registry.direct_polarity_conflict(first, denied) is True
    assert (
        registry.direct_polarity_conflict(
            first, denied.model_copy(update={"valid_time": Interval.of(1000, 1100)})
        )
        is False
    )
    # A proposal to make it functional is inert until the author approves it.
    revision = await scalar(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story)
    async with engine.begin() as conn:
        await registry.file_proposal(
            conn,
            story,
            apprenticed.id,
            registry.ProposedChanges(cardinality=Cardinality.FUNCTIONAL),
        )
    pending = await registry.load(engine, story)
    assert pending.get(apprenticed.id) == apprenticed
    assert (
        await scalar(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story)
        == revision
    )
    decision = await registry.approve(engine, story, apprenticed.id, None, principal="p-author")
    approved = (await registry.load(engine, story)).get(apprenticed.id)
    assert approved is not None
    assert approved.cardinality is Cardinality.FUNCTIONAL
    assert registry.cardinality_may_contradict(approved)
    assert decision.config_revision == revision + 1
    (flagged,) = decision.violations
    assert flagged.subject_id == bobby
    # Core semantics, as documented: degree_awarded is functional per (person, degree id) with
    # a set-valued object; enrollment, membership and programs are multi; no implications.
    degree = CORE_BY_NAME["degree_awarded"]
    assert degree.cardinality is Cardinality.FUNCTIONAL
    assert degree.key_qualifiers == ("degree_id",)
    assert degree.set_object
    assert degree.temporal_kind is TemporalKind.EVENT
    for name in ("enrolled_at", "member_of", "attended_program"):
        assert CORE_BY_NAME[name].cardinality is Cardinality.MULTI, name
    for name in ("enrolled_at", "member_of", "attended_program", "degree_awarded"):
        assert CORE_BY_NAME[name].implications == (), name
    assert all(not p.implications for p in registry.core_registry().predicates.values())
    # The author's predicates and rules take effect as declared.
    declared = registry.ScenarioRules.parse(
        {
            "predicates": [{"name": "plays_for", "cardinality": "functional"}, {"name": "part_of"}],
            "rules": [{"when": "plays_for", "then": "enrolled_at", "via": "part_of"}],
        }
    )
    async with engine.begin() as conn:
        team = await store.add_entity(conn, story, "organization", "the varsity team")
        school = await store.add_entity(conn, story, "organization", "OSU")
        authored = await registry.register_author(conn, story, declared)
    plays_for, part_of = authored.named("plays_for"), authored.named("part_of")
    assert plays_for is not None
    assert part_of is not None
    assert plays_for.layer is Layer.AUTHOR
    assert plays_for.cardinality is Cardinality.FUNCTIONAL
    facts = [
        Proposition(
            subject_id=bobby,
            predicate_id=plays_for.id,
            object_id=team,
            valid_time=Interval.of(0, 50),
        ),
        Proposition(
            subject_id=team,
            predicate_id=part_of.id,
            object_id=school,
            valid_time=Interval.of(0, 90),
        ),
    ]
    (implied,) = registry.imply(facts, authored.rules()).derived
    assert implied.predicate_id == CORE_BY_NAME["enrolled_at"].id
    assert implied.object_id == school
    assert registry.imply(facts, registry.core_registry().rules()).derived == ()
