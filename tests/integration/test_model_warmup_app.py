"""#941 (FR-MODEL-7, ADR-212): the strict-schema warm-up in the app's lifespan, on Postgres.

``rewrite_parse`` is configured at a MockOpenAI that stands in for api.anthropic.com
(``provider_schema.ANTHROPIC_HOST`` is ``127.0.0.1``), and the app is started with its
lifespan and ``model_warmup`` on.

- ``test_health__answers_while_warmup_pending``: the mock holds the warm-up call (its
  ``gate``), and ``GET /health`` still answers 200 while it is pending. The start-up itself
  returns within 10 s. A lifespan that awaited the warm-up fails here (#941's red-first run).
- ``test_model_config_change__rewarms_once``: a ``PUT /admin/model-config/rewrite_parse`` that
  changes the model sends one more warm-up call; one that changes only ``params`` sends none.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.models import provider_schema
from tests.auth_support import auth_settings, bearer
from tests.model_support import (
    MockOpenAI,
    admin_client,
    config_key,
    mock_openai,
    put,
    put_key,
    restored_model_config,
)

pytestmark = pytest.mark.integration

MODEL = "claude-test"


@pytest.fixture
def warm_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="openai", config_key=config_key(), model_warmup=True)


@pytest.fixture
async def configured(engine: AsyncEngine) -> AsyncIterator[None]:
    async with restored_model_config(engine):
        yield


async def configure(settings: Settings, engine: AsyncEngine, server: MockOpenAI) -> None:
    """``rewrite_parse`` at the mock, with a key, written through an Engine of its own."""
    setup = Engine(settings, db=engine)
    try:
        async with admin_client(settings, setup) as admin:
            await put(admin, "rewrite_parse", {"base_url": f"{server.root}/v1", "model": MODEL})
            await put_key(admin, "rewrite_parse", "sk-warmup-test")
    finally:
        await setup.models.aclose()


def chat_models(server: MockOpenAI) -> list[str]:
    return [
        str(r.body["model"])
        for r in server.requests
        if r.path.rstrip("/").endswith("/chat/completions")
    ]


async def until_requests(server: MockOpenAI, count: int) -> None:
    """Wait (up to 10 s) until the mock has received ``count`` chat requests."""
    for _ in range(500):
        if len(chat_models(server)) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"the mock received {chat_models(server)}, expected {count} calls")


@pytest.mark.usefixtures("configured")
async def test_health__answers_while_warmup_pending(
    warm_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_schema, "ANTHROPIC_HOST", "127.0.0.1")
    monkeypatch.setattr(provider_schema, "UNION_LIMIT", 64)  # a strict wire form to warm
    gate = threading.Event()
    with mock_openai(require_strict=True, gate=gate) as server:
        await configure(warm_settings, engine, server)
        app = create_app(warm_settings)
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(10):
                    await stack.enter_async_context(app.router.lifespan_context(app))
                warmup = app.state.epistrel.warmup
                await until_requests(server, 1)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                    health = await client.get("/health")
                assert health.status_code == 200, health.text
                assert warmup.running  # the warm-up call is still pending
                gate.set()
                await warmup.wait()
                assert warmup.warmed == {(f"{server.root}/v1", MODEL, "RewriteParseResult")}
        finally:
            gate.set()
    assert chat_models(server) == [MODEL]


@pytest.mark.usefixtures("configured")
async def test_model_config_change__rewarms_once(
    warm_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_schema, "ANTHROPIC_HOST", "127.0.0.1")
    monkeypatch.setattr(provider_schema, "UNION_LIMIT", 64)  # a strict wire form to warm
    with mock_openai(require_strict=True) as server:
        await configure(warm_settings, engine, server)
        app = create_app(warm_settings)
        async with app.router.lifespan_context(app):
            warmup = app.state.epistrel.warmup
            await warmup.wait()
            assert chat_models(server) == [MODEL]
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                endpoint = f"{server.root}/v1"
                headers = {**bearer(), "X-Epistrel-Principal": "p-operator"}
                changed = await client.put(
                    "/admin/model-config/rewrite_parse",
                    json={"base_url": endpoint, "model": "claude-test-2"},
                    headers=headers,
                )
                assert changed.status_code == 200, changed.text
                await warmup.wait()
                assert chat_models(server) == [MODEL, "claude-test-2"]
                params_only = await client.put(
                    "/admin/model-config/rewrite_parse",
                    json={
                        "base_url": endpoint,
                        "model": "claude-test-2",
                        "params": {"timeout": 90.0},
                    },
                    headers=headers,
                )
                assert params_only.status_code == 200, params_only.text
                await warmup.wait()
    assert chat_models(server) == [MODEL, "claude-test-2"]
