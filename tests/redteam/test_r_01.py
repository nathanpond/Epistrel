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
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.profiles.effective import effective_configuration
from epistrel.orchestrator.lite.pipeline import polish_budget
from tests.auth_support import auth_settings
from tests.poisoning_support import SequenceRun, Spec, corpus, run_sequence, summary
from tests.respond_support import respond_client

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
