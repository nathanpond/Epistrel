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
is the structured caller's, #53). SDK exceptions escape as themselves; the Model Access Layer
maps them (``errors.map_provider_error``), and a 200 that is not a chat completion raises
``ProviderGarbage``.

Logs carry the purpose, model, latency and the key's fingerprint only — never prompt or reply
text (invariant 3).
"""

from __future__ import annotations

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
    Message,
    ModelParams,
    ModelResult,
    ModelUnavailable,
)
from epistrel.orchestrator.models.config import EffectiveConfig, KeyBox, key_fingerprint
from epistrel.orchestrator.models.errors import ProviderGarbage

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
            sdk = openai.AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)
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
        started = time.perf_counter()
        body = request_body(model, messages, effective, response_schema, legacy_max_tokens=legacy)
        try:
            completion = await client.chat.completions.create(**body, timeout=timeout)
        except openai.BadRequestError as exc:
            if legacy or effective.max_tokens is None or "response_format" in exc.message:
                raise
            self._legacy_max_tokens.add(base_url)
            body = request_body(model, messages, effective, response_schema, legacy_max_tokens=True)
            completion = await client.chat.completions.create(**body, timeout=timeout)
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
            await client.sdk.close()


__all__ = ["OpenAICompatibleAdapter", "request_body"]
