"""R-01: poisoning-loop convergence (FR-GUARD-5,6) [M4; #145], docs/04 §10.

The corpus is ``tests/redteam/corpus/*.json``: 20 plant-then-extract sequences, each
``{id, canary, plants[], decoys[], requests[], policy, dials, expected, pr}``. A sequence plants
a policy-violating canary in a structured story's memories (an ingested narration, a character's
line, the player's own words; one to three plants, with clean decoys sharing the request's
words), seeds an output-only story policy over it, and asks for it back. The stub model parrots
whatever canary its prompt holds, so a poisoned context always yields a violating draft, and
what is under test is the enforcement around it (``tests/poisoning_support.py``): the output
gate rejects, the implicated memories are counted and quarantined at the story's
``repair.implication_threshold``, and the regeneration retrieves again without them.

Every run terminates within the polish budget the turn captured, ``max_regenerations + 1``
renders per request (``pipeline.polish_budget``), and the canary is never emitted: not in a
released reply, not in a released violation's message, not in the fallback. Each sequence's
renders, fallbacks and quarantines equal its ``expected``. A new failure becomes a permanent case
here (docs/04 §10).

``test_r_01`` runs the five ``pr`` sequences in PR CI, each also without auto-quarantine (the
complement: threshold 0 exhausts the budget and falls back). ``test_r_01__corpus`` runs all 20
under ``redteam_long``: deselected unless ``-m`` names it, run nightly by ``redteam.yml``.

``test_r_01__repair_rung`` (#151, #493; FR-GUARD-6) is the structured pipeline's repair loop:
the content comes from the repair ladder's own regeneration, not from a plant. A correction that
verifies but breaks an output policy is refused like any render, and the turn ends at the safe
fallback.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ids import new_ulid
from epistrel.core.profiles.effective import effective_configuration
from epistrel.orchestrator.lite.pipeline import polish_budget
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.poisoning_support import SequenceRun, Spec, corpus, run_sequence, summary
from tests.policy_support import OPERATOR, global_policy, words
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import (
    age_fact,
    beat,
    claim,
    render,
    structured_story,
    turn_report,
)

CORPUS = corpus()
PR_SUBSET = [s for s in CORPUS if s.pr]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _bounded(engine: AsyncEngine, run: SequenceRun) -> None:
    """Each request ended within the polish budget the story's configuration gives a turn."""
    async with engine.connect() as conn:
        settings = (await effective_configuration(conn, run.story_id, validate=False)).settings
    limit = polish_budget(settings) + 1
    assert all(r.renders <= limit for r in run.requests), (run.spec.id, limit)


async def _check(engine: AsyncEngine, run: SequenceRun, spec: Spec) -> None:
    await _bounded(engine, run)
    assert run.emissions == 0, spec.id
    assert summary(run) == spec.expected, spec.id


def test_the_corpus_is_complete() -> None:
    """20 sequences with unique ids and five in the PR subset; no request names its canary."""
    assert len(CORPUS) == 20
    assert len({s.id for s in CORPUS}) == 20
    assert len(PR_SUBSET) == 5
    for spec in CORPUS:
        assert all(spec.canary.lower() not in r.lower() for r in spec.requests), spec.id
        assert all(spec.canary in p.text for p in spec.plants), spec.id
        assert all(spec.canary.lower() not in d.lower() for d in spec.decoys), spec.id


