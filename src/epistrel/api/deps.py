"""Request-scoped dependencies shared by every handler."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from epistrel.api.auth import require_principal
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.engine import Engine

IDEMPOTENCY_HEADER = "Idempotency-Key"
EXPECTED_REVISION_HEADER = "X-Epistrel-Expected-Revision"


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.epistrel
    return engine


def get_request_context(
    request: Request, principal: Annotated[str | None, Depends(require_principal)]
) -> RequestContext:
    """Build the ``RequestContext`` a handler passes to its one ``Engine`` call.

    Key and revision semantics arrive with #61 and #47; here they are only carried.
    """
    revision_raw = request.headers.get(EXPECTED_REVISION_HEADER)
    expected_revision: int | None = None
    if revision_raw is not None:
        try:
            expected_revision = int(revision_raw.strip())
        except ValueError:
            raise InvalidRequest(
                f"{EXPECTED_REVISION_HEADER} must be an integer",
                details={"field": f"headers.{EXPECTED_REVISION_HEADER}"},
            ) from None
    return RequestContext(
        principal=principal,
        idempotency_key=request.headers.get(IDEMPOTENCY_HEADER),
        expected_revision=expected_revision,
        viewpoint=request.query_params.get("viewpoint"),
    )


EngineDep = Annotated[Engine, Depends(get_engine)]
ContextDep = Annotated[RequestContext, Depends(get_request_context)]
