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

The routes with a mutating method flagged ``mutating=False`` take no key (``UNKEYED``, each
with its reason): each is sent twice without one, and the repeat must answer the same and add
no row either.

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

UNKEYED_WRITES = {
    "rate_message": (
        "a message rating (#112, docs/02 §9): one rating per principal and message, so the same "
        "value again changes nothing; it records no operation and takes no key. The repeat "
        "answers 200 (the documented re-rate status) with the same rating"
    ),
    "retract_feedback": (
        "a rating's retraction (#112, docs/02 §9): 204 also when there is none, so a repeat "
        "answers the same and records no second retraction"
    ),
    "delete_feedback_export": (
        "an export file's deletion (#126): 204 also when the file is already gone; the export's "
        "record stays, so a repeat adds nothing"
    ),
    "reconcile_erasure": (
        "the erasure ledger's reconciliation (#125, FR-ADMIN-7): it reapplies only what the "
        "ledger already records and records no operation, so a repeat is harmless; each run has "
        "its own ``run_id``"
    ),
}
"""Routes with a mutating method flagged ``mutating=False`` that do write: idempotent by design,
so they take no ``Idempotency-Key`` (docs/02 §9 for the feedback writes). Each is sent twice
without a key; the repeat must answer the same (``REPEAT_STATUS``, ``REPEAT_IGNORED``) and add
no row."""

UNKEYED_READS = {
    "impact_preview": "a profile change's impact preview (#109): computes, writes nothing",
    "validate_scenario": "a scenario validation (FR-SCN-5): a dry run of a publish",
    "validate_profile": "an engine profile validation (FR-PROF-2): a dry run of an edit",
    "suggest_ratings_character": "an advisory rating suggestion (#111): writes nothing",
    "suggest_ratings_scenario": "an advisory rating suggestion (#111): writes nothing",
}
"""Routes with a mutating method flagged ``mutating=False`` that write nothing. They are sent twice
without a key as well: the repeat must answer the same and add no row."""

UNKEYED = {**UNKEYED_WRITES, **UNKEYED_READS}

REPEAT_STATUS = {"rate_message": {201: 200}}
"""The REST status a repeat answers where it differs from the first call's: a re-rate is 200."""

TIMINGS = frozenset({"elapsed_ms", "latency_ms"})

REPEAT_IGNORED = {
    "reconcile_erasure": frozenset({"run_id"}),
    "suggest_ratings_character": TIMINGS,
    "suggest_ratings_scenario": TIMINGS,
}
"""Body fields, at any depth, a repeat answers anew: a reconcile run's id is its own, and a
suggestion's model calls are timed again (their request ids and outcomes must match)."""

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


def unkeyed_routes(app: FastAPI) -> dict[str, APIRoute]:
    """``operation_id → route`` for every route with a mutating method flagged
    ``mutating=False``: no ``Idempotency-Key`` is taken."""
    found: dict[str, APIRoute] = {}
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not route.operation_id:
            continue
        if (route.methods or set()) & MUTATING_METHODS and not is_mutating(route):
            found[route.operation_id] = route
    return found


def unkeyed(route: APIRoute | None) -> str | None:
    """The route's ``operation_id`` when it is one of ``UNKEYED``, else ``None``."""
    if route is None or route.operation_id not in UNKEYED or is_mutating(route):
        return None
    return str(route.operation_id)


def same_answer(operation: str, body: Any) -> Any:
    """``body`` without the fields, at any depth, a repeat of ``operation`` answers anew."""
    return _without(body, REPEAT_IGNORED.get(operation, frozenset()))


def _without(value: Any, ignored: frozenset[str]) -> Any:
    if not ignored:
        return value
    if isinstance(value, dict):
        return {k: _without(v, ignored) for k, v in value.items() if k not in ignored}
    if isinstance(value, list):
        return [_without(item, ignored) for item in value]
    return value


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
    repeated: set[str] = field(default_factory=set)
    """``operation_id``s of the ``UNKEYED`` routes whose call succeeded and was repeated."""
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
    async with c03.surface_engine(settings) as (lib, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for index, step in enumerate(steps(c03)):
                if step.prepare is not None:
                    await step.prepare(lib, ids)
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
                operation = unkeyed(route)
                if operation is not None and first.is_success:
                    label = f"step {index} {operation} ({call.method} {path}), unkeyed"
                    before = await table_counts(db)
                    again = await send()
                    after = await table_counts(db)
                    status = REPEAT_STATUS.get(operation, {}).get(
                        first.status_code, first.status_code
                    )
                    answer = (again.status_code, same_answer(operation, _body(c03, again)))
                    if answer != (status, same_answer(operation, body)):
                        seen.problems.append(
                            f"{label}: repeat answered {answer!r}, not {status} {body!r}"
                        )
                    if grown(before, after):
                        seen.problems.append(f"{label}: repeat wrote rows {grown(before, after)}")
                    seen.repeated.add(operation)
                    step.capture(body, ids)
                    continue
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
            if step.prepare is not None:
                await step.prepare(engine, ids)
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
                operation = unkeyed(route)
                if operation is not None:
                    label = f"step {index} {operation} ({step.operation}), unkeyed"
                    before = await table_counts(db)
                    try:
                        repeat = await step.library(engine, ids)
                    except EpistrelError as error:
                        seen.problems.append(f"{label}: repeat raised {error.code}")
                        continue
                    after = await table_counts(db)
                    if same_answer(operation, repeat) != same_answer(operation, body):
                        seen.problems.append(f"{label}: repeat answered {repeat!r}, not {body!r}")
                    if grown(before, after):
                        seen.problems.append(f"{label}: repeat wrote rows {grown(before, after)}")
                    seen.repeated.add(operation)
                    step.capture(body, ids)
                    continue
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
    "REPEAT_IGNORED",
    "REPEAT_STATUS",
    "REPLACED_NOT_STORED",
    "UNKEYED",
    "UNKEYED_READS",
    "UNKEYED_WRITES",
    "Replays",
    "dry_run",
    "grown",
    "is_mutating",
    "keyed",
    "mutating_routes",
    "replay_library",
    "replay_rest",
    "route_of",
    "same_answer",
    "steps",
    "table_counts",
    "unkeyed",
    "unkeyed_routes",
]
