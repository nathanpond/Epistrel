"""One error hierarchy for every surface (docs/02 §9).

Each error carries a stable snake_case ``code``, a human ``message`` and structured ``details``.
REST maps them to the envelope ``{"error": {"code", "message", "details"}}`` with
``http_status``; the in-process library raises them as they are.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

ValidationLoc = Sequence[int | str]


def field_path(loc: ValidationLoc) -> str:
    """A validation error's location as a dotted path with list indexes in brackets, matching
    the paths the Engine's own errors use: ``body.characters[0].true_age``."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path


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


NON_FIELD_ERRORS = frozenset({"json_invalid", "json_type", "model_attributes_type"})
"""Pydantic error types that describe the document as a whole, never one field: reported at
``body`` (a JSON syntax error's ``loc`` carries a character offset, not a field)."""


class InvalidRequest(EpistrelError):
    """A request field failed validation (FR-API-4).

    ``details.field`` is the dotted path of the first failure (``body.<field>``,
    ``query.<name>``, ``path.<name>`` or ``headers.<name>``; ``body`` when the document as a whole
    is malformed) and ``details.errors`` lists every failure as ``{field, message, type}``.
    """

    code = "validation_error"
    http_status = 422

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        details = dict(details or {})
        if type(self).code == InvalidRequest.code:
            details.setdefault("field", "body")
            details.setdefault(
                "errors", [{"field": details["field"], "message": message, "type": "value_error"}]
            )
        super().__init__(message, details)

    @classmethod
    def from_errors(cls, errors: Sequence[Any], *, prefix: ValidationLoc = ()) -> InvalidRequest:
        """The error for pydantic's ``errors()`` list; each ``loc`` is put under ``prefix``."""
        items = [
            {
                "field": _error_field(error, prefix),
                "message": str(error.get("msg", "invalid value")),
                "type": str(error.get("type", "value_error")),
            }
            for error in errors
        ]
        first = items[0] if items else {"field": field_path(prefix) or "body", "message": ""}
        return cls(
            first["message"] or "invalid request",
            details={"field": first["field"], "errors": items},
        )


NUL = "\x00"
"""PostgreSQL cannot store it in text; an input holding one is refused at its field."""


def nul_location(value: Any, loc: tuple[int | str, ...] = ()) -> tuple[int | str, ...] | None:
    """Where the first NUL character in ``value`` (a JSON-like document: its keys and strings)
    sits, or ``None``."""
    if isinstance(value, str):
        return loc if NUL in value else None
    if isinstance(value, dict):
        for key, item in value.items():
            found = nul_location(key, (*loc, key)) or nul_location(item, (*loc, key))
            if found is not None:
                return found
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found = nul_location(item, (*loc, index))
            if found is not None:
                return found
    return None


def refuse_nul(value: Any, loc: tuple[int | str, ...] = ()) -> None:
    """422 ``validation_error`` (type ``string_nul``) at the first NUL character in ``value``."""
    found = nul_location(value, loc)
    if found is not None:
        raise InvalidRequest.from_errors(
            [{"type": "string_nul", "loc": found, "msg": "Text must not contain NUL characters"}]
        )


