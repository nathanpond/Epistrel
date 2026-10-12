"""The trust boundary (FR-AUTH-1, FR-AUTH-2, FR-AUTH-4, NFR-SEC-5; project invariant 4).

Every REST route except the five in ``EXEMPT_ROUTES`` requires a service credential, applied
app-wide through the protected router include in ``epistrel.api.app``. Every mutating call also
carries an opaque requesting principal. ``unguarded_routes`` is the route-table guard.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from epistrel.config import Settings
from epistrel.core.context import SYSTEM_PRINCIPAL, principal_problem
from epistrel.core.errors import PrincipalRequired, Unauthenticated

log = logging.getLogger(__name__)

PRINCIPAL_HEADER = "X-Epistrel-Principal"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/metrics"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
    }
)
"""(method, path) pairs served without a credential — invariant 4's complete exempt set.
``HEAD`` inherits its ``GET``'s exemption."""

TRUST_BOUNDARY_STATEMENT = (
    "Epistrel runs behind the consumer and must never be exposed to end users. It authenticates "
    "only the calling service, which sends one of the deployment's service credentials as "
    "`Authorization: Bearer <credential>`, and it holds no user roles, grants, or permissions: "
    "which end user may call which operation, act as which character, or request which output "
    "viewpoint is the consumer's decision, made before it calls. Its privacy guarantee is "
    "relative to the request: output is produced for the output viewpoint the request names, "
    "and Epistrel does not verify that the consumer was entitled to request that viewpoint, so "
    "Epistrel by itself does not stop a player from reaching another character's secrets. Every "
    "mutating call carries an opaque requesting principal in `X-Epistrel-Principal`, which "
    "Epistrel records for attribution and never interprets; use opaque IDs rather than names or "
    "email addresses, so recorded attribution identifies no one without your own records. "
    "Ownership and relations are labels the consumer defines; the engine never grants or "
    "withholds an action because of them. Ownership exists for listing, attribution and "
    "offboarding only."
)
"""FR-AUTH-2's documentation half, verbatim in the OpenAPI description, README and docs/02 §9;
its last two sentences are FR-AUTH-8's (#104): owners and relations gate nothing."""

_bearer = HTTPBearer(auto_error=False, description="A service credential (FR-AUTH-1).")


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def match_credential(presented: str, settings: Settings) -> int | None:
    """Index of the configured credential ``presented`` equals, or ``None``.

    Compares against every credential with ``hmac.compare_digest`` so timing reveals neither
    which credential matched nor how much of one did.
    """
    given = presented.encode()
    found: int | None = None
    for index, credential in enumerate(settings.service_credentials):
        if hmac.compare_digest(given, credential.get_secret_value().encode()) and found is None:
            found = index
    return found


async def require_service_credential(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Reject the call with 401 ``unauthenticated`` unless it carries a configured credential."""
    settings = _settings(request)
    if settings.auth_disabled:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise Unauthenticated("a service credential is required")
    index = match_credential(credentials.credentials, settings)
    if index is None:
        raise Unauthenticated("the service credential is not recognised")
    log.debug("service credential #%d accepted", index)


async def require_principal(request: Request) -> str | None:
    """The requesting principal: required on mutating methods (400 ``principal_required``).

    Runs as a router dependency, so it answers before the body is validated. On a read a
    well-formed principal is carried; a malformed one is ignored.
    """
    raw = request.headers.get(PRINCIPAL_HEADER)
    # Only spaces and tabs (HTTP whitespace) are trimmed: any other control character refuses.
    value = raw.strip(" \t") if raw is not None else None
    problem = "is required" if value is None else principal_problem(value)
    if problem is None and value == SYSTEM_PRINCIPAL:
        problem = f"`{SYSTEM_PRINCIPAL}` is reserved for background work"
    if problem is None:
        return value
    if request.method.upper() in MUTATING_METHODS:
        raise PrincipalRequired(
            f"{PRINCIPAL_HEADER} {problem}", details={"header": PRINCIPAL_HEADER}
        )
    return None


PROTECTED_DEPENDENCIES = [Depends(require_service_credential), Depends(require_principal)]
"""Applied to every protected router include (``epistrel.api.app.include_protected``)."""


# --- the route-table guard (invariant 4) -----------------------------------------------------


def _calls(dependant: Dependant) -> set[object]:
    found: set[object] = set()
    stack = [dependant]
    while stack:
        current = stack.pop()
        if current.call is not None:
            found.add(current.call)
        stack.extend(current.dependencies)
    return found


def _exempt(method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    return (method, path) in EXEMPT_ROUTES


def unguarded_routes(app: FastAPI) -> list[str]:
    """Every (method, path) the app serves without ``require_service_credential``.

    Exempt routes are matched by method and path first; any other route that is not an
    ``APIRoute`` (a mount, a websocket, a plain Starlette route) fails closed.
    """
    problems: list[str] = []
    # FastAPI includes routers lazily; route contexts are the routes as actually served, with
    # the include's prefix and dependencies applied.
    for route in iter_route_contexts(app.routes):
        path = route.path or repr(route.original_route)
        methods = sorted(route.methods or {"*"})
        if all(_exempt(m, path) for m in methods):
            continue
        if not isinstance(route.original_route, APIRoute):
            problems.append(f"{','.join(methods)} {path} ({type(route.original_route).__name__})")
            continue
        dependant: Dependant = route.dependant
        if require_service_credential not in _calls(dependant):
            problems.extend(f"{m} {path}" for m in methods if not _exempt(m, path))
    return problems
