"""What an image read says about its output check (FR-IMG-3,10; NFR-QUAL-12; #258): the
``check`` block of every image read (``ImageCheck``: the checker's model and endpoint host, and per
applicable policy its result and whether the checker is certified for it), built from
``media_assets``' stored columns, and FR-IMG-10's two statements, which the media and image
schemas and ``docs/OPERATIONS.md`` § Images carry verbatim. Certification is reported per policy,
never as one status for the checker, so no schema has a top-level ``certified`` field."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from epistrel.core.tables.media import CheckStatus

UNCHECKED_STATEMENT = (
    "`unchecked` means Epistrel did not check this image; it carries no rating or viewpoint "
    "assurance from Epistrel and is never a pass."
)
"""FR-IMG-10's statement on ``unchecked``, verbatim in the media and image schemas and in
``docs/OPERATIONS.md`` § Images (#258)."""
PASSED_STATEMENT = (
    "A `passed` image carries only the measured assurance of its checker (NFR-QUAL-12): the "
    "output check is a backstop with measured error rates, not an assurance that every image is "
    "safe."
)
"""FR-IMG-3/10's statement on ``passed``, in the same places (#258)."""
CHECK_STATEMENTS = f"{UNCHECKED_STATEMENT} {PASSED_STATEMENT}"


class PolicyCheck(BaseModel):
    """One applicable image-output policy's result for a ``passed`` image (FR-IMG-10): the
    policy's id, version and name as they were when the image was checked, its result
    (``pass``; a violating image is never stored) and whether the checker is certified for
    this policy (NFR-QUAL-12). Certification is per policy, never one status for the
    checker."""

    policy_id: str
    policy_version: int | None
    name: str | None
    result: Literal["pass"]
    certified: bool


class ImageCheck(BaseModel):
    """How a ``passed`` image was checked (FR-IMG-3,10): the image-check model, its endpoint's
    host, and, per applicable policy, the result and the checker's certification for it."""

    model_config = ConfigDict(json_schema_extra={"description": CHECK_STATEMENTS})

    model: str
    endpoint: str
    policies: list[PolicyCheck]


def image_check_of(
    status: str, model: str | None, endpoint: str | None, results: Sequence[Mapping[str, Any]]
) -> ImageCheck | None:
    """An asset's ``check`` from its stored columns (``None`` unless ``passed``)."""
    if status != CheckStatus.PASSED.value or model is None or endpoint is None:
        return None
    return ImageCheck(
        model=model,
        endpoint=endpoint,
        policies=[
            PolicyCheck(
                policy_id=str(r["policy_id"]),
                policy_version=r.get("policy_version"),
                name=r.get("policy_name"),
                result="pass",
                certified=bool(r.get("certified")),
            )
            for r in results
        ],
    )


__all__ = [
    "CHECK_STATEMENTS",
    "PASSED_STATEMENT",
    "UNCHECKED_STATEMENT",
    "ImageCheck",
    "PolicyCheck",
    "image_check_of",
]
