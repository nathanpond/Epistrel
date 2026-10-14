"""U-27: facts from the scenario, personality from the character (docs/04 §3), its M3 part.

Every U-27 (M3) part lives in this file (traceability counts a test ID per milestone in one
file). #122's ``test_u_27__lite`` covers the merge rule under Lite (FR-SCN-7) and the
character's own definition in the prompt (FR-CAT-2); the renderer's details are in
``tests/test_genesis_render.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
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
