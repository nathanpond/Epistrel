"""The two model adapters without a database: the stub's determinism and test controls, and
the ``openai`` SDK adapter against a local OpenAI-compatible server."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import openai
import pytest
from pydantic import ValidationError

from epistrel.orchestrator.models.adapter import Message, ModelParams, ModelUnavailable
from epistrel.orchestrator.models.config import ConfigRow, EffectiveConfig, KeyBox, resolve
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter, request_body
from epistrel.orchestrator.models.registry import GuardrailVerdict
from epistrel.orchestrator.models.stub import GARBAGE, StubAdapter
from tests.model_support import config_key, mock_openai

REPO_ROOT = Path(__file__).resolve().parent.parent
HELLO = (Message(role="system", content="Be brief."), Message(role="user", content="Say hello"))


# -- the stub ---------------------------------------------------------------------------------


async def test_stub_default_answer_is_deterministic_and_shows_the_prompt() -> None:
    stub = StubAdapter()
    first = await stub.complete("generation", HELLO, ModelParams())
    second = await StubAdapter().complete("generation", HELLO, ModelParams())
    assert first == second
    assert first.text.startswith("[stub generation ")
    assert first.text.endswith("] Say hello")
    assert first.model == "stub"
    assert first.latency_ms == 1
    # Any change to purpose, messages or params changes the hash.
    other = await stub.complete("generation", HELLO, ModelParams(temperature=0.1))
    assert other.text != first.text
    assert len(stub.calls) == 2


def test_stub_is_byte_identical_across_processes() -> None:
    """The demo's step 4: the same probe in two processes prints the same bytes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTREL_")}
    env["EPISTREL_MODEL_ADAPTER"] = "stub"
    env["PYTHONHASHSEED"] = "random"
    command = [sys.executable, "-m", "epistrel.tools.model_probe", "generation", "Say hello"]
    runs = [
        subprocess.run(
            command, capture_output=True, env=env, cwd=REPO_ROOT, timeout=60, check=True
        ).stdout
        for _ in range(2)
    ]
    assert runs[0] == runs[1]
    output = json.loads(runs[0])
    assert output["adapter"] == "stub"
    assert output["text"].startswith("[stub generation ")
    assert "player_input.say" in output["request_fields"]


async def test_stub_answers_a_schema_with_a_conformant_object() -> None:
    result = await StubAdapter().complete("guardrail_check", HELLO, ModelParams(), GuardrailVerdict)
    verdict = GuardrailVerdict.model_validate_json(result.text)
    assert (verdict.violation, verdict.confidence) == (False, 1.0)


async def test_stub_when_filters_by_purpose_and_needs_every_substring() -> None:
    """``when`` with a purpose answers only that purpose; a tuple needs all its substrings.
    Complement asserted: an unmatched request gets the default answer."""
    stub = StubAdapter()
    stub.when(("hello", "absent"), "both")
    stub.when("hello", "summary only", purpose="lite_summary")
    generated = await stub.complete("generation", HELLO, ModelParams())
    assert generated.text.startswith("[stub generation ")
    assert (await stub.complete("lite_summary", HELLO, ModelParams())).text == "summary only"
    stub.when(("hello",), "any purpose")
    assert (await stub.complete("generation", HELLO, ModelParams())).text == "any purpose"


async def test_stub_scripts_matches_faults_and_latches() -> None:
    stub = StubAdapter()
    stub.script("generation", ["one", "two"])
    stub.when("hello", "matched")
    texts = [(await stub.complete("generation", HELLO, ModelParams())).text for _ in range(3)]
    assert texts == ["one", "two", "matched"]

    stub.fault("generation", "timeout")
    with pytest.raises(openai.APITimeoutError):
        await stub.complete("generation", HELLO, ModelParams())
    stub.fault("generation", "http_500", n=2)
    for _ in range(2):
        with pytest.raises(openai.InternalServerError):
            await stub.complete("generation", HELLO, ModelParams())
    stub.fault("generation", "garbage")
    assert (await stub.complete("generation", HELLO, ModelParams())).text == GARBAGE
    assert (await stub.complete("generation", HELLO, ModelParams())).text == "matched"

    latch = stub.hold("lite_summary")
    pending = asyncio.create_task(stub.complete("lite_summary", HELLO, ModelParams()))
    await asyncio.wait_for(latch.entered.wait(), 5)
    assert not pending.done()
    latch.release()
    assert (await asyncio.wait_for(pending, 5)).text == "matched"

    stub.reset()
    stub.nondeterministic = True
    first = await stub.complete("lite_summary", HELLO, ModelParams())
    second = await stub.complete("lite_summary", HELLO, ModelParams())
    assert first.text != second.text


# -- params and configuration resolution ------------------------------------------------------


