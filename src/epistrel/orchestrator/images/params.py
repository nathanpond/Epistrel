"""The ``image_generation`` port's ``params`` (FR-IMG-1; #254, owner decision 11).

The image adapter is configured as the ``image_generation`` port: its own ``model_config`` row
(``base_url``, ``model``, an encrypted API key) with the image fields inside ``params``, written
through the shared ``PUT /admin/model-config/image_generation`` body
``{base_url, model, params}`` and ``PUT …/image_generation/api-key``. Nothing falls back to the
``default`` row (a chat model is not an image model). ``params`` hold:

- ``adapter``: the registered adapter's name (``openrouter``, #255), or ``null`` (the default:
  image generation is off, and every generation entry point answers 409 ``not_configured``);
- ``default_size`` (``1024x1024``) and ``default_aspect_ratio`` (``16:9``): either or both may
  be stored; ``default_quality`` (``low|medium|high|auto``);
- ``image_capability_ttl_s``: how long an adapter may cache what it discovered about the
  provider's endpoints (#255);
- ``request_timeout_s``: the bound on one provider call, over the adapter's own
  (``Capabilities.request_timeout_s``).

Unset values take the built-in defaults (``BUILTIN_DEFAULTS``): no size, ratio ``1:1``,
quality ``auto``. A request uses its own size or ratio, else the configured size, else the
configured ratio (``resolve_dimensions``).

Whether ``adapter`` names a registered adapter is checked when the row is written (422, field
``body.params.adapter``), not when it is read: an adapter no longer registered in this process
reads back as stored and is ``not_configured`` (``registry.require_image_adapter``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from epistrel.orchestrator.images.port import ASPECT_RATIO_PATTERN, SIZE_PATTERN, Quality

IMAGE_GENERATION_PORT = "image_generation"
MAX_ADAPTER_NAME_CHARS = 64

DEFAULT_ASPECT_RATIO = "1:1"
DEFAULT_QUALITY: Quality = "auto"
BUILTIN_DEFAULTS: dict[str, Any] = {
    "default_size": None,
    "default_aspect_ratio": DEFAULT_ASPECT_RATIO,
    "default_quality": DEFAULT_QUALITY,
}

AdapterName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_ADAPTER_NAME_CHARS,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class ImageGenerationParams(BaseModel):
    """The ``image_generation`` row's ``params``: replaced in full by a ``PUT``; ``None``
    takes the built-in default (module docs)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "adapter": "openrouter",
                    "default_aspect_ratio": "1:1",
                    "default_quality": "auto",
                    "request_timeout_s": 120,
                }
            ]
        },
    )

    adapter: AdapterName | None = None
    """The registered image adapter, or ``null`` (image generation off)."""
    default_size: Annotated[str, StringConstraints(pattern=SIZE_PATTERN)] | None = None
    default_aspect_ratio: Annotated[str, StringConstraints(pattern=ASPECT_RATIO_PATTERN)] | None = (
        None
    )
    default_quality: Literal["low", "medium", "high", "auto"] | None = None
    image_capability_ttl_s: StrictInt | None = Field(default=None, ge=60, le=86_400)
    request_timeout_s: StrictInt | None = Field(default=None, ge=10, le=600)

    def wire(self) -> dict[str, Any]:
        """The set keys, JSON-ready (what the row stores and the read shows)."""
        return self.model_dump(exclude_none=True)


def resolve_dimensions(
    params: ImageGenerationParams, *, size: str | None = None, aspect_ratio: str | None = None
) -> tuple[str | None, str | None]:
    """``(size, aspect_ratio)`` for one request, at most one of them set: the request's own,
    else the configured size, else the configured ratio, else the built-in ``1:1``."""
    if size is not None:
        return size, None
    if aspect_ratio is not None:
        return None, aspect_ratio
    if params.default_size is not None:
        return params.default_size, None
    return None, params.default_aspect_ratio or DEFAULT_ASPECT_RATIO


def resolve_quality(params: ImageGenerationParams, quality: Quality | None = None) -> Quality:
    """The request's quality, else the configured one, else ``auto``."""
    return quality or params.default_quality or DEFAULT_QUALITY


__all__ = [
    "BUILTIN_DEFAULTS",
    "DEFAULT_ASPECT_RATIO",
    "DEFAULT_QUALITY",
    "IMAGE_GENERATION_PORT",
    "ImageGenerationParams",
    "resolve_dimensions",
    "resolve_quality",
]
