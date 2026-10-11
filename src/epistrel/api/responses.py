"""Response bases every mutating route uses, and the route-table check that enforces them.

A story-scoped mutating response carries the story's ``timeline_revision`` after the call
(FR-RESP-6, FR-CONC-1); a deployment-scoped admin response carries ``config_revision`` instead
(FR-CONC-14). A route is story-scoped when its path names ``{story_id}``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel

from epistrel.api.auth import MUTATING_METHODS

STORY_PATH_PARAMETER = "{story_id}"


class MutationResponse(BaseModel):
    """Base of every story-scoped mutating response."""

    timeline_revision: int


class DeploymentMutationResponse(BaseModel):
    """Base of every deployment-scoped (admin) mutating response."""

    config_revision: int


def response_base_problems(app: FastAPI) -> list[str]:
    """Every mutating route whose response model lacks the revision base its scope requires."""
    problems: list[str] = []
    for route in iter_route_contexts(app.routes):
        original = route.original_route
        if not isinstance(original, APIRoute):
            continue
        methods = sorted(m for m in (route.methods or set()) if m in MUTATING_METHODS)
        if not methods:
            continue
        path = route.path or ""
        base = MutationResponse if STORY_PATH_PARAMETER in path else DeploymentMutationResponse
        model = original.response_model
        if not (isinstance(model, type) and issubclass(model, base)):
            problems.extend(f"{m} {path} (needs {base.__name__})" for m in methods)
    return problems
