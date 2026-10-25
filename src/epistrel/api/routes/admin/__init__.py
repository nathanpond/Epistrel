"""Operator endpoints, all under ``/admin`` so a deployment can restrict them at its edge.

FR-AUTH-1 names the operator surface; Epistrel treats these calls like any other (service
credential, principal on mutations). ``epistrel.api.routes`` includes it in the protected router
(#46, the first operator endpoint: ``POST /admin/stories/{id}/rebuild``).
"""

from __future__ import annotations

import re

from fastapi import APIRouter

from epistrel.api.routes.admin import (
    beliefs,
    erasure,
    feedback_export,
    model_config,
    mutation,
    offboarding,
    policies,
    ratings,
    reembed,
    settings,
    stories,
    verify,
)

ADMIN_PREFIX = "/admin"

router = APIRouter(prefix=ADMIN_PREFIX, tags=["admin"])
router.include_router(stories.router)
router.include_router(mutation.router)
router.include_router(beliefs.router)
router.include_router(ratings.router)
router.include_router(settings.router)
router.include_router(policies.router)
router.include_router(model_config.router)
router.include_router(reembed.router)
router.include_router(offboarding.router)
router.include_router(erasure.router)
router.include_router(feedback_export.router)
router.include_router(verify.router)
router.include_router(verify.gate_router)

_SEGMENT = r"(?:^|/){}(?:/|$)"

_OPERATOR_SEGMENTS = (
    r"model-config",  # model configuration
    r"(?:external-)?providers?",  # provider configuration and registration
    r"policies",  # global policies
    r"content-ratings",  # content-rating definitions (not the per-story content-rating)
    r"eras(?:e|ure)",  # erasure
    r"retention",
    r"export(?:s)?",  # story export, feedback export
    r"import(?:s)?",  # story import
    r"commit[_-]fact",  # the low-level mutation calls
    r"facts",  # #145: the low-level fact calls (commit_fact, supersede, lock)
    r"quarantine",  # #145: the mutation primitives
    r"sanitize",
    r"mutate",
    r"record[_-]utterance",
    r"ingest[_-]turn",
    r"offboard(?:ing)?",  # principal offboarding
)

OPERATOR_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    *(re.compile(_SEGMENT.format(segment)) for segment in _OPERATOR_SEGMENTS),
    re.compile(r"^/settings(?:/|$)"),  # deployment settings; /stories/{id}/settings is not
)
"""FR-AUTH-1's operator enumeration, including paths not built yet. A route outside ``/admin``
whose path matches one of these is misplaced."""


def is_operator_path(path: str) -> bool:
    return any(pattern.search(path) for pattern in OPERATOR_PATH_PATTERNS)


__all__ = ["ADMIN_PREFIX", "OPERATOR_PATH_PATTERNS", "is_operator_path", "router"]
