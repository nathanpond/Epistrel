"""Every mutating route's response carries the revision its scope requires (FR-RESP-6)."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from epistrel.api import create_app
from epistrel.api.app import include_protected
from epistrel.api.responses import (
    DeploymentMutationResponse,
    MutationResponse,
    response_base_problems,
)
from tests.auth_support import auth_settings


@pytest.fixture
def app(dummy_dsn: str) -> FastAPI:
    return create_app(auth_settings(dummy_dsn))


def test_every_mutating_route_carries_its_revision(app: FastAPI) -> None:
    """Story-scoped mutations return `timeline_revision`; deployment-scoped admin mutations
    return `config_revision`. Complement: routes without the base are flagged."""
    assert response_base_problems(app) == []

    router = APIRouter()

    class Bare(BaseModel):
        ok: bool

    class StoryResult(MutationResponse):
        pass

    class AdminResult(DeploymentMutationResponse):
        pass

    @router.post("/stories/{story_id}/poke", response_model=Bare)
    async def poke(story_id: str) -> Bare:
        return Bare(ok=True)

    @router.post("/admin/knob", response_model=StoryResult)
    async def knob() -> StoryResult:
        return StoryResult(timeline_revision=1)

    @router.put("/admin/dial", response_model=AdminResult)
    async def dial() -> AdminResult:
        return AdminResult(config_revision=1)

    @router.get("/stories/{story_id}/peek")
    async def peek(story_id: str) -> dict[str, str]:
        return {}

    include_protected(app, router)
    assert response_base_problems(app) == [
        "POST /stories/{story_id}/poke (needs MutationResponse)",
        "POST /admin/knob (needs DeploymentMutationResponse)",
    ]
