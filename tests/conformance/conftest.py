"""The dual-surface fixture (NFR-SEC-5's parity half, #59): one interface over REST and the
in-process library, so a conformance test runs the same requests through both.

Each call returns an ``Outcome``: the error ``code`` and ``details`` (REST's ``error``; the
library's ``EpistrelError``, or ``validation_error`` with REST's ``details.field`` for a
pydantic refusal of the request model) or the JSON body. Library results are rendered through
the same response models REST uses, so the two surfaces are compared on content, not on
Python types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import pytest
from alembic import command
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.errors import field_path
from epistrel.api.routes.history import HistoryResponse
from epistrel.api.routes.respond import RespondResponse
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_sync_engine
from epistrel.core.errors import EpistrelError
from epistrel.core.stories import StorySpec
from epistrel.engine import Engine
from epistrel.orchestrator.lite.schemas import RespondRequest
from tests.auth_support import auth_settings, bearer, forget_ledger
from tests.conftest import alembic_config
from tests.respond_support import respond_client

SURFACES = ("rest", "library")
AUTHOR = "p-author"


@dataclass(frozen=True)
class Outcome:
    status: int
    code: str | None = None
    body: dict[str, Any] | None = None
    details: dict[str, Any] | None = None

    @property
    def json(self) -> dict[str, Any]:
        assert self.body is not None, f"refused: {self.code}"
        return self.body


class Surface:
    """``create_story``, ``respond``, ``history``, the inspection and State reads and
    ``get_story`` on one surface."""

    def __init__(self, name: str, client: httpx.AsyncClient, engine: Engine) -> None:
        self.name = name
        self.client = client
        self.engine = engine

    # -- calls ---------------------------------------------------------------------------

    async def create_story(self, body: dict[str, Any], principal: str = AUTHOR) -> Outcome:
        if self.name == "rest":
            return _rest(await self.client.post("/stories", json=body, headers=_headers(principal)))

        async def call() -> dict[str, Any]:
            created = await self.engine.create_story(
                RequestContext(principal=principal), StorySpec.model_validate(body)
            )
            return {
                "story_id": created.story_id,
                "characters": [{"id": c.id, "name": c.name} for c in created.characters],
            }

        return await _library(call, 201)

    async def respond(
        self,
        story_id: str,
        body: dict[str, Any],
        *,
        viewpoint: str | None = None,
        key: str | None = None,
        principal: str = AUTHOR,
    ) -> Outcome:
        """``viewpoint`` travels as ``?viewpoint=`` over REST and ``output_viewpoint=`` in the
        library; a body ``output_viewpoint`` goes in ``body`` on both."""
        if self.name == "rest":
            headers = _headers(principal) | ({"Idempotency-Key": key} if key else {})
            params = {"viewpoint": viewpoint} if viewpoint else {}
            return _rest(
                await self.client.post(
                    f"/stories/{story_id}/respond", json=body, headers=headers, params=params
                )
            )

        async def call() -> dict[str, Any]:
            result = await self.engine.respond(
                RequestContext(principal=principal, idempotency_key=key),
                story_id,
                RespondRequest.model_validate(body),
                output_viewpoint=viewpoint,
            )
            return _dump(RespondResponse.of(result))

        return await _library(call, 200)

    async def history(
        self, story_id: str, *, principal: str | None = None, **params: Any
    ) -> Outcome:
        """``params`` are the query parameters (``viewpoint``, ``as_of``, ``include_audit``,
        ``limit``, ``before_turn``, ``actor_character_id``) or the library's keywords."""
        if self.name == "rest":
            query = {k: _query(v) for k, v in params.items() if v is not None}
            headers = bearer() | ({"X-Epistrel-Principal": principal} if principal else {})
            return _rest(
                await self.client.get(f"/stories/{story_id}/history", params=query, headers=headers)
            )

        async def call() -> dict[str, Any]:
            found = await self.engine.history(
                RequestContext(principal=principal), story_id, **params
            )
            return _dump(HistoryResponse.of(found))

        return await _library(call, 200)

    async def inspect(
        self, kind: str, story_id: str, *, principal: str | None = None, **params: Any
    ) -> Outcome:
        """``GET /stories/{id}/inspect/<kind>`` (``ledger``, ``beliefs``, ``divergences``,
        ``exposures``; #178; ``external-facts``, #207) or ``Engine.inspect_<kind>``: ``params``
        are the query parameters or the library's keywords (``viewpoint`` among them; omitted
        when ``None``)."""
        if self.name == "rest":
            query = {k: _query(v) for k, v in params.items() if v is not None}
            headers = bearer() | ({"X-Epistrel-Principal": principal} if principal else {})
            return _rest(
                await self.client.get(
                    f"/stories/{story_id}/inspect/{kind}", params=query, headers=headers
                )
            )
        from epistrel.api.routes import inspect as routes

        bodies: dict[str, Any] = {
            "ledger": routes.LedgerBody,
            "beliefs": routes.BeliefsBody,
            "divergences": routes.DivergencesBody,
            "exposures": routes.ExposuresBody,
            "external-facts": routes.ExternalFactsBody,
        }

        async def call() -> dict[str, Any]:
            method = getattr(self.engine, f"inspect_{kind.replace('-', '_')}")
            keywords = {"viewpoint": None, **params}
            found = await method(RequestContext(principal=principal), story_id, **keywords)
            return _dump(bodies[kind].of(found))

        return await _library(call, 200)

    async def states(
        self, story_id: str, *, principal: str | None = None, **params: Any
    ) -> Outcome:
        """``GET /stories/{id}/states`` or ``Engine.get_states`` (#186): ``params`` are the
        query parameters (``viewpoint``, ``actor_character_id``) or the library's keywords."""
        if self.name == "rest":
            query = {k: _query(v) for k, v in params.items() if v is not None}
            headers = bearer() | ({"X-Epistrel-Principal": principal} if principal else {})
            return _rest(
                await self.client.get(f"/stories/{story_id}/states", params=query, headers=headers)
            )
        from epistrel.api.routes.states import StatesBody

        async def call() -> dict[str, Any]:
            context = RequestContext(principal=principal)
            found = await self.engine.get_states(context, story_id, **params)
            return _dump(StatesBody.of(found))

        return await _library(call, 200)

    async def state(self, story_id: str, name: str, **params: Any) -> Outcome:
        """``GET /stories/{id}/states/{name}`` or ``Engine.get_state`` (#186), as ``states``
        (``as_of`` too)."""
        if self.name == "rest":
            query = {k: _query(v) for k, v in params.items() if v is not None}
            return _rest(
                await self.client.get(
                    f"/stories/{story_id}/states/{name}", params=query, headers=bearer()
                )
            )
        from epistrel.api.routes.states import StateViewBody

        async def call() -> dict[str, Any]:
            found = await self.engine.get_state(RequestContext(), story_id, name, **params)
            return _dump(StateViewBody.of(found))

        return await _library(call, 200)

    async def get_story(self, story_id: str, viewpoint: str | None = None) -> Outcome:
        if self.name == "rest":
            params = {"viewpoint": viewpoint} if viewpoint else {}
            return _rest(
                await self.client.get(f"/stories/{story_id}", params=params, headers=bearer())
            )

        async def call() -> dict[str, Any]:
            story = await self.engine.get_story(RequestContext(), story_id, viewpoint=viewpoint)
            return {
                "characters": [
                    {"id": c.id, "name": c.name, "is_player": c.is_player}
                    | ({} if c.true_age is None else {"true_age": c.true_age})
                    # An omniscient read also names the catalog entries promoted from the
                    # character (#114).
                    | ({} if c.promoted_to is None else {"promoted_to": list(c.promoted_to)})
                    # and its narrative weight and the position it was modeled at (#163)
                    | ({} if c.weight is None else {"weight": c.weight})
                    # and every non-player character's participation role (#176)
                    | ({} if c.participation is None else {"participation": c.participation})
                    | (
                        {}
                        if c.modeled_at is None
                        else {
                            "modeled_at": {
                                "turn": c.modeled_at.turn,
                                "variant": c.modeled_at.variant,
                                "slot_k": c.modeled_at.slot_k,
                                "beat": c.modeled_at.beat,
                                "step": c.modeled_at.step,
                            }
                        }
                    )
                    for c in story.characters
                ]
            }

        return await _library(call, 200)


