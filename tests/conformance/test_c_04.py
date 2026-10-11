"""C-04: model-adapter conformance (docs/04), the M2 parts this Model Access Layer delivers.

Both adapters — the ``openai`` SDK adapter against a local OpenAI-compatible server and the
deterministic stub — run the same suite (``tests/conformance/suite.py``).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.policies.model import Policy
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import DELIVERED, GuardrailVerdict
from epistrel.orchestrator.models.stub import GARBAGE
from epistrel.testing.ops import TEST_EVENT, GuardedReplyEvent, guarded_reply, make_policy
from tests.auth_support import auth_settings, bearer
from tests.conformance.suite import (
    DEFAULT_KEY,
    GENERATION_MODEL,
    HOUSE_MODEL,
    run_suite,
)
from tests.event_support import new_story
from tests.model_support import (
    MockOpenAI,
    admin_client,
    config_key,
    mock_openai,
    put,
    put_key,
    restored_model_config,
)
from tests.structured_support import (
    CHECK,
    POLICY_PROMPT,
    nothing_conclusive,
    story_state,
    structured_settings,
)

ADAPTERS = ("openai", "stub")
PROBE = example_instance(DELIVERED["generation"].input_schema)
SUMMARY = example_instance(DELIVERED["lite_summary"].input_schema)


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
    assert set(purposes) == {"default", *DELIVERED}
    assert purposes["lite_summary"]["model"] == HOUSE_MODEL
    assert purposes["generation"]["api_key"] == "***"
    assert purposes["generation"]["sources"]["model"] == "own"
    assert purposes["lite_summary"]["sources"]["model"] == "default"
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


@pytest.mark.integration
@pytest.mark.part("M2", "FR-MODEL-4")
async def test_c_04__runtime(
    model_engine: Engine, adapter_settings: Settings, mock: MockOpenAI
) -> None:
    """An operator changes ``generation``'s model, endpoint and key through
    ``/admin/model-config`` and the very next call uses each change, in the same process with
    the same Engine (no restart). Complement asserted: a call made before a change used the
    old value, so the test would see a stale cache."""
    pid = os.getpid()
    async with admin_client(adapter_settings, model_engine) as client:
        await put(client, "default", {"base_url": f"{mock.root}/v1", "model": "model-a"})
        await put_key(client, "default", "sk-runtime-key-a")
        first = await model_engine.models.call("generation", PROBE)
        assert first.model == "model-a"

        revision = await put(client, "generation", {"model": "model-b"})
        second = await model_engine.models.call("generation", PROBE)
        assert second.model == "model-b"
        # The unchanged purpose still inherits the default.
        assert (await model_engine.models.call("lite_summary", SUMMARY)).model == "model-a"

        await put(client, "generation", {"model": "model-b", "base_url": f"{mock.root}/gw/v1"})
        await put_key(client, "generation", "sk-runtime-key-b")
        third = await model_engine.models.call("generation", PROBE)
        assert third.model == "model-b"
        listing = (await client.get("/admin/model-config", headers=bearer())).json()
        assert listing["config_revision"] > revision
        assert listing["purposes"]["generation"]["sources"]["base_url"] == "own"

    assert os.getpid() == pid
    if adapter_settings.model_adapter == "openai":
        calls = [(r.path, r.authorization, r.body["model"]) for r in mock.requests]
        assert calls == [
            ("/v1/chat/completions", "Bearer sk-runtime-key-a", "model-a"),
            ("/v1/chat/completions", "Bearer sk-runtime-key-a", "model-b"),
            ("/v1/chat/completions", "Bearer sk-runtime-key-a", "model-a"),
            ("/gw/v1/chat/completions", "Bearer sk-runtime-key-b", "model-b"),
        ]
        adapter = model_engine.models.adapter
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert adapter.closed_clients >= 1  # superseded clients were closed once idle
    else:
        assert mock.requests == []


async def _point_default_at(client: httpx.AsyncClient, mock: MockOpenAI) -> None:
    await put(client, "default", {"base_url": f"{mock.root}/v1", "model": "house-model"})
    await put_key(client, "default", DEFAULT_KEY)


def _adapter_calls(engine: Engine, mock: MockOpenAI) -> int:
    if engine.models.adapter.name == "openai":
        return len(mock.requests)
    return len(engine.models.stub.calls)


@pytest.mark.integration
@pytest.mark.part("M2", "FR-MODEL-6,7")
async def test_c_04__structured_output(
    model_engine: Engine, adapter_settings: Settings, mock: MockOpenAI, engine: AsyncEngine
) -> None:
    """Structured results under both adapters: the ``json_schema`` path validates; an endpoint
    that answers ``response_format`` with a 400 is served by parse-validate-retry on attempt 2;
    garbage on every attempt is ``Uncertain`` and the caller's conservative default decides.

    Complement asserted: the garbage text appears nowhere in the story's events, while the
    reply ``generation`` produced on a clean run does (so the search can find payload text)."""
    models = model_engine.models
    openai_kind = adapter_settings.model_adapter == "openai"
    async with admin_client(adapter_settings, model_engine) as client:
        await _point_default_at(client, mock)

        # json_schema: one call, constrained decoding requested, a validated verdict.
        budget = TurnBudget()
        verdict = await models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            CHECK,
            deterministic_first=nothing_conclusive,
            budget=budget,
        )
        assert verdict == GuardrailVerdict(violation=False, confidence=1.0)
        assert budget.used == 1

        # A configuration write moves the revision; the endpoint now rejects response_format.
        await put(client, "guardrail_check", {"model": "strict-less-model"})
        if openai_kind:
            mock.reject_response_format = True
        else:
            models.stub.fault("guardrail_check", "response_format_400")
        budget = TurnBudget()
        verdict = await models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            CHECK,
            deterministic_first=nothing_conclusive,
            budget=budget,
        )
        assert verdict == GuardrailVerdict(violation=False, confidence=1.0)
        assert budget.used == 2  # the 400 probe, then the fallback that succeeded
        if openai_kind:
            sent = ["response_format" in r.body for r in mock.requests]
            assert sent == [True, True, False]
            mock.reject_response_format = False
        else:
            assert [c.response_schema for c in models.stub.calls[-2:]] == [
                "GuardrailVerdict",
                None,
            ]

        # Garbage twice with structured_retry_budget 1 → Uncertain → the policy's default.
        story = await new_story(engine)
        clean = await guarded_reply(
            model_engine, story, "a quiet road", principal="p-test", policies=[model_policy()]
        )
        async with structured_settings(engine, structured_retry_budget=1):
            if openai_kind:
                mock.script.extend([GARBAGE, GARBAGE])
            else:
                models.stub.fault("guardrail_check", "garbage", 2)
            before = _adapter_calls(model_engine, mock)
            op = await guarded_reply(
                model_engine, story, "a dark wood", principal="p-test", policies=[model_policy()]
            )
            assert _adapter_calls(model_engine, mock) - before == 2

    state = await story_state(engine, story)
    payload = op.staged[0].payload
    assert isinstance(payload, GuardedReplyEvent)
    assert (payload.input_violation, payload.input_decided_by) == (True, "uncertain")
    assert payload.reply is None  # refused: no generation call was made
    assert state.statuses[op.operation_id] == "committed"
    assert GARBAGE not in state.payloads
    assert "stub garbage" not in state.payloads
    # The complement: the clean run's reply is in the log, so the search sees payload text.
    clean_payload = clean.staged[0].payload
    assert isinstance(clean_payload, GuardedReplyEvent)
    assert clean_payload.reply
    assert json.dumps(clean_payload.reply)[1:-1] in state.payloads  # as stored (JSON-escaped)


@pytest.mark.integration
@pytest.mark.part("M2", "FR-MODEL-5")
async def test_c_04__deterministic_first(
    model_engine: Engine, adapter_settings: Settings, mock: MockOpenAI, engine: AsyncEngine
) -> None:
    """A word-list violation decides the guardrail check with **zero** adapter calls, under
    both adapters. Complement asserted: an input the word list does not catch goes to the model
    (the check on input and output, and the generation between them)."""
    async with admin_client(adapter_settings, model_engine) as client:
        await _point_default_at(client, mock)
        story = await new_story(engine)
        policies = [
            make_policy({"type": "wordlist", "words": ["dragon"]}, name="w"),
            model_policy(),
        ]

        before = _adapter_calls(model_engine, mock)
        caught = await guarded_reply(
            model_engine, story, "A dragon lands.", principal="p-test", policies=policies
        )
        assert _adapter_calls(model_engine, mock) == before  # zero model calls

        passed = await guarded_reply(
            model_engine, story, "A knight lands.", principal="p-test", policies=policies
        )
        assert _adapter_calls(model_engine, mock) - before == 3

    first, second = caught.staged[0].payload, passed.staged[0].payload
    assert isinstance(first, GuardedReplyEvent)
    assert isinstance(second, GuardedReplyEvent)
    assert (first.input_violation, first.input_decided_by, first.reply) == (
        True,
        "deterministic",
        None,
    )
    assert (second.input_violation, second.input_decided_by) == (False, "model")
    assert second.reply
    state = await story_state(engine, story)
    assert state.events[caught.operation_id] == [TEST_EVENT]


def model_policy() -> Policy:
    return make_policy({"type": "model_assisted", "prompt": POLICY_PROMPT}, name="model")
