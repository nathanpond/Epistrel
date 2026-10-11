"""The Model Access Layer: every model call goes through here (FR-MODEL-1, NFR-PORT-2).

``ModelAccessLayer.from_settings`` picks the adapter from ``EPISTREL_MODEL_ADAPTER``
(``openai`` or ``stub``); ``epistrel.Engine`` owns one and binds it to the database. A call
names a delivered purpose: ``call(purpose, inputs, params)`` validates ``inputs`` against the
purpose's input schema (an undeclared field is refused), renders its template, and completes;
``complete(purpose, messages, params)`` sends prepared messages. Under the stub, calls never
need ``model_config``: the stub only reports the configured model name when a database is bound.

Every call goes through ``send``, which is where provider failures stop (FR-MODEL-6, NFR-REL-3):
the call is bounded by its timeout (the purpose's ``params.timeout``, default 60 s) and by the
turn's deadline when a ``TurnBudget`` is passed, whichever is sooner, and debits that budget;
every SDK exception becomes a ``ModelError`` (``errors.map_provider_error``). An unstructured
call that answers only whitespace raises ``EmptyCompletion``. Structured results go through
``structured_call`` (``structured.py``), which never returns a partially parsed object.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.adapter import (
    DEFAULT_TIMEOUT_S,
    PROBE_TIMEOUT_S,
    REQUEST_FIELDS,
    Message,
    ModelAdapter,
    ModelParams,
    ModelResult,
    ProbeResult,
    UndeclaredInput,
    UnknownPurpose,
)
from epistrel.orchestrator.models.config import (
    ConfigCache,
    ConfigEntry,
    EffectiveConfig,
    KeyBox,
    StructuredSettings,
)
from epistrel.orchestrator.models.errors import (
    BudgetExhausted,
    EmptyCompletion,
    ModelError,
    ModelUnavailable,
    map_provider_error,
)
from epistrel.orchestrator.models.metrics import ModelCall, Outcome, outcome_of, record
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import payload_fields, render
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    RESERVED,
    InputSchema,
    Purpose,
    declared_paths,
)
from epistrel.orchestrator.models.structured import (
    DeterministicFirst,
    ResponseFormatMemo,
    Uncertain,
    structured_call,
)
from epistrel.orchestrator.models.stub import StubAdapter

log = logging.getLogger(__name__)


def get_delivered(name: str) -> Purpose:
    """The delivered purpose ``name``; 422 for a reserved or unknown one."""
    purpose = DELIVERED.get(name)
    if purpose is not None:
        return purpose
    reserved = RESERVED.get(name)
    if reserved is not None:
        raise UnknownPurpose(
            f"model purpose {name!r} is reserved for {reserved.milestone}",
            details={"purpose": name, "reason": "reserved", "milestone": reserved.milestone},
        )
    raise UnknownPurpose(
        f"unknown model purpose {name!r}", details={"purpose": name, "reason": "unknown"}
    )


def build_inputs(purpose: Purpose, inputs: InputSchema | Mapping[str, Any]) -> InputSchema:
    """``inputs`` as exactly the purpose's schema; any field it does not declare is refused.

    A subclass instance (a test builder adding a field, say) is re-validated as the declared
    schema, so its extra field is refused rather than silently carried.
    """
    schema = purpose.input_schema
    if type(inputs) is schema:
        return inputs
    raw = inputs.model_dump() if isinstance(inputs, BaseModel) else dict(inputs)
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        extra = sorted(
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors()
            if error["type"] == "extra_forbidden"
        )
        if extra:
            raise UndeclaredInput(
                f"{purpose.name} does not declare input field(s): {', '.join(extra)}",
                details={"purpose": purpose.name, "fields": extra},
            ) from None
        raise UndeclaredInput(
            f"invalid inputs for {purpose.name}",
            details={
                "purpose": purpose.name,
                "fields": sorted(".".join(str(p) for p in e["loc"]) for e in exc.errors()),
            },
        ) from None


def declared_fields(purpose: Purpose) -> frozenset[str]:
    """Every field path ``purpose`` declares (nested models included)."""
    return declared_paths(purpose.input_schema)


NOT_ATTEMPTED = frozenset({"not_configured", "no_api_key"})
"""Failures the adapter raises before sending anything; they are not call attempts."""


def _attempted(error: ModelError) -> bool:
    return error.details.get("reason") not in NOT_ATTEMPTED


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _has_text(result: ModelResult) -> bool:
    return bool(result.text.strip())


class ModelAccessLayer:
    """One adapter for every purpose, plus the configuration it reads."""

    def __init__(self, settings: Settings, adapter: ModelAdapter | None = None) -> None:
        self.settings = settings
        self.keybox = KeyBox(settings.config_key)
        self.cache = ConfigCache()
        self.response_format = ResponseFormatMemo()
        self._db: AsyncEngine | None = None
        self.adapter: ModelAdapter = adapter or self._make_adapter(settings)

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelAccessLayer:
        return cls(settings)

    def _make_adapter(self, settings: Settings) -> ModelAdapter:
        if settings.model_adapter == "stub":
            return StubAdapter(
                model_lookup=self._configured_model, latency_s=settings.stub_latency_s
            )
        return OpenAICompatibleAdapter(self.resolve, self.keybox)

    def bind(self, db: AsyncEngine | None) -> None:
        self._db = db
        self.cache.clear()
        self.response_format.clear()

    @property
    def db(self) -> AsyncEngine:
        if self._db is None:
            raise RuntimeError("the Model Access Layer has no database; bind one first")
        return self._db

    @property
    def stub(self) -> StubAdapter:
        """The stub adapter (``EPISTREL_MODEL_ADAPTER=stub`` only)."""
        if not isinstance(self.adapter, StubAdapter):
            raise RuntimeError("the model adapter is not the stub")
        return self.adapter

    async def resolve(self, purpose: str) -> EffectiveConfig:
        """``purpose``'s effective configuration as of the current ``config_revision``."""
        return await self.cache.effective(self.db, purpose)

    async def _configured_model(self, purpose: str) -> str | None:
        if self._db is None:
            return None
        return (await self.resolve(purpose)).model

    async def endpoint(self, purpose: str) -> tuple[str, str, int]:
        """``(base_url, model, config_revision)`` a call for ``purpose`` would use now (the
        adapter's name stands in for the URL when nothing is configured or bound)."""
        if self._db is None:
            return self.adapter.name, self.adapter.name, 0
        config = await self.resolve(purpose)
        return config.base_url or self.adapter.name, config.model or "", config.revision

    async def structured_settings(self) -> StructuredSettings:
        """``structured_retry_budget`` and ``uncertain_below`` as of the current revision."""
        if self._db is None:
            return StructuredSettings()
        return (await self.cache.structured(self.db))[1]

    async def _timeout(self, purpose: str, params: ModelParams) -> float:
        if params.timeout is not None:
            return params.timeout
        if self._db is None:
            return DEFAULT_TIMEOUT_S
        return (await self.resolve(purpose)).params.timeout or DEFAULT_TIMEOUT_S

    async def send(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams | None,
        response_schema: type[BaseModel] | None,
        *,
        budget: TurnBudget | None = None,
        attempt: int = 1,
        usable: Callable[[ModelResult], bool] | None = None,
    ) -> ModelResult:
        """One adapter call, exactly as given (``response_schema`` ``None`` sends no
        ``response_format``), bounded by the call's timeout and the budget's deadline and
        debited from the budget. Every provider failure leaves as a ``ModelError``.

        The attempt is recorded once (``metrics.record``: counters and the operation's
        ``model_calls``), as ``attempt`` of its caller's sequence; an answer ``usable`` rejects
        is recorded as ``invalid_output`` and still returned for the caller to handle."""
        get_delivered(purpose)
        params = params or ModelParams()
        if budget is not None:
            if budget.expired():
                raise ModelUnavailable(purpose, "deadline")
            if not budget.can_afford():
                raise BudgetExhausted(purpose, budget.used, budget.max_calls)
        limit, reason = await self._timeout(purpose, params), "timeout"
        remaining = budget.remaining_s() if budget is not None else None
        if remaining is not None and remaining < limit:
            limit, reason = remaining, "deadline"
        if budget is not None:
            budget.debit()
        model = await self._model_name(purpose)
        started = time.perf_counter()
        bound = asyncio.timeout(limit)
        try:
            async with bound:
                result = await self.adapter.complete(purpose, messages, params, response_schema)
        except TimeoutError:
            if not bound.expired():
                raise
            log.warning("model call failed purpose=%s reason=%s", purpose, reason)
            error = ModelUnavailable(purpose, reason)
            self._record_failure(purpose, model, attempt, started, error)
            raise error from None
        except asyncio.CancelledError as exc:
            self._record_failure(purpose, model, attempt, started, exc)
            raise
        except Exception as exc:
            mapped = map_provider_error(purpose, exc)
            if mapped is None:
                raise
            if _attempted(mapped):
                self._record_failure(purpose, model, attempt, started, mapped)
            # ``from None``: the provider's message can echo the prompt (invariant 3).
            raise mapped from None
        outcome: Outcome = "ok" if usable is None or usable(result) else "invalid_output"
        record(
            ModelCall(
                purpose=purpose,
                model=model or result.model,
                attempt=attempt,
                outcome=outcome,
                latency_ms=_elapsed_ms(started),
                tokens_in=result.tokens_in or 0,
                tokens_out=result.tokens_out or 0,
                cost=result.cost,
                request_id=result.request_id,
                finish_reason=result.finish_reason,
            )
        )
        return result

    async def _model_name(self, purpose: str) -> str:
        """The model a call for ``purpose`` is configured to use (the adapter's name when no
        database is bound)."""
        if self._db is None:
            return self.adapter.name
        return (await self.resolve(purpose)).model or ""

    @staticmethod
    def _record_failure(
        purpose: str, model: str, attempt: int, started: float, error: BaseException
    ) -> None:
        record(
            ModelCall(
                purpose=purpose,
                model=model or "unknown",
                attempt=attempt,
                outcome=outcome_of(error),
                latency_ms=_elapsed_ms(started),
                tokens_in=0,
                tokens_out=0,
            )
        )

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams | None = None,
        response_schema: type[BaseModel] | None = None,
        *,
        budget: TurnBudget | None = None,
    ) -> ModelResult:
        """Send prepared ``messages`` for a delivered ``purpose`` (its declared response schema
        unless one is given). An unstructured answer of only whitespace is ``EmptyCompletion``."""
        declared = get_delivered(purpose)
        schema = response_schema if response_schema is not None else declared.response_schema
        usable = _has_text if schema is None else None
        result = await self.send(purpose, messages, params, schema, budget=budget, usable=usable)
        if schema is None and not result.text.strip():
            raise EmptyCompletion(purpose)
        return result

    def prepare(
        self, purpose: str, inputs: InputSchema | Mapping[str, Any]
    ) -> tuple[Purpose, InputSchema, list[Message]]:
        """The delivered purpose, ``inputs`` validated as its schema, and the rendered prompt."""
        declared = get_delivered(purpose)
        payload = build_inputs(declared, inputs)
        return declared, payload, render(declared, payload)

    async def call(
        self,
        purpose: str,
        inputs: InputSchema | Mapping[str, Any],
        params: ModelParams | None = None,
        response_schema: type[BaseModel] | None = None,
        *,
        budget: TurnBudget | None = None,
    ) -> ModelResult:
        """Validate ``inputs`` against the purpose's schema, render its template, complete."""
        _, payload, messages = self.prepare(purpose, inputs)
        token = REQUEST_FIELDS.set(payload_fields(payload))
        try:
            return await self.complete(purpose, messages, params, response_schema, budget=budget)
        finally:
            REQUEST_FIELDS.reset(token)

    async def structured_call[T: BaseModel](
        self,
        purpose: str,
        schema: type[T],
        inputs: InputSchema | Mapping[str, Any],
        *,
        params: ModelParams | None = None,
        deterministic_first: DeterministicFirst[T] | None = None,
        budget: TurnBudget,
    ) -> T | Uncertain:
        """A validated ``schema`` instance or ``Uncertain`` (see ``structured.structured_call``)."""
        return await structured_call(
            self,
            purpose,
            schema,
            inputs,
            params=params,
            deterministic_first=deterministic_first,
            budget=budget,
        )

    async def probe(self, purpose: str) -> ProbeResult:
        """Protocol compatibility for ``purpose``'s endpoint (``ModelAdapter.probe``), bounded
        by ``PROBE_TIMEOUT_S``; every provider failure leaves as a ``ModelError``."""
        get_delivered(purpose)
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                return await self.adapter.probe(purpose)
        except TimeoutError:
            raise ModelUnavailable(purpose, "timeout") from None
        except Exception as exc:
            mapped = map_provider_error(purpose, exc)
            if mapped is None:
                raise
            raise mapped from None

    async def configuration(self) -> tuple[int, dict[str, ConfigEntry]]:
        """``config_revision`` and every configurable purpose (``default`` included)."""
        return await model_config.listing(self.db, self.keybox)

    async def aclose(self) -> None:
        await self.adapter.aclose()


__all__ = [
    "ModelAccessLayer",
    "build_inputs",
    "declared_fields",
    "get_delivered",
    "payload_fields",
]
