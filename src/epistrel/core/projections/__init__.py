"""Projections: caches over the event log, kept in the commit and rebuilt on demand (FR-STORE-4).

The M2 set is ``transcript_messages`` and ``story_heads``. Each projection is registered with
``register_projection`` (#60 and #72 add theirs); ``apply_events`` maintains them inside
``Operation.commit()``, and ``rebuild`` refolds them from the log.
"""

from epistrel.core.errors import StoryNotFound
from epistrel.core.projections.base import (
    LogEvent,
    Projection,
    Rows,
    register_projection,
    registered_projections,
)
from epistrel.core.projections.fold import (
    RebuildResult,
    apply_events,
    fold,
    rebuild,
    replace_with_fold,
)
from epistrel.core.projections.heads import HeadsProjection
from epistrel.core.projections.transcript import TranscriptProjection

TRANSCRIPT = register_projection(TranscriptProjection())
HEADS = register_projection(HeadsProjection())

__all__ = [
    "HEADS",
    "TRANSCRIPT",
    "LogEvent",
    "Projection",
    "RebuildResult",
    "Rows",
    "StoryNotFound",
    "apply_events",
    "fold",
    "rebuild",
    "register_projection",
    "registered_projections",
    "replace_with_fold",
]
