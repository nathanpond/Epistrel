"""Image adapters: registration, the adapter the model configuration names, and the
``image_check`` fail-closed rule (FR-IMG-1,3; docs/02 §4.13; #254).

**Registration.** ``register_image_adapter(name, factory)`` adds an adapter to this process's
table. ``factory`` is called with an ``ImageAdapterConfig`` (the ``image_generation`` row's
model, endpoint, decrypted key and ``params``) and returns an ``ImageGenerator``; it declares
the adapter's ``capabilities()`` itself (an adapter class's classmethod, or a test stub's
method), and its ``status``/``cancel`` when it confirms completion. Registration refuses, with
``ImageAdapterInvalid`` naming the rule:

1. ``sync_confirms``: ``confirms_completion`` with ``submission = sync``;
2. ``missing_status_cancel``: ``confirms_completion`` without both ``status`` and ``cancel``;
3. ``no_request_id``: ``confirms_completion`` without ``returns_request_id``.

A name is registered once (``replace=True`` lets a test swap it). The bundled adapters register
in ``register_builtin_image_adapters()``, which the app factory calls (#255 adds OpenRouter).

**The configured adapter.** ``image_generation`` is a ``model_config`` port row
(``orchestrator.models.config``; ``params`` are ``images.params.ImageGenerationParams``). It is
read through the Model Access Layer's revision cache, so a write takes effect on the next call
without a restart (#52). ``ImageAdapters.get()`` (``require_image_adapter``) builds the adapter
once per configuration revision and hands out an ``ImageGeneratorHandle``; a superseded
instance is ``aclose()``d once its in-flight calls finish. The handle sends the idempotency key
only to an adapter that declares ``idempotency_keys``. With no adapter (``params.adapter``
null, or a name not registered in this process), no model or no key, it raises
``ImageNotConfigured``: 409 ``not_configured`` with ``details.missing`` ``adapter``, ``model``
or ``api_key``. The REST dependency is ``epistrel.api.deps.ImageAdapterDep``; it runs after
authentication and the target's 404, before the body is validated. ``POST /media`` uploads
never call it.

**Image checks.** ``image_check`` has no default model. ``ensure_image_check_configured``
answers its effective configuration when some policy targets ``image_output``, and raises
``ImageCheckUnconfigured`` naming every such policy while it is unset: the job fails closed
(#256 surfaces it as the job's ``status_reason``, never an HTTP code).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import SecretStr

from epistrel.core.errors import EpistrelError
from epistrel.orchestrator.images.params import IMAGE_GENERATION_PORT, ImageGenerationParams
from epistrel.orchestrator.images.port import (
    CancelResult,
    Capabilities,
    CollectResult,
    ConfirmingImageGenerator,
    ImageGenerator,
    ImageRequest,
    StatusResult,
    SubmitResult,
)
from epistrel.orchestrator.models.config import EffectiveConfig
from epistrel.orchestrator.models.registry import IMAGE_CHECK

if TYPE_CHECKING:
    from epistrel.orchestrator.images.references import ReferenceResolver
    from epistrel.orchestrator.models.access import ModelAccessLayer

IMAGE_OUTPUT = "image_output"
"""The policy target an ``image_check`` call judges (``core.policies.model.Target``)."""

type InvalidRule = Literal["sync_confirms", "missing_status_cancel", "no_request_id"]
type Missing = Literal["adapter", "model", "api_key"]

RULES: Mapping[InvalidRule, str] = {
    "sync_confirms": "confirms_completion needs async submission",
    "missing_status_cancel": "confirms_completion needs both status and cancel",
    "no_request_id": "confirms_completion needs a provider request id per call",
}


# -- errors -----------------------------------------------------------------------------------


class ImageAdapterInvalid(Exception):
    """An adapter's declaration breaks a registration rule (module docs)."""

    def __init__(self, adapter: str, rule: InvalidRule) -> None:
        self.adapter = adapter
        self.rule: InvalidRule = rule
        super().__init__(f"image adapter {adapter!r}: {rule}: {RULES[rule]}")


