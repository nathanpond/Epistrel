"""Owner and relation routes, mounted per owned type (FR-AUTH-5,6,8, FR-API-6; #104).

``mount_ownership_routes(router, entity_type)`` adds four routes to the router that serves the
type (whose prefix is the type's segment, e.g. ``/stories`` or ``/catalog/characters``):

- ``PUT /{id}/owner`` ``{principal}`` transfers ownership: 200 ``{entity_type, entity_id,
  owner_principal, previous_owner}``. A transfer to the current owner is a no-op and records
  nothing. A character's companion scenario moves with it.
- ``POST /{id}/relations`` ``{principal, relation}`` adds a relation: 201 with the relation, or
  200 when it already exists.
- ``DELETE /{id}/relations/{relation}/{principal}`` removes one: 204, or 404 when there is none.
  The principal is the rest of the path, so it may contain ``/``.
- ``GET /{id}/relations`` returns the entity's owner and its relations.

Operation ids are ``<Engine method>_<entity type>`` (``transfer_owner_story``), and every route
is tagged ``ownership``. A story's writes answer its ``timeline_revision``; every other type's
answer the deployment's ``config_revision``. Neither moves. Ownership and relations exist for
listing, attribution and offboarding only: nothing in Epistrel grants or refuses an action
because of them (FR-AUTH-8).

The handlers are built per type. Each handler's signature names the type's own path parameter
(``story_id``, ``character_id``), so the routes read like the type's other routes, and C-01 and
C-03 resolve them the same way.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse, MutationResponse
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.ownership import (
    REGISTRY,
    OwnerTransfer,
    OwnerTransferRequest,
    Relation,
    RelationRequest,
    Relations,
    RelationWrite,
)
from epistrel.core.tables.ownership import RELATION_NAME_PATTERN, EntityType

OWNERSHIP_TAG = "ownership"
OWNERSHIP_METHODS = ("transfer_owner", "add_relation", "remove_relation", "list_relations")
"""The Engine methods every owned type's routes serve, as ``<method>_<type>``."""

CONFERS_NOTHING = (
    "Ownership and relations are labels the consumer defines: they exist for listing, "
    "attribution and offboarding only, and Epistrel never grants or withholds an action because "
    "of them (FR-AUTH-8)."
)
"""The sentence every ownership route's description ends with."""


def ownership_operation_ids(entity_type: EntityType) -> tuple[str, ...]:
    """The operation ids ``mount_ownership_routes`` gives ``entity_type``'s routes."""
    return tuple(f"{method}_{entity_type.value}" for method in OWNERSHIP_METHODS)


# -- bodies -------------------------------------------------------------------------------------


class _OwnerFields(BaseModel):
    entity_type: EntityType
    entity_id: str
    owner_principal: str
    previous_owner: str

    @staticmethod
    def fields_of(transfer: OwnerTransfer) -> dict[str, Any]:
        return {
            "entity_type": transfer.entity_type,
            "entity_id": transfer.entity_id,
            "owner_principal": transfer.owner_principal,
            "previous_owner": transfer.previous_owner,
        }


class StoryOwnerBody(_OwnerFields, MutationResponse):
    """A story's owner after a transfer, and its owner before. The two are equal when the request
    named the current owner: nothing was recorded then. ``timeline_revision`` is the story's,
    unchanged, because ownership is not story truth."""

    @classmethod
    def of(cls, transfer: OwnerTransfer) -> StoryOwnerBody:
        assert transfer.timeline_revision is not None  # noqa: S101 - a story's transfer
        return cls(**cls.fields_of(transfer), timeline_revision=transfer.timeline_revision)


class OwnerBody(_OwnerFields, DeploymentMutationResponse):
    """An entity's owner after a transfer, and its owner before. The two are equal when the
    request named the current owner: nothing was recorded then. ``config_revision`` is the
    deployment's, unchanged."""

    @classmethod
    def of(cls, transfer: OwnerTransfer) -> OwnerBody:
        assert transfer.config_revision is not None  # noqa: S101 - not a story's transfer
        return cls(**cls.fields_of(transfer), config_revision=transfer.config_revision)


class _RelationColumns(BaseModel):
    principal: str
    relation: str
    created_at: datetime
    created_by: str

    @staticmethod
    def columns_of(relation: Relation) -> dict[str, Any]:
        return {
            "principal": relation.principal,
            "relation": relation.relation,
            "created_at": relation.created_at,
            "created_by": relation.created_by,
        }


