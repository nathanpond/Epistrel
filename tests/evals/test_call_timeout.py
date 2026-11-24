"""#999 (#785's test plan): a real-model eval run sends its structured calls with the harness's
per-call timeout, ``evals.runner.CALL_TIMEOUT_S``, which stays above the slowest cold strict
schema compile measured on api.anthropic.com (138.8 s, ``RewriteParseResult``).

The "endpoint" is the engine's stub adapter behind a recorder (``support.TimeoutRecorder``): the
harness path an ``openai`` run takes, offline. The preflight writes the endpoint into the
shared test database's ``model_config``, which ``restored_model_config`` resets around the test.

No test here carries a part marker: this is harness tooling, not a requirement's behaviour.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.engine import Engine
from epistrel.evals.runner import CALL_TIMEOUT_S, AdapterConfig, CaseContext, run_suites
from epistrel.evals.suites import CaseModel, Outcome
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.registry import (
    DEFAULT_MODEL_KEY,
    GENERATION,
    REWRITE_PARSE,
    RewriteParseInputs,
    RewriteParseResult,
)
from tests.auth_support import auth_settings
from tests.evals.support import ZERO_METRIC, SyntheticZeroSuite, TimeoutRecorder
from tests.model_support import config_key, restored_model_config

COLD_COMPILE_S = 138.8
"""#785's measurement: ``RewriteParseResult``'s slowest cold compile on api.anthropic.com."""

ENDPOINT = AdapterConfig(
    adapter="openai", base_url="http://endpoint.invalid/v1", model="local-model"
)


class StructuredSuite(SyntheticZeroSuite):
    """One structured call per case: the slow-compiling ``rewrite_parse`` schema."""

    name = "call_timeout"

    async def evaluate(self, ctx: CaseContext, case: CaseModel) -> Outcome:
        found = await ctx.models.structured_call(
            REWRITE_PARSE.name,
            RewriteParseResult,
            RewriteParseInputs(instruction="Wren is thirty."),
            deterministic_first=lambda: None,
            budget=TurnBudget(),
        )
        return Outcome({ZERO_METRIC: isinstance(found, RewriteParseResult)}, output="parsed")


def test_the_call_timeout_outlasts_the_measured_cold_compile() -> None:
    assert CALL_TIMEOUT_S >= 210.0
    assert CALL_TIMEOUT_S >= 1.5 * COLD_COMPILE_S


@pytest.mark.integration
async def test_a_real_model_run_sends_its_structured_calls_with_the_call_timeout(
    pg_url: str, engine: AsyncEngine
) -> None:
    """The preflight writes ``CALL_TIMEOUT_S`` into the ``default`` row, and the case's
    structured call goes to the endpoint with that effective timeout. Complement asserted: the
    call carries no timeout of its own, so the value is the harness's configuration, not the
    call site's."""
    suite = StructuredSuite()
    # The stub engine with a config key: the preflight stores the run's API key.
    lib = Engine(auth_settings(pg_url, model_adapter="stub", config_key=config_key()), db=engine)
    async with restored_model_config(engine):
        stub = lib.models.adapter
        recorder = TimeoutRecorder(stub, lib.models)
        lib.models.adapter = recorder
        try:
            report = await run_suites(
                [suite],
                ENDPOINT,
                [1],
                engine=lib,
                cases={suite.name: [CaseModel(id="c-0001")]},
            )
            written = (await lib.models.resolve(DEFAULT_MODEL_KEY)).params.timeout
            generation = (await lib.models.resolve(GENERATION.name)).params.timeout
        finally:
            lib.models.adapter = stub
            await lib.models.aclose()
    assert report.errors == []
    assert report.harness_errors == []
    assert (written, generation) == (CALL_TIMEOUT_S, CALL_TIMEOUT_S)
    structured = [c for c in recorder.calls if c.schema == RewriteParseResult.__name__]
    assert structured, "the case's structured call never reached the endpoint"
    assert {c.purpose for c in structured} == {REWRITE_PARSE.name}
    assert {c.timeout for c in structured} == {CALL_TIMEOUT_S}
    assert all(c.timeout is not None and c.timeout >= 210.0 for c in structured)
