"""C-04: model-adapter conformance (docs/04), the M2 parts this Model Access Layer delivers, and
the M4 part: the guide's embedding-port row (#138, ``test_c_04__embedding_row``).

Both adapters — the ``openai`` SDK adapter against a local OpenAI-compatible server and the
deterministic stub — run the same suite (``tests/conformance/suite.py``).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.config import Settings
from epistrel.core.policies.model import Policy
from epistrel.engine import Engine
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import metrics, registry
from epistrel.orchestrator.models.config import ConfigRow, KeyBox, describe
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    GuardrailVerdict,
    InputSchema,
    Purpose,
)
from epistrel.orchestrator.models.stub import GARBAGE
from epistrel.testing.ops import (
    TEST_EVENT,
    GuardedReplyEvent,
    guarded_reply,
    make_policy,
    make_router,
)
from epistrel.tools import model_guide
from tests.auth_support import auth_settings, bearer
from tests.conformance.suite import (
    DEFAULT_KEY,
    GENERATION_MODEL,
    HOUSE_MODEL,
    configure,
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
from tests.story_support import WRITE
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

    # FR-MODEL-2: the listing names exactly the delivered purposes and ports, keys masked.
    purposes = observed.listing["purposes"]
    assert isinstance(purposes, dict)
    assert set(purposes) == {"default", *DELIVERED, *registry.PORTS}
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
        # A purpose that defaults to guardrail_check's row (M3, #121) reaches its base_url too.
        # A purpose that defaults to the default row (the eval harness's quality_judge, #134)
        # reaches the default row's base_url.
        inheriting = {
            name: "/gateway/v1/chat/completions"
            if purpose.default_model_key == "guardrail_check"
            else "/v1/chat/completions"
            for name, purpose in DELIVERED.items()
            if name not in ("generation", "lite_summary", "guardrail_check")
        }
        assert observed.paths == {
            "generation": "/v1/chat/completions",
            "lite_summary": "/v1/chat/completions",
            "guardrail_check": "/gateway/v1/chat/completions",
            **inheriting,
        }
        assert observed.paths["quality_judge"] == "/v1/chat/completions"
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


# -- model guide (FR-MODEL-10,13) and call counts (FR-MODEL-12), #54 ---------------------------


class _NoInputs(InputSchema):
    """A synthetic purpose's input schema that declares nothing: no data-flow row."""


def _synthetic(**changes: object) -> Purpose:
    return replace(DELIVERED["lite_summary"], name="synthetic_check", **changes)  # type: ignore[arg-type]


@pytest.mark.part("M2", "FR-MODEL-10,13")
def test_c_04__guide() -> None:
    """The published guide equals the guide generated from the registry, and it has a floor
    and a data-flow row for every purpose delivered so far.

    Complements asserted: a purpose lacking a floor, or lacking a data-flow row, cannot be
    rendered (the generator and the CI check both fail), while the same purpose with both
    renders."""
    guide = (Path(__file__).resolve().parents[2] / "docs" / "models.md").read_text("utf-8")
    assert guide == model_guide.render()
    assert model_guide.problems(DELIVERED.values()) == []
    for name, purpose in DELIVERED.items():
        section = guide.split(f"### `{name}`", 1)[1].split("\n### ", 1)[0]
        assert "**Capability floor:**" in section
        assert f"{purpose.capability_floor.min_context_tokens:,} tokens" in section
        assert "**Receives**" in section
        assert "**Never receives:**" in section
        for category in purpose.never_receives:
            assert f"`{category.value}`" in section.split("**Never receives:**", 1)[1]

    no_floor = _synthetic(capability_floor=None)
    no_row = _synthetic(input_schema=_NoInputs)
    for broken, why in ((no_floor, "no capability floor"), (no_row, "no data-flow row")):
        registry = {**DELIVERED, broken.name: broken}
        assert any(why in p for p in model_guide.problems(registry.values()))
        with pytest.raises(model_guide.GuideError, match=why):
            model_guide.render(registry)
    renders = model_guide.render({**DELIVERED, "synthetic_check": _synthetic()})
    assert "### `synthetic_check`" in renders


