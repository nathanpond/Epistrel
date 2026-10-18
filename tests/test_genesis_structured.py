"""Structured genesis's plan (FR-SCN-9,11, FR-PROP-12; #149), pure: what a creation of the Sunday
Dinner would write, decided from its ``genesis_deferred`` items and the cast alone (no database,
no model). The end-to-end part is ``tests/integration/test_i_27_m4.py``."""

from __future__ import annotations

from typing import Any

from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.enums import MemoryKind
from epistrel.core.genesis import structured
from epistrel.core.genesis.materialize import CastRole, materialize
from epistrel.core.ids import new_ulid
from epistrel.core.resolution.entities import character_spec
from tests import scenario_support as scenarios
from tests import structured_genesis_support as sg

SCENARIO = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
STORY = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C4"
NAMES = {"mother": "June Harrow", "brother": "Tom Harrow", "aunt": "Ivy Harrow", "player": "Sam"}


def _plan(body: dict[str, Any], **options: Any) -> tuple[structured.Plan, dict[str, str]]:
    draft = ScenarioDraft.model_validate(body)
    ids = {role: new_ulid() for role in NAMES}
    cast = [CastRole(role, ids[role], is_player=role == "player") for role in NAMES]
    found = materialize(SCENARIO, 1, body, draft, cast)
    inputs = [
        structured.CastInput(
            character_id=ids[role],
            role_id=role,
            name=name,
            true_age=40,
            spec=character_spec(ids[role], name),
        )
        for role, name in NAMES.items()
    ]
    return structured.plan(SCENARIO, 1, STORY, found.deferred, inputs, **options), ids


def _body() -> dict[str, Any]:
    return sg.structured_dinner(scenarios.LOCKED_CHARACTER, scenarios.DEFAULT_CHARACTER)


def test_visibility_follows_the_audience() -> None:
    """A seed evident to anyone (the aunt's ``perceivable`` one) gets a memory; the mother's
    secret one gets none and stays deferred; a canon seed known to the brother and his
    acquaintances is a fact scoped to him and the roles a relationship links him to, with no
    memory."""
    body = _body()
    body["cast"]["roles"][1]["seeded_memories"].append(
        {
            "text": "Tom Harrow keeps the deed.",
            "holder": "brother",
            "audience": ["self", "acquaintances"],
            "disclosure": "guarded",
            "kind": "canon",
        }
    )
    plan, ids = _plan(body)
    texts = {m.text: m for m in plan.memories}
    assert texts[sg.AUNT_PROSE].visibility.scope == "public"
    assert texts[sg.WORLD_PROSE].visibility.scope == "objective"
    assert texts[sg.WORLD_CANON].kind is MemoryKind.SEMANTIC
    entries = sg.by_field({"materialized": [e.model_dump() for e in plan.entries]})
    assert entries["cast.roles[0].seeded_memories[0]"]["outcomes"] == ["deferred"]
    scoped = [f for f in plan.facts if f.visibility.scope == "characters"]
    assert len(scoped) == 1
    assert set(scoped[0].visibility.characters or ()) == {
        ids["brother"],
        ids["mother"],
        ids["player"],
    }
    assert entries["cast.roles[1].seeded_memories[2]"]["outcomes"] == ["deferred", "canon"]


def test_without_episodic_memory_the_memories_wait() -> None:
    """With the ``episodic`` module off no memory is planned: each seed memory becomes a
    deferred item under ``episodic``, and the seeds' facts are still written."""
    plan, _ = _plan(_body(), episodic=False)
    assert plan.memories == []
    assert {d.capability_needed for d in plan.deferred} == {structured.EPISODIC}
    assert len(plan.deferred) == 3
    assert "starting_state.seeded_memories[1]" in {f.key for f in plan.facts}


def test_an_unresolved_name_in_a_canon_seed_refuses() -> None:
    """A canon seed naming someone the cast and the world lack cannot be canon: the plan carries
    ``seed_unnormalizable`` at the seed's text (the creation refuses it)."""
    body = _body()
    body["starting_state"]["seeded_memories"].append(
        sg.public_seed("June Harrow trusts Lin with the deed.")
    )
    plan, _ = _plan(body)
    assert [(e.code, e.location.field, e.source) for e in plan.errors] == [
        ("seed_unnormalizable", "starting_state.seeded_memories[2].text", "unknown_name")
    ]


def test_a_closed_set_wins_and_an_unknown_reference_refuses() -> None:
    """The author's closed set "the player has no siblings" sits above the world's seeds: the
    brother's ``sibling_of`` relationship loses to it (rule ``closed_set``). A closed set naming
    a reference that is neither a role nor a world entity refuses."""
    body = _body()
    body["starting_state"]["closed_sets"] = [
        {"predicate": "sibling_of", "subject": "player", "members": ["aunt"]}
    ]
    plan, _ = _plan(body)
    entries = sg.by_field({"materialized": [e.model_dump() for e in plan.entries]})
    lost = entries["cast.roles[1].relationships[1]"]
    assert (lost["outcomes"], lost["rule"]) == (["merge_conflict"], "closed_set")
    assert plan.references["player"]
    body["starting_state"]["closed_sets"][0]["members"] = ["cousin"]
    plan, _ = _plan(body)
    assert [(e.code, e.location.field) for e in plan.errors] == [
        ("seed_unnormalizable", "starting_state.closed_sets[0]")
    ]


def test_a_core_name_or_an_unknown_predicate_refuses() -> None:
    """An author predicate named like a core one, or a rule naming no declared or core predicate,
    is ``seed_unnormalizable`` at its field, and nothing of the rules is registered."""
    body = _body()
    body["starting_state"]["predicates"] = [{"name": "owns"}]
    plan, _ = _plan(body)
    assert [(e.code, e.location.field, e.source) for e in plan.errors] == [
        ("seed_unnormalizable", "starting_state.predicates[0].name", "core_predicate_name")
    ]
    assert plan.rules.empty
    body = _body()
    body["starting_state"]["rules"] = [{"when": "inherits", "then": "bequeaths"}]
    plan, _ = _plan(body)
    assert [(e.code, e.location.field, e.source) for e in plan.errors] == [
        ("seed_unnormalizable", "starting_state.rules[0].then", "unknown_predicate")
    ]


def test_names_and_ages_are_facts() -> None:
    """Each character's name is a ``public`` fact and its true age an ``engine`` one."""
    plan, ids = _plan(_body())
    by_key = {f.key: f for f in plan.facts}
    assert by_key["name:mother"].visibility.scope == "public"
    assert by_key["name:mother"].proposition.subject_id == ids["mother"]
    assert by_key["age:mother"].visibility.scope == "engine"
    # The world's two places and its deed, and the two roles' name overrides.
    assert structured.by_outcome(plan.entries)["entity"] == 5
