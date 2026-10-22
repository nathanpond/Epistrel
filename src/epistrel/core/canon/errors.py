"""The errors of canon's write and read primitives (#133), under ``EpistrelError``."""

from __future__ import annotations

from epistrel.core.errors import EpistrelError, InvalidRequest


class FactNotFound(EpistrelError):
    """No fact version with this id in the story."""

    code = "fact_not_found"
    http_status = 404


class FactSuperseded(EpistrelError):
    """The version named is closed in transaction-time by a version in the timeline: supersede
    the fact's current version instead (``details.lineage_id`` names the fact)."""

    code = "fact_superseded"
    http_status = 409


class FactInactive(EpistrelError):
    """The version named left the timeline (its operation is discarded or dormant), so it
    cannot be superseded."""

    code = "fact_inactive"
    http_status = 409


class KeyMismatch(EpistrelError):
    """A supersede that changes the fact's uniqueness key (subject, predicate and key
    qualifiers): that is a different fact, written with ``commit_fact``."""

    code = "key_mismatch"
    http_status = 422


class UnknownPredicate(EpistrelError):
    """The proposition's predicate is not in the story's registry (#140 and #148 register a
    predicate before they commit a fact of it)."""

    code = "unknown_predicate"
    http_status = 422


class InvalidVisibility(InvalidRequest):
    """A visibility scope that does not hold: ``characters`` without characters, or a
    character outside the story's cast (``details.field``)."""


__all__ = [
    "FactInactive",
    "FactNotFound",
    "FactSuperseded",
    "InvalidVisibility",
    "KeyMismatch",
    "UnknownPredicate",
]
