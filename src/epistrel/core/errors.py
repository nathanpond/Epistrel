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


class StoryBusy(EpistrelError):
    """The story has work in flight that the call would race (#50: archive, unarchive and
    delete wait for a running turn; #63: a second turn under ``turn_concurrency = reject``, or
    a full queue under ``queue``, FR-CONC-5).

    ``details``: ``reason`` (``in_flight`` or ``queue_full``), ``operation_id`` (the turn
    holding the story, when one does) and, for turn concurrency, ``queue_depth``. A turn
    refusal carries ``Retry-After``: the seconds left on the running turn's lease.
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


class DraftStale(EpistrelError):
    """The ``draft_revision`` a save or publish was based on is no longer the draft's
    (FR-CAT-3): another editor saved or published since. Nothing is written; re-read the draft
    and retry from its revision. ``details``: ``character_id``, ``draft_revision`` (sent) and
    ``current_draft_revision``."""

    code = "draft_stale"
    http_status = 409


class DraftTooLarge(EpistrelError):
    """The draft, after the patch is applied, is larger than the cap (256 KiB of canonical
    JSON). Nothing is written. ``details``: ``size`` and ``limit`` (bytes)."""

    code = "draft_too_large"
    http_status = 413


class EntryArchived(EpistrelError):
    """The catalog entry is archived: its draft cannot be saved or published until it is
    unarchived (reads still work). Nothing is written. ``details``: ``character_id``."""

    code = "entry_archived"
    http_status = 422


class NotPublished(EpistrelError):
    """A read of the published character before its first publish: read the draft
    (``?draft=true``) instead. ``details``: ``character_id``."""

    code = "not_published"
    http_status = 422


class PublishInvalid(EpistrelError):
    """The draft cannot be published as it stands. Nothing is written. ``details.report``:
    ``{errors, warnings, unavailable}``, each error ``{code, field, message}`` with ``field`` a
    dotted path in the draft (``name``, ``definition.personality``)."""

    code = "publish_invalid"
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
    """A companion scenario's owner and relations follow its character (FR-AUTH-5): change them
    on the character, and they change together. Nothing is written. ``details``:
    ``entity_type``, ``entity_id`` and ``character_id``."""

    code = "companion_follows_character"
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
