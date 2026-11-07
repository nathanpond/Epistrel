"""The image adapter's configuration (FR-IMG-1, NFR-SEC-3; #254): the ``image_generation``
port's row written through the shared ``/admin/model-config`` routes, the adapter it names
built and rebuilt at runtime, ``not_configured`` when there is none, and its key handled like
every other model key."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import errors as api_errors
from epistrel.api.deps import ImageAdapterDep
from epistrel.config import Settings
from epistrel.core.errors import InvalidRequest
from epistrel.engine import Engine
from epistrel.orchestrator.images import registry as images
from epistrel.orchestrator.images.port import Capabilities, ImageRequest, StatusResult
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.adapter import ModelParams
from tests.auth_support import auth_settings, bearer
from tests.conformance.test_c_09 import log_text
from tests.image_support import Outputs, StubImageGenerator, registered
from tests.media_support import png, upload
from tests.model_support import (
    OPERATOR_HEADERS,
    admin_client,
    config_key,
    put,
    put_key,
    restored_model_config,
)

PORT = "/admin/model-config/image_generation"
CANARY = "sk-canary-image-7c41d0e9a3b2f5e8"
REQUEST = ImageRequest(prompt="a lighthouse at dusk")
MEDIA_WRITE = {**bearer(), "X-Epistrel-Principal": "p-media"}


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return auth_settings(
        pg_url, model_adapter="stub", config_key=config_key(), media_root=tmp_path / "media"
    )


@pytest.fixture
async def lib(image_settings: Settings, engine: AsyncEngine) -> AsyncIterator[Engine]:
    async with restored_model_config(engine):
        epistrel = Engine(image_settings, db=engine)
        try:
            yield epistrel
        finally:
            await epistrel.models.aclose()


async def _image_rows(engine: AsyncEngine) -> dict[str, int]:
    """Rows in every ``image_%`` table (none exist before #256) and in ``media_assets``."""
    async with engine.connect() as conn:
        tables = list(
            await conn.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'image\\_%' "
                    "ORDER BY table_name"
                )
            )
        )
        counts = {}
        for table in [*tables, "media_assets"]:
            query = text(f'SELECT count(*) FROM "{table}"')  # noqa: S608 - catalog table names
            counts[table] = int(await conn.scalar(query) or 0)
    return counts


def _guarded_app(lib: Engine) -> FastAPI:
    """A route that only declares ``ImageAdapterDep``, as #260's and #263's will."""
    app = FastAPI()
    api_errors.install(app)
    app.state.epistrel = lib

    @app.post("/generate")
    async def generate(adapter: ImageAdapterDep) -> dict[str, str]:
        return {"adapter": adapter.adapter, "model": adapter.model}

    return app


async def _generate(lib: Engine) -> httpx.Response:
    transport = httpx.ASGITransport(app=_guarded_app(lib))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/generate")


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Each test starts and ends with this process's adapter table as it found it."""
    before = images.registered_adapters()
    yield
    for name in images.registered_adapters() - before:
        images.unregister_image_adapter(name)


class _Declares:
    """An adapter factory that declares ``capabilities`` and implements what it is told to."""

    def __init__(self, capabilities: Capabilities) -> None:
        self._capabilities = capabilities

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def __call__(self, config: images.ImageAdapterConfig) -> StubImageGenerator:
        return StubImageGenerator()


class _NoConfirmation(_Declares):
    """Declares ``confirms_completion`` but has neither ``status`` nor ``cancel``."""


class _StatusOnly(_Declares):
    async def status(self, attempt_ref: str) -> StatusResult:
        return StatusResult("unknown")


class _Confirming(_StatusOnly):
    async def cancel(self, attempt_ref: str) -> str:
        return "not_supported"