class ImageNotConfigured(EpistrelError):
    """Image generation has no usable adapter: 409 ``not_configured``. ``details.missing``
    names the first thing missing: ``adapter`` (none, or not registered in this process),
    ``model`` or ``api_key``. Nothing is queued, reserved or charged; uploads still work."""

    code = "not_configured"
    http_status = 409

    def __init__(self, missing: Missing, adapter: str | None = None) -> None:
        details: dict[str, Any] = {"missing": missing}
        if adapter is not None:
            details["adapter"] = adapter
        super().__init__(f"image generation is not configured: no {missing}", details)
        self.missing: Missing = missing


class ImageCheckUnconfigured(Exception):
    """An image-output policy applies and ``image_check`` has no model: the image cannot be
    checked, so it fails closed (``code`` ``image_check_unconfigured``, naming every policy it
    could not run). A job's ``status_reason`` (#256), never an HTTP answer."""

    code = "image_check_unconfigured"

    def __init__(self, policy_ids: Iterable[str]) -> None:
        self.policy_ids = tuple(sorted(set(policy_ids)))
        super().__init__(
            f"{self.code}: no image_check model for {', '.join(self.policy_ids) or 'no policy'}"
        )


class ConfirmationUnsupported(Exception):
    """``status`` or ``cancel`` on an adapter that does not declare ``confirms_completion``."""


# -- registration ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageAdapterConfig:
    """What a factory builds an adapter from: the ``image_generation`` row as of one
    configuration revision. ``base_url`` ``None`` means the adapter's own default (#255:
    ``https://openrouter.ai``). The key is decrypted only to build the adapter. ``references``
    turns reference media ids into URLs the provider can fetch (#255; the Engine's
    ``images.references.MediaReferences``), ``None`` when nothing can resolve them."""

    adapter: str
    model: str
    base_url: str | None
    api_key: SecretStr
    params: ImageGenerationParams
    revision: int
    references: ReferenceResolver | None = None


@runtime_checkable
class ImageAdapterFactory(Protocol):
    """Builds one adapter instance per configuration, and declares the adapter's capabilities
    before any instance exists (an adapter class whose ``capabilities`` is a classmethod is
    one)."""

    def __call__(self, config: ImageAdapterConfig) -> ImageGenerator: ...

    def capabilities(self) -> Capabilities: ...


_FACTORIES: dict[str, ImageAdapterFactory] = {}


def _confirms(factory: object) -> bool:
    return callable(getattr(factory, "status", None)) and callable(getattr(factory, "cancel", None))


def check_declaration(name: str, factory: ImageAdapterFactory) -> Capabilities:
    """``factory``'s capabilities, or ``ImageAdapterInvalid`` naming the first rule broken."""
    declared = factory.capabilities()
    if declared.confirms_completion:
        if declared.submission == "sync":
            raise ImageAdapterInvalid(name, "sync_confirms")
        if not _confirms(factory):
            raise ImageAdapterInvalid(name, "missing_status_cancel")
        if not declared.returns_request_id:
            raise ImageAdapterInvalid(name, "no_request_id")
    return declared


def register_image_adapter(
    name: str, factory: ImageAdapterFactory, *, replace: bool = False
) -> Capabilities:
    """Add ``factory`` under ``name`` (what ``params.adapter`` names) after checking its
    declaration; a name already taken is refused unless ``replace``. Answers the declared
    capabilities."""
    declared = check_declaration(name, factory)
    if name in _FACTORIES and not replace:
        raise ValueError(f"image adapter {name!r} is already registered")
    _FACTORIES[name] = factory
    return declared


def unregister_image_adapter(name: str) -> None:
    """Remove ``name`` (tests; a configuration naming it then reads ``not_configured``)."""
    _FACTORIES.pop(name, None)


def registered_adapters() -> frozenset[str]:
    """The adapter names registered in this process."""
    return frozenset(_FACTORIES)


def _openrouter() -> ImageAdapterFactory:
    # Imported when registered: the adapter module imports this one.
    from epistrel.orchestrator.images.adapters.openrouter import OpenRouterImageGenerator

    return OpenRouterImageGenerator


