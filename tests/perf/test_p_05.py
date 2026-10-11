"""P-05: the model-call budget (docs/04), the M2 part (NFR-COST-1; #69): per-purpose per-turn
call counts are measured and gated. A Lite turn whose policies are all deterministic is exactly
one call, and a clean turn (one generation, no regeneration, retry or failure) makes exactly
``1 + G``, the generation plus the guardrail calls its budget reserved.

The measurement is the session's turn census (``tests/perf/census.py``): this test runs last
(``tests/conftest.py`` orders it) and gates every turn the scenario suite played, plus turns of
its own so the gate never passes empty."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.perf.census import CENSUS, CensusTurn
from tests.policy_support import AUTHOR, story_item, words
from tests.respond_support import FIRST, PLAYER, SECOND, body, new_story, played, respond_client

TURNS = 5
ASSISTED = {"type": "model_assisted", "prompt": "Is this text about OWLS?", "on_uncertain": "pass"}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _gated(turn: CensusTurn) -> None:
    """The per-turn gate: counted == recorded, within the cap, and a clean turn is 1 + G."""
    assert turn.used == turn.calls, turn
    assert turn.max_calls is None or turn.used <= turn.max_calls, turn
    if turn.clean:
        assert turn.calls == 1 + turn.reserved, turn
        assert turn.call_counts.get("guardrail_check", 0) == turn.reserved, turn
        if turn.reserved == 0:
            assert turn.call_counts == {"generation": 1}, turn


@pytest.mark.part("M2", "NFR-COST-1")
async def test_p_05(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Every turn of the session's census passes the per-turn gate; the turns of the S-tests
    are in it whenever they ran; and this test's own turns, with deterministic input and
    output policies over two render characters, are each exactly one ``generation`` call.

    Complement asserted: one model-assisted output policy makes each clean turn exactly two
    calls (``1 + G`` with G = 1), so the gate distinguishes the two."""
    mark = len(CENSUS.turns)
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client, PLAYER, FIRST, SECOND)
        await lib.policies_set_story(
            AUTHOR,
            story,
            [
                story_item(words("zzinput"), applies_to=["text_input"]),
                story_item(words("zzoutput")),
            ],
        )
        for index in range(TURNS):
            turn = await played(client, story, body(ids[PLAYER], f"Turn {index}."))
            assert turn["call_counts"] == {"generation": 1}

        assisted, assisted_ids = await new_story(client, PLAYER, FIRST)
        await lib.policies_set_story(AUTHOR, assisted, [story_item(ASSISTED)])
        for index in range(TURNS):
            turn = await played(client, assisted, body(assisted_ids[PLAYER], f"Turn {index}."))
            assert turn["call_counts"] == {"generation": 1, "guardrail_check": 1}

    own = CENSUS.turns[mark:]
    assert len(own) == 2 * TURNS
    assert [t.calls for t in own] == [1] * TURNS + [2] * TURNS
    for recorded in CENSUS.turns:
        _gated(recorded)

    s_turns = [t for t in CENSUS.turns if t.s_test]
    if CENSUS.s_tests_collected:
        assert s_turns, "the S-tests ran but no Lite turn was recorded"
        assert any(t.clean and t.reserved == 0 for t in s_turns)
