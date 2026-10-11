"""The ``openai`` SDK adapter: any OpenAI-compatible endpoint, per purpose
(FR-MODEL-1,8; NFR-PORT-1; ADR-150).

Each call resolves its purpose's effective configuration, then sends one Chat Completions
request (``stream=False``) to that purpose's ``base_url`` — a hosted provider, an enterprise
gateway, or LM Studio at ``http://host.docker.internal:1234/v1``. The SDK's own retries are off
(``max_retries=0``): the turn budget owns every attempt. The timeout comes from the effective
params (default 60 s). One ``AsyncOpenAI`` client exists per distinct ``(base_url, api_key)``;
the cache is dropped whenever the configuration revision changes.

``max_tokens`` is sent as ``max_completion_tokens``; an endpoint that answers that with a 400
is retried once with the legacy ``max_tokens`` and remembered.

Logs carry the purpose, model, latency and the key's fingerprint only — never prompt or reply
text (invariant 3).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import openai
from pydantic import BaseModel

from epistrel.orchestrator.models.adapter import (
    DEFAULT_TIMEOUT_S,
    Message,
    ModelParams,
    ModelResult,
    ModelUnavailable,
)
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox, key_fingerprint

log = logging.getLogger(__name__)

type ConfigResolver = Callable[[str], Awaitable[EffectiveConfig]]


def request_body(
    model: str,
    messages: Sequence[Message],
    params: ModelParams,
    response_schema: type[BaseModel] | None,
    *,
    legacy_max_tokens: bool = False,
) -> dict[str, Any]:
    """The Chat Completions arguments (everything but the timeout)."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [m.model_dump() for m in messages],
        "stream": False,
    }
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop is not None:
        body["stop"] = list(params.stop)
    if params.max_tokens is not None:
        body["max_tokens" if legacy_max_tokens else "max_completion_tokens"] = params.max_tokens
    if response_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_schema.__name__,
                "schema": response_schema.model_json_schema(),
            },
        }
    return body


class OpenAICompatibleAdapter:
    """Chat Completions over the ``openai`` SDK, configured per purpose from ``model_config``."""

    name = "openai"

    def __init__(self, resolve: ConfigResolver, keybox: KeyBox) -> None:
        self._resolve = resolve
        self._keybox = keybox
        self._clients: dict[tuple[str, str], openai.AsyncOpenAI] = {}
        self._revision: str | None = None
        self._legacy_max_tokens: set[str] = set()

    def _client(self, config: EffectiveConfig, base_url: str, api_key: str) -> openai.AsyncOpenAI:
        if config.revision != self._revision:
            # A configuration write: rebuild clients from the new rows. Old clients are left
            # to finish their in-flight calls (#52 closes them once idle).
            self._clients = {}
            self._revision = config.revision
        key = (base_url, key_fingerprint(api_key))
        client = self._clients.get(key)
        if client is None:
            client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)
            self._clients[key] = client
        return client

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
        client = self._client(config, config.base_url, secret)
        effective = config.params.merged(params)
        timeout = effective.timeout or DEFAULT_TIMEOUT_S
        legacy = config.base_url in self._legacy_max_tokens
        started = time.perf_counter()
        body = request_body(
            config.model, messages, effective, response_schema, legacy_max_tokens=legacy
        )
        try:
            completion = await client.chat.completions.create(**body, timeout=timeout)
        except openai.BadRequestError:
            if legacy or effective.max_tokens is None:
                raise
            self._legacy_max_tokens.add(config.base_url)
            body = request_body(
                config.model, messages, effective, response_schema, legacy_max_tokens=True
            )
            completion = await client.chat.completions.create(**body, timeout=timeout)
        latency_ms = round((time.perf_counter() - started) * 1000)
        choice = completion.choices[0] if completion.choices else None
        usage = completion.usage
        result = ModelResult(
            text=(choice.message.content or "") if choice is not None else "",
            finish_reason=choice.finish_reason if choice is not None else None,
            model=completion.model or config.model,
            latency_ms=latency_ms,
            tokens_in=usage.prompt_tokens if usage is not None else None,
            tokens_out=usage.completion_tokens if usage is not None else None,
            cost=None,
            request_id=completion.id or None,
        )
        log.info(
            "model call purpose=%s model=%s latency_ms=%d key=%s",
            purpose,
            result.model,
            latency_ms,
            key_fingerprint(secret),
        )
        return result

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.close()


__all__ = ["OpenAICompatibleAdapter", "request_body"]
