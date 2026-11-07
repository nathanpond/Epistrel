"""I-23's M8 parts, and the image adapter's configuration (FR-IMG-1, NFR-SEC-3).

- #254: the ``image_generation`` port's row written through the shared ``/admin/model-config``
  routes, the adapter it names built and rebuilt at runtime, ``not_configured`` when there is
  none, and its key handled like every other model key.
- #255: the bundled OpenRouter adapter's contract, against a stdlib replay of its image API
  (``tests/openrouter_support.py``) serving the synthetic fixtures and, once the owner has
  recorded them, the recorded ones.

Every I-23 M8 part lives in this file (the traceability gate takes one file per test ID and
milestone)."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import errors as api_errors
from epistrel.api.deps import ImageAdapterDep
from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.errors import InvalidRequest
from epistrel.engine import Engine
from epistrel.orchestrator.images import errors as image_errors
from epistrel.orchestrator.images import registry as images
from epistrel.orchestrator.images.adapters import openrouter
from epistrel.orchestrator.images.adapters.openrouter import (
    OPENROUTER_DEDUPES_IDEMPOTENCY_KEY,
    REFERENCES,
    OpenRouterImageGenerator,
)
from epistrel.orchestrator.images.errors import (
    ImageProviderMalformed,
    ImageProviderTimeout,
    ImageProviderUnavailable,
)
from epistrel.orchestrator.images.port import (
    AppliedParameters,
    Capabilities,
    Cost,
    ImageRequest,
    StatusResult,
    SubmitResult,
)
from epistrel.orchestrator.images.references import MediaReferences
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.adapter import ModelParams
from tests.auth_support import auth_settings, bearer
from tests.conformance.test_c_09 import log_text
from tests.image_support import Outputs, StubImageGenerator, registered
from tests.media_support import png, set_media, upload
from tests.model_support import (
    OPERATOR_HEADERS,
    admin_client,
    config_key,
    put,
    put_key,
    restored_model_config,
)
from tests.openrouter_support import API_KEY as OPENROUTER_KEY
from tests.openrouter_support import (
    SYNTHETIC_MODEL,
    FixedReferences,
    Replay,
    fixture,
    needs_recording,
    recorded_calls,
    replay,
    verdict,
)
from tests.openrouter_support import adapter as openrouter_adapter
from tests.openrouter_support import config as openrouter_config

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


# -- the OpenRouter adapter (#255) -------------------------------------------------------------

ENDPOINTS = f"/api/v1/images/models/{SYNTHETIC_MODEL}/endpoints"
GENERATE = "/api/v1/images"
PROMPT = "a lighthouse at dusk, the keeper waving from the gallery"


def _bodies(server: Replay) -> list[Any]:
    return [seen.body for seen in server.requests("POST", GENERATE)]


async def _submit(gen: OpenRouterImageGenerator, request: ImageRequest) -> SubmitResult:
    return await gen.submit(request, "attempt-key-1")


@pytest.mark.part("M8", "FR-IMG-1")
async def test_i_23__m8_openrouter_contract() -> None:
    """The OpenRouter adapter against replayed synthetic fixtures (``synthetic/``):

    - it is registered as the bundled ``openrouter`` adapter and declares ``sync`` submission,
      no ``confirms_completion``, and ``idempotency_keys`` equal to the recorded verdict
      (``False`` until the owner's recording, and no ``Idempotency-Key`` header is sent);
    - the request carries ``model``, the prompt with the negative prompt folded in, ``seed``,
      ``n``, ``size`` and ``quality``; with two endpoints where only ``alpha`` lists ``seed``,
      a seeded request carries ``provider.only = ["alpha"]`` with fallbacks off;
    - each ``b64_json`` is decoded, with width, height and type read from the bytes;
      ``usage.cost`` is the cost, and a missing one is ``unknown``;
    - with no endpoint supporting references, the request has no ``input_references`` and
      ``references_applied = false`` is recorded; where they are supported, they go to those
      endpoints only; with seed and references on disjoint endpoints, references win;
    - a size the provider clamped is recorded at the returned dimensions.

    Complement: an unseeded request carries no ``provider.only``, and parameters no chosen
    endpoint lists are dropped and recorded (FR-IMG-1)."""
    # Registration and declarations.
    assert "openrouter" in images.register_builtin_image_adapters()
    declared = images.check_declaration("openrouter", OpenRouterImageGenerator)
    recorded = verdict()
    expected_dedupe = False if recorded is None else bool(recorded["dedupes"])
    assert declared == Capabilities(
        submission="sync", confirms_completion=False, idempotency_keys=expected_dedupe
    )
    assert OPENROUTER_DEDUPES_IDEMPOTENCY_KEY is expected_dedupe
    assert openrouter.DEFAULT_BASE_URL == "https://openrouter.ai"
    assert OpenRouterImageGenerator(openrouter_config(None)).base_url == "https://openrouter.ai"

    with replay() as server:
        gen = openrouter_adapter(server)
        try:
            server.serve_fixture(fixture("01_endpoints_two.json"))
            two = fixture("04_generation_two.json")
            server.serve_fixture(two)
            seeded = ImageRequest(
                prompt=PROMPT,
                negative_prompt="text, watermarks",
                seed=7,
                n=2,
                size="1024x1024",
                quality="high",
            )
            result = await _submit(gen, seeded)
            [body] = _bodies(server)
            assert body == {
                "model": SYNTHETIC_MODEL,
                "prompt": f"{PROMPT}\n\nAvoid: text, watermarks",
                "seed": 7,
                "n": 2,
                "size": "1024x1024",
                "quality": "high",
                "provider": {"only": ["alpha"], "allow_fallbacks": False},
            }
            [post] = server.requests("POST", GENERATE)
            headers = {k.lower(): v for k, v in post.headers.items()}
            assert headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
            assert headers["x-title"] == "Epistrel"
            assert "http-referer" not in headers
            assert ("idempotency-key" in headers) is OPENROUTER_DEDUPES_IDEMPOTENCY_KEY
            outputs = result.outputs or ()
            assert [(o.media_type, o.width, o.height) for o in outputs] == [
                ("image/png", 32, 24),
                ("image/png", 32, 24),
            ]
            assert [o.width for o in outputs] == two["meta"]["true_width"]
            assert result.cost == Cost(Decimal("0.08"), "USD")
            assert result.attempt_ref == "REDACTED-1"
            assert result.usage is not None
            assert result.usage.images == 2
            assert result.applied_parameters == AppliedParameters(
                endpoint="alpha",
                requested={
                    "n": 2,
                    "seed": 7,
                    "size": "1024x1024",
                    "quality": "high",
                },
                sent={"seed": 7, "n": 2, "size": "1024x1024", "quality": "high"},
                seed_applied=True,
                negative_prompt="folded",
            )

            # Complement: unseeded, so nothing narrows routing; no cost reported.
            server.serve_fixture(fixture("06_generation_no_cost.json"))
            plain = ImageRequest(prompt=PROMPT, aspect_ratio="16:9", quality="low")
            unseeded = await _submit(gen, plain)
            body = _bodies(server)[-1]
            assert "provider" not in body
            assert body == {
                "model": SYNTHETIC_MODEL,
                "prompt": PROMPT,
                "aspect_ratio": "16:9",
                "quality": "low",
            }
            assert unseeded.cost == "unknown"
            assert len(unseeded.outputs or ()) == 1  # the entry without b64_json is skipped
            assert unseeded.applied_parameters is not None
            assert unseeded.applied_parameters.endpoint is None  # two candidates, none named
            assert unseeded.applied_parameters.seed_applied is None
            assert unseeded.applied_parameters.negative_prompt is None
            assert unseeded.attempt_ref == "REDACTED-3"

            # No endpoint supports references: generated without them, and recorded so.
            refs = FixedReferences()
            gen.references = refs
            with_refs = ImageRequest(prompt=PROMPT, n=3, reference_images=("media-1",))
            unreferenced = await _submit(gen, with_refs)
            body = _bodies(server)[-1]
            assert REFERENCES not in body
            assert "provider" not in body
            assert "n" in body  # alpha lists n, so a chosen endpoint does
            assert refs.asked == []
            applied = unreferenced.applied_parameters
            assert applied is not None
            assert applied.references_applied is False
            assert REFERENCES in applied.dropped_parameters
            assert applied.requested[REFERENCES] == ["media-1"]
        finally:
            await gen.aclose()

    with replay() as server:
        refs = FixedReferences()
        gen = openrouter_adapter(server, references=refs)
        try:
            server.serve_fixture(fixture("02_endpoints_refs.json"))
            server.serve(
                "POST", GENERATE, 200, {"id": "g-4", "provider": "delta", "data": [_b64(8, 8)]}
            )
            both = ImageRequest(
                prompt=PROMPT, seed=3, aspect_ratio="1:1", reference_images=("m-1", "m-2")
            )
            result = await _submit(gen, both)
            body = _bodies(server)[-1]
            assert body["provider"] == {"only": ["delta"], "allow_fallbacks": False}
            assert body[REFERENCES] == [
                {"type": "image_url", "image_url": {"url": "https://media.example/m-1.png"}},
                {"type": "image_url", "image_url": {"url": "https://media.example/m-2.png"}},
            ]
            assert body["seed"] == 3
            applied = result.applied_parameters
            assert applied is not None
            assert (applied.endpoint, applied.seed_applied, applied.references_applied) == (
                "delta",
                True,
                True,
            )
            assert applied.sent[REFERENCES] == 2  # a count: never the URLs
            assert "https://" not in json.dumps(applied.model_dump())
        finally:
            await gen.aclose()

    with replay() as server:
        gen = openrouter_adapter(server, references=FixedReferences())
        try:
            server.serve_fixture(fixture("03_endpoints_disjoint.json"))
            server.serve("POST", GENERATE, 200, {"id": "g-5", "data": [_b64(8, 8)]})
            disjoint = ImageRequest(prompt=PROMPT, seed=3, reference_images=("m-1",))
            result = await _submit(gen, disjoint)
            body = _bodies(server)[-1]
            assert body["provider"] == {"only": ["gamma"], "allow_fallbacks": False}
            assert "seed" not in body
            assert REFERENCES in body
            applied = result.applied_parameters
            assert applied is not None
            assert applied.endpoint == "gamma"  # the single endpoint allowed
            assert (applied.seed_applied, applied.references_applied) == (False, True)
            assert applied.dropped_parameters == ("seed",)
        finally:
            await gen.aclose()

    # A clamped size: 2048x2048 asked, 1024x1024 returned, 1024x1024 recorded.
    with replay() as server:
        gen = openrouter_adapter(server)
        try:
            clamped = fixture("05_generation_clamped.json")
            server.serve_fixture(fixture("01_endpoints_two.json"))
            server.serve_fixture(clamped)
            result = await _submit(gen, ImageRequest(prompt=PROMPT, size="2048x2048"))
            assert _bodies(server)[-1]["size"] == "2048x2048"
            [output] = result.outputs or ()
            assert (output.width, output.height) == (1024, 1024)
            assert [output.width] == clamped["meta"]["true_width"]
            assert [output.height] == clamped["meta"]["true_height"]
            # beta served it, and beta lists size: nothing dropped.
            assert result.applied_parameters is not None
            assert result.applied_parameters.endpoint == "beta"
            assert result.applied_parameters.dropped_parameters == ()
        finally:
            await gen.aclose()


@pytest.mark.part("M8", "FR-IMG-1")
async def test_i_23__m8_openrouter_capability_cache() -> None:
    """Endpoint capabilities are cached per model for ``image_capability_ttl_s``: two calls
    within the TTL fetch them once, and a call after it (the clock advanced) fetches them again.
    While a refresh fails, the stale list is served up to twice the TTL; past that, the call
    fails ``ImageProviderUnavailable`` and nothing is generated without routing data
    (FR-IMG-1)."""
    start = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    moment = [start]
    clock.set_clock(lambda: moment[0])
    try:
        with replay() as server:
            gen = openrouter_adapter(server, image_capability_ttl_s=60)
            try:
                server.serve_fixture(fixture("01_endpoints_two.json"))
                server.serve("POST", GENERATE, 200, {"data": [_b64(8, 8)], "usage": {"cost": 0}})
                request = ImageRequest(prompt=PROMPT)
                await _submit(gen, request)
                moment[0] = start + timedelta(seconds=59)
                await _submit(gen, request)
                assert len(server.requests("GET", ENDPOINTS)) == 1
                moment[0] = start + timedelta(seconds=61)
                await _submit(gen, request)
                assert len(server.requests("GET", ENDPOINTS)) == 2
                assert gen.endpoint_fetches == 2

                # The refresh fails: the stale list serves until 2 x TTL after it was fetched.
                server.reset()
                server.serve("GET", ENDPOINTS, 503, {"error": {"code": 503}})
                server.serve("POST", GENERATE, 200, {"data": [_b64(8, 8)], "usage": {"cost": 0}})
                moment[0] = start + timedelta(seconds=61 + 100)
                await _submit(gen, request)
                assert len(server.requests("POST", GENERATE)) == 1
                moment[0] = start + timedelta(seconds=61 + 121)
                with pytest.raises(ImageProviderUnavailable):
                    await _submit(gen, request)
                assert len(server.requests("POST", GENERATE)) == 1
            finally:
                await gen.aclose()
    finally:
        clock.set_clock(None)


@pytest.mark.part("M8", "FR-IMG-1")
async def test_i_23__m8_openrouter_errors() -> None:
    """Each provider status maps to its error (``synthetic/errors.json``): 400/404/413/422 →
    ``ImageRequestInvalid``, 401/403 → ``ImageAdapterAuth``, 402 → ``ImageAdapterCredits``,
    408 → ``ImageProviderTimeout``, 409/429/5xx/524/529 → ``ImageProviderUnavailable`` and a
    redirect → ``ImageProviderMalformed``, with only the last three retryable. A request
    timeout is ``ImageProviderTimeout``, retryable and ambiguous. Malformed answers (not JSON,
    no images, undecodable bytes, a GIF) are ``ImageProviderMalformed``. Discovery: an unknown
    model or no endpoints is ``ImageRequestInvalid``, a failing endpoints API
    ``ImageProviderUnavailable``. No error string carries the prompt, even when the provider
    echoes it, and none chains the transport's exception (NFR-SEC-4)."""
    request = ImageRequest(prompt=PROMPT, negative_prompt="no fog")
    raised: list[image_errors.ImageAdapterError] = []
    with replay() as server:
        gen = openrouter_adapter(server)
        try:
            for case in fixture("errors.json")["cases"]:
                server.reset()
                server.serve_fixture(fixture("01_endpoints_two.json"))
                echoed = json.loads(json.dumps(case["body"]).replace("{prompt}", PROMPT))
                options = {"headers": {"Location": "/elsewhere"}} if case["status"] == 302 else {}
                server.serve("POST", GENERATE, case["status"], echoed, **options)
                with pytest.raises(image_errors.ImageAdapterError) as failure:
                    await _submit(gen, request)
                assert type(failure.value).__name__ == case["raises"], case
                assert failure.value.retryable is case["retryable"], case
                assert failure.value.status == case["status"]
                raised.append(failure.value)
            # The provider's error code is repeated only when it is a short token.
            assert [e.provider_code for e in raised[:2]] == ["invalid_prompt", "404"]

            server.reset()
            server.serve_fixture(fixture("01_endpoints_two.json"))
            server.serve("POST", GENERATE, 200, None, delay_s=1.5)
            gen._client.timeout = httpx.Timeout(0.3, connect=2)
            with pytest.raises(ImageProviderTimeout) as timed_out:
                await _submit(gen, request)
            assert (timed_out.value.retryable, timed_out.value.ambiguous) == (True, True)
            raised.append(timed_out.value)
            gen._client.timeout = httpx.Timeout(30, connect=2)

            gif = base64.b64encode(
                b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01"
                b"\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
            ).decode()
            malformed: list[dict[str, Any]] = [
                {"raw": b"<html>not json</html>"},
                {"body": {"data": []}},
                {"body": {"data": [{"url": "https://example.invalid/a.png"}]}},
                {"body": {"data": [{"b64_json": "!!not base64!!"}]}},
                {"body": {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}},
                {"body": {"data": [{"b64_json": gif}]}},
            ]
            for answer in malformed:
                server.reset()
                server.serve_fixture(fixture("01_endpoints_two.json"))
                server.serve("POST", GENERATE, 200, answer.get("body"), raw=answer.get("raw"))
                with pytest.raises(ImageProviderMalformed) as bad:
                    await _submit(gen, request)
                assert bad.value.retryable
                raised.append(bad.value)
        finally:
            await gen.aclose()

    discovery = [
        (404, {"error": {"code": 404}}, image_errors.ImageRequestInvalid, "model_not_found"),
        (500, {"error": {"code": 500}}, ImageProviderUnavailable, None),
        (200, {"data": {"endpoints": []}}, image_errors.ImageRequestInvalid, "no_endpoints"),
        (401, {"error": {"code": 401}}, image_errors.ImageAdapterAuth, None),
    ]
    for status, body, kind, reason in discovery:
        with replay() as server:
            gen = openrouter_adapter(server)
            try:
                server.serve("GET", ENDPOINTS, status, body)
                with pytest.raises(kind) as failure:
                    await _submit(gen, request)
                assert failure.value.reason == reason
                assert server.requests("POST", GENERATE) == []
                raised.append(failure.value)
            finally:
                await gen.aclose()

    for error in raised:
        text_of = f"{error} {error!r} {error.args}"
        assert PROMPT not in text_of
        assert "no fog" not in text_of
        assert error.__cause__ is None
        assert error.__suppress_context__ or error.__context__ is None


@pytest.mark.part("M8", "FR-IMG-1")
@needs_recording
async def test_i_23__m8_openrouter_recorded() -> None:
    """Against the owner's recorded fixtures (``recorded/``): the endpoints parse with the
    canonical parameter names; each recorded generation, replayed, gets the same request body
    from the adapter that the real API was sent, decodes to images within the 64 px fixture
    bound with their true returned size in ``meta``, and costs what ``usage.cost`` said
    (FR-IMG-1)."""
    recorded = verdict()
    assert recorded is not None
    calls = recorded_calls()
    endpoints_call = next(c for c in calls if c["request"]["url"].endswith("/endpoints"))
    parsed = openrouter.parse_endpoints(endpoints_call["response"]["body"])
    assert parsed, "the recorded model has no endpoints"
    listed = set().union(*(e.parameters for e in parsed))
    assert listed & openrouter.CANONICAL_PARAMETERS, listed
    generations = [
        c for c in calls if c["request"]["method"] == "POST" and c["response"]["status"] == 200
    ]
    assert generations, "no recorded generation succeeded"
    model = str(recorded["model"])
    path = urlsplit(endpoints_call["request"]["url"]).path
    for call in generations:
        sent = call["request"]["body"]
        with replay() as server:
            gen = openrouter_adapter(server, model=model, references=FixedReferences())
            try:
                server.serve("GET", path, 200, endpoints_call["response"]["body"])
                server.serve_fixture(call)
                request = ImageRequest(
                    prompt=sent["prompt"],
                    seed=sent.get("seed"),
                    n=sent.get("n", 1),
                    size=sent.get("size"),
                    aspect_ratio=sent.get("aspect_ratio"),
                    quality=sent.get("quality"),
                    reference_images=tuple(
                        f"ref-{i}" for i in range(len(sent.get(REFERENCES, [])))
                    ),
                )
                result = await gen.submit(request, None)
            finally:
                await gen.aclose()
        [replayed] = server.requests("POST", GENERATE)
        expected = {k: v for k, v in sent.items() if k != REFERENCES}
        assert {k: v for k, v in replayed.body.items() if k != REFERENCES} == expected
        assert len(replayed.body.get(REFERENCES, [])) == len(sent.get(REFERENCES, []))
        outputs = result.outputs or ()
        assert len(outputs) == len(call["meta"]["true_width"])
        for output, width, height in zip(
            outputs, call["meta"]["true_width"], call["meta"]["true_height"], strict=True
        ):
            assert output.width is not None
            assert output.height is not None
            assert max(output.width, output.height) <= 64
            assert width >= output.width
            assert height >= output.height
        assert result.cost == openrouter.cost_of(call["response"]["body"].get("usage"))


@pytest.mark.part("M8", "FR-IMG-1")
@needs_recording
def test_i_23__m8_openrouter_dedupe_flag() -> None:
    """``OPENROUTER_DEDUPES_IDEMPOTENCY_KEY`` matches the recorded verdict
    (``tests/fixtures/openrouter/verdict.json``), and the adapter declares
    ``idempotency_keys`` only when OpenRouter was seen deduplicating a repeated key
    (FR-IMG-1)."""
    recorded = verdict()
    assert recorded is not None
    assert OPENROUTER_DEDUPES_IDEMPOTENCY_KEY is bool(recorded["dedupes"])
    assert OpenRouterImageGenerator.capabilities().idempotency_keys is bool(recorded["dedupes"])
    if recorded.get("inconclusive"):
        assert recorded["dedupes"] is False


def _b64(width: int, height: int) -> dict[str, str]:
    return {"b64_json": base64.b64encode(png(width, height)).decode()}


@pytest.mark.integration
async def test_openrouter_reference_urls(lib: Engine, image_settings: Settings) -> None:
    """A reference image reaches the provider as a signed URL when ``public_base_url`` is a
    public host, and as a data URL when it is unset or a loopback or private host. An id with
    no asset is refused before anything is sent."""
    references = lib.models.images.references
    assert isinstance(references, MediaReferences)
    async with admin_client(image_settings, lib) as client:
        image = (await upload(client, png(), headers=MEDIA_WRITE)).json()
        try:
            unset = await references.resolve(image["id"])
            assert unset.startswith("data:image/png;base64,")
            await set_media(client, public_base_url="http://127.0.0.1:8000")
            assert (await references.resolve(image["id"])).startswith("data:image/png;")
            await set_media(client, public_base_url="https://epistrel.example/base")
            signed = await references.resolve(image["id"])
            assert signed.startswith(f"https://epistrel.example/base/media/{image['id']}/content?")
            assert "sig=" in signed
            assert "exp=" in signed
        finally:
            await set_media(client, public_base_url=None)
    with pytest.raises(image_errors.ImageRequestInvalid) as missing:
        await references.resolve("01JZZZZZZZZZZZZZZZZZZZZZZZ")
    assert missing.value.reason == "reference_not_found"
