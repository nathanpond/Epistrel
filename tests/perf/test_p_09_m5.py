"""P-09: budget and degradation (docs/04), the M5 part's testimony clause (NFR-PERF-7; #162).

A salient witness's testimony adjudication (``belief_adjudication``) is optional work: nothing
is reserved for it, and it spends from the pool only after follow-up and before repair. With a
salient witness whose adjudication the budget cannot fit, the acceptance is defaulted by the
rules (the witness still forms its belief), the response lists ``testimony_defaulted``, and the
step sits in NFR-PERF-7's order after the skipped polish (and any extra repairs) and before
entailment. Complement: with the budget large enough the witness is adjudicated and nothing is
listed.

The three-primary clause (#174, ``test_p_09__three_primaries``): a ``sequential`` turn plans one
render, one extraction and one first output check per primary (B = 3), so its minimum is
``3 + B + C + G``. A three-primary request whose budget is exactly the single-primary minimum
plans one primary (the first named) and (#176) turns the other two into reactive narration, and
reports it: ``primaries_reduced`` in the degradation (cause ``budget``), the response's
``reduced_characters`` and their ``roles`` (``reactive``, source ``reduced``), and the plan's
``reduced``; the step sits in NFR-PERF-7's order after the skipped polish and before beat
replacement. With the default budget (``4 + 5 per primary + G``) all three render; one below the
single-primary minimum is still refused before any model call."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import FRIEND_OF
from epistrel.orchestrator.structured import planner
from tests import multi_support as ms
from tests import perception_support as ps
from tests import reactive_support as rs
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, render, structured_story, turn_report

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _accounted(turn: dict[str, Any]) -> None:
    """P-09's M2 invariant: counted == recorded, within the cap."""
    assert turn["budget"]["used"] == len(turn["model_calls"])
    if turn["budget"]["max_calls"] is not None:
        assert turn["budget"]["used"] <= turn["budget"]["max_calls"]


def _ladder(turn: dict[str, Any]) -> list[str]:
    return [s for s in turn["degradation"] if s in planner.LADDER or s == planner.SAFE_FALLBACK]