@pytest.mark.part("M8", "FR-IMG-1")
def test_i_23__m8_adapter_registration() -> None:
    """Each invalid declaration is refused at registration with ``ImageAdapterInvalid`` naming
    its rule: ``confirms_completion`` with ``sync`` submission; without both ``status`` and
    ``cancel`` (neither, or only one); and without a provider request id per call. Nothing is
    registered for any of them.

    Complement: a valid ``async`` confirming stub and a valid ``sync`` stub both register, the
    table lists them, and a second registration under a taken name is refused unless
    ``replace``."""
    confirming = {"confirms_completion": True, "returns_request_id": True}
    invalid = [
        (_Confirming(Capabilities(submission="sync", **confirming)), "sync_confirms"),
        (_NoConfirmation(Capabilities(submission="async", **confirming)), "missing_status_cancel"),
        (_StatusOnly(Capabilities(submission="async", **confirming)), "missing_status_cancel"),
        (
            _Confirming(Capabilities(submission="async", confirms_completion=True)),
            "no_request_id",
        ),
    ]
    for index, (factory, rule) in enumerate(invalid):
        name = f"invalid_{index}"
        with pytest.raises(images.ImageAdapterInvalid) as refused:
            images.register_image_adapter(name, factory)
        assert refused.value.rule == rule
        assert rule in str(refused.value)
        assert name not in images.registered_adapters()

    async_stub = StubImageGenerator(submission="async", confirms_completion=True)
    sync_stub = StubImageGenerator(submission="sync")
    assert images.register_image_adapter("stub_async", async_stub).confirms_completion
    assert images.register_image_adapter("stub_sync", sync_stub).submission == "sync"
    assert {"stub_async", "stub_sync"} <= images.registered_adapters()
    with pytest.raises(ValueError, match="already registered"):
        images.register_image_adapter("stub_sync", StubImageGenerator())
    images.register_image_adapter("stub_sync", StubImageGenerator(), replace=True)
    # A class factory declares its capabilities the same way.
    images.register_image_adapter(
        "plain", _Declares(Capabilities(submission="sync", confirms_completion=False))
    )
    assert images.register_builtin_image_adapters() >= {"stub_async", "stub_sync", "plain"}


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-1")
async def test_i_23__m8_not_configured(
    lib: Engine, image_settings: Settings, engine: AsyncEngine
) -> None:
    """With ``params.adapter`` null (the default), the generation guard raises
    ``not_configured`` (409, the one envelope, ``details.missing: adapter``) in the library and
    behind a route's dependency, and writes nothing: zero rows reach any ``image_%`` table or
    ``media_assets``. So does an adapter no longer registered in this process (``adapter``), and
    a registered one without a key (``api_key``). ``POST /media`` still answers 201.

    Complement: with the adapter registered and a model and key set, the same guard returns
    the adapter and the route answers 200."""
    before = await _image_rows(engine)
    with pytest.raises(images.ImageNotConfigured) as unset:
        await images.require_image_adapter(lib.models)
    assert (unset.value.code, unset.value.http_status) == ("not_configured", 409)
    assert unset.value.details == {"missing": "adapter"}
    refused = await _generate(lib)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == {
        "code": "not_configured",
        "message": "image generation is not configured: no adapter",
        "details": {"missing": "adapter"},
    }
    assert await _image_rows(engine) == before

    stub = StubImageGenerator(script=[Outputs()])
    async with admin_client(image_settings, lib) as client:
        with registered("stub_sync", stub):
            await put(
                client, "image_generation", {"model": "img-1", "params": {"adapter": "stub_sync"}}
            )
            with pytest.raises(images.ImageNotConfigured) as keyless:
                await images.require_image_adapter(lib.models)
            assert keyless.value.details == {"missing": "api_key", "adapter": "stub_sync"}
            await put_key(client, "image_generation", "sk-image-test-0001")
            handle = await images.require_image_adapter(lib.models)
            assert (handle.adapter, handle.model) == ("stub_sync", "img-1")
            assert (await _generate(lib)).json() == {"adapter": "stub_sync", "model": "img-1"}
        # The row still names stub_sync, but no adapter of that name is registered now.
        gone = await _generate(lib)
        assert gone.status_code == 409
        assert gone.json()["error"]["details"] == {"missing": "adapter", "adapter": "stub_sync"}
        assert stub.requests == []
        assert await _image_rows(engine) == before

        uploaded = await upload(client, png(), headers=MEDIA_WRITE)
        assert uploaded.status_code == 201, uploaded.text
    after = await _image_rows(engine)
    assert after == {**before, "media_assets": before["media_assets"] + 1}


