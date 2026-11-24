"""#941 (FR-MODEL-7, ADR-212): the strict-schema warm-up on MockOpenAI, with no database.

The Model Access Layer's ``resolve`` is replaced by a per-test configuration, and the mock
server stands in for api.anthropic.com (``provider_schema.ANTHROPIC_HOST`` is ``127.0.0.1``) and
answers it as api.anthropic.com did live (``require_strict``).

- At start-up, exactly one strict call per warm-listed schema per strict endpoint: one user line,
  ``strict: true`` with the schema's wire form, ``max_tokens`` 64. It is in no call log and no
  metric. A second pass sends nothing.
- Nothing for a non-strict endpoint, for a purpose with no key, when turned off, or under the
  stub adapter. ``EPISTREL_MODEL_WARMUP`` takes ``0``/``false``/``off``.
- A failed call logs one content-free warning and is not retried.
- A configuration change re-warms once: a new model is sent, changes that arrive during a pass
  are coalesced into one more pass, and an unchanged endpoint and model are not sent again.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator

import pytest

from epistrel.engine import Engine
from epistrel.orchestrator.models import provider_schema
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox
from epistrel.orchestrator.models.metrics import recording, sample
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.registry import RewriteParseResult
from epistrel.orchestrator.models.warmup import (
    WARM_LIST,
    WARMUP_MAX_TOKENS,
    WARMUP_PROMPT,
    StrictWarmup,
    warm_purposes,
)
from tests.auth_support import auth_settings
from tests.model_support import MockOpenAI, config_key, mock_openai
from tests.test_model_adapters import configured

DUMMY_DSN = "postgresql+psycopg://u:p@localhost:5432/db"
WARMUP_LOGGER = "epistrel.orchestrator.models.warmup"


class Configs:
    """What ``resolve`` answers for every purpose: one endpoint, model and key."""

    def __init__(self, base_url: str | None, model: str = "claude-test", key: str | None = "k"):
        self.base_url = base_url
        self.model = model
        self.key = key
        self.reads = 0

    async def resolve(self, purpose: str) -> EffectiveConfig:
        self.reads += 1
        found = configured(self.base_url, self.model, self.key)
        return EffectiveConfig(
            purpose=purpose,
            base_url=found.base_url,
            model=found.model,
            api_key_ciphertext=found.api_key_ciphertext,
            params=found.params,
            revision=1,
        )


def layer(configs: Configs, monkeypatch: pytest.MonkeyPatch) -> ModelAccessLayer:
    """A Model Access Layer whose adapter and ``resolve`` read ``configs``."""
    mal = ModelAccessLayer(
        auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()),
        adapter=OpenAICompatibleAdapter(configs.resolve, KeyBox(config_key())),
    )
    monkeypatch.setattr(mal, "resolve", configs.resolve)
    return mal


@pytest.fixture
def as_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local mock server (127.0.0.1) is api.anthropic.com for this test, and
    ``RewriteParseResult`` stands in for a warm-listed schema with a strict wire form: its 17
    union-typed parameters are over the live ``UNION_LIMIT`` (see
    ``tests/test_strict_union_limit.py``), so the limit is lifted here."""
    monkeypatch.setattr(provider_schema, "ANTHROPIC_HOST", "127.0.0.1")
    monkeypatch.setattr(provider_schema, "UNION_LIMIT", 64)


@pytest.fixture
def strict_server() -> Iterator[MockOpenAI]:
    with mock_openai(require_strict=True) as server:
        yield server


def chat_requests(server: MockOpenAI) -> list[dict[str, object]]:
    return [r.body for r in server.requests if r.path.rstrip("/").endswith("/chat/completions")]


def test_warm_list__rewrite_parse() -> None:
    """``RewriteParseResult`` is on the list, and only ``rewrite_parse`` answers with it."""
    assert RewriteParseResult in WARM_LIST
    assert warm_purposes() == (("rewrite_parse", RewriteParseResult),)