def without_nul(value: Any) -> Any:
    """``value`` (a JSON-like document) with every NUL character removed from its keys and
    strings (#275). For text the Engine did not receive from a caller, a model's answer above
    all: refusing it would answer the caller for what the model wrote."""
    if isinstance(value, str):
        return value.replace(NUL, "")
    if isinstance(value, dict):
        return {without_nul(key): without_nul(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [without_nul(item) for item in value]
    return value


def _error_field(error: Any, prefix: ValidationLoc) -> str:
    loc = (*prefix, *tuple(error.get("loc", ())))
    if error.get("type") in NON_FIELD_ERRORS:
        loc = tuple(part for part in loc if isinstance(part, str))[:1] or ("body",)
    return field_path(loc) or "body"


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


class StoryPaused(EpistrelError):
    """The story is paused while a check runs (FR-CONC-5, FR-RATE-7; #118): truth-changing
    calls are refused with no side effects, and reads are served. The pause ends when the check
    ends, whatever its outcome. ``details``: ``story_id``, ``job_id`` (the check), ``cause``
    (``rating_check``; #120 adds ``reactivation_check``) and ``progress {done, total}``."""

    code = "story_paused"
    http_status = 409


class RatingCheckNotFound(EpistrelError):
    """The rating check a call names is not one of the story's (or its record was purged).
    ``details``: ``story_id`` and ``job_id``."""

    code = "not_found"
    http_status = 404


class StoryBusy(EpistrelError):
    """The story has work in flight that the call would race (#50: archive, unarchive and
    delete wait for a running turn; #63: a second turn under ``turn_concurrency = reject``, or
    a full queue under ``queue``, FR-CONC-5).

    ``details``: ``reason`` (``in_flight`` or ``queue_full``), ``operation_id`` (the turn
    holding the story, when one does) and, for turn concurrency, ``queue_depth``. A turn
    refusal carries ``Retry-After``: the seconds left on the running turn's lease.

    A commit that waited past the story-lock timeout for another transaction is refused the
    same way (#472): ``reason`` ``lock_timeout``, ``story_id``, ``Retry-After`` the lock timeout
    in seconds; nothing was written.
    """

    code = "story_busy"
    http_status = 423

    def __init__(
        self, message: str, details: dict[str, Any] | None = None, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after

    def response_headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}


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


class NothingToUndo(EpistrelError):
    """``untruncate`` found no truncation to revert, or the one it named is not the most recent
    (undo is LIFO, FR-STORE-18; #65). Nothing is written. ``details``: ``story_id``, when the
    request named one ``truncate_operation_id``, and while dormant continuations exist
    ``options`` (``["swap", "fork"]``) and ``dormant_chains`` (#66: a swap is undone by a
    swap)."""

    code = "nothing_to_undo"
    http_status = 409


class TurnBoundaryRequired(InvalidRequest):
    """A truncation or fork named a position inside a turn (a beat or a step): checkpoints,
    truncation and fork operate at turn boundaries only, ``N`` or an inter-turn slot boundary
    ``N⁺k`` (FR-STORE-31; #168). Nothing is written. ``details``: ``field`` (the offending
    field) and ``boundary`` (``to_turn``/``at_turn`` and ``slot``, the accepted shape)."""

    code = "turn_boundary_required"
    http_status = 422


class ViewpointCannotAssignTruth(EpistrelError):
    """A write named a character (or any holder but ``canon``) as the holder of a canon fact:
    a determined proposition has one truth value for everyone, and a character's divergent
    view is an owned belief, never a per-character value (FR-EPI-8; #169). Nothing is written.
    ``details``: ``field`` and ``holder``."""

    code = "viewpoint_cannot_assign_truth"
    http_status = 422


class IdentityMergeNotYet(EpistrelError):
    """A canon ``same_as`` would make two modeled characters one referent, or a caller asked to
    merge entities (FR-PROP-7; #171): merges and splits are amendments, M7's
    (``entities/merge``, ``entities/split``). Nothing is written; a turn's render files an
    identity proposal instead. ``details``: ``entities`` (the pair)."""

    code = "identity_merge_not_yet"
    http_status = 422


class UnknownChain(EpistrelError):
    """A ``{turn, variant}`` chain selector names no variant of the story, or names a chain
    that continues in more than one way (#66). Nothing is written. ``details``:
    ``variant_chain`` and, for an ambiguous chain, ``candidates`` (the chains it continues
    into, each a ``{turn, variant}`` that resolves on its own)."""

    code = "unknown_chain"
    http_status = 409


class NothingToRegenerate(EpistrelError):
    """``regenerate`` found no reply to regenerate: the story has no turns, or its latest turn
    is reply-less (an ingested player turn or a narration turn; FR-STORE-12, #66). Nothing is
    written. ``details``: ``story_id`` and ``turn``."""

    code = "nothing_to_regenerate"
    http_status = 422


class NotLatestTurn(EpistrelError):
    """``correct`` named a turn that is not the story's latest active turn (FR-RESP-14; #598):
    a later turn has committed (or the turn was rewound), so only history rewrite reaches it.
    Nothing is written. ``details``: ``story_id``, ``turn`` (the one named) and ``latest``."""

    code = "not_latest_turn"
    http_status = 409


class AlreadyCorrected(EpistrelError):
    """``correct`` named a turn a correction wrote (FR-RESP-14; #598): a corrected turn cannot
    be corrected again (swap back to the replaced one, or rewrite history). Nothing is written.
    ``details``: ``story_id``, ``turn`` and ``corrected_from`` (the replaced turn's
    operation)."""

    code = "already_corrected"
    http_status = 409


class DatabaseUnavailable(EpistrelError):
    """The database kept refusing a transaction that cannot be retried by the caller: a fork's
    snapshot copy hit a serialization failure on every attempt (#67; ``details``: ``attempts``),
    or the connection to it was lost (#75; ``details``: ``reason`` ``connection``, REST only).
    Nothing was written."""

    code = "database_unavailable"
    http_status = 503


# -- the catalog (#103) -------------------------------------------------------------------------


class CharacterNotFound(EpistrelError):
    """The catalog character (or the version of it) a call names does not exist.
    ``details``: ``character_id`` and, for a version read, ``version``."""

    code = "not_found"
    http_status = 404


class ScenarioNotFound(EpistrelError):
    """The scenario (or the version of it) a call names does not exist (#108). ``details``:
    ``scenario_id`` and, for a version read, ``version``."""

    code = "not_found"
    http_status = 404


class DraftStale(EpistrelError):
    """The ``draft_revision`` a save or publish was based on is no longer the draft's
    (FR-CAT-3): another editor saved or published since. Nothing is written; re-read the draft
    and retry from its revision. ``details``: the entry (``character_id`` or ``scenario_id``),
    ``draft_revision`` (sent) and ``current_draft_revision``."""

    code = "draft_stale"
    http_status = 409


class DraftTooLarge(EpistrelError):
    """The draft, after the patch is applied, is larger than the cap (canonical JSON: 256 KiB
    for a character, 1 MiB for a scenario). Nothing is written. ``details``: ``size`` and
    ``limit`` (bytes)."""

    code = "draft_too_large"
    http_status = 413


class EntryArchived(EpistrelError):
    """The catalog entry is archived: its draft cannot be saved or published until it is
    unarchived (reads still work). Nothing is written. ``details``: the entry
    (``character_id`` or ``scenario_id``)."""

    code = "entry_archived"
    http_status = 422


class NotPublished(EpistrelError):
    """A read of the published entry before its first publish: read the draft
    (``?draft=true``) instead. ``details``: the entry (``character_id`` or ``scenario_id``)."""

    code = "not_published"
    http_status = 422


class PublishInvalid(EpistrelError):
    """The draft cannot be published as it stands. Nothing is written. ``details.report`` is the
    validation report (FR-SCN-5; #111): ``{errors, warnings, unavailable}``, each entry ``{code,
    location: {entry, version, role, field, seed_id}, text, outcome, source, rule}``, every
    error's outcome ``rejected`` and its ``location.field`` a dotted path in the draft
    (``name``, ``cast.roles[0].character``)."""

    code = "publish_invalid"
    http_status = 422


class CastLimitExceeded(EpistrelError):
    """A scenario's ``max_characters_at_start`` above the deployment's
    ``max_characters_at_start_ceiling`` (FR-SCN-4; #108). Nothing is written. ``details``:
    ``field``, ``max_characters_at_start`` and ``ceiling``."""

    code = "cast_limit_exceeded"
    http_status = 422


class CreationRefused(EpistrelError):
    """A story's creation from a scenario refused with nothing written (FR-SCN-6; #119):
    ``details.report`` is the full creation report, listing every error. Each subclass is one
    code (the first of several conditions by precedence, ``core.genesis.create``)."""

    http_status = 422


class SlotRequired(CreationRefused):
    """A required role (or the player's role) is not filled."""

    code = "slot_required"


class LockedSlotReassigned(CreationRefused):
    """A ``locked`` role was given another character."""

    code = "locked_slot_reassigned"


class PlayerRoleFixed(CreationRefused):
    """A player put in a role that is not the scenario's player role, or the player's role
    filled as a character slot (FR-CAT-6)."""

    code = "player_role_fixed"


class PersonaRequired(CreationRefused):
    """A player's persona names a catalog character (FR-CAT-6)."""

    code = "persona_required"


class EligibilityFailed(CreationRefused):
    """A role's character fails one of its eligibility filters (FR-SCN-3)."""

    code = "eligibility_failed"


class OverrideNotAllowed(CreationRefused):
    """An override names a field the scenario's ``creation_rules.overridable`` does not."""

    code = "override_not_allowed"


class RatingCheckFailed(CreationRefused):
    """The requested rating adds guardrails to the scenario's, and the genesis content fails
    them (FR-RATE-7)."""

    code = "rating_check_failed"


class CreationConflicts(CreationRefused):
    """``strict`` creation found a conflict (a personality warning, FR-SCN-7)."""

    code = "creation_conflicts"


class SeedUnnormalizable(CreationRefused):
    """A structured creation's ``canon`` seed names an entity its cast and world do not have, or
    its story rules declare what the registry cannot hold (FR-PROP-12; #149): the author asked
    for canon, so there is no fallback."""

    code = "seed_unnormalizable"


class CreationArchived(CreationRefused):
    """The scenario or a chosen catalog character is archived."""

    code = EntryArchived.code


class CreationNotPublished(CreationRefused):
    """The scenario or a chosen catalog character has no published version (or the version is
    gone)."""

    code = NotPublished.code


class CreationCastLimit(CreationRefused):
    """More character slots, player slots included, than the effective starting-cast limit."""

    code = CastLimitExceeded.code


class InvalidListingStatus(EpistrelError):
    """A listing status that is not one of the deployment's ``listing_status_values``
    (FR-CAT-11; #110): set on an entry, or named as a list filter. Nothing is written.
    ``details``: ``value`` (as sent, trimmed), ``allowed_values`` (the deployment's, in order)
    and ``field``."""

    code = "invalid_listing_status"
    http_status = 422


class ListingStatusInUse(EpistrelError):
    """A ``listing_status_values`` write that removes a value some catalog entry still holds
    (FR-CAT-11; #110): move those entries to another value first. Nothing is written.
    ``details``: ``value`` (the first such value, in the old list's order) and ``count`` (the
    entries holding it: characters and scenarios of every status, companions included)."""

    code = "listing_status_in_use"
    http_status = 409


class CursorInvalid(EpistrelError):
    """A catalog listing's ``cursor`` that cannot continue this request (#110): it is not a
    cursor a listing gave (``reason`` ``malformed``), or it was given for another entry type or
    other filters (``filters_changed``; a cursor binds the filters, not ``limit``). Start again
    without it. ``details``: ``field`` (``query.cursor``) and ``reason``."""

    code = "cursor_invalid"
    http_status = 422


# -- ownership (#104) ---------------------------------------------------------------------------


class EntityNotFound(EpistrelError):
    """The owned entity a call names does not exist, or its type is not delivered by this
    version. ``details``: ``entity_type`` and ``entity_id``."""

    code = "not_found"
    http_status = 404


class RelationNotFound(EpistrelError):
    """The entity has no relation with that name and principal: nothing to remove.
    ``details``: ``entity_type``, ``entity_id``, ``relation`` and ``principal``."""

    code = "not_found"
    http_status = 404


class CompanionFollowsCharacter(EpistrelError):
    """A companion scenario follows its character (FR-CAT-5, FR-CAT-11, FR-AUTH-5): its owner,
    relations, listing status, archiving and deletion change only with the character's, so
    change them on the character. Nothing is written. ``details``: ``entity_type``,
    ``entity_id`` and ``character_id``."""

    code = "companion_follows_character"
    http_status = 422


class CompanionSlotsFixed(EpistrelError):
    """A save would add, remove or change a slot of a companion scenario (FR-CAT-5; #116): its
    two roles, ``npc`` locked to the character and ``player``, keep their ``id``, ``kind``,
    ``fill`` and ``character``; every other field of the scenario stays editable. Nothing is
    written. ``details``: ``scenario_id``, ``character_id``, ``role`` (the role id) and
    ``field`` (the request path of the slot field: ``body.cast.roles[0].fill``)."""

    code = "companion_slots_fixed"
    http_status = 422


# -- media (#105) -------------------------------------------------------------------------------


class MediaNotFound(EpistrelError):
    """The media asset a call names does not exist (or is a private candidate, FR-IMG-7).
    ``details``: ``media_id``."""

    code = "not_found"
    http_status = 404


class SubjectsRequired(EpistrelError):
    """An upload that did not declare ``depicted_subjects`` (FR-MEDIA-1, FR-IMG-9): an
    explicitly empty list is accepted, an omitted one is not. Nothing is stored. ``details``:
    ``field`` (``body.metadata.depicted_subjects``)."""

    code = "subjects_required"
    http_status = 422


class MediaTypeNotAllowed(EpistrelError):
    """The upload's bytes are not a type the deployment allows (``media_allowed_types``), are
    not a type Epistrel recognises, or disagree with the declared ``kind`` or MIME type.
    Nothing is stored. ``details``: ``reason`` (``unrecognized``, ``not_allowed``,
    ``declared_mismatch`` or ``kind_mismatch``), ``mime_type`` (sniffed, or null), ``declared``
    and ``allowed``."""

    code = "media_type_not_allowed"
    http_status = 415


class MediaTooLarge(EpistrelError):
    """The upload is larger than ``media_max_bytes``. Nothing is stored. ``details``:
    ``limit`` (bytes) and ``size`` (the bytes seen when the upload was cut, or the request's
    declared length)."""

    code = "media_too_large"
    http_status = 413


class ModerationRejected(EpistrelError):
    """The deployment's moderation hook answered ``reject``. Nothing is stored. ``details``:
    ``reason``, as the hook gave it (never logged)."""

    code = "moderation_rejected"
    http_status = 422


class ModerationUnavailable(EpistrelError):
    """The moderation hook did not answer ``accept`` or ``reject``: it timed out, could not be
    reached, answered a status other than 2xx (a redirect included), or answered something that
    is not ``{"decision": "accept" | "reject"}``. The upload fails closed and nothing is stored.
    ``details``: ``reason`` (``timeout``, ``connection``, ``status`` or ``malformed``) and, for
    ``status``, the hook's ``status``."""

    code = "moderation_unavailable"
    http_status = 503


class MediaRetired(EpistrelError):
    """A new reference to a media asset that cannot take one (FR-MEDIA-2): it was deleted
    (``retired``), it does not exist (``unknown``), or it is an unattached ``candidate``.
    Existing references keep their content. ``details``: ``media_id``, ``field`` and
    ``reason``."""

    code = "media_retired"
    http_status = 422


class SigningKeyMissing(EpistrelError):
    """``EPISTREL_CONFIG_KEY`` is unset, so the moderation hook's signed content URL and its
    request signature cannot be made: the upload is refused and nothing is stored."""

    code = "config_key_missing"
    http_status = 503


# -- media delivery (#107) ---------------------------------------------------------------------


class UrlExpired(EpistrelError):
    """A signed media URL presented without the service credential whose signature is good but
    whose expiry (``exp``, plus 30 s of clock skew) has passed (FR-MEDIA-3). ``details``:
    ``media_id`` and ``exp``."""

    code = "url_expired"
    http_status = 403


class SignatureInvalid(EpistrelError):
    """A signed media URL presented without the service credential whose ``sig`` does not
    sign its path and ``exp`` (tampered, or for another asset). ``details``: ``media_id``."""

    code = "signature_invalid"
    http_status = 403


class RangeNotSatisfiable(EpistrelError):
    """A ``Range`` no byte of the content satisfies (it starts at or past the end). The REST
    answer carries ``Content-Range: bytes */<size>``. ``details``: ``size``."""

    code = "range_not_satisfiable"
    http_status = 416

    def response_headers(self) -> dict[str, str]:
        return {"Content-Range": f"bytes */{self.details.get('size', 0)}"}


class ConfigMissing(EpistrelError):
    """A deployment setting the call needs is unset (``details.setting``), so it cannot be
    answered: ``GET /media/{id}/url`` on the local store without ``public_base_url``."""

    code = "config_missing"
    http_status = 503


class MediaStoreUnavailable(EpistrelError):
    """The S3-compatible store did not answer, or answered an error, while a call needed it.
    Nothing was written. ``details``: ``operation`` and ``reason`` (the store's error code or
    ``connection``)."""

    code = "media_store_unavailable"
    http_status = 503


class UseMultipart(EpistrelError):
    """A direct upload larger than ``media_multipart_threshold``: such an upload must use a
    resumable session (``POST /media/uploads``). Nothing is stored. ``details``: ``threshold``
    and ``size`` (the declared length, or the bytes seen when the upload was cut)."""

    code = "use_multipart"
    http_status = 413


class LengthRequired(EpistrelError):
    """An upload part sent without a ``Content-Length`` (a chunked body): a part's size must be
    declared. Nothing is stored."""

    code = "length_required"
    http_status = 411


class PartInvalid(EpistrelError):
    """An upload part numbered outside ``1..part_count``, or whose size is not the session's
    ``part_size`` (the last part: the remainder), or whose bytes do not match their declared
    length. Nothing is stored for it. ``details``: ``part``, ``reason`` and the sizes."""

    code = "part_invalid"
    http_status = 422


class UploadClosed(EpistrelError):
    """A part or a completion for an upload session that is no longer ``open``: completed,
    aborted, expired, or being completed by another call. ``details``: ``upload_id`` and
    ``state``."""

    code = "upload_closed"
    http_status = 409


class UploadIncomplete(EpistrelError):
    """A completion before every part arrived. The session stays open. ``details``:
    ``upload_id`` and ``missing`` (the part numbers)."""

    code = "upload_incomplete"
    http_status = 409


class ChecksumMismatch(EpistrelError):
    """The bytes' SHA-256 is not the one declared: the assembled upload's (the session stays
    open and no asset is created), or a part's ``X-Part-SHA256``. ``details``: ``declared``
    and ``actual`` (and ``part`` for a part)."""

    code = "checksum_mismatch"
    http_status = 422


class PosterRequired(EpistrelError):
    """A video uploaded without a poster while ffmpeg, which would extract one from its first
    frame, is unavailable. Nothing is stored. ``details``: ``setting`` (``ffmpeg_path``)."""

    code = "poster_required"
    http_status = 422


class PosterExtractionFailed(EpistrelError):
    """ffmpeg could not take a poster from the video's first frame (it failed, timed out or
    wrote no image). The whole upload is rolled back. ``details``: ``reason``."""

    code = "poster_extraction_failed"
    http_status = 422


class PosterInvalid(EpistrelError):
    """A supplied poster (``poster``) that is not an image asset. ``details``: ``media_id``,
    ``field`` and ``reason`` (``not_image``)."""

    code = "poster_invalid"
    http_status = 422


class UploadNotFound(EpistrelError):
    """The upload session a call names does not exist (or its row was swept). ``details``:
    ``upload_id``."""

    code = "not_found"
    http_status = 404


# -- engine profiles (#106) -------------------------------------------------------------------


class ProfileNotFound(EpistrelError):
    """The engine profile a call names does not exist (or was purged), or has no such version.
    ``details``: ``profile_id`` (and ``version``)."""

    code = "not_found"
    http_status = 404


class InvalidProfile(EpistrelError):
    """A profile's settings (or a story's effective configuration, #113) cannot run: they name a
    module or level this deployment has not delivered, or a module whose dependencies are off
    (FR-PROF-2,4). Nothing is written, and ``ignore_warnings`` never overrides it (FR-PROF-7).
    ``details``: ``missing_dependencies`` (``[{module, level, requires: [{module, levels}]}]``)
    and ``undelivered`` (``[{module, level, setting}]``)."""

    code = "invalid_profile"
    http_status = 422


class ProfileDeleted(EpistrelError):
    """A deleted engine profile cannot be newly assigned (as the deployment default) or edited;
    it still reads, copies, and serves the stories that use it (FR-PROF-1). An unknown profile
    named as the default is refused the same way. ``details``: ``profile_id``, ``field`` and
    ``reason`` (``deleted`` or ``unknown``)."""

    code = "profile_deleted"
    http_status = 422


# -- message feedback (#112) --------------------------------------------------------------------


class MessageNotFound(EpistrelError):
    """The message a rating names is not a generated message of the story: no ``message_rendered``
    event of the story has that id, or it is the consumer's own narration, which no
    configuration produced (FR-FB-1). ``details``: ``story_id`` and ``message_id``."""

    code = "not_found"
    http_status = 404


class UnknownFeedbackTag(EpistrelError):
    """A rating names a reason tag that is not one of the deployment's ``feedback_tags``
    (FR-FB-1). Nothing is written. ``details``: ``field`` (``body.tags``), ``unknown_tags`` (as
    compared: trimmed and lower-cased) and ``allowed_tags`` (the deployment's, in order)."""

    code = "unknown_feedback_tag"
    http_status = 422


class FeedbackTagInUse(EpistrelError):
    """A ``feedback_tags`` write that removes a tag a rating or a history entry still carries
    (FR-FB-1; #112). Nothing is written. ``details``: ``field`` (``body.feedback_tags``) and
    ``in_use``: one ``{tag, current, history}`` per such tag, in the old list's order, counting
    the ratings and the history entries that carry it."""

    code = "feedback_tag_in_use"
    http_status = 409


class FeedbackExportDisabled(EpistrelError):
    """A feedback export asked for while the deployment's ``feedback_export`` setting is off
    (FR-FB-4; #126; the default). Nothing is written: no export, no log row, no idempotency
    key. ``details``: ``setting`` (``feedback_export``)."""

    code = "feedback_export_disabled"
    http_status = 403


class FeedbackExportNotFound(EpistrelError):
    """The feedback export a call names does not exist, or (``reason`` ``deleted``) its file was
    deleted, by an operator or by the media sweep after seven days; the log row stays.
    ``details``: ``export_id`` and, for a deleted file, ``reason``."""

    code = "not_found"
    http_status = 404


# -- personas and story characters (#114) -------------------------------------------------------


class PersonaNotFound(EpistrelError):
    """The persona a call names does not exist (or was deleted). ``details``: ``persona_id``."""

    code = "not_found"
    http_status = 404


class PersonaNameTaken(EpistrelError):
    """The owner already has a persona with that name, ignoring case (FR-CAT-6): a creation, a
    rename, or an owner transfer that would give one principal two personas of one name.
    Nothing is written. ``details``: ``field``, ``owner_principal`` and, when the write names
    one, ``persona_id``."""

    code = "persona_name_taken"
    http_status = 409


class StoryCharacterNotFound(EpistrelError):
    """The story has no character with that id (it may belong to another story). ``details``:
    ``story_id`` and ``character_id``."""

    code = "not_found"
    http_status = 404


class StoryEntityNotFound(EpistrelError):
    """The story has no entity with that id (it may belong to another story; #137's profile
    read). ``details``: ``story_id`` and ``entity_id``."""

    code = "not_found"
    http_status = 404


class OffboardingNotFound(EpistrelError):
    """The offboarding job a call names does not exist (or its report was purged).
    ``details``: ``job_id``."""

    code = "not_found"
    http_status = 404


class PrincipalReserved(EpistrelError):
    """An offboarding of ``system``, the principal background work and the presets act as
    (FR-AUTH-7; #117). Nothing is written. ``details``: ``field``, ``principal``."""

    code = "principal_reserved"
    http_status = 422


class TransferTargetRequired(EpistrelError):
    """An offboarding names a ``transfer`` action but no ``transfer_to`` (FR-AUTH-7; #117).
    Nothing is written. ``details``: ``field`` and ``types`` (the types that transfer)."""

    code = "transfer_target_required"
    http_status = 422


class TransferTargetInvalid(EpistrelError):
    """An offboarding's ``transfer_to`` is the offboarded principal itself or ``system``
    (FR-AUTH-7; #117). Nothing is written. ``details``: ``field``, ``reason`` (``same_principal``
    or ``reserved``)."""

    code = "transfer_target_invalid"
    http_status = 422


class OffboardingInProgress(EpistrelError):
    """The principal already has a queued or running offboarding (FR-AUTH-7; #117): read its
    progress, and ask again once it finishes. Nothing is written. ``details``: ``principal`` and
    ``job_id``."""

    code = "offboarding_in_progress"
    http_status = 409


# -- erasure (#124) -----------------------------------------------------------------------------


class UnknownErasureCandidate(EpistrelError):
    """An erasure confirms a candidate mention it does not meet (ADR-154; #372): the id is
    stale (the text changed since the dry run) or was never one of this subject's candidates.
    Nothing is written. ``details``: ``field`` and ``candidate_ids``."""

    code = "unknown_erasure_candidate"
    http_status = 422


class InvalidErasureScope(EpistrelError):
    """An erasure pairs a subject kind with a scope it cannot have (FR-ADMIN-2; #124): a story
    ``character`` exists only in its story, so its erasure is story-scoped. Nothing is written.
    ``details``: ``field``, ``subject_kind`` and ``scope``."""

    code = "invalid_erasure_scope"
    http_status = 422


class SubjectNotFound(EpistrelError):
    """The subject an erasure names is unknown in its scope (no such character in the story, no
    catalog character or persona by that id, and no earlier erasure of it there): nothing to
    erase or to ledger (#124). A dry run answers zero counts instead. ``details``:
    ``subject_kind``, ``subject_id`` and, for the story scope, ``story_id``."""

    code = "not_found"
    http_status = 404


class ErasureKeyMissing(EpistrelError):
    """``EPISTREL_CONFIG_KEY`` is unset, so the erasure ledger's keyed hashes of the scanned
    terms cannot be made (an unkeyed hash of a name could be reversed by guessing): the erasure
    is refused and nothing is written (#124). ``details``: ``setting``."""

    code = "config_key_missing"
    http_status = 503


class ErasedSubjectReferenced(EpistrelError):
    """The operation's text names, or its records reference, a subject erased from the story
    after the context it was built from (FR-ADMIN-9; #127): an in-flight turn, a late worker
    result or a model output built before the erasure would write the subject back, so nothing
    is written and the operation ends ``abandoned``. The caller's context is stale: rebuild it
    from the current history and retry. ``details``: ``erasure_id`` (the erasure matched) and
    ``match_count`` (the texts and references that matched it)."""

    code = "erased_subject_referenced"
    http_status = 409


class ErasurePending(EpistrelError):
    """The story is blocked by the erasure reconciler (FR-ADMIN-7; #125): the erasure ledger
    holds an erasure of a subject in it that this database has not applied (a restored backup
    that predates it), so the story is not served until the erasure is reapplied. Every read
    and write of the story is refused, except deleting it; retry after ``Retry-After`` seconds.
    ``details``: ``story_id``, ``erasure_id`` (the first pending erasure) and ``pending_count``
    (the story's pending erasures). An undelivered feedback export is also refused while a
    catalog-scope erasure is pending (#378): ``story_id`` is then null and ``pending_count``
    counts the pending catalog erasures."""

    code = "erasure_pending"
    http_status = 503
    RETRY_AFTER_S = 30

    def response_headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.RETRY_AFTER_S)}