class RelationItem(_RelationColumns):
    """One relation: the related principal, the relation's name (consumer-defined, never
    interpreted), and who added it and when."""

    @classmethod
    def of(cls, relation: Relation) -> RelationItem:
        return cls(**cls.columns_of(relation))


class _RelationFields(_RelationColumns):
    entity_type: EntityType
    entity_id: str

    @staticmethod
    def fields_of(write: RelationWrite) -> dict[str, Any]:
        return {
            "entity_type": write.entity_type,
            "entity_id": write.entity_id,
            **_RelationColumns.columns_of(write.relation),
        }


class StoryRelationBody(_RelationFields, MutationResponse):
    """The story's relation: added (201), or already there (200, nothing recorded). The
    ``timeline_revision`` is the story's, unchanged."""

    @classmethod
    def of(cls, write: RelationWrite) -> StoryRelationBody:
        assert write.timeline_revision is not None  # noqa: S101 - a story's relation
        return cls(**cls.fields_of(write), timeline_revision=write.timeline_revision)


class RelationBody(_RelationFields, DeploymentMutationResponse):
    """The entity's relation: added (201), or already there (200, nothing recorded). The
    ``config_revision`` is the deployment's, unchanged."""

    @classmethod
    def of(cls, write: RelationWrite) -> RelationBody:
        assert write.config_revision is not None  # noqa: S101 - not a story's relation
        return cls(**cls.fields_of(write), config_revision=write.config_revision)


class RelationsBody(BaseModel):
    """An entity's owner and its relations, ordered by relation name, then principal."""

    entity_type: EntityType
    entity_id: str
    owner_principal: str
    items: list[RelationItem]

    @classmethod
    def of(cls, found: Relations) -> RelationsBody:
        return cls(
            entity_type=found.entity_type,
            entity_id=found.entity_id,
            owner_principal=found.owner_principal,
            items=[RelationItem.of(item) for item in found.items],
        )


# -- the routes ---------------------------------------------------------------------------------

RelationPath = Annotated[
    str,
    Path(
        pattern=RELATION_NAME_PATTERN,
        description="The relation's name.",
        examples=["editor"],
    ),
]
PrincipalPath = Annotated[
    str,
    Path(
        pattern=PRINCIPAL_PATTERN,
        description="The related principal: the rest of the path, so it may contain `/`.",
        examples=["p-7f3a9c"],
    ),
]


def _keyword(name: str, annotation: Any) -> inspect.Parameter:
    return inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)


def _bind(handler: Any, *parameters: inspect.Parameter) -> Any:
    """``handler`` (which takes ``**values``) with the signature FastAPI reads its parameters
    from: ``parameters``, then the request context and the Engine."""
    signature = inspect.Signature(
        [*parameters, _keyword("ctx", ContextDep), _keyword("engine", EngineDep)]
    )
    setattr(handler, "__signature__", signature)  # noqa: B010 - FastAPI reads it
    return handler


