"""Projections: caches over the event log, kept in the commit and rebuilt on demand (FR-STORE-4).

The M2 set is ``transcript_messages``, ``story_heads``, ``story_texts`` (#60) and
``lite_summary_segments`` (#72). Each projection is registered with ``register_projection``;
``apply_events`` maintains them inside ``Operation.commit()``, and ``rebuild`` refolds them
from the log.
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
from epistrel.core.projections.lite_summary import LiteSummaryProjection
from epistrel.core.projections.story_texts import StoryTextsProjection
from epistrel.core.projections.transcript import TranscriptProjection

TRANSCRIPT = register_projection(TranscriptProjection())
HEADS = register_projection(HeadsProjection())
STORY_TEXTS = register_projection(StoryTextsProjection())
LITE_SUMMARY = register_projection(LiteSummaryProjection())

__all__ = [
    "HEADS",
    "LITE_SUMMARY",
    "STORY_TEXTS",
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
