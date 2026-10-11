"""One error hierarchy for every surface (docs/02 §9).

Each error carries a stable snake_case ``code``, a human ``message`` and structured ``details``.
REST maps them to the envelope ``{"error": {"code", "message", "details"}}`` with
``http_status``; the in-process library raises them as they are.
"""

from __future__ import annotations

from typing import Any, ClassVar


class EpistrelError(Exception):
    """Base of every error Epistrel reports to a caller."""

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}

    def response_headers(self) -> dict[str, str]:
        """Headers a REST response for this error carries (none by default)."""
        return {}


class Unauthenticated(EpistrelError):
    """No valid service credential (FR-AUTH-1)."""

    code = "unauthenticated"
    http_status = 401

    def response_headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": "Bearer"}


class PrincipalRequired(EpistrelError):
    """A mutating call without an acceptable requesting principal (FR-AUTH-4)."""

    code = "principal_required"
    http_status = 400


class InvalidRequest(EpistrelError):
    """A request field failed validation; ``details.field`` is its dotted path."""

    code = "validation_error"
    http_status = 422


class StoryNotFound(EpistrelError):
    """The story named by a call does not exist."""

    code = "not_found"
    http_status = 404


class RevisionConflict(EpistrelError):
    """The story moved under the call and it could not be reconciled (FR-CONC-4c, FR-RESP-6).

    ``details``: ``current_revision``, ``attempts`` (generations tried) and ``reason``
    (``expected_revision`` for a stale ``X-Epistrel-Expected-Revision``, ``read_set_touched`` or
    ``rebase_failed`` when the regeneration budget ran out).
    """

    code = "revision_conflict"
    http_status = 409


class TimelineChanged(EpistrelError):
    """A control action edited the timeline while the operation ran (FR-CONC-4d).

    ``details``: ``current_revision`` and ``control_operation_id``. Distinct from
    ``revision_conflict`` so a consumer can tell "history was edited" from "someone else played".
    """

    code = "timeline_changed"
    http_status = 409


class ChainBroken(EpistrelError):
    """A commit would leave the active turn variants other than one chain (FR-STORE-28).

    An engine bug, never a caller error: the commit is rolled back.
    """

    code = "chain_broken"
    http_status = 500


class ConfigConflict(EpistrelError):
    """A configuration write the current state refuses (``details.reason``), e.g. retiring the
    deployment's default rating (``is_default``) or editing a retired one (``retired``)."""

    code = "validation_error"
    http_status = 409


class RatingNotFound(EpistrelError):
    """The content rating named by a call does not exist."""

    code = "not_found"
    http_status = 404


class RatingRetired(EpistrelError):
    """A rating that is retired (or unknown) cannot be newly assigned (FR-RATE-1).

    ``details``: ``rating_id``, ``field`` and ``reason`` (``retired`` or ``unknown``).
    """

    code = "rating_retired"
    http_status = 422


class InheritanceCycle(EpistrelError):
    """Saving the rating would make its inheritance cyclic (FR-RATE-2).

    ``details``: ``field`` (``body.inherits_from``) and ``cycle``, the rating ids around the loop,
    starting and ending with the rating being saved.
    """

    code = "inheritance_cycle"
    http_status = 422


class PolicyNotFound(EpistrelError):
    """The policy named by a call does not exist."""

    code = "not_found"
    http_status = 404


class CannotRelax(EpistrelError):
    """A policy write that would relax a policy (FR-GUARD-8): an ``effect`` other than ``deny``
    (``allow``/``exempt``, at any scope), or a lower-scope item that names a higher-scope policy.

    ``details``: ``field`` and ``reason`` (``effect`` or ``targets_higher_scope``), plus
    ``policy_id`` for the latter.
    """

    code = "cannot_relax"
    http_status = 422


class StoryArchived(EpistrelError):
    """The story is archived (read-only); truth-changing calls are refused with no side effects
    until it is unarchived (FR-ADMIN-1). ``details``: ``story_id``."""

    code = "story_archived"
    http_status = 409


class StoryBusy(EpistrelError):
    """The story has work in flight that the call would race (#50: archive, unarchive and
    delete wait for a running turn; #63 adds turn concurrency).

    ``details``: ``reason`` (``in_flight``) and ``operation_id``.
    """

    code = "story_busy"
    http_status = 423


class ClientEventConflict(EpistrelError):
    """An ingested ``client_event_id`` the story already recorded with a different payload
    (FR-RESP-10). Nothing is written. ``details``: ``client_event_ids``, each with its
    ``index`` in the request."""

    code = "client_event_conflict"
    http_status = 409


class ViewpointUnresolved(EpistrelError):
    """An output viewpoint names no character of the story, or is not allowed where it is used
    (a story's default viewpoint may only be a character, FR-RESP-12). ``details.field``."""

    code = "viewpoint_unresolved"
    http_status = 422


class OmniscientRequired(EpistrelError):
    """A private read (audit records, FR-RESP-12) asked for by a request that did not name the
    ``omniscient`` viewpoint. ``details``: ``field`` and ``output_viewpoint``."""

    code = "omniscient_required"
    http_status = 403


class IdempotencyConflict(EpistrelError):
    """An ``Idempotency-Key`` already used for a different request: a stored result is never
    handed out for another request, or another viewpoint (FR-CONC-6, FR-RESP-12). Nothing is
    written. ``details``: ``reason``."""

    code = "idempotency_conflict"
    http_status = 409


class OriginalInFlightTimeout(EpistrelError):
    """A retry waited for the original request under its ``Idempotency-Key`` until the turn
    deadline (+5 s) and the original is still running (FR-CONC-6). Nothing was started; retry
    later. ``details``: ``operation_id`` (the original's operation, ``null`` before it began)."""

    code = "original_in_flight_timeout"
    http_status = 503


class GuardrailViolation(EpistrelError):
    """Text breaks one or more policies that apply to it (FR-GUARD-10,11): the request is
    refused with no side effects.

    ``details.violations``: ``[{policy_id, name, message}]``. For an age-conditioned policy the
    error says only that a minor is involved or present: never which character, nor any age.
    """

    code = "guardrail_violation"
    http_status = 422


class BudgetBelowSafetyMinimum(EpistrelError):
    """The call's model budget cannot cover the model calls it must reserve before anything
    else (NFR-PERF-6): refused before any model call, with nothing recorded.

    ``details``: ``minimum_required`` and ``max_calls``.
    """

    code = "budget_below_safety_minimum"
    http_status = 422