def _calls(purpose: str, model: str, outcome: str) -> float:
    return metrics.sample(
        "epistrel_model_calls_total", purpose=purpose, model=model, outcome=outcome
    )


def _tokens(purpose: str, kind: str) -> float:
    return metrics.sample("epistrel_model_tokens_total", purpose=purpose, kind=kind)


@pytest.mark.integration
@pytest.mark.part("M2", "FR-MODEL-12")
async def test_c_04__call_counts(
    model_engine: Engine, adapter_settings: Settings, mock: MockOpenAI, engine: AsyncEngine
) -> None:
    """Per-purpose call counts and tokens are observable as counters and on the operation's
    ``model_calls``, one entry per attempt, for each adapter; cost appears only when the
    endpoint reports it.

    Complements asserted: a failed attempt is counted under its outcome (not ``ok``) with no
    tokens; an answer with no reported cost carries no ``cost`` key."""
    await configure(model_engine, mock)
    kind = adapter_settings.model_adapter
    gen_label = metrics.model_label(GENERATION_MODEL)
    before = {
        "gen_ok": _calls("generation", gen_label, "ok"),
        "gen_err": _calls("generation", gen_label, "http_error"),
        "check_ok": _calls("guardrail_check", HOUSE_MODEL, "ok"),
        "gen_in": _tokens("generation", "input"),
        "gen_out": _tokens("generation", "output"),
    }
    with metrics.recording() as log:
        await model_engine.models.call("generation", PROBE)
        await model_engine.models.call("generation", PROBE)
        verdict = await model_engine.models.structured_call(
            "guardrail_check",
            GuardrailVerdict,
            example_instance(DELIVERED["guardrail_check"].input_schema),
            deterministic_first=nothing_conclusive,
            budget=TurnBudget(),
        )
        assert isinstance(verdict, GuardrailVerdict)
        if kind == "openai":
            mock.fail_with = 500
        else:
            model_engine.models.stub.fault("generation", "http_500")
        with pytest.raises(ModelUnavailable):
            await model_engine.models.call("generation", PROBE)
        mock.fail_with = None

    assert [(c.purpose, c.outcome, c.attempt) for c in log.calls] == [
        ("generation", "ok", 1),
        ("generation", "ok", 1),
        ("guardrail_check", "ok", 1),
        ("generation", "http_error", 1),
    ]
    assert log.by_purpose() == {"generation": 3, "guardrail_check": 1}
    assert all(c.model for c in log.calls)
    ok_generation = [c for c in log.calls if c.purpose == "generation" and c.outcome == "ok"]
    failed = log.calls[-1]
    assert (failed.tokens_in, failed.tokens_out, failed.cost) == (0, 0, None)

    assert _calls("generation", gen_label, "ok") - before["gen_ok"] == 2
    assert _calls("generation", gen_label, "http_error") - before["gen_err"] == 1
    assert _calls("guardrail_check", HOUSE_MODEL, "ok") - before["check_ok"] == 1
    assert _tokens("generation", "input") - before["gen_in"] == sum(
        c.tokens_in for c in ok_generation
    )
    assert _tokens("generation", "output") - before["gen_out"] == sum(
        c.tokens_out for c in ok_generation
    )
    if kind == "openai":
        assert all(c.tokens_in == 7 and c.tokens_out == 3 for c in ok_generation)
        assert all(c.cost is None and "cost" not in c.model_dump() for c in log.calls)
        cost_before = metrics.sample(
            "epistrel_model_cost_total", purpose="generation", model=gen_label
        )
        mock.cost = 0.25
        with metrics.recording() as priced:
            await model_engine.models.call("generation", PROBE)
        assert priced.calls[0].model_dump()["cost"] == 0.25
        cost_after = metrics.sample(
            "epistrel_model_cost_total", purpose="generation", model=gen_label
        )
        assert cost_after - cost_before == 0.25
    else:
        assert all(c.request_id and c.request_id.startswith("stub-") for c in ok_generation)


