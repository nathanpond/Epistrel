"""U-27: facts from the scenario, personality from the character (docs/04 §3), its M5 part
(#173): the seed rules for beliefs, secrets, nested beliefs and audiences, on the pure plan of a
structured genesis with partitions (``core.genesis.structured.plan`` and its partitions branch,
``core.genesis.epistemic``) and the publish-time seed check.

The M3 and M4 parts live in ``tests/test_u_27.py`` (one file per test ID and milestone). The
scenario is S-FAMILY's Sunday Dinner of ``tests/seeded_partitions_support``; ``partitions`` and
``nested_beliefs`` are delivered for the test (#181 delivers them for real). That the seeded
beliefs hold from before the story (story minute -1) is the write's, asserted by I-27 M5.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.checks import seeds as seed_check
from epistrel.core.catalog.checks.base import Subject, ValidationContext
from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.config_changes import DeploymentState
from epistrel.core.genesis import structured
from epistrel.core.genesis.materialize import CastRole, materialize
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.resolution.entities import character_spec
from epistrel.core.seeds.profiles import structured_m4_settings
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.tables.ownership import EntityType
from tests import nested_support as ns
from tests import seeded_partitions_support as sp
from tests.profile_support import merged

ROLES = (*sp.NPC_ROLES, "neighbour", "player")
IDS = {role: f"01J9Z8Q4N5V6W7X8Y9Z0A1B{n:03d}" for n, role in enumerate(ROLES)}
NAMES = {**sp.NAMES, "neighbour": "Mr Pike", "player": "Sam"}


@pytest.fixture
def delivered() -> Iterator[None]:
    with ns.delivered():
        yield


def _settings() -> ProfileSettings:
    return ProfileSettings.model_validate(merged(structured_m4_settings(), sp.PARTITIONED))


def _plan(body: dict[str, Any], settings: Any = "partitioned") -> structured.Plan:
    """The structured plan of ``body`` with every role filled (Sam the player)."""
    draft = ScenarioDraft.model_validate(body)
    persona = {**sp.PERSONA, "gender": "male"}
    members = [
        CastRole(
            role,
            IDS[role],
            is_player=role == "player",
            persona=persona if role == "player" else None,
        )
        for role in ROLES
    ]
    found = materialize("01J9Z8Q4N5V6W7X8Y9Z0A1BSCN", 1, draft.document(), draft, members)
    inputs = [
        structured.CastInput(
            character_id=IDS[role],
            role_id=role,
            name=NAMES[role],
            true_age=22 if role == "player" else 50,
            spec=character_spec(IDS[role], NAMES[role]),
            background=(("description", sp.PERSONA["description"]),) if role == "player" else (),
            is_player=role == "player",
        )
        for role in ROLES
    ]
    opening = draft.starting_state.opening
    return structured.plan(
        "01J9Z8Q4N5V6W7X8Y9Z0A1BSCN",
        1,
        "01J9Z8Q4N5V6W7X8Y9Z0A1BSTY",
        found.deferred,
        inputs,
        settings=_settings() if settings == "partitioned" else settings,
        groups=draft.cast.groups,
        opening=(opening.narration, opening.roles_present),
    )


def _beliefs(found: structured.Plan) -> list[Any]:
    assert found.epistemic is not None
    return found.epistemic.beliefs


def _about(found: structured.Plan, holder: str, text_object: str) -> list[Any]:
    """The planned beliefs of ``holder`` whose proposition's object is the entity named
    ``text_object`` (an item of the world)."""
    entity = next(i for i, spec in found.entities if spec.canonical_name == text_object)
    return [
        b
        for b in _beliefs(found)
        if b.holder == IDS[holder]
        and b.proposition is not None
        and b.proposition.object_id == entity
    ]


def _fact(found: structured.Plan, text_object: str) -> Any:
    entity = next(i for i, spec in found.entities if spec.canonical_name == text_object)
    (fact,) = [f for f in found.facts if f.proposition.object_id == entity]
    return fact


def _entry(found: structured.Plan, field_ref: str) -> Any:
    (entry,) = [e for e in found.entries if e.field_ref == field_ref]
    return entry


async def _checked(body: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """``(code, field, outcome)`` of the publish-time seed check of ``body``."""
    state = DeploymentState(
        config_revision=0, default_content_rating_id=EVERYONE_ID, minor_age_threshold=18
    )
    context = ValidationContext(
        conn=cast(AsyncConnection, None), settings=state.as_settings(), capabilities=frozenset()
    )
    subject = Subject(EntityType.SCENARIO, "scn", "draft", body, ScenarioDraft)
    found = await seed_check.CHECK.run(subject, context)
    return [(e.code, e.location.field, e.outcome.value) for e in found.entries]


@pytest.mark.part("M5", "FR-SCN-8")
async def test_u_27__beliefs(delivered: None) -> None:
    """**Beliefs.** The Brother's seeded false belief that the house is not being sold is kept
    with no conflict (a belief, not canon); the world tells the Mother her husband keeps the
    ring while her own seed says he does not → the world's wins and the Mother's is reported
    ``merge_conflict`` (rule ``polarity``); two contradicting belief seeds of one layer → the
    later is reported ``belief_conflict`` (document order). **Secrets.** The world's secret to
    the Father plus the Mother's own seed of the same fact → one fact, both holders, each
    belief ``secret``; the world seeding the recipe book public while the Father's role seeds
    it secret → the world's audience wins, reported ``visibility_conflict``, and the Father's
    audience naming the player is reported ``seed_audience_unfilled``. **Nested.** The
    Sister's belief that the Brother knows about the sale is kept although he does not.
    **Seed attributes.** Every seeded item's author act carries its holder, audience and
    disclosure; ``acquaintances`` reaches exactly the roles the relationships and the family
    group say know the Sister (not the neighbour, never the player); the persona's name, age
    and traits are held by the family only. **The publish check** refuses an unknown audience
    token and a world-held belief, and warns on ``acquaintances`` for a role nobody knows.

    Complements: without partitions no belief is planned and the belief seeds stay deferred; a
    belief seed that does not normalize refuses the creation."""
    found = _plan(sp.family())
    assert found.errors == []

    # -- the Brother's false belief: kept, no conflict, no canon of its own
    (denied,) = _about(found, "brother", "The sale contract")
    assert (denied.proposition.polarity.value, denied.level.value, denied.fact_key) == (
        "negated",
        "believed",
        None,
    )
    assert _entry(found, "cast.roles[2].seeded_memories[0]").outcomes == ["belief", "memory"]

    # -- the Mother's opposite beliefs: the world wins
    (ring,) = _about(found, "mother", "The ring")
    assert ring.proposition.polarity.value == "affirmed"
    lost = _entry(found, "cast.roles[0].seeded_memories[2]")
    assert "merge_conflict" in lost.outcomes
    assert (lost.winner, lost.loser, lost.rule) == (
        "starting_state.seeded_memories[1]",
        "cast.roles[0].seeded_memories[2]",
        "polarity",
    )

    # -- same layer: document order wins, reported belief_conflict
    body = sp.family()
    body["cast"]["roles"][2]["seeded_memories"] = [
        sp.seed(sp.RING, "brother", ["self"], kind="belief"),
        sp.seed(sp.RING_DENIED, "brother", ["self"], kind="belief"),
    ]
    same = _plan(body)
    (kept,) = _about(same, "brother", "The ring")
    assert kept.proposition.polarity.value == "affirmed"
    order = _entry(same, "cast.roles[2].seeded_memories[1]")
    assert "belief_conflict" in order.outcomes
    assert order.rule == "same_layer_order"

    # -- a secret of the world and of a role: one fact, both holders
    sale = _fact(found, "The sale contract")
    assert sale.visibility.characters == tuple(sorted((IDS["father"], IDS["mother"])))
    assert {b.disclosure for b in _about(found, "father", "The sale contract")} == {"secret"}
    affirmed = [
        b
        for b in _about(found, "mother", "The sale contract")
        if b.proposition.polarity.value == "affirmed"
    ]
    assert [b.disclosure for b in affirmed] == ["secret"]

    # -- public in the world, secret in a role: the world's audience wins
    recipes = _fact(found, "The recipe book")
    assert recipes.visibility.scope == "objective"
    conflict = _entry(found, "cast.roles[1].seeded_memories[0]")
    assert {"visibility_conflict", "seed_audience_unfilled"} <= set(conflict.outcomes)
    assert conflict.winner == "starting_state.seeded_memories[2]"
    assert _about(found, "father", "The recipe book") == []

    # -- the Sister's nested belief, although the Brother does not hold it
    nested = [b for b in _about(found, "sister", "The sale contract") if b.attributed]
    assert [
        (b.attributed, b.attributed_level.value, b.proposition.polarity.value) for b in nested
    ] == [(IDS["brother"], "known", "affirmed")]
    assert _entry(found, "cast.roles[3].seeded_memories[1]").outcomes == ["nested", "memory"]

    # -- seed attributes: every act's holder, audience and disclosure
    assert found.epistemic is not None
    acts = {a.field_ref: a for a in found.epistemic.acts}
    world = acts["starting_state.seeded_memories[0]"]
    assert (world.holder, world.audience, world.disclosure) == (None, ("father",), "secret")
    sister = acts["cast.roles[3].seeded_memories[0]"]
    assert (sister.holder, sister.audience, sister.disclosure) == (
        IDS["sister"],
        ("self", "acquaintances"),
        "guarded",
    )
    assert {b.act for b in _beliefs(found)} <= set(acts)

    # -- acquaintances: the family, not the neighbour, never the player
    holders = {b.holder for b in _about(found, "sister", "The spare key")}
    for role in ("mother", "father", "brother"):
        holders |= {b.holder for b in _about(found, role, "The spare key")}
    assert holders == {IDS[r] for r in sp.NPC_ROLES}
    sams = {b.holder for b in _beliefs(found) if b.proposition is None or b.subject}
    assert sams == {IDS[r] for r in sp.NPC_ROLES}
    assert not [b for b in _beliefs(found) if b.holder in (IDS["neighbour"], IDS["player"])]

    # -- the publish check
    bad = sp.family()
    bad["cast"]["roles"][0]["seeded_memories"][0]["audience"] = ["self", "the crew"]
    world_belief = sp.seed(sp.RING, "world", ["public"], kind="belief")
    bad["starting_state"]["seeded_memories"].append(world_belief)
    bad["cast"]["roles"][4]["seeded_memories"] = [sp.seed(sp.KEY, "neighbour", ["acquaintances"])]
    checked = await _checked(bad)
    role_seed, world_seed = "cast.roles[0].seeded_memories[0]", "starting_state.seeded_memories[3]"
    lonely = "cast.roles[4].seeded_memories[0].audience[0]"
    assert ("seed_audience_unknown", f"{role_seed}.audience[1]", "rejected") in checked
    assert ("seed_holder_required", f"{world_seed}.holder", "rejected") in checked
    assert ("seed_audience_ignored", lonely, "ignored") in checked
    assert [c for c in await _checked(sp.family()) if c[2] == "rejected"] == []

    # -- complements: no partitions, no beliefs; an unreadable belief seed refuses
    shared = _plan(sp.family(), settings=None)
    assert shared.epistemic is None
    assert "deferred" in _entry(shared, "cast.roles[2].seeded_memories[0]").outcomes
    unreadable = copy.deepcopy(sp.family())
    unreadable["cast"]["roles"][2]["seeded_memories"][0]["text"] = "He hums while he works."
    (error,) = _plan(unreadable).errors
    assert (error.code, error.location.field) == (
        "seed_unnormalizable",
        "cast.roles[2].seeded_memories[0].text",
    )