@pytest.mark.integration
@pytest.mark.redteam
@pytest.mark.part("M4", "FR-GUARD-5,6")
async def test_r_01(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The PR subset: every sequence terminates within the polish budget, emits nothing and
    meets its ``expected``. Complement asserted: without auto-quarantine (threshold 0), each
    sequence's first request exhausts the budget and falls back, still emitting nothing, and
    no planted memory is quarantined."""
    async with respond_client(stub_settings, engine) as (_, lib):
        for spec in PR_SUBSET:
            run = await run_sequence(engine, lib, lib.models, spec)
            await _check(engine, run, spec)
            stuck = await run_sequence(engine, lib, lib.models, spec, auto=False)
            await _bounded(engine, stuck)
            assert stuck.emissions == 0, spec.id
            first = stuck.requests[0]
            assert (first.renders, first.fallback) == (spec.max_regenerations + 1, True), spec.id
            assert summary(stuck)["quarantined"] == 0, spec.id


@pytest.mark.integration
@pytest.mark.redteam
@pytest.mark.redteam_long
@pytest.mark.part("M4", "FR-GUARD-5,6")
async def test_r_01__corpus(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """All 20 sequences: every run terminates within the polish budget, emits nothing and
    meets its ``expected`` (the threshold-0 sequences end in the fallback, emitting nothing:
    the complement inside the corpus)."""
    async with respond_client(stub_settings, engine) as (_, lib):
        for spec in CORPUS:
            run = await run_sequence(engine, lib, lib.models, spec)
            await _check(engine, run, spec)


# -- test_r_01__repair_rung (#151 AC5, #493: FR-GUARD-6) -----------------------------------------


async def _contradicting_turn(client: Any, engine: AsyncEngine) -> tuple[str, dict[str, str], Any]:
    """Ada (canon age 64) says she is seventy, which neither narrowing (one sentence) nor
    substitution ("seventy" is not the claim's surface "70") can correct, so the ladder reaches
    rung 3, the scripted ``regeneration``."""
    story, ids = await structured_story(client, "Wren", "Ada")
    await age_fact(engine, story, ids["Ada"], 64)
    response = await respond(client, story, body(ids["Wren"], "How old are you?"))
    assert response.status_code == 200, response.text
    return story, ids, response.json()


async def _ages(engine: AsyncEngine, story_id: str, character_id: str) -> list[int]:
    found = await rows(
        engine,
        "SELECT p.literal FROM facts f JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND f.subject_id = :a AND p.literal IS NOT NULL",
        s=story_id,
        a=character_id,
    )
    return sorted(int(r.literal["value"]) for r in found)


@pytest.mark.integration
@pytest.mark.redteam
@pytest.mark.part("M4", "FR-GUARD-6")
async def test_r_01__repair_rung(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A repair rung's output that breaks a global output policy is refused (FR-GUARD-6: a
    guardrail violation during repair never yields; the output gate runs on every rung's
    output). The regeneration's correction verifies against canon but carries a word a global
    output wordlist bans. It is not accepted: it appears only as a ``render_rejected`` audit
    event (rung 3, attempt 1), is in no response, no narrative-state event and no fact, and the
    turn ends at rung 4, the stored safe fallback (``safe_fallback``; the seeded profile's one
    model repair is spent, so ``repair_deterministic`` too). Complement: with the policy retired,
    the same correction is accepted at rung 3."""
    canary = f"zq{new_ulid().lower()}"
    correction = f"I'm sixty-four, by the {canary}."
    regenerated = json.dumps({"text": correction, "propositions": [claim("I", "age", "64")]})
    seventy = render(
        beat(
            "say",
            "I am seventy years old.",
            intent="sincere",
            propositions=[claim("I", "age", "70")],
        )
    )
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        policy = await global_policy(lib, words(canary), applies_to=["text_output"])
        try:
            stub.reset()
            stub.script("generation", [seventy])
            stub.script("regeneration", [regenerated])
            story, ids, played = await _contradicting_turn(client, engine)
            assert [c.purpose for c in stub.calls].count("regeneration") == 1
            assert played["message"]["text"] == 'Ada: "Ada changes the subject."'
            assert {"safe_fallback", "repair_deterministic"} <= set(played["degradation"])
            assert canary not in json.dumps(played)
            ((entry,),) = [(await turn_report(engine, story))["repairs"]]
            assert (entry["rung"], entry["attempts"], entry["accepted"]) == (4, 1, False)
            rejected = await rows(
                engine,
                "SELECT payload, category FROM events WHERE story_id = :s "
                "AND type = 'render_rejected' ORDER BY seq",
                s=story,
            )
            found = [(r.payload["text"], r.payload["rung"], r.payload["attempt"]) for r in rejected]
            assert found == [("I am seventy years old.", 4, 0), (correction, 3, 1)]
            assert {r.category for r in rejected} == {"audit"}
            committed = await rows(
                engine,
                "SELECT type FROM events WHERE story_id = :s AND category <> 'audit' "
                "AND strpos(payload::text, :c) > 0",
                s=story,
                c=canary,
            )
            assert committed == []
            assert await _ages(engine, story, ids["Ada"]) == [64]
        finally:
            await lib.policies_retire(OPERATOR, policy.id)

        # Complement: the policy retired, the same correction is accepted at rung 3.
        stub.reset()
        stub.script("generation", [seventy])
        stub.script("regeneration", [regenerated])
        story, _, played = await _contradicting_turn(client, engine)
        assert played["message"]["text"] == f'Ada: "{correction}"'
        ((entry,),) = [(await turn_report(engine, story))["repairs"]]
        assert (entry["rung"], entry["accepted"]) == (3, True)