def test_model_params_bounds_and_override() -> None:
    for bad in ({"temperature": 2.5}, {"top_p": -0.1}, {"max_tokens": 4_001}, {"seed": 1}):
        with pytest.raises(ValidationError):
            ModelParams.model_validate(bad)
    base = ModelParams(temperature=0.8, top_p=0.95, max_tokens=400)
    merged = base.merged(ModelParams(temperature=0.2, stop=("END",)))
    assert merged.wire() == {"temperature": 0.2, "top_p": 0.95, "max_tokens": 400, "stop": ["END"]}


def test_effective_config_inherits_per_field_from_default() -> None:
    from datetime import UTC, datetime

    at = datetime(2026, 10, 11, tzinfo=UTC)
    default = ConfigRow("default", "http://gw/v1", "house", "cipher", {"temperature": 0.7}, at)
    own = ConfigRow("generation", None, "special", None, {"top_p": 0.5}, at)
    config = resolve("generation", own, default, 1)
    assert (config.base_url, config.model, config.api_key_ciphertext) == (
        "http://gw/v1",
        "special",
        "cipher",
    )
    assert config.params.wire() == {"temperature": 0.7, "top_p": 0.5}
    alone = resolve("lite_summary", None, default, 1)
    assert alone.model == "house"


def test_request_body_shapes() -> None:
    params = ModelParams(max_tokens=50, temperature=0.0)
    body = request_body("m", HELLO, params, GuardrailVerdict)
    assert body["max_completion_tokens"] == 50
    assert body["stream"] is False
    assert body["response_format"]["json_schema"]["name"] == "GuardrailVerdict"
    legacy = request_body("m", HELLO, params, None, legacy_max_tokens=True)
    assert legacy["max_tokens"] == 50
    assert "max_completion_tokens" not in legacy
    assert "response_format" not in legacy


# -- the openai SDK adapter -------------------------------------------------------------------


def fixed(config: EffectiveConfig) -> OpenAICompatibleAdapter:
    keybox = KeyBox(config_key())

    async def resolver(_: str) -> EffectiveConfig:
        return config

    return OpenAICompatibleAdapter(resolver, keybox)


def configured(base_url: str | None, model: str | None, key: str | None) -> EffectiveConfig:
    keybox = KeyBox(config_key())
    return EffectiveConfig(
        purpose="generation",
        base_url=base_url,
        model=model,
        api_key_ciphertext=keybox.encrypt(key) if key else None,
        params=ModelParams(max_tokens=20, timeout=5),
        revision=1,
    )


async def test_openai_adapter_honours_base_url_and_falls_back_to_max_tokens() -> None:
    with mock_openai(legacy_max_tokens=True) as server:
        adapter = fixed(configured(f"{server.root}/lmstudio/v1", "local-model", "lm-studio"))
        try:
            result = await adapter.complete("generation", HELLO, ModelParams())
            again = await adapter.complete("generation", HELLO, ModelParams())
        finally:
            await adapter.aclose()
    assert result.text == "[mock local-model] Say hello"
    assert (result.tokens_in, result.tokens_out, result.finish_reason) == (7, 3, "stop")
    assert result.request_id == "chatcmpl-mock"
    assert again.text == result.text
    paths = [r.path for r in server.requests]
    assert paths == ["/lmstudio/v1/chat/completions"] * 3  # one 400, one retry, one remembered
    assert "max_completion_tokens" in server.requests[0].body
    assert server.requests[1].body["max_tokens"] == 20
    assert "max_completion_tokens" not in server.requests[2].body
    assert server.requests[0].authorization == "Bearer lm-studio"


async def test_openai_adapter_does_not_retry_on_its_own() -> None:
    """SDK retries are off: one 500 is one request, raised as the SDK's own type."""
    with mock_openai(fail_with=500) as server:
        adapter = fixed(configured(f"{server.root}/v1", "m", "k"))
        try:
            with pytest.raises(openai.InternalServerError):
                await adapter.complete("generation", HELLO, ModelParams())
        finally:
            await adapter.aclose()
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("base_url", "model", "key", "reason"),
    [
        (None, "m", "k", "not_configured"),
        ("http://127.0.0.1:9/v1", None, "k", "not_configured"),
        ("http://127.0.0.1:9/v1", "m", None, "no_api_key"),
    ],
)
async def test_openai_adapter_refuses_an_incomplete_config(
    base_url: str | None, model: str | None, key: str | None, reason: str
) -> None:
    adapter = fixed(configured(base_url, model, key))
    with pytest.raises(ModelUnavailable) as exc:
        await adapter.complete("generation", HELLO, ModelParams())
    assert exc.value.details == {"purpose": "generation", "reason": reason}
    assert exc.value.http_status == 503


async def test_undecryptable_key_is_an_auth_failure() -> None:
    config = configured("http://127.0.0.1:9/v1", "m", "k")
    other = KeyBox(None)

    async def resolver(_: str) -> EffectiveConfig:
        return config

    adapter = OpenAICompatibleAdapter(resolver, other)
    with pytest.raises(ModelUnavailable) as exc:
        await adapter.complete("generation", HELLO, ModelParams())
    assert exc.value.details["reason"] == "auth"