BUILTIN_IMAGE_ADAPTERS: Mapping[str, Callable[[], ImageAdapterFactory]] = {
    "openrouter": _openrouter,
}
"""The bundled adapters by name, each with a loader that returns its factory: ``openrouter``
(#255)."""


def register_builtin_image_adapters() -> frozenset[str]:
    """Register every bundled adapter not already registered (idempotent; the app factory
    calls it). Answers the names registered."""
    for name, load in BUILTIN_IMAGE_ADAPTERS.items():
        if name not in _FACTORIES:
            register_image_adapter(name, load())
    return registered_adapters()


# -- the configured adapter ------------------------------------------------------------------


def missing_of(config: EffectiveConfig) -> Missing | None:
    """What ``config`` (the ``image_generation`` row) lacks for a generation, in the order an
    operator fixes it: an adapter registered here, a model, a key; ``None`` when complete."""
    params = config.image_params or ImageGenerationParams()
    if params.adapter is None or params.adapter not in _FACTORIES:
        return "adapter"
    if config.model is None:
        return "model"
    if config.api_key_ciphertext is None:
        return "api_key"
    return None


@dataclass(eq=False)
class _Live:
    """One built adapter instance and the calls currently using it."""

    instance: ImageGenerator
    capabilities: Capabilities
    key: tuple[Any, ...]
    in_flight: int = 0
    retired: bool = False


@dataclass(eq=False)
class ImageGeneratorHandle:
    """What the engine calls instead of the adapter: the idempotency key reaches the adapter
    only when it declares ``idempotency_keys``; ``status`` and ``cancel`` only when it declares
    ``confirms_completion``; and the instance stays open until its last call ends, even after a
    configuration write supersedes it."""

    adapter: str
    model: str
    _live: _Live
    _owner: ImageAdapters = field(repr=False)

    def capabilities(self) -> Capabilities:
        return self._live.capabilities

    async def _run[T](self, call: Callable[[ImageGenerator], Awaitable[T]]) -> T:
        self._live.in_flight += 1
        try:
            result: T = await call(self._live.instance)
            return result
        finally:
            await self._owner.release(self._live)

    async def submit(self, request: ImageRequest, idempotency_key: str | None) -> SubmitResult:
        key = idempotency_key if self._live.capabilities.idempotency_keys else None
        return await self._run(lambda adapter: adapter.submit(request, key))

    async def collect(self, attempt_ref: str) -> CollectResult:
        return await self._run(lambda adapter: adapter.collect(attempt_ref))

    def _confirming(self) -> None:
        if not self._live.capabilities.confirms_completion:
            raise ConfirmationUnsupported(f"{self.adapter} does not confirm completion")

    async def status(self, attempt_ref: str) -> StatusResult:
        self._confirming()

        async def call(adapter: ImageGenerator) -> StatusResult:
            assert isinstance(adapter, ConfirmingImageGenerator)  # noqa: S101 - checked at registration
            return await adapter.status(attempt_ref)

        return await self._run(call)

    async def cancel(self, attempt_ref: str) -> CancelResult:
        self._confirming()

        async def call(adapter: ImageGenerator) -> CancelResult:
            assert isinstance(adapter, ConfirmingImageGenerator)  # noqa: S101 - checked at registration
            return await adapter.cancel(attempt_ref)

        return await self._run(call)