class LedgerUnavailable(EpistrelError):
    """The erasure ledger's file (``EPISTREL_ERASURE_LEDGER_DIR``) could not be written or read,
    or its hash chain is broken (FR-ADMIN-7; #125): an erasure is refused and nothing is
    written, since an erasure the file does not hold would not survive a restore. ``details``:
    ``reason`` (``unwritable``, ``unreadable``, ``chain_broken``, ``mismatch``)."""

    code = "ledger_unavailable"
    http_status = 503


class ModuleOff(EpistrelError):
    """A call needs a profile module the story's effective configuration has off (FR-PROF-2;
    #143: ``POST /admin/stories/{id}/consolidate`` with ``memory.consolidation.enabled`` off).
    ``details``: ``story_id`` and ``module``. Nothing is written."""

    code = "module_off"
    http_status = 409


class StateNotFound(EpistrelError):
    """The State a call names is not one the story declares on its active timeline (FR-STATE-1;
    #185). ``details``: ``story_id`` and ``state``."""

    code = "not_found"
    http_status = 404


class StateNameTaken(EpistrelError):
    """A State declaration reuses a name a State on the story's active timeline holds (FR-STATE-1;
    #185; #186: a declaration a truncation discarded frees its name), or an untruncation or a
    swap would make two active States share one. ``details``: ``story_id`` and ``state``.
    Nothing is written."""

    code = "state_name_taken"
    http_status = 409


