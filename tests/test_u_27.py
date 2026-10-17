"""U-27: facts from the scenario, personality from the character (docs/04 §3), its M3 and M4
parts.

Every U-27 part lives in this file (traceability counts a test ID per milestone in one file).
#122's ``test_u_27__lite`` (M3) covers the merge rule under Lite (FR-SCN-7) and the character's
own definition in the prompt (FR-CAT-2); the renderer's details are in
``tests/test_genesis_render.py``. #140's ``test_u_27__world_facts`` (M4) is the fact merge by
seed layer under the contradiction rules (FR-SCN-7, FR-PROP-10), over fixture seeds; #149 calls
the same ``merge_layers`` at structured genesis.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.verify.merge import SeedFact, SeedLayer, merge_layers
from epistrel.core.verify.rules import ClosedSetRecord
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests import verify_support as vs
from tests.auth_support import auth_settings
from tests.character_support import DISPOSITIONS
from tests.respond_support import body as body_of
from tests.respond_support import generation_calls, respond, respond_client, system_of

LIAR = "She is a habitual liar."
SISTER = "She has a sister in the city."
COLD = "She is cold to strangers."
ONLY_CHILD = "Linda grew up an only child."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _mother(**fields: Any) -> dict[str, Any]:
    return g.role(
        "mother",
        setup_description=f"Her children are home for the weekend. {LIAR}",
        seeded_memories=[
            scenarios.seed(SISTER, "mother", audience=["public"], disclosure="open"),
            scenarios.seed(COLD, "mother", audience=["public"], disclosure="open"),
        ],
        **fields,
    )


async def _character(client: Any, name: str, honesty: str, **definition: Any) -> str:
    return await g.catalog_character_id(
        client,
        name,
        age=55,
        definition={
            "dispositions": {**DISPOSITIONS, "honesty": {"setting": honesty}},
            **definition,
        },
    )


async def _prompt(client: Any, stub: Any, made: dict[str, Any]) -> str:
    by_role = {c["role_id"]: c["id"] for c in made["characters"]}
    stub.reset()
    answer = await respond(
        client,
        made["story_id"],
        body_of(by_role["player"], "Hello, Mum.", render_character_ids=[by_role["mother"]]),
    )
    assert answer.status_code == 200, answer.text
    (call,) = generation_calls(stub)
    return system_of(call)


@pytest.mark.integration
@pytest.mark.part("M3", "FR-SCN-7; FR-CAT-2")
async def test_u_27__lite(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The catalog Linda is very honest and grew up an only child; the scenario's open Mother
    role says "She is a habitual liar." and seeds "She has a sister in the city." and "She is
    cold to strangers.". Creating the story warns ``personality_in_role`` for the setup
    sentence with the role, the field, the exact text, outcome ``ignored``, the rule
    "personality: character over scenario" and Linda named as the character whose personality
    applies; one more warning names the seed that asserts personality. With ``strict`` the
    same creation is refused with ``creation_conflicts`` and the full report, and nothing is
    created. In the Lite prompt the role's seed "has a sister" and Linda's "only child" are
    both rendered, the scenario's fact first, with the instruction that scenario facts
    prevail; the seed asserting personality is followed by the sentence that it yields to
    Linda's own notes; the fact-merge check is listed unavailable and no conflict is counted.
    Two different characters in the same role put different dispositions into the prompt.

    Complements asserted: the setup sentence itself (a public-card field) is in no prompt; the
    refused creation wrote no row."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        linda = await _character(client, "Linda", "very_honest", background_flavor=ONLY_CHILD)
        rhea = await _character(client, "Rhea", "very_deceitful")
        body = g.scenario_body(_mother(), g.role("player", kind="player"))
        scenario_id, version = await g.published_scenario(client, body)
        request = g.from_scenario(
            scenario_id, slots={"mother": g.catalog_slot(linda)}, projection="full"
        )

        counted = await g.counts(engine)
        status, code, details = await g.refused(client, {**request, "strict": True})
        assert (status, code) == (422, "creation_conflicts")
        assert "personality_in_role" in [e["code"] for e in details["report"]["warnings"]]
        assert await g.counts(engine) == counted

        made = await g.create_from(client, request)
        report = made["report"]
        warnings = [e for e in report["warnings"] if e["code"] == "personality_in_role"]
        setup = next(e for e in warnings if e["location"]["field"].endswith("setup_description"))
        assert setup == {
            "code": "personality_in_role",
            "location": {
                "entry": scenario_id,
                "version": version,
                "role": "mother",
                "field": "cast.roles[0].setup_description",
                "seed_id": None,
            },
            "text": LIAR,
            "outcome": "ignored",
            "source": "Linda",
            "rule": "personality: character over scenario",
        }
        seeded = [e for e in warnings if e is not setup]
        assert [(e["text"], e["source"]) for e in seeded] == [(COLD, "Linda")]
        assert "fact_consistency" in report["unavailable"]
        assert report["counts"]["conflicts"] == 0

        system = await _prompt(client, stub, made)
        assert LIAR not in system
        sister = system.index(SISTER)
        prevail = system.index(lg.PREVAIL)
        assert sister < prevail < system.index(ONLY_CHILD)
        assert system.index(COLD) < system.index("Linda's own author notes decide") < prevail
        honest = "They are scrupulously honest."
        assert honest in system

        other = await g.create_from(
            client, {**request, "slots": {"mother": g.catalog_slot(rhea)}, "name": "Again"}
        )
        changed = await _prompt(client, stub, other)
        assert "They lie readily and often." in changed
        assert honest not in changed
        assert lg.section(system, "### Author notes: Linda", "## Your part") != lg.section(
            changed, "### Author notes: Rhea", "## Your part"
        )


LINDA = "01JA0000000000000000000101"
SISTER = "01JA0000000000000000000100"
"""The role's sister has the lower id, so ``sibling_of`` is stored with her as its subject: the
closed set on Linda must still apply (symmetric predicates match either direction)."""
FATHER = "01JA0000000000000000000102"
HOUSE = "01JA0000000000000000000103"


@pytest.mark.part("M4", "FR-SCN-7; FR-PROP-10")
def test_u_27__world_facts() -> None:
    """In a structured story the fact merge is decided by the contradiction rules, layer by
    layer (#140's ``merge_layers``, which #149 calls at genesis): Linda's background flavor
    "only child" (a closed set of no siblings) loses to the role's "has a sister" and is dropped
    entirely (rule ``closed_set``); the world's "the father is dead" overrides the role's "the
    father is alive" (rule ``exclusion``); both are reported with winner and loser. Complement:
    seeds that do not conflict are all kept; a conflict within one layer goes to document order;
    a pair the rules cannot decide keeps both and is reported ``merge_uncertain``."""
    registry = vs.REGISTRY
    world_dead = SeedFact(
        "seed-world-dead", SeedLayer.WORLD, vs.prop(FATHER, vs.DEAD, when=vs.span(0, None))
    )
    world_house = SeedFact(
        "seed-world-house", SeedLayer.WORLD, vs.prop(LINDA, vs.LOCATED_AT, HOUSE, vs.span(0, None))
    )
    role_sister = SeedFact(
        "seed-role-sister", SeedLayer.ROLE, vs.prop(LINDA, vs.SIBLING_OF, SISTER)
    )
    role_alive = SeedFact(
        "seed-role-alive", SeedLayer.ROLE, vs.prop(FATHER, vs.ALIVE, when=vs.span(0, None))
    )
    only_child = SeedFact(
        "seed-character-only-child",
        SeedLayer.CHARACTER,
        closed_set=ClosedSetRecord("seed-character-only-child", vs.SIBLING_OF, LINDA, ()),
    )
    result = merge_layers(
        {
            SeedLayer.CHARACTER: [only_child],
            SeedLayer.ROLE: [role_sister, role_alive],
            SeedLayer.WORLD: [world_dead, world_house],
        },
        registry=registry,
    )
    assert [s.seed_id for s in result.kept] == [
        "seed-world-dead",
        "seed-world-house",
        "seed-role-sister",
    ]
    decided = {(c.loser.seed_id, c.winner.seed_id, c.rule, c.outcome) for c in result.dropped}
    assert decided == {
        ("seed-role-alive", "seed-world-dead", "exclusion", "dropped"),
        ("seed-character-only-child", "seed-role-sister", "closed_set", "dropped"),
    }
    assert all(
        c.verdict is not None and c.verdict.kind.value == "contradicted" for c in result.dropped
    )
    assert result.uncertain == ()
    # Complement: without the conflicting seeds, everything is kept.
    calm = merge_layers(
        {SeedLayer.WORLD: [world_house], SeedLayer.ROLE: [role_sister]}, registry=registry
    )
    assert (len(calm.kept), calm.dropped, calm.uncertain) == (2, (), ())
    # Within one layer, document order wins.
    same = merge_layers(
        {SeedLayer.ROLE: [role_alive, replace(world_dead, layer=SeedLayer.ROLE)]}, registry=registry
    )
    ((conflict),) = same.dropped
    assert (conflict.winner.seed_id, conflict.loser.seed_id, conflict.rule) == (
        "seed-role-alive",
        "seed-world-dead",
        "same_layer_order",
    )
    # Two places no containment fact relates: world knowledge, so both stay and it is reported.
    elsewhere = SeedFact(
        "seed-role-elsewhere",
        SeedLayer.ROLE,
        vs.prop(LINDA, vs.LOCATED_AT, vs.OHIO, vs.span(0, None)),
    )
    doubtful = merge_layers(
        {SeedLayer.WORLD: [world_house], SeedLayer.ROLE: [elsewhere]}, registry=registry
    )
    assert len(doubtful.kept) == 2
    ((pair),) = doubtful.uncertain
    assert (pair.winner.seed_id, pair.loser.seed_id, pair.outcome) == (
        "seed-world-house",
        "seed-role-elsewhere",
        "merge_uncertain",
    )
