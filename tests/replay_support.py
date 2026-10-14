"""Replaying every mutating route under its ``Idempotency-Key`` (#128, FR-API-6, FR-CONC-6):
``test_c_15__replay``.

The test reruns C-03's scripted sequence (``tests/conformance/test_c_03._steps``), which already
reaches every Engine operation on both surfaces in an order that makes each one succeed. Every
step whose route is mutating (a ``POST``/``PUT``/``PATCH``/``DELETE`` not flagged
``mutating=False``, ``api.deps.route_flags``) is sent under its own key; each one that succeeds
is sent again under the same key at once. The replay must answer the stored result: the same
status and body, ``Idempotent-Replay: true`` over REST, the same rendered result in-process;
and it must add no row to any table of the database (every base table is counted, so the
operation and the entity's own table are among them).

The library form of a step reads C-03's module-level ``CTX``; ``keyed`` swaps in a context
carrying the step's key for the step's two calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import compile_path

from epistrel.api.deps import MUTATING_EXTRA
from epistrel.core.context import RequestContext
from epistrel.core.tables.ownership import EntityType
from tests.media_support import files, png

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

REPLACED_NOT_STORED = {
    "put_upload_part": (
        "a binary part upload (#107): sending a part again replaces it, so its key is echoed, "
        "not kept, and its replay is a re-execution with the same answer and no new row"
    ),
}
"""Mutating routes idempotent by replacement rather than by a stored result: their replay must
answer the same and write no row, but carries no ``Idempotent-Replay`` flag."""

TABLES = text(
    "SELECT table_schema, table_name FROM information_schema.tables "
    "WHERE table_type = 'BASE TABLE' "
    "AND table_schema NOT IN ('pg_catalog', 'information_schema') "
    "AND table_name <> 'alembic_version' ORDER BY 1, 2"
)


def mutating_routes(app: FastAPI) -> dict[str, APIRoute]:
    """``operation_id → route`` for every mutating route the app serves."""
    found: dict[str, APIRoute] = {}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not route.operation_id:
            continue
        if not (route.methods or set()) & MUTATING_METHODS:
            continue
        if (route.openapi_extra or {}).get(MUTATING_EXTRA, True) is False:
            continue
        found[route.operation_id] = route
    return found


def route_of(app: FastAPI, method: str, path: str) -> APIRoute | None:
    """The route ``method path`` reaches (``path`` without its query), matched on the served
    path (an included router's route knows only its own part of it)."""
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or method not in (context.methods or ()):
            continue
        regex, _, _ = compile_path(context.path or "")
        if regex.match(path):
            return route
    return None


def is_mutating(route: APIRoute | None) -> bool:
    if route is None or not (route.methods or set()) & MUTATING_METHODS:
        return False
    return (route.openapi_extra or {}).get(MUTATING_EXTRA, True) is not False


async def table_counts(db: AsyncEngine) -> dict[str, int]:
    """The row count of every base table, in one statement."""
    async with db.connect() as conn:
        names = [(r.table_schema, r.table_name) for r in await conn.execute(TABLES)]
        # The names come from the catalog query above, never from input.
        selects = ", ".join(
            f'(SELECT count(*) FROM "{schema}"."{name}") AS "{schema}.{name}"'  # noqa: S608
            for schema, name in names
        )
        row = (await conn.execute(text(f"SELECT {selects}"))).mappings().one()
    return {name: int(value) for name, value in row.items()}


def grown(before: dict[str, int], after: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Tables whose row count changed."""
    return {
        name: (before.get(name, 0), count)
        for name, count in after.items()
        if count != before.get(name, 0)
    }


def steps(c03: Any) -> list[Any]:
    """C-03's sequence, then the mutating routes it reaches through another route of the same
    operation only: a real erasure (C-03's ``erase`` is a dry run) of a new story's character,
    and new entities of every owned type with a relation added and removed (C-03 adds one to
    some types and removes one from stories only), and a scenario's listing status."""
    sequence = c03._steps()
    create = sequence[0]
    assert create.operation == "create_story"
    return [*sequence, create, _real_erase(c03), *_owned(c03)]


def _real_erase(c03: Any) -> Any:
    def real(ids: dict[str, Any]) -> dict[str, Any]:
        return {**c03._erasure_request(ids), "dry_run": False}

    return c03.Step(
        "erase",
        lambda ids: c03.Rest("POST", "/admin/erasure", real(ids)),
        lambda engine, ids: c03._erase(engine, real(ids)),
    )


REPLAY_PNG = png(nonce="c-15 replay")


def _owned(c03: Any) -> list[Any]:
    """A new entity of each catalog-side owned type, a relation added to it and removed."""
    persona = {**c03.PERSONA, "name": "Replay persona"}
    profile = {**c03.PROFILE, "name": "Replay profile"}
    scenario = c03.ScenarioCreate
    made = [
        c03.Step(
            "create_character_draft",
            lambda ids: c03.Rest("POST", "/catalog/characters", c03.CHARACTER),
            lambda e, ids: c03._draft(
                e.create_character_draft(c03.CTX, c03.CharacterCreate(**c03.CHARACTER))
            ),
            c03._keep("r_character", "id"),
        ),
        c03.Step(
            "create_scenario_draft",
            lambda ids: c03.Rest("POST", "/catalog/scenarios", c03._scenario_body(ids)),
            lambda e, ids: c03._scenario_draft(
                e.create_scenario_draft(c03.CTX, scenario.model_validate(c03._scenario_body(ids)))
            ),
            c03._keep("r_scenario", "id"),
        ),
        c03.Step(
            "create_persona",
            lambda ids: c03.Rest("POST", "/personas", persona),
            lambda e, ids: c03._persona_write(
                e.create_persona(c03.CTX, c03.PersonaCreate(**persona))
            ),
            c03._keep("r_persona", "id"),
        ),
        c03.Step(
            "upload_media",
            lambda ids: c03.Rest("POST", "/media", files=files(REPLAY_PNG, c03.MEDIA)),
            lambda e, ids: _upload(c03, e),
            c03._keep("r_media", "id"),
        ),
        c03.Step(
            "create_profile",
            lambda ids: c03.Rest("POST", "/engine-profiles", profile),
            lambda e, ids: c03._profile_write(
                e.create_profile(c03.CTX, c03.ProfileCreate(**profile))
            ),
            c03._keep("r_profile", "id"),
        ),
        c03.Step(
            "set_listing_status",
            lambda ids: c03.Rest(
                "PUT", "/catalog/scenarios/{r_scenario}/listing-status", c03.PUBLIC
            ),
            lambda e, ids: c03._listing_status(
                e.set_listing_status(
                    c03.CTX,
                    EntityType.SCENARIO,
                    ids["r_scenario"],
                    c03.ListingStatusRequest(**c03.PUBLIC),
                )
            ),
        ),
    ]
    relations: list[Any] = []
    for entity, base, name in (
        (EntityType.CHARACTER, "/catalog/characters", "r_character"),
        (EntityType.SCENARIO, "/catalog/scenarios", "r_scenario"),
        (EntityType.PERSONA, "/personas", "r_persona"),
        (EntityType.MEDIA, "/media", "r_media"),
        (EntityType.ENGINE_PROFILE, "/engine-profiles", "r_profile"),
    ):
        relations += _relation_steps(c03, entity, f"{base}/{{{name}}}", name)
    return [*made, *relations]


def _relation_steps(c03: Any, entity: EntityType, path: str, name: str) -> list[Any]:
    relation = c03.RELATION
    return [
        c03.Step(
            "add_relation",
            lambda ids: c03.Rest("POST", f"{path}/relations", relation),
            lambda e, ids: c03._relation(
                e.add_relation(c03.CTX, entity, ids[name], c03.RelationRequest(**relation))
            ),
        ),
        c03.Step(
            "remove_relation",
            lambda ids: c03.Rest(
                "DELETE", f"{path}/relations/{relation['relation']}/{relation['principal']}"
            ),
            lambda e, ids: c03._no_content(
                e.remove_relation(
                    c03.CTX, entity, ids[name], relation["relation"], relation["principal"]
                )
            ),
        ),
    ]


async def _upload(c03: Any, engine: Any) -> dict[str, Any]:
    request = c03.MediaUpload.model_validate(c03.MEDIA)
    written = await engine.upload_media(c03.CTX, request, REPLAY_PNG, content_type="image/png")
    dumped: dict[str, Any] = c03.dump(c03.MediaUploadedBody.of(written))
    return dumped


def dry_run(call: Any) -> bool:
    """A request that asks for a dry run: it writes nothing and stores no result (#117,
    #124), so its repeat is a re-execution that must answer the same and write nothing."""
    return isinstance(call.json, dict) and call.json.get("dry_run") is True


@dataclass
class Replays:
    """What one surface's run saw: the operations replayed, and every problem found."""

    replayed: set[str] = field(default_factory=set)
    """``operation_id``s of the mutating routes whose keyed call succeeded and was replayed."""
    problems: list[str] = field(default_factory=list)
    answers: list[tuple[str, int]] = field(default_factory=list)
    """Each step's operation and first answer's status (in-process: 200 for a result, the
    error's status for a refusal), in order."""


@contextmanager
def keyed(module: Any, principal: str, key: str) -> Iterator[None]:
    """C-03's library ``CTX`` with ``key``, for the block."""
    previous = module.CTX
    module.CTX = RequestContext(principal=principal, idempotency_key=key)
    try:
        yield
    finally:
        module.CTX = previous


async def replay_rest(c03: Any, settings: Any, db: AsyncEngine) -> Replays:
    """C-03's sequence over REST, each successful mutating step replayed under its key."""
    import httpx2 as httpx

    from tests.auth_support import bearer

    seen = Replays()
    ids: dict[str, Any] = {}
    base = bearer() | {"X-Epistrel-Principal": c03.PRINCIPAL}
    async with c03.surface_engine(settings) as (_, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for index, step in enumerate(steps(c03)):
                call = step.rest(ids)
                path = call.path.format(**ids)
                route = route_of(app, call.method, path)
                mutating = is_mutating(route)
                headers = dict(base)
                if mutating:
                    headers["Idempotency-Key"] = f"replay-rest-{index}"

                async def send(
                    call: Any = call, path: str = path, headers: dict[str, str] = headers
                ) -> Any:
                    return await client.request(
                        call.method,
                        path,
                        json=call.json,
                        params=call.params,
                        files=call.files,
                        content=call.content,
                        headers=headers,
                    )

                first = await send()
                body = _body(c03, first)
                seen.answers.append((step.operation, first.status_code))
                if not (mutating and first.is_success):
                    if first.is_success:
                        step.capture(body, ids)
                    continue
                assert route is not None
                label = f"step {index} {route.operation_id} ({call.method} {path})"
                before = await table_counts(db)
                again = await send()
                after = await table_counts(db)
                if (again.status_code, _body(c03, again)) != (first.status_code, body):
                    seen.problems.append(
                        f"{label}: replay answered {again.status_code} "
                        f"{_body(c03, again)!r}, not {first.status_code} {body!r}"
                    )
                stored = route.operation_id not in REPLACED_NOT_STORED and not dry_run(call)
                if stored and again.headers.get("Idempotent-Replay") != "true":
                    seen.problems.append(f"{label}: replay without Idempotent-Replay: true")
                if grown(before, after):
                    seen.problems.append(f"{label}: replay wrote rows {grown(before, after)}")
                if not dry_run(call):
                    seen.replayed.add(str(route.operation_id))
                step.capture(body, ids)
    return seen


def _body(c03: Any, response: Any) -> Any:
    kind = response.headers.get("content-type", "application/json")
    if response.content and not kind.startswith("application/json"):
        return c03._bytes(response.content, kind)
    return response.json() if response.content else {}


async def replay_library(c03: Any, settings: Any, db: AsyncEngine) -> Replays:
    """C-03's sequence in-process, each successful mutating step replayed under its key."""
    from epistrel.core.errors import EpistrelError

    seen = Replays()
    ids: dict[str, Any] = {}
    async with c03.surface_engine(settings) as (engine, app):
        for index, step in enumerate(steps(c03)):
            call = step.rest(ids)
            route = route_of(app, call.method, call.path.format(**ids))
            mutating = is_mutating(route)
            key = f"replay-lib-{index}"
            with keyed(c03, c03.PRINCIPAL, key) if mutating else _unkeyed():
                try:
                    body = await step.library(engine, ids)
                except EpistrelError as refused:
                    seen.answers.append((step.operation, refused.http_status))
                    continue
                seen.answers.append((step.operation, 200))
                if not mutating:
                    step.capture(body, ids)
                    continue
                assert route is not None
                label = f"step {index} {route.operation_id} ({step.operation})"
                before = await table_counts(db)
                try:
                    again = await step.library(engine, ids)
                except EpistrelError as error:
                    seen.problems.append(f"{label}: replay raised {error.code}")
                    continue
                after = await table_counts(db)
            if again != body:
                seen.problems.append(f"{label}: replay answered {again!r}, not {body!r}")
            if grown(before, after):
                seen.problems.append(f"{label}: replay wrote rows {grown(before, after)}")
            if not dry_run(call):
                seen.replayed.add(str(route.operation_id))
            step.capture(body, ids)
    return seen


@contextmanager
def _unkeyed() -> Iterator[None]:
    yield


__all__ = [
    "MUTATING_METHODS",
    "REPLACED_NOT_STORED",
    "Replays",
    "dry_run",
    "grown",
    "is_mutating",
    "keyed",
    "mutating_routes",
    "replay_library",
    "replay_rest",
    "route_of",
    "steps",
    "table_counts",
]
