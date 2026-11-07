"""The bundled OpenRouter image adapter (FR-IMG-1; ADR on per-endpoint capabilities; #255).

``OpenRouterImageGenerator`` generates through OpenRouter's image API, which reaches many image
models through one interface:

- ``POST {base_url}/api/v1/images`` with ``model`` and ``prompt``, answered with base64 images
  (``data[].b64_json``) and ``usage.cost`` in USD. The provider returns its identifier only with
  the results, so submission is ``sync`` and ``confirms_completion`` is not declared.
- **Capabilities are per endpoint.** OpenRouter routes one model to several provider endpoints,
  each with its own ``supported_parameters``. ``GET {base_url}/api/v1/images/models/{id}/endpoints``
  is read once per model and cached for ``image_capability_ttl_s`` (default 3600 s). While a
  refresh fails, the stale entry is served for up to twice the TTL; with no entry, the call fails
  (``ImageProviderUnavailable``): a request is never sent without routing data.

**Routing** (``route_request``). A requested seed or reference images narrow the request to the
endpoints that support them, as ``provider: {only: [...], allow_fallbacks: false}``. When seed and
references are supported only by disjoint sets, references win and the seed is dropped. When no
endpoint supports one, it is dropped. Other parameters never narrow routing: a parameter that no
chosen endpoint lists is dropped (``n`` is then 1). Size and ratio are not translated. After the
call, what was applied is judged against the endpoint that served it (the response's ``provider``,
else the single chosen endpoint), and recorded as the attempt's ``AppliedParameters``.

The negative prompt is folded into the prompt as ``\\n\\nAvoid: <negative>``: the API has no field
for it. Reference images go as ``input_references`` (``{"type": "image_url", "image_url":
{"url": ...}}``): signed URLs or data URLs (``images.references``).

**Outputs.** Each ``b64_json`` is decoded (``validate=True``, a ``data:`` prefix stripped) and read
with Pillow: the width, height and media type come from the bytes, never from the request. Only
PNG, JPEG and WebP are accepted, up to ``MAX_IMAGE_PIXELS``. Fewer outputs than ``n`` is a partial
success; entries without ``b64_json`` are skipped; none at all is ``ImageProviderMalformed``.

**Errors** (``images.errors``): 400/404/413/422/other 4xx → ``ImageRequestInvalid``; 401/403 →
``ImageAdapterAuth``; 402 → ``ImageAdapterCredits``; 408 or a timeout → ``ImageProviderTimeout``
(ambiguous); 409/429/5xx → ``ImageProviderUnavailable``; a redirect (never followed), malformed
JSON or unusable images → ``ImageProviderMalformed``. No message carries the prompt or provider
text (NFR-SEC-4), and the adapter is single-shot: retries belong to the job (#256). Logs name the
model, endpoint, status, latency and cost, never the prompt.

**Idempotency.** ``idempotency_keys`` is ``OPENROUTER_DEDUPES_IDEMPOTENCY_KEY``, set from the
owner's recording run (``tools.record_openrouter_fixtures``); until it is verified it is
``False``, and the ``Idempotency-Key`` header is not sent.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from epistrel.core import clock
from epistrel.core.ids import new_ulid
from epistrel.orchestrator.images.errors import (
    ImageAdapterAuth,
    ImageAdapterCredits,
    ImageAdapterError,
    ImageProviderMalformed,
    ImageProviderTimeout,
    ImageProviderUnavailable,
    ImageRequestInvalid,
)
from epistrel.orchestrator.images.params import ADAPTER_DEFAULT_BASE_URLS, DEFAULT_CAPABILITY_TTL_S
from epistrel.orchestrator.images.port import (
    DEFAULT_REQUEST_TIMEOUT_S,
    AppliedParameters,
    AttemptCost,
    Capabilities,
    CollectResult,
    Cost,
    ImageOutput,
    ImageRequest,
    ImageUsage,
    SubmitResult,
)
from epistrel.orchestrator.images.references import ReferenceResolver
from epistrel.orchestrator.images.registry import ImageAdapterConfig

log = logging.getLogger(__name__)

NAME = "openrouter"
DEFAULT_BASE_URL = ADAPTER_DEFAULT_BASE_URLS[NAME]
OPENROUTER_DEDUPES_IDEMPOTENCY_KEY = False
"""Whether OpenRouter deduplicates two requests with the same ``Idempotency-Key``. Set from
``tests/fixtures/openrouter/verdict.json`` (the owner's recording run); ``False`` until then."""

GENERATE_PATH = "/api/v1/images"
MODELS_PATH = "/api/v1/images/models"
ENDPOINTS_PATH = "/api/v1/images/models/{model}/endpoints"
CONNECT_TIMEOUT_S = 10
ENDPOINTS_TIMEOUT_S = 15
STALE_FACTOR = 2
"""A cached endpoint list is served for up to this many TTLs while its refresh fails."""
MAX_IMAGE_PIXELS = 64_000_000
MEDIA_TYPES: Mapping[str, str] = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
"""Pillow's format name → the media type an output is recorded with (anything else is refused)."""
TITLE = "Epistrel"
NEGATIVE_PREFIX = "\n\nAvoid: "

SEED = "seed"
REFERENCES = "input_references"
CANONICAL_PARAMETERS = frozenset(
    {SEED, REFERENCES, "n", "size", "aspect_ratio", "quality", "resolution"}
)
"""The names matched against an endpoint's ``supported_parameters``."""
SLUG_FIELDS = ("tag", "provider_slug", "slug", "provider_name", "name")
"""Where an endpoint record names itself, in the order they are tried."""


# -- endpoint capabilities ---------------------------------------------------------------------


class EndpointRecord(BaseModel):
    """One endpoint as the endpoints API lists it (unknown fields ignored)."""

    model_config = ConfigDict(extra="ignore")

    tag: str | None = None
    provider_slug: str | None = None
    slug: str | None = None
    provider_name: str | None = None
    name: str | None = None
    supported_parameters: dict[str, Any] | list[Any] | None = None
    """A list of names, or a mapping keyed by name; missing: supports nothing."""
    supports_streaming: bool | None = None
    pricing: Any = None


@dataclass(frozen=True)
class Endpoint:
    """One provider endpoint of a model, and the parameters it supports."""

    slug: str
    parameters: frozenset[str]
    names: frozenset[str] = frozenset()
    """Every way the record names itself (lower-cased), to match a response's ``provider``."""
    supports_streaming: bool | None = None
    pricing: Any = None

    def supports(self, parameter: str) -> bool:
        return parameter in self.parameters


def _parameters(raw: dict[str, Any] | list[Any] | None) -> frozenset[str]:
    if raw is None:
        return frozenset()
    names: Iterable[Any] = raw.keys() if isinstance(raw, dict) else raw
    return frozenset(name for name in names if isinstance(name, str))


def parse_endpoints(body: Any) -> tuple[Endpoint, ...]:
    """The endpoints of an endpoints-API answer: ``{"data": {"endpoints": [...]}}``,
    ``{"data": [...]}`` or ``{"endpoints": [...]}``. ``ImageProviderMalformed`` for anything
    else."""
    records: Any = body
    if isinstance(records, dict) and "data" in records:
        records = records["data"]
    if isinstance(records, dict):
        records = records.get("endpoints")
    if not isinstance(records, list):
        raise ImageProviderMalformed(reason="endpoints_malformed")
    endpoints: list[Endpoint] = []
    for index, raw in enumerate(records):
        try:
            record = EndpointRecord.model_validate(raw)
        except ValidationError:
            raise ImageProviderMalformed(reason="endpoints_malformed") from None
        named = [getattr(record, f) for f in SLUG_FIELDS if getattr(record, f)]
        endpoints.append(
            Endpoint(
                slug=named[0] if named else f"endpoint-{index}",
                parameters=_parameters(record.supported_parameters),
                names=frozenset(n.lower() for n in named),
                supports_streaming=record.supports_streaming,
                pricing=record.pricing,
            )
        )
    return tuple(endpoints)


@dataclass(frozen=True)
class _Cached:
    fetched_at: datetime
    endpoints: tuple[Endpoint, ...]


# -- routing -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """Where one request may go: ``only`` (the ``provider.only`` slugs, or ``None`` for any
    endpoint), the endpoints chosen, and whether the seed and references are sent."""

    only: tuple[str, ...] | None
    chosen: tuple[Endpoint, ...]
    seed: bool
    references: bool

    def lists(self, parameter: str) -> bool:
        """Whether some chosen endpoint lists ``parameter``."""
        return any(e.supports(parameter) for e in self.chosen)


def route_request(
    request: ImageRequest, endpoints: Sequence[Endpoint], *, can_reference: bool = True
) -> Route:
    """The routing rules (module docs). ``can_reference`` ``False``: references cannot be
    resolved here, so they never narrow routing and are dropped."""
    wants_seed = request.seed is not None
    wants_refs = bool(request.reference_images) and can_reference
    seeded = tuple(e for e in endpoints if e.supports(SEED))
    referenced = tuple(e for e in endpoints if e.supports(REFERENCES))
    chosen: tuple[Endpoint, ...] = ()
    seed = refs = False
    if wants_seed and wants_refs:
        both = tuple(e for e in seeded if e in referenced)
        if both:
            chosen, seed, refs = both, True, True
        elif referenced:
            chosen, refs = referenced, True
        elif seeded:
            chosen, seed = seeded, True
    elif wants_refs and referenced:
        chosen, refs = referenced, True
    elif wants_seed and seeded:
        chosen, seed = seeded, True
    if chosen:
        return Route(tuple(e.slug for e in chosen), chosen, seed, refs)
    return Route(None, tuple(endpoints), False, False)


@dataclass(frozen=True)
class PlannedBody:
    """The request body (without ``input_references``, which are resolved later), what it
    sends (as recorded), what the request asked for, and what was dropped before the call."""

    body: dict[str, Any]
    sent: dict[str, Any]
    requested: dict[str, Any]
    dropped: tuple[str, ...]


def requested_of(request: ImageRequest) -> dict[str, Any]:
    """What ``request`` asked for, by wire name (reference images by media id; never the
    prompt)."""
    requested: dict[str, Any] = {"n": request.n}
    for name, value in (
        (SEED, request.seed),
        ("size", request.size),
        ("aspect_ratio", request.aspect_ratio),
        ("quality", request.quality),
    ):
        if value is not None:
            requested[name] = value
    if request.reference_images:
        requested[REFERENCES] = list(request.reference_images)
    return requested


def prompt_of(request: ImageRequest) -> str:
    """The prompt as sent: the negative prompt folded in (the API has no field for it)."""
    if request.negative_prompt:
        return f"{request.prompt}{NEGATIVE_PREFIX}{request.negative_prompt}"
    return request.prompt


def plan_body(model: str, request: ImageRequest, route: Route) -> PlannedBody:
    """The body ``route`` allows for ``request`` (module docs)."""
    body: dict[str, Any] = {"model": model, "prompt": prompt_of(request)}
    dropped: list[str] = []
    if request.seed is not None:
        if route.seed:
            body[SEED] = request.seed
        else:
            dropped.append(SEED)
    if request.n > 1:
        if route.lists("n"):
            body["n"] = request.n
        else:
            dropped.append("n")
    for name, value in (
        ("size", request.size),
        ("aspect_ratio", request.aspect_ratio),
        ("quality", request.quality),
    ):
        if value is None:
            continue
        if route.lists(name):
            body[name] = value
        else:
            dropped.append(name)
    if request.reference_images and not route.references:
        dropped.append(REFERENCES)
    if route.only is not None:
        body["provider"] = {"only": list(route.only), "allow_fallbacks": False}
    sent = {k: v for k, v in body.items() if k not in ("model", "prompt", "provider")}
    if route.references:
        sent[REFERENCES] = len(request.reference_images)
    return PlannedBody(body, sent, requested_of(request), tuple(dropped))


def serving_endpoint(route: Route, provider: object) -> Endpoint | None:
    """The endpoint that served the call: the response's ``provider`` when it names a chosen
    endpoint, else the single chosen endpoint, else ``None``."""
    if isinstance(provider, str):
        wanted = provider.lower()
        for endpoint in route.chosen:
            if wanted == endpoint.slug.lower() or wanted in endpoint.names:
                return endpoint
    if len(route.chosen) == 1:
        return route.chosen[0]
    return None


def applied_of(
    request: ImageRequest, planned: PlannedBody, serving: Endpoint | None
) -> AppliedParameters:
    """What was applied, judged against the serving endpoint when it is known."""

    def applied(name: str) -> bool:
        return name in planned.sent and (serving is None or serving.supports(name))

    dropped = list(planned.dropped)
    if serving is not None:
        dropped += [name for name in planned.sent if not serving.supports(name)]
    return AppliedParameters(
        endpoint=None if serving is None else serving.slug,
        requested=planned.requested,
        sent=planned.sent,
        seed_applied=None if request.seed is None else applied(SEED),
        references_applied=None if not request.reference_images else applied(REFERENCES),
        negative_prompt="folded" if request.negative_prompt else None,
        dropped_parameters=tuple(dict.fromkeys(dropped)),
    )


# -- responses ---------------------------------------------------------------------------------


class _Datum(BaseModel):
    model_config = ConfigDict(extra="ignore")

    b64_json: str | None = None


class _Generation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    data: list[_Datum] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    provider: Any = None


def decode_image(encoded: str) -> ImageOutput:
    """One ``b64_json`` as an ``ImageOutput`` whose media type, width and height Pillow read
    from the bytes (``ImageProviderMalformed`` when they are no PNG, JPEG or WebP image)."""
    text = encoded.strip()
    if text.startswith("data:"):
        text = text.partition(",")[2]
    try:
        raw = base64.b64decode(text, validate=True)
    except binascii.Error, ValueError:
        raise ImageProviderMalformed(reason="undecodable_image") from None
    from PIL import Image  # Pillow is loaded only when an image is decoded

    try:
        with Image.open(io.BytesIO(raw)) as image:
            kind = image.format
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ImageProviderMalformed(reason="pixel_bomb")
            image.verify()
    except ImageProviderMalformed:
        raise
    except Exception:  # truncated, corrupt, a pixel bomb, an unknown format
        raise ImageProviderMalformed(reason="undecodable_image") from None
    media_type = MEDIA_TYPES.get(kind or "")
    if media_type is None:
        raise ImageProviderMalformed(reason="unsupported_format")
    return ImageOutput(raw, media_type, width, height)


def cost_of(usage: Mapping[str, Any] | None) -> AttemptCost:
    """``usage.cost`` as a USD ``Cost``; ``"unknown"`` when it is missing or not a number."""
    value = (usage or {}).get("cost")
    if isinstance(value, bool) or value is None:
        return "unknown"
    if isinstance(value, float) and not math.isfinite(value):
        return "unknown"
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return "unknown"
    if not amount.is_finite() or amount < 0:
        return "unknown"
    return Cost(amount, "USD")


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def status_error(status: int, body: Any) -> ImageAdapterError:
    """The error a non-2xx answer maps to (module docs), naming only the status and the
    provider's error code."""
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if 300 <= status < 400:
        return ImageProviderMalformed(status=status, provider_code=code, reason="redirect")
    if status in (401, 403):
        return ImageAdapterAuth(status=status, provider_code=code)
    if status == 402:
        return ImageAdapterCredits(status=status, provider_code=code)
    if status == 408:
        return ImageProviderTimeout(status=status, provider_code=code)
    if status in (409, 429) or status >= 500:
        return ImageProviderUnavailable(status=status, provider_code=code)
    if 400 <= status < 500:
        return ImageRequestInvalid(status=status, provider_code=code)
    return ImageProviderMalformed(status=status, provider_code=code)


