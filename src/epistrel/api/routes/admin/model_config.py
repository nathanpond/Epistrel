"""``/admin/model-config``: per-purpose model configuration, read and changed at runtime
(FR-MODEL-1..4; NFR-SEC-3).

- ``GET`` lists ``default`` and every **delivered** purpose (reserved ones are not listed,
  FR-MODEL-2) with its effective configuration, the ``source`` (``own``/``default``) of each
  field, and the deployment's ``config_revision``; ``GET /{purpose}`` returns one.
- ``PUT /{purpose}`` replaces the purpose's own ``base_url``, ``model`` and ``params`` (omitted or
  ``null`` inherits from ``default``); 200 with the effective entry and ``config_revision``.
- ``DELETE /{purpose}`` drops the purpose's override (200); ``default`` cannot be removed (409);
  nothing to drop is 204.
- ``PUT|DELETE /{purpose}/api-key`` set or remove the purpose's key (204).

API keys are never returned: ``api_key`` is ``"***"`` when one applies, else ``null``, with the
first 8 hex of its SHA-256 as ``api_key_fingerprint``. Every write takes effect on the next
model call, without a restart, and is recorded as a prospective configuration change.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.context import RequestContext
from epistrel.engine import Engine
from epistrel.orchestrator.models.config import (
    ApiKeyBody,
    ConfigEntry,
    ConfigReplacement,
    ConfigWrite,
)

router = APIRouter(prefix="/model-config")

MASK = "***"

type Source = Literal["own", "default"]


class Sources(BaseModel):
    """Where each effective value came from; ``params`` per key."""

    base_url: Source
    model: Source
    api_key: Source
    params: dict[str, Source]


class ModelConfigEntry(BaseModel):
    base_url: str | None
    model: str | None
    params: dict[str, Any]
    api_key: Literal["***"] | None
    api_key_fingerprint: str | None
    sources: Sources

    @classmethod
    def fields_of(cls, entry: ConfigEntry) -> dict[str, Any]:
        return {
            "base_url": entry.base_url,
            "model": entry.model,
            "params": entry.params,
            "api_key": MASK if entry.has_api_key else None,
            "api_key_fingerprint": entry.api_key_fingerprint,
            "sources": Sources.model_validate(entry.sources),
        }

    @classmethod
    def of(cls, entry: ConfigEntry) -> ModelConfigEntry:
        return cls(**cls.fields_of(entry))


class ModelConfigBody(BaseModel):
    adapter: Literal["openai", "stub"]
    config_revision: int
    purposes: dict[str, ModelConfigEntry]


class ModelConfigItem(ModelConfigEntry):
    """One purpose, with the ``config_revision`` it was read at."""

    purpose: str
    config_revision: int


class ModelConfigMutation(ModelConfigEntry, DeploymentMutationResponse):
    """One purpose after a write, with the ``config_revision`` the write produced."""

    purpose: str


@router.get("", response_model=ModelConfigBody)
async def read_model_config(ctx: ContextDep, engine: EngineDep) -> ModelConfigBody:
    revision, entries = await engine.model_configuration(ctx)
    return ModelConfigBody(
        adapter=engine.settings.model_adapter,
        config_revision=revision,
        purposes={name: ModelConfigEntry.of(entry) for name, entry in entries.items()},
    )


@router.get("/{purpose}", response_model=ModelConfigItem)
async def read_purpose_config(purpose: str, ctx: ContextDep, engine: EngineDep) -> ModelConfigItem:
    revision, entry = await engine.model_config_entry(ctx, purpose)
    return ModelConfigItem(
        purpose=purpose, config_revision=revision, **ModelConfigEntry.fields_of(entry)
    )


async def _mutation(
    engine: Engine, ctx: RequestContext, purpose: str, write: ConfigWrite, response: Response
) -> ModelConfigMutation:
    """The purpose's entry after a write (read back, so a replay shows the current entry with
    the stored write's revision)."""
    _, entry = await engine.model_config_entry(ctx, purpose)
    answer = ModelConfigMutation(
        purpose=purpose, config_revision=write.config_revision, **ModelConfigEntry.fields_of(entry)
    )
    return answered(answer, ctx, response, replayed=write.replayed)


@router.put("/{purpose}", response_model=ModelConfigMutation)
async def replace_purpose_config(
    purpose: str, body: ConfigReplacement, response: Response, ctx: ContextDep, engine: EngineDep
) -> ModelConfigMutation:
    """Replace the purpose's own configuration; a PUT that changes nothing writes nothing."""
    write = await engine.replace_model_config(ctx, purpose, body)
    return await _mutation(engine, ctx, purpose, write, response)


@router.delete(
    "/{purpose}",
    response_model=ModelConfigMutation,
    responses={status.HTTP_204_NO_CONTENT: {"description": "No override; nothing changed."}},
)
async def remove_purpose_override(
    purpose: str, response: Response, ctx: ContextDep, engine: EngineDep
) -> ModelConfigMutation | Response:
    """Drop the override (200 with the now-inherited entry); 204 when there was none; 409 for
    ``default``."""
    write = await engine.remove_model_override(ctx, purpose)
    if not write.changed:
        return no_content(ctx, replayed=write.replayed)
    return await _mutation(engine, ctx, purpose, write, response)


@router.put("/{purpose}/api-key", status_code=status.HTTP_204_NO_CONTENT)
async def set_purpose_api_key(
    purpose: str, body: ApiKeyBody, ctx: ContextDep, engine: EngineDep
) -> Response:
    """Store the purpose's API key (encrypted at rest; never returned). 503
    ``config_key_missing`` without ``EPISTREL_CONFIG_KEY``."""
    write = await engine.set_model_api_key(ctx, purpose, body.api_key)
    return no_content(ctx, replayed=write.replayed)


@router.delete("/{purpose}/api-key", status_code=status.HTTP_204_NO_CONTENT)
async def remove_purpose_api_key(purpose: str, ctx: ContextDep, engine: EngineDep) -> Response:
    """Remove the purpose's own API key; it then inherits ``default``'s, if any."""
    write = await engine.remove_model_api_key(ctx, purpose)
    return no_content(ctx, replayed=write.replayed)