@pytest.mark.part("M5", "NFR-PERF-7")
async def test_p_09__testimony_defaulted(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring: caps 4 (defaulted, listed after ``skip_polish``), 5 (the
    adjudication fits, the polish does not) and none (adjudicated, nothing listed)."""
    expected = {
        None: [],
        5: [planner.SKIP_POLISH],
        4: [planner.SKIP_POLISH, planner.TESTIMONY_DEFAULTED],
    }
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        for cap, ladder in expected.items():
            story, ids, things = await ts.room(client, engine, "Wren", "Jenni", "Bobby")
            await ts.enable_testimony(engine, story, model_calls=cap)
            await ps._author(
                engine,
                story,
                Proposition(
                    subject_id=ids["Bobby"],
                    predicate_id=FRIEND_OF,
                    object_id=ids["Jenni"],
                    valid_time=Interval.of(None, None),
                ),
            )
            stub.reset()
            stub.script(
                "generation",
                [
                    render(
                        beat(
                            "say",
                            ts.CLAIM_LINE,
                            intent="sincere",
                            propositions=[claim("Olivia", "sent", "the text")],
                        ),
                        beat("do", "Jenni folds her arms."),
                    )
                ],
            )
            stub.script("salience_detection", [ts.salient_say(2)])
            answer = await respond(client, story, body(ids["Wren"], "What happened?"))
            assert answer.status_code == 200, answer.text
            turn = answer.json()
            _accounted(turn)
            assert _ladder(turn) == ladder, (cap, turn["degradation"])
            adjudications = turn["call_counts"].get("belief_adjudication", 0)
            report = await turn_report(engine, story)
            told = report["epistemic"]["testimony"]
            if planner.TESTIMONY_DEFAULTED in ladder:
                assert adjudications == 0
                assert told["defaulted"] == 1
                details = {d["step"]: d["cause"] for d in turn["degradation_details"]}
                assert details[planner.TESTIMONY_DEFAULTED] == "budget"
            else:
                assert adjudications == 1
                assert (told["adjudicated"], told["defaulted"]) == (1, 0)
                assert report["budget"]["used_by_kind"]["testimony"] == 1
            # Defaulted or adjudicated, Bobby (who trusts Jenni) believes her.
            held = [
                b
                for b in ts.first_order(await ts.beliefs_of(engine, story, ids["Bobby"]))
                if b.predicate_id == things["sent"]
            ]
            assert [(b.cause, b.outcome, b.confidence_level) for b in held] == [
                ("testimony", "accept", "believed")
            ]


@pytest.mark.part("M5", "NFR-PERF-7")
async def test_p_09__three_primaries(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring (the three-primary clause)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Bobby", "Jenni", "Ada")
        await ms.per_character(engine, story)
        three = [ids["Bobby"], ids["Jenni"], ids["Ada"]]
        refused = await respond(
            client,
            story,
            body(ids["Cal"], "Well?", render_character_ids=three[:1], budget={"max_calls": 1}),
        )
        assert refused.status_code == 422, refused.text
        minimum = refused.json()["error"]["details"]["minimum_required"]

        # Exactly the single-primary minimum: one primary plans, the others are deferred.
        stub.reset()
        for name in ("Bobby", "Jenni", "Ada"):
            ms.renders_for(stub, name, ms.render(beat("say", f"{name} speaks.", intent="sincere")))
        answer = await respond(
            client,
            story,
            body(ids["Cal"], "Well?", render_character_ids=three, budget={"max_calls": minimum}),
        )
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        _accounted(turn)
        # #176 (NFR-PERF-7 (6)): the extra primaries (default roles) render as reactive
        # narration instead of being deferred.
        assert planner.PRIMARIES_REDUCED in turn["degradation"]
        assert planner.PRIMARIES_DEFERRED not in turn["degradation"]
        details = {d["step"]: d["cause"] for d in turn["degradation_details"]}
        assert details[planner.PRIMARIES_REDUCED] == "budget"
        ladder = _ladder(turn)
        assert ladder == sorted(ladder, key=planner.ladder_rank)
        assert planner.ladder_rank(planner.PRIMARIES_REDUCED) < planner.ladder_rank(
            planner.BEAT_REPLACED
        )
        assert planner.ladder_rank(planner.SKIP_POLISH) < planner.ladder_rank(
            planner.PRIMARIES_REDUCED
        )
        assert len(ms.render_prompts(stub, "Bobby")) == 1
        assert ms.render_prompts(stub, "Jenni") == ms.render_prompts(stub, "Ada") == []
        assert turn["reduced_characters"] == three[1:]
        assert {c: turn["roles"][c] for c in three[1:]} == {
            c: {"role": "reactive", "source": "reduced"} for c in three[1:]
        }
        report = await turn_report(engine, story)
        assert report["multi"]["deferred"] == []
        assert report["budget"]["plan"]["primaries"] == 1
        assert report["budget"]["plan"]["reduced"] == three[1:]
        assert "order" not in turn  # one primary rendered

        # The default budget plans all three: B is one extraction per render.
        stub.reset()
        for name in ("Bobby", "Jenni", "Ada"):
            ms.renders_for(stub, name, ms.render(beat("say", f"{name} speaks.", intent="sincere")))
        answer = await respond(client, story, body(ids["Cal"], "Well?", render_character_ids=three))
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        _accounted(turn)
        assert turn["order"] == three
        assert planner.PRIMARIES_DEFERRED not in turn["degradation"]
        report = await turn_report(engine, story)
        plan = report["budget"]["plan"]
        assert plan["primaries"] == 3
        assert plan["minimum"] == 3 + plan["reservation"]["extraction"] + (
            plan["reservation"]["composed"] + plan["reservation"]["guardrail"]
        )
        if plan["reservation"]["extraction"]:
            assert plan["reservation"]["extraction"] == 3
        assert report["budget"]["max_calls"] == 4 + 5 * 3 + plan["reservation"]["guardrail"]

        # Below the single-primary minimum is still refused before any model call.
        stub.reset()
        refused = await respond(
            client,
            story,
            body(
                ids["Cal"], "Well?", render_character_ids=three, budget={"max_calls": minimum - 1}
            ),
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "budget_below_safety_minimum"
        assert stub.calls == []


@pytest.mark.part("M5", "NFR-PERF-7")
async def test_p_09__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """NFR-PERF-7 step 6 (#176): a request naming Bobby, Ada (her role fixed ``primary``) and
    Dale (a default role) with the single-primary minimum as its budget plans Bobby alone; Dale
    renders as reactive narration (``primaries_reduced``, ``reduced_characters``, his role
    ``reactive`` from ``reduced``, and Bobby's render may narrate his reaction), and Ada, whose
    fixed ``primary`` the engine never lowers, is deferred with a notice (``primaries_deferred``
    and ``primary_deferred``, ``deferred_characters`` with ``fixed``) and never narrated
    reactively (Bobby's narration of her is dropped). The steps sit in NFR-PERF-7's order:
    after the defaulted testimony (#162's step 3) and entailment's safe disposition, before beat
    replacement."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        b = await rs.bar(client, engine)
        await rs.set_role(client, b.story_id, b["Ada"], "primary", fixed=True)
        named = [b["Bobby"], b["Ada"], b["Dale"]]
        refused = await respond(
            client,
            b.story_id,
            body(b["Cal"], "Well?", render_character_ids=named[:1], budget={"max_calls": 1}),
        )
        minimum = refused.json()["error"]["details"]["minimum_required"]
        stub.reset()
        ms.renders_for(
            stub,
            "Bobby",
            rs.render(
                beat("say", "Evening.", intent="sincere"),
                reactions=[
                    rs.reaction(b["Dale"], "Dale nods."),
                    rs.reaction(b["Ada"], "Ada smiles."),
                ],
            ),
        )
        answer = await respond(
            client,
            b.story_id,
            body(b["Cal"], "Well?", render_character_ids=named, budget={"max_calls": minimum}),
        )
        assert answer.status_code == 200, answer.text
        turn = answer.json()
        _accounted(turn)
        assert rs.character_renders(stub) == ["Bobby"]
        assert turn["reduced_characters"] == [b["Dale"]]
        assert turn["deferred_characters"] == [
            {"character_id": b["Ada"], "reason": "budget", "fixed": True}
        ]
        assert turn["roles"][b["Dale"]] == {"role": "reactive", "source": "reduced"}
        assert turn["roles"][b["Ada"]] == {"role": "primary", "source": "fixed"}
        assert [n.character_id for n in await rs.narrated(engine, b.story_id)] == [b["Dale"]]
        steps = _ladder(turn)
        for step in (
            planner.PRIMARIES_REDUCED,
            planner.PRIMARIES_DEFERRED,
            planner.PRIMARY_DEFERRED,
        ):
            assert step in steps
        assert steps == sorted(steps, key=planner.ladder_rank)
        report = await turn_report(engine, b.story_id)
        assert report["budget"]["plan"]["reduced"] == [b["Dale"]]
        assert report["budget"]["plan"]["deferred"] == [b["Ada"]]
    rank = planner.ladder_rank
    assert (
        rank(planner.SKIP_POLISH)
        < rank(planner.REPAIR_DETERMINISTIC)
        < rank(planner.TESTIMONY_DEFAULTED)
        < rank(planner.ENTAILMENT_SAFE_DISPOSITION)
        < rank(planner.PRIMARIES_REDUCED)
        == rank(planner.PRIMARIES_DEFERRED)
        == rank(planner.PRIMARY_DEFERRED)
        < rank(planner.BEAT_REPLACED)
    )
