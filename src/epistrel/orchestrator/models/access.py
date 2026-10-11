"""The Model Access Layer: every model call goes through here (FR-MODEL-1, NFR-PORT-2).

``ModelAccessLayer.from_settings`` picks the adapter from ``EPISTREL_MODEL_ADAPTER``
(``openai`` or ``stub``); ``epistrel.Engine`` owns one and binds it to the database. A call
names a delivered purpose: ``call(purpose, inputs, params)`` validates ``inputs`` against the
purpose's input schema (an undeclared field is refused), renders its template, and completes;
``complete(purpose, messages, params)`` sends prepared messages. Under the stub, calls never
need ``model_config``: the stub only reports the configured model name when a database is bound.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.models.adapter import (
    REQUEST_FIELDS,
    Message,
    ModelAdapter,
    ModelParams,
    ModelResult,
    UndeclaredInput,
    UnknownPurpose,
)
from epistrel.orchestrator.models.config import ConfigCache, ConfigEntry, EffectiveConfig, KeyBox
from epistrel.orchestrator.models.openai_compatible import OpenAICompatibleAdapter
from epistrel.orchestrator.models.prompts import render
from epistrel.orchestrator.models.registry import (
    DELIVERED,
    RESERVED,
    InputSchema,
    Purpose,
    declared_paths,
)
from epistrel.orchestrator.models.stub import StubAdapter


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


def _paths(value: Any, prefix: str = "") -> list[str]:
    """The field paths actually present in a payload (list indexes dropped)."""
    found: list[str] = []
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            path = f"{prefix}{name}"
            found.append(path)
            found.extend(_paths(getattr(value, name), f"{path}."))
    elif isinstance(value, list | tuple):
        for item in value:
            found.extend(p for p in _paths(item, prefix) if p not in found)
    return found


def payload_fields(inputs: InputSchema) -> tuple[str, ...]:
    """Sorted, de-duplicated field paths of a validated payload."""
    return tuple(sorted(set(_paths(inputs))))


def declared_fields(purpose: Purpose) -> frozenset[str]:
    """Every field path ``purpose`` declares (nested models included)."""
    return declared_paths(purpose.input_schema)


class ModelAccessLayer:
    """One adapter for every purpose, plus the configuration it reads."""

    def __init__(self, settings: Settings, adapter: ModelAdapter | None = None) -> None:
        self.settings = settings
        self.keybox = KeyBox(settings.config_key)
        self.cache = ConfigCache()
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

    async def complete(
        self,
        purpose: str,
        messages: Sequence[Message],
        params: ModelParams | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        """Send prepared ``messages`` for a delivered ``purpose``."""
        declared = get_delivered(purpose)
        schema = response_schema if response_schema is not None else declared.response_schema
        return await self.adapter.complete(purpose, messages, params or ModelParams(), schema)

    async def call(
        self,
        purpose: str,
        inputs: InputSchema | Mapping[str, Any],
        params: ModelParams | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResult:
        """Validate ``inputs`` against the purpose's schema, render its template, complete."""
        declared = get_delivered(purpose)
        payload = build_inputs(declared, inputs)
        messages = render(declared, payload)
        token = REQUEST_FIELDS.set(payload_fields(payload))
        try:
            return await self.complete(purpose, messages, params, response_schema)
        finally:
            REQUEST_FIELDS.reset(token)

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