# -- the adapter -------------------------------------------------------------------------------


class OpenRouterImageGenerator:
    """The OpenRouter adapter (module docs): its own factory, registered as ``openrouter``.

    ``transport`` replaces the HTTP transport (the recorder's); ``send_idempotency_key``
    overrides the constant (the recorder's dedupe check)."""

    def __init__(
        self,
        config: ImageAdapterConfig,
        *,
        references: ReferenceResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        send_idempotency_key: bool | None = None,
    ) -> None:
        params = config.params
        self.model = config.model
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.ttl = timedelta(seconds=params.image_capability_ttl_s or DEFAULT_CAPABILITY_TTL_S)
        self.request_timeout_s = params.request_timeout_s or DEFAULT_REQUEST_TIMEOUT_S
        self.references = references if references is not None else config.references
        self.send_idempotency_key = (
            OPENROUTER_DEDUPES_IDEMPOTENCY_KEY
            if send_idempotency_key is None
            else send_idempotency_key
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                "X-Title": TITLE,
            },
            timeout=httpx.Timeout(self.request_timeout_s, connect=CONNECT_TIMEOUT_S),
            follow_redirects=False,
            transport=transport,
        )
        self._cache: dict[str, _Cached] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.endpoint_fetches = 0
        """How many endpoint lists this instance has fetched (observable by tests)."""

    @classmethod
    def capabilities(cls) -> Capabilities:
        return Capabilities(
            submission="sync",
            confirms_completion=False,
            idempotency_keys=OPENROUTER_DEDUPES_IDEMPOTENCY_KEY,
            returns_request_id=False,
        )

    # -- capability discovery ----------------------------------------------------------------

    async def endpoints(self, model: str | None = None) -> tuple[Endpoint, ...]:
        """``model``'s endpoints (this adapter's model by default), from the cache while it is
        fresh (module docs)."""
        model = model or self.model
        cached = self._cache.get(model)
        if cached is not None and clock.now() - cached.fetched_at < self.ttl:
            return cached.endpoints
        lock = self._locks.setdefault(model, asyncio.Lock())
        async with lock:
            cached = self._cache.get(model)
            now = clock.now()
            if cached is not None and now - cached.fetched_at < self.ttl:
                return cached.endpoints
            try:
                endpoints = await self._fetch_endpoints(model)
            except ImageRequestInvalid, ImageAdapterAuth, ImageAdapterCredits:
                raise
            except ImageAdapterError as failure:
                if cached is not None and now - cached.fetched_at < self.ttl * STALE_FACTOR:
                    log.warning(
                        "image endpoints refresh failed model=%s error=%s; serving the cached list",
                        model,
                        failure.code,
                    )
                    return cached.endpoints
                raise
            self._cache[model] = _Cached(clock.now(), endpoints)
            return endpoints

    async def _fetch_endpoints(self, model: str) -> tuple[Endpoint, ...]:
        self.endpoint_fetches += 1
        path = ENDPOINTS_PATH.format(model=model)
        try:
            response = await self._client.get(
                path, timeout=httpx.Timeout(ENDPOINTS_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            )
        except httpx.TransportError:
            raise ImageProviderUnavailable(reason="endpoints_unreachable") from None
        if response.status_code == 404:
            raise ImageRequestInvalid(status=404, reason="model_not_found")
        if response.status_code != 200:
            error = status_error(response.status_code, _json(response))
            if isinstance(error, ImageProviderTimeout | ImageProviderMalformed):
                raise ImageProviderUnavailable(
                    status=response.status_code, reason="endpoints_unavailable"
                )
            raise error
        body = _json(response)
        if body is None:
            raise ImageProviderMalformed(status=200, reason="endpoints_malformed")
        endpoints = parse_endpoints(body)
        if not endpoints:
            raise ImageRequestInvalid(reason="no_endpoints")
        return endpoints

    # -- generation --------------------------------------------------------------------------

    async def _resolve(self, media_ids: Sequence[str]) -> list[dict[str, Any]]:
        assert self.references is not None  # noqa: S101 - route_request(can_reference=...)
        urls = [await self.references.resolve(media_id) for media_id in media_ids]
        return [{"type": "image_url", "image_url": {"url": url}} for url in urls]

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult:
        endpoints = await self.endpoints()
        route = route_request(request, endpoints, can_reference=self.references is not None)
        planned = plan_body(self.model, request, route)
        body = dict(planned.body)
        if route.references:
            body[REFERENCES] = await self._resolve(request.reference_images)
        headers: dict[str, str] = {}
        if self.send_idempotency_key and idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        started = time.monotonic()
        try:
            response = await self._client.post(GENERATE_PATH, json=body, headers=headers)
        except httpx.ConnectTimeout:
            raise ImageProviderTimeout(reason="connect_timeout", ambiguous=False) from None
        except httpx.TimeoutException:
            raise ImageProviderTimeout(reason="timeout") from None
        except httpx.ConnectError:
            raise ImageProviderUnavailable(reason="unreachable") from None
        except httpx.TransportError:
            raise ImageProviderUnavailable(reason="transport", ambiguous=True) from None
        latency_ms = round((time.monotonic() - started) * 1000)
        if response.status_code != 200:
            log.info(
                "image call model=%s status=%d latency_ms=%d",
                self.model,
                response.status_code,
                latency_ms,
            )
            raise status_error(response.status_code, _json(response))
        result = self._result(request, planned, route, _json(response))
        applied = result.applied_parameters
        cost = result.cost
        log.info(
            "image call model=%s endpoint=%s status=200 latency_ms=%d images=%d cost=%s",
            self.model,
            (applied.endpoint if applied is not None else None) or "unknown",
            latency_ms,
            len(result.outputs or ()),
            cost.amount if isinstance(cost, Cost) else "unknown",
        )
        return result

    def _result(
        self, request: ImageRequest, planned: PlannedBody, route: Route, body: Any
    ) -> SubmitResult:
        if not isinstance(body, dict):
            raise ImageProviderMalformed(status=200, reason="malformed_json")
        try:
            generation = _Generation.model_validate(body)
        except ValidationError:
            raise ImageProviderMalformed(status=200, reason="malformed_json") from None
        outputs = tuple(decode_image(d.b64_json) for d in generation.data if d.b64_json)
        if not outputs:
            raise ImageProviderMalformed(status=200, reason="no_images")
        applied = applied_of(request, planned, serving_endpoint(route, generation.provider))
        usage = ImageUsage(images=len(outputs), raw=dict(generation.usage or {}))
        return SubmitResult(
            attempt_ref=generation.id or f"openrouter-{new_ulid()}",
            outputs=outputs,
            usage=usage,
            cost=cost_of(generation.usage),
            applied_parameters=applied,
        )

    async def collect(self, attempt_ref: str) -> CollectResult:
        raise NotImplementedError("the OpenRouter adapter is sync: submit returns the outputs")

    async def models(self) -> Any:
        """The image models list (``GET /api/v1/images/models``), as JSON: the recorder's."""
        response = await self._client.get(
            MODELS_PATH, timeout=httpx.Timeout(ENDPOINTS_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        )
        if response.status_code != 200:
            raise status_error(response.status_code, _json(response))
        return _json(response)

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = [
    "CANONICAL_PARAMETERS",
    "DEFAULT_BASE_URL",
    "ENDPOINTS_PATH",
    "GENERATE_PATH",
    "MODELS_PATH",
    "NAME",
    "OPENROUTER_DEDUPES_IDEMPOTENCY_KEY",
    "REFERENCES",
    "SEED",
    "Endpoint",
    "OpenRouterImageGenerator",
    "PlannedBody",
    "Route",
    "applied_of",
    "cost_of",
    "decode_image",
    "parse_endpoints",
    "plan_body",
    "prompt_of",
    "requested_of",
    "route_request",
    "serving_endpoint",
    "status_error",
]