class StateConstraintViolation(EpistrelError):
    """A State write would leave a value its constraints refuse (FR-STATE-6; #185): a definition
    update whose new constraints the current value already violates (#186's set refuses the
    same way). ``details``: ``state``, ``constraint``, ``attempted`` and ``current``. Nothing is
    written."""

    code = "state_constraint_violation"
    http_status = 422


class StateReadonlyExternal(EpistrelError):
    """A set of an externally owned State through the State API (FR-STATE-2,9, FR-EXT-8; #186):
    its value is the provider's, written through the external path (#209, #210). ``details``:
    ``story_id`` and ``state``. Nothing is written."""

    code = "state_readonly_external"
    http_status = 422


class StateUnpermitted(EpistrelError):
    """A State write from a source the State's ``authorization.sources`` does not admit
    (FR-STATE-9; #186, owner decision 2026-10-30; #188 owns the full rule): an API set or an
    ingested ``state_change`` of a State whose sources omit ``consumer``. ``details``:
    ``story_id``, ``state``, ``reason`` (``source``) and ``source``. Nothing is written."""

    code = "state_unpermitted"
    http_status = 422


class StateDerived(EpistrelError):
    """A direct set of a derived State (FR-STATE-7; #186, #192): its value is recomputed from
    other States. ``details``: ``story_id`` and ``state``. Nothing is written."""

    code = "state_derived"
    http_status = 422