@pytest.mark.integration
@pytest.mark.part("M2", "FR-MODEL-12")
async def test_c_04__call_counts_on_the_response(pg_url: str, engine: AsyncEngine) -> None:
    """A turn-shaped operation's response carries ``model_calls``: one entry per attempt, with
    purpose, model, latency, tokens, attempt and outcome (#57's ``respond`` reports the same).

    Complement asserted: without a model-assisted policy the same request makes one call."""

    settings = auth_settings(pg_url, model_adapter="stub")
    app = create_app(settings)
    include_protected(app, make_router())
    epistrel: Engine = app.state.epistrel
    epistrel.bind(engine)
    story = await new_story(engine)
    url = f"/_test/stories/{story}/guarded-reply"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        guarded = await client.post(
            url, json={"said": "a quiet road", "model_prompt": POLICY_PROMPT}, headers=WRITE
        )
        plain = await client.post(url, json={"said": "a quiet road"}, headers=WRITE)
    assert guarded.status_code == 200, guarded.text
    calls = guarded.json()["model_calls"]
    assert [c["purpose"] for c in calls] == ["guardrail_check", "generation", "guardrail_check"]
    for call in calls:
        assert {"purpose", "model", "latency_ms", "tokens_in", "tokens_out", "attempt"} <= set(call)
        assert call["outcome"] == "ok"
        assert call["model"] == "stub"
    assert [c["purpose"] for c in plain.json()["model_calls"]] == ["generation"]


@pytest.mark.part("M4", "FR-MODEL-13; FR-EMBED-1")
def test_c_04__embedding_row() -> None:
    """The guide gains the embedding port's row (#138): a section under the delivered ports with
    its capability floor (input tokens, dimension range, batch), its own ``model_config`` row,
    its data-flow row (``text``: ``memory_text``) and the categories it never receives, and the
    hosted-retention note covers the texts it embeds. The registry delivers it as a port (its
    own row, not routable by a profile) and no longer reserves it; resolution gives it its own
    row only.

    Complements: a port declaring an input it never receives fails the registry's check, and
    the chat purposes still inherit the ``default`` row that the port does not."""
    guide = (Path(__file__).resolve().parents[2] / "docs" / "models.md").read_text("utf-8")
    assert guide == model_guide.render()
    ports = guide.split("## Ports delivered", 1)[1].split("## Reserved purposes", 1)[0]
    section = ports.split("### `embedding`", 1)[1]
    assert "**Capability floor:** inputs of at least 512 tokens" in section
    assert "vectors of 64 to 4,096 dimensions; batches of up to 64 texts" in section
    assert "the `embedding` row of `model_config` only" in section
    assert "| `text` | `memory_text` |" in section
    never = section.split("**Never receives:**", 1)[1]
    assert "`player_input`" in never
    assert "`subject_true_age`" in never
    assert "`memory_text`" not in never
    assert "leave the deployment when this row names a hosted provider" in section
    retention = guide.split("## Hosted providers and retention", 1)[1]
    assert "retrieval queries the `embedding` port sends" in retention
    assert "| `embedding` |" not in guide.split("## Reserved purposes", 1)[1]

    port = registry.PORTS["embedding"]
    assert port.status is registry.PurposeStatus.DELIVERED
    assert port.default_model_key == "embedding"
    assert not port.story_scoped
    assert "embedding" not in registry.RESERVED
    assert "embedding" not in registry.STORY_PURPOSES
    with pytest.raises(registry.RegistryError, match="never receives"):
        registry.validate_port(replace(port, never_receives=frozenset(registry.ContentCategory)))

    stamp = datetime(2026, 10, 17, tzinfo=UTC)
    rows = {
        "default": ConfigRow("default", "http://house/v1", "house-model", "k", {}, stamp),
    }
    keybox = KeyBox(None)
    embedding = describe("embedding", rows, keybox)
    assert (embedding.base_url, embedding.model, embedding.has_api_key) == (None, None, False)
    assert embedding.sources == {"base_url": "own", "model": "own", "api_key": "own", "params": {}}
    generation = describe("generation", rows, keybox)
    assert generation.model == "house-model"
    assert generation.sources["model"] == "default"