@pytest.mark.integration
async def test_image_adapter_follows_the_configuration(
    lib: Engine, image_settings: Settings
) -> None:
    """An operator turns image generation on by naming an adapter and model and off by
    clearing it, through the shared body, and the next call follows without a restart: a
    changed model builds a new instance and the superseded one is closed. The entry reads
    ``sources: own`` throughout, names its endpoint, and warns while ``image_check`` is unset.

    Complements: an unregistered adapter, an adapter without a model, a chat parameter on the
    port, an image field on a chat purpose and ``api_key`` in the body are each 422 naming
    the field, with the previous configuration untouched."""
    first = StubImageGenerator(script=[Outputs(), Outputs()])
    with registered("stub_sync", first):
        async with admin_client(image_settings, lib) as client:
            listing = (await client.get("/admin/model-config", headers=bearer())).json()
            entry = listing["purposes"]["image_generation"]
            assert (entry["model"], entry["params"], entry["endpoint"]) == (None, {}, None)
            assert entry["sources"] == {
                "base_url": "own",
                "model": "own",
                "api_key": "own",
                "params": {},
            }

            on = await client.put(
                PORT,
                json={
                    "base_url": "https://images.example.test/api",
                    "model": "img-1",
                    "params": {"adapter": "stub_sync", "default_size": "512x512"},
                },
                headers=OPERATOR_HEADERS,
            )
            assert on.status_code == 200, on.text
            body = on.json()
            assert body["warnings"] == [{"code": "image_check_unset"}]
            assert body["endpoint"] == "images.example.test"
            assert body["params"] == {"adapter": "stub_sync", "default_size": "512x512"}
            assert body["sources"]["params"] == {"adapter": "own", "default_size": "own"}
            await put_key(client, "image_generation", "sk-image-test-0002")

            handle = await images.require_image_adapter(lib.models)
            result = await handle.submit(REQUEST, "attempt-1")
            assert result.attempt_ref == "stub-1"
            assert first.requests[0].idempotency_key is None  # not declared: stripped
            (config,) = first.configs
            assert (config.model, config.base_url) == ("img-1", "https://images.example.test/api")
            assert config.api_key.get_secret_value() == "sk-image-test-0002"
            assert config.params.default_size == "512x512"
            assert (await images.require_image_adapter(lib.models))._live is handle._live

            revision = await put(
                client, "image_generation", {"model": "img-2", "params": {"adapter": "stub_sync"}}
            )
            rebuilt = await images.require_image_adapter(lib.models)
            assert rebuilt.model == "img-2"
            assert lib.models.images.closed == 1
            assert first.closed == 1

            await put(client, "image_check", {"base_url": "http://vision.test/v1", "model": "v"})
            quiet = await client.put(
                PORT,
                json={"model": "img-2", "params": {"adapter": "stub_sync"}},
                headers=OPERATOR_HEADERS,
            )
            assert quiet.json()["warnings"] == []

            before = (await client.get(f"{PORT}", headers=bearer())).json()
            refusals: list[tuple[str, dict[str, Any], str]] = [
                (
                    PORT,
                    {"model": "img-3", "params": {"adapter": "nonesuch"}},
                    "body.params.adapter",
                ),
                (PORT, {"params": {"adapter": "stub_sync"}}, "body.model"),
                (
                    PORT,
                    {"model": "img-3", "params": {"temperature": 0.5}},
                    "body.params.temperature",
                ),
                (
                    PORT,
                    {"model": "img-3", "params": {"default_quality": "ultra"}},
                    "body.params.default_quality",
                ),
                (PORT, {"model": "img-3", "api_key": CANARY}, "body.api_key"),
                (
                    "/admin/model-config/generation",
                    {"params": {"adapter": "stub_sync"}},
                    "body.params.adapter",
                ),
            ]
            for path, refused_body, field in refusals:
                refused = await client.put(path, json=refused_body, headers=OPERATOR_HEADERS)
                assert refused.status_code == 422, (refused_body, refused.text)
                assert refused.json()["error"]["details"]["field"] == field, refused.text
            after = (await client.get(f"{PORT}", headers=bearer())).json()
            assert after == before
            with pytest.raises(InvalidRequest):
                await model_config.write_config(
                    lib.db,
                    "image_generation",
                    keybox=lib.models.keybox,
                    principal="p-operator",
                    params=ModelParams(temperature=0.2),
                )

            off = await put(
                client, "image_generation", {"model": "img-2", "params": {"adapter": None}}
            )
            assert off > revision
            with pytest.raises(images.ImageNotConfigured) as cleared:
                await images.require_image_adapter(lib.models)
            assert cleared.value.details == {"missing": "adapter"}