@pytest.mark.usefixtures("as_anthropic")
async def test_startup__one_strict_call_per_schema(
    strict_server: MockOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = Configs(f"{strict_server.root}/v1")
    mal = layer(configs, monkeypatch)
    warmup = StrictWarmup(mal, enabled=True)
    before = sample(
        "epistrel_model_calls_total", purpose="rewrite_parse", model="claude-test", outcome="ok"
    )
    try:
        with recording() as log:
            warmup.start()
            await warmup.wait()
            assert await warmup.warm_once() == 0  # warmed: a second pass sends nothing
    finally:
        await mal.aclose()
    (body,) = chat_requests(strict_server)
    json_schema = body["response_format"]["json_schema"]  # type: ignore[index]
    assert json_schema["name"] == "RewriteParseResult"
    assert json_schema["strict"] is True
    assert body["messages"] == [{"role": "user", "content": WARMUP_PROMPT}]
    assert WARMUP_MAX_TOKENS in (body.get("max_completion_tokens"), body.get("max_tokens"))
    assert log.calls == []  # not a request's call
    after = sample(
        "epistrel_model_calls_total", purpose="rewrite_parse", model="claude-test", outcome="ok"
    )
    assert after == before
    assert warmup.warmed == {(f"{strict_server.root}/v1", "claude-test", "RewriteParseResult")}


async def test_not_strict_endpoint__no_call(
    strict_server: MockOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any host but api.anthropic.com is never sent a warm-up."""
    mal = layer(Configs(f"{strict_server.root}/v1"), monkeypatch)
    warmup = StrictWarmup(mal, enabled=True)
    try:
        warmup.start()
        await warmup.wait()
    finally:
        await mal.aclose()
    assert chat_requests(strict_server) == []


@pytest.mark.usefixtures("as_anthropic")
async def test_disabled_or_keyless__no_call(
    strict_server: MockOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = Configs(f"{strict_server.root}/v1")
    mal = layer(configs, monkeypatch)
    off = StrictWarmup(mal, enabled=False)
    keyless = StrictWarmup(layer(Configs(configs.base_url, key=None), monkeypatch), enabled=True)
    try:
        off.start()
        off.changed()
        await off.wait()
        assert configs.reads == 0
        keyless.start()
        await keyless.wait()
    finally:
        await mal.aclose()
        await keyless.models.aclose()
    assert chat_requests(strict_server) == []


@pytest.mark.parametrize(
    ("value", "expected"), [("0", False), ("false", False), ("off", False), ("1", True)]
)
def test_setting__env_values(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("EPISTREL_MODEL_WARMUP", value)
    assert auth_settings(DUMMY_DSN).model_warmup is expected


def test_setting__default_on_and_stub_never_warms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EPISTREL_MODEL_WARMUP")
    assert auth_settings(DUMMY_DSN).model_warmup is True
    openai = Engine(auth_settings(DUMMY_DSN, model_adapter="openai", config_key=config_key()))
    stub = Engine(auth_settings(DUMMY_DSN, model_adapter="stub"))
    assert openai.warmup.enabled is True
    assert stub.warmup.enabled is False


@pytest.mark.usefixtures("as_anthropic")
async def test_failure__one_warning_no_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with mock_openai(fail_with=500) as server:
        mal = layer(Configs(f"{server.root}/v1"), monkeypatch)
        warmup = StrictWarmup(mal, enabled=True)
        try:
            with caplog.at_level(logging.DEBUG, logger=WARMUP_LOGGER):
                warmup.start()
                await warmup.wait()
        finally:
            await mal.aclose()
    assert len(chat_requests(server)) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "purpose=rewrite_parse host=127.0.0.1 error=InternalServerError" in message
    for content in (WARMUP_PROMPT, "RewriteParseResult", "mock failure", "properties"):
        assert content not in message
    assert warmup.warmed == set()


@pytest.mark.usefixtures("as_anthropic")
async def test_config_change__rewarms_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model change during a pass is warmed by one coalesced pass after it; a change that
    leaves the endpoint and model alone sends nothing."""
    gate = threading.Event()
    with mock_openai(require_strict=True, gate=gate) as server:
        configs = Configs(f"{server.root}/v1")
        mal = layer(configs, monkeypatch)
        warmup = StrictWarmup(mal, enabled=True)
        try:
            warmup.start()
            for _ in range(200):
                if server.requests:
                    break
                await asyncio.sleep(0.02)
            configs.model = "claude-test-2"
            warmup.changed()
            warmup.changed()
            gate.set()
            await warmup.wait()
            warmup.changed()  # unchanged endpoint and model: nothing to send
            await warmup.wait()
        finally:
            gate.set()
            await warmup.stop()
            await mal.aclose()
    models = [body["model"] for body in chat_requests(server)]
    assert models == ["claude-test", "claude-test-2"]


async def test_stop__cancels_a_pending_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Shutdown cancels the pass without waiting for the call, and logs no failure."""
    monkeypatch.setattr(provider_schema, "ANTHROPIC_HOST", "127.0.0.1")
    monkeypatch.setattr(provider_schema, "UNION_LIMIT", 64)  # a strict wire form to warm
    gate = threading.Event()
    with mock_openai(require_strict=True, gate=gate) as server:
        mal = layer(Configs(f"{server.root}/v1"), monkeypatch)
        warmup = StrictWarmup(mal, enabled=True)
        try:
            with caplog.at_level(logging.WARNING, logger=WARMUP_LOGGER):
                warmup.start()
                for _ in range(200):
                    if server.requests:
                        break
                    await asyncio.sleep(0.02)
                assert warmup.running
                async with asyncio.timeout(5):
                    await warmup.stop()
                warmup.changed()  # stopped: no new pass
                assert not warmup.running
        finally:
            gate.set()
            await mal.aclose()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