def _headers(principal: str) -> dict[str, str]:
    return bearer() | {"X-Epistrel-Principal": principal}


def _query(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _rest(response: httpx.Response) -> Outcome:
    body = response.json()
    if response.status_code >= 400:
        error = body["error"]
        return Outcome(response.status_code, code=error["code"], details=error.get("details"))
    return Outcome(response.status_code, body=body)


def _dump(model: BaseModel) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json", exclude_unset=True)
    return dumped


async def _library(call: Callable[[], Awaitable[dict[str, Any]]], status: int) -> Outcome:
    try:
        return Outcome(status, body=await call())
    except EpistrelError as error:
        return Outcome(error.http_status, code=error.code, details=dict(error.details or {}))
    except ValidationError as error:
        first = error.errors()[0]
        field = field_path(("body", *first["loc"]))
        return Outcome(422, code="validation_error", details={"field": field})


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture(params=SURFACES)
async def surface(
    request: pytest.FixtureRequest, stub_settings: Settings, engine: AsyncEngine
) -> AsyncIterator[Surface]:
    """The REST app and the library Engine behind it (one stub adapter), as one surface."""
    async with respond_client(stub_settings, engine) as (client, lib):
        yield Surface(request.param, client, lib)


@pytest.fixture
async def both(stub_settings: Settings, engine: AsyncEngine) -> AsyncIterator[dict[str, Surface]]:
    """Both surfaces over one app, for tests that compare them directly."""
    async with respond_client(stub_settings, engine) as (client, lib):
        yield {name: Surface(name, client, lib) for name in SURFACES}


@contextmanager
def migrated_database(pg_url: str, name: str) -> Iterator[str]:
    """A fresh database on the session's server, migrated to head, dropped afterwards (#73:
    C-01 and C-03 write deployment-wide state that must not reach the shared database). An app
    that ``auth_settings`` makes for it gets the database's own erasure ledger directory, emptied
    when the database is made and when it is dropped (#125)."""
    admin = make_sync_engine(pg_url, isolation_level="AUTOCOMMIT")
    drop = text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    try:
        with admin.connect() as conn:
            conn.execute(drop)
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = pg_url.rsplit("/", 1)[0] + f"/{name}"
        forget_ledger(url)
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(drop)
        admin.dispose()
        forget_ledger(pg_url.rsplit("/", 1)[0] + f"/{name}")