@pytest.mark.integration
@pytest.mark.part("M8", "NFR-SEC-3")
async def test_c_09__m8_image_key(
    lib: Engine,
    image_settings: Settings,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The image adapter's key, set with ``PUT …/image_generation/api-key``, is stored
    encrypted and read back only as ``***`` plus its 8-hex fingerprint; no log record and no
    ``config_changes`` row holds it (the change row records the fingerprint).

    Complement: the stored ciphertext decrypts to the key in-process, and the key really
    reaches the adapter the row builds, so the empty searches mean absence."""
    caplog.set_level(logging.DEBUG)
    stub = StubImageGenerator(script=[Outputs()])
    with registered("stub_sync", stub):
        async with admin_client(image_settings, lib) as client:
            revision = (await client.get("/admin/model-config", headers=bearer())).json()[
                "config_revision"
            ]
            await put(
                client, "image_generation", {"model": "img-1", "params": {"adapter": "stub_sync"}}
            )
            await put_key(client, "image_generation", CANARY)
            read = await client.get(PORT, headers=bearer())
            listing = await client.get("/admin/model-config", headers=bearer())
            handle = await images.require_image_adapter(lib.models)
            await handle.submit(REQUEST, None)
        fingerprint = model_config.key_fingerprint(CANARY)
        entry = read.json()
        assert (entry["api_key"], entry["api_key_fingerprint"]) == ("***", fingerprint)
        assert entry["sources"]["api_key"] == "own"
        for response in (read, listing):
            assert CANARY not in response.text
        assert stub.configs[0].api_key.get_secret_value() == CANARY
        async with engine.connect() as conn:
            changes = (
                await conn.execute(
                    text(
                        "SELECT setting, old::text AS old, new::text AS new "
                        "FROM config_changes WHERE config_revision > :r ORDER BY change_id"
                    ),
                    {"r": revision},
                )
            ).all()
            stored = await conn.scalar(
                text(
                    "SELECT api_key_ciphertext FROM model_config WHERE purpose = 'image_generation'"
                )
            )
            at_rest = "\n".join(await conn.scalars(text("SELECT m::text FROM model_config m")))
        keyed = [row for row in changes if row.setting == "model_config:image_generation.api_key"]
        assert len(keyed) == 1
        assert fingerprint in keyed[0].new
        assert all(CANARY not in f"{row.old} {row.new}" for row in changes)
        assert CANARY not in at_rest
        assert lib.models.keybox.decrypt(stored, purpose="x").get_secret_value() == CANARY
        assert caplog.records, "nothing was logged, so the log search proves nothing"
        assert CANARY not in log_text(caplog.records)
        assert CANARY not in caplog.text