def mount_ownership_routes(router: APIRouter, entity_type: EntityType) -> None:
    """Add ``entity_type``'s owner and relation routes to ``router`` (see the module
    docstring). Each owned type's route module calls this once it serves the type."""
    owned = REGISTRY[entity_type]
    story = entity_type is EntityType.STORY
    owner_body: type[StoryOwnerBody | OwnerBody] = StoryOwnerBody if story else OwnerBody
    relation_body: type[StoryRelationBody | RelationBody] = (
        StoryRelationBody if story else RelationBody
    )
    id_param = owned.id_param
    item = f"/{{{id_param}}}"
    entity = _keyword(
        id_param,
        Annotated[
            str,
            Path(
                pattern=ULID_PATTERN,
                description=f"The {owned.noun}'s id (a ULID).",
                examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"],
            ),
        ],
    )
    transfer_id, add_id, remove_id, list_id = ownership_operation_ids(entity_type)
    a = f"a {owned.noun}" if owned.noun[0] not in "aeiou" else f"an {owned.noun}"
    revision = "its `timeline_revision`" if story else "the deployment's `config_revision`"

    async def transfer_owner(**values: Any) -> Any:
        ctx = values["ctx"]
        transfer = await values["engine"].transfer_owner(
            ctx, entity_type, values[id_param], values["body"]
        )
        body = owner_body.of(transfer)
        return answered(body, ctx, values["response"], replayed=transfer.replayed)

    router.add_api_route(
        f"{item}/owner",
        _bind(
            transfer_owner,
            entity,
            _keyword("body", OwnerTransferRequest),
            _keyword("response", Response),
        ),
        methods=["PUT"],
        response_model=owner_body,
        operation_id=transfer_id,
        summary=f"Transfer {a}'s ownership",
        description=(
            f"Make `principal` the owner of {a} (FR-AUTH-5): it answers the owner now and the "
            "one before, with "
            f"{revision} (unchanged). The transfer is recorded with the acting principal and "
            "time. "
            + ("A character's companion scenario moves with it. " if owned.companions else "")
            + (
                "A companion scenario follows its character: 422 `companion_follows_character`. "
                if owned.follows is not None
                else ""
            )
            + "A transfer to the current owner changes nothing and records nothing. `system` "
            "is refused (422). Allowed whatever the entity's status. " + CONFERS_NOTHING
        ),
    )

    async def add_relation(**values: Any) -> Any:
        ctx, response = values["ctx"], values["response"]
        write = await values["engine"].add_relation(
            ctx, entity_type, values[id_param], values["body"]
        )
        if not write.created:
            response.status_code = http_status.HTTP_200_OK
        return answered(relation_body.of(write), ctx, response, replayed=write.replayed)

    router.add_api_route(
        f"{item}/relations",
        _bind(
            add_relation,
            entity,
            _keyword("body", RelationRequest),
            _keyword("response", Response),
        ),
        methods=["POST"],
        status_code=http_status.HTTP_201_CREATED,
        response_model=relation_body,
        responses={
            http_status.HTTP_200_OK: {
                "model": relation_body,
                "description": "The relation already existed; nothing was recorded.",
            }
        },
        operation_id=add_id,
        summary=f"Relate a principal to {a}",
        description=(
            f"Add the relation `relation` (consumer-defined, e.g. `editor`, `co_author`, "
            f"`player`: 1 to 64 of `A-Z a-z 0-9 _ -`, case-sensitive) between `principal` and "
            f"{a} (FR-AUTH-6): 201 with the relation, recorded with the acting principal; 200 "
            "when it already exists (nothing recorded). Relations are list filters "
            "(`?relation=name:principal`), and Epistrel never interprets a name. "
            + (
                "A companion scenario's relations follow its character: 422 "
                "`companion_follows_character`. "
                if owned.follows is not None
                else ""
            )
            + CONFERS_NOTHING
        ),
    )

    async def remove_relation(**values: Any) -> Response:
        ctx = values["ctx"]
        removed = await values["engine"].remove_relation(
            ctx, entity_type, values[id_param], values["relation"], values["principal"]
        )
        return no_content(ctx, replayed=removed.replayed)

    router.add_api_route(
        f"{item}/relations/{{relation}}/{{principal:path}}",
        _bind(
            remove_relation,
            entity,
            _keyword("relation", RelationPath),
            _keyword("principal", PrincipalPath),
        ),
        methods=["DELETE"],
        status_code=http_status.HTTP_204_NO_CONTENT,
        response_class=Response,
        operation_id=remove_id,
        summary=f"Remove a relation from {a}",
        description=(
            f"Remove {a}'s relation `relation` to `principal` (204), recorded with the acting "
            "principal; 404 `not_found` when there is no such relation. The principal is the "
            "rest of the path and may contain `/`. A retry under the same `Idempotency-Key` is "
            "a 204 with `Idempotent-Replay: true`. " + CONFERS_NOTHING
        ),
    )

    async def list_relations(**values: Any) -> Any:
        found = await values["engine"].list_relations(values["ctx"], entity_type, values[id_param])
        return RelationsBody.of(found)

    router.add_api_route(
        f"{item}/relations",
        _bind(list_relations, entity),
        methods=["GET"],
        response_model=RelationsBody,
        operation_id=list_id,
        summary=f"List {a}'s owner and relations",
        description=(
            f"{a[0].upper()}{a[1:]}'s owner and its relations, by relation name, then principal. "
            + CONFERS_NOTHING
        ),
    )


__all__ = [
    "CONFERS_NOTHING",
    "OWNERSHIP_METHODS",
    "OWNERSHIP_TAG",
    "OwnerBody",
    "RelationBody",
    "RelationItem",
    "RelationsBody",
    "StoryOwnerBody",
    "StoryRelationBody",
    "mount_ownership_routes",
    "ownership_operation_ids",
]
