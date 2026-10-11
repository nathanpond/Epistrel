"""The dual-surface fixture (NFR-SEC-5's parity half, #59): one interface over REST and the
in-process library, so a conformance test runs the same requests through both.

Each call returns an ``Outcome``: the error ``code`` (REST's ``error.code``; the library's
``EpistrelError.code``, or ``validation_error`` for a pydantic refusal of the request model)
or the JSON body. Library results are rendered through the same response models REST uses,
so the two surfaces are compared on content, not on Python types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.routes.history import HistoryResponse
from epistrel.api.routes.respond import RespondResponse
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import EpistrelError
from epistrel.core.stories import StorySpec
from epistrel.engine import Engine
from epistrel.orchestrator.lite.schemas import RespondRequest
from tests.auth_support import auth_settings, bearer
from tests.respond_support import respond_client

SURFACES = ("rest", "library")
AUTHOR = "p-author"


@dataclass(frozen=True)
class Outcome:
    status: int
    code: str | None = None
    body: dict[str, Any] | None = None

    @property
    def json(self) -> dict[str, Any]:
        assert self.body is not None, f"refused: {self.code}"
        return self.body


class Surface:
    """``create_story``, ``respond``, ``history`` and ``get_story`` on one surface."""

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
        return Outcome(response.status_code, code=body["error"]["code"])
    return Outcome(response.status_code, body=body)


def _dump(model: BaseModel) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json", exclude_unset=True)
    return dumped


async def _library(call: Callable[[], Awaitable[dict[str, Any]]], status: int) -> Outcome:
    try:
        return Outcome(status, body=await call())
    except EpistrelError as error:
        return Outcome(error.http_status, code=error.code)
    except ValidationError:
        return Outcome(422, code="validation_error")


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
