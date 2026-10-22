"""The Model Access Layer: every model call goes through here (FR-MODEL-1, NFR-PORT-2).

``ModelAccessLayer.from_settings`` picks the adapter from ``EPISTREL_MODEL_ADAPTER``
(``openai`` or ``stub``); ``epistrel.Engine`` owns one and binds it to the database. A call
names a delivered purpose: ``call(purpose, inputs, params)`` validates ``inputs`` against the
purpose's input schema (an undeclared field is refused), renders its template, and completes;
``complete(purpose, messages, params)`` sends prepared messages. Under the stub, calls never
need ``model_config``: the stub only reports the configured model name when a database is bound.

**Routing** (#325): ``routing(routes)`` scopes a story's per-purpose routes (``config.Route``,
from its effective configuration's ``models.purposes``) to the calls made inside it, in this task
and the tasks it starts. ``resolve`` lays the route over the deployment's configuration
(``config.routed``), so the adapter, the model a call is recorded under and the stub's reported
model all follow it. A Lite turn routes from the configuration it captured at its start, and the
``lite_summary`` job from the story's; calls outside a story (rating suggestions, catalog checks,
probes) use the deployment's configuration.

Every call goes through ``send``, which is where provider failures stop (FR-MODEL-6, NFR-REL-3):
the call is bounded by its timeout (the purpose's ``params.timeout``, default 60 s) and by the
turn's deadline when a ``TurnBudget`` is passed, whichever is sooner (``TurnBudget.clamp``), and
debits that budget: a first attempt may spend a reserved slot, a retry (``attempt`` > 1) only
the unreserved remainder, and a call the budget cannot afford is not made (``BudgetExhausted``,
#69);
every SDK exception becomes a ``ModelError`` (``errors.map_provider_error``). An unstructured
call that answers only whitespace raises ``EmptyCompletion``. Structured results go through
``structured_call`` (``structured.py``), which never returns a partially parsed object.

**Eval scope** (#134): inside ``eval_scope(case_id, seed)`` every call ``send`` makes carries
an ``X-Eval-Case: <case id>`` line appended to its system message (a system message of its own
when the prompt has none), and the run's ``seed`` when the call's params set none. Only the eval
harness opens one; outside it nothing is added, so every other prompt is byte-identical.

A completion's text leaves ``send`` without NUL characters (#275): PostgreSQL cannot store one,
and a model's answer reaches event payloads (a generation, a guardrail's reason), so a NUL failed
the commit with a database error. The model wrote it, not the caller, so it is removed rather
than refused; only the purpose and the count are logged (invariant 3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.errors import NUL
from epistrel.core.settings_schema import StructuredSettings
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.adapter import (
    DEFAULT_TIMEOUT_S,
    EVAL_CASE_HEADER,
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
    Route,
    routed,
)
from epistrel.orchestrator.models.errors import (
    BudgetExhausted,
    EmptyCompletion,
    ModelError,
    ModelUnavailable,
    map_provider_error,
)
from epistrel.orchestrator.models.metrics import (
    ModelCall,
    Outcome,
    call_started,
    outcome_of,
    record,
)
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import payload_fields, render
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    PORTS,
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

if TYPE_CHECKING:
    from epistrel.orchestrator.embeddings.service import EmbeddingService

log = logging.getLogger(__name__)


NO_ROUTES: Mapping[str, Route] = MappingProxyType({})
ROUTES: ContextVar[Mapping[str, Route]] = ContextVar("ROUTES", default=NO_ROUTES)
"""The per-purpose routes of the story whose calls are being made (``routing``)."""


@contextmanager
def routing(routes: Mapping[str, Route]) -> Iterator[None]:
    """Route the calls made inside the block by ``routes`` (a story's ``config.routes_of``)."""
    token = ROUTES.set(MappingProxyType(dict(routes)))
    try:
        yield
    finally:
        ROUTES.reset(token)


@dataclass(frozen=True, slots=True)
class EvalScope:
    """The eval case the calls in progress belong to, and the run's seed (#134)."""

    case_id: str
    seed: int | None = None


EVAL_SCOPE: ContextVar[EvalScope | None] = ContextVar("EVAL_SCOPE", default=None)
"""The eval case of the calls being made (``eval_scope``); ``None`` outside the harness."""


@contextmanager
def eval_scope(case_id: str, seed: int | None = None) -> Iterator[None]:
    """Mark the calls made inside the block as eval case ``case_id`` under ``seed`` (see the
    module docstring). A case id is one token: no whitespace."""
    if not case_id or any(ch.isspace() for ch in case_id):
        raise ValueError(f"an eval case id is one non-empty token, got {case_id!r}")
    token = EVAL_SCOPE.set(EvalScope(case_id, seed))
    try:
        yield
    finally:
        EVAL_SCOPE.reset(token)


def _in_scope(
    messages: Sequence[Message], params: ModelParams
) -> tuple[Sequence[Message], ModelParams]:
    """``messages`` and ``params`` as the current eval scope sends them (unchanged outside one)."""
    scope = EVAL_SCOPE.get()
    if scope is None:
        return messages, params
    line = f"{EVAL_CASE_HEADER}: {scope.case_id}"
    marked = list(messages)
    first = next((i for i, m in enumerate(marked) if m.role == "system"), None)
    if first is None:
        marked.insert(0, Message(role="system", content=line))
    else:
        marked[first] = Message(role="system", content=f"{marked[first].content}\n\n{line}")
    if params.seed is None and scope.seed is not None:
        params = params.model_copy(update={"seed": scope.seed})
    return marked, params


def get_delivered(name: str) -> Purpose:
    """The delivered purpose ``name``; 422 for a reserved or unknown one."""
    purpose = DELIVERED.get(name)
    if purpose is not None:
        return purpose
    if name in PORTS:
        raise UnknownPurpose(
            f"{name!r} is a port, not a chat purpose",
            details={"purpose": name, "reason": "port"},
        )
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


def _without_nul(purpose: str, result: ModelResult) -> ModelResult:
    """``result`` with the NUL characters removed from its text (#275; see the module
    docstring)."""
    count = result.text.count(NUL)
    if not count:
        return result
    log.warning("model answer held NUL characters purpose=%s removed=%d", purpose, count)
    return result.model_copy(update={"text": result.text.replace(NUL, "")})


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
        # The embedding port (#138): its own adapter (EPISTREL_EMBEDDING_ADAPTER), row and
        # accounting. Imported here: the embeddings package imports this one's modules.
        from epistrel.orchestrator.embeddings.service import EmbeddingService

        self.embeddings: EmbeddingService = EmbeddingService(settings, self.keybox)

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelAccessLayer:
        return cls(settings)

    def _make_adapter(self, settings: Settings) -> ModelAdapter:
        if settings.model_adapter == "stub":
            return StubAdapter(
                model_lookup=self._configured_model,
                latency_s=settings.stub_latency_s,
                render_claims=[
                    c.model_dump(exclude_none=True) for c in settings.stub_render_claims
                ],
            )
        return OpenAICompatibleAdapter(self.resolve, self.keybox)

    def bind(self, db: AsyncEngine | None) -> None:
        self._db = db
        self.cache.clear()
        self.response_format.clear()
        self.embeddings.bind(db)

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
        """``purpose``'s effective configuration as of the current ``config_revision``, under
        the current story's route for it (``routing``) when it has one."""
        return routed(await self.cache.effective(self.db, purpose), ROUTES.get().get(purpose))

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
        messages, params = _in_scope(messages, params or ModelParams())
        first = attempt == 1
        if budget is not None:
            if budget.expired():
                raise ModelUnavailable(purpose, "deadline")
            # A first attempt may spend a reserved slot (a named one only its owner, #156); a
            # retry only the unreserved remainder.
            if not budget.affordable(purpose, first=first):
                raise BudgetExhausted(purpose, budget.used, budget.max_calls)
        limit, reason = await self._timeout(purpose, params), "timeout"
        if budget is not None:
            limit, reason = budget.clamp(limit)
            budget.debit(claim=first, purpose=purpose)
        model = await self._model_name(purpose)
        call_started()
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
            elif budget is not None:
                budget.refund(purpose)  # nothing was sent: the budget counts recorded calls only
            # ``from None``: the provider's message can echo the prompt (invariant 3).
            raise mapped from None
        result = _without_nul(purpose, result)
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
        self,
        purpose: str,
        inputs: InputSchema | Mapping[str, Any],
        *,
        variant: str | None = None,
    ) -> tuple[Purpose, InputSchema, list[Message]]:
        """The delivered purpose, ``inputs`` validated as its schema, and the rendered prompt
        (through the purpose's template ``variant`` when one is named, #150)."""
        declared = get_delivered(purpose)
        payload = build_inputs(declared, inputs)
        return declared, payload, render(declared, payload, variant)

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
        variant: str | None = None,
        failures: list[str] | None = None,
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
            variant=variant,
            failures=failures,
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
        await self.embeddings.aclose()


__all__ = [
    "EVAL_SCOPE",
    "NO_ROUTES",
    "ROUTES",
    "EvalScope",
    "ModelAccessLayer",
    "build_inputs",
    "declared_fields",
    "eval_scope",
    "get_delivered",
    "payload_fields",
    "routing",
]
