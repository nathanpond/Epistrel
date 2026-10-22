"""P-05: the model-call budget (docs/04), the M2 part (NFR-COST-1; #69): per-purpose per-turn
call counts are measured and gated. A Lite turn whose policies are all deterministic is exactly
one call, and a clean turn (one generation, no regeneration, retry or failure) makes exactly
``1 + G``, the generation plus the guardrail calls its budget reserved.

The measurement is the session's turn census (``tests/perf/census.py``): this test runs last
(``tests/conftest.py`` orders it) and gates every turn the scenario suite played, plus turns of
its own so the gate never passes empty.

The M4 part (NFR-PERF-5; #155): ``test_p_05__escalation`` plays structured turns on the stub
with a scripted escalation rate and reads it back from ``GET /admin/evals/escalation-yield``,
computed from the turns' ``turn_report.verify`` and reported with the 0.2 reference baseline,
never as a gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.escalation_support import (
    camp_render,
    camp_story,
    clear_gates,
    database_now,
    entailment,
    play_camp,
    regenerated,
    yield_report,
)
from tests.fixtures.bobby_education import bobby_education
from tests.perf.census import CENSUS, CensusTurn
from tests.policy_support import AUTHOR, story_item, words
from tests.respond_support import FIRST, PLAYER, SECOND, body, new_story, played, respond_client
from tests.structured_turn_support import turn_report

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


@pytest.mark.part("M4", "NFR-PERF-5")
async def test_p_05__escalation(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Two structured turns whose camp claim is ``uncertain(world_knowledge)`` (each escalated
    once) and one whose rule decides it deterministically (none escalated): the report's rate
    is the turns' escalated claims over their verified claims, as their reports count them, and
    it carries ``reference_baseline: 0.2`` and ``is_gate: false``.

    Complement asserted: the deterministic turn verified claims and escalated none, so the
    scripted rate is below 1 and the denominator is every verified claim, not every escalation."""
    await clear_gates(engine)
    started = await database_now(engine)
    reports = []
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for escalates in (True, True, False):
            stub.reset()
            edu = (
                await camp_story(client, engine)
                if escalates
                else await bobby_education(
                    client, engine, never_in_florida=True, presence_rule=True
                )
            )
            stub.script("generation", [camp_render()])
            if escalates:
                stub.script("entailment", [entailment("contradicted")])
            stub.script("regeneration", [regenerated()])
            await play_camp(client, edu)
            reports.append(await turn_report(engine, edu.story_id))
        found = await yield_report(
            client,
            **{
                "from": started.isoformat(),
                "to": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            },
        )
    claims = sum(r["verify"]["claims"] for r in reports)
    escalated = sum(r["verify"]["escalated"] for r in reports)
    assert [r["verify"]["escalated"] for r in reports] == [1, 1, 0]
    assert reports[2]["verify"]["claims"] >= 1
    assert (found["claims"], found["escalated"]) == (claims, escalated)
    assert found["escalation_rate"] == round(escalated / claims, 4)
    assert 0 < found["escalation_rate"] < 1
    assert (found["reference_baseline"], found["is_gate"]) == (0.2, False)
