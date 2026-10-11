"""REST side of idempotent mutations (FR-CONC-6, FR-RESP-6; #61).

Every mutating request carries an ``Idempotency-Key`` (``get_request_context`` generates a ULID
when the caller sent none) and every mutating response echoes it: the ``Idempotency-Key``
header and, on a JSON body, the ``idempotency_key`` field. A replay of the stored result answers
with the original status and body plus ``Idempotent-Replay: true``; after the result window the
reference ``{operation_id, reference, <revision>, idempotency_key}`` is a 200 with the same
header (``ReferenceReplay``).
"""

from __future__ import annotations

from typing import Protocol

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from epistrel.core.context import RequestContext
from epistrel.core.idempotency import (
    IDEMPOTENCY_HEADER,
    IDEMPOTENT_REPLAY_HEADER,
    ReferenceReplay,
)


class _Keyed(Protocol):
    idempotency_key: str | None


def answered[B: _Keyed](body: B, ctx: RequestContext, response: Response, *, replayed: bool) -> B:
    """``body`` with the echoed key; ``Idempotent-Replay: true`` when it is a stored result."""
    body.idempotency_key = ctx.idempotency_key
    if replayed:
        response.headers[IDEMPOTENT_REPLAY_HEADER] = "true"
    return body


def no_content(ctx: RequestContext, *, replayed: bool) -> Response:
    """A 204 that still echoes the key (and flags a replay)."""
    headers = {IDEMPOTENCY_HEADER: ctx.idempotency_key or ""}
    if replayed:
        headers[IDEMPOTENT_REPLAY_HEADER] = "true"
    return Response(status_code=204, headers=headers)


async def reference_handler(request: Request, exc: Exception) -> JSONResponse:
    """A retry after the result window: 200 with the reference to the original operation."""
    assert isinstance(exc, ReferenceReplay)  # noqa: S101 - registered for it only
    return JSONResponse(
        exc.body(),
        status_code=200,
        headers={IDEMPOTENCY_HEADER: exc.key, IDEMPOTENT_REPLAY_HEADER: "true"},
    )


__all__ = ["answered", "no_content", "reference_handler"]
