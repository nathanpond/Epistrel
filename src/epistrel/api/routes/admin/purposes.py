"""Consumer purposes (FR-MODEL-14; #824).

``/admin/purposes`` (operator, deployment-scoped): model purposes a consumer registers, each a
named prompt template and output schema, stored and versioned (ADR-185).

- ``POST`` registers one at version 1 (201). Refused with 422 ``validation_error`` and
  ``details: {reason, field, message}``: ``name_invalid``, ``name_taken`` (a built-in purpose's
  name or one already registered, archived ones included), ``template_invalid``,
  ``schema_invalid`` or ``purpose_limit`` (more than 100 live purposes), checked in that order.
- ``GET`` lists every purpose by name, archived ones included, a page at a time; ``GET
  /{name}`` reads the latest version (``?version=n``: that one, 404 ``version_not_found`` when
  it has none); ``GET /{name}/versions`` lists the versions.
- ``PUT /{name}`` writes version n+1, replacing every per-version field; ``expected_version``
  must be the latest (409 ``version_stale``). An unknown name is 404, an archived purpose 410
  ``purpose_archived``. A replacement identical to the latest version writes nothing.
- ``POST /{name}/archive`` archives it: its calls are refused with 410 ``purpose_archived``,
  its versions stay readable, its name stays taken and its model configuration is hidden. A
  repeat archive is 200 and changes nothing; there is no unarchive.

Each purpose is routed like a built-in one: ``/admin/model-config`` lists every live purpose,
and with no override of its own it uses ``generation``'s model. Every write is a prospective
deployment configuration change (``consumer_purpose:<name>``) and honours ``Idempotency-Key``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.purposes.store import (
    DEFAULT_PAGE,
    MAX_PAGE,
    VERSION_MAX,
    ConsumerPurpose,
    PurposeCreate,
    PurposeReplace,
    PurposeSummary,
    PurposeVersionInfo,
    PurposeWrite,
)
from epistrel.core.purposes.validation import NAME_PATTERN

router = APIRouter(prefix="/purposes")

PurposeNamePath = Annotated[
    str,
    Path(
        max_length=64,
        description="The consumer purpose's name.",
        examples=["vote_reason"],
    ),
]
VersionQuery = Annotated[
    int | None,
    Query(ge=1, le=VERSION_MAX, description="A version's number (default: the latest)."),
]


class PurposeBody(BaseModel):
    """A consumer purpose at one version: its templates, output schema and description, which
    version it is and the latest one, and when it was registered, written and archived."""

    name: str
    version: int
    latest_version: int
    description: str
    template: str
    system_template: str | None
    output_schema: dict[str, Any]
    created_at: datetime
    version_created_at: datetime
    version_created_by: str
    archived_at: datetime | None

    @classmethod
    def fields_of(cls, purpose: ConsumerPurpose) -> dict[str, Any]:
        return purpose.model_dump()

    @classmethod
    def of(cls, purpose: ConsumerPurpose) -> PurposeBody:
        return cls(**cls.fields_of(purpose))


class PurposeMutation(PurposeBody, DeploymentMutationResponse):
    """The purpose at its latest version after a write, with the ``config_revision`` the write
    produced (unchanged when it changed nothing) and whether it ``changed`` anything."""

    changed: bool

    @classmethod
    def written(cls, write: PurposeWrite) -> PurposeMutation:
        return cls(
            config_revision=write.config_revision,
            changed=write.changed,
            **PurposeBody.fields_of(write.purpose),
        )


class PurposeListBody(BaseModel):
    """One page of consumer purposes by name (archived ones included, with ``archived_at``),
    the deployment's ``config_revision`` and the cursor of the next page."""

    config_revision: int
    items: list[PurposeSummary]
    next_cursor: str | None


