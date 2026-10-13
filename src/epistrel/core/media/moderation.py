"""The consumer's moderation hook: one synchronous POST per upload, failing closed (FR-MEDIA-2;
#105).

Epistrel does not moderate media itself. When ``media_moderation_webhook_url`` is set, every
upload is sent there **before anything is stored**: a JSON body
``{asset: {id, kind, mime_type, size, width, height, duration_ms, content_rating_id, alt_text,
caption, owner_principal, depicted_subjects}, sha256, content_url}``, where ``content_url`` is a
5-minute signed URL of the staged bytes (``core.media.signing``), with the headers
``X-Epistrel-Timestamp`` (Unix seconds) and ``X-Epistrel-Signature`` (``sha256=`` + the HMAC of
``"<timestamp>." + body`` with the hook secret).

The hook must answer 2xx with ``{"decision": "accept"}`` or ``{"decision": "reject",
"reason": "…"}``. ``reject`` refuses the upload with 422 ``moderation_rejected`` and the hook's
reason. Everything else fails closed with 503 ``moderation_unavailable`` (the owner's decision):
a timeout (``media_moderation_timeout_ms`` for the whole exchange), a connection error, a
status other than 2xx (redirects are not followed), an answer larger than 64 KiB, or one that
is not such an object. There is no retry, and nothing is stored. The reason is returned to the
caller and never logged: log lines carry the outcome only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx2 as httpx

from epistrel.core import clock
from epistrel.core.errors import ModerationRejected, ModerationUnavailable
from epistrel.core.media.signing import Signer

logger = logging.getLogger(__name__)

TIMESTAMP_HEADER = "X-Epistrel-Timestamp"
SIGNATURE_HEADER = "X-Epistrel-Signature"
MAX_ANSWER_BYTES = 64 * 1024
REASON_MAX_CHARS = 1_000
ACCEPT, REJECT = "accept", "reject"


def _unavailable(reason: str, **details: Any) -> ModerationUnavailable:
    logger.warning("moderation hook unavailable (%s); the upload is refused", reason)
    return ModerationUnavailable(
        "the moderation hook did not accept the upload; nothing was stored",
        {"reason": reason, **details},
    )


def encode(payload: Mapping[str, Any]) -> bytes:
    """The request body: compact JSON with sorted keys (the bytes the signature covers)."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


async def _exchange(url: str, body: bytes, headers: Mapping[str, str], timeout_s: float) -> Any:
    """POST ``body`` and return the answer's JSON document (``ModerationUnavailable`` for
    anything that is not a 2xx JSON answer within the limits)."""
    try:
        async with (
            asyncio.timeout(timeout_s),
            httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False) as client,
            client.stream("POST", url, content=body, headers=dict(headers)) as response,
        ):
            if not 200 <= response.status_code < 300:
                raise _unavailable("status", status=response.status_code)
            answer = bytearray()
            async for chunk in response.aiter_bytes():
                answer += chunk
                if len(answer) > MAX_ANSWER_BYTES:
                    raise _unavailable("malformed")
    except TimeoutError, httpx.TimeoutException:
        raise _unavailable("timeout") from None
    except (httpx.HTTPError, OSError) as exc:
        raise _unavailable("connection") from exc
    try:
        return json.loads(bytes(answer))
    except ValueError:
        raise _unavailable("malformed") from None


async def moderate(
    url: str,
    payload: Mapping[str, Any],
    *,
    signer: Signer,
    timeout_ms: int,
) -> None:
    """Send ``payload`` to the hook at ``url`` and return when it accepts (see the module
    docstring); 422 ``moderation_rejected`` or 503 ``moderation_unavailable`` otherwise."""
    body = encode(payload)
    timestamp = int(clock.now().timestamp())
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: signer.hook_signature(timestamp, body),
    }
    answer = await _exchange(url, body, headers, timeout_ms / 1000)
    decision = answer.get("decision") if isinstance(answer, dict) else None
    if decision == ACCEPT:
        logger.info("moderation hook accepted the upload")
        return
    if decision == REJECT:
        reason = answer.get("reason")
        logger.info("moderation hook rejected the upload")
        raise ModerationRejected(
            "the moderation hook rejected the upload; nothing was stored",
            {"reason": reason[:REASON_MAX_CHARS] if isinstance(reason, str) else None},
        )
    raise _unavailable("malformed")


__all__ = [
    "ACCEPT",
    "MAX_ANSWER_BYTES",
    "REJECT",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "encode",
    "moderate",
]
