"""I-23's M8 parts, and the image adapter's configuration (FR-IMG-1, NFR-SEC-3).

- #254: the ``image_generation`` port's row written through the shared ``/admin/model-config``
  routes, the adapter it names built and rebuilt at runtime, ``not_configured`` when there is
  none, and its key handled like every other model key.
- #255: the bundled OpenRouter adapter's contract, against a stdlib replay of its image API
  (``tests/openrouter_support.py``) serving the synthetic fixtures and, once the owner has
  recorded them, the recorded ones.
- #259: a catalog character's image prompt built from perceivable inputs and returned without
  generating (``POST /image-prompts``), and its base appearance prompt drafted
  (``tests/image_prompt_support.py``).
- #263: story images generated from what their audience viewpoint perceived, through the real
  story target (``tests/story_image_generation_support.py``): the whisper scene, Lite's delivered
  message, the stored prompt, the rating's guidance and the style, and the camera POV.
- #269: a story's image usage and cost, reported from its records by ``story_id``.

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
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.engine import Engine
from epistrel.orchestrator.images import errors as image_errors
from epistrel.orchestrator.images import jobs as image_jobs
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
from epistrel.orchestrator.images.jobs import ImageJobRequest, PreparedImageJob
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
from epistrel.testing.images import (
    PASS,
    ScriptedChecks,
    TestImageTarget,
    installed_checks,
    registered_target,
)
from tests import catalog_image_support as cs
from tests import image_prompt_support as ip
from tests import story_image_generation_support as sg
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import BASE, read, save
from tests.conformance.test_c_09 import log_text
from tests.image_prompt_support import prompt_app
from tests.image_support import Outputs, StubImageGenerator, catalog_origin, registered
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
from tests.story_support import WRITE

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


# -- the catalog prompt builder (#259) ------------------------------------------------------


class _CandidateTarget(TestImageTarget):
    """The test target as a catalog job's (#260 delivers the real one)."""

    success_status = ImageJobStatus.CANDIDATES_READY

    async def result_ids(self, conn: Any, job: Any) -> tuple[str, list[str]]:
        return "candidate_ids", list(job.asset_ids)


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-2; FR-CAT-4; FR-RATE-4")
async def test_i_23__m8_catalog_prompt(image_settings: Settings, engine: AsyncEngine) -> None:
    """A 34-year-old character draft with a visual identity (a ``perceivable`` and a
    ``restricted`` reference image, a restricted-details prompt, an add-on of each audience)
    and a companion visual style. ``POST /image-prompts`` for its primary image answers the
    prompt and its parameters. The prompt states the band ("mid thirties"), never the age, and
    holds the style. The reference ids list only the perceivable image. The seed is the
    identity's, the negative prompt is the identity's terms then the style's (deduplicated),
    and the ratio is the primary image's 1:1 with no adapter configured. The rendered
    ``image_prompt`` input carries the band, the style and the rating's image guidance as
    fixed sections. Nothing is generated: no job, reservation, cost or media row.

    Complement: the restricted reference, the restricted-details prompt, the restricted add-on
    and the true age reach neither the captured rendered input nor the answer. The same
    prompt and parameters, sent through an image job, reach the stub image adapter with the
    identity's seed and the perceivable reference only."""
    async with prompt_app(image_settings, engine) as (client, lib):
        stub = lib.models.stub
        seen, hidden = await ip.references(client)
        created = await ip.create_character(client, ip.character_body(seen, hidden))
        character_id = created["id"]
        await ip.style_companion(client, character_id)
        before = await ip.image_rows(engine)

        found = await ip.built(client, "character", character_id, "primary", draft=True)
        assert found["target"] == {
            "kind": "character",
            "id": character_id,
            "version": None,
            "draft_revision": created["draft_revision"],
        }
        assert ip.BAND in found["prompt"]
        assert ip.STYLE in found["prompt"]
        assert ip.NEVER.search(found["prompt"]) is None
        assert found["reference_image_ids"] == [seen]
        assert found["seed"] == ip.SEED
        assert found["negative_prompt"] == "text, watermark, Blurry, lens flare"
        assert (found["size"], found["aspect_ratio"]) == (None, "1:1")
        assert set(found) == {
            "prompt",
            "negative_prompt",
            "seed",
            "reference_image_ids",
            "size",
            "aspect_ratio",
            "target",
            "depicted_subjects",
        }
        # FR-IMG-9 (#266): a catalog character's images depict the character itself.
        assert found["depicted_subjects"] == [{"kind": "catalog_character", "id": character_id}]
        assert await ip.image_rows(engine) == before

        [call] = ip.prompt_calls(stub)
        sent = ip.rendered(call)
        for section in ("## Purpose", "## Subjects", "## Scene", "## Visual style"):
            assert section in sent
        assert "## Rating image guidance\nAll-ages imagery only." in sent
        assert f"apparent age: {ip.BAND}" in sent
        assert ip.STYLE in sent
        assert ip.BASE_PROMPT in sent
        assert "copper braid" in sent
        assert ip.NEVER.search(sent) is None
        for absent in (seen, hidden, str(ip.SEED), "watermark", "harbour-light-lora"):
            assert absent not in sent

        # The same prompt and parameters through a job: the adapter receives the identity's
        # seed and the perceivable reference only.
        generator = StubImageGenerator(script=[Outputs()])
        with (
            registered("stub_sync", generator),
            registered_target(_CandidateTarget(), ImageJobKind.CATALOG),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            async with admin_client(image_settings, lib) as admin:
                await put(
                    admin,
                    "image_generation",
                    {"model": "img-1", "params": {"adapter": "stub_sync"}},
                )
                await put_key(admin, "image_generation", "sk-image-test-0001")
            job = PreparedImageJob(
                ImageJobKind.CATALOG,
                {},
                catalog_origin(character_id),
                ImageJobRequest(
                    prompt=found["prompt"],
                    negative_prompt=found["negative_prompt"],
                    seed=found["seed"],
                    aspect_ratio=found["aspect_ratio"],
                    reference_image_ids=tuple(found["reference_image_ids"]),
                ),
                "p-catalog",
            )
            async with engine.begin() as conn:
                job_id = (await image_jobs.insert_image_job(conn, job, None)).job_id
            done = await image_jobs.run_image_job(lib.image_runtime(), job_id, 0)
            assert done["image_job"] == "candidates_ready", done
        [received] = generator.requests
        assert received.request.seed == ip.SEED
        assert received.request.reference_images == (seen,)
        assert received.request.prompt == found["prompt"]
        assert ip.NEVER.search(received.request.model_dump_json(exclude={"seed"})) is None


@pytest.mark.integration
@pytest.mark.part("M8", "FR-CAT-4")
async def test_i_23__m8_draft_base_prompt(image_settings: Settings, engine: AsyncEngine) -> None:
    """``POST …/draft/visual-identity/draft-base-prompt`` drafts a base appearance prompt from
    the draft's appearance (its authored age left out) and answers it with the draft's
    revision, saving nothing: the draft is unchanged until an ordinary draft save stores the
    (edited) text, which a ``full`` read then shows. A drafted text that states the true age or
    uses a restricted-details term is regenerated once; a second such text is 503
    ``image_prompt_unavailable``. A stale ``draft_revision`` is 409 ``draft_stale``; a draft
    with no appearance is 422 at ``definition.appearance``.

    Complement: the drafting call's input holds no age, no band, no style, no guidance and
    no restricted detail."""
    async with prompt_app(image_settings, engine) as (client, lib):
        stub = lib.models.stub
        seen, hidden = await ip.references(client)
        body = ip.character_body(seen, hidden)
        body["definition"]["appearance"] = "A 34-year-old woman with a copper braid."
        created = await ip.create_character(client, body)
        character_id, revision = created["id"], created["draft_revision"]
        await ip.style_companion(client, character_id)
        url = f"{BASE}/{character_id}/draft/visual-identity/draft-base-prompt"

        ip.answer(stub, "tall woman, copper braid, 34", "tall woman, copper braid, sailcloth")
        drafted = await client.post(url, json={"draft_revision": revision}, headers=ip.HEADERS)
        assert drafted.status_code == 200, drafted.text
        assert drafted.json() == {
            "base_appearance_prompt": "tall woman, copper braid, sailcloth",
            "draft_revision": revision,
        }
        calls = ip.prompt_calls(stub)
        assert len(calls) == 2
        for call in calls:
            sent = ip.rendered(call)
            assert "A woman with a copper braid." in sent
            assert ip.NEVER.search(sent) is None
            for absent in (ip.BAND, ip.STYLE, "All-ages imagery", "Visual style"):
                assert absent not in sent
        unchanged = await read(client, character_id, draft="true", projection="full")
        assert unchanged.json()["draft_revision"] == revision
        assert unchanged.json()["visual_identity"]["base_appearance_prompt"] == ip.BASE_PROMPT

        edited = "tall woman, copper braid, sailcloth, salt-stained boots"
        saved = await save(
            client, character_id, revision, visual_identity={"base_appearance_prompt": edited}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["visual_identity"]["base_appearance_prompt"] == edited

        ip.answer(stub, f"tall woman, {ip.RESTRICTED_TERM} tattoo", "a 34 year old woman")
        refused = await client.post(url, headers=ip.HEADERS)
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["code"] == "image_prompt_unavailable"
        assert refused.json()["error"]["details"] == {
            "purpose": "image_prompt",
            "reason": "invalid_output",
        }
        stale = await client.post(url, json={"draft_revision": revision}, headers=ip.HEADERS)
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "draft_stale"

        bare = await ip.create_character(client, {"public_card": {"name": "Nobody"}})
        missing = await client.post(
            f"{BASE}/{bare['id']}/draft/visual-identity/draft-base-prompt", headers=ip.HEADERS
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["details"]["field"] == "definition.appearance"


# -- catalog candidates (#260) ------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-4,7")
async def test_i_23__m8_candidate_metadata(image_settings: Settings, engine: AsyncEngine) -> None:
    """A catalog candidate carries its full generation metadata (adapter, model, prompt,
    negative prompt, requested and applied parameters with the seed, reference ids, usage and
    cost) and its check, and the job's origin records the draft revision and image-input
    fingerprint it was requested from. It is attached only on request: the draft's images are
    unchanged by the job, and the save naming it puts its media id in the slot.

    Complement asserted: until that save, the draft has no primary image and the card shows
    none."""
    async with cs.candidate_app(image_settings, engine) as app:
        created = await cs.character(app)
        character_id, revision = created["id"], created["draft_revision"]
        job_id, [candidate_id] = await cs.generated(app, character_id, Outputs(n=1, size=(79, 45)))
        [item] = (await cs.listed(app, character_id))["items"]
        metadata = item["media"]["generation_metadata"]
        assert (metadata["adapter"], metadata["model"]) == (cs.ADAPTER, "img-1")
        assert ip.BAND in metadata["prompt"]
        assert metadata["negative_prompt"]
        assert metadata["applied_parameters"]["seed"] == ip.SEED
        assert metadata["requested_parameters"]["n"] == 1
        assert "usage" in metadata
        assert "cost" in metadata
        assert item["media"]["check"]["model"] == "stub-vision"
        assert (item["slot"], item["job_id"], item["draft_revision"]) == (
            "primary",
            job_id,
            revision,
        )
        async with engine.connect() as conn:
            origin = await conn.scalar(
                text("SELECT origin FROM image_jobs WHERE id = :j"), {"j": job_id}
            )
        assert origin["draft_revision"] == revision
        assert origin["image_input_fingerprint"].startswith("v1:")
        assert (await cs.draft(app, character_id))["images"]["primary"] is None

        saved = await cs.save(
            app,
            character_id,
            revision,
            public_card={"images": {"primary": {"candidate_id": candidate_id}}},
        )
        assert saved.status_code == 200, saved.text
        primary = (await cs.draft(app, character_id))["images"]["primary"]
        assert primary["generation_metadata"] == metadata


# -- #261: story images at narrative positions for an audience viewpoint ---------------------


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-5")
async def test_i_23__m8_story_images(
    image_settings: Settings, engine: AsyncEngine, tmp_path: Path
) -> None:
    """In the whisper scene, an image uploaded after turn 2's player beat for Bobby is in
    Bobby's history right after that beat and absent from Jenni's; an ``omniscient`` image
    reaches only ``omniscient`` reads (and an omniscient read gets no Bobby image). Truncating
    turn 2 takes Bobby's image away and untruncating brings it back; a regenerate makes it
    dormant and a swap back reactivates it; a fork carries it by reference (a new image id, the
    same asset). An inter-turn slot upload is in place after its turn. A deleted image leaves
    every read (and a repeat delete is 204). Export then import shows the image in place in the
    new story; a prune of a truncated tail removes its image rows (complement: the active image
    remains); an as-recorded read at an earlier checkpoint omits a later image."""
    from tests import story_image_support as si
    from tests.respond_support import respond_client
    from tests.story_support import READ, WRITE

    async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await client.post(path, json=body, headers=WRITE)
        assert response.status_code in (200, 201), response.text
        found: dict[str, Any] = response.json()
        return found

    async with respond_client(image_settings, engine) as (client, epistrel):
        story, ids = await si.scene(client, turns=2)
        bobby, jenni = si.character(ids[si.BOBBY]), si.character(ids[si.JENNI])
        for_bobby = (await si.placed(client, story, si.meta({"turn": 2, "beat": 1}, bobby)))[
            "image_id"
        ]
        whole = (await si.placed(client, story, si.meta({"turn": 1, "beat": 2}, si.OMNISCIENT)))[
            "image_id"
        ]
        bobby_items = await si.history(client, story, bobby)
        assert si.image_ids(bobby_items) == [for_bobby]
        before = si.after(bobby_items, for_bobby)
        assert before is not None
        assert (before["turn"], before["beat"]) == (2, 1)
        assert bobby_items[-1]["kind"] == "message_rendered"  # the reply follows the image
        assert si.image_ids(await si.history(client, story, jenni)) == []
        omniscient = await si.history(client, story, si.OMNISCIENT)
        assert si.image_ids(omniscient) == [whole]
        before = si.after(omniscient, whole)
        assert before is not None
        assert (before["turn"], before["beat"]) == (1, 2)
        assert (await si.get(client, story, for_bobby, bobby)).status_code == 200
        for other in (jenni, si.OMNISCIENT):
            assert (await si.get(client, story, for_bobby, other)).status_code == 404

        # An inter-turn slot after the last completed turn sits after that turn's items.
        slot = (await si.placed(client, story, si.meta({"slot_after_turn": 2}, si.OMNISCIENT)))[
            "image_id"
        ]
        omniscient = await si.history(client, story, si.OMNISCIENT)
        assert si.image_ids(omniscient) == [whole, slot]
        assert omniscient[-1]["image_id"] == slot

        # As recorded: the latest checkpoint (2⁺1) holds both; turn 2's checkpoint committed
        # before either was uploaded.
        async def recorded(**params: Any) -> list[str]:
            response = await client.get(
                f"/stories/{story}/history",
                params={"viewpoint": "omniscient", "view": "as_recorded", **params},
                headers=READ,
            )
            assert response.status_code == 200, response.text
            return si.image_ids(response.json()["items"])

        assert await recorded() == [whole, slot]
        assert await recorded(as_of="2") == []

        # Truncate and untruncate.
        cut = await post(f"/stories/{story}/truncate", {"to_turn": 1})
        assert si.image_ids(await si.history(client, story, bobby)) == []
        assert si.image_ids(await si.history(client, story, si.OMNISCIENT)) == [whole]
        await post(f"/stories/{story}/untruncate", {"truncate_operation_id": cut["operation_id"]})
        assert si.image_ids(await si.history(client, story, bobby)) == [for_bobby]
        assert si.image_ids(await si.history(client, story, si.OMNISCIENT)) == [whole, slot]

        # Regenerate turn 2 (its variant 1 goes dormant with the image), then swap back.
        await post(f"/stories/{story}/regenerate", {})
        assert si.image_ids(await si.history(client, story, bobby)) == []
        await post(f"/stories/{story}/swap", {"to_variant_chain": {"turn": 2, "variant": 1}})
        assert si.image_ids(await si.history(client, story, bobby)) == [for_bobby]

        # A fork copies the rows by reference: new image ids, the same assets.
        forked = await post(f"/stories/{story}/fork", {"at_turn": 2})
        fork_id = forked["story_id"]
        fork_bobby = si.character(
            next(c["id"] for c in forked["characters"] if c["name"] == si.BOBBY)
        )
        copied = [i for i in await si.history(client, fork_id, fork_bobby) if i.get("kind")]
        copied = [i for i in copied if i["kind"] == "image"]
        assert len(copied) == 1
        assert copied[0]["image_id"] != for_bobby
        source_media = next(
            i["media_id"]
            for i in await si.history(client, story, bobby)
            if i.get("kind") == "image"
        )
        assert copied[0]["media_id"] == source_media

        # Delete: gone from every read, a repeat is 204, the asset stays live.
        assert (await si.delete(client, story, for_bobby)).status_code == 204
        assert si.image_ids(await si.history(client, story, bobby)) == []
        assert (await si.get(client, story, for_bobby, bobby)).status_code == 404
        assert (await si.delete(client, story, for_bobby)).status_code == 204
        async with engine.connect() as conn:
            state = await conn.scalar(
                text("SELECT state FROM media_assets WHERE id = :m"), {"m": source_media}
            )
        assert state == "live"

        # Export, then import: the omniscient images in place in the new story, new assets.
        data = await si.exported(client, engine, story, tmp_path, blobs=epistrel._blob_bytes)
        new_story = await si.imported(client, engine, data)
        brought = [
            i
            for i in await si.history(client, new_story, si.OMNISCIENT)
            if i.get("kind") == "image"
        ]
        assert [(i["turn"], i.get("beat"), i.get("slot_after_turn")) for i in brought] == [
            (1, 2, None),
            (2, None, 2),
        ]
        assert {i["image_id"] for i in brought}.isdisjoint({whole, slot})

        # Prune a truncated tail: its image rows go; the active image (turn 1) remains.
        await post(f"/stories/{story}/truncate", {"to_turn": 1})
        pruned = await post(f"/stories/{story}/prune", {"ignore_warnings": True})
        assert pruned.get("applied", True) is not False, pruned
        async with engine.connect() as conn:
            left = set(
                await conn.scalars(
                    text("SELECT id FROM story_images WHERE story_id = :s"), {"s": story}
                )
            )
        assert whole in left
        assert for_bobby not in left
        assert slot not in left
        assert si.image_ids(await si.history(client, story, si.OMNISCIENT)) == [whole]


@pytest.mark.part("M8", "FR-IMG-5")
async def test_i_23__m8_story_image_anchoring(
    image_settings: Settings, engine: AsyncEngine
) -> None:
    """An image after a beat of turn N survives truncate-to-N and goes with truncate-to-N-1; a
    slot image after N goes with truncate-to-N. A character-viewpoint read carries no
    ``depicted_subjects``; complement: the omniscient read does."""
    from tests import story_image_support as si
    from tests.respond_support import respond_client
    from tests.story_support import WRITE

    async def _truncate(client: Any, story_id: str, to_turn: int) -> None:
        response = await client.post(
            f"/stories/{story_id}/truncate", json={"to_turn": to_turn}, headers=WRITE
        )
        assert response.status_code == 200, response.text

    async with respond_client(image_settings, engine) as (client, _):
        story_id, ids = await si.scene(client, turns=2)
        subjects = [{"kind": "character", "id": ids[si.JENNI]}]
        beat = await si.placed(
            client,
            story_id,
            si.meta({"turn": 2, "beat": 1}, si.character(ids[si.BOBBY]), subjects=subjects),
        )
        slot = await si.placed(
            client, story_id, si.meta({"slot_after_turn": 2}, si.OMNISCIENT, subjects=subjects)
        )
        bobby = await si.history(client, story_id, si.character(ids[si.BOBBY]))
        assert si.image_ids(bobby) == [beat["image_id"]]
        shown = next(i for i in bobby if i.get("kind") == "image")
        assert "depicted_subjects" not in shown
        omniscient = await si.history(client, story_id, si.OMNISCIENT)
        assert si.image_ids(omniscient) == [slot["image_id"]]
        shown = next(i for i in omniscient if i.get("kind") == "image")
        assert shown["depicted_subjects"] == subjects
        assert shown["slot_after_turn"] == 2
        assert "beat" not in shown

        await _truncate(client, story_id, 2)  # the head: only its slots go
        assert si.image_ids(await si.history(client, story_id, si.OMNISCIENT)) == []
        assert si.image_ids(await si.history(client, story_id, si.character(ids[si.BOBBY]))) == [
            beat["image_id"]
        ]
        await _truncate(client, story_id, 1)
        assert si.image_ids(await si.history(client, story_id, si.character(ids[si.BOBBY]))) == []


# -- #263: generated story images (FR-IMG-2, FR-IMG-5, FR-IMG-8) ---------------------------------


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-2,5,8")
async def test_i_23__m8_story_generation(image_settings: Settings, engine: AsyncEngine) -> None:
    """In the whisper scene, an image for Bobby after Jenni's whisper (turn 1, beat 2): the
    ``image_prompt`` input describes the whisper as an act and holds none of its words, the
    adapter is asked for one image (``n = 1``), and the promoted image is in Bobby's history right
    after that beat, stored ``source: generated``, ``viewpoint_basis: filtered_inputs`` on both
    its row and its asset, with the stored prompt and job, under a ``story_image_added`` event of
    the operation minted at enqueue. Under Lite, the scene is built from the delivered message:
    after the reply the template's beats hold the message the player was shown, as it was shown;
    after the player's input (beat 1) only the input.

    Complement asserted: Jenni's history holds no image; the Lite image after beat 1 holds no
    part of the message."""
    from tests import story_image_support as si

    async with sg.generation_app(image_settings, engine) as app:
        scene = await sg.whisper(app, engine)
        story, ids = scene.story_id, scene.ids
        bobby = sg.character(ids["Bobby"])
        job = await sg.generated(
            app, story, position={"turn": 1, "beat": 2}, audience_viewpoint=bobby
        )
        [sent] = [sg.payload(c) for c in app.prompt_calls()]
        assert "- Jenni whispers something to Marcus" in sent
        assert "blue mat" not in sent
        assert sg.LEAN not in sent  # beat 3, after the position
        [received] = app.generator.requests
        assert received.request.n == 1
        assert job["params"]["n"] == 1

        [row] = await sg.story_image_rows(engine, story)
        assert (row["source"], row["viewpoint_basis"]) == ("generated", "filtered_inputs")
        assert (row["asset_source"], row["asset_basis"]) == ("generated", "filtered_inputs")
        assert (row["prompt"], str(row["job_id"])) == (job["prompt"], str(job["id"]))
        assert str(row["operation_id"]) == job["origin"]["operation_id"]
        assert row["audience_viewpoint"] == bobby
        assert row["camera_pov"] == {"kind": "third_person"}
        assert {s["id"] for s in row["asset_subjects"]} >= {ids["Jenni"], ids["Marcus"]}
        async with engine.connect() as conn:
            event = await conn.scalar(
                text(
                    "SELECT operation_id FROM events WHERE story_id = :s "
                    "AND type = 'story_image_added'"
                ),
                {"s": story},
            )
        assert str(event) == job["origin"]["operation_id"]

        items = await si.history(app.client, story, bobby)
        [image_id] = si.image_ids(items)
        before = si.after(items, image_id)
        assert before is not None
        assert (before["turn"], before["beat"]) == (1, 2)
        assert si.image_ids(await si.history(app.client, story, sg.character(ids["Jenni"]))) == []

        # Lite: the scene comes from the delivered message.
        lite, cast = await si.scene(app.client, turns=1)
        player = sg.character(cast[si.BOBBY])
        items = await si.history(app.client, lite, player)
        [message] = [i["text"] for i in items if i["kind"] == "message_rendered"]
        before_calls = len(app.prompt_calls())
        await sg.queued(app, lite, position={"turn": 1, "beat": 2}, audience_viewpoint=player)
        await sg.queued(app, lite, position={"turn": 1, "beat": 1}, audience_viewpoint=player)
        after_reply, after_input = (sg.payload(c) for c in app.prompt_calls()[before_calls:])
        assert f"\n- {message}\n" in after_reply
        assert "Turn 1: who is there?" in after_reply
        assert "Turn 1: who is there?" in after_input
        assert message not in after_input


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-8")
async def test_i_23__m8_story_stored_prompt(image_settings: Settings, engine: AsyncEngine) -> None:
    """The adapter receives byte for byte the prompt stored on the job at enqueue: the worker
    never rebuilds it, and the ``image_prompt`` model is called once per job. A built prompt
    that hits the protected match is rewritten once, with the fixed instruction and never the
    hit term; one that still hits is 422 ``viewpoint_protected_content`` with no job. A consumer
    prompt carrying a 4-gram of Jenni's whisper is refused for Bobby.

    Complement asserted: a second job calls the prompt model again; the 4-gram is accepted for
    ``omniscient``."""
    from epistrel.orchestrator.images.prompt_builder import REWRITE_INSTRUCTION

    async with sg.generation_app(image_settings, engine) as app:
        scene = await sg.whisper(app, engine)
        story, ids = scene.story_id, scene.ids
        bobby = sg.character(ids["Bobby"])
        at = {"turn": 1, "beat": 3}
        first = await sg.queued(app, story, position=at, audience_viewpoint=bobby)
        assert len(app.prompt_calls()) == 1
        stored = (await sg.job_row(engine, first))["prompt"]
        done = await sg.run(app, first)
        assert done["image_job"] == "promoted", done
        assert len(app.prompt_calls()) == 1  # the worker never calls the prompt model
        assert app.adapter_prompts() == [stored]
        await sg.queued(app, story, position=at, audience_viewpoint=bobby)
        assert len(app.prompt_calls()) == 2

        # A leaking answer is rewritten once with the fixed instruction.
        leak = json.dumps({"prompt": "Jenni whispers that the key is under the blue mat."})
        clean = json.dumps({"prompt": "A woman leans close to a sailor in a lamplit parlor."})
        app.stub.when(("## What the viewer perceived", REWRITE_INSTRUCTION), clean, sg.PURPOSE)
        app.stub.when(("## What the viewer perceived",), leak, sg.PURPOSE)
        before = len(app.prompt_calls())
        rewritten = await sg.queued(app, story, position=at, audience_viewpoint=bobby)
        first_try, second_try = (sg.payload(c) for c in app.prompt_calls()[before:])
        assert REWRITE_INSTRUCTION not in first_try
        assert REWRITE_INSTRUCTION in second_try
        assert "blue mat" not in second_try
        assert (await sg.job_row(engine, rewritten))["prompt"].startswith("A woman leans close")

        # One that still leaks is refused, with nothing written.
        app.stub.reset()
        app.stub.when(("## What the viewer perceived",), leak, sg.PURPOSE)
        held = await sg.counts(engine, story)
        refused = await sg.request(app, story, position=at, audience_viewpoint=bobby)
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "viewpoint_protected_content"
        assert refused.json()["error"]["details"] == {"field": "prompt"}
        assert len(app.prompt_calls()) == 2
        assert await sg.counts(engine, story) == held

        # A consumer prompt with a 4-gram of the whisper.
        consumer: dict[str, Any] = {
            "prompt": "A note: under the blue mat.",
            "depicted_subjects": [],
        }
        refused = await sg.request(app, story, position=at, audience_viewpoint=bobby, **consumer)
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"] == {"field": "body.prompt"}
        assert await sg.counts(engine, story) == held
        accepted = await sg.request(
            app, story, position=at, audience_viewpoint="omniscient", **consumer
        )
        assert accepted.status_code == 202, accepted.text


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-2; FR-RATE-4")
async def test_i_23__m8_story_guidance(image_settings: Settings, engine: AsyncEngine) -> None:
    """Every story image prompt carries the story rating's image guidance and the scenario's
    visual style: in the captured adapter request of a built prompt, and of a consumer prompt,
    where both follow the consumer's text as sections of their own; the style's negative prompt
    is the request's negative.

    Complement asserted: neither the style nor the image guidance is in any text-generation
    prompt of the story."""
    from epistrel.core.seeds.ratings import EVERYONE_ID, SEED_RATINGS

    guidance = next(r.image_guidance for r in SEED_RATINGS if r.id == EVERYONE_ID)
    async with sg.generation_app(image_settings, engine) as app:
        scene = await sg.whisper(app, engine)
        story, ids = scene.story_id, scene.ids
        at = {"turn": 1, "beat": 3}
        await sg.generated(app, story, position=at, audience_viewpoint=sg.character(ids["Bobby"]))
        text_prompt = "Two figures by a window at dusk."
        await sg.generated(
            app,
            story,
            position=at,
            audience_viewpoint=sg.character(ids["Bobby"]),
            prompt=text_prompt,
            depicted_subjects=[{"kind": "character", "id": ids["Jenni"]}],
        )
        built, consumer = (r.request for r in app.generator.requests)
        for request in (built, consumer):
            assert sg.STYLE in request.prompt
            assert guidance.strip() in request.prompt
            assert request.negative_prompt is not None
            assert sg.STYLE_NEGATIVE in request.negative_prompt
        assert consumer.prompt == (
            f"{text_prompt}\n\nVisual style: {sg.STYLE}\n\nImage guidance: {guidance.strip()}"
        )
        text_calls = [c for c in app.stub.calls if c.purpose != sg.PURPOSE]
        assert text_calls
        sent = "\n".join(sg.payload(c) for c in text_calls)
        assert sg.STYLE not in sent
        assert "All-ages imagery only" not in sent


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-5")
async def test_i_23__m8_story_camera_pov(image_settings: Settings, engine: AsyncEngine) -> None:
    """A camera POV is framing only: with it omitted the image is ``third_person``; an
    ``over_the_shoulder`` of Jenni, present and perceived by Bobby, changes only the template's
    framing line. A character camera on the guard (behind the curtain: not perceived by Bobby)
    or on a character of another story is 422 ``camera_pov_not_present`` with no job.

    Complement asserted: the two inputs differ in exactly one line."""
    async with sg.generation_app(image_settings, engine) as app:
        scene = await sg.whisper(app, engine)
        story, ids = scene.story_id, scene.ids
        bobby = sg.character(ids["Bobby"])
        at = {"turn": 1, "beat": 3}
        await sg.queued(app, story, position=at, audience_viewpoint=bobby)
        await sg.queued(
            app,
            story,
            position=at,
            audience_viewpoint=bobby,
            camera_pov={"kind": "over_the_shoulder", "character_id": ids["Jenni"]},
        )
        default, shoulder = (sg.payload(c).splitlines() for c in app.prompt_calls())
        assert "third-person view of the scene" in default
        changed = [(a, b) for a, b in zip(default, shoulder, strict=True) if a != b]
        assert len(changed) == 1, changed
        assert changed[0] == ("third-person view of the scene", "over the shoulder of person 2")

        held = await sg.counts(engine, story)
        for character_id in (ids["Guard"], "01J9Z8Q4N5V6W7X8Y9Z0A1B2C6"):
            refused = await sg.request(
                app,
                story,
                position=at,
                audience_viewpoint=bobby,
                camera_pov={"kind": "first_person", "character_id": character_id},
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == "camera_pov_not_present"
        assert await sg.counts(engine, story) == held
        assert len(app.prompt_calls()) == 2


# -- #265: automatic illustration (FR-IMG-6) ------------------------------------------------------


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-6")
async def test_i_23__m8_auto(image_settings: Settings, engine: AsyncEngine) -> None:
    """Automatic illustration in a Lite story, with the stub model, image adapter and checks.

    - With the setting omitted (``on_request``), a reply ending in an illustrate tag queues
      nothing (``pending_images: []``) and its prompt asks for no tag; the tag is stripped all
      the same.
    - Under ``auto``, a new-location reply queues one job, and the response lists it at the
      reply beat. The job is ``system``'s, keyed ``auto:<operation_id>:0``, for the turn's
      output viewpoint, ``third_person``, with its ``jobs`` row and no reservation yet. Run, it
      builds its prompt, promotes, and the image is in history right after the reply.
    - The turn's model calls are the same as without automatic illustration (one
      ``generation``: the flag rides the single Lite prompt).
    - A respond replayed under the same key answers the same jobs and queues none.
    - Regenerating the turn queues the new variant's own job; the old one ends ``stale``
      (``target_inactive``, ``regenerated``) at #264's revalidation. A one-time ``correct``
      (ADR-174) queues the corrected turn's own job the same way.

    Complement asserted: an untagged ``auto`` reply queues nothing."""
    from epistrel.orchestrator.images.auto import LITE_ILLUSTRATE_INSTRUCTION
    from tests import auto_illustration_support as ai
    from tests import story_image_support as si

    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await ai.lite_story(app)
        bobby = ids[si.BOBBY]
        viewpoint = sg.character(bobby)

        off = await ai.lite_turn(app, story, bobby)
        assert off.status_code == 200, off.text
        assert off.json()["pending_images"] == []
        assert off.json()["message"]["text"] == ai.REPLY
        assert LITE_ILLUSTRATE_INSTRUCTION not in ai.generation_prompts(app)[-1]
        assert await ai.auto_jobs(engine, story) == []

        await ai.set_auto(app.client, story)
        untagged = await ai.lite_turn(app, story, bobby, "The lamp gutters.")
        assert untagged.json()["pending_images"] == []
        assert LITE_ILLUSTRATE_INSTRUCTION in ai.generation_prompts(app)[-1]
        assert await ai.auto_jobs(engine, story) == []

        on = await ai.lite_turn(app, story, bobby, key="auto-turn-3")
        assert on.status_code == 200, on.text
        found = on.json()
        assert found["message"]["text"] == ai.REPLY
        assert found["call_counts"] == off.json()["call_counts"]
        assert found["call_counts"]["generation"] == 1
        [job] = await ai.auto_jobs(engine, story)
        assert found["pending_images"] == [
            {"job_id": str(job["id"]), "position": {"turn": 3, "beat": 2}}
        ]
        operation = job["origin"]["auto"]["operation_id"]
        assert (job["principal"], job["idempotency_key"]) == ("system", f"auto:{operation}:0")
        assert job["origin"]["auto"]["reason"] == "new_location"
        assert (job["target"]["audience_viewpoint"], job["viewpoint"]) == (viewpoint, viewpoint)
        assert job["target"]["camera_pov"] == {"kind": "third_person"}
        assert (job["status"], job["prompt"]) == ("queued", "")
        assert await ai.jobs_rows(engine, [str(job["id"])]) == 1
        assert await ai.reservations(engine, story) == 0

        replayed = await ai.lite_turn(app, story, bobby, key="auto-turn-3")
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["pending_images"] == found["pending_images"]
        assert len(await ai.auto_jobs(engine, story)) == 1

        # Regenerate turn 3 while its job is queued: the new variant queues its own job, and
        # the old one ends stale when it runs.
        app.stub.script("generation", [ai.tagged("The cellar breathes cold air.")])
        regenerated = await app.client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
        assert regenerated.status_code == 200, regenerated.text
        old, new = await ai.auto_jobs(engine, story)
        assert str(old["id"]) == str(job["id"])
        assert regenerated.json()["pending_images"] == [
            {"job_id": str(new["id"]), "position": {"turn": 3, "beat": 2}}
        ]
        assert (old["origin"]["variant"], new["origin"]["variant"]) == (1, 2)
        assert new["origin"]["auto"]["operation_id"] != operation
        assert new["idempotency_key"] == f"auto:{new['origin']['auto']['operation_id']}:0"

        stale = await sg.run(app, str(old["id"]))
        assert stale["image_job"] == "stale", stale
        view = await ai.job_view(app, str(old["id"]))
        assert (view["status"], view["status_reason"]) == ("stale", "target_inactive")
        assert view["status_detail"]["cause"] == "regenerated"

        done = await sg.run(app, str(new["id"]))
        assert done["image_job"] == "promoted", done
        assert (await ai.job_view(app, str(new["id"])))["status"] == "promoted"
        items = await si.history(app.client, story, viewpoint)
        [image_id] = si.image_ids(items)
        before = si.after(items, image_id)
        assert before is not None
        assert (before["turn"], before["beat"]) == (3, 2)
        ready = (await ai.auto_jobs(engine, story))[1]
        assert ready["prompt"]
        assert ready["params"]["source"] == "generated"

        # A one-time correct (ADR-174) is a regeneration: the corrected turn queues its own.
        app.stub.script("generation", [ai.tagged("The stair is gone.")])
        corrected = await app.client.post(
            f"/stories/{story}/correct",
            json={"turn": 3, "player_input": {"say": "What was that noise?"}},
            headers=WRITE,
        )
        assert corrected.status_code == 200, corrected.text
        jobs = await ai.auto_jobs(engine, story)
        assert len(jobs) == 3
        assert corrected.json()["pending_images"] == [
            {"job_id": str(jobs[2]["id"]), "position": {"turn": 3, "beat": 2}}
        ]
        assert jobs[2]["origin"]["variant"] == 3


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-6")
async def test_i_23__m8_auto_structured(image_settings: Settings, engine: AsyncEngine) -> None:
    """A structured story under ``auto``: a character render marking three illustration points
    (a duplicate on one beat and an invalid entry besides) queues only
    ``max_auto_images_per_turn`` of them, the first in beat order, each at its committed beat;
    the render's prompt carries the structured instruction.

    Complement asserted: with a cap of 1 one job is queued; ``on_request``, the prompt has no
    instruction and an emitted ``illustrate`` is ignored (the turn commits, nothing is
    queued)."""
    from epistrel.orchestrator.images.auto import STRUCTURED_ILLUSTRATE_INSTRUCTION
    from tests import auto_illustration_support as ai
    from tests.respond_support import body, respond
    from tests.structured_turn_support import beat, renders_of, structured_story

    beats = [
        beat("say", "Look, the old mill.", intent="sincere"),
        beat("do", "Ada points across the river."),
        beat("do", "Ada steps onto the bridge."),
    ]
    marks: list[dict[str, Any]] = [
        {"beat_index": 2, "reason": "turning_point"},
        {"beat_index": 0, "reason": "new_location"},
        {"beat_index": 0, "reason": "first_appearance"},
        {"beat_index": 1, "reason": "first_appearance"},
        {"beat_index": "one", "reason": "sunset"},
    ]
    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await structured_story(app.client, "Wren", "Ada")
        await app.configure()

        app.stub.script("generation", [ai.marked_render(beats, marks)])
        off = await respond(app.client, story, body(ids["Wren"], "Ada, where are we?"))
        assert off.status_code == 200, off.text
        assert off.json()["pending_images"] == []
        [call] = renders_of(app.stub)[-1:]
        assert STRUCTURED_ILLUSTRATE_INSTRUCTION not in sg.payload(call)

        await ai.set_auto(app.client, story, cap=2)
        app.stub.script("generation", [ai.marked_render(beats, marks)])
        on = await respond(app.client, story, body(ids["Wren"], "Ada, what now?"))
        assert on.status_code == 200, on.text
        [call] = renders_of(app.stub)[-1:]
        assert STRUCTURED_ILLUSTRATE_INSTRUCTION in sg.payload(call)
        first, second = await ai.auto_jobs(engine, story)
        turn = on.json()["turn"]
        # Beat 1 is the player's input: the render's beats are 2, 3 and 4.
        assert on.json()["pending_images"] == [
            {"job_id": str(first["id"]), "position": {"turn": turn, "beat": 2}},
            {"job_id": str(second["id"]), "position": {"turn": turn, "beat": 3}},
        ]
        assert [j["origin"]["auto"]["reason"] for j in (first, second)] == [
            "new_location",
            "first_appearance",
        ]
        assert [j["origin"]["auto"]["k"] for j in (first, second)] == [0, 1]

        await ai.set_auto(app.client, story, cap=1)
        app.stub.script("generation", [ai.marked_render(beats, marks)])
        one = await respond(app.client, story, body(ids["Wren"], "Ada, and then?"))
        assert one.status_code == 200, one.text
        assert len(one.json()["pending_images"]) == 1
        assert len(await ai.auto_jobs(engine, story)) == 3


@pytest.mark.part("M8", "FR-IMG-6")
async def test_i_23__m8_auto_rejected_point(pg_url: str, tmp_path: Path) -> None:
    """A point refused inside its job never touches the turn (on a scratch database: the
    deployment's limits are deployment-wide).

    - ``image_limit_deployment_day = 0``: the turn succeeds and lists the job, which ends
      ``cap_reached`` with ``status_reason: images_disabled`` when it runs, with no reservation.
    - A point whose prompt a policy refuses ends its job ``failed`` with ``status_reason:
      guardrail_violation`` and the policy ids; the turn succeeded all the same.

    Complement asserted: with the limits available the job promotes."""
    from tests import auto_illustration_support as ai
    from tests import story_image_support as si

    async with ai.scratch_app(pg_url, tmp_path, "auto_rejected") as (app, db):
        story, ids = await ai.lite_story(app)
        bobby = ids[si.BOBBY]
        await ai.set_auto(app.client, story)

        await ai.deployment(db, image_limit_deployment_day=0)
        capped = await ai.lite_turn(app, story, bobby)
        assert capped.status_code == 200, capped.text
        [pending] = capped.json()["pending_images"]
        ended = await sg.run(app, pending["job_id"])
        assert ended["image_job"] == "cap_reached", ended
        view = await ai.job_view(app, pending["job_id"])
        assert (view["status"], view["status_reason"]) == ("cap_reached", "images_disabled")
        assert await ai.reservations(db, story) == 0

        await ai.deployment(db, image_limit_deployment_day=200)
        app.checks.prompt_violation = "auto_refused"
        refused = await ai.lite_turn(app, story, bobby, ai.tagged(reason="turning_point"))
        assert refused.status_code == 200, refused.text
        [pending] = refused.json()["pending_images"]
        ended = await sg.run(app, pending["job_id"])
        assert ended["image_job"] == "failed", ended
        view = await ai.job_view(app, pending["job_id"])
        assert (view["status"], view["status_reason"]) == ("failed", "guardrail_violation")
        assert view["policy_ids"] == ["auto_refused"]
        assert await ai.reservations(db, story) == 0

        app.checks.prompt_violation = None
        passed = await ai.lite_turn(app, story, bobby)
        [pending] = passed.json()["pending_images"]
        done = await sg.run(app, pending["job_id"])
        assert done["image_job"] == "promoted", done
        assert await ai.reservations(db, story) == 1


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-IMG-6")
async def test_i_23__m8_auto_cap_zero(image_settings: Settings, engine: AsyncEngine) -> None:
    """``max_auto_images_per_turn = 0`` under ``auto``: a marked point queues no job and
    ``pending_images`` is ``[]``; the prompt asks for no tag. Out-of-range and non-integer caps
    and an unknown mode are 422, and nothing changes.

    Complement asserted: a cap of 1 queues one job."""
    from epistrel.orchestrator.images.auto import LITE_ILLUSTRATE_INSTRUCTION
    from tests import auto_illustration_support as ai
    from tests import story_image_support as si

    async with sg.generation_app(image_settings, engine) as app:
        story, ids = await ai.lite_story(app)
        bobby = ids[si.BOBBY]
        await ai.set_auto(app.client, story, cap=0)
        for mode, cap in (("auto", 5), ("auto", -1), ("auto", True), ("auto", 1.0), ("always", 1)):
            bad = await ai.illustrate(app.client, story, mode, cap)
            assert bad.status_code == 422, (mode, cap, bad.text)
        zero = await ai.lite_turn(app, story, bobby)
        assert zero.status_code == 200, zero.text
        assert zero.json()["pending_images"] == []
        assert LITE_ILLUSTRATE_INSTRUCTION not in ai.generation_prompts(app)[-1]
        assert await ai.auto_jobs(engine, story) == []

        await ai.set_auto(app.client, story, cap=1)
        one = await ai.lite_turn(app, story, bobby)
        assert len(one.json()["pending_images"]) == 1
        assert len(await ai.auto_jobs(engine, story)) == 1


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-6")
@pytest.mark.parametrize("point", ["before_commit", "after_commit_before_response"])
async def test_i_23__m8_auto_jobs_in_commit_tx(
    point: str, image_settings: Settings, engine: AsyncEngine, pg_url: str
) -> None:
    """The automatic jobs commit with their turn, in its transaction. A child engine (no job
    worker) killed at ``point`` while it plays an ``auto`` Lite turn marking a point: just
    before the commit (``before_commit``), neither the turn nor a job exists; after the commit,
    before the response (``after_commit_before_response``), the turn and its job, with its
    ``jobs`` row, are both there.

    Complement asserted: each parameter asserts the other's absence or presence."""
    import contextlib

    from epistrel.core.idempotency import IDEMPOTENCY_HEADER
    from epistrel.testing import crash
    from epistrel.testing.faults import EngineProcess, StubRule, crash_at
    from tests import auto_illustration_support as ai
    from tests import story_image_support as si
    from tests.auth_support import CREDENTIAL_A
    from tests.crash_support import recover
    from tests.image_job_support import clean, configure_child_adapter
    from tests.model_support import CONFIG_KEY
    from tests.respond_support import body, respond_client

    say = "Is that a cellar door?"
    async with (
        restored_model_config(engine),
        respond_client(image_settings, engine) as (
            client,
            lib,
        ),
    ):
        try:
            story, ids = await si.scene(client, 0)
            await configure_child_adapter(lib, image_settings)
            await ai.set_auto(client, story)
            child = EngineProcess(
                pg_url,
                CREDENTIAL_A,
                crash=crash_at(point, 1, "turn" if point == "before_commit" else None),
                mode="api",
                images=True,
                config_key=CONFIG_KEY,
                stub_rules=[StubRule((say,), ai.TAGGED, "generation")],
            )
            async with child, httpx.AsyncClient(base_url=child.base_url, timeout=30) as remote:
                with contextlib.suppress(httpx.HTTPError):
                    await remote.post(
                        f"/stories/{story}/respond",
                        json=body(ids[si.BOBBY], say),
                        headers={**WRITE, IDEMPOTENCY_HEADER: f"auto-crash-{point}"},
                    )
                assert await child.wait_exit(30) == crash.EXIT_STATUS
            await recover(engine)
            async with engine.connect() as conn:
                turns = await conn.scalar(
                    text(
                        "SELECT count(*) FROM operations WHERE story_id = :s AND kind = 'turn' "
                        "AND status = 'committed'"
                    ),
                    {"s": story},
                )
            jobs = await ai.auto_jobs(engine, story)
            if point == "before_commit":
                assert (turns, jobs) == (0, [])
            else:
                assert turns == 1
                [job] = jobs
                assert job["status"] == "queued"
                assert await ai.jobs_rows(engine, [str(job["id"])]) == 1
        finally:
            await clean(engine)


@pytest.mark.usefixtures("drained_embedding_jobs", "states_delivered")
@pytest.mark.part("M8", "FR-IMG-6")
async def test_i_23__m8_auto_staged(image_settings: Settings, engine: AsyncEngine) -> None:
    """A staged turn (#212: an effect on a synchronous write-capable provider's value) under
    ``auto``: its intent commit stores the accepted point with the operation
    (``turn_report.illustration``) and queues no job; the completion stage's commit queues it,
    and the finished response lists it.

    Complement asserted: the job was written after the intent committed (its ``created_at``
    is past the intent's stage-log time)."""
    import json as json_

    from tests import auto_illustration_support as ai
    from tests import effect_support as es
    from tests import multi_support as ms
    from tests import staged_support as sp
    from tests.structured_turn_support import beat

    async with sg.generation_app(image_settings, engine) as app:
        await app.configure()
        story, cal, _, _ = await sp.sync_story(app.client, engine, app.lib)
        await ai.set_auto(app.client, story)
        app.stub.reset()
        sp.outcome_step(app.stub, beat("narration", sp.WENT))
        marked = json_.loads(sp.purchase(cal))
        marked["illustrate"] = [{"beat_index": 0, "reason": "turning_point"}]
        ms.renders_for(app.stub, "Mira", json_.dumps(marked))
        es.narrator(app.stub, es.render(beat("narration", sp.COUNTS)))

        bought = await sp.buy(app.client, story, cal)
        assert bought.status_code == 200, bought.text
        op = await sp.staged_op(engine, story)
        assert op.turn_stage == "completed"
        [job] = await ai.auto_jobs(engine, story)
        assert job["origin"]["auto"]["operation_id"] == str(op.operation_id)
        assert job["origin"]["auto"]["reason"] == "turning_point"
        assert bought.json()["pending_images"] == [
            {"job_id": str(job["id"]), "position": job["origin"]["request_position"]}
        ]
        async with engine.connect() as conn:
            report = await conn.scalar(
                text("SELECT turn_report FROM operations WHERE operation_id = :o"),
                {"o": str(op.operation_id)},
            )
        stored = report["illustration"]
        assert stored["points"] == [
            {"beat": job["origin"]["request_position"]["beat"], "reason": "turning_point"}
        ]
        [intent] = [e for e in op.stage_log if e.get("stage") == "intent"]
        assert job["created_at"] > datetime.fromisoformat(str(intent["at"]))


@pytest.mark.integration
@pytest.mark.part("M8", "FR-IMG-11")
async def test_i_23__m8_story_cost(pg_url: str, tmp_path: Path) -> None:
    """``GET /stories/{id}/image-usage`` reports the story's image usage and cost from its
    records and cost rows by ``story_id`` (FR-IMG-11): two outputs billed 0.04 USD each, one of
    them discarded by the check, are both in ``outputs_used`` and in the cost, all-time and on
    their day. The response carries no ``limits`` and no per-turn list: per-story admission is
    the consumer's (#262).

    Complement asserted: another story's image (and its cost) is not included in the first's,
    and appears in its own."""
    from tests.event_support import new_story
    from tests.image_job_support import CTX, MINORS, prepared
    from tests.image_limits_support import space
    from tests.image_support import Outputs, StubImageGenerator
    from tests.image_support import registered as registered_adapter

    async with space(pg_url, tmp_path, "i23_cost") as sp:
        story, other = await new_story(sp.db), await new_story(sp.db)
        stub = StubImageGenerator(script=[])
        with registered_adapter("stub_sync", stub), registered_target():
            await sp.configure()
            first = await sp.insert(prepared(story, origin=_turn_origin(story, 2)))
            stub.script.extend([Outputs(), Outputs()])
            with installed_checks(ScriptedChecks([MINORS, PASS], fail_closed=False)):
                assert (await sp.run(first))["image_job"] == "promoted"
            second = await sp.insert(prepared(other, origin=_turn_origin(other, 2)))
            stub.script.append(Outputs(cost=Cost(Decimal("0.5"), "USD")))
            with installed_checks(ScriptedChecks(fail_closed=False)):
                assert (await sp.run(second))["image_job"] == "promoted"
        found = await sp.client.get(f"/stories/{story}/image-usage", headers=bearer())
        assert found.status_code == 200, found.text
        body = found.json()
        assert set(body) == {"story_id", "from", "to", "retained_from", "all_time", "days"}
        assert "limits" not in body
        assert "turns" not in body
        usd = [{"currency": "USD", "known_total": "0.08000000", "known_attempts": 2}]
        assert (body["all_time"]["outputs_used"], body["all_time"]["costs"]) == (2, usd)
        [day] = body["days"]
        assert (day["outputs_used"], day["costs"]) == (2, usd)
        theirs = (await sp.client.get(f"/stories/{other}/image-usage", headers=bearer())).json()
        assert (theirs["all_time"]["outputs_used"], theirs["all_time"]["costs"]) == (
            1,
            [{"currency": "USD", "known_total": "0.50000000", "known_attempts": 1}],
        )
        library = await sp.lib.get_story_image_usage(CTX, story)
        assert library.all_time.outputs_used == 2


def _turn_origin(story_id: str, turn: int) -> dict[str, Any]:
    from tests.image_support import story_origin

    return story_origin(story_id, turn=turn)
