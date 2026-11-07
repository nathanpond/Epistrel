"""The ``openai`` SDK adapter: any OpenAI-compatible endpoint, per purpose
(FR-MODEL-1,8; NFR-PORT-1; ADR-150).

Each call resolves its purpose's effective configuration, then sends one Chat Completions
request (``stream=False``) to that purpose's ``base_url`` — a hosted provider, an enterprise
gateway, or LM Studio at ``http://host.docker.internal:1234/v1``. The SDK's own retries are off
(``max_retries=0``): the turn budget owns every attempt. The timeout comes from the effective
params (default 60 s). One ``AsyncOpenAI`` client exists per distinct ``(base_url, api_key)``
at the current ``config_revision``; when the revision moves, the next call builds new clients and
each old one is closed as soon as its in-flight calls finish (FR-MODEL-4).

``max_tokens`` is sent as ``max_completion_tokens``; an endpoint that answers that with a 400
is retried once with the legacy ``max_tokens`` and remembered (a 400 about ``response_format``
is the structured caller's, #53). A model that answers a 400 naming ``temperature`` or
``top_p`` is retried once without both and remembered per endpoint and model (#235: Claude 4.7
and later accept only their default sampling; the model's default then applies). A client for
``api.anthropic.com`` also sends ``anthropic-version``: that endpoint's ``GET /models`` is the
native Anthropic API, which answers a request without it with a 400 (#235). A structured call
to ``api.anthropic.com`` sends ``json_schema.strict: true`` with the schema's strict wire form,
as that endpoint refuses any other (#761, ``provider_schema``); every other endpoint's request is
unchanged. SDK
exceptions escape as themselves; the Model Access Layer maps them
(``errors.map_provider_error``), and a 200 that is not a chat completion raises
``ProviderGarbage``.

Logs carry the purpose, model, latency and the key's fingerprint only — never prompt or reply
text (invariant 3).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import openai
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from epistrel.orchestrator.models.adapter import (
    DEFAULT_TIMEOUT_S,
    PROBE_MESSAGES,
    PROBE_TIMEOUT_S,
    Message,
    ModelParams,
    ModelResult,
    ModelUnavailable,
    ProbeResult,
)
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox, key_fingerprint
from epistrel.orchestrator.models.errors import ProviderGarbage
from epistrel.orchestrator.models.provider_schema import is_anthropic, strict_wire_schema

log = logging.getLogger(__name__)

type ConfigResolver = Callable[[str], Awaitable[EffectiveConfig]]


def request_body(
    model: str,
    messages: Sequence[Message],
    params: ModelParams,
    response_schema: type[BaseModel] | None,
    *,
    legacy_max_tokens: bool = False,
    sampling: bool = True,
    strict: bool = False,
) -> dict[str, Any]:
    """The Chat Completions arguments (everything but the timeout); without ``sampling``, no
    ``temperature`` or ``top_p``. With a ``response_schema``, ``response_format`` is
    ``{"type": "json_schema", "json_schema": {"name", "schema"}}``; with ``strict`` (an
    api.anthropic.com endpoint, #761) it also carries ``"strict": true`` and the schema is its
    strict wire form (``provider_schema.strict_wire_schema``). A schema with no wire form is sent
    as without ``strict``; ``structured_call`` never asks for one (``json_schema_mode``). A
    message with image parts is sent as a content array of ``text`` and ``image_url`` parts
    (#258)."""
    body: dict[str, Any] = {
        "model": model,
        # A multimodal message's parts go as OpenAI's content array (#258).
        "messages": [m.wire() for m in messages],
        "stream": False,
    }
    if sampling and params.temperature is not None:
        body["temperature"] = params.temperature
    if sampling and params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop is not None:
        body["stop"] = list(params.stop)
    if params.seed is not None:
        body["seed"] = params.seed
    if params.max_tokens is not None:
        body["max_tokens" if legacy_max_tokens else "max_completion_tokens"] = params.max_tokens
    if response_schema is not None:
        schema = response_schema.model_json_schema()
        wire = strict_wire_schema(schema) if strict else None
        json_schema: dict[str, Any] = {"name": response_schema.__name__}
        if wire is not None:
            json_schema["strict"] = True
        json_schema["schema"] = schema if wire is None else wire
        body["response_format"] = {"type": "json_schema", "json_schema": json_schema}
    return body


ANTHROPIC_VERSION = "2023-06-01"
"""The ``anthropic-version`` header Anthropic's ``GET /v1/models`` requires."""


def provider_headers(base_url: str) -> dict[str, str]:
    """Extra headers a provider needs beyond the OpenAI protocol: ``anthropic-version`` for
    ``api.anthropic.com`` (#235); none for any other endpoint."""
    if is_anthropic(base_url):
        return {"anthropic-version": ANTHROPIC_VERSION}
    return {}


def rejects_sampling(exc: openai.BadRequestError) -> bool:
    """Whether a 400 names ``temperature`` or ``top_p`` (a model that takes only its default
    sampling)."""
    try:
        body = json.dumps(exc.body) if exc.body is not None else ""
    except TypeError, ValueError:
        body = ""
    text = f"{exc.message} {body}"
    return "temperature" in text or "top_p" in text


def versions_of(models: Sequence[Any]) -> dict[str, str]:
    """#710: each listed model's ``version`` field (an extra field of the SDK's ``Model``),
    when it carries a non-empty one; at most 200 characters."""
    found: dict[str, str] = {}
    for model in models:
        version = (getattr(model, "model_extra", None) or {}).get("version")
        if isinstance(version, str | int | float) and str(version).strip():
            found[str(model.id)] = str(version).strip()[:200]
    return found


def reported_cost(usage: object) -> float | None:
    """``usage.cost`` when the endpoint reports one (some gateways do; the SDK keeps it as an
    extra field), else ``None``: cost is never estimated (FR-MODEL-12)."""
    extra = getattr(usage, "model_extra", None) or {}
    cost = extra.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
        return None
    return float(cost)


@dataclass(eq=False)
class _Client:
    """One SDK client and the number of calls currently using it."""

    sdk: openai.AsyncOpenAI
    in_flight: int = 0
    retired: bool = False


class OpenAICompatibleAdapter:
    """Chat Completions over the ``openai`` SDK, configured per purpose from ``model_config``."""

    name = "openai"

    def __init__(self, resolve: ConfigResolver, keybox: KeyBox) -> None:
        self._resolve = resolve
        self._keybox = keybox
        self._clients: dict[tuple[str, str], _Client] = {}
        self._revision: int | None = None
        self._legacy_max_tokens: set[str] = set()
        self._default_sampling: set[tuple[str, str]] = set()
        """``(base_url, model)`` pairs that refused ``temperature``/``top_p``."""
        self.closed_clients = 0
        """How many superseded clients have been closed (observable by tests)."""

    async def _acquire(self, config: EffectiveConfig, base_url: str, api_key: str) -> _Client:
        if config.revision != self._revision:
            # A configuration write: the next calls use new clients. A superseded client is
            # closed now if idle, else by the last of its in-flight calls (``_release``).
            superseded, self._clients = list(self._clients.values()), {}
            self._revision = config.revision
            for old in superseded:
                old.retired = True
                if old.in_flight == 0:
                    await self._close(old)
        key = (base_url, key_fingerprint(api_key))
        client = self._clients.get(key)
        if client is None:
            sdk = openai.AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
                default_headers=provider_headers(base_url),
            )
            client = self._clients[key] = _Client(sdk)
        client.in_flight += 1
        return client

    async def _release(self, client: _Client) -> None:
        client.in_flight -= 1
        if client.retired and client.in_flight == 0:
            await self._close(client)

    async def _close(self, client: _Client) -> None:
        await client.sdk.close()
        self.closed_clients += 1

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        config = await self._resolve(purpose)
        if config.base_url is None or config.model is None:
            raise ModelUnavailable(purpose, "not_configured")
        if config.api_key_ciphertext is None:
            raise ModelUnavailable(purpose, "no_api_key")
        api_key = self._keybox.decrypt(config.api_key_ciphertext, purpose=purpose)
        secret = api_key.get_secret_value()
        client = await self._acquire(config, config.base_url, secret)
        try:
            return await self._send(
                client.sdk,
                config,
                messages,
                params,
                response_schema,
                secret,
                config.base_url,
                config.model,
            )
        finally:
            await self._release(client)

    async def _send(
        self,
        client: openai.AsyncOpenAI,
        config: EffectiveConfig,
        messages: Sequence[Message],
        params: ModelParams,
        response_schema: type[BaseModel] | None,
        secret: str,
        base_url: str,
        model: str,
    ) -> ModelResult:
        purpose = config.purpose
        effective = config.params.merged(params)
        timeout = effective.timeout or DEFAULT_TIMEOUT_S
        legacy = base_url in self._legacy_max_tokens
        sampling = (base_url, model) not in self._default_sampling
        started = time.perf_counter()
        while True:
            body = request_body(
                model,
                messages,
                effective,
                response_schema,
                legacy_max_tokens=legacy,
                sampling=sampling,
                strict=is_anthropic(base_url),
            )
            try:
                completion = await client.chat.completions.create(**body, timeout=timeout)
                break
            except openai.BadRequestError as exc:
                sent = "temperature" in body or "top_p" in body
                if sampling and sent and rejects_sampling(exc):
                    self._default_sampling.add((base_url, model))
                    sampling = False
                    continue
                if legacy or effective.max_tokens is None or "response_format" in exc.message:
                    raise
                self._legacy_max_tokens.add(base_url)
                legacy = True
        latency_ms = round((time.perf_counter() - started) * 1000)
        # A 200 whose body is not a chat completion (an HTML page, ``{}``, no choices) is
        # garbage, not an empty answer.
        if not isinstance(completion, ChatCompletion) or not completion.choices:
            raise ProviderGarbage(f"{purpose}: the response is not a chat completion")
        choice = completion.choices[0]
        if getattr(choice, "message", None) is None:
            raise ProviderGarbage(f"{purpose}: the response has no message")
        usage = completion.usage
        result = ModelResult(
            text=choice.message.content or "",
            finish_reason=choice.finish_reason,
            refused=bool(choice.message.refusal),
            model=completion.model or model,
            latency_ms=latency_ms,
            tokens_in=usage.prompt_tokens if usage is not None else None,
            tokens_out=usage.completion_tokens if usage is not None else None,
            cost=reported_cost(usage),
            request_id=completion.id or None,
            reported_version=getattr(completion, "system_fingerprint", None) or None,
        )
        log.info(
            "model call purpose=%s model=%s latency_ms=%d key=%s",
            purpose,
            result.model,
            latency_ms,
            key_fingerprint(secret),
        )
        return result

    async def probe(self, purpose: str) -> ProbeResult:
        """Protocol compatibility for ``purpose``'s endpoint: ``GET /models`` answers with a
        model list, and one minimal chat completion succeeds (NFR-PORT-1). Failures escape as
        the SDK's exceptions, like ``complete``."""
        config = await self._resolve(purpose)
        if config.base_url is None or config.model is None:
            raise ModelUnavailable(purpose, "not_configured")
        if config.api_key_ciphertext is None:
            raise ModelUnavailable(purpose, "no_api_key")
        secret = self._keybox.decrypt(config.api_key_ciphertext, purpose=purpose)
        client = await self._acquire(config, config.base_url, secret.get_secret_value())
        try:
            page = await client.sdk.models.list(timeout=config.params.timeout or PROBE_TIMEOUT_S)
            listed = tuple(sorted(model.id for model in page.data))
        finally:
            await self._release(client)
        result = await self.complete(purpose, PROBE_MESSAGES, ModelParams(max_tokens=16))
        return ProbeResult(models=listed, model=result.model)

    async def listed_versions(self, purpose: str) -> dict[str, str]:
        """#710 (FR-PROF-16): the ``version`` field of each ``GET /models`` entry of
        ``purpose``'s endpoint that carries one (an OpenAI-compatible extension; the OpenAI API
        itself sends none). Failures escape as the SDK's exceptions."""
        config = await self._resolve(purpose)
        if config.base_url is None or config.api_key_ciphertext is None:
            return {}
        secret = self._keybox.decrypt(config.api_key_ciphertext, purpose=purpose)
        client = await self._acquire(config, config.base_url, secret.get_secret_value())
        try:
            page = await client.sdk.models.list(timeout=config.params.timeout or PROBE_TIMEOUT_S)
        finally:
            await self._release(client)
        return versions_of(page.data)

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.sdk.close()


__all__ = [
    "OpenAICompatibleAdapter",
    "provider_headers",
    "rejects_sampling",
    "reported_cost",
    "request_body",
]