class PurposeVersionsBody(BaseModel):
    """Every version of a consumer purpose, oldest first."""

    items: list[PurposeVersionInfo]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PurposeMutation,
    operation_id="register_purpose",
    summary="Register a consumer purpose",
    description=(
        "Register a model purpose of the consumer's own (FR-MODEL-14) at version 1: a name "
        f"(`{NAME_PATTERN}`, never a built-in purpose's), a Jinja2 `template` of the user "
        "message, an optional `system_template`, and the `output_schema` the reply must match. "
        "Everything is checked now, never at call time: 422 `validation_error` with "
        "`details.reason` `name_invalid`, `name_taken`, `template_invalid`, `schema_invalid` "
        "or `purpose_limit`, the first fault only. It has no quality floor and no "
        "certification: its output quality is the consumer's concern (ADR-185)."
    ),
)
async def register_purpose(
    body: PurposeCreate, response: Response, ctx: ContextDep, engine: EngineDep
) -> PurposeMutation:
    write = await engine.register_purpose(ctx, body)
    return answered(PurposeMutation.written(write), ctx, response, replayed=write.replayed)


@router.get(
    "",
    response_model=PurposeListBody,
    operation_id="list_purposes",
    summary="List the consumer purposes",
)
async def list_purposes(
    ctx: ContextDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    cursor: Annotated[
        str | None, Query(max_length=200, description="The previous page's `next_cursor`.")
    ] = None,
) -> PurposeListBody:
    revision, page = await engine.list_purposes(ctx, limit=limit, cursor=cursor)
    return PurposeListBody(config_revision=revision, items=page.items, next_cursor=page.next_cursor)


@router.get(
    "/{name}",
    response_model=PurposeBody,
    operation_id="get_purpose",
    summary="Read a consumer purpose",
    description=(
        "The purpose at its latest version; `version` reads another (404 "
        "`version_not_found`). An archived purpose still reads, with its `archived_at`."
    ),
)
async def get_purpose(
    name: PurposeNamePath, ctx: ContextDep, engine: EngineDep, version: VersionQuery = None
) -> PurposeBody:
    return PurposeBody.of(await engine.get_purpose(ctx, name, version=version))


@router.get(
    "/{name}/versions",
    response_model=PurposeVersionsBody,
    operation_id="list_purpose_versions",
    summary="List a consumer purpose's versions",
)
async def list_purpose_versions(
    name: PurposeNamePath, ctx: ContextDep, engine: EngineDep
) -> PurposeVersionsBody:
    return PurposeVersionsBody(items=await engine.list_purpose_versions(ctx, name))


@router.put(
    "/{name}",
    response_model=PurposeMutation,
    operation_id="replace_purpose",
    summary="Replace a consumer purpose (a new version)",
    description=(
        "Write version n+1, replacing the description, templates and output schema; "
        "`expected_version` must be the latest (409 `version_stale`). An archived purpose is "
        "410 `purpose_archived`. A replacement identical to the latest version writes nothing "
        "(`changed: false`). Calls that do not pin a version use the new one from their next "
        "call."
    ),
)
async def replace_purpose(
    name: PurposeNamePath,
    body: PurposeReplace,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> PurposeMutation:
    write = await engine.replace_purpose(ctx, name, body)
    return answered(PurposeMutation.written(write), ctx, response, replayed=write.replayed)


@router.post(
    "/{name}/archive",
    response_model=PurposeMutation,
    operation_id="archive_purpose",
    summary="Archive a consumer purpose",
    description=(
        "Archive the purpose: its calls are refused with 410 `purpose_archived`, its versions "
        "stay readable, its name stays taken and its model configuration is hidden. Archiving "
        "it again changes nothing. There is no unarchive."
    ),
)
async def archive_purpose(
    name: PurposeNamePath, response: Response, ctx: ContextDep, engine: EngineDep
) -> PurposeMutation:
    write = await engine.archive_purpose(ctx, name)
    return answered(PurposeMutation.written(write), ctx, response, replayed=write.replayed)


__all__ = ["router"]
