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


__all__ = ["EpistemicError", "HolderHasNoPartition"]
