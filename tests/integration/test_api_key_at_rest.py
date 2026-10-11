"""Model API keys are stored encrypted, and a stored key without ``EPISTREL_CONFIG_KEY`` stops
the server from starting (unless the stub adapter is in use)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import cli
from epistrel.api import create_app
from epistrel.config import ConfigError, load_settings
from epistrel.engine import Engine
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.config import KeyBox
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.model_support import CONFIG_KEY, config_key, restored_model_config

CANARY = "sk-canary-at-rest-4f1d9c0e7b2a"


async def test_api_key_at_rest(pg_url: str, engine: AsyncEngine) -> None:
    """``model_config.api_key_ciphertext`` is ciphertext and no ``SELECT`` returns the
    plaintext; the complement: the key in-process decrypts back to it."""
    settings = auth_settings(pg_url, config_key=config_key())
    epistrel = Engine(settings, db=engine)
    async with restored_model_config(engine):
        await model_config.write_config(
            engine,
            "default",
            keybox=epistrel.models.keybox,
            principal="p-operator",
            base_url="http://host.docker.internal:1234/v1",
            model="local",
            api_key=CANARY,
        )
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT m::text FROM model_config m"))).scalars().all()
            stored = await conn.scalar(
                text("SELECT api_key_ciphertext FROM model_config WHERE purpose = 'default'")
            )
        assert rows
        assert all(CANARY not in row for row in rows)
        assert stored is not None
        assert stored != CANARY
        assert KeyBox(config_key()).decrypt(stored, purpose="default").get_secret_value() == CANARY
        resolved = await epistrel.models.resolve("generation")
        assert resolved.api_key_ciphertext == stored  # inherited from default, still encrypted


async def test_stored_key_without_config_key_refuses_to_start(
    pg_url: str, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    keybox = KeyBox(config_key())
    async with restored_model_config(engine):
        await model_config.write_config(
            engine, "default", keybox=keybox, principal="p-operator", api_key=CANARY
        )
        missing = auth_settings(pg_url)
        assert await model_config.config_key_problem(missing, engine) == (
            model_config.CONFIG_KEY_REQUIRED
        )
        # Complements: with the key set, or under the stub, the check passes.
        with_key = auth_settings(pg_url, config_key=config_key())
        assert await model_config.config_key_problem(with_key, engine) is None
        stub = auth_settings(pg_url, model_adapter="stub")
        assert await model_config.config_key_problem(stub, engine) is None

        # The server's lifespan refuses to start.
        with pytest.raises(ConfigError), TestClient(create_app(missing)):
            pass
        with TestClient(create_app(stub)) as client:
            assert client.get("/health").status_code == 200

        # `epistrel serve` exits 78 before starting uvicorn.
        monkeypatch.setenv("EPISTREL_DATABASE_URL", pg_url)
        monkeypatch.setenv("EPISTREL_SERVICE_CREDENTIALS", CREDENTIAL_A)
        assert await asyncio.to_thread(cli.main, ["serve"]) == cli.EX_CONFIG
        monkeypatch.setenv("EPISTREL_CONFIG_KEY", CONFIG_KEY)
        assert await model_config.config_key_problem(load_settings(), engine) is None


async def test_writes_validate_purpose_and_base_url(engine: AsyncEngine) -> None:
    from epistrel.core.errors import InvalidRequest

    keybox = KeyBox(None)
    async with restored_model_config(engine):
        with pytest.raises(InvalidRequest):
            await model_config.write_config(engine, "entailment", keybox=keybox, principal="p")
        with pytest.raises(InvalidRequest):
            await model_config.write_config(
                engine, "default", keybox=keybox, principal="p", base_url="ftp://x"
            )
        with pytest.raises(model_config.ConfigKeyMissing):
            await model_config.write_config(
                engine, "default", keybox=keybox, principal="p", api_key="k"
            )
        await model_config.write_config(
            engine, "generation", keybox=keybox, principal="p", model="g"
        )
        await model_config.remove_override(engine, "generation")
        with pytest.raises(InvalidRequest):
            await model_config.remove_override(engine, "default")