class ClockBackward(EpistrelError):
    """A world clock advance that would move it backward along the active timeline (FR-CLOCK-8,13;
    #193): a negative number of minutes, or a target offset before the clock's value there.
    ``details``: ``story_id``, ``current`` (the offset in minutes) and ``attempted``. Nothing is
    written; earlier story-time is reached by a truncation or a rewrite."""

    code = "clock_backward"
    http_status = 422


class ClockUnanchored(EpistrelError):
    """A clock target given as a time of day (``"HH:MM"``) while the clock's anchor does not fix
    the hour (FR-CLOCK-3; #193, until #195's anchors): the engine never picks a time the story
    has not established. ``details``: ``story_id`` and ``to``. Nothing is written."""

    code = "clock_unanchored"
    http_status = 422


class ClockExternal(EpistrelError):
    """A consumer's advance of a world clock in ``external`` mode (FR-CLOCK-5; #209): the clock is
    a provider's value, read and pinned each turn, so Epistrel never moves it (the consumer
    advances its own clock). ``details``: ``story_id`` and ``provider_id``. Nothing is
    written."""

    code = "clock_external"
    http_status = 422


class ScopeMismatch(EpistrelError):
    """A consolidation's inputs span viewpoint scopes no one output may combine (FR-MEM-9; #143):
    the intersection of their scopes is empty (two characters' private memories). Refused when
    the job is planned, before any model call. ``details``: ``story_id`` and ``scopes`` (the
    input scopes found)."""

    code = "scope_mismatch"
    http_status = 422


