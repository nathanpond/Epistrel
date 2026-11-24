"""The strict-schema warm-up (FR-MODEL-7; #941, ADR-212).

api.anthropic.com compiles a strict ``json_schema`` into a grammar the first time it sees it,
and caches the grammar for a while. Most schemas compile cold in 2 to 7 s, but
``RewriteParseResult`` took 128.6 to 138.8 s cold, live, on 2026-11-22 (#941). That is past
``rewrite.parse.PARSE_DEADLINE_S`` (120 s), and the deadline clamps the call, so the first
``POST /stories/{id}/rewrites`` after the provider's cache expired failed with 503
``model_unavailable`` (``timeout``).

``StrictWarmup`` sends one minimal strict call for each schema on ``WARM_LIST``, in the
background, once the service is up, so a request does not wait on the compile:

- **What is sent.** For each delivered purpose whose registry ``response_schema`` is on
  ``WARM_LIST``, the purpose's effective configuration (``default`` included) is read. A call is
  sent only when its ``base_url`` is api.anthropic.com (``provider_schema.is_anthropic``, where
  Epistrel sends ``strict: true``) and the schema has a strict wire form. The calls are deduped
  by ``(base_url, model, schema)``. A purpose with no endpoint is skipped. So is one with no API
  key, logged at debug. Each call is one user line (``WARMUP_PROMPT``) with the schema as its
  strict ``response_format`` and ``max_tokens`` ``WARMUP_MAX_TOKENS`` (64), over the purpose's
  own params. The compile is keyed on the schema, so the line's content does not matter. The
  answer is discarded: a truncated or invalid answer still compiled the grammar.
- **How.** Through the normal adapter (``ModelAccessLayer.adapter.complete``), under its own
  ``WARMUP_TIMEOUT_S`` (210 s, above the measured cold compile), with no turn budget and no
  deadline clamp. It is a single attempt, and the calls are sequential. It is not a request's
  call: nothing goes to the model-call metrics or to any operation's ``model_calls``. With
  tracing on, each call is a ``model_call`` span whose purpose attribute is ``warmup``.
- **Failures.** A failed or timed-out call is logged once, at warning, with the purpose, the
  endpoint's host and the exception's type only (invariant 3), and it is not retried. A
  configuration that cannot be read is logged once, and the pass ends. Each pass ends with an
  info line: ``model warm-up finished``.
- **When.** ``start`` runs one pass, at start-up, in the processes that serve the API
  (``api``, ``all``; a ``worker`` parses no rewrite). After that, ``changed`` (every
  ``/admin/model-config`` write) runs another pass. A triple already warmed in this process is
  not sent again, so only a new endpoint or model costs a call. Requests that arrive while a
  pass runs are coalesced into one more pass. ``stop`` cancels the pass at shutdown.
- **Off.** ``EPISTREL_MODEL_WARMUP=0`` (or ``false``/``off``) turns it off, and the stub adapter
  never warms. A non-strict endpoint is never sent anything.

A rewrite that arrives before the pass finishes can still wait on the compile.

**Today the list warms nothing (#941, live 2026-11-24).** api.anthropic.com now refuses a strict
schema with more than 16 union-typed parameters, and ``RewriteParseResult``'s wire form has 17
(``provider_schema.UNION_LIMIT``). So it has no strict wire form: ``rewrite_parse`` goes there as
parse-validate-retry, compiles no grammar, and this module skips it. ``WARM_LIST`` keeps it, so
it is warmed again if its wire form returns under the limit.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import urllib.parse
from contextlib import suppress
from typing import TYPE_CHECKING

from pydantic import BaseModel

from epistrel.observability import tracing
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.metrics import Outcome, model_label, purpose_label
from epistrel.orchestrator.models.provider_schema import is_anthropic, strict_wire_schema
from epistrel.orchestrator.models.registry import DELIVERED, RewriteParseResult

if TYPE_CHECKING:
    from epistrel.orchestrator.models.access import ModelAccessLayer

log = logging.getLogger(__name__)

WARM_LIST: tuple[type[BaseModel], ...] = (RewriteParseResult,)
"""The response schemas whose strict grammar compiles too slowly cold (over 30 s) for a
request to wait on it (#941). The other registry schemas compiled cold in 1.9 to 6.6 s
(``docs/evals/README.md``, "First use of a strict schema on api.anthropic.com")."""

WARMUP_TIMEOUT_S = 210.0
"""Each warm-up call's own timeout: above the 128.6 to 138.8 s cold compile #941 measured."""

WARMUP_MAX_TOKENS = 64
"""A warm-up call's ``max_tokens``: the answer is discarded, and the compile comes first."""

WARMUP_PROMPT = "Reply with a JSON object that fits the response schema."
"""The warm-up call's only message. It holds no story content."""

WARMUP_LABEL = "warmup"
"""The purpose attribute of a warm-up call's ``model_call`` span."""


def warm_purposes() -> tuple[tuple[str, type[BaseModel]], ...]:
    """Each delivered purpose whose response schema is on ``WARM_LIST``, with that schema."""
    return tuple(
        (purpose.name, schema)
        for schema in WARM_LIST
        for purpose in DELIVERED.values()
        if purpose.response_schema is schema
    )


def _host(base_url: str) -> str:
    return urllib.parse.urlsplit(base_url).hostname or "unknown"


class StrictWarmup:
    """The background warm-up of one process (module docstring). ``enabled`` is
    ``EPISTREL_MODEL_WARMUP`` under the ``openai`` adapter."""

    def __init__(self, models: ModelAccessLayer, *, enabled: bool) -> None:
        self.models = models
        self.enabled = enabled
        self.warmed: set[tuple[str, str, str]] = set()
        """``(base_url, model, schema)`` triples warmed in this process."""
        self._started = False
        self._again = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin the first pass in the background (the app's start-up); nothing when off."""
        if not self.enabled:
            return
        self._started = True
        self._launch()

    def changed(self) -> None:
        """The model configuration changed: run another pass, once started."""
        if self._started:
            self._launch()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait(self) -> None:
        """Wait for the pass in progress and any queued after it (tests)."""
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)

    async def stop(self) -> None:
        """Cancel the pass in progress (shutdown); later ``changed`` calls do nothing."""
        self._started = False
        self._again = False
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _launch(self) -> None:
        if self.running:
            self._again = True  # coalesced: one more pass after this one
            return
        # A context of its own: no request's call log, story route or eval scope reaches it.
        self._task = asyncio.get_running_loop().create_task(
            self._passes(), name="model-warmup", context=contextvars.Context()
        )

    async def _passes(self) -> None:
        while True:
            self._again = False
            await self.warm_once()
            if not self._again:
                return

    async def warm_once(self) -> int:
        """One pass: the number of warm-up calls sent."""
        started = time.perf_counter()
        sent = failed = 0
        seen: set[tuple[str, str, str]] = set()
        for purpose, schema in warm_purposes():
            try:
                config = await self.models.resolve(purpose)
            except Exception as exc:  # a database fault: the pass ends, no retry
                log.warning(
                    "model warm-up could not read the model configuration error=%s",
                    type(exc).__name__,
                )
                break
            base_url, model = config.base_url, config.model
            if not base_url or not model or not is_anthropic(base_url):
                continue
            if strict_wire_schema(schema.model_json_schema()) is None:
                continue
            key = (base_url, model, schema.__name__)
            if key in self.warmed or key in seen:
                continue
            seen.add(key)
            if not config.has_api_key:
                log.debug("model warm-up skipped purpose=%s reason=no_api_key", purpose)
                continue
            sent += 1
            if await self._call(purpose, schema, base_url, model):
                self.warmed.add(key)
            else:
                failed += 1
        log.info(
            "model warm-up finished sent=%d failed=%d elapsed_ms=%d",
            sent,
            failed,
            round((time.perf_counter() - started) * 1000),
        )
        return sent

    async def _call(self, purpose: str, schema: type[BaseModel], base_url: str, model: str) -> bool:
        """One warm-up call; whether it answered (any answer: the grammar compiled)."""
        started = time.perf_counter()
        outcome: Outcome = "ok"
        try:
            async with asyncio.timeout(WARMUP_TIMEOUT_S):
                await self.models.adapter.complete(
                    purpose,
                    [Message(role="user", content=WARMUP_PROMPT)],
                    ModelParams(max_tokens=WARMUP_MAX_TOKENS, timeout=WARMUP_TIMEOUT_S),
                    schema,
                )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "timeout" if isinstance(exc, TimeoutError) else "http_error"
            # The type only: a provider's message can echo the request (invariant 3).
            log.warning(
                "model warm-up call failed purpose=%s host=%s error=%s",
                purpose_label(purpose),
                _host(base_url),
                type(exc).__name__,
            )
            return False
        finally:
            tracing.finished_span(
                "model_call",
                time.perf_counter() - started,
                {
                    "epistrel.purpose": WARMUP_LABEL,
                    "epistrel.model": model_label(model),
                    "epistrel.attempt": 1,
                    "epistrel.outcome": outcome,
                },
            )
        return True


__all__ = [
    "WARMUP_LABEL",
    "WARMUP_MAX_TOKENS",
    "WARMUP_PROMPT",
    "WARMUP_TIMEOUT_S",
    "WARM_LIST",
    "StrictWarmup",
    "warm_purposes",
]
