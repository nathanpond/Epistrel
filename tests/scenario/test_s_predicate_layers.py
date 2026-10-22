"""S-PREDICATE-LAYERS: weakest semantics for unknown relations (docs/04 §7), its M4 part.

#131's slice covers the registry the scenario stands on: an undeclared relation from a render is
auto-registered with the weakest semantics, so a second apprenticeship can never contradict the
first on cardinality while a direct negation over overlapping time is still a polarity
conflict; a strengthening proposal is inert until the author approves it; the core predicates
carry their documented semantics; and author-declared predicates and rules take effect as
declared. #140 adds the verdicts on top (``novel``, ``contradicted``, the model residual).
#491 adds where the proposal comes from: structured turns on the stub file it once the relation
has been used single-valued often enough, and the author approves it over REST.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import (
    Cardinality,
    Interval,
    Layer,
    Predicate,
    Proposition,
    TemporalKind,
)
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from tests.auth_support import auth_settings
from tests.event_support import new_story, rows, scalar
from tests.respond_support import body, played, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, render, structured_story


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


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _apprentice_story(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> tuple[str, dict[str, str]]:
    """A structured story with Wren (the player), Ada, and two smiths Ada could name."""
    story, ids = await structured_story(client, "Wren", "Ada")
    async with engine.begin() as conn:
        await store.add_entity(conn, story, "person", "the blacksmith")
        await store.add_entity(conn, story, "person", "the farrier")
    return story, ids


async def _apprenticed(
    client: httpx.AsyncClient, story: str, ids: dict[str, str], stub: Any, master: str
) -> None:
    """One structured turn: Ada's render declares that she apprenticed with ``master``."""
    stub.script(
        "generation",
        [
            render(
                beat(
                    "say",
                    f"I apprenticed with {master}.",
                    intent="sincere",
                    propositions=[claim("I", "apprenticed with", master)],
                )
            )
        ],
    )
    await played(client, story, body(ids["Wren"], "Who taught you?"))


async def _predicate(engine: AsyncEngine, story: str) -> Predicate:
    found = (await registry.load(engine, story)).named("apprenticed_with")
    assert found is not None  # the render's relation was auto-registered
    return found


async def _proposals(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT id, predicate_id, status, proposed, filed_by FROM predicate_proposals "
        "WHERE story_id = :s ORDER BY id",
        s=story,
    )


async def _observations(engine: AsyncEngine, story: str, predicate_id: str) -> int | None:
    async with engine.connect() as conn:
        return await registry.single_valued_observations(conn, story, predicate_id)


async def _decide(
    client: httpx.AsyncClient, story: str, predicate_id: str, action: str
) -> httpx.Response:
    return await client.post(
        f"/stories/{story}/predicates/{predicate_id}/{action}", json={}, headers=WRITE
    )


@pytest.mark.part("M4", "FR-PROP-4")
async def test_s_predicate_layers__proposal_from_turns(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Ada says, turn after turn, that she apprenticed with the blacksmith. The relation is
    auto-registered weak. Once ``PROPOSAL_OBSERVATIONS`` of its affirmed propositions are
    single-valued, that turn's commit files one ``functional`` proposal, by the engine and
    inert (the predicate stays weak), and ``POST .../approve`` applies it (200). Complements:
    before the threshold there is no proposal (approve is 404); a story where she names two
    different masters never gets one; a rejected proposal is not filed again by a later
    turn."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        story, ids = await _apprentice_story(client, engine)
        await _apprenticed(client, story, ids, stub, "the blacksmith")
        apprenticed = await _predicate(engine, story)
        assert (apprenticed.layer, apprenticed.weak) == (Layer.AUTO, True)
        observed = await _observations(engine, story, apprenticed.id)
        assert observed is not None
        assert 0 < observed < registry.PROPOSAL_OBSERVATIONS
        # Below the threshold: nothing is filed, so there is nothing to approve.
        assert await _proposals(engine, story) == []
        early = await _decide(client, story, apprenticed.id, "approve")
        assert early.status_code == 404, early.text
        filed: list[Any] = []
        for _ in range(registry.PROPOSAL_OBSERVATIONS):
            await _apprenticed(client, story, ids, stub, "the blacksmith")
            observed = await _observations(engine, story, apprenticed.id)
            assert observed is not None
            filed = await _proposals(engine, story)
            assert len(filed) == int(observed >= registry.PROPOSAL_OBSERVATIONS)
            if filed:
                break
        (proposal,) = filed
        assert (proposal.predicate_id, proposal.status, proposal.filed_by) == (
            apprenticed.id,
            "pending",
            registry.ENGINE_PRINCIPAL,
        )
        assert proposal.proposed["changes"] == {"cardinality": "functional"}
        assert await _predicate(engine, story) == apprenticed  # inert until approved
        listed = await client.get(f"/stories/{story}/predicates", headers=WRITE)
        assert listed.status_code == 200, listed.text
        (row,) = [p for p in listed.json()["predicates"] if p["id"] == apprenticed.id]
        assert row["pending_proposal"]["id"] == proposal.id
        # A later turn finds it pending and files no second one.
        await _apprenticed(client, story, ids, stub, "the blacksmith")
        assert [p.id for p in await _proposals(engine, story)] == [proposal.id]
        approved = await _decide(client, story, apprenticed.id, "approve")
        assert approved.status_code == 200, approved.text
        answer = approved.json()
        assert (answer["proposal_id"], answer["status"]) == (proposal.id, "approved")
        assert answer["predicate"]["cardinality"] == "functional"
        assert answer["violations"] == []
        assert (await _predicate(engine, story)).cardinality is Cardinality.FUNCTIONAL

        # Complement: two masters for one subject is not single-valued, so there is no
        # proposal however often the relation is used.
        story, ids = await _apprentice_story(client, engine)
        for n in range(registry.PROPOSAL_OBSERVATIONS + 1):
            master = "the farrier" if n == 1 else "the blacksmith"
            await _apprenticed(client, story, ids, stub, master)
        apprenticed = await _predicate(engine, story)
        assert await _observations(engine, story, apprenticed.id) is None
        assert await _proposals(engine, story) == []
        refused = await _decide(client, story, apprenticed.id, "approve")
        assert refused.status_code == 404, refused.text

        # Complement: a rejected proposal stays rejected; the next use does not re-file it.
        story, ids = await _apprentice_story(client, engine)
        for _ in range(registry.PROPOSAL_OBSERVATIONS):
            await _apprenticed(client, story, ids, stub, "the blacksmith")
        apprenticed = await _predicate(engine, story)
        (proposal,) = await _proposals(engine, story)
        rejected = await _decide(client, story, apprenticed.id, "reject")
        assert rejected.status_code == 200, rejected.text
        await _apprenticed(client, story, ids, stub, "the blacksmith")
        assert [(p.id, p.status) for p in await _proposals(engine, story)] == [
            (proposal.id, "rejected")
        ]
        assert (await _predicate(engine, story)).weak
