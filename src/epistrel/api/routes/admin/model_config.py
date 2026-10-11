"""``/admin/model-config``: per-purpose model configuration (FR-MODEL-1..3).

``GET`` lists the ``default`` row and every **delivered** purpose with its effective
configuration (reserved purposes are not listed, FR-MODEL-2). API keys are never returned:
``api_key`` is ``"***"`` when one applies, else ``null``. #52 adds the writes.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.orchestrator.models.config import EffectiveConfig

router = APIRouter(prefix="/model-config")

MASK = "***"


class ModelConfigEntry(BaseModel):
    base_url: str | None
    model: str | None
    params: dict[str, Any]
    api_key: Literal["***"] | None

    @classmethod
    def of(cls, config: EffectiveConfig) -> ModelConfigEntry:
        return cls(
            base_url=config.base_url,
            model=config.model,
            params=config.params.wire(),
            api_key=MASK if config.has_api_key else None,
        )


class ModelConfigBody(BaseModel):
    adapter: Literal["openai", "stub"]
    default: ModelConfigEntry
    purposes: dict[str, ModelConfigEntry]


@router.get("", response_model=ModelConfigBody)
async def read_model_config(ctx: ContextDep, engine: EngineDep) -> ModelConfigBody:
    default, purposes = await engine.model_configuration(ctx)
    return ModelConfigBody(
        adapter=engine.settings.model_adapter,
        default=ModelConfigEntry.of(default),
        purposes={name: ModelConfigEntry.of(config) for name, config in purposes.items()},
    )
