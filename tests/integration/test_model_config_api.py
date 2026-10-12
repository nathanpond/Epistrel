"""``/admin/model-config`` over REST: the listing, the writes and what they record, the API-key
endpoints, and the refusals that leave the previous configuration untouched (FR-MODEL-4,
NFR-SEC-3; #52). The runtime "next call uses it" scenario is C-04's ``test_c_04__runtime``."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.models.adapter import ModelUnavailable
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import example_instance
from epistrel.orchestrator.models.registry import DELIVERED
from tests.auth_support import auth_settings, bearer
from tests.model_support import (
    OPERATOR_HEADERS,
    admin_client,
    config_key,
    mock_openai,
    put,
    put_key,
    restored_model_config,
)

KEY = "sk-api-test-key-0001"
GENERATION_KEY = "/admin/model-config/generation/api-key"
PROBE = example_instance(DELIVERED["generation"].input_schema)


@pytest.fixture
def settings_with_key(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub", config_key=config_key())


@pytest.fixture
async def epistrel(settings_with_key: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with restored_model_config(engine):
        found = Engine(settings_with_key, db=engine)
        try:
            yield found
        finally:
            await found.models.aclose()


@pytest.fixture
async def client(settings_with_key: Settings, epistrel: Engine) -> AsyncIterator[httpx.AsyncClient]:
    async with admin_client(settings_with_key, epistrel) as found:
        yield found


async def listing(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/admin/model-config", headers=bearer())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def changes(engine: AsyncEngine, after: int) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT config_revision, setting, old, new, class, actor, scope "
                "FROM config_changes WHERE setting LIKE 'model_config:%' "
                "AND config_revision > :after ORDER BY config_revision, setting"
            ),
            {"after": after},
        )
        return [dict(row) for row in rows.mappings()]


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


async def test_listing_shape(client: httpx.AsyncClient) -> None:
    """Every configurable purpose, ``default`` included, with per-field sources; no key."""
    await put(client, "default", {"model": "house", "params": {"temperature": 0.7}})
    await put(client, "generation", {"params": {"top_p": 0.5}})
    body = await listing(client)
    assert isinstance(body["config_revision"], int)
    assert set(body["purposes"]) == {"default", *DELIVERED}
    generation = body["purposes"]["generation"]
    assert generation["model"] == "house"
    assert generation["params"] == {"temperature": 0.7, "top_p": 0.5}
    assert generation["sources"] == {
        "base_url": "default",
        "model": "default",
        "api_key": "default",
        "params": {"temperature": "default", "top_p": "own"},
    }
    assert generation["api_key"] is None
    assert generation["api_key_fingerprint"] is None
    assert body["purposes"]["default"]["sources"]["model"] == "own"
    single = await client.get("/admin/model-config/generation", headers=bearer())
    assert single.status_code == 200
    assert single.json()["purpose"] == "generation"
    assert single.json()["config_revision"] == body["config_revision"]


async def test_put_records_a_prospective_change(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    before = (await listing(client))["config_revision"]
    response = await client.put(
        "/admin/model-config/generation",
        json={"base_url": "https://gw.example/v1/", "model": "  m-1  "},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["config_revision"] == before + 1
    assert body["base_url"] == "https://gw.example/v1"  # trailing slash stripped
    assert body["model"] == "m-1"  # trimmed
    rows = await changes(engine, before)
    assert [(r["setting"], r["old"], r["new"]) for r in rows] == [
        ("model_config:generation.base_url", None, "https://gw.example/v1"),
        ("model_config:generation.model", None, "m-1"),
    ]
    assert {(r["class"], r["actor"], r["scope"], r["config_revision"]) for r in rows} == {
        ("prospective", "p-operator", "deployment", before + 1)
    }
    # A PUT that changes nothing writes nothing.
    again = await client.put(
        "/admin/model-config/generation",
        json={"base_url": "https://gw.example/v1", "model": "m-1"},
        headers=OPERATOR_HEADERS,
    )
    assert again.status_code == 200
    assert again.json()["config_revision"] == before + 1
    assert len(await changes(engine, before)) == 2
    # Full replace: omitting a field returns it to inheritance.
    await put(client, "generation", {"model": "m-1"})
    assert (await listing(client))["purposes"]["generation"]["sources"]["base_url"] == "default"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"base_url": "not a url"}, "body.base_url"),
        ({"base_url": "ftp://host/v1"}, "body.base_url"),
        ({"base_url": "http://"}, "body.base_url"),
        ({"model": "   "}, "body.model"),
        ({"model": "m" * 201}, "body.model"),
        ({"params": {"temperature": 3}}, "body.params.temperature"),
        ({"params": {"max_tokens": 0}}, "body.params.max_tokens"),
        ({"params": {"frequency": 1}}, "body.params.frequency"),
        ({"api_key": "sk-in-the-body"}, "body.api_key"),
    ],
)
async def test_invalid_config_is_refused_and_nothing_changes(
    client: httpx.AsyncClient, engine: AsyncEngine, body: dict[str, Any], field: str
) -> None:
    """Negative: 422 naming the field; the previous configuration and revision are untouched."""
    await put(client, "generation", {"model": "kept", "base_url": "http://gw/v1"})
    before = await listing(client)
    response = await client.put(
        "/admin/model-config/generation", json=body, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["field"] == field
    assert "sk-in-the-body" not in response.text
    assert await listing(client) == before
    assert await changes(engine, before["config_revision"]) == []


@pytest.mark.parametrize("purpose", ["nonsense", "entailment"])
async def test_unknown_purpose_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, purpose: str
) -> None:
    """Negative (#52 AC4): 422 naming the path; the previous configuration, its revision and
    the ``config_changes`` log are untouched (#285)."""
    await put(client, "generation", {"model": "kept", "base_url": "http://gw/v1"})
    before = await listing(client)
    response = await client.put(
        f"/admin/model-config/{purpose}", json={"model": "m"}, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["field"] == "path.purpose"
    assert await listing(client) == before
    assert await changes(engine, before["config_revision"]) == []
    read = await client.get(f"/admin/model-config/{purpose}", headers=bearer())
    assert read.status_code == 422


async def test_api_key_set_never_returned_and_removed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    before = (await listing(client))["config_revision"]
    await put_key(client, "generation", f"  {KEY}  ")
    body = await listing(client)
    entry = body["purposes"]["generation"]
    assert entry["api_key"] == "***"
    assert entry["api_key_fingerprint"] == fingerprint(KEY)  # the trimmed key
    assert entry["sources"]["api_key"] == "own"
    assert body["purposes"]["lite_summary"]["api_key"] is None
    assert KEY not in str(body)
    rows = await changes(engine, before)
    assert [(r["setting"], r["old"], r["new"]) for r in rows] == [
        ("model_config:generation.api_key", None, fingerprint(KEY))
    ]
    # The same key again changes nothing.
    await put_key(client, "generation", KEY)
    assert len(await changes(engine, before)) == 1

    removed = await client.delete(GENERATION_KEY, headers=OPERATOR_HEADERS)
    assert removed.status_code == 204
    assert (await listing(client))["purposes"]["generation"]["api_key"] is None
    rows = await changes(engine, before)
    assert (rows[-1]["old"], rows[-1]["new"]) == (fingerprint(KEY), None)
    again = await client.delete(GENERATION_KEY, headers=OPERATOR_HEADERS)
    assert again.status_code == 204
    assert len(await changes(engine, before)) == 2  # a no-op DELETE writes nothing


@pytest.mark.parametrize(("key", "status"), [("", 422), ("   ", 422), ("k" * 513, 422)])
async def test_api_key_validation(client: httpx.AsyncClient, key: str, status: int) -> None:
    response = await client.put(
        "/admin/model-config/generation/api-key",
        json={"api_key": key},
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == status
    assert response.json()["error"]["details"]["field"] == "body.api_key"


async def test_api_key_without_config_key(pg_url: str, engine: AsyncEngine) -> None:
    settings = auth_settings(pg_url, model_adapter="stub")
    async with restored_model_config(engine):
        found = Engine(settings, db=engine)
        async with admin_client(settings, found) as client:
            response = await client.put(
                "/admin/model-config/generation/api-key",
                json={"api_key": KEY},
                headers=OPERATOR_HEADERS,
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "config_key_missing"
            assert (await listing(client))["purposes"]["generation"]["api_key"] is None


async def test_delete_override(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    await put(client, "default", {"model": "house"})
    await put(client, "generation", {"model": "special"})
    response = await client.delete("/admin/model-config/generation", headers=OPERATOR_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "house"
    assert response.json()["sources"]["model"] == "default"
    revision = response.json()["config_revision"]
    again = await client.delete("/admin/model-config/generation", headers=OPERATOR_HEADERS)
    assert again.status_code == 204
    assert await changes(engine, revision) == []
    default = await client.delete("/admin/model-config/default", headers=OPERATOR_HEADERS)
    assert default.status_code == 409
    assert default.json()["error"]["details"]["reason"] == "is_default"


async def test_writes_need_a_principal(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/admin/model-config/generation", json={"model": "m"}, headers=bearer()
    )
    assert response.status_code == 400


async def test_no_api_key_is_reported_on_first_use_not_at_startup(
    pg_url: str, engine: AsyncEngine
) -> None:
    """A purpose with no key and no default key starts fine and answers 503 ``model_unavailable``
    (``no_api_key``) on its first call; the complement: once a key is set, the call succeeds."""
    settings = auth_settings(pg_url, config_key=config_key())
    async with restored_model_config(engine):
        with TestClient(create_app(settings)) as started:  # the lifespan's checks pass
            assert started.get("/health").status_code == 200
        found = Engine(settings, db=engine)
        try:
            with mock_openai() as mock:
                async with admin_client(settings, found) as client:
                    await put(client, "default", {"base_url": f"{mock.root}/v1", "model": "m"})
                    with pytest.raises(ModelUnavailable) as caught:
                        await found.models.call("generation", PROBE)
                    assert caught.value.http_status == 503
                    assert caught.value.code == "model_unavailable"
                    assert caught.value.details["reason"] == "no_api_key"
                    assert mock.requests == []
                    await put_key(client, "default", KEY)
                    result = await found.models.call("generation", PROBE)
                    assert result.model == "m"
                    assert mock.requests[-1].authorization == f"Bearer {KEY}"
        finally:
            await found.models.aclose()


async def test_superseded_clients_close_once_idle() -> None:
    """A client from an older revision stays open for its in-flight call and is closed by it."""
    from epistrel.orchestrator.models.adapter import ModelParams
    from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox

    keybox = KeyBox(config_key())

    def at(revision: int) -> EffectiveConfig:
        return EffectiveConfig(
            purpose="generation",
            base_url="http://127.0.0.1:9/v1",
            model="m",
            api_key_ciphertext=keybox.encrypt(KEY),
            params=ModelParams(),
            revision=revision,
        )

    async def unused(_: str) -> EffectiveConfig:
        raise AssertionError

    adapter = OpenAICompatibleAdapter(unused, keybox)
    old = await adapter._acquire(at(1), "http://127.0.0.1:9/v1", KEY)
    idle = await adapter._acquire(at(1), "http://127.0.0.1:9/v1", "other-key")
    await adapter._release(idle)
    new = await adapter._acquire(at(2), "http://127.0.0.1:9/v1", KEY)
    assert new is not old
    assert adapter.closed_clients == 1  # the idle one, at once
    assert old.retired
    await adapter._release(old)
    assert adapter.closed_clients == 2  # the busy one, when its call finished
    await adapter._release(new)
    assert adapter.closed_clients == 2  # current clients stay open
    await adapter.aclose()
