"""``/personas``: the player personas principals save for reuse (FR-CAT-6, FR-API-6,
FR-AUTH-5; #114).

- ``POST /personas``: a persona ``{name, gender?, age, description}``, owned by the requester or
  the named ``owner``; 201. 409 ``persona_name_taken`` when the owner has one of that name.
- ``GET /personas``: personas, newest first, a page at a time; ``owner`` and ``relation`` narrow
  the list only when asked (FR-AUTH-8).
- ``GET /personas/{id}``: one persona.
- ``PATCH /personas/{id}``: a JSON merge patch, last write wins.
- ``DELETE /personas/{id}``: a hard delete (204); stories created with the persona keep their
  snapshot.
- ``PUT /personas/{id}/owner``, ``POST``/``GET …/relations`` and ``DELETE
  …/relations/{relation}/{principal}``: the persona's owner and relations (``routes.ownership``,
  #104). Nothing gates on them (FR-AUTH-8).

Every handler makes one ``Engine`` call. Writes are deployment-scoped: idempotent under
``Idempotency-Key``, answering the deployment's ``config_revision`` (which they leave as it is).
A persona is not a catalog entry: it has no draft, versions or listing status, and no catalog
listing shows it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, WithJsonSchema

from epistrel.api.deps import ContextDep, EngineDep, PersonaId
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.ownership import mount_ownership_routes
from epistrel.api.routes.stories import (
    OWNER_FILTER_SCHEMA,
    RELATION_FILTERS_SCHEMA,
    RelationFilterParam,
)
from epistrel.core.catalog.personas import (
    CURSOR_MAX,
    DEFAULT_PAGE,
    MAX_PAGE,
    Persona,
    PersonaCreate,
    PersonaPage,
    PersonaPatch,
    PersonaWrite,
)
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.tables.ownership import EntityType

PERSONAS_TAG = "personas"

router = APIRouter(prefix="/personas", tags=[PERSONAS_TAG])


class PersonaBody(Persona, DeploymentMutationResponse):
    """The persona after a create or an edit, with the deployment's ``config_revision``
    (unchanged)."""

    model_config = ConfigDict(frozen=False)

    @classmethod
    def of(cls, write: PersonaWrite) -> PersonaBody:
        return cls(**write.persona.model_dump(), config_revision=write.config_revision)


class PersonaListBody(BaseModel):
    """A page of personas, newest first; ``next_cursor`` fetches the next page (``null`` on the
    last)."""

    items: list[Persona]
    next_cursor: str | None

    @classmethod
    def of(cls, page: PersonaPage) -> PersonaListBody:
        return cls(items=page.items, next_cursor=page.next_cursor)


_SNAPSHOT = (
    "A story created with a persona keeps a snapshot of it, so editing or deleting the persona "
    "never changes a story (FR-CAT-6)."
)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=PersonaBody,
    operation_id="create_persona",
    summary="Save a persona",
    description=(
        "Save a player persona (FR-CAT-6): its `name` (1 to 120 characters), an optional "
        "`gender` (free text, up to 40), its `age` (0 to 150: the persona's true age) and a "
        "short `description` (personality, and optionally appearance; up to 4,000), owned by "
        "`owner` (default: the requesting principal; never `system`). A name is unique per "
        "owner, ignoring case: a second one is 409 `persona_name_taken`. A persona is not a "
        f"catalog entry. {_SNAPSHOT}"
    ),
)
async def create_persona(
    body: PersonaCreate, response: Response, ctx: ContextDep, engine: EngineDep
) -> PersonaBody:
    write = await engine.create_persona(ctx, body)
    return answered(PersonaBody.of(write), ctx, response, replayed=write.replayed)


@router.get(
    "",
    response_model=PersonaListBody,
    operation_id="list_personas",
    summary="List personas",
    description=(
        "Personas, newest first, `limit` at a time; `cursor` (a page's `next_cursor`) continues "
        "with the same filters (422 `cursor_invalid` otherwise). Every principal's personas are "
        "listed unless the request filters: by `owner` (only that principal's personas), or by "
        "`relation` (`name:principal`, or `name:` for any principal; repeatable, all must hold). "
        "Owners and relations narrow a list only when a request asks: they gate nothing "
        "(FR-AUTH-8)."
    ),
)
async def list_personas(
    ctx: ContextDep,
    engine: EngineDep,
    owner: Annotated[
        str | None,
        Query(pattern=PRINCIPAL_PATTERN, description="Only the personas this principal owns."),
        WithJsonSchema(OWNER_FILTER_SCHEMA),
    ] = None,
    relation: Annotated[
        list[RelationFilterParam] | None,
        Query(
            description="Only the personas with this relation: `name:principal`, or `name:` "
            "for any principal (repeatable; all must hold)."
        ),
        WithJsonSchema(RELATION_FILTERS_SCHEMA),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    cursor: Annotated[
        str | None,
        Query(max_length=CURSOR_MAX, description="The previous page's `next_cursor`."),
    ] = None,
) -> PersonaListBody:
    page = await engine.list_personas(
        ctx, owner=owner, relation=relation or (), limit=limit, cursor=cursor
    )
    return PersonaListBody.of(page)


@router.get(
    "/{persona_id}",
    response_model=Persona,
    operation_id="get_persona",
    summary="Read a persona",
    description="The persona: its owner, its fields and when it was created and last changed.",
)
async def get_persona(persona_id: PersonaId, ctx: ContextDep, engine: EngineDep) -> Persona:
    return await engine.get_persona(ctx, persona_id)


@router.patch(
    "/{persona_id}",
    response_model=PersonaBody,
    operation_id="update_persona",
    summary="Edit a persona",
    description=(
        "Edit the persona with a JSON merge patch (RFC 7396), last write wins: a key sets that "
        "field and `null` removes it. Only `gender` may be removed; `null` for `name`, `age` or "
        "`description` is 422. A new name its owner already uses (ignoring case) is 409 "
        "`persona_name_taken`. An edit that changes nothing records nothing. The owner changes "
        f"only by a transfer (`PUT /personas/{{id}}/owner`). {_SNAPSHOT}"
    ),
)
async def update_persona(
    persona_id: PersonaId,
    body: PersonaPatch,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> PersonaBody:
    write = await engine.update_persona(ctx, persona_id, body)
    return answered(PersonaBody.of(write), ctx, response, replayed=write.replayed)


@router.delete(
    "/{persona_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_persona",
    summary="Delete a persona",
    description=(
        "Delete the persona and its relations (204), recorded with a `persona_deleted` audit "
        f"row. {_SNAPSHOT} A retry under the same `Idempotency-Key` is a 204 with "
        "`Idempotent-Replay: true`."
    ),
)
async def delete_persona(persona_id: PersonaId, ctx: ContextDep, engine: EngineDep) -> Response:
    deleted = await engine.delete_persona(ctx, persona_id)
    return no_content(ctx, replayed=deleted.replayed)


mount_ownership_routes(router, EntityType.PERSONA)
