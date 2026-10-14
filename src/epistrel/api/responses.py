"""Response bases every mutating route uses, and the route-table check that enforces them.

A story-scoped mutating response carries the story's ``timeline_revision`` after the call
(FR-RESP-6, FR-CONC-1); a deployment-scoped admin response carries ``config_revision`` instead
(FR-CONC-14). A route is story-scoped when its path names ``{story_id}``, and so is a route
that creates a story (``STORY_CREATING_ROUTES``): it returns the new story's revision. A route
whose status is 204 returns no body, so it carries no revision (``DELETE /stories/{id}``: the
story is gone), and a route flagged ``mutating=False`` (``deps.route_flags``) changes nothing,
so it carries none either.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, Field

from epistrel.api.deps import mutates

STORY_PATH_PARAMETER = "{story_id}"
STORY_CREATING_ROUTES = frozenset({("POST", "/stories")})
NO_CONTENT = 204


class MutationResponse(BaseModel):
    """Base of every story-scoped mutating response."""

    timeline_revision: int
    idempotency_key: str | None = Field(default=None, exclude_if=lambda key: key is None)
    """The request's ``Idempotency-Key``, echoed (server-generated when it sent none; #61).
    Absent from reads that share the model."""


class DeploymentMutationResponse(BaseModel):
    """Base of every deployment-scoped (admin) mutating response."""

    config_revision: int
    idempotency_key: str | None = Field(default=None, exclude_if=lambda key: key is None)
    """The request's ``Idempotency-Key``, echoed (server-generated when it sent none; #61).
    Absent from reads that share the model."""


def response_base_problems(app: FastAPI) -> list[str]:
    """Every mutating route whose response model lacks the revision base its scope requires."""
    problems: list[str] = []
    for route in iter_route_contexts(app.routes):
        original = route.original_route
        if not isinstance(original, APIRoute):
            continue
        methods = sorted(m for m in (route.methods or set()) if mutates(m, original))
        if not methods:
            continue
        if original.status_code == NO_CONTENT:
            continue
        path = route.path or ""
        model = original.response_model
        for method in methods:
            story_scoped = STORY_PATH_PARAMETER in path or (method, path) in STORY_CREATING_ROUTES
            base = MutationResponse if story_scoped else DeploymentMutationResponse
            if not (isinstance(model, type) and issubclass(model, base)):
                problems.append(f"{method} {path} (needs {base.__name__})")
    return problems