class MemoryNotFound(EpistrelError):
    """The memory a call names is not one of the story's (FR-GUARD-4; #145). ``details``:
    ``story_id`` and ``memory_id``."""

    code = "not_found"
    http_status = 404


class PinLimitExceeded(EpistrelError):
    """A pin would take a character or the story past its pin ceiling (FR-MEM-8; #147):
    ``max_pinned_per_character`` or ``max_pinned_per_story`` (a story's pinned memories and its
    text pins together). ``details``: ``scope`` (``character`` or ``story``: the first limit the
    pin hits), ``limit``, ``actual`` (what the story would hold) and, for a character,
    ``character_id``. Nothing is written; unpinning admits again."""

    code = "pin_limit_exceeded"
    http_status = 422


class SeedMemoryInvalid(EpistrelError):
    """A memory a promotion names to seed into the new character's companion scenario cannot
    be seeded (FR-CAT-8; #498). ``details``: ``field`` (``body.seed_memories[n]``),
    ``memory_id`` and ``reason``: ``not_in_story`` (no such memory in the story),
    ``inactive`` (off the active timeline), ``quarantined``, or ``not_known`` (the character
    neither holds it nor can know it). Nothing is written."""

    code = "seed_memory_invalid"
    http_status = 422


class SpanNotFound(EpistrelError):
    """A sanitize's ``span`` does not occur in the memory's text (FR-GUARD-4; #145): it is an
    exact substring, every occurrence of which is replaced. ``details``: ``memory_id``.
    Nothing is written."""

    code = "span_not_found"
    http_status = 422


class GateNotPromotable(EpistrelError):
    """A shape's proposed gate tightening cannot be promoted yet (FR-OBS-4, NFR-PERF-5; #155):
    its shadow window holds fewer escalations than ``gate_promotion_min_cases``, or fewer than
    ``gate_promotion_min_agreement`` of the would-have-skipped ones agreed with the model.
    ``details``: ``shape``, ``cases``, ``required_cases``, ``agreement`` (``null`` with no
    would-have-skipped case) and ``required_agreement``. Nothing is written."""

    code = "gate_not_promotable"
    http_status = 409


class GateThresholdNotFound(EpistrelError):
    """No gate threshold is proposed or in force for the shape a call names (#155).
    ``details``: ``shape``."""

    code = "not_found"
    http_status = 404


# -- external-data providers (FR-EXT-1,6; #204) ---------------------------------------------------


class ProviderNotFound(EpistrelError):
    """No external-data provider is registered under the name a call gives (FR-EXT-1; #204).
    ``details``: ``provider``."""

    code = "not_found"
    http_status = 404


class ProviderExists(EpistrelError):
    """A registration names a provider already registered (#204): replace it with ``PUT``.
    ``details``: ``provider``. Nothing is written."""

    code = "provider_exists"
    http_status = 409


class ProviderInUse(EpistrelError):
    """Deleting a provider a story's State names (#204: ``state_definitions.provider_id`` has no
    foreign key, so the delete is refused). ``details``: ``provider`` and ``stories`` (their
    ids). Nothing is written."""

    code = "provider_in_use"
    http_status = 409


class InvalidManifest(InvalidRequest):
    """A provider registration's manifest breaks a rule (FR-EXT-1, FR-EXT-5; #204): an unknown
    placeholder, a placeholder in a URL's scheme, host or port, a URL that is not ``https`` (or
    ``http`` on a loopback host), a host the deployment's ``provider_allowed_hosts`` lacks, a
    path expression outside ``$``/``.key``/``[n]``, or a write template on a provider that is not
    write-capable. ``details``: ``field`` (the manifest member) and ``reason``. Nothing is
    written."""

    code = "invalid_manifest"
    http_status = 422


class BindingIncomplete(EpistrelError):
    """A story's binding to a provider lacks a key the provider's manifest substitutes
    (``{binding.<key>}``; #204). ``details``: ``provider`` and ``missing`` (the keys). Nothing
    is written."""

    code = "binding_incomplete"
    http_status = 422


class ProviderUnbound(EpistrelError):
    """A turn of a story that needs a provider (an externally owned State names it, or the
    story's ``required_providers`` lists it) has no binding to it (#204): it fails closed before
    anything is recorded. ``details``: ``story_id`` and ``providers`` (the unbound ones)."""

    code = "provider_unbound"
    http_status = 422


