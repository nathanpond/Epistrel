"""Projections: caches over the event log, kept in the commit and rebuilt on demand (FR-STORE-4).

The M2 set is ``transcript_messages``, ``story_heads``, ``story_texts`` (#60) and
``lite_summary_segments`` (#72); M4 adds the structured projections over canon,
``relationships``, ``placement`` and ``scene_state`` (#137, ``structured``); M5 the belief
projections over the partitions, ``belief_state`` (#159, ``beliefs``) and ``nested_belief_state``
(#167). Each projection
is registered with ``register_projection``; ``apply_events`` maintains them inside
``Operation.commit()``, and ``rebuild`` refolds them from the log (the structured ones from
canon).
"""

from epistrel.core.errors import StoryNotFound
from epistrel.core.projections.base import (
    LogEvent,
    Projection,
    Rows,
    register_projection,
    registered_projections,
)
from epistrel.core.projections.beliefs import BeliefStateProjection, NestedBeliefStateProjection
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
from epistrel.core.projections.structured import (
    PlacementProjection,
    RelationshipsProjection,
    SceneStateProjection,
)
from epistrel.core.projections.transcript import TranscriptProjection

TRANSCRIPT = register_projection(TranscriptProjection())
HEADS = register_projection(HeadsProjection())
STORY_TEXTS = register_projection(StoryTextsProjection())
LITE_SUMMARY = register_projection(LiteSummaryProjection())
RELATIONSHIPS = register_projection(RelationshipsProjection())
PLACEMENT = register_projection(PlacementProjection())
SCENE_STATE = register_projection(SceneStateProjection())
STRUCTURED = (RELATIONSHIPS, PLACEMENT, SCENE_STATE)
"""The structured projections (#137), folded from canon; a fork refolds them."""
BELIEF_STATE = register_projection(BeliefStateProjection())
"""The belief projection (#159), folded from the story's beliefs."""
NESTED_BELIEF_STATE = register_projection(NestedBeliefStateProjection())
"""The nested-belief projection (#167), folded from the story's nested beliefs."""
STORED_STATE = (*STRUCTURED, BELIEF_STATE, NESTED_BELIEF_STATE)
"""The projections folded from stored state rather than the log; a fork refolds them."""

__all__ = [
    "BELIEF_STATE",
    "HEADS",
    "LITE_SUMMARY",
    "NESTED_BELIEF_STATE",
    "PLACEMENT",
    "RELATIONSHIPS",
    "SCENE_STATE",
    "STORED_STATE",
    "STORY_TEXTS",
    "STRUCTURED",
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
