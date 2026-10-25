"""The epistemic model's errors (#159; #160 adds its own), under ``EpistrelError``."""

from __future__ import annotations

from epistrel.core.errors import EpistrelError


class EpistemicError(EpistrelError):
    """The base of the epistemic model's refusals."""

    code = "epistemic_error"
    http_status = 422


class HolderHasNoPartition(EpistemicError):
    """A belief written for a character the engine keeps no partition for: the player's own
    character (FR-EPI-12: no engine-held interiority), or a name that is no character of the
    story (``details``: ``story_id``, ``holder``). Nothing is written."""

    code = "holder_has_no_partition"
    http_status = 422


class SelfAttestation(EpistemicError):
    """A belief transition whose antecedent does not precede the dependent step in (turn, slot,
    beat, step) order: the step would establish its own cause (FR-EPI-13,14; #160).
    ``details``: ``antecedent`` (its kind and reference) and both positions. Nothing is
    written; the render path turns it into a hallucination and repairs (#166)."""

    code = "self_attestation"
    http_status = 422


class AntecedentInvalid(EpistemicError):
    """A belief transition's antecedent is not a legitimate cause (FR-EPI-7,14; #160): it does
    not exist, is not committed or not on the timeline, is in another variant of the turn, is
    the wrong kind of record for the transition's kind, or the oracle cannot verify it.
    ``details``: ``reason`` and the antecedent. Nothing is written."""

    code = "antecedent_invalid"
    http_status = 422


class ConfidenceInconsistent(EpistemicError):
    """A belief's categorical level and scalar contradict each other: the scalar projects onto
    another level (FR-BELIEF-4; #160). ``details``: ``confidence_level``, ``confidence_scalar``
    and ``projected``. Nothing is written."""

    code = "confidence_inconsistent"
    http_status = 422


__all__ = [
    "AntecedentInvalid",
    "ConfidenceInconsistent",
    "EpistemicError",
    "HolderHasNoPartition",
    "SelfAttestation",
]
