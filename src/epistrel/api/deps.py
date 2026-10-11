"""Request-scoped dependencies shared by every handler."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Request, Response

from epistrel.api.auth import MUTATING_METHODS, require_principal
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest
from epistrel.core.idempotency import IDEMPOTENCY_HEADER, validate_key
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.engine import Engine
from epistrel.orchestrator.policies.schemas import POLICY_ID_PATTERN

EXPECTED_REVISION_HEADER = "X-Epistrel-Expected-Revision"


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.epistrel
    return engine


def get_request_context(
    request: Request,
    response: Response,
    principal: Annotated[str | None, Depends(require_principal)],
) -> RequestContext:
    """Build the ``RequestContext`` a handler passes to its one ``Engine`` call.

    ``X-Epistrel-Expected-Revision`` must be an integer (422 otherwise). It is enforced by the
    Engine at the start of every story-scoped mutation (``Engine.check_expected_revision``,
    409 ``revision_conflict``) so the library gets the same rule; deployment-scoped calls ignore
    it.

    ``Idempotency-Key`` is optional on every mutating call (FR-RESP-6): a malformed key is 422
    (``headers.Idempotency-Key``), and a missing one is generated (a ULID). Every mutating
    response echoes the key in its ``Idempotency-Key`` header (#61).
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
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if request.method.upper() in MUTATING_METHODS:
        key = new_ulid() if key is None else validate_key(key)
        response.headers[IDEMPOTENCY_HEADER] = key
    return RequestContext(
        principal=principal,
        idempotency_key=key,
        expected_revision=expected_revision,
        viewpoint=request.query_params.get("viewpoint"),
        received_at=getattr(request.state, "received_at", None),
    )


PREFER_HEADER = "Prefer"
RESPOND_ASYNC = "respond-async"


def prefers_async(request: Request) -> bool:
    """``Prefer: respond-async`` (RFC 7240): answer a queued turn with 202 at once instead of
    waiting for it (#63). Other preferences are ignored."""
    tokens = (
        part.split("=", 1)[0].strip().lower()
        for header in request.headers.getlist(PREFER_HEADER)
        for part in header.replace(";", ",").split(",")
    )
    return RESPOND_ASYNC in tokens


EngineDep = Annotated[Engine, Depends(get_engine)]
ContextDep = Annotated[RequestContext, Depends(get_request_context)]
RespondAsyncDep = Annotated[bool, Depends(prefers_async)]


# -- path parameters: a malformed id is 422 at ``path.<name>`` before anything runs (FR-API-4) --

StoryId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The story's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"],
    ),
]
OperationId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The operation's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C4"],
    ),
]
RatingId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The content rating's id (a ULID).",
        examples=["00000000000000000000RAT002"],
    ),
]
PolicyIdPath = Annotated[
    str,
    Path(
        pattern=POLICY_ID_PATTERN,
        description="The policy's id: a client slug or a ULID.",
        examples=["minors_no_sexual_content"],
    ),
]
Purpose = Annotated[
    str,
    Path(
        description="A model purpose (`default`, `generation`, `guardrail_check`, …).",
        examples=["generation"],
    ),
]