class ImageAdapters:
    """The Model Access Layer's image side (``ModelAccessLayer.images``): the adapter the
    ``image_generation`` row names, built once per configuration and rebuilt after a write."""

    def __init__(self, models: ModelAccessLayer) -> None:
        self._models = models
        self._current: _Live | None = None
        self.references: ReferenceResolver | None = None
        """What resolves reference images for the adapters built here (the Engine sets it)."""
        self.closed = 0
        """How many superseded instances have been closed (observable by tests)."""

    async def config(self) -> EffectiveConfig:
        """The ``image_generation`` row as of the current configuration revision."""
        return await self._models.cache.effective(self._models.db, IMAGE_GENERATION_PORT)

    async def get(self) -> ImageGeneratorHandle:
        """The configured adapter's handle; ``ImageNotConfigured`` without one."""
        config = await self.config()
        missing = missing_of(config)
        params = config.image_params or ImageGenerationParams()
        if missing is not None:
            raise ImageNotConfigured(missing, params.adapter)
        assert params.adapter is not None  # noqa: S101 - missing_of
        assert config.model is not None  # noqa: S101 - missing_of
        assert config.api_key_ciphertext is not None  # noqa: S101 - missing_of
        factory = _FACTORIES[params.adapter]
        key = (
            params.adapter,
            id(factory),
            config.model,
            config.base_url,
            config.api_key_ciphertext,
            params,
        )
        live = self._current
        if live is None or live.key != key:
            api_key = self._models.keybox.decrypt(
                config.api_key_ciphertext, purpose=IMAGE_GENERATION_PORT
            )
            built = factory(
                ImageAdapterConfig(
                    adapter=params.adapter,
                    model=config.model,
                    base_url=config.base_url,
                    api_key=api_key,
                    params=params,
                    revision=config.revision,
                    references=self.references,
                )
            )
            fresh = _Live(built, check_declaration(params.adapter, factory), key)
            self._current = fresh
            if live is not None:
                await self._retire(live)
            live = fresh
        return ImageGeneratorHandle(params.adapter, config.model, live, self)

    async def _retire(self, live: _Live) -> None:
        live.retired = True
        if live.in_flight == 0:
            await self._close(live)

    async def release(self, live: _Live) -> None:
        live.in_flight -= 1
        if live.retired and live.in_flight == 0:
            await self._close(live)

    async def _close(self, live: _Live) -> None:
        await live.instance.aclose()
        self.closed += 1

    async def aclose(self) -> None:
        live, self._current = self._current, None
        if live is not None:
            await self._retire(live)


async def require_image_adapter(models: ModelAccessLayer) -> ImageGeneratorHandle:
    """The configured image adapter, or 409 ``not_configured`` (``ImageNotConfigured``). Every
    generation entry point calls it before creating any job, reservation or cost row."""
    return await models.images.get()


get_adapter = require_image_adapter
"""The same lookup under the name the plan's key link uses (``registry.get_adapter()``)."""


# -- image checks ----------------------------------------------------------------------------


class TargetedPolicy(Protocol):
    """What the fail-closed check reads of a policy (``core.policies.model.Policy`` is one)."""

    @property
    def id(self) -> str: ...

    @property
    def applies_to(self) -> Collection[str]: ...


def image_output_policies(policies: Iterable[TargetedPolicy]) -> list[str]:
    """The ids of the policies that target ``image_output``."""
    return [policy.id for policy in policies if IMAGE_OUTPUT in policy.applies_to]


async def ensure_image_check_configured(
    models: ModelAccessLayer, policies: Iterable[TargetedPolicy]
) -> EffectiveConfig | None:
    """``image_check``'s configuration when some policy targets ``image_output``; ``None``
    when none does (no check is needed, and the image stays ``unchecked``). Raises
    ``ImageCheckUnconfigured`` naming every such policy while ``image_check`` has no model."""
    targeted = image_output_policies(policies)
    if not targeted:
        return None
    config = await models.resolve(IMAGE_CHECK.name)
    if config.model is None:
        raise ImageCheckUnconfigured(targeted)
    return config


__all__ = [
    "BUILTIN_IMAGE_ADAPTERS",
    "IMAGE_OUTPUT",
    "RULES",
    "ConfirmationUnsupported",
    "ImageAdapterConfig",
    "ImageAdapterFactory",
    "ImageAdapterInvalid",
    "ImageAdapters",
    "ImageCheckUnconfigured",
    "ImageGeneratorHandle",
    "ImageNotConfigured",
    "InvalidRule",
    "Missing",
    "TargetedPolicy",
    "check_declaration",
    "ensure_image_check_configured",
    "get_adapter",
    "image_output_policies",
    "missing_of",
    "register_builtin_image_adapters",
    "register_image_adapter",
    "registered_adapters",
    "require_image_adapter",
    "unregister_image_adapter",
]
