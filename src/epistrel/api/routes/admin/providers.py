"""External-data providers (FR-EXT-1,6; #204).

``/admin/external-providers`` (operator, deployment-scoped):

- ``POST`` registers a provider (201): a declarative manifest (``http``) or an in-process one;
  422 ``invalid_manifest`` at the manifest member a rule refuses, 409 ``provider_exists``.
- ``GET`` lists every provider with the deployment's ``config_revision``; ``GET /{name}`` one.
- ``PUT /{name}`` replaces a registration (a replacement that changes nothing writes nothing);
  it takes effect at the next operation that resolves the provider, without a restart.
- ``DELETE /{name}`` deletes one (204); 409 ``provider_in_use`` with the stories whose States
  name it.

Each write is an interpretive deployment configuration change
(``external_provider:<name>[.<field>]``). Manifests come back as registered: template text and
``{secret.<NAME>}`` references, never a secret.

``/stories/{id}/providers`` (the story's own, ``story_router``):

- ``PUT /{name}/binding`` binds the story to a provider with its ``external_ids``
  (idempotent); 422 ``binding_incomplete`` when they leave a ``{binding.<key>}`` of the
  manifest uncovered.
- ``GET /{name}/binding`` reads one binding (404 when the story has none to that provider);
  ``GET`` lists the story's bindings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response, status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse, MutationResponse
from epistrel.core.providers import (
    PROVIDER_NAME_PATTERN,
    BindingRequest,
    ProviderBinding,
    ProviderRow,
)
from epistrel.orchestrator.providers.manifest import ProviderRegistration, ProviderSpec

router = APIRouter(prefix="/external-providers")
story_router = APIRouter(tags=["settings"])

ProviderNamePath = Annotated[
    str,
    Path(
        pattern=PROVIDER_NAME_PATTERN,
        description="The provider's name (lowercase letters, digits, `_`, `.`, `-`).",
        examples=["game_ledger"],
    ),
]


class ProviderBody(BaseModel):
    """A registered provider, as registered (secret references only), with when it was
    created and last changed and the deployment ``config_revision`` of its last change."""

    name: str
    trust_tier: str
    transport: str
    manifest: dict[str, Any]
    write_capable: bool
    confirmation: str | None
    dedupes_dispatch: bool | None
    returns_resulting_values: bool | None
    unavailability: str
    freshness_minutes: int | None
    confirm_wait_max_seconds: int
    changed_at_revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def fields_of(cls, row: ProviderRow) -> dict[str, Any]:
        return {
            "name": row.name,
            "trust_tier": row.trust_tier,
            "transport": row.transport,
            "manifest": row.manifest,
            "write_capable": row.write_capable,
            "confirmation": row.confirmation,
            "dedupes_dispatch": row.dedupes_dispatch,
            "returns_resulting_values": row.returns_resulting_values,
            "unavailability": row.unavailability,
            "freshness_minutes": row.freshness_minutes,
            "confirm_wait_max_seconds": row.confirm_wait_max_seconds,
            "changed_at_revision": row.config_revision,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @classmethod
    def of(cls, row: ProviderRow) -> ProviderBody:
        return cls(**cls.fields_of(row))


class ProviderList(BaseModel):
    """Every registered provider, by name, and the deployment's ``config_revision``."""

    config_revision: int
    providers: list[ProviderBody]

    @classmethod
    def of(cls, revision: int, rows: list[ProviderRow]) -> ProviderList:
        return cls(config_revision=revision, providers=[ProviderBody.of(r) for r in rows])


class ProviderItem(ProviderBody):
    """One provider, with the ``config_revision`` it was read at."""

    config_revision: int


class ProviderMutation(ProviderBody, DeploymentMutationResponse):
    """A provider after a write, with the ``config_revision`` the write produced."""

    @classmethod
    def written(cls, row: ProviderRow, revision: int) -> ProviderMutation:
        return cls(config_revision=revision, **ProviderBody.fields_of(row))


class BindingBody(BaseModel):
    """A story's binding to one provider."""

    provider: str
    external_ids: dict[str, str]
    bound_at: datetime

    @classmethod
    def of(cls, binding: ProviderBinding) -> BindingBody:
        return cls(
            provider=binding.provider,
            external_ids=dict(binding.external_ids),
            bound_at=binding.bound_at,
        )


class BindingMutation(BindingBody, MutationResponse):
    """The binding as stored, with the story's (unchanged) ``timeline_revision`` and, when the
    binding made the story's ``required_providers`` live (#204), that list."""

    activated_required_providers: list[str] | None = None


