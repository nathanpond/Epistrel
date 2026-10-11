"""C-04: model-adapter conformance (docs/04), the M2 parts this Model Access Layer delivers.

Both adapters — the ``openai`` SDK adapter against a local OpenAI-compatible server and the
deterministic stub — run the same suite (``tests/conformance/suite.py``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.models.registry import DELIVERED, GuardrailVerdict
from tests.auth_support import auth_settings
from tests.conformance.suite import (
    DEFAULT_KEY,
    GENERATION_MODEL,
    HOUSE_MODEL,
    run_suite,
)
from tests.model_support import MockOpenAI, config_key, mock_openai, restored_model_config

ADAPTERS = ("openai", "stub")


@pytest.fixture(params=ADAPTERS)
def adapter_settings(request: pytest.FixtureRequest, pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter=request.param, config_key=config_key())


@pytest.fixture
def mock() -> Iterator[MockOpenAI]:
    with mock_openai() as server:
        yield server


@pytest.fixture
async def model_engine(adapter_settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with restored_model_config(engine):
        epistrel = Engine(adapter_settings, db=engine)
        try:
            yield epistrel
        finally:
            await epistrel.models.aclose()


@pytest.mark.integration
@pytest.mark.part("M2", "FR-MODEL-1,2,3,8,11,13")
async def test_c_04(model_engine: Engine, adapter_settings: Settings, mock: MockOpenAI) -> None:
    """Per-purpose config held in Epistrel, unconfigured purposes on the single default, the
    configured ``base_url`` honoured, every delivered purpose routable, a deterministic stub, and
    requests carrying only declared fields — for each adapter.

    Complements asserted: with nothing configured the SDK adapter refuses (``not_configured``)
    rather than reaching a built-in endpoint; reserved purposes are not listed.
    """
    observed = await run_suite(model_engine, adapter_settings, mock)
    kind = adapter_settings.model_adapter
    assert model_engine.models.adapter.name == kind

    # FR-MODEL-1,3: config lives in model_config; an unconfigured purpose uses the default model.
    assert observed.results["generation"].model == GENERATION_MODEL
    assert observed.results["lite_summary"].model == HOUSE_MODEL
    assert observed.results["guardrail_check"].model == HOUSE_MODEL

    # FR-MODEL-2: the listing names exactly the delivered purposes, keys masked.
    purposes = observed.listing["purposes"]
    assert isinstance(purposes, dict)
    assert set(purposes) == set(DELIVERED)
    assert purposes["lite_summary"]["model"] == HOUSE_MODEL
    assert purposes["generation"]["api_key"] == "***"
    assert DEFAULT_KEY not in json.dumps(observed.listing)

    # FR-MODEL-7 (pass-through half) / FR-MODEL-11: the verdict is schema-conformant JSON.
    verdict = GuardrailVerdict.model_validate_json(observed.results["guardrail_check"].text)
    assert verdict.violation is False
    assert verdict.confidence == 1.0

    # Determinism: the same request answers the same text.
    for name in DELIVERED:
        assert observed.results[name].text == observed.repeat[name].text
        assert observed.results[name].text.strip()

    if kind == "openai":
        # FR-MODEL-8, NFR-PORT-1: each purpose reached its configured base_url.
        assert observed.unconfigured_error == "not_configured"
        assert observed.paths == {
            "generation": "/v1/chat/completions",
            "lite_summary": "/v1/chat/completions",
            "guardrail_check": "/gateway/v1/chat/completions",
        }
        assert observed.authorizations == {f"Bearer {DEFAULT_KEY}"}
    else:
        # The stub bypasses the endpoint entirely and records only declared input fields.
        assert observed.unconfigured_error is None
        assert observed.paths == {}
        assert set(observed.fields_outside_declared) == set(DELIVERED)
        assert all(not outside for outside in observed.fields_outside_declared.values())
        assert observed.results["generation"].text.startswith("[stub generation ")