class ProviderDown(EpistrelError):
    """A turn read an external value whose provider could not answer (an adapter error, a
    timeout, a missing secret or callable, a malformed answer) and the provider's unavailability
    policy is ``fail_turn`` (FR-EXT-7; #206): the turn fails cleanly before its commit, so
    nothing is recorded (no event, no ``external_read``). ``details``: ``provider`` and
    ``value`` (the provider's value name)."""

    code = "provider_unavailable"
    http_status = 503


class ProviderUnregistered(EpistrelError):
    """A story created from a scenario requires providers the deployment has not registered
    (``story_settings_defaults.required_providers``; #204). ``details``: ``providers`` (the
    missing ones). Nothing is written."""

    code = "provider_unregistered"
    http_status = 422


class EffectNotFound(EpistrelError):
    """An ingested ``outcome`` names an external effect the story does not know (#217,
    FR-EXT-21): no outbox row with its ``outbox_id``, no ``requested_effect`` with its
    ``effect_id``. ``details``: ``locator`` (``events[i]``) and ``effect_id`` or ``outbox_id``.
    Nothing is written."""

    code = "effect_not_found"
    http_status = 422


class SettleRefused(EpistrelError):
    """An author's settle-now (#218, FR-EXT-22) whose re-verification against current state
    fails at that moment: the effect is not applied and nothing changes. ``details``:
    ``outbox_id`` and ``reasons`` (codes: ``precondition:availability``,
    ``precondition:ownership``, ``bounds:<constraint>``, ``settlement_conflict``,
    ``not_confirmed``)."""

    code = "settle_refused"
    http_status = 409


class ProviderReadOnly(EpistrelError):
    """A compensating effect (#218, FR-EXT-22) on a State whose provider is not write-capable:
    Epistrel cannot issue it. ``details``: ``state`` and ``provider``. Nothing is written."""

    code = "provider_read_only"
    http_status = 422


# -- amendments (#221) ---------------------------------------------------------------------------


class AmendmentInvalidPart(EpistrelError):
    """An amendment part cannot apply (FR-REWRITE-12; #221): ``details`` name its ``ordinal``
    and ``reason`` (``record_not_found``, ``wrong_story``, ``table_not_correctable``,
    ``column_not_allowed``, ``action_not_allowed``, ``unresolved_time``, ``state_constraint``,
    ``contingent_change``, ``external_effect``, ``derived_state``, ``clock_advance``). Nothing is
    written."""

    code = "invalid_amendment_part"
    http_status = 422


class AmendmentTargetInactive(EpistrelError):
    """A record part names a record outside the active timeline (discarded or dormant; #221):
    ``details``: ``record_table``, ``record_id``, ``membership``. Nothing is written."""

    code = "record_inactive"
    http_status = 409


class ScheduleNotAmendable(EpistrelError):
    """A proposition part on ``scheduled`` (an event's planned time, place or participants;
    #221, the owner's decision 4): a superseded ``scheduled`` fact does not move its event, so
    the event is moved with ``PATCH /stories/{id}/scheduled-events/{event_id}`` (``details``:
    ``event_id``, ``route``). Nothing is written."""

    code = "schedule_not_amendable"
    http_status = 422


class RewriteUnparseable(EpistrelError):
    """A rewrite instruction (or a free-text answer to a new session) could not be parsed into
    changes after #53's retries (#223): 422; no session is written."""

    code = "rewrite_unparseable"
    http_status = 422


class RewriteNoop(EpistrelError):
    """A rewrite instruction parsed into no change, or only changes already true in canon
    (#223): 422; no session is written."""

    code = "rewrite_noop"
    http_status = 422


class RewriteSessionNotFound(EpistrelError):
    """No rewrite session has this id in this story (#223): 404."""

    code = "rewrite_session_not_found"
    http_status = 404


class RewriteSessionClosed(EpistrelError):
    """An action on a session that has ended (applied, cancelled, expired or abandoned; #223):
    409. ``details``: ``session_id`` and ``state``."""

    code = "rewrite_session_closed"
    http_status = 409


class RewriteSessionLimit(EpistrelError):
    """The story already has ``rewrite_max_open_sessions`` open sessions (#223): 409.
    ``details``: ``limit`` and ``open``."""

    code = "rewrite_session_limit"
    http_status = 409


class InvalidAnswer(EpistrelError):
    """An answer names a question the session does not ask, an option the question does not
    have, or free text that parses to nothing (#223): 422; nothing is stored."""

    code = "invalid_answer"
    http_status = 422


class BaseRevisionRequired(EpistrelError):
    """An apply names no ``base_revision`` (#224): 422; nothing is written."""

    code = "base_revision_required"
    http_status = 422


class RewriteStale(EpistrelError):
    """The plan no longer holds (#224, #237): 409, nothing written. An apply naming another
    ``base_revision`` than the session's preview has ``reason: base_mismatch`` and the current
    preview; a plan the final commit's revalidation found stale was recomputed:
    ``base_revision`` (the new one), ``preview``, ``carried_answers``, ``new_questions``,
    ``reasons`` (``{kind, refs}``) and ``stale_count``; a changed pipeline is
    ``reason: pipeline_changed`` (the session abandoned)."""

    code = "rewrite_stale"
    http_status = 409


class RewriteQuestionSuperseded(EpistrelError):
    """An answer to a question a re-plan superseded (#237): 409. ``details``:
    ``question_id``, ``new_question_id`` (the question now asked under the same key, or
    ``None``)."""

    code = "rewrite_question_superseded"
    http_status = 409


class RewriteQuestionsPending(EpistrelError):
    """An apply while required questions are unanswered (#224): 409. ``details``:
    ``question_ids``."""

    code = "rewrite_questions_pending"
    http_status = 409


class RewriteApplyInProgress(EpistrelError):
    """An action on a session whose apply is running (#224): 409."""

    code = "rewrite_apply_in_progress"
    http_status = 409


class RewriteApplyFailed(EpistrelError):
    """A rewrite's apply failed transiently and wrote nothing (#224): 503, ``retryable``. The
    session is back in ``awaiting_approval`` with ``last_error``."""

    code = "rewrite_apply_failed"
    http_status = 503


class RecordInFlight(EpistrelError):
    """An apply would correct a record of a staged turn that has not completed (#224, #212):
    409. ``details``: ``record_table`` and ``record_id``."""

    code = "record_in_flight"
    http_status = 409


class IntentNotRetconnable(EpistrelError):
    """A part other than an explicit intent change touched an utterance's intent (#225,
    FR-DIRECT-8): a bug guard, never expected (500)."""

    code = "intent_not_retconnable"
    http_status = 500


class MergeInvalid(EpistrelError):
    """An identity merge that cannot be (#225): fewer than two entities, one not of the story,
    entities of different kinds, or entities already one referent. ``details``: ``reason``
    (``too_few``, ``not_in_story``, ``type_mismatch``, ``already_same``) and ``entity_ids``."""

    code = "merge_invalid"
    http_status = 422