class BindingList(BaseModel):
    """The story's bindings, by provider name."""

    bindings: list[BindingBody]


# -- /admin/external-providers ---------------------------------------------------------------


@router.get(
    "",
    response_model=ProviderList,
    operation_id="list_external_providers",
    summary="List the external-data providers",
)
async def list_providers(ctx: ContextDep, engine: EngineDep) -> ProviderList:
    revision, rows = await engine.list_external_providers(ctx)
    return ProviderList.of(revision, rows)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ProviderMutation,
    operation_id="register_external_provider",
    summary="Register an external-data provider",
)
async def register_provider(
    body: ProviderRegistration, response: Response, ctx: ContextDep, engine: EngineDep
) -> ProviderMutation:
    """Register a provider: its manifest is checked (placeholders, `$`/`.key`/`[n]` paths,
    https or loopback, the deployment's `provider_allowed_hosts`) and refused with 422
    `invalid_manifest` at the offending member."""
    write = await engine.register_external_provider(ctx, body)
    assert write.provider is not None  # noqa: S101 - a registration answers the provider
    answer = ProviderMutation.written(write.provider, write.config_revision)
    return answered(answer, ctx, response, replayed=write.replayed)


@router.get(
    "/{name}",
    response_model=ProviderItem,
    operation_id="get_external_provider",
    summary="Read an external-data provider",
)
async def get_provider(name: ProviderNamePath, ctx: ContextDep, engine: EngineDep) -> ProviderItem:
    revision, row = await engine.get_external_provider(ctx, name)
    return ProviderItem(config_revision=revision, **ProviderBody.fields_of(row))


@router.put(
    "/{name}",
    response_model=ProviderMutation,
    operation_id="replace_external_provider",
    summary="Replace an external-data provider's registration",
)
async def replace_provider(
    name: ProviderNamePath,
    body: ProviderSpec,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ProviderMutation:
    """Replace every field of the registration; it applies from the next operation that
    resolves the provider (a turn already under way keeps what it resolved)."""
    write = await engine.replace_external_provider(ctx, name, body)
    assert write.provider is not None  # noqa: S101 - a replacement answers the provider
    answer = ProviderMutation.written(write.provider, write.config_revision)
    return answered(answer, ctx, response, replayed=write.replayed)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_external_provider",
    summary="Delete an external-data provider",
)
async def delete_provider(name: ProviderNamePath, ctx: ContextDep, engine: EngineDep) -> Response:
    """Delete a provider no story's State names (else 409 `provider_in_use`, listing the
    stories)."""
    write = await engine.delete_external_provider(ctx, name)
    return no_content(ctx, replayed=write.replayed)


# -- /stories/{id}/providers ------------------------------------------------------------------


@story_router.put(
    "/stories/{story_id}/providers/{name}/binding",
    response_model=BindingMutation,
    operation_id="bind_provider",
    summary="Bind the story to a provider",
)
async def bind_provider(
    story_id: StoryId,
    name: ProviderNamePath,
    body: BindingRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> BindingMutation:
    """Store the story's ids at a provider (replacing any it had); each `{binding.<key>}` of
    the manifest needs its key (422 `binding_incomplete`). The timeline revision is unchanged."""
    write = await engine.bind_provider(ctx, story_id, name, body)
    answer = BindingMutation(
        timeline_revision=write.timeline_revision,
        activated_required_providers=write.activated,
        **BindingBody.of(write.binding).model_dump(),
    )
    return answered(answer, ctx, response, replayed=write.replayed)


@story_router.get(
    "/stories/{story_id}/providers/{name}/binding",
    response_model=BindingBody,
    operation_id="provider_binding",
    summary="Read the story's binding to a provider",
)
async def get_binding(
    story_id: StoryId, name: ProviderNamePath, ctx: ContextDep, engine: EngineDep
) -> BindingBody:
    """The story's ids at one provider (404 when it has no binding to it)."""
    return BindingBody.of(await engine.provider_binding(ctx, story_id, name))


@story_router.get(
    "/stories/{story_id}/providers",
    response_model=BindingList,
    operation_id="provider_bindings",
    summary="List the story's provider bindings",
)
async def list_bindings(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> BindingList:
    found = await engine.provider_bindings(ctx, story_id)
    return BindingList(bindings=[BindingBody.of(b) for b in found])


__all__ = ["router", "story_router"]
