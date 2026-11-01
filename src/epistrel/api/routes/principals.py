"""``GET /principals/{principal_id}``: everything linked to a principal (FR-AUTH-7, FR-API-6;
#117).

What the principal owns and what names it in a relation, by type, each a page with its count:
stories, catalog characters (each with its companion; companions are not listed as
scenarios), scenarios, personas, media assets and engine profiles. A principal is opaque and
never resolved, so one that nothing names answers empty pages (never 404). The path holds the
rest of the URL, so a principal may contain ``/``. ``limit`` applies to every page. Each page
continues with its own cursor: ``cursor[<type>]`` for an owned page, ``cursor[related.<type>]``
for a related one. Offboarding is the operator's (``/admin/offboarding``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.principals import (
    CURSOR_MAX,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    RELATED_PREFIX,
    PrincipalLookup,
)

PRINCIPALS_TAG = "principals"

router = APIRouter(prefix="/principals", tags=[PRINCIPALS_TAG])


def _owned(key: str) -> str:
    return f"The `next_cursor` of the previous page of owned {key.replace('_', ' ')}."


def _related(key: str) -> str:
    return f"The `next_cursor` of the previous page of related {key.replace('_', ' ')}."


@router.get(
    "/{principal_id:path}",
    response_model=PrincipalLookup,
    operation_id="lookup_principal",
    summary="Look up everything linked to a principal",
    description=(
        "Every entity linked to the principal (FR-AUTH-7), by type: `owned` (its "
        "`owner_principal`) and `related` (the entities with a relation naming it, with the "
        "relations' names). Stories, catalog characters, scenarios, personas, media assets and "
        "engine profiles each answer `{count, items, next_cursor}`, newest first. An owned item "
        "is `{id, name, status, version, listing_status}` (`null` where the type has none), and "
        "a character's names its companion scenario (`companion`), which follows it and is not "
        "listed under scenarios. A related item is `{id, relations, name, status, version, "
        "listing_status}`, with an owned item's display fields (a media candidate's `name` is "
        "`null`). Archived stories and entries, retired media and deleted "
        "profiles are listed with their status. A story held `blocked_by_erasure` after a "
        "restore is listed with a `null` `name`, owned or related, until its erasure is "
        "reapplied (FR-ADMIN-7). "
        "`limit` (1 to 200) applies to every page; "
        "each page continues with its own cursor, `cursor[<type>]` or "
        "`cursor[related.<type>]` (422 `cursor_invalid` for one another page gave). A "
        "principal nothing names answers empty pages: principals are opaque and never "
        "resolved. Ownership and relations gate nothing (FR-AUTH-8)."
    ),
)
async def lookup_principal(
    principal_id: Annotated[
        str,
        Path(
            pattern=PRINCIPAL_PATTERN,
            description="The principal (opaque): the rest of the path, so it may contain `/`.",
            examples=["p-7f3a9c"],
        ),
    ],
    ctx: ContextDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    stories: Annotated[
        str | None,
        Query(alias="cursor[stories]", max_length=CURSOR_MAX, description=_owned("stories")),
    ] = None,
    characters: Annotated[
        str | None,
        Query(alias="cursor[characters]", max_length=CURSOR_MAX, description=_owned("characters")),
    ] = None,
    scenarios: Annotated[
        str | None,
        Query(alias="cursor[scenarios]", max_length=CURSOR_MAX, description=_owned("scenarios")),
    ] = None,
    personas: Annotated[
        str | None,
        Query(alias="cursor[personas]", max_length=CURSOR_MAX, description=_owned("personas")),
    ] = None,
    media: Annotated[
        str | None, Query(alias="cursor[media]", max_length=CURSOR_MAX, description=_owned("media"))
    ] = None,
    engine_profiles: Annotated[
        str | None,
        Query(
            alias="cursor[engine_profiles]",
            max_length=CURSOR_MAX,
            description=_owned("engine_profiles"),
        ),
    ] = None,
    related_stories: Annotated[
        str | None,
        Query(
            alias="cursor[related.stories]", max_length=CURSOR_MAX, description=_related("stories")
        ),
    ] = None,
    related_characters: Annotated[
        str | None,
        Query(
            alias="cursor[related.characters]",
            max_length=CURSOR_MAX,
            description=_related("characters"),
        ),
    ] = None,
    related_scenarios: Annotated[
        str | None,
        Query(
            alias="cursor[related.scenarios]",
            max_length=CURSOR_MAX,
            description=_related("scenarios"),
        ),
    ] = None,
    related_personas: Annotated[
        str | None,
        Query(
            alias="cursor[related.personas]",
            max_length=CURSOR_MAX,
            description=_related("personas"),
        ),
    ] = None,
    related_media: Annotated[
        str | None,
        Query(alias="cursor[related.media]", max_length=CURSOR_MAX, description=_related("media")),
    ] = None,
    related_engine_profiles: Annotated[
        str | None,
        Query(
            alias="cursor[related.engine_profiles]",
            max_length=CURSOR_MAX,
            description=_related("engine_profiles"),
        ),
    ] = None,
) -> PrincipalLookup:
    owned = {
        "stories": stories,
        "characters": characters,
        "scenarios": scenarios,
        "personas": personas,
        "media": media,
        "engine_profiles": engine_profiles,
    }
    related = {
        "stories": related_stories,
        "characters": related_characters,
        "scenarios": related_scenarios,
        "personas": related_personas,
        "media": related_media,
        "engine_profiles": related_engine_profiles,
    }
    cursors = {key: value for key, value in owned.items() if value is not None}
    cursors.update(
        {f"{RELATED_PREFIX}{key}": value for key, value in related.items() if value is not None}
    )
    return await engine.lookup_principal(ctx, principal_id, limit=limit, cursors=cursors)


__all__ = ["PRINCIPALS_TAG", "router"]