class DirectiveUnparseable(EpistrelError):
    """A directive's text could not be parsed (#226): 422; nothing is written."""

    code = "directive_unparseable"
    http_status = 422


class DirectiveRequiresRewrite(EpistrelError):
    """A Lite directive about the past (#244): a history rewrite, not a directive, until #245
    routes it; ``details.rewrite_route``. Nothing is written."""

    code = "directive_requires_rewrite"
    http_status = 422


class DirectiveNoChanges(EpistrelError):
    """A durable directive parsed into no change (#226): 422; nothing is written."""

    code = "directive_no_changes"
    http_status = 422


class DirectiveAmbiguous(EpistrelError):
    """A directive names something the engine cannot pin down (#226, #241, #242): 422.
    ``details``: ``candidates``."""

    code = "directive_ambiguous"
    http_status = 422


class AuthorityConflict(EpistrelError):
    """A write of a lower authority tier would supersede a fact a higher tier holds (#226,
    FR-DIRECT-6): narrative canon never supersedes the author's locked canon. Internal: the
    verifier reads the claim as contradicted (409 if it ever surfaces)."""

    code = "authority_conflict"
    http_status = 409


class StateNotAmendable(EpistrelError):
    """A State proposition part on a State an amendment may not correct (#236): the
    ``world_clock`` (clock advances are corrected with forward directives, the owner's decision
    3), a derived State, a transfer-conserved State, an externally owned State or a discarded
    definition. ``details``: ``ordinal``, ``state`` and ``reason`` (``clock``, ``derived``,
    ``transfer_conserved``, ``external``, ``discarded``). Nothing is written."""

    code = "state_not_amendable"
    http_status = 422


class AmendmentAlreadyReverted(EpistrelError):
    """The amendment was reverted already (#221, #238): ``details``: ``amendment_id``,
    ``reverted_by``."""

    code = "amendment_already_reverted"
    http_status = 409


class ExternalValueNotOverridable(EpistrelError):
    """An author's change to a value a game's provider owns (#242, FR-EXT-8): 422.
    ``details``: ``changes`` (``[{provider, value_name, scope_key}]``) and ``explanation``."""

    code = "external_value_not_overridable"
    http_status = 422


class AmendmentNotFound(EpistrelError):
    """No such amendment in the story (#238): 404. ``details``: ``amendment_id``."""

    code = "amendment_not_found"
    http_status = 404


class AmendmentNotReverted(EpistrelError):
    """A redo of an amendment that is still active (#238): 409. ``details``:
    ``amendment_id``."""

    code = "amendment_not_reverted"
    http_status = 409


class AmendmentAlreadyRedone(EpistrelError):
    """A redo while an active amendment already redoes it (#238): 409. ``details``:
    ``amendment_id``, ``redo_id``."""

    code = "amendment_already_redone"
    http_status = 409


class UseRedo(EpistrelError):
    """An undo of an ``undo`` amendment (#238): 422; redo the amendment it undid instead.
    ``details``: ``amendment_id``, ``undoes``."""

    code = "use_redo"
    http_status = 422


class CertificationNotFound(EpistrelError):
    """No certification record has this id (#233)."""

    code = "certification_not_found"
    http_status = 404


class CertificationNotValid(EpistrelError):
    """Only a ``valid`` certification can be invalidated (#234)."""

    code = "certification_not_valid"
    http_status = 409


class CertificationRunNotFound(EpistrelError):
    """No certification run has this id (#233)."""

    code = "certification_run_not_found"
    http_status = 404


class InvalidCertificationRecord(EpistrelError):
    """A certification record does not validate (#233): ``details``: ``reason``
    (``schema``, ``report_hash``, ``report_missing``) and, for ``schema``, the ``errors``."""

    code = "invalid_certification_record"
    http_status = 422


class CertificationRecordConflict(EpistrelError):
    """A certification record with this id is stored with different content (#233):
    ``details``: ``certification_id``."""

    code = "certification_record_conflict"
    http_status = 409


class CertificationRunInProgress(EpistrelError):
    """A certification run for this fingerprint is queued or running (#233): ``details``:
    ``run_id``, ``fingerprint``."""

    code = "certification_run_in_progress"
    http_status = 409


class CertificationUnavailable(EpistrelError):
    """This process has no eval harness installed, so it cannot estimate or run a
    certification (#233)."""

    code = "certification_unavailable"
    http_status = 503


class BackfillNotFound(EpistrelError):
    """The story has no backfill (or none running, for a cancel) (#228)."""

    code = "backfill_not_found"
    http_status = 404


class BackfillNotCancellable(EpistrelError):
    """The story's latest backfill has ended, so it cannot be cancelled (#228): ``details``:
    ``backfill_id``, ``status``."""

    code = "backfill_not_cancellable"
    http_status = 409


class BackfillInProgress(EpistrelError):
    """A backfill of the story is running (#228): ``details``: ``backfill_id``, ``status``."""

    code = "backfill_in_progress"
    http_status = 409


class NothingToBackfill(EpistrelError):
    """Every capability the story has on already covers its turns (#228)."""

    code = "nothing_to_backfill"
    http_status = 409


class BackfillConflictNotFound(EpistrelError):
    """No conflict of that id belongs to the story's backfill (#230)."""

    code = "backfill_conflict_not_found"
    http_status = 404


class BackfillConflictClosed(EpistrelError):
    """The conflict is not open: resolved, or left uncertain (#230): ``details``:
    ``conflict_id``, ``status``."""

    code = "backfill_conflict_closed"
    http_status = 409


class BackfillNotAwaitingConflicts(EpistrelError):
    """The story's backfill is not paused for conflicts (#230): ``details``: ``backfill_id``,
    ``status``."""

    code = "backfill_not_awaiting_conflicts"
    http_status = 409


class InvalidResolution(EpistrelError):
    """A conflict resolution names no statement of the conflict, or a value that does not
    normalize for its predicate (#230)."""

    code = "invalid_resolution"
    http_status = 422


class CheckpointNotFound(EpistrelError):
    """No committed checkpoint is at that turn, slot or variant (#222)."""

    code = "checkpoint_not_found"
    http_status = 404


class CheckpointIncomplete(EpistrelError):
    """The checkpoint is a staged turn still under way (#222): ``details``: ``turn``,
    ``turn_stage``."""

    code = "checkpoint_incomplete"
    http_status = 409


class InvalidView(EpistrelError):
    """An unknown history view (#222): ``details``: ``allowed``."""

    code = "invalid_view"
    http_status = 422


class EvalRunNotFound(EpistrelError):
    """No eval run has that id (#532)."""

    code = "eval_run_not_found"
    http_status = 404
